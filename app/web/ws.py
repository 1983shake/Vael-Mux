"""WebSocket 实时状态端点。"""

from fastapi import APIRouter, WebSocket, WebSocketDisconnect

from app.utils.state import state

ws_router = APIRouter()


@ws_router.websocket("/ws/status")
async def ws_status(websocket: WebSocket) -> None:
    await websocket.accept()
    state.subscribers.append(websocket)
    try:
        await websocket.send_json({"event": "state", **state.snapshot()})
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
