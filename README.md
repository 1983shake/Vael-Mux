# Vael-Mux

多协议订阅聚合 / 测活 / 测速 / 路由 / 代理客户端。Python + FastAPI + Mihomo。

## 功能
- 订阅聚合（v2ray / mihomo / clash 等标准订阅格式）
- 多地址测活 & 多地址测速
- 评分排序（延迟 + 下载速度 + 稳定性加权）
- 自动生成多种格式订阅（mihomo YAML / V2Ray base64）
- 可作为本地代理客户端（Mihomo 内核）
- Web 面板：配置热编辑、实时日志、节点状态

## 快速开始
```bash
pip install -r requirements.txt
pip install -e .
# 放入 mihomo 二进制到 ./bin/mihomo
cp config.example.yaml config.yaml
vael-mux --config config.yaml
```
<!-- 访问 http://localhost:8000 -->
访问 http://localhost:8010

## Docker
```bash
docker compose up -d --build
```

## 许可证
GPL-3.0-or-later（详见 LICENSE）。