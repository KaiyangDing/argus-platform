# ADR-006：argus-lg v0.2 研究结论同步返工

日期：2026-08-19 ｜ 状态：已采纳

## 背景

研究仓 argus-lg v0.2 收口（覆盖 13/18 历史最高，报告升级为 6000~9000 字
三层研报），本仓 P1 研究链是按 v0.1 设计自含重写的，需要返工同步。
同步单位仍是**研究结论而非代码**（ADR-005 spike-then-rewrite 口径不变）。

## 决定

四批返工：①llm 硬化（timeout=120s/max_retries=2）+ RetryingStruct
（图内结构化输出一律 function_calling，空返回催办扰动重试）+ prompts 套件；
②研究图 v0.2（多轮 map-reduce researcher / review 复审补派 / merge 同名归并 /
write 一致性核对+三层化 / 节点级 RetryPolicy）；③章节面包屑（免重嵌：只进
metadata 与证据渲染，检索出口按 chunks.jsonl 回贴，旧索引零迁移）+ 语料概况
数据驱动注入 + PyPDFLoader 退役改 pypdf 直读；④报告渲染升级（表格/列表）。

## 已知边界（记录不改，候选回流研究仓）

- 表列对齐错配 = pypdf 线性化固有天花板，prompt/架构层无解；处方=结构化
  表格解析（mineru spike 待立项拍板）。
- merge 同名归并按 aspect_id 字符串排序，f 系排在 r 系前：补研与原方面同名
  （模型违纪兜底路径）时【补充研究】标签位次颠倒、write 反查取到补研 focus。
- BM25Retriever 仍在 langchain-community（上游无独立包去处），保留一条
  日落告警；轮间方差钉板（录放层）留 P3。
