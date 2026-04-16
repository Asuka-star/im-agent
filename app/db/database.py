from pathlib import Path

from sqlalchemy import create_engine, inspect, text
from sqlalchemy.orm import declarative_base, sessionmaker

from app.core.config import settings


def _prepare_sqlite_directory(database_url: str) -> None:
    sqlite_prefix = "sqlite:///"
    if not database_url.startswith(sqlite_prefix):
        return

    db_path = database_url.removeprefix(sqlite_prefix)
    path = Path(db_path)
    if path.parent and str(path.parent) not in {"", "."}:
        path.parent.mkdir(parents=True, exist_ok=True)


_prepare_sqlite_directory(settings.database_url)

connect_args = {"check_same_thread": False} if settings.database_url.startswith("sqlite") else {}
engine = create_engine(settings.database_url, connect_args=connect_args)
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
Base = declarative_base()


def init_db() -> None:
    from app.db.models import Memory, MemoryChunk, Message, Session, Task  # noqa: F401

    _prepare_database_extensions()
    Base.metadata.create_all(bind=engine)
    _run_lightweight_migrations()


def _prepare_database_extensions() -> None:
    if not settings.database_url.startswith("postgresql"):
        return

    with engine.begin() as connection:
        connection.execute(text("CREATE EXTENSION IF NOT EXISTS vector"))


def _run_lightweight_migrations() -> None:
    inspector = inspect(engine)
    tables = set(inspector.get_table_names())
    if "messages" not in tables:
        return

    columns = {column["name"] for column in inspector.get_columns("messages")}
    if "message_id" not in columns:
        with engine.begin() as connection:
            connection.execute(text("ALTER TABLE messages ADD COLUMN message_id VARCHAR(128)"))
            connection.execute(text("CREATE UNIQUE INDEX IF NOT EXISTS ix_messages_message_id ON messages (message_id)"))
    if "sender_id" not in columns:
        with engine.begin() as connection:
            connection.execute(text("ALTER TABLE messages ADD COLUMN sender_id VARCHAR(128)"))

    if "memories" in tables:
        memory_columns = {column["name"] for column in inspector.get_columns("memories")}
        if "payload" not in memory_columns:
            with engine.begin() as connection:
                connection.execute(text("ALTER TABLE memories ADD COLUMN payload TEXT"))

    if "memory_chunks" in tables:
        chunk_columns = {column["name"] for column in inspector.get_columns("memory_chunks")}
        if "metadata_json" not in chunk_columns:
            with engine.begin() as connection:
                connection.execute(text("ALTER TABLE memory_chunks ADD COLUMN metadata_json TEXT"))

        if settings.database_url.startswith("postgresql"):
            with engine.begin() as connection:
                connection.execute(
                    text(
                        "CREATE INDEX IF NOT EXISTS ix_memory_chunks_embedding_hnsw "
                        "ON memory_chunks USING hnsw (embedding vector_cosine_ops)"
                    )
                )
