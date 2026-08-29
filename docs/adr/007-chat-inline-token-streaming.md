# ADR-007: 追问对话在 API 进程内联执行，token 级 SSE 流式

日期：2026-08-29 · 状态：已接受

## 背景
P2 追问对话是第二核心功能：对已生成的报告继续问，回答仍检索同一
语料、仍带 [n] 引用。它与研究任务的交互形态完全不同：研究是分钟级
批处理（用户可离开等结果），追问是秒级对话（用户在等下一个字）。
P1.5 已有一条 worker + Redis Streams + SSE 的进度事件链路，问题是
追问要不要复用它。

## 决策
- **对话图（condense → retrieve → answer）在 FastAPI 进程内联执行，
  不入 arq 队列**：排队对秒级交互只增延迟；索引载入等同步 IO 甩
  asyncio.to_thread，不堵事件循环。
- **token 级流式用 langgraph `stream_mode=["messages","updates"]`
  双模式消费**：messages 供 token（按 `langgraph_node` 只放行 answer
  节点），updates 供终态（answer 全文 + evidence）落库。与 P1.5 的
  节点级 updates 是同框架的两个流式层，各配各的交互形态。
- **不落 Redis Streams**：生产者消费者同进程、无跨进程边界；对话
  无"刷新回放进行中进度"的需求（历史消息由 messages 表承担），
  断流即弃、用户重问。
- **持久化两段式**：user 消息在请求事务先落（失败也留痕）；
  assistant 消息流完后以 SessionFactory 独立短事务落——不依赖框架
  对 yield 依赖的清理时序（FastAPI 历史上变过），也不让 DB 会话
  横跨流式期。客户端中途断开 = CancelledError 穿透，assistant
  不落库（半截回答不如不落）。
- **SSE 事件协议自建错误面**：`delta`×N → `done`（携落库消息）；
  异常发 `error` 事件——流式响应头一旦发出，HTTP 状态码已定死，
  mid-stream 错误只能在应用层协议表达。

## 后果
- 对话请求的重活（索引载入 + BM25 建索引）目前每消息重做一次，
  小语料下约秒级；索引缓存或 pgvector 迁移是 P3 优化候选
- 长回答期间占用一个 API 并发槽；对话限流/超时预算归 P3
- 同公司并发对话存在历史交错的理论竞态，单用户场景可接受
