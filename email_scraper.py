"""
Email scraper — lit les alertes mails LBC/LaCentrale/AS24 depuis Gmail via IMAP.

Flux :
  1. Connexion IMAP à Gmail (imap.gmail.com:993)
  2. Cherche les emails UNSEEN venant des expéditeurs connus
  3. Parse le HTML pour extraire les annonces
  4. Marque les emails comme lus (pour ne pas les retraiter)
  5. Retourne une liste de Listing

Comptes Gmail :
  - GMAIL_LBC_USER / GMAIL_LBC_PASS  → timdelmas123@gmail.com (LeBonCoin)
  - GMAIL_MAIN_USER / GMAIL_MAIN_PASS → chabodt@gmail.com (LaCentrale + AS24)
"""
from __future__ import annotations

import email as _email_lib
import imaplib
import os
import re
from typing import List, Optional

from bs4 import BeautifulSoup

from models import Listing
from parsers.common import clean_text

# ---------------------------------------------------------------------------
# Expéditeurs connus (on cherche ces patterns dans le champ FROM)
# ---------------------------------------------------------------------------
SENDERS = {
    "leboncoin":  ["alertes@leboncoin.fr", "noreply@leboncoin.fr", "leboncoin.fr"],
    "lacentrale": ["alerte@lacentrale.fr",  "noreply@lacentrale.fr", "lacentrale.fr"],
    "autoscout24": ["noreply@autoscout24.fr", "autoscout24.fr", "autoscout24.com"],
}


# ---------------------------------------------------------------------------
# Helpers parsing
# ---------------------------------------------------------------------------
def _parse_price(val) -> Optional[int]:
    if not val:
        return None
    digits = re.sub(r"[^\d]", "", str(val))
    v = int(digits) if digits else None
    return v if v and 100 < v < 100_000 else None


def _parse_km(val) -> Optional[int]:
    if not val:
        return None
    m = re.search(r"(\d[\d\s]{2,6})\s*km", str(val), re.IGNORECASE)
    if m:
        digits = re.sub(r"[^\d]", "", m.group(1))
        v = int(digits) if digits else None
        return v if v and 0 < v < 1_000_000 else None
    digits = re.sub(r"[^\d]", "", str(val))
    v = int(digits) if digits else None
    return v if v and 0 < v < 1_000_000 else None


def _parse_year(val) -> Optional[int]:
    m = re.search(r"(19[89]\d|20[0-3]\d)", str(val))
    return int(m.group(1)) if m else None


def _get_html_body(msg) -> Optional[str]:
    """Extrait le corps HTML d'un email multipart."""
    if msg.is_multipart():
        for part in msg.walk():
            if part.get_content_type() == "text/html":
                charset = part.get_content_charset() or "utf-8"
                try:
                    return part.get_payload(decode=True).decode(charset, errors="replace")
                except Exception:
                    pass
    else:
        if msg.get_content_type() == "text/html":
            charset = msg.get_content_charset() or "utf-8"
            try:
                return msg.get_payload(decode=True).decode(charset, errors="replace")
            except Exception:
                pass
    return None


# ---------------------------------------------------------------------------
# IMAP Gmail
# ---------------------------------------------------------------------------
def _fetch_gmail_htmls(username: str, password: str, sender_patterns: list[str], label: str = "") -> list[str]:
    """
    Connexion IMAP Gmail, retourne les corps HTML des emails non lus
    venant des expéditeurs correspondants, et les marque comme lus.
    """
    html_bodies = []
    try:
        mail = imaplib.IMAP4_SSL("imap.gmail.com", 993)
        mail.login(username, password)
        mail.select("INBOX")

        for pattern in sender_patterns:
            status, data = mail.search(None, f'(UNSEEN FROM "{pattern}")')
            if status != "OK":
                continue
            msg_ids = data[0].split()
            if not msg_ids:
                continue
            print(f"  [email] {len(msg_ids)} email(s) non lu(s) de '{pattern}'")
            for msg_id in msg_ids:
                try:
                    status2, msg_data = mail.fetch(msg_id, "(RFC822)")
                    if status2 != "OK":
                        continue
                    raw = msg_data[0][1]
                    msg = _email_lib.message_from_bytes(raw)
                    html = _get_html_body(msg)
                    if html:
                        html_bodies.append(html)
                    # Marquer comme lu immédiatement
                    mail.store(msg_id, "+FLAGS", "\\Seen")
                except Exception as e:
                    print(f"  [email] Erreur lecture msg {msg_id}: {e}")

        mail.logout()
    except imaplib.IMAP4.error as e:
        print(f"  [email] IMAP auth/connexion KO ({label}): {e}")
        print("  [email] → Vérifie que l'IMAP est activé dans Gmail + que le mot de passe est un App Password")
    except Exception as e:
        print(f"  [email] Erreur inattendue ({label}): {type(e).__name__}: {e}")
    return html_bodies


