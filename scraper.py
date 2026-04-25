"""
Scraper direct — LeBonCoin (RSS), LaCentrale, AutoScout24.
fetch_listings() retourne une liste de Listing compatibles avec main.py.
"""
from __future__ import annotations

import json
import re
import time
import traceback
import xml.etree.ElementTree as ET
from typing import List, Optional

import requests
from bs4 import BeautifulSoup

from models import Listing
from parsers.common import extract_price, extract_year, extract_mileage, clean_text


# ---------------------------------------------------------------------------
# Headers — simule Chrome macOS
# ---------------------------------------------------------------------------
HEADERS = {
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Accept-Language": "fr-FR,fr;q=0.9,en;q=0.8",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Encoding": "gzip, deflate, br",
    "Connection": "keep-alive",
    "Upgrade-Insecure-Requests": "1",
}

HEADERS_RSS = {
    "User-Agent": "Mozilla/5.0 (compatible; RSS reader)",
    "Accept": "application/rss+xml, application/xml, text/xml, */*",
}

# ---------------------------------------------------------------------------
# URLs de recherche
# ---------------------------------------------------------------------------
SEARCH_URLS = [
    # --- LeBonCoin via RSS (pas bloqué par Cloudflare) ---
    {
        "site": "leboncoin",
        "method": "rss",
        "url": "https://www.leboncoin.fr/recherche/rss?category=2&text=clio+3&price_max=3000&mileage_max=260000",
        "label": "LBC RSS Clio 3",
    },
    {
        "site": "leboncoin",
        "method": "rss",
        "url": "https://www.leboncoin.fr/recherche/rss?category=2&text=clio+iii&price_max=3000&mileage_max=260000",
        "label": "LBC RSS Clio III",
    },
    {
        "site": "leboncoin",
        "method": "rss",
        "url": "https://www.leboncoin.fr/recherche/rss?category=2&text=peugeot+207&price_max=3000&mileage_max=260000",
        "label": "LBC RSS 207",
    },
    # --- AutoScout24 (fonctionne, on corrige juste l'extraction) ---
    {
        "site": "autoscout24",
        "method": "html",
        "url": "https://www.autoscout24.fr/lst/renault/clio?mmvco=1&ustate=N%2CU&sort=age&desc=1&milemax=260000&priceto=3000&cy=F&atype=C",
        "label": "AS24 Clio",
    },
    {
        "site": "autoscout24",
        "method": "html",
        "url": "https://www.autoscout24.fr/lst/peugeot/207?mmvco=1&ustate=N%2CU&sort=age&desc=1&milemax=260000&priceto=3000&cy=F&atype=C",
        "label": "AS24 207",
    },
    # --- LaCentrale ---
    {
        "site": "lacentrale",
        "method": "html",
        "url": "https://www.lacentrale.fr/listing?makesModelsCommercialNames=RENAULT%3ACLIO+3&mileageMax=260000&priceMax=3000",
        "label": "LaCentrale Clio 3",
    },
    {
        "site": "lacentrale",
        "method": "html",
        "url": "https://www.lacentrale.fr/listing?makesModelsCommercialNames=PEUGEOT%3A207&mileageMax=260000&priceMax=3000",
        "label": "LaCentrale 207",
    },
]


# ---------------------------------------------------------------------------
# Session HTTP
# ---------------------------------------------------------------------------
def _make_session() -> requests.Session:
    s = requests.Session()
    s.headers.update(HEADERS)
    return s


