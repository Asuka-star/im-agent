from sqlalchemy import select

from app.db.database import SessionLocal
from app.db.models import AppSetting


class AppStateService:
    """Stores small app-level runtime state in the database."""

    def get_value(self, key: str) -> str | None:
        with SessionLocal() as session:
            row = session.execute(select(AppSetting).where(AppSetting.key == key)).scalar_one_or_none()
            if row is None:
                return None
            value = (row.value or "").strip()
            return value or None

    def set_value(self, key: str, value: str) -> None:
        normalized = value.strip()
        with SessionLocal() as session:
            row = session.execute(select(AppSetting).where(AppSetting.key == key)).scalar_one_or_none()
            if row is None:
                session.add(AppSetting(key=key, value=normalized))
            else:
                row.value = normalized
            session.commit()
