from fastapi import APIRouter, Query, WebSocket, WebSocketDisconnect

from app.services.realtime_hub import realtime_hub
from app.services.task_run_service import TaskRunService


router = APIRouter()
task_run_service = TaskRunService()


@router.websocket("/task-runs/{task_run_id}")
async def watch_task_run(websocket: WebSocket, task_run_id: str) -> None:
    room = realtime_hub.task_run_room(task_run_id)
    await realtime_hub.connect(websocket, room)
    try:
        detail = task_run_service.get_task_run(task_run_id)
        await websocket.send_json(
            {
                "type": "task_run.snapshot",
                "task_run_id": task_run_id,
                "task_run": detail.model_dump(mode="json") if detail is not None else None,
            }
        )
        while True:
            message = await websocket.receive_text()
            if message.strip().lower() == "ping":
                await websocket.send_json({"type": "pong", "task_run_id": task_run_id})
    except WebSocketDisconnect:
        realtime_hub.disconnect(websocket, room)
    except Exception:
        realtime_hub.disconnect(websocket, room)
        raise


@router.websocket("/sessions/{session_id}")
async def watch_session_task_runs(
    websocket: WebSocket,
    session_id: str,
    limit: int = Query(default=20, ge=1, le=100),
) -> None:
    room = realtime_hub.session_room(session_id)
    await realtime_hub.connect(websocket, room)
    try:
        task_runs = task_run_service.list_task_runs(session_id=session_id, limit=limit)
        await websocket.send_json(
            {
                "type": "session.snapshot",
                "session_id": session_id,
                "task_runs": [item.model_dump(mode="json") for item in task_runs],
            }
        )
        while True:
            message = await websocket.receive_text()
            if message.strip().lower() == "ping":
                await websocket.send_json({"type": "pong", "session_id": session_id})
    except WebSocketDisconnect:
        realtime_hub.disconnect(websocket, room)
    except Exception:
        realtime_hub.disconnect(websocket, room)
        raise
