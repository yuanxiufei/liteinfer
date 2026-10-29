# LiteInfer 开发镜像（骨架）
#
# 注意：这是 CPU 调试骨架，不能用于正式 GPU 验收。
# WBS-0.2 锁定兼容组合后，必须改为固定的 CUDA 基础镜像，并记录 repository:tag 与 digest：
#   ARG CUDA_BASE_IMAGE=nvidia/cuda:<runtime-tag>@sha256:<digest>
# 禁止使用 latest。

ARG PYTHON_IMAGE_TAG=3.11-slim

FROM python:${PYTHON_IMAGE_TAG}

LABEL org.opencontainers.image.title="LiteInfer"
LABEL org.opencontainers.image.description="LiteInfer dev skeleton (CPU only, not for GPU acceptance)"

ENV PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PYTHONDONTWRITEBYTECODE=1

WORKDIR /app

COPY pyproject.toml README.md ./
COPY src/ ./src/

# 运行时依赖待 WBS-0.2 锁定后写入 pyproject.toml；此处只装本体骨架。
RUN pip install --no-cache-dir .

CMD ["python", "-c", "import liteinfer; print(liteinfer.__version__)"]
