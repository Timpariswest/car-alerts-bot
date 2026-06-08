"""
Scraper multi-sources : LeBonCoin (curl_cffi Chrome impers.), AutoScout24,
LaCentrale (curl_cffi), OuestFrance-Auto, Argus, ParuVendu.

curl_cffi impersonne les fingerprints TLS/HTTP2 de Chrome → contourne Cloudflare.
"""
from __future__ import annotations

import json
import re
import time
import traceback
from typing import List, Optional

import requests
from bs4 import BeautifulSoup

import os
import urllib3
from urllib.parse import quote as _url_quote

# Supprime les avertissements SSL liés au proxy Bright Data (verify=False intentionnel)
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

try:
    from curl_cffi import requests as cf_requests
    HAS_CURL_CFFI = True
    print("[scraper] curl_cffi disponible")
except ImportError:
    HAS_CURL_CFFI = False
    print("[scraper] curl_cffi non disponible — fallback requests standard")

# Bright Data proxy résidentiel (env vars injectées par GitHub Actions secrets)
_BRD_HOST = os.getenv("BRD_HOST", "")
_BRD_PORT = os.getenv("BRD_PORT", "22225")
_BRD_USER = os.getenv("BRD_USER", "")
_BRD_PASS = os.getenv("BRD_PASS", "")
HAS_PROXY = bool(_BRD_HOST and _BRD_USER and _BRD_PASS)

if HAS_PROXY:
    # URL-encode user/pass pour éviter que les caractères spéciaux (@ / + =) cassent l'URL
    _user_enc = _url_quote(_BRD_USER, safe="")
    _pass_enc = _url_quote(_BRD_PASS, safe="")
    _PROXY_URL = f"http://{_user_enc}:{_pass_enc}@{_BRD_HOST}:{_BRD_PORT}"
    _PROXIES = {"http": _PROXY_URL, "https": _PROXY_URL}
    print(f"[scraper] Proxy Bright Data configuré ({_BRD_HOST}:{_BRD_PORT}, user={_BRD_USER[:12]}...)")
else:
    _PROXIES = {}
    print("[scraper] Pas de proxy configuré")

from models import Listing
from parsers.common import extract_price, extract_year, extract_mileage, clean_text


# ---------------------------------------------------------------------------
# Headers
# ---------------------------------------------------------------------------
UA_CHROME = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"

HEADERS_HTML = {
    "User-Agent": UA_CHROME,
    "Accept-Language": "fr-FR,fr;q=0.9",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Encoding": "gzip, deflate, br",
    "Connection": "keep-alive",
    "Sec-Fetch-Dest": "document",
    "Sec-Fetch-Mode": "navigate",
    "Sec-Fetch-Site": "none",
    "Sec-CH-UA": '"Chromium";v="124", "Google Chrome";v="124"',
    "Sec-CH-UA-Mobile": "?0",
    "Sec-CH-UA-Platform": '"macOS"',
}


# ---------------------------------------------------------------------------
# Session CF (curl_cffi ou fallback requests)
# ---------------------------------------------------------------------------
def _make_cf_session():
    """
    Retourne une session pour les requêtes avec proxy.
    curl_cffi avec Chrome impersonation ne gère pas bien le CONNECT tunnel Bright Data
    → on utilise requests standard pour les sessions proxy.
    """
    s = requests.Session()
    if HAS_PROXY:
        s.proxies.update(_PROXIES)
        s.verify = False  # Bright Data fait interception SSL
    if HAS_CURL_CFFI and not HAS_PROXY:
        # Sans proxy, curl_cffi est utile pour bypass Cloudflare
        cf_s = cf_requests.Session(impersonate="chrome124")
        return cf_s, True
    return s, False


def _make_direct_session():
    """Session sans proxy pour AS24 (pas bloqué)."""
    if HAS_CURL_CFFI:
        return cf_requests.Session(impersonate="chrome124"), True
    return requests.Session(), False


