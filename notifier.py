"""
Notifier Telegram — envoie une annonce scorée avec photo.
"""
from __future__ import annotations

import os
import time
from typing import Optional

import requests

from models import Listing


TELEGRAM_API = "https://api.telegram.org/bot{token}/{method}"


class TelegramNotifier:
    def __init__(self, token: Optional[str] = None, chat_id: Optional[str] = None):
        self.token = token or os.getenv("TELEGRAM_BOT_TOKEN", "")
        self.chat_id = chat_id or os.getenv("TELEGRAM_CHAT_ID", "")
        self.enabled = bool(self.token and self.chat_id)
        if not self.enabled:
            print("[notifier] TELEGRAM_BOT_TOKEN ou TELEGRAM_CHAT_ID manquant.")

    def send(self, listing: Listing) -> bool:
        if not self.enabled:
            print(f"[notifier] (off) {listing.title} — {listing.url}")
            return False

        caption = self._format(listing)

        if listing.image_url:
            if self._send_photo(listing.image_url, caption):
                return True
        return self._send_message(caption)

    def _format(self, l: Listing) -> str:
        price = f"{l.price:,} €".replace(",", " ") if l.price else "—"
        year = str(l.year) if l.year else "—"
        km = f"{l.mileage:,} km".replace(",", " ") if l.mileage else "—"
        loc = l.location or "—"
        site = l.site.upper()
        search = l.matched_search or ""
        score = f"{l.score:+.1f}" if l.score else "0.0"

        # Calcul écart prix vs cote
        price_note = ""
        if l.price and l.score_breakdown:
            # Extraire la ligne prix du breakdown
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

    def _send_photo(self, photo: str, caption: str) -> bool:
        url = TELEGRAM_API.format(token=self.token, method="sendPhoto")
        payload = {"chat_id": self.chat_id, "photo": photo, "caption": caption[:1024], "parse_mode": "HTML"}
        try:
            r = requests.post(url, data=payload, timeout=15)
            if r.status_code == 200:
                return True
            print(f"[notifier] sendPhoto {r.status_code}: {r.text[:200]}")
        except requests.RequestException as e:
            print(f"[notifier] sendPhoto error: {e}")
        return False

    def _send_message(self, text: str) -> bool:
        url = TELEGRAM_API.format(token=self.token, method="sendMessage")
        payload = {"chat_id": self.chat_id, "text": text[:4096], "parse_mode": "HTML"}
        try:
            r = requests.post(url, data=payload, timeout=15)
            if r.status_code == 200:
                return True
            print(f"[notifier] sendMessage {r.status_code}: {r.text[:200]}")
        except requests.RequestException as e:
            print(f"[notifier] sendMessage error: {e}")
        return False

    def throttle(self, s: float = 1.0) -> None:
        time.sleep(s)
