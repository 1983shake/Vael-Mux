## `README.md`

```markdown
# Vael-Mux

订阅聚合 / 节点检测 / 多格式订阅输出的容器化服务。

## 特性

- Web 服务先行启动，后台异步执行完整流水线
- WebSocket 实时推送进度，日志实时上屏
- 订阅源在 `config/config.yaml` 中以换行方式集中管理
- 支持输出 mihomo / sing-box / base64 / v2ray / v2ray-json
- 单节点流水线检测：**延迟通过 -> 立刻测速**，节点级并发
- 测试目标独立开关（`enabled: true/false`）：关闭的目标不检测、不计入判定、不显示
- 有效节点上限（`check.max_valid_nodes`），达到上限立即停止后续检测
- **更新即重建**：只有检测导出全部成功才清空并写入本次节点列表
  - 中途失败 / 手动停止 → 节点列表保持不变，不导入任何新节点
- 日志级别可配置（`logging.level` / 环境变量 `VAEL_LOG_LEVEL`）
- 每次检测地址结果打印到日志（节点自身 / 每个启用目标）
- 速度值严格过滤 `> 0`：`0.0` 视为无效
- 表格列头带单位（`延迟 (ms)` / `速度 (Mbps)`），单元格只显示数值
- 节点列表持久化到 `output/nodes.json`，支持在 Web 页面增删改查
- 修改关键字段后自动重测，重新生成订阅
- 支持定时调度（cron）
- 静态资源免缓存（`no-store` + 资源版本号），前端更新后立即生效
- 底部展示版权、项目链接、版本（版本号来自 `app/__init__.py`）
- 单容器，无外部依赖

## 检测模型

**单节点流水线**：每个节点独立完成「延迟 -> 速度」，节点间通过 `check.concurrent` 控制并发数。

```
节点 A: [延迟 ...............] -> 有效 -> [速度 .............] -> 有效节点
节点 B: [延迟 ..] -> 无效 -> 跳过速度
节点 C: [延迟 ......] -> 有效 -> [速度 ......] -> 有效节点
...
（同一时刻最多 check.concurrent 个节点在跑）
```

**有效性定义**（所有【启用】目标都必须通过，任一失败即淘汰）：

| 阶段 | 判定 |
|------|------|
| 有效延迟 | 节点自身 `server:port` TCP 握手成功 **且** 所有启用的 `latency_targets` 目标握手成功 |
| 有效速度 | 所有启用的 `speed_targets` 目标 HTTP 下载成功 **且** `speed_mbps > 0` |
| 有效节点 | 有效延迟 && 有效速度 |

- 延迟无效 -> 跳过速度测试，不计入有效延迟，也不计入有效节点
- 延迟有效 -> 立刻进入速度测试（无独立速度并发池，受节点并发约束）
- 有效节点达到 `check.max_valid_nodes` 后立即停止后续检测

## 更新语义

**每次更新 = 清空 + 重导入**，但只有流水线成功走到最后一步才执行。

```
拉取订阅源
  ↓
解析节点（仅保存在内存，不写入 store）
  ↓
检测（历史节点仅参与检测，不算入本次导入）
  ↓
