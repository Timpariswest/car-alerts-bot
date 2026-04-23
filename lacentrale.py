"""
Parser d'email d'alerte LaCentrale.
Format : email "Nouvelles annonces correspondant à votre recherche"
contenant des cartes avec lien /auto-occasion-annonce-{id}.html
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


ID_PATTERNS = [
    re.compile(r"auto-occasion-annonce-([A-Z0-9]+)\.html", re.IGNORECASE),
    re.compile(r"lacentrale\.fr/.*?annonce[-/](\d{6,})", re.IGNORECASE),
]


def parse(raw_email) -> List[Listing]:
    listings: List[Listing] = []
    html = raw_email.html_body or ""
    if not html:
        return _parse_text(raw_email.text_body or "")

    soup = BeautifulSoup(html, "lxml")

    seen_ids = set()
    for a in soup.find_all("a", href=True):
        href = clean_tracking_url(a["href"])
        if "lacentrale" not in href.lower():
            continue
        listing_id = extract_listing_id_from_url(href, ID_PATTERNS)
        if not listing_id or listing_id in seen_ids:
            continue
        seen_ids.add(listing_id)

        block = _find_card_block(a)
        block_text = clean_text(block.get_text(" ", strip=True) if block else a.get_text(" ", strip=True))

        title = _extract_title(a, block)
        price = extract_price(block_text)
        year = extract_year(block_text)
        mileage = extract_mileage(block_text)
        location = _extract_location(block_text)
        image_url = _extract_image(block)

        listings.append(Listing(
            site="lacentrale",
            listing_id=listing_id,
            title=title or "Annonce LaCentrale",
            price=price,
            year=year,
            mileage=mileage,
            location=location,
            url=f"https://www.lacentrale.fr/auto-occasion-annonce-{listing_id}.html",
            image_url=image_url,
            description=block_text[:500] if block_text else None,
        ))

    return listings


def _find_card_block(a_tag: Tag) -> Optional[Tag]:
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
    a_text = clean_text(a_tag.get_text(" ", strip=True))
    if a_text and len(a_text) >= 10 and not a_text.lower().startswith(("voir", "consulter", "http")):
        return a_text[:200]
    if block:
        for tag_name in ("h1", "h2", "h3", "h4", "strong", "b"):
            tag = block.find(tag_name)
            if tag:
                txt = clean_text(tag.get_text(" ", strip=True))
                if txt and len(txt) >= 5:
                    return txt[:200]
    return a_text[:200] if a_text else None


def _extract_location(text: str) -> Optional[str]:
    # LaCentrale affiche souvent "Département (XX)" ou "Ville - XX"
    m = re.search(r"([A-ZÀ-Ÿ][A-Za-zÀ-ÿ\-' ]{2,30})\s*\(?(\d{2})\)?", text)
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


def _parse_text(text: str) -> List[Listing]:
    listings = []
    for pat in ID_PATTERNS:
        for m in pat.finditer(text):
            listing_id = m.group(1)
            listings.append(Listing(
                site="lacentrale",
                listing_id=listing_id,
                title="Annonce LaCentrale",
                price=extract_price(text),
                year=extract_year(text),
                mileage=extract_mileage(text),
                location=None,
                url=f"https://www.lacentrale.fr/auto-occasion-annonce-{listing_id}.html",
            ))
    return listings
