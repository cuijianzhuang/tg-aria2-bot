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
    node: str = "default"

    @classmethod
    def from_row(cls, row) -> PendingTask:
        # batch_id/node 是后加的迁移字段，旧库经 ALTER TABLE 补列后行里一定带
        # 这些 key，防御性判断只为覆盖测试里手工构造的精简行
        keys = row.keys()
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
            batch_id=row["batch_id"] if "batch_id" in keys else None,
            node=row["node"] if "node" in keys else "default",
        )
