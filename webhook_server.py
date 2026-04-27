"""
Webhook Telegram — répond instantanément aux boutons interactifs.

Boutons gérés :
  r1/r2/r3  → noter l'annonce (⭐ / 👍 / 👎)
  d_        → afficher la description
  bilan     → résumé des classements

State stocké directement dans state.json du repo GitHub via l'API GitHub.
"""
from __future__ import annotations

import base64
import json
import os
import time

import requests
from flask import Flask, request, jsonify

app = Flask(__name__)

TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
CHAT_ID = os.environ["TELEGRAM_CHAT_ID"]
GITHUB_TOKEN = os.environ["GITHUB_TOKEN"]
GITHUB_REPO = os.environ.get("GITHUB_REPO", "Timpariswest/car-alerts-bot")
WEBHOOK_SECRET = os.environ.get("WEBHOOK_SECRET", "")

TELEGRAM_API = f"https://api.telegram.org/bot{TOKEN}"
GITHUB_API = f"https://api.github.com/repos/{GITHUB_REPO}/contents/state.json"

RATING_LABELS = {1: "⭐ Très intéressant", 2: "👍 Ok", 3: "👎 Nul"}


# ─── Telegram helpers ────────────────────────────────────────────────────────

def tg(method: str, **kwargs) -> dict:
    r = requests.post(f"{TELEGRAM_API}/{method}", json=kwargs, timeout=10)
    return r.json()


def answer_callback(cq_id: str, text: str = "", alert: bool = False):
    tg("answerCallbackQuery", callback_query_id=cq_id, text=text, show_alert=alert)


def edit_keyboard(chat_id: str, msg_id: int, keyboard: dict):
    tg("editMessageReplyMarkup", chat_id=chat_id, message_id=msg_id, reply_markup=keyboard)


def send_message(chat_id: str, text: str):
    tg("sendMessage", chat_id=chat_id, text=text[:4096], parse_mode="HTML")


# ─── GitHub state helpers ────────────────────────────────────────────────────

def get_github_state() -> tuple[dict, str | None]:
    """Lit state.json depuis le repo GitHub. Retourne (data, sha)."""
    r = requests.get(
        GITHUB_API,
        headers={"Authorization": f"Bearer {GITHUB_TOKEN}",
                 "Accept": "application/vnd.github+json"},
        timeout=15,
    )
    if r.status_code == 200:
        blob = r.json()
        content = base64.b64decode(blob["content"]).decode("utf-8")
        return json.loads(content), blob["sha"]
    print(f"[webhook] get_github_state {r.status_code}: {r.text[:200]}")
    return {}, None


def update_github_state(state_data: dict, sha: str) -> bool:
    """Écrit state.json dans le repo GitHub (atomic via SHA)."""
    content = base64.b64encode(
        json.dumps(state_data, ensure_ascii=False, indent=None).encode("utf-8")
    ).decode()
    r = requests.put(
        GITHUB_API,
        json={
            "message": "chore: update ratings [skip ci]",
            "content": content,
            "sha": sha,
        },
        headers={"Authorization": f"Bearer {GITHUB_TOKEN}",
                 "Accept": "application/vnd.github+json"},
        timeout=15,
    )
    ok = r.status_code in (200, 201)
    if not ok:
        print(f"[webhook] update_github_state {r.status_code}: {r.text[:200]}")
    return ok


# ─── Keyboard helpers ────────────────────────────────────────────────────────

def build_keyboard_after_rating(uid_key: str, rating: int) -> dict:
    return {
        "inline_keyboard": [
            [{"text": f"✅ {RATING_LABELS[rating]}", "callback_data": f"r{rating}_{uid_key}"}],
            [
                {"text": "📋 Description", "callback_data": f"d_{uid_key}"},
                {"text": "📊 Mon bilan", "callback_data": "bilan"},
            ],
        ]
    }


def find_uid_by_key(mapping: dict, key: str) -> str | None:
    for uid in mapping:
        if uid[:55] == key:
            return uid
    return None


# ─── Bilan ───────────────────────────────────────────────────────────────────

def send_bilan(chat_id: str, state_data: dict):
    ratings = state_data.get("ratings", {})
    lines = ["📊 <b>Ton bilan d'annonces</b>\n"]
    for rating, label in [(1, "⭐ Très intéressant"), (2, "👍 Ok"), (3, "👎 Nul")]:
        items = [v for v in ratings.values() if v.get("rating") == rating]
        lines.append(f"<b>{label}</b> ({len(items)})")
        for item in items[:10]:
            price = f"{item['price']} €" if item.get("price") else "?"
            title = (item.get("title") or "")[:50]
            url = item.get("url", "")
            lines.append(f'  • <a href="{url}">{title}</a> — {price}')
        if not items:
            lines.append("  (aucune)")
        lines.append("")
    send_message(chat_id, "\n".join(lines))


# ─── Webhook endpoint ────────────────────────────────────────────────────────

@app.route("/webhook", methods=["POST"])
def webhook():
    update = request.json or {}

    # ── Callback query (bouton cliqué) ──
    cq = update.get("callback_query")
    if cq:
        cq_id = cq["id"]
        data = cq.get("data", "")
        msg = cq.get("message", {})
        cq_chat_id = str(msg.get("chat", {}).get("id", CHAT_ID))
        cq_msg_id = msg.get("message_id")

        # Rating : r1_key / r2_key / r3_key
        if data.startswith(("r1_", "r2_", "r3_")):
            rating = int(data[1])
            uid_key = data[3:]

            # Répond immédiatement → arrête le chargement
            answer_callback(cq_id, f"Classé : {RATING_LABELS[rating]}")
            # Met à jour le clavier tout de suite
            edit_keyboard(cq_chat_id, cq_msg_id, build_keyboard_after_rating(uid_key, rating))

            # Stocke le rating dans le repo GitHub
            state_data, sha = get_github_state()
            if sha:
                uid = find_uid_by_key(state_data.get("msg_ids", {}), uid_key)
                if uid:
                    meta = state_data.get("msg_ids", {}).get(uid, {})
                    state_data.setdefault("ratings", {})[uid] = {
                        "rating": rating,
                        "title": meta.get("title", ""),
                        "price": meta.get("price"),
                        "url": meta.get("url", ""),
                    }
                    update_github_state(state_data, sha)

        # Description : d_key
        elif data.startswith("d_"):
            uid_key = data[2:]
            state_data, _ = get_github_state()
            uid = find_uid_by_key(state_data.get("msg_ids", {}), uid_key)
            if uid is None:
                uid = find_uid_by_key(state_data.get("descriptions", {}), uid_key)
            desc = state_data.get("descriptions", {}).get(uid) if uid else None
            if desc:
                answer_callback(cq_id)
                send_message(cq_chat_id, f"📋 <b>Description :</b>\n\n{desc[:3000]}")
            else:
                answer_callback(cq_id, "Pas de description disponible.", alert=True)

        # Bilan
        elif data == "bilan":
            answer_callback(cq_id)
            state_data, _ = get_github_state()
            send_bilan(cq_chat_id, state_data)

        else:
            answer_callback(cq_id)

    # ── Message texte ──
    msg = update.get("message")
    if msg:
        text = msg.get("text", "").strip().lower()
        msg_chat_id = str(msg.get("chat", {}).get("id", ""))
        if text in ("/bilan", "/resume", "/résumé") and msg_chat_id == CHAT_ID:
            state_data, _ = get_github_state()
            send_bilan(CHAT_ID, state_data)

    return jsonify({"ok": True})


@app.route("/", methods=["GET"])
def health():
    return "OK", 200


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)
