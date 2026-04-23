"""
State persistant — mémorise les listing_id déjà notifiés.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Set


class State:
    def __init__(self, path: str = "state.json"):
        self.path = Path(path)
        self.seen: Set[str] = set()
        self._load()

    def _load(self) -> None:
        if self.path.exists():
            try:
                with self.path.open("r", encoding="utf-8") as f:
                    data = json.load(f)
                self.seen = set(data.get("seen", []))
            except (json.JSONDecodeError, OSError) as e:
                print(f"[state] Impossible de charger {self.path}: {e}. Repart vide.")
                self.seen = set()

    def save(self) -> None:
        MAX = 50_000
        data = {"seen": sorted(self.seen)[-MAX:]}
        tmp = self.path.with_suffix(".tmp")
        with tmp.open("w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False)
        os.replace(tmp, self.path)

    def is_seen(self, uid: str) -> bool:
        return uid in self.seen

    def mark_seen(self, uid: str) -> None:
        self.seen.add(uid)
