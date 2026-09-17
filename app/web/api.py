"""API / 订阅输出服务 (默认 8110)。"""

from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse

from app.config import load_base_config
from app.utils.state import state

FORMAT_MAP = {
    "mihomo": ("mihomo.yaml", "text/yaml; charset=utf-8"),
    "clash": ("mihomo.yaml", "text/yaml; charset=utf-8"),
    "singbox": ("singbox.json", "application/json; charset=utf-8"),
    "sing-box": ("singbox.json", "application/json; charset=utf-8"),
    "base64": ("base64.txt", "text/plain; charset=utf-8"),
    "v2ray": ("base64.txt", "text/plain; charset=utf-8"),
}


def create_api_app() -> FastAPI:
    app = FastAPI(title="Vael-Mux API", version="1.0.0")

    @app.get("/health")
    async def health():
        return {"status": "ok"}

    @app.get("/api/status")
    async def status():
        return state.snapshot()

    @app.get("/sub/{fmt}")
    async def get_sub(fmt: str):
        key = fmt.lower()
        if key not in FORMAT_MAP:
            raise HTTPException(status_code=404, detail=f"未知订阅格式: {fmt}")

        filename, media_type = FORMAT_MAP[key]
        config = load_base_config()
        out_dir = Path(config["output"].get("directory", "./output"))
        path = out_dir / filename

        if not path.exists():
            raise HTTPException(
                status_code=503,
                detail="订阅文件尚未生成，请等待首次检测完成",
            )

        return FileResponse(
            str(path),
            media_type=media_type,
            filename=filename,
            headers={
                "Cache-Control": "no-store",
                "Profile-Update-Interval": "12",
            },
        )

    return app
