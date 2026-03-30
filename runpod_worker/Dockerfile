# NVIDIA CUDA + cuDNN runtime — avoids missing libcudnn in generic Python images.
# Pin the base tag to match your RunPod GPU driver / CUDA capability.
FROM nvidia/cuda:12.4.1-cudnn9-runtime-ubuntu22.04

ENV DEBIAN_FRONTEND=noninteractive \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PYTHONPATH=/app/stemmy_pkg

RUN apt-get update && apt-get install -y --no-install-recommends \
    python3 \
    python3-pip \
    ffmpeg \
    && rm -rf /var/lib/apt/lists/* \
    && ln -sf /usr/bin/python3 /usr/local/bin/python

WORKDIR /app

COPY runpod_worker/requirements-worker.txt /app/requirements-worker.txt
RUN python3 -m pip install --upgrade pip setuptools wheel \
    && python3 -m pip install -r /app/requirements-worker.txt

# Package tree: repo src/stemmy_cli/ → import stemmy_cli
COPY src/stemmy_cli /app/stemmy_pkg/stemmy_cli

# CPU-only machine builds: verify imports only (GPU is checked at runtime / HEALTHCHECK).
RUN python3 -c "import torch; import whisperx; print('build_check torch', getattr(torch, '__version__', '?'))"

COPY runpod_worker/healthcheck_cuda.py /app/healthcheck_cuda.py
COPY runpod_worker/handler.py /app/handler.py

HEALTHCHECK --interval=30s --timeout=20s --start-period=120s --retries=3 \
    CMD python3 /app/healthcheck_cuda.py || exit 1

CMD ["python3", "-u", "/app/handler.py"]
