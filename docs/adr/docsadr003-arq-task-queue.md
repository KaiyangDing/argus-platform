# ADR-003: 异步任务队列用 arq（Redis 后端）

日期：2026-08-16 · 状态：已接受

## 背景
入库流水线与研究任务是长时任务，须异步执行、失败可重试、worker 重启可续。

## 决策
用 arq：asyncio 原生、Redis 后端、自带重试与定时。不自研队列；
不上 Celery（同步 worker 模型、依赖重）；不上 Kafka（超出运维需求）。
任务进度事件走 Redis Streams，API 以 SSE 转发（P1.5）。

## 后果
- 与 FastAPI 同为 asyncio 栈，app / worker 代码风格一致
- Redis 单点承担队列 + 事件流，dev 与小规模生产足够
- 量级上来再评估 Kafka 槽位，接口形态不变（Streams 消费端隔离）