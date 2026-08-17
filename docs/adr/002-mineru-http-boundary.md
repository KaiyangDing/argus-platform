# ADR-002: PDF 解析走 mineru-api HTTP 边界

日期：2026-08-16 · 状态：已接受

## 背景
MinerU 解析需要 GPU 与 py3.12+CUDA 环境，与本仓 py3.14 存在依赖墙；
GPU 进程容器化在 Windows dev 环境成本高。

## 决策
解析作为本机 GPU 进程运行 mineru-api，平台经 HTTP 调用，
URL 配置化（ARGUS_MINERU_API_URL），不进 docker-compose。

## 后果
- 依赖墙隔离：平台仓不背 CUDA / py3.12 依赖
- 解析可独立部署到任意 GPU 机器，改一个 URL 即迁移
- 需正视 HTTP 边界的失败语义：超时 / 重试（P1.4）、熔断降级（P3）