"""SseGate 槽位泄漏验证器（P3.3 压测抓获的 bug 的最小复现）。

模拟「读到业务 done 立刻断开」的客户端，同一账号顺序打 8 次 chat：
- 服务端有泄漏 bug：每次断开泄 1 槽，第 6 次起 429（5 槽满，锁 30 分钟）；
- 修复后（release 挪 StreamingResponse BackgroundTask）：8 次全 200。

前置：fake 服务端在跑（省钱），users.json 已 seed。跑法（仓根）：

    uv run python scripts/loadtest/probe_sse_leak.py
"""

import json
import sys
from pathlib import Path

import requests

HERE = Path(__file__).resolve().parent


def main() -> None:
    users = json.loads((HERE / "users.json").read_text(encoding="utf-8"))
    cred = users[0]
    session = requests.Session()
    session.headers["Authorization"] = f"Bearer {cred['token']}"
    url = f"http://localhost:8000/api/companies/{cred['company_id']}/chat"

    statuses: list[int] = []
    for i in range(8):
        with session.post(
            url, json={"content": "泄漏探针"}, stream=True, timeout=(5, 30)
        ) as resp:
            statuses.append(resp.status_code)
            if resp.status_code != 200:
                print(f"{i}: {resp.status_code} {resp.text[:80]}")
                continue
            for raw in resp.iter_lines():
                if raw and raw.startswith(b"data: "):
                    event = json.loads(raw[len(b"data: ") :])
                    if event.get("type") in ("done", "error"):
                        break  # 关键：不读到流尾，立刻断开（bug 触发姿势）
            print(f"{i}: 200 (broke off after done)")

    throttled = statuses.count(429)
    if throttled:
        print(f"\nLEAK: {throttled}/8 requests hit 429 — SseGate 槽位在断开路径泄漏")
        sys.exit(1)
    print("\nCLEAN: 8/8 requests passed — 断开路径正常还槽")


if __name__ == "__main__":
    main()
