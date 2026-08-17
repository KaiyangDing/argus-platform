# ADR-005: 同步点形态——按研究仓结论产品化重写，不引入依赖

日期：2026-08-17 · 状态：已接受（取代 ADR-004 的解析路线；废除旧规划的 path dep 条款）

## 背景
研究引擎 argus-lg（LangChain + LangGraph 重写实验）已收口（tag v0.1），
验证了完整的语料管线与混合检索方案。旧双仓规划设想产品仓以路径依赖
引入引擎；现裁定 argus-lg 定位为研究专用仓（评测、实验、方案验证），
产品仓独立持有生产实现。

## 决策
- 产品仓按研究仓验证过的结论**全量重写**语料管线与检索层，
  不 import、不依赖 argus-lg：研究仓自由实验不受产品约束，
  产品代码独立演进不受实验波及——spike-then-rewrite 的标准形态。
- 吸收的验证结论（设计同源，代码独立）：PyPDFLoader 页粒度解析；
  RecursiveCharacterTextSplitter 中文分隔符 500/50 keep_separator=end；
  chunk 字段 source_id/company/page/seq/chunk_id/text；
  embedding=text-embedding-v4 经 dashscope 兼容端点
  （check_embedding_ctx_length=False、批 10）；
  BM25(jieba)+向量+RRF 混合、深池再切 k（P1.5 检索侧引入）。
- 解析切换为 PyPDFLoader；mineru 容器退役：compose 移除服务，
  docker/mineru/ 与 ADR-002/004 保留为历史归档，未来需要高精度
  解析（扫描件、复杂表格）时可复活为可选路线。

## 后果
- 语料管线零 GPU 依赖，compose 回归纯 CPU 三件套
- 研究仓的后续实验（v0.2 候选）不影响产品；结论成熟一项吸收一项
- 两仓等价逻辑各自维护是已认账的成本；以"研究结论"而非"共享代码"
  作为同步单位
