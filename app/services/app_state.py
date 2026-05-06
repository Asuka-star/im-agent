from __future__ import annotations

import json
from typing import Any, Callable, TypeVar

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from app.db.database import SessionLocal
from app.db.models import AppSetting

T = TypeVar("T")


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
        while True:
            with SessionLocal() as session:
                row = session.execute(
                    select(AppSetting).where(AppSetting.key == key).with_for_update()
                ).scalar_one_or_none()
                if row is None:
                    session.add(AppSetting(key=key, value=normalized))
                else:
                    row.value = normalized
                try:
                    session.commit()
                    return
                except IntegrityError:
                    session.rollback()

    def update_json_value(
        self,
        key: str,
        *,
        default: T,
        updater: Callable[[T], T],
    ) -> T:
        while True:
            with SessionLocal() as session:
                row = session.execute(
                    select(AppSetting).where(AppSetting.key == key).with_for_update()
                ).scalar_one_or_none()
                if row is None:
                    current = self._clone_json_value(default)
                    row = AppSetting(
                        key=key,
                        value=json.dumps(current, ensure_ascii=False),
                    )
                    session.add(row)
                else:
                    current = self._load_json_value(row.value, default)

                updated = updater(self._clone_json_value(current))
                row.value = json.dumps(updated, ensure_ascii=False)
                try:
                    session.commit()
                    return self._clone_json_value(updated)
                except IntegrityError:
                    session.rollback()

    @staticmethod
    def _load_json_value(raw: str | None, default: T) -> T:
        if not raw:
            return AppStateService._clone_json_value(default)
        try:
            parsed = json.loads(raw)
        except json.JSONDecodeError:
            return AppStateService._clone_json_value(default)
        return parsed if isinstance(parsed, type(default)) else AppStateService._clone_json_value(default)

    @staticmethod
    def _clone_json_value(value: T) -> T:
        return json.loads(json.dumps(value, ensure_ascii=False))
