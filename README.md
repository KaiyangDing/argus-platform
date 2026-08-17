# Argus Platform

企业尽调研究平台：建公司 → 上传 PDF → 异步解析入库 → 多 Agent 生成带引用的
尽调报告，并可对报告追问对话（回答仍检索同一语料、仍带引用）。

本仓是平台壳（API / 队列 / 存储 / 前端）。研究引擎在同级 argus 仓独立收口，
完成后以依赖方式引入，两仓按冻结契约（chunk schema / SearchFn / 文档状态机）并行。

## 架构

    browser ──► Vite dev (5173) ──proxy──► FastAPI app (8000) ──┬─► PostgreSQL 16 + pgvector
                                               │                ├─► Redis（队列 / 事件流）
                                               │                └─► MinIO（PDF 原件）
                                          arq worker ◄─── Redis 队列
                                               │
                                               └──HTTP──► mineru-api（本机 GPU 进程，URL 配置化）

模块化单体：app 与 worker 同一代码库、同一镜像、不同启动命令（见 docs/adr/）。

## 怎么跑（dev）

前置：Docker Desktop、uv、Python 3.14、Node 20+。

起基础设施：

    docker compose up -d

起后端（http://localhost:8000，API 文档在 /docs）：

    uv run uvicorn app.main:app --reload

起前端（http://localhost:5173）：

    cd frontend
    npm install
    npm run dev

探活：页面首页即三依赖状态；或 curl http://localhost:8000/healthz

## 测试

    uv run pytest

## 进度

- [x] P1.1 骨架与编排
- [ ] P1.2 用户体系
- [ ] P1.3 语料域与上传
- [ ] P1.4 异步入库流水线
- [ ] P1.5 研究任务中心与报告呈现
- [ ] P2 追问对话
- [ ] P3 工程硬化