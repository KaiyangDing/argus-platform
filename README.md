# Argus Platform

企业尽调研究平台：建公司 → 上传 PDF → 异步解析入库 → 多 Agent 生成带引用的
尽调报告，并可对报告追问对话（回答仍检索同一语料、仍带引用）。

本仓是平台壳（API / 队列 / 存储 / 前端）+ 自含的语料管线与研究图实现。
研究设计在同级 argus-lg 研究仓先行验证，本仓按**研究结论**全量重写引入
（spike-then-rewrite，不共享代码，见 docs/adr/005、006）；当前实现对齐
argus-lg v0.2：多轮 map-reduce researcher、复审补派、三层研报、章节面包屑。

## 架构

    browser ──► Vite dev (5173) ──proxy──► FastAPI app (8000) ──┬─► PostgreSQL 16 + pgvector
                                               │                ├─► Redis（队列 / 事件流）
                                               │                └─► MinIO（PDF 原件）
                                          arq worker ◄─── Redis 队列
                                               │
                                               └── 语料管线 / 研究图（产品内实现，设计源自研究仓 argus-lg）

模块化单体：app 与 worker 同一代码库、同一镜像、不同启动命令（见 docs/adr/）。

## 怎么跑（dev）

前置：Docker Desktop、uv、Python 3.14、Node 20+。

起基础设施（PG / Redis / MinIO）：

    docker compose up -d

起后端（http://localhost:8000，API 文档在 /docs）：

    uv run uvicorn app.main:app --reload

起 worker（异步入库执行者，另开终端）：

    uv run arq app.worker.WorkerSettings

起前端（http://localhost:5173）：

    cd frontend
    npm install
    npm run dev

探活：页面首页即三依赖状态；或 curl http://localhost:8000/healthz

## 测试

    uv run pytest

## 压测（P3.3 基线）

两线口径（ADR-011；工具与手册在 scripts/loadtest/）：**线 A** 真模型单用户
小样本记真实体验；**线 B** `ARGUS_FAKE_LLM=1` 全 fake（0.5s/调用模拟时延）
+ locust 并发压自家壳层。单机全家桶（压测器 / app / worker / PG / Redis /
MinIO 同机），读路径尾部含资源争抢的间接效应。

线 A（qwen-flash 真跑，研究走含复审补派的完整路径）：chat 首字 6.5~7.6s、
全程约 8s、每轮 ≈¥0.001；研究端到端 20.8 分钟、¥0.236（445k in /
113k out，记账 missing_calls=0）。

线 B（每档 3 分钟；429=0，失败率 ≤0.4% 且均为档尾硬切竞态）：

| 指标 | u10 | u20 | u40 |
|---|---|---|---|
| 总吞吐 req/s | 3.1 | 6.2 | 7.0 |
| 读路径 P50 / P95 | 6~12 / 18~42 ms | 7~11 / 21~32 ms | 9~13 ms / 3.8~13 s |
| chat 首 delta P50 / P95 | 1.1 / 1.2 s | 1.1 / 1.2 s | 1.1 / 9.8 s |
| research e2e P50 / max | 12 / 26 s | 17 / 32 s | 61 / 107 s |

基线定位（处方均排 P3.4，复测同参数对比）：

- **worker 串行**（max_jobs=1）：research 纯执行 ≈6.5s（三档 min 一致），
  P50 12→17→61s 全是排队；用户 u20→u40 翻倍吞吐仅 +13%，饱和点在
  u20~u40 之间。
- **每请求全量重载 per-company 索引**：u40 下 chat 尾部与读路径 P90+
  秒级劣化而 P50 不动——CPU / 线程池争抢的指纹（pgvector 迁移后消失）。
- 三道闸（HTTP 频率 / 业务配额 / SSE 并发）在并发下全部按设计生效。

压测战果（两个真问题，比数字值钱）：

1. **SseGate 槽位泄漏 bug**：客户端在流终止前断开（关页面 / 刷新 / 压测
   硬切）时，release 挂在 async generator 的 finally 上不被及时执行——
   5 次断开即把该用户 429 锁 30 分钟（TTL 自愈）。修复 = release 挪
   StreamingResponse 的 BackgroundTask；最小复现与修复验证器
   scripts/loadtest/probe_sse_leak.py。
2. **压测执行纪律**：档间残留（SSE 槽悬空、arq 残队）会污染下一档数字——
   locustfile 内置 test_start/test_stop 清槽钩子，手册含档间排空检查。

## 进度

- [x] P1.1 骨架与编排
- [x] P1.2 用户体系
- [x] P1.3 语料域与上传
- [x] P1.4 异步入库流水线（全链真跑：解析 → 切块 → embedding → per-company 混合索引）
- [x] P1.5 研究任务中心与报告呈现（多 Agent 研究图 + SSE 实时进度 + 引用校验报告）
- [x] P1 返工：argus-lg v0.2 结论同步（多轮 researcher / 复审补派 / 三层研报 /
      章节面包屑 / 语料概况注入 / pypdf 直读 / RetryingStruct + RetryPolicy 硬化）
- [x] P2 追问对话（condense 查询改写 → 同语料混合检索 → 带 [n] 引用回答；
      token 级 SSE 流式；messages 持久化；报告页内嵌对话界面）
- [x] P3.1 成本与配额（token_usage 记账：研究一任务一行 / 对话一轮一行，
      节点级明细与 missing_calls；24h 滚动预算闸 + 研究并发槽闸 → 429；
      /api/usage 与前端用量条；ADR-008）
- [x] P3.2 限流与流式韧性（HTTP 频率闸 per-user/IP + SSE 并发闸 + 业务配额
      三层分工；对话流生产/消费解耦：断线续写、块间 120s / 全程 300s 超时
      预算自守；测试 Redis 隔离 db1；ADR-009/010）
- [x] P3.3 压测基线（两线口径：真跑体验数字 + fake 压壳 u10/u20/u40 三档
      QPS 与分位数；定位 worker 串行与索引重载两大瓶颈；抓获 SSE 槽位
      泄漏 bug；ADR-011）
- [ ] P3 其余（索引迁 pgvector + 并发化 / 熔断降级 / 可观测与复测收官）