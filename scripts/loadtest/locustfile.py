"""线 B 压测场景（P3.3，ADR-011）：fake 服务端（ARGUS_FAKE_LLM=1）+ locust。

先跑 seed_loadtest.py 产出 users.json，再（仓根）：

    uv run locust -f scripts/loadtest/locustfile.py --headless -u 20 -r 5 -t 3m --csv scripts/loadtest/out/fake-u20

口径（详见 scripts/loadtest/README.md）：
- 每个虚拟用户绑定一个 seed 账号：-u 不要超过 seed 的 --users 数，
  单机单 locust 进程（多进程会复用账号，per-user 限流/SSE 槽被摊薄）；
- SSE 端点用自定义计时条目：chat first-delta / chat e2e /
  research e2e (incl. queue)，在 locust 报表里与普通请求并列；
- 429 是配额/限流生效，不算失败：计成功并打 throttled 计数条目；
- 不打 /healthz：它有 per-IP 限流（防压测放大器），压它只能测出 429。
"""

import itertools
import json
import random
import sys
import time
from pathlib import Path
from uuid import uuid4

from locust import HttpUser, between, events, task

_HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(_HERE))  # locust -f 加载不保证脚本目录进 sys.path

_USERS_PATH = _HERE / "users.json"
try:
    USERS = json.loads(_USERS_PATH.read_text(encoding="utf-8"))
except FileNotFoundError:
    sys.exit("users.json 不存在：先跑 uv run python scripts/loadtest/seed_loadtest.py")

_counter = itertools.count()

REDIS_URL = "redis://localhost:6379/0"  # dev 压测本机口径，与服务端一致


def _clear_sse_gate() -> None:
    """清 SseGate 槽位计数器（压测工具的 setup/teardown）。

    压测到时是硬切：in-flight 的 SSE 流没走到 release，槽位悬空（服务端
    TTL 30min 自愈）。不清理的话，上一档的残留会把下一档前排账号整场锁死
    ——首轮基线实测抓获的跨档污染，429 计数与吞吐全被带歪。"""
    from redis import Redis  # 项目依赖自带 redis-py，延迟 import 与 pdfgen 同理

    client = Redis.from_url(REDIS_URL)
    try:
        keys = list(client.scan_iter("sse:conc:*"))
        if keys:
            client.delete(*keys)
    finally:
        client.close()


@events.test_start.add_listener
def _on_test_start(environment, **kwargs) -> None:
    _clear_sse_gate()


@events.test_stop.add_listener
def _on_test_stop(environment, **kwargs) -> None:
    _clear_sse_gate()


QUESTIONS = [
    "公司最近一期的营业收入和毛利率表现如何？",
    "主营业务由哪些板块构成？各自占比多少？",
    "报告里提示了哪些主要经营风险？",
    "经营活动现金流和负债结构情况怎么样？",
]

RESEARCH_CAP_S = 900  # fake 线研究 e2e 硬帽：worker 串行下队尾任务也该在此之前完成


def _fire(name: str, seconds: float, exc: Exception | None = None) -> None:
    events.request.fire(
        request_type="SSE",
        name=name,
        response_time=seconds * 1000,
        response_length=0,
        exception=exc,
        context={},
    )


def _pdf_bytes(nonce: str) -> bytes:
    from pdfgen import make_pdf  # 延迟 import：sys.path 补丁生效之后

    return make_pdf(
        [
            "Argus loadtest synthetic upload",
            f"nonce {nonce}",  # 每次内容不同：绕开 (company_id, sha256) 去重
            "Revenue 2024: 1,234 million CNY, up 12 percent.",
            "Gross margin stable at 38 percent.",
        ]
    )