# ---------------------------------------------------------------------------
# Parseurs HTML emails
# ---------------------------------------------------------------------------
def _extract_block(a_tag, soup_root=None) -> tuple[str, Optional[str]]:
    """Remonte depuis un lien pour trouver le bloc annonce. Retourne (texte, image_url)."""
    block = a_tag
    for _ in range(8):
        p = block.parent
        if not p:
            break
        txt = p.get_text(" ", strip=True)
        if len(txt) > 60:
            block = p
            break
        block = p
    bt = clean_text(block.get_text(" ", strip=True))
    img = block.find("img")
    image_url = None
    if img:
        image_url = img.get("src") or img.get("data-src")
        # Filtrer les images de tracking/logo trop petites
        if image_url and ("pixel" in image_url or "spacer" in image_url or "logo" in image_url.lower()):
            image_url = None
    return bt, image_url


def _parse_lbc_email(html: str) -> List[Listing]:
    """Parse un email d'alerte LeBonCoin."""
    soup = BeautifulSoup(html, "lxml")
    listings = []
    id_pat = re.compile(r"leboncoin\.fr/ad/(?:voitures?|auto)/(\d+)", re.IGNORECASE)
    # Aussi: /voitures/occasions/ ancien format
    id_pat2 = re.compile(r"leboncoin\.fr/voitures/occasions/(\d+)", re.IGNORECASE)
    seen = set()

    for a in soup.find_all("a", href=True):
        href = a.get("href", "")
        m = id_pat.search(href) or id_pat2.search(href)
        if not m:
            continue
        listing_id = m.group(1)
        if listing_id in seen:
            continue
        seen.add(listing_id)

        bt, image_url = _extract_block(a)

        # Titre : chercher dans le bloc
        title = ""
        block = a
        for _ in range(8):
            p = block.parent
            if not p or len(p.get_text(strip=True)) > 60:
                block = p or block
                break
            block = p
        for tag in block.find_all(["h2", "h3", "h4", "strong", "b", "p"]):
            t = clean_text(tag.get_text(" ", strip=True))
            if 5 < len(t) < 120:
                title = t
                break
        if not title:
            title = clean_text(a.get_text(" ", strip=True)) or "LeBonCoin"

        listings.append(Listing(
            site="leboncoin", listing_id=listing_id,
            title=title[:200],
            price=_parse_price(bt),
            year=_parse_year(bt),
            mileage=_parse_km(bt),
            location=None,
            url=f"https://www.leboncoin.fr/ad/voitures/{listing_id}",
            image_url=image_url,
            description=bt[:300],
        ))

    return listings


def _parse_lacentrale_email(html: str) -> List[Listing]:
    """Parse un email d'alerte LaCentrale."""
    soup = BeautifulSoup(html, "lxml")
    listings = []
    id_pat  = re.compile(r"auto-occasion-annonce-([A-Z0-9]+)\.html", re.IGNORECASE)
    id_pat2 = re.compile(r"lacentrale\.fr/(?:listing|annonce)/([A-Z0-9\-]+)", re.IGNORECASE)
    seen = set()

    for a in soup.find_all("a", href=True):
        href = a.get("href", "")
        m = id_pat.search(href) or id_pat2.search(href)
        if not m:
            continue
        listing_id = m.group(1)
        if listing_id in seen:
            continue
        seen.add(listing_id)

        bt, image_url = _extract_block(a)

        title = clean_text(a.get_text(" ", strip=True))
        if not title or len(title) < 4:
            block = a
            for _ in range(6):
                p = block.parent
                if not p:
                    break
                block = p
            for tag in block.find_all(["h2", "h3", "h4", "strong"]):
                t = clean_text(tag.get_text(" ", strip=True))
                if 5 < len(t) < 120:
                    title = t
                    break
        title = title or "LaCentrale"

        listings.append(Listing(
            site="lacentrale", listing_id=listing_id,
            title=title[:200],
            price=_parse_price(bt),
            year=_parse_year(bt),
            mileage=_parse_km(bt),
            location=None,
            url=f"https://www.lacentrale.fr/auto-occasion-annonce-{listing_id}.html",
            image_url=image_url,
            description=bt[:300],
        ))

    return listings


