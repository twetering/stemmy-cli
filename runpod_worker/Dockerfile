# NVIDIA CUDA + cuDNN 9 runtime (tag must exist on Docker Hub).
# 12.4.1-cudnn9-runtime-ubuntu22.04 is not published; 12.3.2-cudnn9 is.
FROM nvidia/cuda:12.3.2-cudnn9-runtime-ubuntu22.04

ENV DEBIAN_FRONTEND=noninteractive \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

RUN apt-get update && apt-get install -y --no-install-recommends \
    python3 \
    python3-pip \
    ffmpeg \
    && rm -rf /var/lib/apt/lists/* \
    && ln -sf /usr/bin/python3 /usr/local/bin/python

WORKDIR /app

COPY runpod_worker/requirements-worker.txt /app/requirements-worker.txt
RUN python3 -m pip install --upgrade pip setuptools wheel \
    && python3 -m pip install -r /app/requirements-worker.txt \
    && python3 -c "import faster_whisper, ctranslate2; print('build_check', faster_whisper.__version__)"

COPY runpod_worker/healthcheck_cuda.py /app/healthcheck_cuda.py
COPY runpod_worker/handler.py /app/handler.py

HEALTHCHECK --interval=30s --timeout=20s --start-period=120s --retries=3 \
    CMD python3 /app/healthcheck_cuda.py || exit 1

CMD ["python3", "-u", "/app/handler.py"]
