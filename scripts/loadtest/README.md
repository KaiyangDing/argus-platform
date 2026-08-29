# 压测（P3.3 基线）操作手册

两线口径见 docs/adr/011：**线 A** 真模型小样本拿真实体验数字，**线 B**
fake 模型 + locust 并发压自家壳层（HTTP / PG / Redis / MinIO / worker
编排 / SSE）拿 QPS 与 P95/P99。结果数字进仓根 README 压测节。

## 前置（一次性）

    uv add --group dev locust

源码侧 fake 开关（`ARGUS_FAKE_LLM`）已在 app/config.py、app/fakes.py、
app/llm.py、app/ingest.py 就位；`uv run pytest tests/test_fakes.py` 全绿
说明 fake 线自洽。

## 线 B：fake 压壳

1. 两个终端起 **fake 服务**（PowerShell；压测不要带 `--reload`，热重载
   监视有额外开销，数字失真）：

       $env:ARGUS_FAKE_LLM = "1"
       uv run uvicorn app.main:app --port 8000

       $env:ARGUS_FAKE_LLM = "1"
       uv run arq app.worker.WorkerSettings

   可选 `$env:ARGUS_FAKE_LLM_DELAY_S = "0"`：去掉模拟时延，拿纯吞吐
   上限（默认 0.5s/调用，用于逼近真实任务时长与队列形态）。

2. 铺数据（幂等可重跑）：

       uv run python scripts/loadtest/seed_loadtest.py --users 20

3. 压（-u 不超过 seed 用户数；建议 10 / 20 / 40 三档，每档取同时长）：

       uv run locust -f scripts/loadtest/locustfile.py --headless -u 20 -r 5 -t 3m --csv scripts/loadtest/out/fake-u20

4. 看数：`out/fake-u20_stats.csv` 每端点一行（RPS、P50/P95/P99）。
   自定义条目：`chat first-delta`、`chat e2e`、`research e2e (incl. queue)`
   是 SSE 计时；`throttled 429 (...)` 是限流/配额命中计数（**是设计内
   行为的证明，不算失败**）。

压完复原：`Remove-Item Env:ARGUS_FAKE_LLM`（两个终端都要，或直接关掉重开）。

## 线 A：真跑基线

⚠️ 花真钱（研究约 ¥1~1.5/次，默认不跑研究）。用真实语料的公司，
服务端不设 `ARGUS_FAKE_LLM`：

    uv run python scripts/loadtest/real_baseline.py --email you@example.com --password *** --company-id <uuid> --research

## 档间纪律（多档连跑必读）

- **等队列排空再开下一档**：上一档排进 arq 的 research/ingest 任务会把下一
  档的 research e2e 数字整体抬高（首轮基线实测：残队把 u10 的 P50 从 12s
  抬到 73s）。检查（两个都为 0 才开跑）：

      docker compose exec redis redis-cli ZCARD arq:queue
      docker compose exec postgres psql -U argus -d argus -c "SELECT count(*) FROM research_tasks WHERE status IN ('queued','running')"

- SSE 槽残留由 locustfile 的 test_start/test_stop 钩子自动清理。
- SSE 消费读到**服务端关流**为止（与前端 consumeSse 同款），不在业务 done
  上提前断开——提前断开触发 SseGate 泄漏 bug（见下），数字全被带歪。

## SseGate 泄漏验证器

probe_sse_leak.py 用「done 即断开」姿势打 8 次 chat：泄漏时第 6 次起 429，
修复后 8/8 通过。服务端 BackgroundTask 修复落地后跑它验收：

    uv run python scripts/loadtest/probe_sse_leak.py

## 口径与注意

- 线 B 数字不含真模型时延，只证壳层承载力；对外两线并列表述。
- seed 公司索引是 fake 向量，只在 fake 服务端下有意义；真模型别问它们。
- upload 任务会持续加深 worker 队列、增大索引——设计内负载；长跑后想
  回到干净基线，重跑 seed 不会清理，直接重建 dev 库/桶最快。
- locust 单机单进程；多进程 worker 会复用账号，per-user 限流与 SSE
  槽位被摊薄，数字口径就变了。
- 不压 /healthz（per-IP 限流防压测放大器，压它只能测出 429）。
- 预期基线瓶颈（跑完对照）：research e2e 随并发线性恶化 = worker
  max_jobs=1 全局串行；chat P95 里索引重载占比 = 每消息重载索引。
  两者处方都在 P3.4（pgvector + 放开并发），复测同参数对比。
