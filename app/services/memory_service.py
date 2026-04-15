import json

from sqlalchemy import delete, desc, select

from app.db.database import SessionLocal
from app.db.models import Memory, Message, Session, Task
from app.schemas.analyze import AnalyzeResponse


class MemoryService:
    """Stores discussion history, task snapshots, and lightweight session context."""

    def ensure_session(self, session_id: str) -> None:
        with SessionLocal() as session:
            existing = session.execute(
                select(Session.id).where(Session.session_id == session_id)
            ).scalar_one_or_none()
            if existing is None:
                session.add(Session(session_id=session_id))
                session.commit()

    def save_user_message(
        self,
        *,
        session_id: str,
        message_id: str | None,
        sender_id: str | None,
        content: str,
    ) -> None:
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
                    sender_id=sender_id,
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
                    sender_id="assistant",
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

    def get_recent_messages(self, session_id: str, limit: int = 12) -> list[Message]:
        with SessionLocal() as session:
            return (
                session.execute(
                    select(Message)
                    .where(Message.session_id == session_id)
                    .order_by(desc(Message.id))
                    .limit(limit)
                )
                .scalars()
                .all()
            )

    def get_current_tasks(self, session_id: str) -> list[Task]:
        with SessionLocal() as session:
            return (
                session.execute(
                    select(Task)
                    .where(Task.session_id == session_id)
                    .order_by(Task.id.asc())
                )
                .scalars()
                .all()
            )

    def get_recent_memories(self, session_id: str, limit: int = 3) -> list[Memory]:
        with SessionLocal() as session:
            return (
                session.execute(
                    select(Memory)
                    .where(Memory.session_id == session_id)
                    .order_by(desc(Memory.id))
                    .limit(limit)
                )
                .scalars()
                .all()
            )

    def get_pending_user_messages(
        self,
        session_id: str,
        *,
        exclude_message_id: str | None = None,
        limit: int = 20,
    ) -> list[Message]:
        with SessionLocal() as session:
            last_assistant_id = session.execute(
                select(Message.id)
                .where(Message.session_id == session_id, Message.role == "assistant")
                .order_by(desc(Message.id))
                .limit(1)
            ).scalar_one_or_none()

            statement = (
                select(Message)
                .where(Message.session_id == session_id, Message.role == "user")
                .order_by(Message.id.asc())
            )
            if last_assistant_id is not None:
                statement = statement.where(Message.id > last_assistant_id)
            if exclude_message_id:
                statement = statement.where(Message.message_id != exclude_message_id)

            messages = session.execute(statement).scalars().all()
            if limit and len(messages) > limit:
                return messages[-limit:]
            return messages

    def build_discussion_block(
        self,
        session_id: str,
        *,
        exclude_message_id: str | None = None,
        limit: int = 20,
    ) -> str:
        messages = self.get_pending_user_messages(
            session_id,
            exclude_message_id=exclude_message_id,
            limit=limit,
        )
        if not messages:
            return ""

        lines = ["[近期群聊讨论]"]
        for message in messages:
            speaker = message.sender_id or "成员"
            lines.append(f"- {speaker}: {message.content}")
        return "\n".join(lines)

    def build_workspace_context(self, session_id: str, *, include_pending: bool = True) -> str:
        tasks = self.get_current_tasks(session_id)
        memories = self.get_recent_memories(session_id)
        recent_messages = self.get_recent_messages(session_id)
        pending_block = self.build_discussion_block(session_id) if include_pending else ""

        if not tasks and not memories and not recent_messages and not pending_block:
            return ""

        lines: list[str] = ["[协作上下文]"]

        if pending_block:
            lines.append(pending_block)

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

        if recent_messages:
            lines.append("最近消息：")
            for message in reversed(recent_messages[-6:]):
                role = "群成员" if message.role == "user" else "助手"
                speaker = message.sender_id or role
                lines.append(f"- {speaker}: {message.content}")

        return "\n".join(lines)

    def load_memory_payload(self, session_id: str) -> dict:
        memories = self.get_recent_memories(session_id, limit=1)
        if not memories or not memories[0].payload:
            return {}

        try:
            payload = json.loads(memories[0].payload)
        except json.JSONDecodeError:
            return {}

        return payload if isinstance(payload, dict) else {}
