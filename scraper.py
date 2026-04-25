"""
Scraper direct — LeBonCoin (API JSON), LaCentrale, AutoScout24.
fetch_listings() retourne une liste de Listing compatibles avec main.py.
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
# Headers
# ---------------------------------------------------------------------------
HEADERS_HTML = {
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Accept-Language": "fr-FR,fr;q=0.9",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Encoding": "gzip, deflate, br",
    "Connection": "keep-alive",
}

# Clé API publique utilisée par le site LeBonCoin lui-même
LBC_API_KEY = "ba0c2dad52b3565fd92a81af2b6386d7"
HEADERS_LBC = {
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36",
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "fr-FR,fr;q=0.9",
    "Content-Type": "application/json",
    "api_key": LBC_API_KEY,
    "Origin": "https://www.leboncoin.fr",
    "Referer": "https://www.leboncoin.fr/",
}


# ---------------------------------------------------------------------------
# Config des recherches
# ---------------------------------------------------------------------------
LBC_SEARCHES = [
    {"text": "clio 3",      "label": "LBC Clio 3"},
    {"text": "clio iii",    "label": "LBC Clio III"},
    {"text": "peugeot 207", "label": "LBC 207"},
]

SEARCH_URLS = [
    # AutoScout24 — fonctionne bien
    {
        "site": "autoscout24", "method": "html",
        "url": "https://www.autoscout24.fr/lst/renault/clio?mmvco=1&ustate=N%2CU&sort=age&desc=1&milemax=260000&priceto=3000&cy=F&atype=C",
        "label": "AS24 Clio",
    },
    {
        "site": "autoscout24", "method": "html",
        "url": "https://www.autoscout24.fr/lst/peugeot/207?mmvco=1&ustate=N%2CU&sort=age&desc=1&milemax=260000&priceto=3000&cy=F&atype=C",
        "label": "AS24 207",
    },
    # LaCentrale
    {
        "site": "lacentrale", "method": "html",
        "url": "https://www.lacentrale.fr/listing?makesModelsCommercialNames=RENAULT%3ACLIO+3&mileageMax=260000&priceMax=3000",
        "label": "LaCentrale Clio 3",
    },
    {
        "site": "lacentrale", "method": "html",
        "url": "https://www.lacentrale.fr/listing?makesModelsCommercialNames=PEUGEOT%3A207&mileageMax=260000&priceMax=3000",
        "label": "LaCentrale 207",
    },
]


# ---------------------------------------------------------------------------
# Utilitaires
# ---------------------------------------------------------------------------
def _extract_next_data(html: str) -> Optional[dict]:
    m = re.search(r'<script[^>]+id=["\']__NEXT_DATA__["\'][^>]*>([^<]+)</script>', html, re.DOTALL)
    if not m:
        return None
    try:
        return json.loads(m.group(1))
    except (json.JSONDecodeError, ValueError):
        return None


def _safe_get(d, *keys, default=None):
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
    if isinstance(val, (int, float)) and val > 0:
        v = int(val)
        return v if 100 < v < 100000 else None
    if isinstance(val, list) and val:
        return _parse_price(val[0])
    if isinstance(val, str):
        digits = re.sub(r"[^\d]", "", val)
        v = int(digits) if digits else None
        return v if v and 100 < v < 100000 else None
    return None


def _parse_km(val) -> Optional[int]:
    if val is None:
        return None
    if isinstance(val, (int, float)):
        v = int(val)
        return v if 0 < v < 1000000 else None
    if isinstance(val, str):
        digits = re.sub(r"[^\d]", "", val)
        v = int(digits) if digits else None
        return v if v and 0 < v < 1000000 else None
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
# LeBonCoin — API JSON interne
# ---------------------------------------------------------------------------
def _fetch_leboncoin_api(session: requests.Session, search_text: str, label: str) -> List[Listing]:
    """
    Utilise l'API interne LeBonCoin (endpoint adfinder) pour récupérer
    les annonces en JSON — non bloqué contrairement au scraping HTML.
    """
    url = "https://api.leboncoin.fr/api/adfinder/v1/search"
    payload = {
        "limit": 35,
        "limit_alu": 3,
        "filters": {
            "category": {"id": "2"},
            "keywords": {"text": search_text, "type": "all"},
            "ranges": {
                "price": {"max": 3000},
                "mileage": {"max": 260000},
            },
            "location": {},
        },
        "sort_by": "time",
        "sort_order": "desc",
        "owner": {"type": "all"},
    }

    try:
        r = session.post(url, json=payload, headers=HEADERS_LBC, timeout=20)
        print(f"  [{label}] API LBC HTTP {r.status_code}")
        if r.status_code != 200:
            return []
        data = r.json()
    except Exception as e:
        print(f"  [{label}] Erreur API LBC: {e}")
        return []

    ads = data.get("ads") or []
    print(f"  [{label}] {len(ads)} annonces")
    listings = []

    for ad in ads:
        try:
            listing_id = str(ad.get("list_id", ""))
            if not listing_id:
                continue

            title = ad.get("subject", "") or "Annonce LeBonCoin"

            # Prix
            price = _parse_price(ad.get("price"))
            if price is None:
                price_list = ad.get("price", [])
                if isinstance(price_list, list) and price_list:
                    price = _parse_price(price_list[0])

            # Attributs (km, année)
            attrs = ad.get("attributes", [])
            km = None
            year = None
            if isinstance(attrs, list):
                attr_map = {a.get("key"): a.get("value") for a in attrs if isinstance(a, dict)}
                km = _parse_km(attr_map.get("mileage"))
                year_raw = attr_map.get("regdate") or attr_map.get("year")
                year = _parse_year(year_raw)
            elif isinstance(attrs, dict):
                km = _parse_km(attrs.get("mileage"))
                year = _parse_year(attrs.get("regdate") or attrs.get("year"))

            # Localisation
            loc_obj = ad.get("location") or {}
            city = loc_obj.get("city", "") if isinstance(loc_obj, dict) else ""
            dept = str(loc_obj.get("department_id", "")) if isinstance(loc_obj, dict) else ""
            location = f"{city} ({dept})" if city and dept else city or None

            # Image
            images = ad.get("images") or {}
            image_url = None
            if isinstance(images, dict):
                thumbs = images.get("thumb_url") or images.get("small_url") or images.get("urls", [None])[0]
                image_url = thumbs
            elif isinstance(images, list) and images:
                image_url = images[0].get("small_url") if isinstance(images[0], dict) else None

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
                description=clean_text(ad.get("body", title))[:300],
            ))
        except Exception:
            continue

    return listings


# ---------------------------------------------------------------------------
# AutoScout24 — __NEXT_DATA__ avec multi-chemins
# ---------------------------------------------------------------------------
def _parse_autoscout24(html: str, source_url: str) -> List[Listing]:
    listings: List[Listing] = []
    data = _extract_next_data(html)

    if not data:
        print("    [AS24] __NEXT_DATA__ introuvable, fallback HTML")
        return _parse_autoscout24_html(html)

    page_props = _safe_get(data, "props", "pageProps") or {}
    ads = (
        page_props.get("listings")
        or page_props.get("ads")
        or _safe_get(page_props, "searchResponse", "listings")
        or _safe_get(page_props, "searchResponse", "ads")
        or _safe_get(page_props, "initialState", "listings")
        or []
    )

    print(f"    [AS24] {len(ads)} annonces dans __NEXT_DATA__")
    if ads:
        print(f"    [AS24 debug] clés: {list(ads[0].keys())[:12]}")
        # Debug prix pour le 1er item
        ad0 = ads[0]
        print(f"    [AS24 debug] price={ad0.get('price')} prices={ad0.get('prices')} "
              f"vehicle_keys={list(ad0.get('vehicle', {}).keys())[:8]}")

    for ad in ads:
        try:
            listing_id = str(ad.get("id") or ad.get("guid") or "")
            if not listing_id:
                continue

            vehicle = ad.get("vehicle") or {}
            tracking = ad.get("tracking") or {}

            # Titre
            make = vehicle.get("make") or tracking.get("make") or ad.get("make") or ""
            model = vehicle.get("model") or tracking.get("model") or ad.get("model") or ""
            version = vehicle.get("version") or vehicle.get("modelVersion") or ""
            title = (ad.get("title") or
                     " ".join(filter(None, [str(make), str(model), str(version)])).strip() or
                     "AutoScout24")

            # Prix — 4 chemins
            price = None
            prices_obj = ad.get("prices") or {}
            if isinstance(prices_obj, dict):
                pub = prices_obj.get("public") or {}
                price = _parse_price(pub.get("priceRaw") or pub.get("price") or pub.get("value"))
            if price is None:
                price_obj = ad.get("price") or {}
                if isinstance(price_obj, dict):
                    price = _parse_price(
                        price_obj.get("value") or price_obj.get("amount") or
                        price_obj.get("priceRaw") or price_obj.get("raw")
                    )
                else:
                    price = _parse_price(price_obj)
            if price is None:
                price = _parse_price(ad.get("priceRaw") or ad.get("priceValue") or
                                     vehicle.get("price"))

            # Km — 3 chemins
            km = (_parse_km(vehicle.get("mileage") or vehicle.get("km")) or
                  _parse_km(ad.get("mileage") or ad.get("km")) or
                  _parse_km(tracking.get("mileage")))

            # Année — 4 chemins
            year = (_parse_year(vehicle.get("firstRegistrationYear")) or
                    _parse_year(vehicle.get("firstRegistration")) or
                    _parse_year(ad.get("firstRegistration") or ad.get("year")) or
                    _parse_year(vehicle.get("registrationDate")))

            # Localisation
            loc = ad.get("location") or {}
            location = None
            if isinstance(loc, dict):
                city = loc.get("city") or ""
                zip_code = str(loc.get("zip") or loc.get("postalCode") or "")
                location = f"{city} ({zip_code[:5]})" if city else None

            # Image
            images = ad.get("images") or ad.get("photos") or []
            image_url = None
            if isinstance(images, list) and images:
                i0 = images[0]
                image_url = (i0.get("url") or i0.get("src") or i0.get("uri")) if isinstance(i0, dict) else i0

            # URL
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
            traceback.print_exc()
            continue

    if not listings:
        return _parse_autoscout24_html(html)

    return listings


def _parse_autoscout24_html(html: str) -> List[Listing]:
    listings = []
    soup = BeautifulSoup(html, "lxml")
    id_pat = re.compile(r"/off(?:res|ers)/([0-9a-f\-]{30,})", re.IGNORECASE)
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
        for _ in range(6):
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
            title=clean_text(a.get_text(" ", strip=True))[:200] or "AutoScout24",
            price=extract_price(block_text),
            year=extract_year(block_text),
            mileage=extract_mileage(block_text),
            location=None,
            url=f"https://www.autoscout24.fr/offres/{listing_id}",
        ))
    return listings


# ---------------------------------------------------------------------------
# LaCentrale — __NEXT_DATA__
# ---------------------------------------------------------------------------
def _parse_lacentrale(html: str, source_url: str) -> List[Listing]:
    listings: List[Listing] = []
    data = _extract_next_data(html)

    if data:
        page_props = _safe_get(data, "props", "pageProps") or {}
        vehicles = (
            page_props.get("vehicles") or page_props.get("listings") or
            _safe_get(page_props, "searchResult", "vehicles") or
            _safe_get(page_props, "searchResult", "listings") or
            page_props.get("ads") or []
        )

        for v in vehicles:
            try:
                listing_id = str(v.get("id") or v.get("listingId") or v.get("adId") or "")
                if not listing_id:
                    continue

                make = v.get("make") or v.get("brand") or ""
                model = v.get("model") or v.get("modelLabel") or ""
                version = v.get("version") or v.get("versionLabel") or ""
                title = " ".join(filter(None, [str(make), str(model), str(version)])).strip() or "LaCentrale"

                price_raw = v.get("price")
                if isinstance(price_raw, dict):
                    price_raw = price_raw.get("value") or price_raw.get("amount")
                price = _parse_price(price_raw)

                km = _parse_km(v.get("mileage") or v.get("km"))
                year = _parse_year(
                    v.get("year") or v.get("firstRegistrationDate") or v.get("registrationDate")
                )

                loc = v.get("location") or v.get("localisation") or {}
                location = None
                if isinstance(loc, dict):
                    city = loc.get("city") or loc.get("commune") or ""
                    dept = str(loc.get("zipCode") or loc.get("codePostal") or loc.get("departmentCode") or "")
                    location = f"{city} ({dept[:2]})" if city and dept else city or None

                photos = v.get("photos") or v.get("images") or []
                image_url = None
                if isinstance(photos, list) and photos:
                    p0 = photos[0]
                    image_url = (p0.get("url") or p0.get("src")) if isinstance(p0, dict) else p0

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
            title=clean_text(a.get_text(" ", strip=True))[:200] or "LaCentrale",
            price=extract_price(block_text),
            year=extract_year(block_text),
            mileage=extract_mileage(block_text),
            location=None,
            url=f"https://www.lacentrale.fr/auto-occasion-annonce-{listing_id}.html",
        ))
    return listings


# ---------------------------------------------------------------------------
# Dispatcher HTML
# ---------------------------------------------------------------------------
SITE_PARSERS = {
    "lacentrale": _parse_lacentrale,
    "autoscout24": _parse_autoscout24,
}


def _fetch_html(session: requests.Session, entry: dict, retries: int = 2) -> List[Listing]:
    site = entry["site"]
    url = entry["url"]
    label = entry.get("label", url)
    parser = SITE_PARSERS[site]

    for attempt in range(retries + 1):
        try:
            r = session.get(url, timeout=25, headers=HEADERS_HTML, allow_redirects=True)
            if r.status_code == 200:
                listings = parser(r.text, url)
                print(f"  [{label}] HTTP 200 — {len(listings)} annonces")
                return listings
            elif r.status_code in (403, 429):
                wait = 5 * (attempt + 1)
                print(f"  [{label}] HTTP {r.status_code} bloqué (tentative {attempt+1})")
                if attempt < retries:
                    time.sleep(wait)
            else:
                print(f"  [{label}] HTTP {r.status_code}")
                return []
        except requests.RequestException as e:
            print(f"  [{label}] Erreur réseau: {e}")
            if attempt < retries:
                time.sleep(3)

    return []


# ---------------------------------------------------------------------------
# Point d'entrée public
# ---------------------------------------------------------------------------
def fetch_listings(urls: Optional[List[dict]] = None) -> List[Listing]:
    session = requests.Session()
    all_listings: List[Listing] = []

    # 1. LeBonCoin via API JSON interne
    print("[scraper] LeBonCoin API...")
    for search in LBC_SEARCHES:
        try:
            listings = _fetch_leboncoin_api(session, search["text"], search["label"])
            all_listings.extend(listings)
        except Exception as e:
            print(f"  [{search['label']}] Exception: {e}")
        time.sleep(1.0)

    # 2. AutoScout24 + LaCentrale via HTML
    targets = urls or SEARCH_URLS
    print(f"[scraper] HTML sources ({len(targets)})...")
    for entry in targets:
        try:
            listings = _fetch_html(session, entry)
            all_listings.extend(listings)
        except Exception as e:
            print(f"  [{entry.get('label')}] Exception: {e}")
            traceback.print_exc()
        time.sleep(1.5)

    # Dédup
    seen_uids: dict = {}
    for l in all_listings:
        if l.uid not in seen_uids:
            seen_uids[l.uid] = l

    result = list(seen_uids.values())
    print(f"[scraper] Total : {len(result)} annonces uniques")
    return result
