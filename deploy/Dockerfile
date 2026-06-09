ARG BASE_IMAGE=python:3.11-slim
FROM ${BASE_IMAGE}

LABEL org.opencontainers.image.title="cartpilot-backend" \
      org.opencontainers.image.description="E-commerce AI Agent backend (FastAPI + ChromaDB)"

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    ANONYMIZED_TELEMETRY=False \
    CHROMA_TELEMETRY_IMPL=none \
    HF_HUB_OFFLINE=1 \
    TRANSFORMERS_OFFLINE=1 \
    USE_RERANK=1 \
    HOST=0.0.0.0 \
    PORT=8000

WORKDIR /app

# 火山云 ECS 内网 apt 源，避免 deb.debian.org 超时
RUN set -eu; \
    if [ -f /etc/apt/sources.list.d/debian.sources ]; then \
      sed -i 's|deb.debian.org|mirrors.ivolces.com|g; s|security.debian.org|mirrors.ivolces.com|g' /etc/apt/sources.list.d/debian.sources; \
    elif [ -f /etc/apt/sources.list ]; then \
      sed -i 's|deb.debian.org|mirrors.ivolces.com|g; s|security.debian.org|mirrors.ivolces.com|g' /etc/apt/sources.list; \
    fi; \
    apt-get update \
    && apt-get install -y --no-install-recommends build-essential \
    && rm -rf /var/lib/apt/lists/*

COPY backend/requirements.txt /app/backend/requirements.txt
RUN pip install --upgrade pip \
    && pip install -i https://mirrors.ivolces.com/pypi/simple/ --trusted-host mirrors.ivolces.com \
       -r /app/backend/requirements.txt

COPY backend /app/backend
COPY data /app/data

WORKDIR /app/backend

EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/health', timeout=3).read()"

CMD ["python", "-m", "uvicorn", "api.main:app", "--host", "0.0.0.0", "--port", "8000"]
