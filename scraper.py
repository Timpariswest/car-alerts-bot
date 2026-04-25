"""
Scraper direct — LeBonCoin, LaCentrale, AutoScout24.
Remplace la lecture d'emails : on scrape directement les pages de résultats.

Chaque site est un SPA Next.js : on extrait le JSON embarqué dans __NEXT_DATA__
(ou d'autres variables window.*) plutôt que de parser le DOM à la volée.

fetch_listings() retourne une liste de Listing, compatible avec main.py.
"""
from __future__ import annotations

import json
import re
import time
import traceback
from typing import List, Optional

import requests
from bs4 import BeautifulSoup

from models import Listing
from parsers.common import extract_price, extract_year, extract_mileage, clean_text


# ---------------------------------------------------------------------------
# Headers communs — simule Chrome macOS
# ---------------------------------------------------------------------------
HEADERS = {
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Accept-Language": "fr-FR,fr;q=0.9",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Encoding": "gzip, deflate, br",
    "Connection": "keep-alive",
    "Upgrade-Insecure-Requests": "1",
    "Sec-Fetch-Dest": "document",
    "Sec-Fetch-Mode": "navigate",
    "Sec-Fetch-Site": "none",
    "Sec-Fetch-User": "?1",
}

# URLs à scraper — Clio 3 et Peugeot 207 strictement filtrés
SEARCH_URLS = [
    # LeBonCoin — "clio 3" et "clio iii" pour cibler uniquement la 3e génération
    {
        "site": "leboncoin",
        "url": "https://www.leboncoin.fr/recherche?category=2&text=clio+3&price_max=3000&mileage_max=260000&sort=time&order=desc",
        "label": "LBC Clio 3",
    },
    {
        "site": "leboncoin",
        "url": "https://www.leboncoin.fr/recherche?category=2&text=clio+iii&price_max=3000&mileage_max=260000&sort=time&order=desc",
        "label": "LBC Clio III",
    },
    {
        "site": "leboncoin",
        "url": "https://www.leboncoin.fr/recherche?category=2&text=peugeot+207&price_max=3000&mileage_max=260000&sort=time&order=desc",
        "label": "LBC 207",
    },
    # LaCentrale — filtre modèle exact Clio 3 et 207
    {
        "site": "lacentrale",
        "url": "https://www.lacentrale.fr/listing?makesModelsCommercialNames=RENAULT%3ACLIO+3&mileageMax=260000&priceMax=3000",
        "label": "LaCentrale Clio 3",
    },
    {
        "site": "lacentrale",
        "url": "https://www.lacentrale.fr/listing?makesModelsCommercialNames=PEUGEOT%3A207&mileageMax=260000&priceMax=3000",
        "label": "LaCentrale 207",
    },
    # AutoScout24 — filtre modèle + prix + km dans l'URL
    {
        "site": "autoscout24",
        "url": "https://www.autoscout24.fr/lst/renault/clio?mmvco=1&ustate=N%2CU&sort=age&desc=1&milemax=260000&priceto=3000&cy=F&atype=C",
        "label": "AS24 Clio",
    },
    {
        "site": "autoscout24",
        "url": "https://www.autoscout24.fr/lst/peugeot/207?mmvco=1&ustate=N%2CU&sort=age&desc=1&milemax=260000&priceto=3000&cy=F&atype=C",
        "label": "AS24 207",
    },
]


# ---------------------------------------------------------------------------
# Session HTTP partagée
# ---------------------------------------------------------------------------
def _make_session() -> requests.Session:
    s = requests.Session()
    s.headers.update(HEADERS)
    return s


# ---------------------------------------------------------------------------
# Utilitaires
# ---------------------------------------------------------------------------

def _extract_next_data(html: str) -> Optional[dict]:
    """Extrait le JSON embarqué dans <script id="__NEXT_DATA__">."""
    m = re.search(r'<script[^>]+id=["\']__NEXT_DATA__["\'][^>]*>([^<]+)</script>', html, re.DOTALL)
    if not m:
        return None
    try:
        return json.loads(m.group(1))
    except (json.JSONDecodeError, ValueError):
        return None


