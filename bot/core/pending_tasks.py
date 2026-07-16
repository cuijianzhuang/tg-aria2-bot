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

    @classmethod
    def from_row(cls, row) -> "PendingTask":
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
        )