class ArgusUser(HttpUser):
    host = "http://localhost:8000"
    wait_time = between(2, 6)

    def on_start(self) -> None:
        cred = USERS[next(_counter) % len(USERS)]
        self.client.headers["Authorization"] = f"Bearer {cred['token']}"
        self.company_id = cred["company_id"]

    # ---- 读路径（无限流，承载力主战场） ----

    @task(6)
    def list_documents(self) -> None:
        self.client.get(
            f"/api/companies/{self.company_id}/documents", name="GET documents"
        )

    @task(3)
    def list_companies(self) -> None:
        self.client.get("/api/companies", name="GET companies")

    @task(3)
    def list_messages(self) -> None:
        self.client.get(
            f"/api/companies/{self.company_id}/messages", name="GET messages"
        )

    @task(2)
    def list_research(self) -> None:
        self.client.get(
            f"/api/companies/{self.company_id}/research", name="GET research list"
        )

    # ---- 核心流（SSE + worker） ----

    @task(4)
    def chat_turn(self) -> None:
        question = random.choice(QUESTIONS)
        start = time.perf_counter()
        with self.client.post(
            f"/api/companies/{self.company_id}/chat",
            json={"content": question},
            stream=True,
            catch_response=True,
            name="POST chat [sse]",
            timeout=(5, 150),  # 服务端块间超时 120s，读超时留余量
        ) as resp:
            if resp.status_code == 429:
                _fire("throttled 429 (chat)", 0.0)
                resp.success()
                return
            if resp.status_code != 200:
                resp.failure(f"HTTP {resp.status_code}")
                return
            first = None
            outcome = None
            done_at = 0.0
            # 读到服务端关流为止（与前端 consumeSse 同款），不在业务 done 上提前
            # 断开：提前断开会触发服务端 SseGate 槽位泄漏（P3.3 压测抓获的 bug，
            # done 后服务端紧接关流，多读的尾巴只有毫秒级）
            for raw in resp.iter_lines():
                if not raw or not raw.startswith(b"data: "):
                    continue
                if first is None:
                    first = time.perf_counter() - start
                    _fire("chat first-delta", first)
                event = json.loads(raw[len(b"data: ") :])
                if event.get("type") in ("done", "error") and outcome is None:
                    outcome = event["type"]
                    done_at = time.perf_counter() - start
            if outcome == "done":
                _fire("chat e2e", done_at)
                resp.success()
            else:
                resp.failure(f"SSE ended with {outcome!r}")

    @task(1)
    def research_e2e(self) -> None:
        start = time.perf_counter()
        with self.client.post(
            f"/api/companies/{self.company_id}/research",
            catch_response=True,
            name="POST research",
        ) as resp:
            if resp.status_code == 429:
                # 预算闸 / 并发槽（max_running_research）都走这里
                _fire("throttled 429 (research)", 0.0)
                resp.success()
                return
            if resp.status_code != 201:
                resp.failure(f"HTTP {resp.status_code}")
                return
            resp.success()
            task_id = resp.json()["id"]

        with self.client.get(
            f"/api/research/{task_id}/events",
            stream=True,
            catch_response=True,
            name="GET research events [sse]",
            timeout=(5, 60),  # 服务端 XREAD block 15s：keepalive 间隔远小于 60
        ) as resp:
            if resp.status_code == 429:
                _fire("throttled 429 (sse-slot)", 0.0)
                resp.success()
                return
            if resp.status_code != 200:
                resp.failure(f"HTTP {resp.status_code}")
                return
            outcome = None
            deadline = start + RESEARCH_CAP_S
            for raw in resp.iter_lines():
                if time.perf_counter() > deadline:
                    break  # 逃生断开（服务端修复 BackgroundTask 前会泄 1 槽，接受）
                if not raw or not raw.startswith(b"data: "):
                    continue  # keepalive 注释行
                event = json.loads(raw[len(b"data: ") :])
                if event.get("node") in ("done", "failed"):
                    outcome = event["node"]
                    # 不 break：服务端读到终态自己关流，循环随之自然耗尽
            if outcome == "done":
                # 含排队时间是刻意口径：worker max_jobs=1 的串行瓶颈就要在这条曲线上现形
                _fire("research e2e (incl. queue)", time.perf_counter() - start)
                resp.success()
            elif outcome == "failed":
                resp.failure("research failed")
            else:
                resp.failure(f"research not finished in {RESEARCH_CAP_S}s")

    @task(1)
    def upload_document(self) -> None:
        nonce = uuid4().hex
        pdf = _pdf_bytes(nonce)
        with self.client.post(
            f"/api/companies/{self.company_id}/documents",
            files={"file": (f"lt-{nonce[:8]}.pdf", pdf, "application/pdf")},
            catch_response=True,
            name="POST upload",
        ) as resp:
            if resp.status_code == 429:
                _fire("throttled 429 (upload)", 0.0)
                resp.success()
            elif resp.status_code == 201:
                resp.success()
            else:
                resp.failure(f"HTTP {resp.status_code}")