def _safe_get(d: dict, *keys, default=None):
    """Descend dans un dict imbriqué sans KeyError."""
    cur = d
    for k in keys:
        if not isinstance(cur, dict):
            return default
        cur = cur.get(k, default)
        if cur is None:
            return default
    return cur


def _parse_price(val) -> Optional[int]:
    if val is None:
        return None
    if isinstance(val, (int, float)):
        return int(val)
    if isinstance(val, list) and val:
        return _parse_price(val[0])
    if isinstance(val, str):
        return extract_price(val + " EUR")
    return None


def _parse_km(val) -> Optional[int]:
    if val is None:
        return None
    if isinstance(val, (int, float)):
        return int(val)
    if isinstance(val, str):
        digits = re.sub(r"[^\d]", "", val)
        return int(digits) if digits else None
    return None


def _parse_year(val) -> Optional[int]:
    if val is None:
        return None
    if isinstance(val, int):
        return val if 1980 <= val <= 2030 else None
    if isinstance(val, str):
        m = re.search(r"(19[89]\d|20[0-3]\d)", val)
        return int(m.group(1)) if m else None
    return None


# ---------------------------------------------------------------------------
# Parser LeBonCoin
# ---------------------------------------------------------------------------

def _parse_leboncoin(html: str, source_url: str) -> List[Listing]:
    """
    LeBonCoin Next.js : annonces dans __NEXT_DATA__.props.pageProps.searchData.ads[]
    Chaque ad : list_id, subject, price[], attributes[], location{city,department_id}, images{small_url}
    """
    listings: List[Listing] = []
    data = _extract_next_data(html)

    if data:
        ads = (
            _safe_get(data, "props", "pageProps", "searchData", "ads")
            or _safe_get(data, "props", "pageProps", "initialData", "ads")
            or _safe_get(data, "props", "pageProps", "ads")
            or []
        )

        for ad in ads:
            try:
                listing_id = str(ad.get("list_id", ""))
                if not listing_id:
                    continue

                title = ad.get("subject", "") or "Annonce LeBonCoin"
                price = _parse_price(ad.get("price"))

                attrs = ad.get("attributes", [])
                km = None
                year = None
                if isinstance(attrs, list):
                    attr_map = {a.get("key"): a.get("value") for a in attrs if isinstance(a, dict)}
                    km = _parse_km(attr_map.get("mileage"))
                    year = _parse_year(attr_map.get("regdate"))
                elif isinstance(attrs, dict):
                    km = _parse_km(attrs.get("mileage"))
                    year = _parse_year(attrs.get("regdate"))

                loc_obj = ad.get("location") or {}
                city = loc_obj.get("city", "") if isinstance(loc_obj, dict) else ""
                dept = loc_obj.get("department_id", "") if isinstance(loc_obj, dict) else ""
                location = f"{city} ({dept})" if city and dept else city or None

                images = ad.get("images") or {}
                image_url = None
                if isinstance(images, dict):
                    image_url = images.get("small_url") or images.get("thumb_url")
                elif isinstance(images, list) and images:
                    i0 = images[0]
                    image_url = i0.get("small_url") if isinstance(i0, dict) else None

                listings.append(Listing(
                    site="leboncoin",
                    listing_id=listing_id,
                    title=clean_text(title)[:200],
                    price=price,
                    year=year,
                    mileage=km,
                    location=location,
                    url=f"https://www.leboncoin.fr/ad/voitures/{listing_id}",
                    image_url=image_url,
                    description=clean_text(title),
                ))
            except Exception:
                continue

    if not listings:
        listings = _parse_leboncoin_html(html)

    return listings


