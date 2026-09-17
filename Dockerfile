FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

RUN apt-get update && apt-get install -y --no-install-recommends \
        ca-certificates curl tzdata \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
# RUN pip install -r requirements.txt
RUN pip install -r requirements.txt https://mirrors.aliyun.com/pypi/simple/

COPY . .
RUN pip install -e .

# Mihomo 内核目录 (用户可挂载或启动时自动下载)
RUN mkdir -p /app/bin /app/data

# EXPOSE 8000 7890
EXPOSE 8010 8901

CMD ["vael-mux", "--config", "/app/config.yaml"]