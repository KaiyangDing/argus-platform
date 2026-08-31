# 016 容器化部署：同镜像双角色与 profile 分形态

日期：2026-09-01 · 状态：已定（P4.1）

## 背景

ADR-001 的架构定式「app 与 worker 同镜像不同启动命令」此前只存在于
文档：dev 实际是三终端（uvicorn / arq / npm）+ compose 三件套。P4.1
把它兑现为一条 `docker compose --profile full up -d` 起全栈，并开放
局域网访问；公网部署（VPS + HTTPS）暂不做，前提清单在本档归档。

## 决策

- **一份 Dockerfile 两阶段**：node 构建前端 dist → python:3.14-slim +
  uv 装依赖；app 与 worker 是同一镜像的两个 compose 服务，只差
  command——单体的部署承诺落地。依赖层与代码层分离，改代码重建秒级。
- **compose profile 分形态**：默认 `up` 仍只起三件套（host dev 三终端
  照旧，8000 不被抢占）；`--profile full` 才起 app+worker。两形态共用
  同一 PG/Redis/MinIO 卷，数据互通。
- **前端由 FastAPI 同源托管**（StaticFiles(html=True) 挂 "/"，注册在
  所有路由之后）：免 CORS、免 vite 代理、免 nginx 一个组件；本 SPA 无
  客户端路由深链，html=True 即够。挂载以 dist 存在为条件，dev 无感。
- **迁移放 app 启动序**（alembic upgrade head && uvicorn）：单副本下
  幂等且免人工步骤；worker depends_on app healthy，保证见到的 schema
  已迁完。多副本部署时此法有并发迁移风险，要抽独立 init job——已知
  边界，能讲清。
- **暴露面收敛**：PG/Redis/MinIO 端口改绑 127.0.0.1，局域网可达的
  只有 8000；.env 不进镜像（.dockerignore），运行时经 env_file 注入。
- **日志随容器化免费升级**：stdout 由 Docker json-file 驱动持久化，
  `docker compose logs` 可回看——「日志要不要落文件」的 12-factor
  答案由平台兑现，应用代码零改动。

## 后果

- 三终端 → 一条命令；局域网内手机/他机可用 `http://<内网IP>:8000`。
- 公网部署（未做）的前提清单：注册上闸（邀请码或关闭——预算配额是
  per-user，开放注册=烧 key 无总上限）、ARGUS_ENVIRONMENT 非 dev +
  真 JWT 密钥（启动守卫强制）、三件套不暴露、反代 HTTPS（Caddy）、
  SSE 经反代需关缓冲。国内 VPS 绑域名涉备案。
- 镜像内 uvicorn 单进程无 --reload；dev 迭代仍用 host 三终端形态。
