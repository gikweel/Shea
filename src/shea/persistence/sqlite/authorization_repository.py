from __future__ import annotations

import sqlite3
from datetime import datetime

from shea.contracts.models import Authorization

from .unit_of_work import SqliteUnitOfWork


class SqliteAuthorizationRepository:
    """Authorizations are insert-only and listed, never upserted — a task
    can legitimately accumulate more than one over its lifetime (e.g.
    re-authorization after recovery), and each is its own audit-relevant
    fact per Appendix B: "USER OVERRIDE = EXPLICIT + AUDITABLE".
    """

    def __init__(self, conn: sqlite3.Connection, *, unit_of_work: SqliteUnitOfWork) -> None:
        self._conn = conn
        self._uow = unit_of_work
        
    def _parse_optional_datetime(self, value: str | None) -> datetime | None:
        if value is None:
            return None
        return datetime.fromisoformat(value)

    def save(self, authorization: Authorization) -> None:
        with self._uow:
            self._conn.execute(
                """
                INSERT INTO authorizations
                    (id, task_id, profile_id, granted, granted_by, explicit,
                     plan_hash, step_hash, arguments_hash, expires_at, used_at, nonce)
                VALUES
                    (:id, :task_id, :profile_id, :granted, :granted_by, :explicit,
                     :plan_hash, :step_hash, :arguments_hash, :expires_at, :used_at, :nonce)
                ON CONFLICT(id) DO UPDATE SET
                    granted = :granted,
                    granted_by = :granted_by,
                    explicit = :explicit,
                    plan_hash = :plan_hash,
                    step_hash = :step_hash,
                    arguments_hash = :arguments_hash,
                    expires_at = :expires_at,
                    used_at = :used_at,
                    nonce = :nonce
                """,
                {
                    "id": authorization.id,
                    "task_id": authorization.task_id,
                    "profile_id": authorization.profile_id,
                    "granted": int(authorization.granted),
                    "granted_by": authorization.granted_by,
                    "explicit": int(authorization.explicit),
                    "plan_hash": authorization.plan_hash,
                    "step_hash": authorization.step_hash,
                    "arguments_hash": authorization.arguments_hash,
                    "expires_at": (authorization.expires_at.isoformat()
                    if authorization.expires_at
                    else None),
                    "used_at": (authorization.used_at.isoformat()
                    if authorization.used_at
                    else None),
                    "nonce": authorization.nonce,
                },
            )

    _COLUMNS = (
        "id, task_id, profile_id, granted, granted_by, explicit, "
        "plan_hash, step_hash, arguments_hash, expires_at, used_at, nonce"
    )

    def get(self, authorization_id: str) -> Authorization | None:
        row = self._conn.execute(
            f"SELECT {self._COLUMNS} FROM authorizations WHERE id = ?",
            (authorization_id,),
        ).fetchone()
        return self._row_to_authorization(row) if row is not None else None

    def list_by_task(self, task_id: str) -> list[Authorization]:
        rows = self._conn.execute(
            f"SELECT {self._COLUMNS} FROM authorizations WHERE task_id = ? ORDER BY rowid ASC",
            (task_id,),
        ).fetchall()
        return [self._row_to_authorization(row) for row in rows]

    def _row_to_authorization(self, row: sqlite3.Row) -> Authorization:
        return Authorization(
            id=row["id"],
            task_id=row["task_id"],
            profile_id=row["profile_id"],
            granted=bool(row["granted"]),
            granted_by=row["granted_by"],
            explicit=bool(row["explicit"]),
            plan_hash=row["plan_hash"],
            step_hash=row["step_hash"],
            arguments_hash=row["arguments_hash"],
            expires_at=self._parse_optional_datetime(row["expires_at"]),
            used_at=self._parse_optional_datetime(row["used_at"]),
            nonce=row["nonce"],
        )