"""WebSocket 实时状态端点。"""

from fastapi import APIRouter, WebSocket, WebSocketDisconnect

from app.utils.state import state

ws_router = APIRouter()


@ws_router.websocket("/ws/status")
async def ws_status(websocket: WebSocket) -> None:
    await websocket.accept()
    state.subscribers.append(websocket)
    try:
        # 立刻推送一份当前快照，新连接无需等待
        await websocket.send_json(state.snapshot())
        while True:
            # 客户端只需保持连接，服务端主动推送
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
