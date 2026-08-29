# 011 压测基线：两线口径与 fake LLM 开关

日期：2026-08-29 · 状态：已定

## 背景

P3 压测收官要求 QPS / P95 / P99 与「压测 → 定位 → 优化 → 复测」的
前后对比（PLAN P3）。但本系统的时延主体是外部 LLM / embedding API：
直接并发压真模型，一是烧钱（一次研究 ¥1+），二是压出来的是 dashscope
的排队曲线，不是自己系统的瓶颈。

## 决策

压测走两线，口径分开记录：

- **线 A（真跑基线）**：真模型、单用户小样本（几轮 chat + 一次研究），
  记真实体验数字（chat 首字/全程、研究端到端）。scripts/loadtest/real_baseline.py。
- **线 B（fake 压壳）**：`ARGUS_FAKE_LLM=1` 起服务，LLM 与 embedding
  全换确定性 fake，locust 并发压自家壳层（HTTP / PG / Redis / MinIO /
  worker 编排 / SSE），拿 QPS 与分位数。P3.4 优化后同参数复测对比。

fake 开关下沉到模型工厂（make_chat / make_embeddings 各一个 if）：
deps、chat 路由、worker ctx 所有注入点零改动统一生效。FakeChat 自带
with_structured_output（按 schema 返回最小合法实例），RetryingStruct
与图代码零感知。fake 响应携带非零 usage_metadata——记账落行、配额
聚合这些写路径同样要被压到。`ARGUS_FAKE_LLM_DELAY_S`（默认 0.5s/调用）
模拟单调用时延：没有它任务瞬间完成，队列深度与 SSE 长连接形态全压不出来。

压测数据不走 API 铺（注册/上传各有限流、入库要过队列）：seed 脚本
直连 DB/MinIO，复用生产管线函数造 ready 文档与索引，token 直签。

## 后果

- 线 B 数字不含真模型时延，只证壳层承载力；对外表述两线并列。
- seed 语料是 fake 向量，只在 fake 服务端下有意义；真模型对压测公司
  发问，检索无意义（不清理：账号隔离，不影响真数据）。
- 预期基线瓶颈（待压测证实）：worker max_jobs=1 全局串行 → research
  e2e 随并发线性恶化；每轮对话全量重载索引 → chat P95 随语料增长。
  两者处方都排在 P3.4（pgvector + 放开并发），正是「定位 → 优化 →
  复测」叙事的主线。

## 实测追记（2026-08-29 基线跑完）

两个预期瓶颈均被三档数字证实（见 README 压测节）。额外战果：压测抓获
SseGate 槽位泄漏 bug——客户端在流终止前断开时，挂在 async generator
finally 上的 release 不被及时执行，5 次断开即锁用户 30 分钟；修复为
release 挪 StreamingResponse 的 BackgroundTask（starlette 保证断开也执行），
最小复现器 scripts/loadtest/probe_sse_leak.py。压测消费端与前端保持同款
「读到服务端关流」姿势，档间需排空 arq 队列（手册档间纪律节）。
