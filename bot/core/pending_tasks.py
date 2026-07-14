from __future__ import annotations

from dataclasses import dataclass
from time import time
from uuid import uuid4


@dataclass
class PendingTask:
    kind: str
    user_id: int
    chat_id: int
    source_ref: str | None
    file_name: str | None
    file_size: int | None
    payload: str
    created_at: float


_TASKS: dict[str, PendingTask] = {}
TTL_SECONDS = 30 * 60


def create_pending(
    *,
    kind: str,
    user_id: int,
    chat_id: int,
    source_ref: str | None,
    file_name: str | None,
    file_size: int | None,
    payload: str,
) -> str:
    cleanup_expired()
    token = uuid4().hex[:12]
    _TASKS[token] = PendingTask(
        kind=kind,
        user_id=user_id,
        chat_id=chat_id,
        source_ref=source_ref,
        file_name=file_name,
        file_size=file_size,
        payload=payload,
        created_at=time(),
    )
    return token


def pop_pending(token: str) -> PendingTask | None:
    cleanup_expired()
    return _TASKS.pop(token, None)


def get_pending(token: str) -> PendingTask | None:
    cleanup_expired()
    return _TASKS.get(token)


def delete_pending(token: str):
    _TASKS.pop(token, None)


def cleanup_expired():
    now = time()
    expired = [token for token, task in _TASKS.items() if now - task.created_at > TTL_SECONDS]
    for token in expired:
        _TASKS.pop(token, None)