# ---------------------------------------------------------------------------
# Utilitaires JSON
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
        return int(val)
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
# Parser LeBonCoin via RSS
# ---------------------------------------------------------------------------
def _parse_leboncoin_rss(xml_text: str) -> List[Listing]:
    """
    Parse le flux RSS LeBonCoin.
    Chaque item : <title>, <link>, <description> (HTML avec détails)
    """
    listings: List[Listing] = []
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError:
        return []

    ns = {"media": "http://search.yahoo.com/mrss/"}
    items = root.findall(".//item")

    for item in items:
        try:
            title_el = item.find("title")
            link_el = item.find("link")
            desc_el = item.find("description")

            if title_el is None or link_el is None:
                continue

            title = (title_el.text or "").strip()
            url = (link_el.text or "").strip()

            # Extraire l'ID depuis l'URL
            m = re.search(r"/(\d{8,})[^/]*$", url)
            if not m:
                # Essai avec le texte de la balise suivante (LBC met l'URL dans le texte après <link/>)
                next_el = list(item)
                for el in next_el:
                    if el.tag == "link" and el.tail:
                        url_candidate = el.tail.strip()
                        m2 = re.search(r"/(\d{8,})[^/]*$", url_candidate)
                        if m2:
                            url = url_candidate
                            m = m2
                            break
            if not m:
                continue

            listing_id = m.group(1)

            # Parser la description HTML
            desc_html = desc_el.text if desc_el is not None else ""
            desc_text = ""
            if desc_html:
                try:
                    soup = BeautifulSoup(desc_html, "lxml")
                    desc_text = clean_text(soup.get_text(" ", strip=True))
                except Exception:
                    desc_text = clean_text(desc_html)

            blob = f"{title} {desc_text}"
            price = extract_price(blob) if blob else None
            km = extract_mileage(blob) if blob else None
            year = extract_year(blob) if blob else None

            # Image depuis media:content
            image_url = None
            media_el = item.find("media:content", ns)
            if media_el is not None:
                image_url = media_el.get("url")

            location = None
            # Cherche ville dans description : ex "Paris (75)"
            loc_m = re.search(r"([A-ZÀ-Ÿa-zà-ÿ\s\-]+)\s*\((\d{2,5})\)", desc_text)
            if loc_m:
                location = f"{loc_m.group(1).strip()} ({loc_m.group(2)[:2]})"

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
                description=desc_text[:500],
            ))
        except Exception:
            continue

    return listings


# ---------------------------------------------------------------------------
# Parser AutoScout24 — structure corrigée
# ---------------------------------------------------------------------------
def _parse_autoscout24(html: str, source_url: str) -> List[Listing]:
    listings: List[Listing] = []
    data = _extract_next_data(html)

    if data:
        page_props = _safe_get(data, "props", "pageProps") or {}

        # Plusieurs chemins possibles selon la version du site
        ads = (
            page_props.get("listings")
            or page_props.get("ads")
            or _safe_get(page_props, "searchResponse", "listings")
            or _safe_get(page_props, "searchResponse", "ads")
            or _safe_get(page_props, "initialState", "listings")
            or []
        )

        if ads:
            print(f"    [AS24 debug] {len(ads)} annonces dans __NEXT_DATA__")
            # Debug: affiche les clés du 1er élément pour diagnostiquer
            if ads:
                print(f"    [AS24 debug] clés du 1er item: {list(ads[0].keys())[:15]}")

        for ad in ads:
            try:
                listing_id = str(ad.get("id") or ad.get("guid") or "")
                if not listing_id:
                    continue

                # --- Titre ---
                vehicle = ad.get("vehicle") or {}
                tracking = ad.get("tracking") or {}
                make = (vehicle.get("make") or tracking.get("make") or
                        ad.get("make") or "")
                model = (vehicle.get("model") or tracking.get("model") or
                         ad.get("model") or "")
                version = vehicle.get("version") or vehicle.get("modelVersion") or ""
                title = (ad.get("title") or
                         " ".join(filter(None, [str(make), str(model), str(version)])).strip() or
                         "Annonce AutoScout24")

                # --- Prix : plusieurs chemins selon la version AS24 ---
                price = None
                # Chemin 1 : prices.public.priceRaw (version récente)
                prices_obj = ad.get("prices") or {}
                if isinstance(prices_obj, dict):
                    pub = prices_obj.get("public") or {}
                    price = _parse_price(pub.get("priceRaw") or pub.get("price"))
                # Chemin 2 : price.value (ancienne version)
                if price is None:
                    price_obj = ad.get("price") or {}
                    if isinstance(price_obj, dict):
                        price = _parse_price(
                            price_obj.get("value") or price_obj.get("amount") or
                            price_obj.get("priceRaw")
                        )
                    else:
                        price = _parse_price(price_obj)
                # Chemin 3 : directement sur l'objet
                if price is None:
                    price = _parse_price(ad.get("priceRaw") or ad.get("priceValue"))

                # --- Kilométrage ---
                km = None
                # Chemin 1 : vehicle.mileage
                km = _parse_km(vehicle.get("mileage") or vehicle.get("km"))
                # Chemin 2 : directement
                if km is None:
                    km = _parse_km(ad.get("mileage") or ad.get("km"))
                # Chemin 3 : tracking
                if km is None:
                    km = _parse_km(tracking.get("mileage"))

                # --- Année ---
                year = None
                # Chemin 1 : vehicle.firstRegistrationYear (int)
                year = _parse_year(vehicle.get("firstRegistrationYear"))
                # Chemin 2 : firstRegistration string "MM/YYYY" ou "YYYY-MM"
                if year is None:
                    year = _parse_year(
                        vehicle.get("firstRegistration") or
                        ad.get("firstRegistration") or
                        ad.get("year") or
                        tracking.get("firstRegistration")
                    )
                # Chemin 3 : registrationDate
                if year is None:
                    year = _parse_year(
                        vehicle.get("registrationDate") or ad.get("registrationDate")
                    )

                # --- Localisation ---
                loc = ad.get("location") or {}
                location = None
                if isinstance(loc, dict):
                    city = (loc.get("city") or loc.get("countryCode") or "")
                    zip_code = str(loc.get("zip") or loc.get("postalCode") or "")
                    location = f"{city} ({zip_code[:5]})" if city else None

                # --- Image ---
                images = ad.get("images") or ad.get("photos") or []
                image_url = None
                if isinstance(images, list) and images:
                    i0 = images[0]
                    if isinstance(i0, dict):
                        image_url = (i0.get("url") or i0.get("src") or
                                     i0.get("thumb") or i0.get("uri"))
                    elif isinstance(i0, str):
                        image_url = i0

                # --- URL ---
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
        listings = _parse_autoscout24_html(html)

    return listings


