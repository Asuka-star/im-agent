import threading

from sqlalchemy import select

from app.db.database import SessionLocal
from app.db.models import Message


class MessageDedupService:
    """Ensures the same inbound Feishu message is handled only once."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._in_progress: set[str] = set()

    def accept_for_processing(self, message_id: str | None) -> bool:
        if not message_id:
            return True

        with self._lock:
            if message_id in self._in_progress:
                return False
            if self.already_processed(message_id):
                return False
            self._in_progress.add(message_id)
            return True

    def finish_processing(self, message_id: str | None) -> None:
        if not message_id:
            return
        with self._lock:
            self._in_progress.discard(message_id)

    def already_processed(self, message_id: str | None) -> bool:
        if not message_id:
            return False

        with SessionLocal() as session:
            stmt = select(Message.id).where(Message.message_id == message_id)
            return session.execute(stmt).scalar_one_or_none() is not None

    def mark_processed(self, *, message_id: str | None, session_id: str, content: str) -> None:
        if not message_id:
            return

        with SessionLocal() as session:
            existing = session.execute(
                select(Message.id).where(Message.message_id == message_id)
            ).scalar_one_or_none()
            if existing is not None:
                return

            session.add(Message(message_id=message_id, session_id=session_id, role="user", content=content))
            session.commit()
