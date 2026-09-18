"""Vael-Mux 主入口。

同时启动两个 uvicorn 服务：
  - Web 管理界面 (默认 8100)
  - API / 订阅输出 (默认 8110)

Web 服务先就绪，完整流水线在 Web 的 lifespan 中以后台任务方式启动。
"""

import asyncio
import sys

import uvicorn

from app.config import load_base_config
from app.utils.runtime import logger
from app.web.api import create_api_app
from app.web.app import create_web_app


async def _serve() -> None:
    config = load_base_config()
    server_cfg = config.get("server", {})
    host = server_cfg.get("host", "0.0.0.0")
    web_port = int(server_cfg.get("web_port", 8100))
    api_port = int(server_cfg.get("api_port", 8110))

    web_app = create_web_app()
    api_app = create_api_app()

    web_server = uvicorn.Server(
        uvicorn.Config(
            web_app,
            host=host,
            port=web_port,
            log_level="info",
            access_log=False,
        )
    )
    api_server = uvicorn.Server(
        uvicorn.Config(
            api_app,
            host=host,
            port=api_port,
            log_level="warning",
            access_log=False,
        )
    )

    logger.info(f"Web 管理界面: http://{host}:{web_port}")
    logger.info(f"API / 订阅输出: http://{host}:{api_port}")

    await asyncio.gather(web_server.serve(), api_server.serve())


def main() -> None:
    try:
        asyncio.run(_serve())
    except KeyboardInterrupt:
        logger.info("收到中断信号，正在退出...")
        sys.exit(0)


if __name__ == "__main__":
    main()
