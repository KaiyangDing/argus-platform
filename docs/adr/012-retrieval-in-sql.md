# 012 检索迁 PG：SQL 内混合检索（词法 + 向量 + RRF）

日期：2026-08-30 · 状态：已定（P3.4 批2）

## 背景

MinIO 索引形态（chunks.jsonl + vectors.json）在每次检索时全量载入：
反序列化全部向量、对全部块现建 BM25（jieba 分词是大头）。压测基线
（ADR-011）把它定位为 u40 下 chat 尾部 P95 9.8s 与读路径尾部劣化的
主要成因（CPU / 线程池 / GIL 争抢）。批1 已把块与向量迁入 chunks 表
（HNSW + GIN + tsvector 列）。

## 决策

检索整体下沉为一条 SQL：两路 CTE 各取深池 200 名次（向量 `<=>` 余弦、
词法 `ts_rank`），FULL OUTER JOIN 后按 RRF（1/(60+rank) 相加）融合，
排序切 k。SearchFn 契约与函数签名不变，图与路由零改动——这正是当年
冻结「检索实现可整体替换而图不知情」契约要兑现的场景。

关键口径：

- **查询侧 OR 组词，不用 plainto**：plainto_tsquery 是 AND 语义，真实
  查询切出七八个词、单块几乎不可能全含，词法路会整路打灭、混检退化成
  纯向量。改为 jieba 切词后 `to_tsquery('simple', '词1 | 词2 | …')`
  （词加引号防特殊字符；无有效词传 NULL，词法路自然空），对齐 BM25
  「任一词命中即计分」的语义。
- **ts_rank 非严格 BM25**（无 IDF 项与文档长度归一）。可接受的依据：
  下游 RRF 只吃名次不吃分值，对打分函数的绝对形状不敏感；检索质量的
  最终裁判是引擎仓评测口径。
- **同步 engine（psycopg）**：SearchFn 在图的同步节点/线程池里执行，
  async engine 进不去；QueuePool 线程安全、懒连接。异步改图是大动作，
  不为此做。
- **空语料 EXISTS 短路**：研究图空语料路径每方面 3 查询，不短路会
  白打十几次 embed_query。
- **section 回贴机制退役**：旧机制是双文件快照不同步（向量 dump 无
  section）的补丁；单表事实源后 section 就在行里，机制整体消失。

## 后果

- 每请求成本从「全量载入 + 全量分词」降到「embed_query 一次 + 两路
  索引查询」；批4 复测验证 chat 尾部收敛。
- MinIO 的 index/ 对象自此无人读写，代码与依赖（rank-bm25 /
  EnsembleRetriever / InMemoryVectorStore / langchain-classic）批3 清理，
  对象本身留到复测收口后再删（免费回滚保险）。
- 词法行为与 rank-bm25 存在打分差异（诚实记录）；若评测口径显示词法
  召回退化，升级梯是 ParadeDB pg_search（PG 内真 BM25）或回引应用层
  BM25——目前不做。

## 实测追记（2026-08-30 批4 同参数复测）

预告的收敛全部兑现：u40 读路径 P95 从 3.8~13s 回到 21~32ms、chat P95
9.8s→1.1s（争抢拔源）；research e2e 61s→4.4s（max_jobs 1→4 + write
batch 并发）；u40 吞吐 7.0→13.2 req/s 仍未饱和，瓶颈移回外部 LLM 时延。
另一条实战发现：ts_rank 无 IDF + 泛问词面不对撞（「财务表现」查不到
利润表）由 chat 首问检索式改写（condense 升级）治理——泛问 → 语料
词面的翻译层，研究图 QUERIES_PROMPT 的同款缺环补齐。MinIO index/
读侧遗产（load 函数、迁移脚本、index 对象）随本批清理退役。
