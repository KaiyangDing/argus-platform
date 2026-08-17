FROM python:3.12-slim

# MinerU pipeline 后端的 cv2 运行时依赖
RUN apt-get update && apt-get install -y --no-install-recommends \
        libgl1 libglib2.0-0 \
    && rm -rf /var/lib/apt/lists/*

# 默认源的 torch 即 CUDA 版，wheel 自带 CUDA 运行时，无需 nvidia/cuda 基镜像
RUN pip install --no-cache-dir "mineru[pipeline]"

# 设备不写死：容器拿到 GPU 就用 CUDA，没拿到自动回落 CPU
ENV MINERU_MODEL_SOURCE=modelscope

EXPOSE 8000
CMD ["mineru-api", "--host", "0.0.0.0", "--port", "8000"]