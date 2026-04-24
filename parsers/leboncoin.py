"""
Parser d'email d'alerte Leboncoin.
Les emails "Nouvelle annonce pour votre recherche" contiennent une ou plusieurs annonces
avec un lien vers /voitures/{id}.htm, prix, km, année, localisation.
"""
from __future__ import annotations

import re
from typing import List, Optional

from bs4 import BeautifulSoup, Tag

from models import Listing
from .common import (
    clean_text, clean_tracking_url, extract_listing_id_from_url,
    extract_mileage, extract_price, extract_year,
)


# URLs des annonces Leboncoin : .../voitures/12345.htm ou .../2976547281.htm?...
ID_PATTERNS = [
    re.compile(r"leboncoin\.fr/[^?]*?/(\d{8,})\.htm", re.IGNORECASE),
    re.compile(r"leboncoin\.fr/[^?]*?/(\d{8,})(?:/|\?|$)", re.IGNORECASE),
    re.compile(r"/ad/voitures/(\d{8,})", re.IGNORECASE),
]


def parse(raw_email) -> List[Listing]:
    """Retourne la liste des annonces extraites de l'email."""
    listings: List[Listing] = []

    html = raw_email.html_body or ""
    if not html:
        # fallback texte
        return _parse_text(raw_email.text_body or "")

    soup = BeautifulSoup(html, "lxml")

    # Stratégie : on trouve tous les <a> avec une URL d'annonce valide,
    # puis on remonte au bloc parent et on extrait les infos autour.
    seen_ids = set()
    for a in soup.find_all("a", href=True):
        href = clean_tracking_url(a["href"])
        listing_id = extract_listing_id_from_url(href, ID_PATTERNS)
        if not listing_id or listing_id in seen_ids:
            continue
        seen_ids.add(listing_id)

        # Bloc parent : souvent un <tr>, <td> ou <div> qui contient toute la carte annonce
        block = _find_card_block(a)

        block_text = clean_text(block.get_text(" ", strip=True) if block else a.get_text(" ", strip=True))

        title = _extract_title(a, block)
        price = extract_price(block_text)
        year = extract_year(block_text)
        mileage = extract_mileage(block_text)
        location = _extract_location(block_text, block)
        image_url = _extract_image(block)

        listings.append(Listing(
            site="leboncoin",
            listing_id=listing_id,
            title=title or "Annonce Leboncoin",
            price=price,
            year=year,
            mileage=mileage,
            location=location,
            url=_canonical_url(listing_id, href),
            image_url=image_url,
            description=block_text[:500] if block_text else None,
        ))

    return listings


def _find_card_block(a_tag: Tag) -> Optional[Tag]:
    """Remonte jusqu'au premier <tr> ou bloc contenant les infos de l'annonce."""
    current = a_tag
    for _ in range(6):
        parent = current.parent
        if not parent or not isinstance(parent, Tag):
            break
        if parent.name in ("tr", "table", "div") and len(parent.get_text(strip=True)) > 40:
            return parent
        current = parent
    return a_tag.parent if a_tag.parent else a_tag


def _extract_title(a_tag: Tag, block: Optional[Tag]) -> Optional[str]:
    # Priorité 1 : le texte du <a> lui-même s'il est informatif
    a_text = clean_text(a_tag.get_text(" ", strip=True))
    if a_text and len(a_text) >= 10 and not a_text.lower().startswith(("voir", "consulter", "http")):
        return a_text[:200]
    # Priorité 2 : premier <h1>..<h4> ou <strong> dans le bloc
    if block:
        for tag_name in ("h1", "h2", "h3", "h4", "strong", "b"):
            tag = block.find(tag_name)
            if tag:
                txt = clean_text(tag.get_text(" ", strip=True))
                if txt and len(txt) >= 5:
                    return txt[:200]
    return a_text[:200] if a_text else None


def _extract_location(text: str, block: Optional[Tag]) -> Optional[str]:
    # Format Leboncoin : "Ville (XX)" ou "Ville - Département"
    m = re.search(r"([A-ZÀ-Ÿ][A-Za-zÀ-ÿ\-' ]{2,30})\s*\((\d{2,3})\)", text)
    if m:
        return f"{m.group(1).strip()} ({m.group(2)})"
    return None


def _extract_image(block: Optional[Tag]) -> Optional[str]:
    if not block:
        return None
    img = block.find("img")
    if img and img.get("src"):
        src = img["src"]
        if src.startswith("http"):
            return src
    return None


def _canonical_url(listing_id: str, fallback: str) -> str:
    # Construit l'URL canonique directe vers l'annonce
    return f"https://www.leboncoin.fr/ad/voitures/{listing_id}"


def _parse_text(text: str) -> List[Listing]:
    """Fallback : parser le corps texte uniquement si HTML absent."""
    listings = []
    for pat in ID_PATTERNS:
        for m in pat.finditer(text):
            listing_id = m.group(1)
            listings.append(Listing(
                site="leboncoin",
                listing_id=listing_id,
                title="Annonce Leboncoin",
                price=extract_price(text),
                year=extract_year(text),
                mileage=extract_mileage(text),
                location=None,
                url=f"https://www.leboncoin.fr/ad/voitures/{listing_id}",
            ))
    return listings
