import json
from typing import Any

from sqlalchemy import delete, desc, select

from app.db.database import SessionLocal
from app.db.models import Memory, Message, Session, Task
from app.schemas.analyze import AnalyzeResponse


class MemoryService:
    """Stores chat history and task snapshots, and builds lightweight context windows."""

    def ensure_session(self, session_id: str) -> None:
        with SessionLocal() as session:
            existing = session.execute(
                select(Session.id).where(Session.session_id == session_id)
            ).scalar_one_or_none()
            if existing is None:
                session.add(Session(session_id=session_id))
                session.commit()

    def save_user_message(self, *, session_id: str, message_id: str | None, content: str) -> None:
        self.ensure_session(session_id)
        with SessionLocal() as session:
            if message_id:
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

    def save_assistant_message(self, *, session_id: str, content: str) -> None:
        self.ensure_session(session_id)
        with SessionLocal() as session:
            session.add(
                Message(
                    session_id=session_id,
                    role="assistant",
                    content=content,
                )
            )
            session.commit()

    def save_round(self, *, session_id: str, analysis: AnalyzeResponse) -> None:
        self.ensure_session(session_id)
        payload = {
            "summary": analysis.summary,
            "risks": analysis.risks,
            "next_actions": analysis.next_actions,
        }

        with SessionLocal() as session:
            session.add(
                Memory(
                    session_id=session_id,
                    summary=analysis.summary,
                    payload=json.dumps(payload, ensure_ascii=False),
                )
            )

            session.execute(delete(Task).where(Task.session_id == session_id))
            for task in analysis.tasks:
                session.add(
                    Task(
                        session_id=session_id,
                        title=task.title,
                        owner=task.owner,
                        priority=task.priority,
                        due_date=task.due_date,
                        status=task.status,
                        notes=task.notes,
                    )
                )

            session.commit()

    def build_context_block(self, session_id: str, limit: int = 6) -> str:
        with SessionLocal() as session:
            messages = (
                session.execute(
                    select(Message)
                    .where(Message.session_id == session_id)
                    .order_by(desc(Message.id))
                    .limit(limit)
                )
                .scalars()
                .all()
            )

            tasks = (
                session.execute(
                    select(Task)
                    .where(Task.session_id == session_id)
                    .order_by(Task.id.asc())
                )
                .scalars()
                .all()
            )

            memories = (
                session.execute(
                    select(Memory)
                    .where(Memory.session_id == session_id)
                    .order_by(desc(Memory.id))
                    .limit(3)
                )
                .scalars()
                .all()
            )

        if not messages and not tasks and not memories:
            return ""

        lines: list[str] = ["[历史上下文]"]

        if messages:
            lines.append("最近消息：")
            for message in reversed(messages):
                role = "用户" if message.role == "user" else "助手"
                lines.append(f"- {role}: {message.content}")

        if tasks:
            lines.append("当前任务快照：")
            for task in tasks:
                lines.append(
                    f"- {task.title} | 负责人: {task.owner} | 截止: {task.due_date} | 优先级: {task.priority} | 状态: {task.status}"
                )

        if memories:
            lines.append("最近总结：")
            for memory in reversed(memories):
                lines.append(f"- {memory.summary}")

        return "\n".join(lines)
