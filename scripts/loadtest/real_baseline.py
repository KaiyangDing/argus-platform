"""线 A：小样本真跑基线（P3.3，ADR-011）——真 LLM、单用户顺序请求，
拿真实体验数字（chat 首字 / 全程、研究端到端）。

⚠️ 花真钱：一次研究约 ¥1~1.5、一轮追问几厘到几分钱。研究默认不跑，
--research 才跑。对象用你 dev 里真实语料的公司（已有 ready 文档那家），
不要用 seed 的压测公司（其索引是 fake 向量，真模型检索无意义）。
服务端保持 ARGUS_FAKE_LLM 未设。

    uv run python scripts/loadtest/real_baseline.py --email you@example.com --password *** --company-id <uuid> [--chats 3] [--research]

输出 JSON 打到 stdout 并写 scripts/loadtest/out/real_baseline.json，
数字进 README 压测节（线 A 列）。
"""

import argparse
import json
import time
from collections.abc import Iterable, Iterator
from pathlib import Path

import httpx

OUT_PATH = Path(__file__).resolve().parent / "out" / "real_baseline.json"

QUESTIONS = [
    "公司最近一期的营业收入和毛利率表现如何？",
    "主营业务由哪些部分构成？占比怎样？",
    "报告里提示了哪些主要经营风险？",
]


def _sse_events(lines: Iterable[str]) -> Iterator[dict]:
    for line in lines:
        if line.startswith("data: "):
            yield json.loads(line[len("data: ") :])


def chat_round(client: httpx.Client, company_id: str, question: str) -> dict:
    start = time.perf_counter()
    first: float | None = None
    chars = 0
    outcome = "incomplete"
    total = 0.0
    with client.stream(
        "POST", f"/api/companies/{company_id}/chat", json={"content": question}
    ) as resp:
        resp.raise_for_status()
        # 读到服务端关流为止（与前端 consumeSse 同款）：done 上提前断开会触发
        # 服务端 SseGate 槽位泄漏（P3.3 压测抓获的 bug），聊 5 轮就把自己锁死
        for event in _sse_events(resp.iter_lines()):
            if event.get("type") == "delta":
                if first is None:
                    first = time.perf_counter() - start
                chars += len(event.get("text", ""))
            elif event.get("type") in ("done", "error") and outcome == "incomplete":
                outcome = event["type"]
                total = time.perf_counter() - start
    return {
        "question": question,
        "first_delta_s": round(first, 2) if first is not None else None,
        "total_s": round(total or (time.perf_counter() - start), 2),
        "answer_chars": chars,
        "outcome": outcome,
    }


def research_round(client: httpx.Client, company_id: str) -> dict:
    start = time.perf_counter()
    resp = client.post(f"/api/companies/{company_id}/research")
    resp.raise_for_status()
    task_id = resp.json()["id"]
    outcome = "incomplete"
    seen_events = 0
    with client.stream("GET", f"/api/research/{task_id}/events") as events:
        events.raise_for_status()
        for event in _sse_events(events.iter_lines()):
            seen_events += 1
            if event.get("node") in ("done", "failed"):
                outcome = event["node"]
                # 不 break：服务端读到终态自己关流，循环随之自然耗尽
    return {
        "task_id": task_id,
        "e2e_s": round(time.perf_counter() - start, 1),
        "events": seen_events,
        "outcome": outcome,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="线 A 真跑基线（花真钱，见文件头）")
    parser.add_argument("--host", default="http://localhost:8000")
    parser.add_argument("--email", required=True)
    parser.add_argument("--password", required=True)
    parser.add_argument("--company-id", required=True)
    parser.add_argument("--chats", type=int, default=3)
    parser.add_argument("--research", action="store_true")
    args = parser.parse_args()

    # read 超时按 SSE 静默上限留量：chat 块间 120s、研究事件间 keepalive 15s
    with httpx.Client(
        base_url=args.host, timeout=httpx.Timeout(10.0, read=300.0)
    ) as client:
        resp = client.post(
            "/api/auth/login", json={"email": args.email, "password": args.password}
        )
        resp.raise_for_status()
        client.headers["Authorization"] = f"Bearer {resp.json()['access_token']}"

        chats = [
            chat_round(client, args.company_id, QUESTIONS[i % len(QUESTIONS)])
            for i in range(args.chats)
        ]
        research = research_round(client, args.company_id) if args.research else None
        # 长研究会跑过 access token 的 15 分钟 TTL：usage 快照前重新登录
        resp = client.post(
            "/api/auth/login", json={"email": args.email, "password": args.password}
        )
        resp.raise_for_status()
        client.headers["Authorization"] = f"Bearer {resp.json()['access_token']}"
        usage_after = client.get("/api/usage").json()  # 真实花费佐证一并归档

    result = {
        "host": args.host,
        "ran_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "chat_rounds": chats,
        "research": research,
        "usage_after": usage_after,
    }
    out = json.dumps(result, ensure_ascii=False, indent=2)
    print(out)
    OUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    OUT_PATH.write_text(out, encoding="utf-8")
    print(f"\nwritten -> {OUT_PATH}")


if __name__ == "__main__":
    main()
