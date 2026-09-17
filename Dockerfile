FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    TZ=Asia/Shanghai

WORKDIR /app

RUN apt-get update && apt-get install -y --no-install-recommends \
    curl ca-certificates tzdata \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
# RUN pip install --no-cache-dir -r requirements.txt -i https://mirrors.aliyun.com/pypi/simple/

COPY app/ ./app/
COPY config/ ./config/

RUN mkdir -p /app/output

EXPOSE 8100 8110

HEALTHCHECK --interval=15s --timeout=5s --start-period=15s --retries=5 \
    CMD curl -fsS http://localhost:8100/health || exit 1

CMD ["python", "-m", "app.main"]