FROM python:3.11-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    TZ=Asia/Shanghai

WORKDIR /app

# ---- 系统依赖 ----
RUN apt-get update \
    && apt-get install -y --no-install-recommends curl ca-certificates \
    && rm -rf /var/lib/apt/lists/*

# ---- 安装 sing-box（多架构自动选择） ----
ARG SINGBOX_VERSION=1.10.0
RUN set -eux; \
    arch="$(dpkg --print-architecture)"; \
    case "$arch" in \
        amd64) sb_arch="amd64" ;; \
        arm64) sb_arch="arm64" ;; \
        armhf) sb_arch="armv7" ;; \
        *)     sb_arch="amd64" ;; \
    esac; \
    curl -fsSL "https://github.com/SagerNet/sing-box/releases/download/v${SINGBOX_VERSION}/sing-box-${SINGBOX_VERSION}-linux-${sb_arch}.tar.gz" \
        -o /tmp/sing-box.tar.gz; \
    tar -xzf /tmp/sing-box.tar.gz -C /tmp; \
    mv "/tmp/sing-box-${SINGBOX_VERSION}-linux-${sb_arch}/sing-box" /usr/local/bin/sing-box; \
    chmod +x /usr/local/bin/sing-box; \
    rm -rf /tmp/sing-box*; \
    sing-box version

# ---- Python 依赖 ----
COPY requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir -r /app/requirements.txt

# ---- 应用代码 ----
COPY app /app/app

# ---- 运行目录 ----
RUN mkdir -p /app/config /app/output

EXPOSE 8100 8110 7890 7891

CMD ["python", "-m", "app.main"]