def _parse_autoscout24_html(html: str) -> List[Listing]:
    """Fallback HTML si __NEXT_DATA__ vide."""
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
            title=clean_text(a.get_text(" ", strip=True))[:200] or "Annonce AutoScout24",
            price=extract_price(block_text),
            year=extract_year(block_text),
            mileage=extract_mileage(block_text),
            location=None,
            url=f"https://www.autoscout24.fr/offres/{listing_id}",
        ))
    return listings


# ---------------------------------------------------------------------------
# Parser LaCentrale
# ---------------------------------------------------------------------------
def _parse_lacentrale(html: str, source_url: str) -> List[Listing]:
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

                photos = v.get("photos") or v.get("images") or []
                image_url = None
                if isinstance(photos, list) and photos:
                    p0 = photos[0]
                    if isinstance(p0, dict):
                        image_url = p0.get("url") or p0.get("src")
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
# Dispatcher
# ---------------------------------------------------------------------------
SITE_PARSERS = {
    "leboncoin": None,       # géré via RSS
    "lacentrale": _parse_lacentrale,
    "autoscout24": _parse_autoscout24,
}


def _fetch_one(session: requests.Session, entry: dict, retries: int = 2) -> List[Listing]:
    site = entry["site"]
    url = entry["url"]
    label = entry.get("label", url)
    method = entry.get("method", "html")

    for attempt in range(retries + 1):
        try:
            if method == "rss":
                r = session.get(url, timeout=20, headers=HEADERS_RSS, allow_redirects=True)
            else:
                r = session.get(url, timeout=25, allow_redirects=True)

            if r.status_code == 200:
                if method == "rss":
                    listings = _parse_leboncoin_rss(r.text)
                else:
                    parser = SITE_PARSERS[site]
                    listings = parser(r.text, url)
                print(f"  [{label}] HTTP 200 — {len(listings)} annonces")
                return listings
            elif r.status_code in (403, 429):
                wait = 5 * (attempt + 1)
                print(f"  [{label}] HTTP {r.status_code} (tentative {attempt+1}) — attente {wait}s")
                if attempt < retries:
                    time.sleep(wait)
            else:
                print(f"  [{label}] HTTP {r.status_code}")
                return []
        except requests.RequestException as e:
            print(f"  [{label}] Erreur réseau (tentative {attempt+1}): {e}")
            if attempt < retries:
                time.sleep(3)

    return []


# ---------------------------------------------------------------------------
# Point d'entrée public
# ---------------------------------------------------------------------------
def fetch_listings(urls: Optional[List[dict]] = None) -> List[Listing]:
    targets = urls or SEARCH_URLS
    session = _make_session()
    all_listings: List[Listing] = []

    print(f"[scraper] Scraping {len(targets)} sources...")
    for entry in targets:
        try:
            listings = _fetch_one(session, entry)
            all_listings.extend(listings)
        except Exception as e:
            print(f"  [{entry.get('label', entry['url'])}] Exception: {e}")
            traceback.print_exc()
        time.sleep(1.5)

    # Dédup par uid
    seen_uids: dict = {}
    for l in all_listings:
        if l.uid not in seen_uids:
            seen_uids[l.uid] = l

    result = list(seen_uids.values())
    print(f"[scraper] Total : {len(result)} annonces uniques")
    return result
