"""Record-and-replay store: (task, step, screen fingerprint) → the action that worked last time."""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
import time
from dataclasses import asdict, dataclass, field
from typing import Any


@dataclass
class CachedStep:
    """One recorded step. ``next_fp`` is the screen the action is expected to lead to."""

    index: int
    fp: str
    action: dict[str, Any]
    answer: str
    selector: dict | None = None
    next_fp: str | None = None
    package: str = ""
    version: str = ""
    hits: int = 0
    misses: int = 0
    recorded_at: float = field(default_factory=time.time)


def task_key(task: str) -> str:
    return hashlib.sha1(" ".join(task.split()).encode("utf-8")).hexdigest()[:16]


def profile_key(profile: dict) -> str:
    return hashlib.sha1(json.dumps(profile, sort_keys=True).encode("utf-8")).hexdigest()[:12]


class ActionCache:
    """
    JSON-file cache. Steps are grouped per device profile (size, density, rotation), so a
    different screen never reuses coordinates, and per task. Writes are atomic.
    """

    def __init__(self, path: str, profile: dict):
        self.path = path
        self.profile = profile
        self._profile = profile_key(profile)
        self._data: dict[str, Any] = {"version": 1, "profiles": {}}
        if os.path.exists(path):
            with open(path, encoding="utf-8") as f:
                loaded = json.load(f)
            if loaded.get("version") == 1:
                self._data = loaded

    def _steps(self, task: str) -> dict[str, Any]:
        profiles = self._data["profiles"].setdefault(self._profile, {"device": self.profile, "tasks": {}})
        return profiles["tasks"].setdefault(task_key(task), {"task": task, "steps": {}})["steps"]

    def lookup(self, task: str, index: int, fp: str, version: str | None = None) -> CachedStep | None:
        raw = self._steps(task).get(str(index))
        if raw is None:
            return None
        step = CachedStep(**raw)
        if step.fp != fp:
            return None
        if version is not None and step.version and step.version != version:
            # App was updated: every recorded step of this task is suspect.
            self.invalidate(task, 0)
            return None
        return step

    def record(self, task: str, step: CachedStep) -> None:
        self._steps(task)[str(step.index)] = asdict(step)

    def mark(self, task: str, index: int, hit: bool) -> None:
        raw = self._steps(task).get(str(index))
        if raw is not None:
            raw["hits" if hit else "misses"] += 1

    def invalidate(self, task: str, from_index: int) -> None:
        """Forget ``from_index`` and everything after it: the path diverged there."""
        steps = self._steps(task)
        for key in [k for k in steps if int(k) >= from_index]:
            del steps[key]

    def save(self) -> None:
        directory = os.path.dirname(os.path.abspath(self.path))
        os.makedirs(directory, exist_ok=True)
        fd, temporary = tempfile.mkstemp(dir=directory, suffix=".tmp")
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(self._data, f, ensure_ascii=False, indent=1)
        os.replace(temporary, self.path)
