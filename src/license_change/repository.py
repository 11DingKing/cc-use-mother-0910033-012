"""内存仓储：单进程内的聚合存储，支持快照用于原子回滚。"""
from __future__ import annotations

import copy
import threading
from typing import Any


class InMemoryStore:
    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._data: dict[str, dict[str, Any]] = {
            "institutions": {},
            "versions": {},          # institution_id -> list[dict]
            "current_version": {},   # institution_id -> version_no
            "projects": {},
            "bookings": {},
            "requests": {},
            "chains": {},            # institution_id -> list[dict]
        }

    @property
    def lock(self) -> threading.RLock:
        return self._lock

    def snapshot(self) -> dict[str, Any]:
        return copy.deepcopy(self._data)

    def restore(self, snap: dict[str, Any]) -> None:
        self._data = copy.deepcopy(snap)

    def table(self, name: str) -> dict[str, Any]:
        return self._data[name]
