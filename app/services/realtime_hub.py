import asyncio
import logging
from collections import defaultdict
from threading import Lock

from fastapi import WebSocket


logger = logging.getLogger(__name__)


class RealtimeHub:
    """A tiny in-memory pub/sub hub for dashboard websocket subscribers."""

    def __init__(self) -> None:
        self._loop: asyncio.AbstractEventLoop | None = None
        self._rooms: dict[str, set[WebSocket]] = defaultdict(set)
        self._lock = Lock()

    def bind_loop(self, loop: asyncio.AbstractEventLoop) -> None:
        self._loop = loop

    def task_run_room(self, task_run_id: str) -> str:
        return f"task-run:{task_run_id}"

    def session_room(self, session_id: str) -> str:
        return f"session:{session_id}"

    def all_task_runs_room(self) -> str:
        return "task-runs:all"

    async def connect(self, websocket: WebSocket, room: str) -> None:
        await websocket.accept()
        with self._lock:
            self._rooms[room].add(websocket)

    def disconnect(self, websocket: WebSocket, room: str) -> None:
        with self._lock:
            sockets = self._rooms.get(room)
            if not sockets:
                return
            sockets.discard(websocket)
            if not sockets:
                self._rooms.pop(room, None)

    async def broadcast(self, room: str, message: dict) -> None:
        with self._lock:
            sockets = list(self._rooms.get(room, set()))

        stale: list[WebSocket] = []
        for socket in sockets:
            try:
                await socket.send_json(message)
            except Exception as exc:  # noqa: BLE001
                logger.warning("Realtime broadcast failed for room=%s: %s", room, exc)
                stale.append(socket)

        if stale:
            with self._lock:
                current = self._rooms.get(room, set())
                for socket in stale:
                    current.discard(socket)
                if not current:
                    self._rooms.pop(room, None)

    def emit_room(self, room: str, message: dict) -> None:
        if self._loop is None:
            return
        self._loop.call_soon_threadsafe(asyncio.create_task, self.broadcast(room, message))


realtime_hub = RealtimeHub()
