# Vael-Mux

订阅聚合 / 节点检测 / 多格式订阅输出的容器化服务。

## 特性

- Web 服务先行启动，后台异步执行完整流水线
- WebSocket 实时推送进度，日志实时上屏
- Web 控制台可视化编辑全部配置，保存后自动重载生效
- 订阅源在配置中以换行方式集中管理
- 支持输出 mihomo / sing-box / base64 / v2ray / v2ray-json
- 单节点流水线检测：**延迟通过 → 立刻测速**，节点级并发
- 测试目标独立开关（`enabled: true/false`）：关闭的目标不检测、不计入判定、不显示
- 有效延迟 / 有效速度支持两种判定模式：**全部通过（all）** 或 **任一通过（any）**
- 有效节点上限（`check.max_valid_nodes`），达到上限立即停止后续检测
- **更新即重建**：只有检测导出全部成功才清空并写入本次节点列表
  - 中途失败 / 手动停止 → 节点列表保持不变，不导入任何新节点
- 流水线可随时手动停止（初始启动 / 手动触发 / 定时触发均支持）
- 三级去重提示：源内重复 / ID 冲突 / 待测列表重复
- 配置文件首次启动自动创建；加载时自动自修复（缺失字段 / 类型错误 / 越界值 / 非法枚举 / cron 语法）
- 日志级别可配置（`logging.level` / 环境变量 `VAEL_LOG_LEVEL`）
- 每次检测地址结果打印到日志（节点自身 / 每个启用目标）
- 速度值严格过滤 `> 0`：`0.0` 视为无效
- 表格列头带单位（`延迟 (ms)` / `速度 (Mbps)`），单元格只显示数值
- 节点列表持久化到 `output/nodes.json`，支持在 Web 页面增删改查
- 修改关键字段后自动重测，重新生成订阅
- 支持定时调度（cron）
- 静态资源免缓存（`no-store` + 资源版本号），前端更新后立即生效
- 底部展示版权、GitHub 图标链接、版本
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

**有效性定义**：

| 阶段 | 判定 |
|------|------|
| 有效延迟 | 节点自身 `server:port` TCP 握手成功 **且** 按 `latency_mode` 判定各启用延迟目标 |
| 有效速度 | 按 `speed_mode` 判定各启用速度目标（`speed_mbps > 0`） |
| 有效节点 | 有效延迟 && 有效速度 |

**判定模式**：

| 模式 | 含义 |
|------|------|
| `all`（默认） | 所有【启用】目标都必须通过 |
| `any` | 至少一个【启用】目标通过 |

- 无启用目标时（全部 `enabled: false`），两种模式均视为通过
- 有效延迟中，节点自身 TCP 握手成功是**硬前提**，无论 `all` / `any`
- 延迟无效 → 跳过速度测试，不计入有效延迟，也不计入有效节点
- 延迟有效 → 立刻进入速度测试（无独立速度并发池，受节点并发约束）
- 有效节点达到 `check.max_valid_nodes` 后立即停止后续检测

## 更新语义

**每次更新 = 清空 + 重导入**，但只有流水线成功走到最后一步才执行。

```
拉取订阅源
  ↓
解析节点（仅保存在内存，不写入 store）
  ↓
去重（源内 / ID 冲突 / 待测列表），输出统计
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

| 端口 | 用途 | 说明 |
|------|------|------|
| 8100 | Web 管理界面 | **内部固定** |
| 8110 | API / 订阅输出 | **内部固定** |

- 内部端口不受配置文件管理，始终为 8100 / 8110
- **对外端口由 `docker-compose.yml` 的 `ports` 映射控制**

```yaml
ports:
  - "8100:8100"     # 外部 8100 -> 内部 Web
  - "8110:8110"     # 外部 8110 -> 内部 API
```

如需改为对外 `18100` / `18110`：

```yaml
ports:
  - "18100:8100"
  - "18110:8110"
```

## 订阅端点

| 端点 | 说明 |
|------|------|
| `http://<host>:<对外API端口>/sub/mihomo`     | Mihomo / Clash.Meta YAML |
| `http://<host>:<对外API端口>/sub/singbox`    | sing-box JSON |
| `http://<host>:<对外API端口>/sub/v2ray`      | v2rayN / v2rayNG / v2rayA 订阅 |
| `http://<host>:<对外API端口>/sub/v2ray-json` | v2ray 完整 config.json |
| `http://<host>:<对外API端口>/sub/base64`     | 通用 Base64 订阅 |

## API

### 状态与控制（8100）