def _cf_get(session, url: str, *, is_cf: bool, timeout: int = 30, retries: int = 2, verify: bool = True):
    """GET avec retry. Retourne un objet response ou None."""
    for attempt in range(retries + 1):
        try:
            if is_cf:
                r = session.get(url, headers=HEADERS_HTML, timeout=timeout, verify=verify)
            else:
                r = session.get(url, headers=HEADERS_HTML, timeout=timeout)
            print(f"    [HTTP {r.status_code}] {url[:80]}")
            if r.status_code == 200:
                return r
            if r.status_code in (403, 429, 503):
                print(f"    [CF bloqué?] {r.status_code} — tentative {attempt+1}/{retries+1}")
                if attempt < retries:
                    time.sleep(5 * (attempt + 1))
        except Exception as e:
            print(f"    [Erreur réseau] {e} (tentative {attempt+1})")
            if attempt < retries:
                time.sleep(3)
    return None


def _cf_post(session, url: str, json_payload: dict, headers: dict, *, is_cf: bool, timeout: int = 30, verify: bool = True):
    """POST JSON avec curl_cffi ou requests."""
    try:
        if is_cf:
            r = session.post(url, json=json_payload, headers=headers, timeout=timeout, verify=verify)
        else:
            # requests : ne pas passer verify en arg, utiliser la config de la session
            r = session.post(url, json=json_payload, headers=headers, timeout=timeout)
        return r
    except Exception as e:
        print(f"    [Erreur POST] {e}")
        return None


# ---------------------------------------------------------------------------
# Utilitaires JSON
# ---------------------------------------------------------------------------
def _extract_next_data(html: str) -> Optional[dict]:
    m = re.search(r'<script[^>]+id=["\']__NEXT_DATA__["\'][^>]*>(.*?)</script>', html, re.DOTALL)
    if not m:
        return None
    try:
        return json.loads(m.group(1))
    except Exception:
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
        return v if 100 < v < 100_000 else None
    if isinstance(val, list) and val:
        return _parse_price(val[0])
    if isinstance(val, str):
        digits = re.sub(r"[^\d]", "", val)
        v = int(digits) if digits else None
        return v if v and 100 < v < 100_000 else None
    return None


