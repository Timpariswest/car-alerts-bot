"""
Client IMAP Gmail — lit les emails d'alerte de Leboncoin / LaCentrale / AutoScout24
depuis la(les) boîte(s) de l'utilisateur.

Supporte jusqu'à 3 comptes Gmail en parallèle :
  - GMAIL_USER / GMAIL_APP_PASSWORD (obligatoire)
  - GMAIL_USER_2 / GMAIL_APP_PASSWORD_2 (optionnel)
  - GMAIL_USER_3 / GMAIL_APP_PASSWORD_3 (optionnel)

Nécessite pour chaque compte un "mot de passe d'application" Google (voir README).
"""
from __future__ import annotations

import email
import email.utils
import imaplib
import os
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from email.message import Message
from typing import List, Optional


IMAP_HOST = "imap.gmail.com"
IMAP_PORT = 993


SENDER_PATTERNS = {
    "leboncoin": [
        "leboncoin.fr",
        "@mail.leboncoin.fr",
        "noreply@leboncoin.fr",
        "no-reply@leboncoin.fr",
    ],
    "lacentrale": [
        "lacentrale.fr",
        "@mail.lacentrale.fr",
        "noreply@lacentrale.fr",
    ],
    "autoscout24": [
        "autoscout24.com",
        "autoscout24.fr",
        "noreply@autoscout24.com",
    ],
}


@dataclass
class GmailAccount:
    user: str
    app_password: str


@dataclass
class RawEmail:
    msg_id: str
    sender: str
    subject: str
    date: datetime
    site: str
    html_body: str
    text_body: str
    account: str  # adresse Gmail source


def detect_site(sender: str) -> Optional[str]:
    s = (sender or "").lower()
    for site, patterns in SENDER_PATTERNS.items():
        if any(p in s for p in patterns):
            return site
    return None


def _decode_header(value: Optional[str]) -> str:
    if not value:
        return ""
    try:
        parts = email.header.decode_header(value)
        out = []
        for chunk, enc in parts:
            if isinstance(chunk, bytes):
                out.append(chunk.decode(enc or "utf-8", errors="replace"))
            else:
                out.append(chunk)
        return "".join(out)
    except Exception:
        return value


def _get_body(msg: Message) -> tuple[str, str]:
    html_body = ""
    text_body = ""

    if msg.is_multipart():
        for part in msg.walk():
            ctype = part.get_content_type()
            disp = str(part.get("Content-Disposition", ""))
            if "attachment" in disp.lower():
                continue
            try:
                payload = part.get_payload(decode=True)
                if payload is None:
                    continue
                charset = part.get_content_charset() or "utf-8"
                decoded = payload.decode(charset, errors="replace")
            except Exception:
                continue
            if ctype == "text/html" and not html_body:
                html_body = decoded
            elif ctype == "text/plain" and not text_body:
                text_body = decoded
    else:
        try:
            payload = msg.get_payload(decode=True)
            if payload is not None:
                charset = msg.get_content_charset() or "utf-8"
                decoded = payload.decode(charset, errors="replace")
                if msg.get_content_type() == "text/html":
                    html_body = decoded
                else:
                    text_body = decoded
        except Exception:
            pass

    return html_body, text_body


def load_accounts_from_env() -> List[GmailAccount]:
    accounts: List[GmailAccount] = []
    for suffix in ("", "_2", "_3"):
        u = os.getenv(f"GMAIL_USER{suffix}", "").strip()
        p = os.getenv(f"GMAIL_APP_PASSWORD{suffix}", "").strip()
        if u and p:
            accounts.append(GmailAccount(user=u, app_password=p))
    if not accounts:
        raise RuntimeError(
            "Aucun compte Gmail configuré. Définis GMAIL_USER + GMAIL_APP_PASSWORD "
            "(et éventuellement _2, _3). Voir README."
        )
    return accounts


def fetch_all_accounts(
    accounts: List[GmailAccount],
    mailbox: str = "INBOX",
    max_age_days: int = 3,
    label_filter: Optional[str] = None,
) -> List[RawEmail]:
    """Fetch les emails d'alerte sur tous les comptes Gmail fournis."""
    all_emails: List[RawEmail] = []
    for acc in accounts:
        try:
            emails = _fetch_account(acc, mailbox, max_age_days, label_filter)
            all_emails.extend(emails)
            print(f"[imap] {acc.user}: {len(emails)} emails d'alerte")
        except Exception as e:
            print(f"[imap] ERREUR sur {acc.user}: {e}")
    return all_emails


def _fetch_account(
    account: GmailAccount,
    mailbox: str,
    max_age_days: int,
    label_filter: Optional[str],
) -> List[RawEmail]:
    conn = imaplib.IMAP4_SSL(IMAP_HOST, IMAP_PORT)
    try:
        conn.login(account.user, account.app_password)

        if label_filter:
            status, _ = conn.select("[Gmail]/Tous les messages", readonly=True)
            if status != "OK":
                conn.select("[Gmail]/All Mail", readonly=True)
        else:
            conn.select(mailbox, readonly=True)

        since_date = (datetime.now() - timedelta(days=max_age_days)).strftime("%d-%b-%Y")

        if label_filter:
            gmail_query = f"label:{label_filter} newer_than:{max_age_days}d"
            typ, data = conn.search(None, "X-GM-RAW", f'"{gmail_query}"')
        else:
            sender_clauses: List[str] = []
            for patterns in SENDER_PATTERNS.values():
                for p in patterns:
                    sender_clauses.append(f'FROM "{p}"')

            def nest_or(clauses: List[str]) -> str:
                if len(clauses) == 1:
                    return clauses[0]
                if len(clauses) == 2:
                    return f"OR {clauses[0]} {clauses[1]}"
                return f"OR {clauses[0]} ({nest_or(clauses[1:])})"

            or_query = nest_or(sender_clauses)
            query = f"SINCE {since_date} ({or_query})"
            typ, data = conn.search(None, query)

        if typ != "OK" or not data or not data[0]:
            return []

        ids = data[0].split()
        out: List[RawEmail] = []
        for msg_id in ids:
            try:
                typ, msg_data = conn.fetch(msg_id, "(RFC822)")
                if typ != "OK" or not msg_data:
                    continue
                raw = msg_data[0][1]
                if not isinstance(raw, (bytes, bytearray)):
                    continue
                msg = email.message_from_bytes(raw)

                sender = _decode_header(msg.get("From"))
                site = detect_site(sender)
                if not site:
                    continue

                subject = _decode_header(msg.get("Subject"))
                date_str = msg.get("Date", "")
                try:
                    parsed = email.utils.parsedate_to_datetime(date_str)
                    if parsed.tzinfo is None:
                        parsed = parsed.replace(tzinfo=timezone.utc)
                    date = parsed
                except Exception:
                    date = datetime.now(timezone.utc)

                html_body, text_body = _get_body(msg)
                out.append(RawEmail(
                    msg_id=msg_id.decode() if isinstance(msg_id, bytes) else str(msg_id),
                    sender=sender,
                    subject=subject,
                    date=date,
                    site=site,
                    html_body=html_body,
                    text_body=text_body,
                    account=account.user,
                ))
            except Exception as e:
                print(f"[imap:{account.user}] skip {msg_id!r}: {e}")
                continue
        return out
    finally:
        try:
            conn.logout()
        except Exception:
            pass
