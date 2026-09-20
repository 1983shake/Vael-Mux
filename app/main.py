"""Vael-Mux 主入口。

同时启动两个 uvicorn 服务：
  - Web 管理界面 (固定 8100)
  - API / 订阅输出 (固定 8110)

内部端口固定不变，对外端口由 docker-compose.yml 的 ports 映射控制。
例如 docker-compose.yml 里写 "18100:8100"，则通过 18100 访问 Web。

配置：
  - 启动时确保 config.yaml 存在，不存在则创建默认配置
  - 加载时自动执行配置自修复（缺失字段 / 类型错误 / 越界值等）
"""

import asyncio
import sys

import uvicorn

from app.models import ensure_config_file, load_base_config
from app.runtime import logger
from app.web.server import create_api_app, create_web_app

# 内部端口固定，不接受配置文件覆盖。
# 如需调整对外端口，请修改 docker-compose.yml 的 ports 映射。
WEB_PORT = 8100
API_PORT = 8110


async def _serve() -> None:
    config = load_base_config()
    server_cfg = config.get("server", {})
    host = server_cfg.get("host", "0.0.0.0")

    web_app = create_web_app()
    api_app = create_api_app()

    web_server = uvicorn.Server(
        uvicorn.Config(
            web_app,
            host=host,
            port=WEB_PORT,
            log_level="info",
            access_log=False,
        )
    )
    api_server = uvicorn.Server(
        uvicorn.Config(
            api_app,
            host=host,
            port=API_PORT,
            log_level="warning",
            access_log=False,
        )
    )

    logger.info(f"Web 管理界面（内部）: http://{host}:{WEB_PORT}")
    logger.info(f"API / 订阅输出（内部）: http://{host}:{API_PORT}")
    logger.info("对外端口由 docker-compose.yml 的 ports 映射决定")

    await asyncio.gather(web_server.serve(), api_server.serve())


def main() -> None:
    # 启动前确保配置文件存在（不存在则创建默认配置）
    try:
        cfg_path = ensure_config_file()
        logger.info(f"配置文件：{cfg_path}")
    except Exception as e:
        logger.error(f"确保配置文件存在时出错：{e}")

    try:
        asyncio.run(_serve())
    except KeyboardInterrupt:
        logger.info("收到中断信号，正在退出...")
        sys.exit(0)


if __name__ == "__main__":
    main()
