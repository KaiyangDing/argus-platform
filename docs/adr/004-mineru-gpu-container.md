# ADR-004: 解析服务容器化进 compose（GPU 直通）

日期：2026-08-16 · 状态：已接受（部分取代 ADR-002）

## 背景
ADR-002 将 mineru-api 定为本机 GPU 进程，主因是对 Windows dev 环境
GPU 容器化成本的判断。实际条件已变：Docker Desktop（WSL2 后端）的
NVIDIA 直通已成熟，且本机 pipeline GPU 解析在引擎仓全量语料重建中
实测过关；引擎侧跑批高峰已过，容器化不再干扰引擎开发。

## 决策
compose 增加 mineru 服务并声明 GPU 设备预留（deploy.resources.
reservations.devices）。镜像自建：python:3.12-slim + mineru[pipeline]
（默认 CUDA 版 torch，wheel 自带 CUDA 运行时，无需 nvidia/cuda 基镜像）。
不用官方 vllm 镜像——那是 vlm 后端路线，与引擎已验证的 pipeline
解析链路不同源且镜像更重。保持 pipeline 后端、仅设备加速：
调用方传 backend=pipeline，设备 auto 检测（有卡 CUDA、无卡回落 CPU）。
HTTP 边界与 URL 配置化不变（ADR-002 核心维持）。

## 后果
- compose up 一键起全部依赖，解析走 GPU 全速
- mineru 服务声明了 GPU 预留，无 NVIDIA 的机器需去掉 deploy 段
  （设备检测自动回落 CPU 模式），其余服务不受影响
- 单卡显存与本机其他 CUDA 进程共享：闲时 docker compose stop mineru
