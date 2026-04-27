"""
State persistant — mémorise les listing_id déjà notifiés, les ratings,
les descriptions et les message_id Telegram.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Dict, Optional, Set


class State:
    def __init__(self, path: str = "state.json"):
        self.path = Path(path)
        self.seen: Set[str] = set()
        # uid → {"rating": 1|2|3, "title": str, "price": int|None, "url": str}
        self.ratings: Dict[str, dict] = {}
        # uid → description text
        self.descriptions: Dict[str, str] = {}
        # uid → {"msg_id": int, "chat_id": str, "is_photo": bool}
        self.msg_ids: Dict[str, dict] = {}
        # offset pour getUpdates Telegram
        self.last_update_id: int = 0
        self._load()

    def _load(self) -> None:
        if self.path.exists():
            try:
                with self.path.open("r", encoding="utf-8") as f:
                    data = json.load(f)
                self.seen = set(data.get("seen", []))
                self.ratings = data.get("ratings", {})
                self.descriptions = data.get("descriptions", {})
                self.msg_ids = data.get("msg_ids", {})
                self.last_update_id = int(data.get("last_update_id", 0))
            except (json.JSONDecodeError, OSError) as e:
                print(f"[state] Impossible de charger {self.path}: {e}. Repart vide.")
                self.seen = set()

    def save(self) -> None:
        MAX = 50_000
        # Garde uniquement les 2000 derniers msg_ids et descriptions
        msg_ids = dict(list(self.msg_ids.items())[-2000:])
        descriptions = dict(list(self.descriptions.items())[-2000:])
        data = {
            "seen": sorted(self.seen)[-MAX:],
            "ratings": self.ratings,
            "descriptions": descriptions,
            "msg_ids": msg_ids,
            "last_update_id": self.last_update_id,
        }
        tmp = self.path.with_suffix(".tmp")
        with tmp.open("w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False)
        os.replace(tmp, self.path)

    def is_seen(self, uid: str) -> bool:
        return uid in self.seen

    def mark_seen(self, uid: str) -> None:
        self.seen.add(uid)

    def store_listing_meta(self, uid: str, description: Optional[str],
                           msg_id: int, chat_id: str, is_photo: bool,
                           title: str, price: Optional[int], url: str) -> None:
        if description:
            self.descriptions[uid] = description
        self.msg_ids[uid] = {"msg_id": msg_id, "chat_id": chat_id, "is_photo": is_photo,
                             "title": title, "price": price, "url": url}

    def rate_listing(self, uid: str, rating: int) -> None:
        meta = self.msg_ids.get(uid, {})
        self.ratings[uid] = {
            "rating": rating,
            "title": meta.get("title", ""),
            "price": meta.get("price"),
            "url": meta.get("url", ""),
        }

    def get_rated_listings(self, rating: int) -> list:
        return [v for v in self.ratings.values() if v.get("rating") == rating]
