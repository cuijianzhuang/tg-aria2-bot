from __future__ import annotations

from dataclasses import dataclass

TTL_SECONDS = 30 * 60


@dataclass
class PendingTask:
    token: str
    kind: str
    user_id: int
    chat_id: int
    source_ref: str | None
    file_name: str | None
    file_size: int | None
    payload: str
    created_at: float
    batch_id: str | None = None

    @classmethod
    def from_row(cls, row) -> PendingTask:
        # batch_id 列是后加的迁移字段，旧库里的行经 sqlite3.Row 仍然带这个 key
        # （ALTER TABLE 已经把它加到表结构上了），直接取值即可，不存在需要兼容的场景
        return cls(
            token=row["token"],
            kind=row["kind"],
            user_id=row["user_id"],
            chat_id=row["chat_id"],
            source_ref=row["source_ref"],
            file_name=row["file_name"],
            file_size=row["file_size"],
            payload=row["payload"],
            created_at=row["created_at"],
            batch_id=row["batch_id"] if "batch_id" in row.keys() else None,
        )
