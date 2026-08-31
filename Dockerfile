# 同镜像双角色（ADR-001 兑现，取舍见 ADR-016）：app 与 worker 共用这份
# 镜像，compose 里用不同 command 启动——单体的部署形态。
# 两阶段：node 构建前端静态产物 → python 运行时（uv 装依赖 + 代码 + dist）。

FROM node:20-slim AS frontend
WORKDIR /build
# 国内网络下 npmjs 官方源常整分钟级停滞；换回官方源改这个参数即可
ARG NPM_REGISTRY=https://registry.npmmirror.com
RUN npm config set registry $NPM_REGISTRY
COPY frontend/package.json frontend/package-lock.json ./
RUN npm ci
COPY frontend/ ./
RUN npm run build

FROM python:3.14-slim
COPY --from=ghcr.io/astral-sh/uv:latest /uv /uvx /usr/local/bin/
# 镜像内已有 3.14，禁 uv 自下解释器；venv 路径进 PATH 后命令免前缀
ENV UV_PYTHON_DOWNLOADS=never \
    UV_COMPILE_BYTECODE=1 \
    PATH="/srv/.venv/bin:$PATH"
WORKDIR /srv
# 依赖层与代码层分离：pyproject/lock 不动则重建走缓存、秒级
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev
COPY alembic.ini ./
COPY migrations/ migrations/
COPY app/ app/
COPY --from=frontend /build/dist frontend/dist
# 默认起 API；worker 服务在 compose 覆盖 command 起 arq
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
