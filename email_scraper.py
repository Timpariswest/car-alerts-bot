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
import socket
from datetime import datetime, timedelta
from typing import List, Optional

# Timeout global pour toutes les connexions réseau (IMAP inclus)
socket.setdefaulttimeout(20)

from bs4 import BeautifulSoup

from models import Listing
from parsers.common import clean_text

# ---------------------------------------------------------------------------
# Expéditeurs connus (on cherche ces patterns dans le champ FROM)
# ---------------------------------------------------------------------------
SENDERS = {
    "leboncoin":  ["alertes@leboncoin.fr", "noreply@leboncoin.fr", "leboncoin.fr"],
    "lacentrale": ["alerte@lacentrale.fr"],  # UNIQUEMENT les alertes de recherche, pas les messages acheteurs (no_reply@)
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
        mail = imaplib.IMAP4_SSL("imap.gmail.com", 993, timeout=20)
        mail.login(username, password)
        mail.select("INBOX")

        since = (datetime.utcnow() - timedelta(days=7)).strftime("%d-%b-%Y")
        for pattern in sender_patterns:
            status, data = mail.search(None, f'(FROM "{pattern}" SINCE "{since}")')
            if status != "OK":
                continue
            msg_ids = data[0].split()
            if not msg_ids:
                print(f"  [email] 0 email de '{pattern}' (7 derniers jours) — configurer alerte sur le site ?")
                continue
            # Max 20 emails par pattern
            msg_ids = msg_ids[-20:]
            print(f"  [email] {len(msg_ids)} email(s) de '{pattern}' (7j)")
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
        print("  [email] → Vérifie IMAP activé dans Gmail + App Password (pas ton vrai mdp)")
    except socket.timeout:
        print(f"  [email] IMAP timeout ({label}) — Gmail inaccessible depuis GitHub Actions")
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
    """
    Parse un email d'alerte LeBonCoin.
    Chaque email contient une ou plusieurs cartes d'annonce en HTML.
    On ne prend que les liens /ad/voitures/ (pas motos, immo, etc.)
    """
    soup = BeautifulSoup(html, "lxml")
    listings = []

    # Uniquement les annonces voitures (filtre catégorie)
    id_pat = re.compile(r"leboncoin\.fr/ad/voitures/(\d{7,})", re.IGNORECASE)
    # Texte complet de l'email (chaque email LBC = 1 annonce → on peut tout scanner)
    full_text = soup.get_text(" ", strip=True)
    # Image principale (première image avec extension photo)
    main_image = None
    for img in soup.find_all("img"):
        src = img.get("src") or img.get("data-src", "")
        if src and re.search(r"\.(jpg|jpeg|png|webp)", src, re.IGNORECASE):
            main_image = src
            break

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

        # Titre : cherche un texte court qui ressemble à un nom de véhicule
        # dans les éléments proches du lien (pas tout l'email)
        title = ""
        node = a
        for _ in range(5):
            if not node or not node.parent:
                break
            node = node.parent
            for tag in node.find_all(["h1","h2","h3","h4","strong","b"]):
                t = clean_text(tag.get_text(" ", strip=True))
                if (6 < len(t) < 80
                        and not re.match(r"^[\d\s€,\.]+$", t)
                        and not t.lower().startswith("bonjour")
                        and "km" not in t.lower()[:8]):
                    title = t
                    break
            if title:
                break
        if not title:
            title = "LeBonCoin"

        # Prix / km / année : scanne le texte complet de l'email
        # (fiable car 1 annonce par email)
        price = _parse_price(full_text)
        km    = _parse_km(full_text)
        year  = _parse_year(full_text)

        listings.append(Listing(
            site="leboncoin", listing_id=listing_id,
            title=title[:200],
            price=price, year=year, mileage=km,
            location=None,
            url=f"https://www.leboncoin.fr/ad/voitures/{listing_id}",
            image_url=main_image,
            description=clean_text(full_text[:400]),
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
def _get_gmail_accounts() -> list[tuple[str, str]]:
    """
    Retourne la liste des comptes Gmail configurés (user, password).
    Supporte les noms de secrets :
      GMAIL_USER / GMAIL_APP_PASSWORD
      GMAIL_USER_1 / GMAIL_APP_PASSWORD_1
      GMAIL_USER_2 / GMAIL_APP_PASSWORD_2
      GMAIL_LBC_USER / GMAIL_LBC_PASS
      GMAIL_MAIN_USER / GMAIL_MAIN_PASS
    """
    pairs = [
        (os.getenv("GMAIL_USER",   ""), os.getenv("GMAIL_APP_PASSWORD",   "")),
        (os.getenv("GMAIL_USER_1", ""), os.getenv("GMAIL_APP_PASSWORD_1", "")),
        (os.getenv("GMAIL_USER_2", ""), os.getenv("GMAIL_APP_PASSWORD_2", "")),
        (os.getenv("GMAIL_LBC_USER",  ""), os.getenv("GMAIL_LBC_PASS",  "")),
        (os.getenv("GMAIL_MAIN_USER", ""), os.getenv("GMAIL_MAIN_PASS", "")),
    ]
    # Déduplique par user (on garde le premier mot de passe trouvé pour chaque compte)
    seen: dict[str, str] = {}
    for user, pwd in pairs:
        if user and pwd and user not in seen:
            seen[user] = pwd
    return list(seen.items())


def fetch_email_listings() -> List[Listing]:
    """
    Récupère toutes les annonces depuis les alertes email Gmail.
    Scanne TOUS les comptes configurés pour TOUTES les sources
    → pas besoin de savoir quel compte reçoit quoi.
    """
    all_listings: List[Listing] = []

    accounts = _get_gmail_accounts()
    if not accounts:
        print("[email] Aucun compte Gmail configuré — email scraper désactivé")
        print("[email] → Ajoute GMAIL_USER_1 + GMAIL_APP_PASSWORD_1 (et _2) dans les secrets GitHub")
        return []

    print(f"[email] {len(accounts)} compte(s) Gmail configuré(s)")

    for user, pwd in accounts:
        print(f"[email] Scan de {user}...")
        for source, patterns in SENDERS.items():
            htmls = _fetch_gmail_htmls(user, pwd, patterns, label=f"{source}@{user[:15]}")
            if htmls:
                print(f"  [email] {len(htmls)} email(s) {source} sur {user}")
            for html in htmls:
                lst = EMAIL_PARSERS[source](html)
                if lst:
                    print(f"  [email {source}] {len(lst)} annonce(s) extraite(s)")
                all_listings.extend(lst)

    # Dédup
    seen_uids: dict = {}
    for l in all_listings:
        if l.uid not in seen_uids:
            seen_uids[l.uid] = l

    result = list(seen_uids.values())
    print(f"[email] Total : {len(result)} annonces email uniques")
    return result
