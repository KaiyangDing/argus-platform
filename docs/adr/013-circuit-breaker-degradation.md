# 013 熔断与降级：Redis 共享态熔断器 + 分路降级动作

日期：2026-08-31 · 状态：已定（P3.5 批1/批2）

## 背景

既有韧性体系是三层重试（openai max_retries=2 → 图节点 RetryPolicy(3) →
arq Retry×3），治瞬态抖动；端点持续故障（实测窗口十几分钟级，2026-08-19
抖动事故）下重试是放大器：所有在跑任务与对话各自烧满全部退避才失败，
且持续锤已挂的端点。缺的是跨调用、跨进程的「端点挂了」共享事实。

## 决策

- **pybreaker + CircuitRedisStorage**（不自研）：三态状态机，状态与失败
  计数存 Redis——调用方横跨 API 进程（chat 内联图）与 worker 进程（研究
  图 + 入库 embedding），进程内熔断各数各的无意义，共享态让第一个发现
  故障的进程为所有进程拉闸。
- **两端点分开熔断**：chat（qwen-flash）与 embedding（text-embedding-v4）
  故障域独立。阈值推导：LLM fail_max=5（每次外显失败背后客户端已内重试
  2 次，5 外显 ≈ 15 连败）、embedding fail_max=3（无业务层重试、调用密
  度高、有词法兜底拉闸代价小）；reset_timeout 60s/30s = 半开探针间隔，
  锚在实测故障窗（60s 一探，20 分钟窗内至多空探 20 次）。
- **失败判据白名单**：只有连接失败/超时、429、5xx 计数（exclude 反向
  谓词）。RetryingStruct 耗尽的 ValueError 是模型行为、AuthenticationError
  是配置事故——计入会污染「闸开=端点不可用」的语义。
- **包装点 = ChatOpenAI/OpenAIEmbeddings 子类覆盖同步 _generate/_stream/
  embed_***：图内全部调用形态（invoke/batch/token 流）汇于这几个同步方法，
  单点全覆盖，callback 链路（记账/调用日志）零感知。
- **降级动作分路**：embedding 不可用 → 检索退纯词法（SQL 中 :has_vec
  布尔开关剪空向量 CTE；qvec 裸 IS NOT NULL 会让 psycopg 服务器端绑定
  的参数类型推断歧义，实测抓获）；chat 不可用 → SSE 秒级中文明确报错；
  worker 遇 CircuitBreakerError → Retry defer 拉长到冷却窗后（90s），
  否则三试半分钟内全撞闸白落 failed。
- **fail-open**：Redis 不可用时 storage 回落 closed 放行——护栏失效的
  后果不得重于其所防的事故（与 ADR-009 限流 fail-open 同判据）；单挂
  Redis 时端点是好的，放行恰是正确行为。

## 后果

- 端点持续故障期间：新调用毫秒级明确失败（此前 ~92s 重试退避）、检索
  降级仍可用、恢复靠半开探针自动完成，全程无人工。
- 降级可观测：breaker 状态迁移 warning 日志 + retrieval_degraded 日志；
  静默降级=质量事故没人知道。
- 代价：闭合态每次调用多一次 Redis GET（亚毫秒，相对秒级 LLM 调用可忽
  略）；pybreaker 为同步库，若未来图节点 async 化需换 async 熔断方案。
