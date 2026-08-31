# 015 可观测采集栈裁决不做：取舍与替代件

日期：2026-08-31 · 状态：已定（P3.6 收官裁决）

## 背景

P3 原计划含可观测（Prometheus + Grafana）。埋点层曾完整落地（HTTP RED
标准件 + 六组业务指标 + API/worker 双进程 /metrics，161 测绿）后整体
回滚：采集/查询/面板/告警（Prometheus、PromQL、Grafana provisioning）
是一套独立技术栈，学习成本超出本仓的硬性复杂度上限——「面试能讲清」
纪律是双向的，讲不清的组件是负资产而非加分项。可观测在目标岗叙事
排序（评测驱动 > 编排 > 检索 > 成本 > 韧性 > 中间件）中也居末位。

## 决策

- 采集栈不做。开发期可观测由既有件承担：structlog 结构化日志（全链
  JSON）、LLMCallLogger 调用级日志（节点/耗时/错误，2026-08-19 十九
  分钟静默事故的解药）、token_usage 记账表（成本可查、可对账单）。
- 不以 Langfuse/LangSmith 替代：它们是 LLM 内容层 traces 平台（单请求
  因果链 + prompt/输出全文 + 评测标注），与服务层 metrics 分属可观测
  三支柱的不同支柱，本就不是同一问题的两个答案。且本仓记账必须事务性
  自持——配额闸在同一事实源上执行（ADR-008），observe-only 遥测管道
  替代不了；LangSmith SaaS 有语料出境问题（尽调 PDF 证据原文随 prompt
  外发）；Langfuse v3 自托管（PG+ClickHouse+Redis+S3+worker）比本仓
  业务栈本身更重。
- 升级路径留白：LangGraph 接 LangSmith 是一个环境变量，Langfuse 是
  callbacks 一行；需要 prompt 版本管理与标注评测时再上，是加法不是
  重构。

## 后果

- 无聚合指标与面板：健康度以 locust 报表 + README 数字为准（P3.3/P3.4
  同参数复测）；生产化部署时采集栈是第一补课项。
- 已知观测盲区随回滚退场、在此记录在案：chat 路径无调用级日志
  （LLMCallLogger 只挂研究图）；embedding 调用不走 LangChain 回调体系
  （无 on_embedding_* 事件）、无调用级观测——P3.5 真跑三十分钟静默
  沙漏的盲区根因。
