"""
Traitement des callbacks Telegram (boutons interactifs).

Appelé au début de chaque run main.py.
Récupère les updates en attente via getUpdates (polling court),
traite les clics boutons, répond à Telegram.

Boutons gérés :
  r1_{uid_key}  → noter ⭐ Très intéressant
  r2_{uid_key}  → noter 👍 Ok
  r3_{uid_key}  → noter 👎 Nul
  d_{uid_key}   → afficher la description
  bilan         → afficher le résumé des classements
"""
from __future__ import annotations

import json
import os
import requests
from typing import Optional, TYPE_CHECKING

if TYPE_CHECKING:
    from state import State

TELEGRAM_API = "https://api.telegram.org/bot{token}/{method}"
RATING_LABELS = {1: "⭐ Très intéressant", 2: "👍 Ok", 3: "👎 Nul"}


def _api(token: str, method: str, **kwargs) -> Optional[dict]:
    url = TELEGRAM_API.format(token=token, method=method)
    try:
        r = requests.post(url, json=kwargs, timeout=15)
        if r.status_code == 200:
            return r.json().get("result")
        print(f"[callback] {method} {r.status_code}: {r.text[:200]}")
    except requests.RequestException as e:
        print(f"[callback] {method} error: {e}")
    return None


def _answer_callback(token: str, callback_id: str, text: str = "", alert: bool = False):
    _api(token, "answerCallbackQuery",
         callback_query_id=callback_id, text=text, show_alert=alert)


def _edit_keyboard(token: str, chat_id: str, msg_id: int, keyboard: dict):
    _api(token, "editMessageReplyMarkup",
         chat_id=chat_id, message_id=msg_id,
         reply_markup=keyboard)


def _send_message(token: str, chat_id: str, text: str):
    _api(token, "sendMessage",
         chat_id=chat_id, text=text[:4096], parse_mode="HTML")


def _build_keyboard_after_rating(uid_key: str, rating: int) -> dict:
    from notifier import RATING_LABELS
    return {
        "inline_keyboard": [
            [{"text": f"✅ {RATING_LABELS[rating]}", "callback_data": f"r{rating}_{uid_key}"}],
            [
                {"text": "📋 Description", "callback_data": f"d_{uid_key}"},
                {"text": "📊 Mon bilan", "callback_data": "bilan"},
            ],
        ]
    }


def _find_uid_by_key(state: "State", key: str) -> Optional[str]:
    """Retrouve l'uid complet depuis le key tronqué (55 chars)."""
    for uid in state.msg_ids:
        if uid[:55] == key:
            return uid
    # Fallback : cherche dans descriptions
    for uid in state.descriptions:
        if uid[:55] == key:
            return uid
    return None


def _send_bilan(token: str, chat_id: str, state: "State"):
    """Envoie le résumé des 3 catégories."""
    lines = ["📊 <b>Ton bilan d'annonces</b>\n"]
    for rating, emoji_label in [(1, "⭐ Très intéressant"), (2, "👍 Ok"), (3, "👎 Nul")]:
        items = state.get_rated_listings(rating)
        lines.append(f"<b>{emoji_label}</b> ({len(items)})")
        for item in items[:10]:  # max 10 par catégorie
            price = f"{item['price']} €" if item.get("price") else "?"
            title = (item.get("title") or "")[:50]
            url = item.get("url", "")
            lines.append(f'  • <a href="{url}">{title}</a> — {price}')
        if not items:
            lines.append("  (aucune)")
        lines.append("")

    _send_message(token, chat_id, "\n".join(lines))


def process_callbacks(state: "State") -> None:
    """Récupère et traite tous les callbacks Telegram en attente."""
    token = os.getenv("TELEGRAM_BOT_TOKEN", "")
    chat_id = os.getenv("TELEGRAM_CHAT_ID", "")
    if not token or not chat_id:
        return

    # getUpdates avec offset = last_update_id + 1
    offset = state.last_update_id + 1 if state.last_update_id else None
    params = {"timeout": 1, "allowed_updates": ["callback_query", "message"]}
    if offset:
        params["offset"] = offset

    url = TELEGRAM_API.format(token=token, method="getUpdates")
    try:
        r = requests.get(url, params=params, timeout=10)
        if r.status_code != 200:
            print(f"[callback] getUpdates {r.status_code}")
            return
        updates = r.json().get("result", [])
    except requests.RequestException as e:
        print(f"[callback] getUpdates error: {e}")
        return

    if not updates:
        return

    print(f"[callback] {len(updates)} update(s) à traiter")

    for upd in updates:
        update_id = upd.get("update_id", 0)
        if update_id > state.last_update_id:
            state.last_update_id = update_id

        # --- Callback query (bouton cliqué) ---
        cq = upd.get("callback_query")
        if cq:
            cq_id = cq["id"]
            data = cq.get("data", "")
            msg = cq.get("message", {})
            cq_chat_id = str(msg.get("chat", {}).get("id", chat_id))
            cq_msg_id = msg.get("message_id")

            # Bouton rating : r1_key, r2_key, r3_key
            if data.startswith(("r1_", "r2_", "r3_")):
                rating = int(data[1])
                uid_key = data[3:]
                uid = _find_uid_by_key(state, uid_key)
                if uid:
                    state.rate_listing(uid, rating)
                    label = RATING_LABELS[rating]
                    _answer_callback(token, cq_id, f"Classé : {label}")
                    # Met à jour le clavier du message
                    new_keyboard = _build_keyboard_after_rating(uid_key, rating)
                    _edit_keyboard(token, cq_chat_id, cq_msg_id, new_keyboard)
                    print(f"[callback] rating {rating} → {uid[:40]}")
                else:
                    _answer_callback(token, cq_id, "Annonce introuvable dans l'historique.")

            # Bouton description : d_key
            elif data.startswith("d_"):
                uid_key = data[2:]
                uid = _find_uid_by_key(state, uid_key)
                desc = state.descriptions.get(uid) if uid else None
                if desc:
                    _answer_callback(token, cq_id)
                    _send_message(token, cq_chat_id,
                                  f"📋 <b>Description :</b>\n\n{desc[:3000]}")
                else:
                    _answer_callback(token, cq_id,
                                     "Pas de description disponible pour cette annonce.", alert=True)

            # Bouton bilan
            elif data == "bilan":
                _answer_callback(token, cq_id)
                _send_bilan(token, cq_chat_id, state)

            else:
                _answer_callback(token, cq_id)

        # --- Message texte (commandes manuelles) ---
        msg = upd.get("message")
        if msg:
            text = msg.get("text", "").strip().lower()
            msg_chat_id = str(msg.get("chat", {}).get("id", ""))
            if text in ("/bilan", "/resume", "/résumé") and msg_chat_id == chat_id:
                _send_bilan(token, chat_id, state)