def _parse_leboncoin_html(html: str) -> List[Listing]:
    listings = []
    soup = BeautifulSoup(html, "lxml")
    id_pat = re.compile(r"/ad/voitures/(\d{8,})", re.IGNORECASE)
    seen = set()
    for a in soup.find_all("a", href=True):
        m = id_pat.search(a["href"])
        if not m:
            continue
        listing_id = m.group(1)
        if listing_id in seen:
            continue
        seen.add(listing_id)
        block = a
        for _ in range(5):
            p = block.parent
            if not p:
                break
            if len(p.get_text(strip=True)) > 30:
                block = p
                break
            block = p
        block_text = clean_text(block.get_text(" ", strip=True))
        listings.append(Listing(
            site="leboncoin",
            listing_id=listing_id,
            title=clean_text(a.get_text(" ", strip=True))[:200] or "Annonce LeBonCoin",
            price=extract_price(block_text),
            year=extract_year(block_text),
            mileage=extract_mileage(block_text),
            location=None,
            url=f"https://www.leboncoin.fr/ad/voitures/{listing_id}",
        ))
    return listings


# ---------------------------------------------------------------------------
# Parser LaCentrale
# ---------------------------------------------------------------------------

def _parse_lacentrale(html: str, source_url: str) -> List[Listing]:
    """
    LaCentrale Next.js : annonces dans __NEXT_DATA__.props.pageProps.(vehicles|listings|searchResult.vehicles)[]
    Chaque véhicule : id, price, mileage, year/firstRegistrationDate, make, model, location{city,zipCode}, photos[]
    """
    listings: List[Listing] = []
    data = _extract_next_data(html)

    if data:
        page_props = _safe_get(data, "props", "pageProps") or {}
        vehicles = (
            page_props.get("vehicles")
            or page_props.get("listings")
            or _safe_get(page_props, "searchResult", "vehicles")
            or _safe_get(page_props, "searchResult", "listings")
            or page_props.get("ads")
            or []
        )

        for v in vehicles:
            try:
                listing_id = str(v.get("id") or v.get("listingId") or v.get("adId") or "")
                if not listing_id:
                    continue

                make = v.get("make") or v.get("brand") or ""
                model = v.get("model") or v.get("modelLabel") or ""
                version = v.get("version") or v.get("versionLabel") or ""
                title = " ".join(filter(None, [str(make), str(model), str(version)])).strip() or "Annonce LaCentrale"

                price_raw = v.get("price")
                if isinstance(price_raw, dict):
                    price_raw = price_raw.get("value") or price_raw.get("amount")
                price = _parse_price(price_raw)

                km = _parse_km(v.get("mileage") or v.get("km"))
                year = _parse_year(
                    v.get("year") or v.get("firstRegistrationDate") or v.get("registrationDate")
                )

                loc = v.get("location") or v.get("localisation") or {}
                if isinstance(loc, dict):
                    city = loc.get("city") or loc.get("commune") or ""
                    dept = str(loc.get("zipCode") or loc.get("codePostal") or loc.get("departmentCode") or "")
                    location = f"{city} ({dept[:2]})" if city and dept else city or None
                else:
                    location = None

                photos = v.get("photos") or v.get("images") or v.get("pictures") or []
                image_url = None
                if isinstance(photos, list) and photos:
                    p0 = photos[0]
                    if isinstance(p0, dict):
                        image_url = p0.get("url") or p0.get("src") or p0.get("thumb")
                    elif isinstance(p0, str):
                        image_url = p0

                listings.append(Listing(
                    site="lacentrale",
                    listing_id=listing_id,
                    title=clean_text(title)[:200],
                    price=price,
                    year=year,
                    mileage=km,
                    location=location,
                    url=f"https://www.lacentrale.fr/auto-occasion-annonce-{listing_id}.html",
                    image_url=image_url,
                    description=clean_text(title),
                ))
            except Exception:
                continue

    if not listings:
        listings = _parse_lacentrale_html(html)

    return listings


