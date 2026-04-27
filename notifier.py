"""
Notifier Telegram — envoie une annonce scorée avec photo + boutons interactifs.

Boutons :
  ⭐ Très intéressant | 👍 Ok | 👎 Nul
  📋 Description (envoie la description en message séparé)
  📊 Mon bilan (résumé des classements)
"""
from __future__ import annotations

import os
import time
from typing import Optional, TYPE_CHECKING

import requests

from models import Listing

if TYPE_CHECKING:
    from state import State


TELEGRAM_API = "https://api.telegram.org/bot{token}/{method}"

RATING_LABELS = {1: "⭐ Très intéressant", 2: "👍 Ok", 3: "👎 Nul"}


def _build_keyboard(uid: str, current_rating: Optional[int] = None) -> dict:
    """Construit l'InlineKeyboardMarkup pour une annonce."""
    key = uid[:55]  # max 64 bytes total pour le callback_data

    if current_rating:
        # Après un vote : affiche seulement le choix sélectionné + description
        rating_row = [{"text": f"✅ {RATING_LABELS[current_rating]}", "callback_data": f"r{current_rating}_{key}"}]
    else:
        rating_row = [
            {"text": "⭐", "callback_data": f"r1_{key}"},
            {"text": "👍", "callback_data": f"r2_{key}"},
            {"text": "👎", "callback_data": f"r3_{key}"},
        ]

    desc_row = [
        {"text": "📋 Description", "callback_data": f"d_{key}"},
        {"text": "📊 Mon bilan", "callback_data": "bilan"},
    ]

    return {"inline_keyboard": [rating_row, desc_row]}


class TelegramNotifier:
    def __init__(self, token: Optional[str] = None, chat_id: Optional[str] = None):
        self.token = token or os.getenv("TELEGRAM_BOT_TOKEN", "")
        self.chat_id = chat_id or os.getenv("TELEGRAM_CHAT_ID", "")
        self.enabled = bool(self.token and self.chat_id)
        if not self.enabled:
            print("[notifier] TELEGRAM_BOT_TOKEN ou TELEGRAM_CHAT_ID manquant.")

    def send(self, listing: Listing, state: "State") -> bool:
        """Envoie l'annonce et stocke les métadonnées dans state."""
        if not self.enabled:
            print(f"[notifier] (off) {listing.title} — {listing.url}")
            return False

        caption = self._format(listing)
        keyboard = _build_keyboard(listing.uid)
        is_photo = bool(listing.image_url)

        msg_id = None
        if is_photo:
            msg_id = self._send_photo(listing.image_url, caption, keyboard)
        if msg_id is None:
            is_photo = False
            msg_id = self._send_message(caption, keyboard)

        if msg_id:
            state.store_listing_meta(
                uid=listing.uid,
                description=listing.description,
                msg_id=msg_id,
                chat_id=self.chat_id,
                is_photo=is_photo,
                title=listing.title,
                price=listing.price,
                url=listing.url,
            )
            return True
        return False

    def _format(self, l: Listing) -> str:
        price = f"{l.price:,} €".replace(",", " ") if l.price else "—"
        year = str(l.year) if l.year else "—"
        km = f"{l.mileage:,} km".replace(",", " ") if l.mileage else "—"
        loc = l.location or "—"
        site = l.site.upper()
        score = f"{l.score:+.1f}" if l.score else "0.0"

        price_note = ""
        if l.price and l.score_breakdown:
            for line in l.score_breakdown:
                if "vs cote" in line:
                    price_note = f"\n<i>{self._esc(line)}</i>"
                    break

        lines = [
            f"🚗 <b>{self._esc(l.title)}</b>",
            f"💰 <b>{price}</b>{price_note}",
            f"📍 {self._esc(loc)}",
            f"🛣 {km}",
            f"📅 {year}",
            f"🏷 {site}  ⭐ score {score}",
            f'🔗 <a href="{l.url}">Voir l\'annonce</a>',
        ]
        return "\n".join(lines)

    @staticmethod
    def _esc(s: str) -> str:
        return (s or "").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")

    def _send_photo(self, photo: str, caption: str, keyboard: dict) -> Optional[int]:
        """Retourne message_id ou None."""
        url = TELEGRAM_API.format(token=self.token, method="sendPhoto")
        payload = {
            "chat_id": self.chat_id,
            "photo": photo,
            "caption": caption[:1024],
            "parse_mode": "HTML",
            "reply_markup": __import__("json").dumps(keyboard),
        }
        try:
            r = requests.post(url, data=payload, timeout=15)
            if r.status_code == 200:
                return r.json()["result"]["message_id"]
            print(f"[notifier] sendPhoto {r.status_code}: {r.text[:200]}")
        except requests.RequestException as e:
            print(f"[notifier] sendPhoto error: {e}")
        return None

    def _send_message(self, text: str, keyboard: dict) -> Optional[int]:
        """Retourne message_id ou None."""
        import json as _json
        url = TELEGRAM_API.format(token=self.token, method="sendMessage")
        payload = {
            "chat_id": self.chat_id,
            "text": text[:4096],
            "parse_mode": "HTML",
            "reply_markup": _json.dumps(keyboard),
        }
        try:
            r = requests.post(url, data=payload, timeout=15)
            if r.status_code == 200:
                return r.json()["result"]["message_id"]
            print(f"[notifier] sendMessage {r.status_code}: {r.text[:200]}")
        except requests.RequestException as e:
            print(f"[notifier] sendMessage error: {e}")
        return None

    def throttle(self, s: float = 1.0) -> None:
        time.sleep(s)
