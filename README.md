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
- [ ] P3 其余（压测基线 / 索引迁 pgvector / 熔断降级 / 可观测与复测收官）