def _parse_lacentrale_html(html: str) -> List[Listing]:
    listings = []
    soup = BeautifulSoup(html, "lxml")
    id_pat = re.compile(r"auto-occasion-annonce-([A-Z0-9]+)\.html", re.IGNORECASE)
    seen = set()
    for a in soup.find_all("a", href=True):
        m = id_pat.search(a["href"])
        if not m:
            continue
        listing_id = m.group(1)
        if listing_id in seen:
            continue
        seen.add(listing_id)
        block = a
        for _ in range(5):
            p = block.parent
            if not p:
                break
            if len(p.get_text(strip=True)) > 30:
                block = p
                break
            block = p
        block_text = clean_text(block.get_text(" ", strip=True))
        listings.append(Listing(
            site="lacentrale",
            listing_id=listing_id,
            title=clean_text(a.get_text(" ", strip=True))[:200] or "Annonce LaCentrale",
            price=extract_price(block_text),
            year=extract_year(block_text),
            mileage=extract_mileage(block_text),
            location=None,
            url=f"https://www.lacentrale.fr/auto-occasion-annonce-{listing_id}.html",
        ))
    return listings


# ---------------------------------------------------------------------------
# Parser AutoScout24
# ---------------------------------------------------------------------------

def _parse_autoscout24(html: str, source_url: str) -> List[Listing]:
    """
    AutoScout24 Next.js : annonces dans __NEXT_DATA__.props.pageProps.(listings|ads|searchResponse.listings)[]
    Chaque listing : id/guid, price{value}, mileage, firstRegistration, tracking{make,model},
                     location{city,zip}, images[]
    """
    listings: List[Listing] = []
    data = _extract_next_data(html)

    if data:
        page_props = _safe_get(data, "props", "pageProps") or {}
        ads = (
            page_props.get("listings")
            or page_props.get("ads")
            or _safe_get(page_props, "searchResponse", "listings")
            or _safe_get(page_props, "searchResponse", "ads")
            or _safe_get(page_props, "initialState", "listings")
            or []
        )

        for ad in ads:
            try:
                listing_id = str(ad.get("id") or ad.get("guid") or "")
                if not listing_id:
                    continue

                tracking = ad.get("tracking") or {}
                vehicle = ad.get("vehicle") or {}
                make = tracking.get("make") or vehicle.get("make") or ad.get("make") or ""
                model = tracking.get("model") or vehicle.get("model") or ad.get("model") or ""
                title = ad.get("title") or " ".join(filter(None, [str(make), str(model)])).strip() or "Annonce AutoScout24"

                price_obj = ad.get("price") or {}
                if isinstance(price_obj, dict):
                    price = _parse_price(price_obj.get("value") or price_obj.get("amount"))
                else:
                    price = _parse_price(price_obj)

                km = _parse_km(ad.get("mileage") or ad.get("km") or vehicle.get("mileage"))
                year = _parse_year(
                    ad.get("firstRegistration")
                    or ad.get("year")
                    or vehicle.get("firstRegistration")
                    or vehicle.get("registrationDate")
                )

                loc = ad.get("location") or {}
                if isinstance(loc, dict):
                    city = loc.get("city") or loc.get("countryCode") or ""
                    zip_code = str(loc.get("zip") or loc.get("postalCode") or "")
                    location = f"{city} ({zip_code[:5]})" if city else None
                else:
                    location = None

                images = ad.get("images") or ad.get("photos") or []
                image_url = None
                if isinstance(images, list) and images:
                    i0 = images[0]
                    if isinstance(i0, dict):
                        image_url = i0.get("url") or i0.get("src") or i0.get("thumb")
                    elif isinstance(i0, str):
                        image_url = i0

                ad_url = ad.get("url") or f"https://www.autoscout24.fr/offres/{listing_id}"
                if not str(ad_url).startswith("http"):
                    ad_url = "https://www.autoscout24.fr" + str(ad_url)

                listings.append(Listing(
                    site="autoscout24",
                    listing_id=listing_id,
                    title=clean_text(str(title))[:200],
                    price=price,
                    year=year,
                    mileage=km,
                    location=location,
                    url=ad_url,
                    image_url=image_url,
                    description=clean_text(str(title)),
                ))
            except Exception:
                continue

    if not listings:
        listings = _parse_autoscout24_html(html)

    return listings