导出（基于本次检测结果，写入 output/*.yaml/json/txt）
  ↓
导出成功 → 清空 store → 写入本次订阅源节点 → 保存 nodes.json
```

| 场景 | 节点列表变化 |
|------|--------------|
| 完整成功 | **清空** 旧列表，**写入** 本次订阅源节点 |
| 拉取订阅源失败 | **不变** |
| 解析失败 | **不变** |
| 检测中途手动停止 | **不变** |
| 检测抛出异常 | **不变** |
| 导出失败 | **不变** |

保留的用户状态（同 ID 节点）：自定义名称、启用/禁用状态、首次入库时间。测试数据使用本次检测结果覆盖。

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

## API

### 状态与控制 (8100)

| 方法 | 路径 | 说明 |
|------|------|------|
| GET    | `/api/state`                 | 当前流水线状态 |
| GET    | `/api/version`               | 版本号（来自 `app/__init__.py`） |
| GET    | `/api/targets`               | 当前延迟 / 速度目标配置 |
| POST   | `/api/trigger`               | 立即触发一次完整流水线 |
| POST   | `/api/stop`                  | 请求停止当前流水线 |

### 节点管理 (8100)

| 方法 | 路径 | 说明 |
|------|------|------|
| GET    | `/api/nodes`                 | 列出所有节点（后端分页） |
| GET    | `/api/nodes/{id}`            | 获取单个节点 |
| POST   | `/api/nodes`                 | 新增节点 |
| PATCH  | `/api/nodes/{id}`            | 修改节点（关键字段变更会自动重测） |
| DELETE | `/api/nodes/{id}`            | 删除节点 |
| POST   | `/api/nodes/{id}/enable`     | 启用节点 |
| POST   | `/api/nodes/{id}/disable`    | 禁用节点 |
| POST   | `/api/nodes/{id}/retest`     | 立即重测 |
| POST   | `/api/nodes/batch`           | 批量操作 (`enable`/`disable`/`delete`/`retest`) |

### WebSocket

| 路径 | 说明 |
|------|------|
| `/ws/status` | 实时推送 `state` / `log` / `nodes_updated` 事件 |

## 部署

```bash
docker compose up -d --build
```

访问 `http://<host>:8100` 查看实时状态与节点列表。

## 配置

配置文件位于 `config/config.yaml`，可参考 `config.example.yaml`。

### 服务器

```yaml
server:
  host: "0.0.0.0"
  web_port: 8100
  api_port: 8110
```

### 日志

```yaml
logging:
  # DEBUG / INFO / WARNING / ERROR / CRITICAL
  # 环境变量 VAEL_LOG_LEVEL 优先级更高（若设置则覆盖此处）
  level: INFO
```

### 订阅源

```yaml
subscriptions: |
  https://example.com/api/v1/client/subscribe?token=xxx
  https://example.org/sub?token=yyy
```

### 检测参数

```yaml
check:
  concurrent: 50                # 节点并发数（同时跑「延迟 -> 速度」的节点数）
  timeout_ms: 5000              # 单次 TCP 连接超时（毫秒）
  samples: 3                    # 节点自身的延迟采样次数
  include_history: false        # 是否将 store 中的历史节点并入本轮测试
  max_valid_nodes: 50           # 有效节点上限（0 表示不限制）
  schedule: "0 9,21 * * *"      # 定时任务 cron 表达式
```

### 延迟目标

```yaml
  latency_targets:
    - name: "CF"
      url: "https://www.cloudflare.com/cdn-cgi/trace"
      enabled: true
    - name: "GG"
      url: "https://www.google.com/generate_204"
      enabled: true
    - name: "GH"
      url: "https://github.com/robots.txt"
      enabled: false     # 关闭后不检测、不计入有效判定、不显示在表格
```

### 速度目标

```yaml
  speed_targets:
    - name: "CF"
      url: "https://speed.cloudflare.com/__down?bytes=2000000"
      size_hint: 2000000
      enabled: true
    - name: "GH"
      url: "https://raw.githubusercontent.com/git/git/master/README.md"
      size_hint: 500000
      enabled: true
```

- `enabled: true`：参与检测，必须成功才算有效
- `enabled: false`：完全跳过（不检测、不计入判定、不打印日志、不显示列）
- 缺省视为启用

### 输出

```yaml
output:
  max_nodes: 50                 # 导出上限（0 表示不限制）
  formats:
    - mihomo
    - singbox
    - base64
    - v2ray
    - v2ray-json
  directory: "./output"
```

### 通知（可选）

```yaml
notify:
  webhook: ""
```

## 状态字段说明

Web UI 顶部六项统计：

| 字段 | 含义 |
|------|------|
| 订阅源 | 已拉取 / 总数 |
| 延迟检测 | 已检测 / 待测总数 |
| 有效延迟 | 所有延迟目标均通过的节点数 |
| 有效速度 | 所有速度目标均通过（`> 0`）的节点数 |
| 有效节点 | 有效数量 / `max_valid_nodes`（无上限时显示 `∞`） |
| 已导出 | 实际写入订阅文件的节点数 |

节点列表表头：

- 启用的延迟目标：`<名称>` + `延迟 (ms)`
- 启用的速度目标：`<名称>` + `速度 (Mbps)`
- 关闭的目标：`<名称>` + `已关闭`（表头半透明，单元格显示灰色 `×`）
- 无数据 / 无效值：单元格显示 `—`

速度显示精度：

| 范围 | 小数位 | 示例 |
|------|--------|------|
| `< 0.1` Mbps | 3 位 | `0.063` |
| `>= 0.1` Mbps | 2 位 | `12.50` |

## 版本号

版本号定义在 `app/__init__.py`：

```python
__version__ = "1.0.0"
```

Web 界面底部会自动显示该版本；`/api/version` 提供接口访问；`/docs` 的 OpenAPI 文档也使用同一个版本号。

## 目录结构

```
.
├── app/
│   ├── __init__.py          # __version__
│   ├── main.py              # 双 uvicorn 入口
│   ├── config.py            # 配置加载
│   ├── models.py            # NodeRecord 数据模型
│   ├── core/
│   │   ├── checker.py       # 单节点流水线检测
│   │   ├── exporter.py      # 多格式订阅导出
│   │   ├── fetcher.py       # 订阅源并发拉取
│   │   ├── parser.py        # YAML / Base64 / URI 解析
│   │   ├── startup.py       # 后台流水线编排
│   │   └── store.py         # 节点持久化（bulk_upsert / replace_all）
│   ├── utils/
│   │   ├── logger.py        # 日志（级别可配 + WS 广播）
│   │   └── state.py         # 全局状态 / WebSocket 广播
│   └── web/
│       ├── app.py           # Web 服务（8100）
│       ├── api.py           # API 服务（8110）
│       ├── ws.py            # WebSocket 端点
│       └── static/          # 前端资源
├── config.example.yaml
├── docker-compose.yml
├── requirements.txt
└── README.md
```

## 前端缓存

静态资源（`app.js` / `style.css`）响应头包含：

```
Cache-Control: no-cache, no-store, must-revalidate
Pragma: no-cache
Expires: 0
```

`index.html` 由后端动态注入资源版本号（取 `app.js` / `style.css` 的文件 mtime）：

```html
<link rel="stylesheet" href="/static/style.css?v=1730000000-1730000001">
<script src="/static/app.js?v=1730000000-1730000001"></script>
```

每次更新代码并重建容器后，浏览器自动拉取新版本，无需手动清缓存。

## 许可

本项目采用 **GNU General Public License v3.0** 发布。完整许可文本见 [LICENSE](./LICENSE) 或 <https://www.gnu.org/licenses/gpl-3.0.txt>。