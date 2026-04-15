from sqlalchemy import select

from app.db.database import SessionLocal
from app.db.models import Message


class MessageDedupService:
    """Ensures the same inbound Feishu message is handled only once."""

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

            session.add(
                Message(
                    message_id=message_id,
                    session_id=session_id,
                    role="user",
                    content=content,
                )
            )
            session.commit()