def _parse_km(val) -> Optional[int]:
    if val is None:
        return None
    if isinstance(val, (int, float)):
        v = int(val)
        return v if 0 < v < 1_000_000 else None
    if isinstance(val, str):
        digits = re.sub(r"[^\d]", "", val)
        v = int(digits) if digits else None
        return v if v and 0 < v < 1_000_000 else None
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
# LeBonCoin — API interne (clé publique) via curl_cffi
# ---------------------------------------------------------------------------
def _fetch_lbc(proxy_session=None, proxy_is_cf: bool = False, direct_session=None, direct_is_cf: bool = False) -> List[Listing]:
    """
    Scrape LeBonCoin via API JSON interne.
    Stratégie : essai direct (curl_cffi Chrome) → si échec et proxy dispo, retry via proxy.
    Fallback HTML si API entièrement bloquée.
    """
    url_api = "https://api.leboncoin.fr/api/adfinder/v1/search"

    # LBC a plusieurs clés API selon les déploiements — on les essaie toutes
    API_KEYS = [
        "ba0c2dad52b3565fd92a81af2b6386d7",  # clé historique
        "9a9d5cf76f55c6fb2f31c8c23e8e5e55",  # clé alternative
    ]

    def _make_headers(api_key):
        return {
            "User-Agent": UA_CHROME,
            "Accept": "application/json, text/plain, */*",
            "Accept-Language": "fr-FR,fr;q=0.9",
            "Content-Type": "application/json",
            "api_key": api_key,
            "Origin": "https://www.leboncoin.fr",
            "Referer": "https://www.leboncoin.fr/voitures/occasions/",
            "X-Source-Type": "web",
        }

    searches = [("renault clio 3", "clio3"), ("renault clio iii", "clio3"), ("peugeot 207", "207")]
    all_listings: List[Listing] = []
    api_total = 0

    # Sessions à essayer : direct d'abord, proxy en secours
    sessions_to_try = []
    if direct_session:
        sessions_to_try.append((direct_session, direct_is_cf, True, "direct"))   # verify=True
    if proxy_session and HAS_PROXY:
        sessions_to_try.append((proxy_session, proxy_is_cf, False, "proxy"))      # verify=False (BRD SSL interception)

    print("[scraper] LeBonCoin API (essai direct puis proxy)...")
    for text, label in searches:
        payload = {
            "limit": 35,
            "filters": {
                "category": {"id": "2"},
                "keywords": {"text": text, "type": "all"},
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
        success = False
        for sess, is_cf, verify, sess_label in sessions_to_try:
            for api_key in API_KEYS:
                r = _cf_post(sess, url_api, payload, _make_headers(api_key), is_cf=is_cf, verify=verify)
                if r is not None and r.status_code == 200:
                    try:
                        data = r.json()
                        lst = _parse_lbc_json(data)
                        print(f"  [LBC API '{text}' via {sess_label}] {len(lst)} annonces (clé={api_key[:8]}...)")
                        all_listings.extend(lst)
                        api_total += len(lst)
                        success = True
                        break
                    except Exception as e:
                        print(f"  [LBC API '{text}' via {sess_label}] parse error: {e}")
                elif r is not None:
                    print(f"  [LBC API '{text}' via {sess_label}] HTTP {r.status_code} (clé={api_key[:8]}...)")
                else:
                    print(f"  [LBC API '{text}' via {sess_label}] Pas de réponse (exception)")
            if success:
                break
            time.sleep(1)
        time.sleep(1.5)

    if api_total > 0:
        return all_listings

    # ── Fallback HTML ─────────────────────────────────────────────────────────
    print("[scraper] LBC API = 0 → fallback HTML (CF bypass)...")
    for sess, is_cf, verify, sess_label in sessions_to_try:
        # Warmup cookie LBC
        _cf_get(sess, "https://www.leboncoin.fr/", is_cf=is_cf, retries=0, verify=verify)
        time.sleep(2)
        for text, _ in searches:
            url = (
                f"https://www.leboncoin.fr/voitures/occasions/recherche"
                f"?text={requests.utils.quote(text)}&price_max=3000&mileage_max=260000&sort=time&order=desc"
            )
            r = _cf_get(sess, url, is_cf=is_cf, verify=verify)
            if r:
                lst = _parse_lbc_next_data(r.text)
                print(f"  [LBC HTML '{text}' via {sess_label}] {len(lst)} annonces")
                all_listings.extend(lst)
            time.sleep(2)
        if all_listings:
            break  # une session a suffi

    return all_listings


def _parse_lbc_json(data: dict) -> List[Listing]:
    ads = data.get("ads") or []
    listings = []
    for ad in ads:
        try:
            listing_id = str(ad.get("list_id", ""))
            if not listing_id:
                continue
            title = ad.get("subject", "") or "LeBonCoin"
            price = _parse_price(ad.get("price"))
            if price is None and isinstance(ad.get("price"), list):
                price = _parse_price(ad["price"][0] if ad["price"] else None)
            attrs = ad.get("attributes", [])
            km, year = None, None
            if isinstance(attrs, list):
                attr_map = {a.get("key"): a.get("value") for a in attrs if isinstance(a, dict)}
                km = _parse_km(attr_map.get("mileage"))
                year = _parse_year(attr_map.get("regdate") or attr_map.get("year"))
            loc = ad.get("location") or {}
            city = loc.get("city", "") if isinstance(loc, dict) else ""
            dept = str(loc.get("department_id", "")) if isinstance(loc, dict) else ""
            location = f"{city} ({dept})" if city and dept else city or None
            images = ad.get("images") or {}
            image_url = None
            if isinstance(images, dict):
                image_url = images.get("thumb_url") or images.get("small_url")
                if not image_url and images.get("urls"):
                    image_url = images["urls"][0] if images["urls"] else None
            listings.append(Listing(
                site="leboncoin", listing_id=listing_id,
                title=clean_text(title)[:200], price=price, year=year,
                mileage=km, location=location,
                url=f"https://www.leboncoin.fr/ad/voitures/{listing_id}",
                image_url=image_url,
                description=clean_text(ad.get("body", title))[:300],
            ))
        except Exception:
            continue
    return listings


def _parse_lbc_next_data(html: str) -> List[Listing]:
    data = _extract_next_data(html)
    if not data:
        return []
    ads = (
        _safe_get(data, "props", "pageProps", "searchData", "ads")
        or _safe_get(data, "props", "pageProps", "initialData", "ads")
        or _safe_get(data, "props", "pageProps", "ads")
        or []
    )
    return _parse_lbc_json({"ads": ads})


# ---------------------------------------------------------------------------
# AutoScout24
# ---------------------------------------------------------------------------
def _parse_autoscout24(html: str, source_url: str) -> List[Listing]:
    listings: List[Listing] = []
    data = _extract_next_data(html)
    if not data:
        return _parse_autoscout24_html(html)

    page_props = _safe_get(data, "props", "pageProps") or {}
    ads = (
        page_props.get("listings")
        or page_props.get("ads")
        or _safe_get(page_props, "searchResponse", "listings")
        or _safe_get(page_props, "searchResponse", "ads")
        or []
    )

    print(f"    [AS24] {len(ads)} annonces dans __NEXT_DATA__")
    if ads:
        ad0 = ads[0]
        print(f"    [AS24 debug] clés={list(ad0.keys())[:10]} | price={ad0.get('price')} | prices={ad0.get('prices')}")

    for ad in ads:
        try:
            listing_id = str(ad.get("id") or ad.get("guid") or "")
            if not listing_id:
                continue

            vehicle = ad.get("vehicle") or {}
            tracking = ad.get("tracking") or {}
            make = vehicle.get("make") or tracking.get("make") or ad.get("make") or ""
            model = vehicle.get("model") or tracking.get("model") or ad.get("model") or ""
            version = vehicle.get("version") or vehicle.get("modelVersion") or ""
            title = ad.get("title") or " ".join(filter(None, [str(make), str(model), str(version)])).strip() or "AutoScout24"

            price = None
            for p_val in [
                _safe_get(ad, "prices", "public", "priceRaw"),
                _safe_get(ad, "prices", "public", "price"),
                _safe_get(ad, "price", "priceFormatted"),   # nouveau format AS24
                _safe_get(ad, "price", "value"),
                _safe_get(ad, "price", "amount"),
                _safe_get(ad, "price", "priceRaw"),
                ad.get("priceRaw"), ad.get("priceValue"),
                vehicle.get("price"),
            ]:
                price = _parse_price(p_val)
                if price:
                    break

            km = None
            for km_val in [vehicle.get("mileage"), vehicle.get("km"), ad.get("mileage"), ad.get("km"), tracking.get("mileage")]:
                km = _parse_km(km_val)
                if km:
                    break

            year = None
            for y_val in [
                vehicle.get("firstRegistrationYear"),
                vehicle.get("firstRegistration"),
                ad.get("firstRegistration"), ad.get("year"),
                vehicle.get("registrationDate"),
            ]:
                year = _parse_year(y_val)
                if year:
                    break

            loc = ad.get("location") or {}
            location = None
            if isinstance(loc, dict):
                city = loc.get("city") or ""
                zip_code = str(loc.get("zip") or loc.get("postalCode") or "")
                location = f"{city} ({zip_code[:5]})" if city else None

            images = ad.get("images") or ad.get("photos") or []
            image_url = None
            if isinstance(images, list) and images:
                i0 = images[0]
                image_url = (i0.get("url") or i0.get("src") or i0.get("uri")) if isinstance(i0, dict) else i0

            ad_url = ad.get("url") or f"https://www.autoscout24.fr/offres/{listing_id}"
            if not str(ad_url).startswith("http"):
                ad_url = "https://www.autoscout24.fr" + str(ad_url)

            listings.append(Listing(
                site="autoscout24", listing_id=listing_id,
                title=clean_text(str(title))[:200], price=price,
                year=year, mileage=km, location=location,
                url=ad_url, image_url=image_url,
                description=clean_text(str(title)),
            ))
        except Exception:
            continue

    return listings or _parse_autoscout24_html(html)


def _parse_autoscout24_html(html: str) -> List[Listing]:
    listings = []
    soup = BeautifulSoup(html, "lxml")
    id_pat = re.compile(r"/off(?:res|ers)/([0-9a-f\-]{30,})", re.IGNORECASE)
    seen = set()
    for a in soup.find_all("a", href=True):
        m = id_pat.search(a["href"])
        if not m or m.group(1) in seen:
            continue
        seen.add(m.group(1))
        block = a
        for _ in range(6):
            p = block.parent
            if not p or len(p.get_text(strip=True)) > 30:
                block = p or block
                break
            block = p
        bt = clean_text(block.get_text(" ", strip=True))
        listings.append(Listing(
            site="autoscout24", listing_id=m.group(1),
            title=clean_text(a.get_text(" ", strip=True))[:200] or "AutoScout24",
            price=extract_price(bt), year=extract_year(bt), mileage=extract_mileage(bt),
            location=None, url=f"https://www.autoscout24.fr/offres/{m.group(1)}",
        ))
    return listings


# ---------------------------------------------------------------------------
# LaCentrale — via curl_cffi
# ---------------------------------------------------------------------------
def _parse_lacentrale(html: str, source_url: str) -> List[Listing]:
    listings: List[Listing] = []
    data = _extract_next_data(html)
    if data:
        page_props = _safe_get(data, "props", "pageProps") or {}
        # Debug : affiche les clés disponibles pour diagnostiquer les changements de structure
        print(f"    [LaCentrale] __NEXT_DATA__ pageProps clés={list(page_props.keys())[:15]}")
        # Tente dehydratedState (React Query / TanStack Query)
        dehydrated = page_props.get("dehydratedState") or {}
        queries = dehydrated.get("queries") or []
        dehydrated_ads = []
        for q in queries:
            qdata = _safe_get(q, "state", "data") or {}
            for key in ("classifiedAds", "vehicles", "listings", "ads", "results"):
                if isinstance(qdata.get(key), list) and qdata[key]:
                    dehydrated_ads = qdata[key]
                    print(f"    [LaCentrale] dehydratedState → clé='{key}' {len(dehydrated_ads)} annonces")
                    break
            if dehydrated_ads:
                break
        vehicles = (
            dehydrated_ads
            or page_props.get("classifiedAds")
            or page_props.get("vehicles") or page_props.get("listings")
            or _safe_get(page_props, "searchResult", "classifiedAds")
            or _safe_get(page_props, "searchResult", "vehicles")
            or _safe_get(page_props, "searchResult", "listings")
            or _safe_get(page_props, "initialData", "classifiedAds")
            or _safe_get(page_props, "initialData", "vehicles")
            or page_props.get("ads") or []
        )
        if not vehicles:
            print(f"    [LaCentrale] 0 annonces dans __NEXT_DATA__ — structure inconnue, clés={list(page_props.keys())}")
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
                year = _parse_year(v.get("year") or v.get("firstRegistrationDate") or v.get("registrationDate"))
                loc = v.get("location") or v.get("localisation") or {}
                location = None
                if isinstance(loc, dict):
                    city = loc.get("city") or loc.get("commune") or ""
                    dept = str(loc.get("zipCode") or loc.get("codePostal") or "")
                    location = f"{city} ({dept[:2]})" if city and dept else city or None
                photos = v.get("photos") or v.get("images") or []
                image_url = None
                if isinstance(photos, list) and photos:
                    p0 = photos[0]
                    image_url = (p0.get("url") or p0.get("src")) if isinstance(p0, dict) else p0
                listings.append(Listing(
                    site="lacentrale", listing_id=listing_id,
                    title=clean_text(title)[:200], price=price, year=year,
                    mileage=km, location=location,
                    url=f"https://www.lacentrale.fr/auto-occasion-annonce-{listing_id}.html",
                    image_url=image_url, description=clean_text(title),
                ))
            except Exception:
                continue

    return listings or _parse_lacentrale_html(html)


def _parse_lacentrale_html(html: str) -> List[Listing]:
    listings = []
    soup = BeautifulSoup(html, "lxml")
    # Plusieurs patterns d'URL LaCentrale (ils changent parfois)
    id_patterns = [
        re.compile(r"auto-occasion-annonce-([A-Z0-9]+)\.html", re.IGNORECASE),
        re.compile(r"lacentrale\.fr/(?:listing|annonce)/([A-Z0-9\-]+)", re.IGNORECASE),
        re.compile(r"/(?:listing|annonce)/([A-Z0-9]{8,})", re.IGNORECASE),
    ]
    seen = set()
    # Log pour diagnostic
    all_hrefs = [a["href"] for a in soup.find_all("a", href=True) if "lacentrale" in a["href"].lower() or "/listing/" in a["href"].lower() or "annonce" in a["href"].lower()]
    if all_hrefs:
        print(f"    [LaCentrale HTML] exemples d'URLs trouvées : {all_hrefs[:3]}")
    else:
        # Compter les liens et la taille HTML pour diagnostic
        total_links = len(soup.find_all("a", href=True))
        print(f"    [LaCentrale HTML] Aucun lien lacentrale trouvé. Total liens={total_links}, HTML={len(html)} chars")
    for a in soup.find_all("a", href=True):
        listing_id = None
        for pat in id_patterns:
            m = pat.search(a["href"])
            if m:
                listing_id = m.group(1)
                break
        if not listing_id or listing_id in seen:
            continue
        seen.add(listing_id)
        block = a
        for _ in range(5):
            p = block.parent
            if not p or len(p.get_text(strip=True)) > 30:
                block = p or block
                break
            block = p
        bt = clean_text(block.get_text(" ", strip=True))
        listings.append(Listing(
            site="lacentrale", listing_id=listing_id,
            title=clean_text(a.get_text(" ", strip=True))[:200] or "LaCentrale",
            price=extract_price(bt), year=extract_year(bt), mileage=extract_mileage(bt),
            location=None, url=f"https://www.lacentrale.fr/auto-occasion-annonce-{listing_id}.html",
        ))
    if listings:
        print(f"    [LaCentrale HTML fallback] {len(listings)} annonces trouvées")
    return listings


# ---------------------------------------------------------------------------
# OuestFrance-Auto — grand site régional, hors Cloudflare
# ---------------------------------------------------------------------------
def _parse_ouestfrance(html: str, source_url: str) -> List[Listing]:
    listings = []
    soup = BeautifulSoup(html, "lxml")

    # Structure OuestFrance-Auto: articles avec data-id ou liens /annonces/detail/...
    id_pat = re.compile(r"/annonces/(?:detail|fiche)/[^/?#]*[/-](\d{6,})", re.IGNORECASE)
    id_pat2 = re.compile(r"[?&]id=(\d{5,})")
    seen = set()

    for a in soup.find_all("a", href=True):
        href = a["href"]
        m = id_pat.search(href) or id_pat2.search(href)
        if not m:
            continue
        lid = m.group(1)
        if lid in seen:
            continue
        seen.add(lid)

        # Remonter pour trouver le bloc annonce
        block = a
        for _ in range(7):
            p = block.parent
            if not p:
                break
            txt = p.get_text(strip=True)
            if len(txt) > 50:
                block = p
                break
            block = p

        bt = clean_text(block.get_text(" ", strip=True))
        full_url = href if href.startswith("http") else "https://www.ouestfrance-auto.com" + href
        title = clean_text(a.get_text(" ", strip=True))[:200] or bt[:80] or "OuestFrance"
        listings.append(Listing(
            site="ouestfrance", listing_id=lid,
            title=title,
            price=extract_price(bt), year=extract_year(bt), mileage=extract_mileage(bt),
            location=None, url=full_url,
        ))

    # Fallback : chercher JSON embarqué (Next.js ou autre)
    if not listings:
        data = _extract_next_data(html)
        if data:
            page_props = _safe_get(data, "props", "pageProps") or {}
            ads = (
                page_props.get("ads") or page_props.get("listings")
                or page_props.get("vehicles") or []
            )
            for ad in ads:
                try:
                    lid = str(ad.get("id") or ad.get("adId") or "")
                    if not lid or lid in seen:
                        continue
                    seen.add(lid)
                    title = ad.get("title") or ad.get("subject") or "OuestFrance"
                    listings.append(Listing(
                        site="ouestfrance", listing_id=lid,
                        title=clean_text(str(title))[:200],
                        price=_parse_price(ad.get("price")),
                        year=_parse_year(ad.get("year") or ad.get("firstRegistration")),
                        mileage=_parse_km(ad.get("mileage") or ad.get("km")),
                        location=None,
                        url=f"https://www.ouestfrance-auto.com/annonces/detail/{lid}",
                    ))
                except Exception:
                    continue

    return listings


# ---------------------------------------------------------------------------
# Argus (L'Argus occasions)
# ---------------------------------------------------------------------------
def _parse_argus(html: str, source_url: str) -> List[Listing]:
    listings = []
    soup = BeautifulSoup(html, "lxml")
    id_pat = re.compile(r"/voiture-occasion/vente/[^/]+/(\d{6,})", re.IGNORECASE)
    seen = set()
    for a in soup.find_all("a", href=True):
        m = id_pat.search(a["href"])
        if not m or m.group(1) in seen:
            continue
        seen.add(m.group(1))
        block = a
        for _ in range(6):
            p = block.parent
            if not p or len(p.get_text(strip=True)) > 30:
                block = p or block
                break
            block = p
        bt = clean_text(block.get_text(" ", strip=True))
        full_url = a["href"] if a["href"].startswith("http") else "https://www.largus.fr" + a["href"]
        listings.append(Listing(
            site="argus", listing_id=m.group(1),
            title=clean_text(a.get_text(" ", strip=True))[:200] or "Argus",
            price=extract_price(bt), year=extract_year(bt), mileage=extract_mileage(bt),
            location=None, url=full_url,
        ))
    return listings


# ---------------------------------------------------------------------------
# ParuVendu
# ---------------------------------------------------------------------------
def _parse_paruvendu(html: str, source_url: str) -> List[Listing]:
    listings = []
    soup = BeautifulSoup(html, "lxml")
    id_pat = re.compile(r"annonce-(\d{6,})", re.IGNORECASE)
    seen = set()
    for a in soup.find_all("a", href=True):
        href = a["href"]
        if "annonce" not in href.lower():
            continue
        m = id_pat.search(href)
        if not m or m.group(1) in seen:
            continue
        seen.add(m.group(1))
        block = a
        for _ in range(6):
            p = block.parent
            if not p or len(p.get_text(strip=True)) > 30:
                block = p or block
                break
            block = p
        bt = clean_text(block.get_text(" ", strip=True))
        full_url = href if href.startswith("http") else "https://www.paruvendu.fr" + href
        listings.append(Listing(
            site="paruvendu", listing_id=m.group(1),
            title=clean_text(a.get_text(" ", strip=True))[:200] or "ParuVendu",
            price=extract_price(bt), year=extract_year(bt), mileage=extract_mileage(bt),
            location=None, url=full_url,
        ))
    return listings


# ---------------------------------------------------------------------------
# Config sources HTML (hors LBC qui a son propre pipeline)
# ---------------------------------------------------------------------------
SITE_PARSERS = {
    "autoscout24": _parse_autoscout24,
    "lacentrale": _parse_lacentrale,
    "ouestfrance": _parse_ouestfrance,
    "argus": _parse_argus,
    "paruvendu": _parse_paruvendu,
}

HTML_SOURCES = [
    # AutoScout24 — sans proxy (pas bloqué)
    {"site": "autoscout24", "label": "AS24 Clio", "proxy": False,
     "url": "https://www.autoscout24.fr/lst/renault/clio?mmvco=1&ustate=N%2CU&sort=age&desc=1&milemax=260000&priceto=3000&cy=F&atype=C"},
    {"site": "autoscout24", "label": "AS24 207", "proxy": False,
     "url": "https://www.autoscout24.fr/lst/peugeot/207?mmvco=1&ustate=N%2CU&sort=age&desc=1&milemax=260000&priceto=3000&cy=F&atype=C"},
    # LaCentrale — proxy Bright Data requis
    {"site": "lacentrale", "label": "LaCentrale Clio 3", "proxy": True,
     "url": "https://www.lacentrale.fr/listing?makesModelsCommercialNames=RENAULT%3ACLIO+3&mileageMax=260000&priceMax=3000"},
    {"site": "lacentrale", "label": "LaCentrale 207", "proxy": True,
     "url": "https://www.lacentrale.fr/listing?makesModelsCommercialNames=PEUGEOT%3A207&mileageMax=260000&priceMax=3000"},
]


def _fetch_html_source(proxy_session, proxy_is_cf: bool, direct_session, direct_is_cf: bool, entry: dict) -> List[Listing]:
    site = entry["site"]
    url = entry["url"]
    label = entry.get("label", url)
    parser = SITE_PARSERS[site]
    use_proxy = entry.get("proxy", False) and HAS_PROXY

    session = proxy_session if use_proxy else direct_session
    is_cf = proxy_is_cf if use_proxy else direct_is_cf
    verify = not use_proxy  # Bright Data fait interception SSL → désactiver verify

    r = _cf_get(session, url, is_cf=is_cf, verify=verify)
    if r:
        print(f"  [{label}] HTTP {r.status_code}, HTML={len(r.text)} chars, proxy={'oui' if use_proxy else 'non'}")
        listings = parser(r.text, url)
        print(f"  [{label}] {len(listings)} annonces parsées")
        return listings
    print(f"  [{label}] ÉCHEC requête (pas de réponse), proxy={'oui' if use_proxy else 'non'}")
    return []


# ---------------------------------------------------------------------------
# Point d'entrée public
# ---------------------------------------------------------------------------
def fetch_listings(sources: Optional[List[dict]] = None) -> List[Listing]:
    proxy_session, proxy_is_cf = _make_cf_session()    # avec proxy Bright Data
    direct_session, direct_is_cf = _make_direct_session()  # sans proxy (AS24)
    all_listings: List[Listing] = []

    # 1. LeBonCoin — direct d'abord, proxy en secours si dispo
    print("[scraper] LeBonCoin...")
    try:
        lbc = _fetch_lbc(
            proxy_session=proxy_session, proxy_is_cf=proxy_is_cf,
            direct_session=direct_session, direct_is_cf=direct_is_cf,
        )
        all_listings.extend(lbc)
        print(f"[scraper] LBC total: {len(lbc)} annonces")
    except Exception as e:
        print(f"[scraper] LBC erreur: {e}")
        traceback.print_exc()

    # 2. Sources HTML
    src_list = sources or HTML_SOURCES
    print(f"[scraper] Sources HTML ({len(src_list)})...")
    for entry in src_list:
        try:
            lst = _fetch_html_source(proxy_session, proxy_is_cf, direct_session, direct_is_cf, entry)
            all_listings.extend(lst)
        except Exception as e:
            print(f"  [{entry.get('label')}] Exception: {e}")
        time.sleep(1.5)

    # Dédup
    seen_uids: dict = {}
    for l in all_listings:
        if l.uid not in seen_uids:
            seen_uids[l.uid] = l

    result = list(seen_uids.values())
    print(f"[scraper] Total : {len(result)} annonces uniques")
    return result
