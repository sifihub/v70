FROM python:3.11-slim-bookworm

ENV DEBIAN_FRONTEND=noninteractive
ENV TZ=Etc/UTC
ENV PIP_NO_CACHE_DIR=1
ENV PYTHONUNBUFFERED=1
ENV CHROMIUM_PATH=/usr/bin/ungoogled-chromium
ENV ZARA_CHROMIUM_BINARY=/usr/bin/ungoogled-chromium
ENV ZARA_PROFILE_DIRECTORY=Default
ENV ZARA_WINDOW_SIZE=1366x768
ENV ZARA_DRIVER_BACKEND=undetected,selenium
ENV ZARA_UC_USE_WEBDRIVER_MANAGER=1
ENV ZARA_UC_USE_SUBPROCESS=1
ENV ZARA_PREFER_SYSTEM_CHROMEDRIVER=0
ENV DISPLAY=:99
ENV ZARA_PROFILE_PATH=/app/chromium
ENV OLLAMA_LLM_LIBRARY=cpu
ENV OLLAMA_HOST=http://127.0.0.1:11434
ENV OLLAMA_KEEP_ALIVE=20m
ENV ZARA_PRIMARY_MODEL=qwen2.5:0.5b
ENV ZARA_OLLAMA_MODELS=qwen2.5:0.5b,smollm2:135m,tinyllama:1.1b,deepseek-coder:1.3b
ENV ZARA_LLM_PREFERENCE=remote-first
ENV ZARA_LLM_TIMEOUT_SECONDS=90
ENV ZARA_SKIP_OLLAMA_MODEL_DISCOVERY=1
ENV ZARA_ENABLE_OLLAMA_CLI_FALLBACK=0

RUN apt-get update -qq && \
    apt-get install -y --no-install-recommends \
        ca-certificates \
        chromium \
        chromium-driver \
        curl \
        fonts-liberation \
        git \
        libopenblas0 \
        libvulkan1 \
        procps \
        zstd \
        xvfb && \
    ln -sf /usr/bin/chromium /usr/bin/ungoogled-chromium && \
    ln -sf /usr/bin/chromium /usr/bin/chromium-browser && \
    mkdir -p /tmp/ollama-dist /usr/lib/ollama && \
    curl -fsSL https://ollama.com/download/ollama-linux-amd64.tar.zst | \
      tar --zstd -x -C /tmp/ollama-dist \
        --exclude='*/runners/cuda*' \
        --exclude='*/runners/rocm*' \
        --exclude='*/cuda_v*' \
        --exclude='*/libcuda.so*' \
        --exclude='*/libcudart.so*' \
        --exclude='*/libggml-cuda*' \
        --exclude='*/rocm*' && \
    install -m 755 /tmp/ollama-dist/bin/ollama /usr/bin/ollama && \
    cp -a /tmp/ollama-dist/lib/ollama/. /usr/lib/ollama/ && \
    rm -rf /tmp/ollama-dist && \
    chmod +x /usr/bin/chromium /usr/bin/chromedriver /usr/bin/ungoogled-chromium /usr/bin/chromium-browser || true && \
    rm -rf /var/lib/apt/lists/*

WORKDIR /app

RUN mkdir -p /app/profile /app/data /app/runtime /app/chromium

COPY requirements.txt /tmp/requirements.txt
RUN python -m pip install --upgrade pip && \
    python -m pip install -r /tmp/requirements.txt

CMD ["bash"]
