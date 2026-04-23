"""
Utilitaires communs aux parsers d'emails.
Les emails d'alerte contiennent des annonces structurées dans du HTML.
On extrait : titre, prix, km, année, localisation, URL, image.
"""
from __future__ import annotations

import html as ihtml
import re
from typing import Optional, Iterable
from urllib.parse import urlparse, parse_qs, unquote


# Prix format réaliste : 1-3 chiffres puis éventuellement groupes de 3 chiffres
# séparés par espace/NBSP/point. Borne supérieure : pas plus de 7 chiffres en tout.
PRICE_RE = re.compile(
    r"(?<![\d\w])(\d{1,3}(?:[\s\u00a0\.]\d{3}){0,2}|\d{3,6})\s*(?:€|eur|EUR)",
    re.IGNORECASE,
)
YEAR_RE = re.compile(r"(?<!\d)(19[89]\d|20[0-3]\d)(?!\d)")
# Kilométrage : même forme que le prix
MILEAGE_RE = re.compile(
    r"(?<![\d\w])(\d{1,3}(?:[\s\u00a0\.]\d{3}){0,2}|\d{3,6})\s*km",
    re.IGNORECASE,
)


def clean_text(s: str) -> str:
    if not s:
        return ""
    s = ihtml.unescape(s)
    s = re.sub(r"\s+", " ", s)
    return s.strip()


def parse_int(raw: str) -> Optional[int]:
    if not raw:
        return None
    digits = re.sub(r"[^\d]", "", raw)
    if not digits:
        return None
    try:
        return int(digits)
    except ValueError:
        return None


def extract_price(text: str) -> Optional[int]:
    if not text:
        return None
    m = PRICE_RE.search(text)
    if not m:
        return None
    return parse_int(m.group(1))


def extract_year(text: str) -> Optional[int]:
    if not text:
        return None
    m = YEAR_RE.search(text)
    if not m:
        return None
    try:
        return int(m.group(1))
    except ValueError:
        return None


def extract_mileage(text: str) -> Optional[int]:
    if not text:
        return None
    m = MILEAGE_RE.search(text)
    if not m:
        return None
    return parse_int(m.group(1))


def clean_tracking_url(url: str) -> str:
    """Beaucoup d'emails d'alerte encapsulent les liens dans un tracker.
    On tente d'extraire l'URL cible si elle est dans un paramètre courant."""
    if not url:
        return url
    try:
        parsed = urlparse(url)
        qs = parse_qs(parsed.query)
        # Paramètres courants de redirect
        for key in ("url", "redirect", "target", "u", "link", "dest"):
            if key in qs and qs[key]:
                candidate = unquote(qs[key][0])
                if candidate.startswith("http"):
                    return candidate
        return url
    except Exception:
        return url


def extract_listing_id_from_url(url: str, patterns: Iterable[re.Pattern]) -> Optional[str]:
    for pat in patterns:
        m = pat.search(url)
        if m:
            return m.group(1)
    return None
