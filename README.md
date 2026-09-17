# Vael-Mux

订阅聚合 / 节点检测 / 多格式订阅输出的容器化服务。

## 特性

- Web 服务先行启动，后台异步执行完整流水线
- WebSocket 实时推送进度
- 订阅源在 `config/config.yaml` 中以换行方式集中管理
- 支持输出 mihomo / sing-box / base64 / v2ray / v2ray-json
- 支持定时调度（cron）
- 单容器，无外部依赖

## 端口

| 端口 | 用途 |
|------|------|
| 8100 | Web 管理界面 |
| 8110 | API / 订阅输出 |

## 订阅端点

| 端点 | 说明 |
|------|------|
| `http://<host>:8110/sub/mihomo`     | Mihomo / Clash.Meta YAML |
| `http://<host>:8110/sub/singbox`    | sing-box JSON |
| `http://<host>:8110/sub/v2ray`      | v2rayN / v2rayNG / v2rayA 订阅（Base64 URI 列表） |
| `http://<host>:8110/sub/v2ray-json` | v2ray 完整 config.json |
| `http://<host>:8110/sub/base64`     | 通用 Base64 订阅 |

**v2ray 别名**（等价于 `/sub/v2ray`）：
`/sub/v2rayn`、`/sub/v2rayng`、`/sub/v2raya`

**v2ray-json 别名**：`/sub/v2ray_json`、`/sub/v2ray-config`

可通过 `GET /api/formats` 查看当前服务端支持的所有格式。

## 部署

```bash
docker compose up -d --build
```

访问 `http://<host>:8100` 查看实时状态。

## 目录

```
vael-mux/
├── app/
│   ├── main.py
│   ├── config.py
│   ├── core/          # 拉取 / 解析 / 检测 / 导出
│   ├── web/           # Web + API
│   └── utils/         # 日志 / 状态
├── config/config.yaml
├── output/
├── Dockerfile
└── docker-compose.yml
```