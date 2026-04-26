from pgvector.sqlalchemy import Vector
from sqlalchemy import Column, DateTime, Integer, String, Text, func

from app.core.config import settings
from app.db.database import Base


class Session(Base):
    __tablename__ = "sessions"

    id = Column(Integer, primary_key=True, index=True)
    session_id = Column(String(128), unique=True, nullable=False, index=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)


class AppSetting(Base):
    __tablename__ = "app_settings"

    id = Column(Integer, primary_key=True, index=True)
    key = Column(String(128), unique=True, nullable=False, index=True)
    value = Column(Text, nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False)


class UserAlias(Base):
    __tablename__ = "user_aliases"

    id = Column(Integer, primary_key=True, index=True)
    session_id = Column(String(128), nullable=False, index=True)
    user_id = Column(String(128), nullable=True, index=True)
    open_id = Column(String(128), nullable=True, index=True)
    union_id = Column(String(128), nullable=True, index=True)
    display_name = Column(String(128), nullable=False)
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)


class Episode(Base):
    __tablename__ = "episodes"

    id = Column(Integer, primary_key=True, index=True)
    session_id = Column(String(128), nullable=False, index=True)
    status = Column(String(32), nullable=False, default="active", index=True)
    title = Column(String(255), nullable=True)
    opened_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    closed_at = Column(DateTime(timezone=True), nullable=True)


class Message(Base):
    __tablename__ = "messages"

    id = Column(Integer, primary_key=True, index=True)
    message_id = Column(String(128), unique=True, nullable=True, index=True)
    session_id = Column(String(128), nullable=False, index=True)
    episode_id = Column(Integer, nullable=True, index=True)
    role = Column(String(32), nullable=False)
    sender_id = Column(String(128), nullable=True)
    mentions_json = Column(Text, nullable=True)
    content = Column(Text, nullable=False)
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)


class Task(Base):
    __tablename__ = "tasks"

    id = Column(Integer, primary_key=True, index=True)
    session_id = Column(String(128), nullable=False, index=True)
    title = Column(String(255), nullable=False)
    owner = Column(String(128), nullable=False, default="TBD")
    priority = Column(String(32), nullable=False, default="medium")
    due_date = Column(String(64), nullable=False, default="TBD")
    status = Column(String(64), nullable=False, default="draft")
    notes = Column(Text, nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)


class TaskChangeLog(Base):
    __tablename__ = "task_change_logs"

    id = Column(Integer, primary_key=True, index=True)
    session_id = Column(String(128), nullable=False, index=True)
    episode_id = Column(Integer, nullable=True, index=True)
    action = Column(String(32), nullable=False, index=True)
    title = Column(String(255), nullable=False)
    owner = Column(String(128), nullable=False, default="TBD")
    priority = Column(String(32), nullable=False, default="medium")
    due_date = Column(String(64), nullable=False, default="TBD")
    status = Column(String(64), nullable=False, default="draft")
    notes = Column(Text, nullable=True)
    reason = Column(Text, nullable=True)
    details_json = Column(Text, nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)


class Memory(Base):
    __tablename__ = "memories"

    id = Column(Integer, primary_key=True, index=True)
    session_id = Column(String(128), nullable=False, index=True)
    summary = Column(Text, nullable=False)
    payload = Column(Text, nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)


class MemoryChunk(Base):
    __tablename__ = "memory_chunks"

    id = Column(Integer, primary_key=True, index=True)
    session_id = Column(String(128), nullable=False, index=True)
    source_type = Column(String(32), nullable=False, default="message")
    source_id = Column(String(128), nullable=True, index=True)
    content = Column(Text, nullable=False)
    metadata_json = Column(Text, nullable=True)
    embedding = Column(Vector(settings.embedding_dimensions), nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)


class TaskRun(Base):
    __tablename__ = "task_runs"

    id = Column(Integer, primary_key=True, index=True)
    task_run_id = Column(String(64), unique=True, nullable=False, index=True)
    session_id = Column(String(128), nullable=False, index=True)
    source_type = Column(String(32), nullable=False, default="group", index=True)
    source_ref = Column(String(128), nullable=True, index=True)
    trigger_message_id = Column(String(128), nullable=True, index=True)
    intent = Column(String(64), nullable=True, index=True)
    title = Column(String(255), nullable=False)
    stage = Column(String(64), nullable=False, default="queued", index=True)
    status = Column(String(64), nullable=False, default="queued", index=True)
    latest_summary = Column(Text, nullable=True)
    latest_reply_preview = Column(Text, nullable=True)
    latest_error = Column(Text, nullable=True)
    metadata_json = Column(Text, nullable=True)
    created_by = Column(String(128), nullable=True, index=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False)
    completed_at = Column(DateTime(timezone=True), nullable=True)


class TaskRunStep(Base):
    __tablename__ = "task_run_steps"

    id = Column(Integer, primary_key=True, index=True)
    task_run_id = Column(String(64), nullable=False, index=True)
    step_key = Column(String(64), nullable=False, index=True)
    title = Column(String(255), nullable=False)
    step_type = Column(String(64), nullable=False, default="system", index=True)
    status = Column(String(64), nullable=False, default="pending", index=True)
    input_json = Column(Text, nullable=True)
    output_json = Column(Text, nullable=True)
    error = Column(Text, nullable=True)
    started_at = Column(DateTime(timezone=True), nullable=True)
    finished_at = Column(DateTime(timezone=True), nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False)


class Artifact(Base):
    __tablename__ = "artifacts"

    id = Column(Integer, primary_key=True, index=True)
    artifact_id = Column(String(64), unique=True, nullable=False, index=True)
    task_run_id = Column(String(64), nullable=False, index=True)
    artifact_type = Column(String(64), nullable=False, index=True)
    provider = Column(String(64), nullable=False, default="local", index=True)
    title = Column(String(255), nullable=False)
    status = Column(String(64), nullable=False, default="ready", index=True)
    url = Column(Text, nullable=True)
    version = Column(Integer, nullable=False, default=1)
    preview_json = Column(Text, nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False)


class ConfirmationRequest(Base):
    __tablename__ = "confirmation_requests"

    id = Column(Integer, primary_key=True, index=True)
    confirmation_id = Column(String(64), unique=True, nullable=False, index=True)
    task_run_id = Column(String(64), nullable=False, index=True)
    prompt = Column(Text, nullable=False)
    options_json = Column(Text, nullable=True)
    status = Column(String(64), nullable=False, default="pending", index=True)
    answer_value = Column(Text, nullable=True)
    answered_by = Column(String(128), nullable=True)
    answered_at = Column(DateTime(timezone=True), nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False)
