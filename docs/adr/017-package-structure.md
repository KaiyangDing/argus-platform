# 017 包结构重组：按域分包，入口留根

日期：2026-09-01 · 状态：已定（收官后重构，用户裁定）

## 背景

app/ 此前为平铺 20 模块 + routers/ 一个子包——在 3k 行、单人规模下
可辩护（模块名即边界、FastAPI 官方模板同构），但可读性偏好裁定改为
显式分包。切分依据取仓库自己的叙事线（README 首段）：平台壳 vs
自含引擎。

## 决策

- **三个域包 + 入口留根**：core/（横切基础设施：config/logs/db/
  security/storage/breakers/limits）、engine/（研究引擎域，argus-lg
  结论的自含重写：llm/prompts/fakes/ingest/retrieval/research/chat）、
  domain/（业务域：models/schemas/usage）；main.py / worker.py /
  deps.py 留根——**部署字符串 `app.main:app` 与
  `app.worker.WorkerSettings` 零改动**，Dockerfile/compose 不碰。
- **依赖方向单向**（重组前的实际依赖图验证，非事后规定）：core 不
  依赖兄弟包；engine 只依赖 core（retrieval 走裸 SQL，不碰 domain
  的 ORM 模型，是这条规则能成立的关键）；domain 只依赖 core；入口
  与 routers 组装一切。
- **按域不按层**：不设 services/、repositories/、utils/ 之类仪式层
  ——层是逻辑上的（router → 域 → 基础设施），不用文件夹重复声明。
- **纯机械搬迁**：git mv 保留历史 + 全仓正则改写 import（含测试的
  字符串 monkeypatch 目标）；行为零变化由 151 项测试锁定。历史
  ADR 文内的旧路径**不回改**（ADR 只增不改，记录当时事实）。

## 后果

- import 路径一次性全变（46 文件），此后新模块有明确归属判据：
  无业务知识 → core；引擎算法 → engine；数据契约与配额 → domain。
- 镜像需 rebuild 后新结构才进容器（COPY app/ 递归，无需改动）。
- 若未来按特性竖切（auth/、corpus/ 各带 router+schema+逻辑），
  再做一次同型搬迁即可，本次不预支。
