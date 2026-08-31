# Argus Platform

企业尽调研究平台：建公司 → 上传 PDF → 异步解析入库 → 多 Agent 生成带引用的
尽调报告，并可对报告追问对话（回答仍检索同一语料、仍带引用）。

本仓是平台壳（API / 队列 / 存储 / 前端）+ 自含的语料管线与研究图实现。
研究设计在同级 argus-lg 研究仓先行验证，本仓按**研究结论**全量重写引入
（spike-then-rewrite，不共享代码，见 docs/adr/005、006）；当前实现对齐
argus-lg v0.2：多轮 map-reduce researcher、复审补派、三层研报、章节面包屑。

**当前状态：项目收官。**两大核心（研究报告 / 追问对话）与 P3 工程硬化
（成本配额 / 限流韧性 / 压测调优 / 检索并发化 / 熔断降级与断点续跑）
全部收口，151 项测试全绿；可观测采集栈经取舍不做（docs/adr/015）。

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

线 A（qwen-flash 真跑，两轮研究都走含复审补派的完整路径）——基线 → P3.4：

- chat 首字 6.5~7.6s → **2.9~4.6s**、全程 ~8s → **4.4~5.4s**：索引重载
  消失吞掉了首问新增 condense 改写的成本还有余；
- 研究端到端 20.8 → **18.5 分钟**：write 并发的收益被未并发的
  researcher 层（总时长大头）稀释，单样本含时段抖动，如实记录；
- 一次研究成本 ≈¥0.2~0.24、每轮 chat ≈¥0.001 量级不变——并发省时间
  不省 token；记账 missing_calls=0（batch 下 usage 回调完整）。

线 B（每档 3 分钟、**同参数前后复测**；两轮 429=0，失败率 ≤0.4% 且均为
档尾硬切竞态）——基线（P3.3）→ 优化后（P3.4）：

| 指标 | u10 | u20 | u40 |
|---|---|---|---|
| 总吞吐 req/s | 3.1 → 3.4 | 6.2 → 6.7 | 7.0 → **13.2** |
| 读路径 P95 | 18~42 → 23~40 ms* | 21~32 → 17~31 ms | **3.8~13 s → 21~32 ms** |
| chat 首 delta P50 / P95 | 1.1/1.2 → 1.0/1.6 s* | 1.1/1.2 → 1.0/1.1 s | 1.1/**9.8** → 1.0/**1.1** s |
| research e2e P50 / max | 12/26 → **4.4/5.8 s** | 17/32 → **4.4/4.7 s** | **61/107 → 4.4/6.7 s** |
| research 完成数 | 20 → 21 | 24 → 31 | 24 → **82** |

*u10 复测档含冷启动尖刺（53 样本里一次 270ms / 首档缓存未热），P50 与
u20/u40 一致。

优化项（P3.4）→ 复测结论：

- **索引迁 pgvector + tsvector 单表，SQL 内 RRF**（ADR-012）：每请求
  「全量载入 + BM25 现建」整体消失 → u40 读路径 P95 从秒级回到 30ms
  （GIL/线程池争抢拔源），chat P95 9.8s → 1.1s。
- **worker max_jobs 1→4**（chunks 行级 ON CONFLICT 幂等使串行锁失去
  存在理由）：research 排队消失，三档 e2e 持平 ≈4.4s——基线 61s 的
  主体全是排队，排队论的 M/M/1 → M/M/c 实证。
- **write 分节/终稿 batch 并发**：fake 线纯执行 6.5s → 4.4s。
- **chat 首问检索式改写**（condense 升级，三只松鼠泛问实证驱动）：
  宽泛提问翻译成语料词面再检索，代价为首问 +1 次秒级小调用。
- u40 吞吐 13.2 req/s 仍未见饱和拐点：瓶颈从自家壳层移回外部 LLM 时延
  ——这正是本轮优化的完成判据。

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
- [x] P3.4 检索迁 PG 与并发化（chunks 表 pgvector+tsvector、SQL 内 RRF、
      OR 组词；worker max_jobs 1→4；write 分节/终稿 batch 并发；chat 首问
      检索式改写；同参数复测：research e2e 61s→4.4s、chat P95 9.8s→1.1s、
      u40 吞吐近翻倍；ADR-012）
- [x] P3.5 熔断降级与断点续跑（pybreaker Redis 共享态熔断 dashscope 两端点
      ——API 与 worker 进程同视野；embedding 熔断时检索纯词法兜底、chat 秒级
      明确报错、arq 重投 defer 联动冷却窗；LangGraph checkpointer 断点续跑：
      superstep 间 + pending writes 两粒度恢复，失败任务重试端点=续跑入口；
      入队韧性与孤儿 queued 自愈（_job_id 幂等 + enqueue 失败落 failed）；
      ADR-013/014）
- [x] P3.6 收官（可观测采集栈 Prometheus/Grafana 裁决不做——取舍、替代件
      与 Langfuse/LangSmith 层次分析记 ADR-015；压测数字以 P3.3/P3.4
      同参数复测为准）