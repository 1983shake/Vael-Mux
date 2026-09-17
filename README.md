# Vael-Mux

订阅聚合 / 节点检测 / 多格式订阅输出的容器化服务。

## 特性

- Web 服务先行启动，后台异步执行完整流水线
- WebSocket 实时推送进度
- 订阅源在 `config/config.yaml` 中以换行方式集中管理
- 支持输出 mihomo / sing-box / base64 / v2ray / v2ray-json
- 支持有效节点上限 (`output.max_nodes`)
- 节点列表持久化到 `output/nodes.json`，支持在 Web 页面增删改查
- 修改关键字段后自动重测，重新生成订阅
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
| `http://<host>:8110/sub/v2ray`      | v2rayN / v2rayNG / v2rayA 订阅 |
| `http://<host>:8110/sub/v2ray-json` | v2ray 完整 config.json |
| `http://<host>:8110/sub/base64`     | 通用 Base64 订阅 |

## 节点管理 API (8100)

| 方法 | 路径 | 说明 |
|------|------|------|
| GET    | `/api/nodes`                | 列出所有节点 |
| PATCH  | `/api/nodes/{id}`           | 修改节点（关键字段变更会自动重测） |
| DELETE | `/api/nodes/{id}`           | 删除节点 |
| POST   | `/api/nodes/{id}/enable`    | 启用节点 |
| POST   | `/api/nodes/{id}/disable`   | 禁用节点 |
| POST   | `/api/nodes/{id}/retest`    | 立即重测 |
| POST   | `/api/nodes/batch`          | 批量操作 (`enable`/`disable`/`delete`/`retest`) |

## 部署

```bash
docker compose up -d --build
```

访问 `http://<host>:8100` 查看实时状态与节点列表。