def _parse_autoscout24_html(html: str) -> List[Listing]:
    listings = []
    soup = BeautifulSoup(html, "lxml")
    id_pat = re.compile(r"/off(?:res|ers)/([0-9a-f-]{32,40})", re.IGNORECASE)
    seen = set()
    for a in soup.find_all("a", href=True):
        m = id_pat.search(a["href"])
        if not m:
            continue
        listing_id = m.group(1)
        if listing_id in seen:
            continue
        seen.add(listing_id)
        block = a
        for _ in range(5):
            p = block.parent
            if not p:
                break
            if len(p.get_text(strip=True)) > 30:
                block = p
                break
            block = p
        block_text = clean_text(block.get_text(" ", strip=True))
        listings.append(Listing(
            site="autoscout24",
            listing_id=listing_id,
            title=clean_text(a.get_text(" ", strip=True))[:200] or "Annonce AutoScout24",
            price=extract_price(block_text),
            year=extract_year(block_text),
            mileage=extract_mileage(block_text),
            location=None,
            url=f"https://www.autoscout24.fr/offres/{listing_id}",
        ))
    return listings


# ---------------------------------------------------------------------------
# Dispatcher
# ---------------------------------------------------------------------------

SITE_PARSERS = {
    "leboncoin": _parse_leboncoin,
    "lacentrale": _parse_lacentrale,
    "autoscout24": _parse_autoscout24,
}


def _fetch_one(session: requests.Session, entry: dict, retries: int = 2) -> List[Listing]:
    site = entry["site"]
    url = entry["url"]
    label = entry.get("label", url)
    parser = SITE_PARSERS[site]

    for attempt in range(retries + 1):
        try:
            r = session.get(url, timeout=20, allow_redirects=True)
            if r.status_code == 200:
                listings = parser(r.text, url)
                print(f"  [{label}] HTTP 200 — {len(listings)} annonces")
                return listings
            elif r.status_code in (403, 429):
                wait = 5 * (attempt + 1)
                print(f"  [{label}] HTTP {r.status_code} (attempt {attempt+1}) — attente {wait}s")
                if attempt < retries:
                    time.sleep(wait)
            else:
                print(f"  [{label}] HTTP {r.status_code}")
                return []
        except requests.RequestException as e:
            print(f"  [{label}] Erreur réseau (attempt {attempt+1}): {e}")
            if attempt < retries:
                time.sleep(3)

    return []


# ---------------------------------------------------------------------------
# Point d'entrée public
# ---------------------------------------------------------------------------

def fetch_listings(urls: Optional[List[dict]] = None) -> List[Listing]:
    """
    Scrape tous les sites et retourne une liste unifiée de Listing.
    Paramètre optionnel : liste de dicts {"site", "url", "label"} pour surcharger SEARCH_URLS.
    """
    targets = urls or SEARCH_URLS
    session = _make_session()
    all_listings: List[Listing] = []

    print(f"[scraper] Scraping {len(targets)} URLs...")
    for entry in targets:
        try:
            listings = _fetch_one(session, entry)
            all_listings.extend(listings)
        except Exception as e:
            print(f"  [{entry.get('label', entry['url'])}] Exception: {e}")
            traceback.print_exc()
        time.sleep(1.5)  # politesse inter-requêtes

    # Dédup global par uid
    seen_uids: dict = {}
    for l in all_listings:
        if l.uid not in seen_uids:
            seen_uids[l.uid] = l

    result = list(seen_uids.values())
    print(f"[scraper] Total : {len(result)} annonces uniques")
    return result
