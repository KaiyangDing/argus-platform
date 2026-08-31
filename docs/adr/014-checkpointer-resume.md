# 014 checkpointer 断点续跑：故障过后的零浪费恢复

日期：2026-08-31 · 状态：已定（P3.5 批3/批4）

## 背景

研究任务分钟级、每跑 ¥0.2 量级。任何终杀（60 分钟超时、worker 重启、
熔断终试失败、arq 重投）都从头重跑——第 18 分钟崩=前 18 分钟的钱与
时间全弃。重试体系（ADR-013 前三层 + 熔断）解决「要不要再试」，没解决
「再试从哪开始」。

## 决策

- **langgraph-checkpoint-postgres（AsyncPostgresSaver）**：每 superstep
  自动落全量 state 进 PG；`astream(None, config)` 从最新 checkpoint 续跑。
  图代码零改动——持久化是编排层横切关切，`build_graph` 无感。
- **thread_id = task_id**：任务与执行时间线 1:1，arq 重投（同 task_id）
  与手动重试天然落回同一时间线，零关联表。
- **两粒度恢复**（图层测试锁定）：superstep 间——write 崩，前置节点全
  部从 checkpoint 恢复零调用；superstep 内（pending writes）——并行
  researcher 部分失败，成功分支产出已存，续跑只重跑失败分支。
- **取值走终态快照**（graph.aget_state），updates 流只做进度事件：续跑
  时已恢复节点不再流经 updates，从流里捡 report/evidence 会拿到空。
- **checkpoint 生命周期**：done 即删（adelete_thread，每任务 MB 级无保留
  价值）；failed/取消保留=续跑的本钱。`next==()` 且有 values 的快照走
  直取分支——治「图跑完、done 落库前崩」的窗口（幂等窗口审计，P1.4
  方法论第二次应用）。
- **重试端点 = 续跑入口**：POST /research/{id}/retry，收 failed（断点
  续跑）与 queued（孤儿自愈）；配额闸分状态（failed 才要新槽，queued
  已在并发计数内）。入队全链 `_job_id` 幂等（arq 同 id 在队/在跑即
  no-op）+ `keep_result=0`（result key 不留，任务完成后同 id 可立即重
  投）；enqueue 包 try 失败落 failed——「commit 后 enqueue 前崩」不再
  留静默孤儿。
- **schema 自管**：checkpoints 三表由 saver.setup() 建与迁移，不进
  alembic——第三方库私有 schema 与业务表解耦；worker on_startup 幂等
  确保。
- **Windows 适配**：psycopg async 不支持 Proactor 循环，conftest 与
  worker 模块级换 SelectorEventLoopPolicy（Linux 部署不执行；policy
  系统 py3.16 移除前随 arq/pytest-asyncio 新口径迁移）。

## 后果

- 故障恢复成本从「重烧全程」降到「只补失败段」；用户点重试=从断点继续，
  SSE 时间线以 resume 事件标记。
- 每 superstep 一次 checkpoint 写（几百 KB 序列化 + PG 写入，相对分钟级
  任务开销可忽略）；chat 图不挂 checkpointer（秒级交互无续跑价值，
  ADR-010 断线续写已覆盖）。
- 永不重试的 failed 任务 checkpoint 永久残留——清理策略并入 P3 后可选
  池「任务/文件删除」时做级联。