def _parse_autoscout24_email(html: str) -> List[Listing]:
    """Parse un email d'alerte AutoScout24."""
    soup = BeautifulSoup(html, "lxml")
    listings = []
    id_pat = re.compile(r"autoscout24\.[a-z]+/offre[s]?/([0-9a-f\-]{20,})", re.IGNORECASE)
    seen = set()

    for a in soup.find_all("a", href=True):
        href = a.get("href", "")
        m = id_pat.search(href)
        if not m:
            continue
        listing_id = m.group(1)
        if listing_id in seen:
            continue
        seen.add(listing_id)

        bt, image_url = _extract_block(a)
        url = href if href.startswith("http") else f"https://www.autoscout24.fr/offres/{listing_id}"

        title = clean_text(a.get_text(" ", strip=True))
        if not title or len(title) < 4:
            block = a
            for _ in range(6):
                p = block.parent
                if not p:
                    break
                block = p
            for tag in block.find_all(["h2", "h3", "h4", "strong"]):
                t = clean_text(tag.get_text(" ", strip=True))
                if 5 < len(t) < 120:
                    title = t
                    break
        title = title or "AutoScout24"

        listings.append(Listing(
            site="autoscout24", listing_id=listing_id,
            title=title[:200],
            price=_parse_price(bt),
            year=_parse_year(bt),
            mileage=_parse_km(bt),
            location=None,
            url=url,
            image_url=image_url,
            description=bt[:300],
        ))

    return listings


EMAIL_PARSERS = {
    "leboncoin":  _parse_lbc_email,
    "lacentrale": _parse_lacentrale_email,
    "autoscout24": _parse_autoscout24_email,
}


# ---------------------------------------------------------------------------
# Point d'entrée public
# ---------------------------------------------------------------------------
def fetch_email_listings() -> List[Listing]:
    """
    Récupère toutes les annonces depuis les alertes email Gmail.
    Retourne une liste de Listing (déduplicés par uid).
    """
    all_listings: List[Listing] = []

    # ── Compte LBC : timdelmas123@gmail.com ─────────────────────────────────
    lbc_user = os.getenv("GMAIL_LBC_USER", "")
    lbc_pass = os.getenv("GMAIL_LBC_PASS", "")
    if lbc_user and lbc_pass:
        print(f"[email] LeBonCoin — compte {lbc_user}")
        htmls = _fetch_gmail_htmls(lbc_user, lbc_pass, SENDERS["leboncoin"], label="LBC")
        print(f"[email] {len(htmls)} email(s) LBC traité(s)")
        for html in htmls:
            lst = _parse_lbc_email(html)
            print(f"  [email LBC] {len(lst)} annonce(s) extraite(s)")
            all_listings.extend(lst)
    else:
        print("[email] GMAIL_LBC_USER/PASS non configurés — LBC email désactivé")

    # ── Compte principal : chabodt@gmail.com ────────────────────────────────
    main_user = os.getenv("GMAIL_MAIN_USER", "")
    main_pass = os.getenv("GMAIL_MAIN_PASS", "")
    if main_user and main_pass:
        for source in ["lacentrale", "autoscout24"]:
            print(f"[email] {source} — compte {main_user}")
            htmls = _fetch_gmail_htmls(main_user, main_pass, SENDERS[source], label=source)
            print(f"[email] {len(htmls)} email(s) {source} traité(s)")
            for html in htmls:
                lst = EMAIL_PARSERS[source](html)
                print(f"  [email {source}] {len(lst)} annonce(s) extraite(s)")
                all_listings.extend(lst)
    else:
        print("[email] GMAIL_MAIN_USER/PASS non configurés — LaCentrale/AS24 email désactivé")

    # Dédup
    seen_uids: dict = {}
    for l in all_listings:
        if l.uid not in seen_uids:
            seen_uids[l.uid] = l

    result = list(seen_uids.values())
    print(f"[email] Total : {len(result)} annonces email uniques")
    return result
