import json
import logging
import threading

from sqlalchemy import delete, desc, select

from app.db.database import SessionLocal
from app.db.models import Memory, MemoryChunk, Message, Session, Task
from app.schemas.analyze import AnalyzeResponse
from app.services.embeddings import EmbeddingService

logger = logging.getLogger(__name__)


class MemoryService:
    """Stores discussion history, task snapshots, and hybrid memory context."""

    def __init__(self) -> None:
        self.embedding_service = EmbeddingService()

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
        embed: bool = True,
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

        self.save_memory_chunk(
            session_id=session_id,
            source_type="message",
            source_id=message_id,
            content=content,
            metadata={"sender_id": sender_id or "", "role": "user"},
            embed=embed,
        )

    def save_assistant_message(self, *, session_id: str, content: str, embed: bool = True) -> None:
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

        self.save_memory_chunk(
            session_id=session_id,
            source_type="assistant_reply",
            source_id=None,
            content=content,
            metadata={"role": "assistant"},
            embed=embed,
        )

    def save_round(
        self,
        *,
        session_id: str,
        analysis: AnalyzeResponse,
        embed: bool = True,
        async_embed: bool = False,
    ) -> None:
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

        chunk_kwargs = {
            "session_id": session_id,
            "source_type": "summary",
            "source_id": None,
            "content": analysis.summary,
            "metadata": {
                "risks": analysis.risks,
                "next_actions": analysis.next_actions,
                "task_count": len(analysis.tasks),
            },
            "embed": embed,
        }
        if async_embed and embed and self.embedding_service.is_configured():
            threading.Thread(
                target=self.save_memory_chunk,
                kwargs=chunk_kwargs,
                daemon=True,
            ).start()
        else:
            self.save_memory_chunk(**chunk_kwargs)

    def save_memory_chunk(
        self,
        *,
        session_id: str,
        source_type: str,
        source_id: str | None,
        content: str,
        metadata: dict | None = None,
        embed: bool = True,
    ) -> None:
        if not content.strip():
            return

        embedding = None
        if embed and self.embedding_service.is_configured():
            try:
                embedding = self.embedding_service.embed_text(content)
            except Exception:
                embedding = None

        with SessionLocal() as session:
            session.add(
                MemoryChunk(
                    session_id=session_id,
                    source_type=source_type,
                    source_id=source_id,
                    content=content,
                    metadata_json=json.dumps(metadata or {}, ensure_ascii=False),
                    embedding=embedding,
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

    def search_relevant_memories(
        self,
        session_id: str,
        query_text: str,
        *,
        limit: int = 5,
    ) -> list[MemoryChunk]:
        if not self.embedding_service.is_configured() or not query_text.strip():
            return []

        try:
            query_embedding = self.embedding_service.embed_text(query_text)
        except Exception as exc:  # noqa: BLE001
            logger.warning("Embedding retrieval skipped due to embedding error: %s", exc)
            return []

        with SessionLocal() as session:
            statement = (
                select(MemoryChunk)
                .where(
                    MemoryChunk.session_id == session_id,
                    MemoryChunk.embedding.is_not(None),
                )
                .order_by(MemoryChunk.embedding.cosine_distance(query_embedding))
                .limit(limit)
            )
            return session.execute(statement).scalars().all()

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

    def build_workspace_context(
        self,
        session_id: str,
        *,
        include_pending: bool = True,
        exclude_message_id: str | None = None,
        query_text: str | None = None,
        include_semantic_search: bool = True,
    ) -> str:
        tasks = self.get_current_tasks(session_id)
        memories = self.get_recent_memories(session_id)
        recent_messages = self.get_recent_messages(session_id)
        retrieved_chunks = (
            self.search_relevant_memories(session_id, query_text or "", limit=5)
            if include_semantic_search
            else []
        )
        pending_block = (
            self.build_discussion_block(session_id, exclude_message_id=exclude_message_id)
            if include_pending
            else ""
        )

        if not tasks and not memories and not recent_messages and not pending_block and not retrieved_chunks:
            return ""

        lines: list[str] = ["[协作上下文]"]

        if pending_block:
            lines.append(pending_block)

        if tasks:
            lines.append("[当前任务快照]")
            for task in tasks:
                lines.append(
                    f"- {task.title} | 负责人: {task.owner} | 截止: {task.due_date} | 优先级: {task.priority} | 状态: {task.status}"
                )

        if memories:
            lines.append("[最近总结]")
            for memory in reversed(memories):
                lines.append(f"- {memory.summary}")

        if retrieved_chunks:
            lines.append("[相关历史记忆]")
            for chunk in retrieved_chunks:
                lines.append(f"- ({chunk.source_type}) {chunk.content}")

        if recent_messages:
            lines.append("[最近消息]")
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
