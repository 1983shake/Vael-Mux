"""WebSocket 实时状态端点。"""

from fastapi import APIRouter, WebSocket, WebSocketDisconnect

from app.utils.logger import get_recent_logs
from app.utils.state import state

ws_router = APIRouter()

# 连接建立时回填的历史日志条数
BACKFILL_LOGS = 200


@ws_router.websocket("/ws/status")
async def ws_status(websocket: WebSocket) -> None:
    await websocket.accept()
    state.subscribers.append(websocket)
    try:
        # 1) 当前状态
        await websocket.send_json({"event": "state", **state.snapshot()})

        # 2) 回填最近日志
        for entry in get_recent_logs(BACKFILL_LOGS):
            try:
                await websocket.send_json({"event": "log", **entry})
            except Exception:
                break

        # 3) 保持连接（客户端不发消息，仅接收推送）
        while True:
            await websocket.receive_text()
    except WebSocketDisconnect:
        pass
    except Exception:
        pass
    finally:
        try:
            state.subscribers.remove(websocket)
        except ValueError:
            pass