| 方法 | 路径 | 说明 |
|------|------|------|
| GET    | `/api/state`                 | 当前流水线状态 |
| GET    | `/api/version`               | 版本号 |
| GET    | `/api/targets`               | 当前延迟 / 速度目标配置及判定模式 |
| POST   | `/api/trigger`               | 立即触发一次完整流水线 |
| POST   | `/api/stop`                  | 请求停止当前流水线 |

### 配置管理（8100）

| 方法 | 路径 | 说明 |
|------|------|------|
| GET    | `/api/config`                | 读取当前配置（不含内部字段） |
| PUT    | `/api/config`                | 保存配置并自动重载 |

### 节点管理（8100）

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

首次启动时，程序会在 `config/config.yaml` 自动创建一份包含默认延迟 / 速度目标的配置。挂载目录 `./config` 需可写。

访问 `http://<host>:8100` 查看实时状态与节点列表。

## 配置

配置文件位于 `config/config.yaml`，由程序自动创建与自修复。可通过 Web 控制台「配置」按钮可视化编辑，也可以直接编辑文件（保存后重新加载生效）。

### 服务器

```yaml
server:
  # 容器内监听地址；保持 0.0.0.0 让 docker 端口映射生效
  host: "0.0.0.0"
```

> 端口不在此处配置：内部固定 8100 / 8110，对外由 `docker-compose.yml` 控制。

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
  # 每行一个订阅源，支持 # 注释
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

  latency_mode: all             # all：全部启用目标通过 / any：任一启用目标通过
  speed_mode: all               # 同上

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
  # 注：output.max_nodes 已废弃，不再作为独立导出上限。
  #     导出数量由 check.max_valid_nodes 决定。
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

## 配置自修复

程序在每次加载配置时会执行以下修复。只要有实际修复，就会把修复后的内容原子写回磁盘，并在日志中打出 `[WARNING] 配置自修复：...`。

| 问题 | 处理 |
|------|------|
| 配置文件不存在 | 用内置默认配置创建 |
| 段落类型错误（如 `check` 不是 dict） | 重置为空对象并补默认值 |
| 字段缺失（如未写 `concurrent`） | 补默认值 |
| 类型错误（如 `concurrent: "abc"`） | 回退默认值 |
| 越界值（如 `concurrent: 0`） | 裁剪到合法范围 |
| 枚举值非法（如 `latency_mode: "foo"`） | 回退 `all` |
| 日志级别非法 | 回退 `INFO` |
| cron 表达式非法 | 清空 |
| `targets` 缺 url / 格式错误 | 跳过该条目并记录 |
| 旧字段 `server.web_port` / `server.api_port` | 移除 |

> 修复会重写整个文件，YAML 注释会被覆盖。

## 去重提示

解析阶段会输出三级去重统计：

1. **源内重复**：不同订阅源之间的重复节点，按 `(type, server, port, uuid|password)` 合并
2. **ID 冲突**：不同源字段组合后生成相同 `id` 的节点，按 `id` 去重
3. **待测列表重复**：历史节点与新节点合并后仍存在的重复

日志与状态消息都会给出「原始数 / 去重后数 / 移除数」。

## 状态字段说明

Web UI 顶部六项统计：

| 字段 | 含义 |
|------|------|
| 订阅源 | 已拉取 / 总数 |
| 延迟检测 | 已检测 / 待测总数 |
| 有效延迟 | 所有延迟目标均通过的节点数（按 `latency_mode` 判定） |
| 有效速度 | 所有速度目标均通过的节点数（按 `speed_mode` 判定） |
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
__version__ = "2.0.0"
```

Web 界面底部会自动显示该版本；`/api/version` 提供接口访问；`/docs` 的 OpenAPI 文档也使用同一个版本号。

## 目录结构

```
.
├── app/
│   ├── __init__.py          # __version__
│   ├── main.py              # 双 uvicorn 入口
│   ├── models.py            # 配置（加载 / 保存 / 自修复） + NodeRecord
│   ├── ingest.py            # 订阅源拉取 + 内容解析 + 去重
│   ├── checker.py           # 单节点流水线检测
│   ├── exporter.py          # 多格式订阅导出
│   ├── store.py             # 节点持久化（bulk_upsert / replace_all）
│   ├── pipeline.py          # 后台流水线编排 + 调度器
│   ├── runtime.py           # 日志 + 全局状态 + WebSocket 广播
│   └── web/
│       ├── __init__.py
│       ├── server.py        # Web (8100) + API (8110)
│       └── static/          # 前端资源
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