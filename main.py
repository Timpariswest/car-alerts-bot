"""
Bot d'alertes voitures — orchestration principale.

Pipeline (nouveau mode scraping direct) :
  1. Scrape directement LeBonCoin, LaCentrale, AutoScout24
  2. Pour chaque annonce : matche avec une recherche config, applique les filtres durs
  3. Score l'annonce (prix vs cote + keywords + km)
  4. Si score >= seuil et pas déjà vue → notifie Telegram
  5. Marque comme vue et sauvegarde state

Usage :
  python main.py               # run normal
  python main.py --dry-run     # pas d'envoi Telegram, pas de state save
  python main.py --seed        # marque tout ce qu'on voit comme déjà vu (pas d'envoi)
"""
from __future__ import annotations

import argparse
import sys
import traceback
from pathlib import Path

import yaml

from scraper import fetch_listings
from models import Listing, ScoringConfig, SearchConfig
from notifier import TelegramNotifier
from scoring import listing_passes_hard_filters, score_listing
from state import State
from callback_handler import process_callbacks


CONFIG_PATH = Path(__file__).parent / "config.yaml"
STATE_PATH = Path(__file__).parent / "state.json"


def load_config() -> dict:
    with CONFIG_PATH.open("r", encoding="utf-8") as f:
        return yaml.safe_load(f)


def build_searches(cfg: dict) -> list[SearchConfig]:
    return [SearchConfig(**s) for s in cfg.get("searches", [])]


def build_scoring(cfg: dict) -> ScoringConfig:
    return ScoringConfig(**cfg.get("scoring", {}))


def match_listing_to_search(listing: Listing, searches: list[SearchConfig]) -> SearchConfig | None:
    """Trouve la recherche qui correspond à l'annonce (première qui matche keywords_must)."""
    blob = " ".join(filter(None, [listing.title or "", listing.description or ""])).lower()
    for s in searches:
        if not s.keywords_must:
            continue
        if all(k.lower() in blob for k in s.keywords_must):
            return s
    return None


def _send_telegram_error(msg: str) -> None:
    """Envoie une alerte Telegram si les vars d'env sont disponibles. Jamais de crash."""
    import os, requests as _req
    token = os.getenv("TELEGRAM_BOT_TOKEN", "")
    chat_id = os.getenv("TELEGRAM_CHAT_ID", "")
    if not token or not chat_id:
        return
    try:
        _req.post(
            f"https://api.telegram.org/bot{token}/sendMessage",
            json={"chat_id": chat_id, "text": f"🚨 Bot erreur critique :\n{msg[:1000]}", "parse_mode": "HTML"},
            timeout=10,
        )
    except Exception:
        pass


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true", help="Pas d'envoi, pas de save state.")
    ap.add_argument("--seed", action="store_true", help="Marque tout comme vu sans envoyer.")
    args = ap.parse_args()

    try:
        cfg = load_config()
        searches = build_searches(cfg)
        scoring_cfg = build_scoring(cfg)
    except Exception as e:
        # Erreur config = bug réel → on alerte et on échoue pour voir le mail
        msg = f"Impossible de charger la config : {e}"
        print(f"[main] CRITIQUE: {msg}")
        _send_telegram_error(msg)
        return 1

    max_notifs = int(cfg.get("max_notifications_per_run", 10))

    print(f"[main] dry_run={args.dry_run} seed={args.seed} max_notifs={max_notifs}")
    print(f"[main] {len(searches)} recherches configurées")

    # 0. Traitement des callbacks Telegram (non-bloquant — échec ignoré silencieusement)
    state_early = State(str(STATE_PATH))
    if not args.dry_run and not args.seed:
        try:
            process_callbacks(state_early)
            state_early.save()
        except Exception as e:
            print(f"[main] Callbacks ignorés (erreur non-bloquante) : {e}")

    # 1. Scraping direct des sites
    try:
        all_listings = fetch_listings()
    except Exception as e:
        # Erreur scraping = transitoire (CloudFlare, site down…) → pas de mail GitHub
        print(f"[main] Scraping échoué (transitoire) : {e}")
        traceback.print_exc()
        return 0  # ← exit 0 : workflow vert, pas de mail

    print(f"[main] {len(all_listings)} annonces brutes récupérées")
    for l in all_listings[:5]:
        print(f"  [debug] {l.site} | {l.title[:60]} | prix={l.price} | km={l.mileage} | url={l.url[:60]}")

    # Dédup par uid
    dedup: dict[str, Listing] = {}
    for l in all_listings:
        if l.uid not in dedup:
            dedup[l.uid] = l
    all_listings = list(dedup.values())

    # 2-3. Pour chaque annonce : match + filtres durs + score
    state = State(str(STATE_PATH))
    notifier = TelegramNotifier() if not args.dry_run else None

    matched_count = 0
    filtered_count = 0
    candidates: list[Listing] = []
    for l in all_listings:
        s = match_listing_to_search(l, searches)
        if not s:
            continue
        matched_count += 1
        ok, reason = listing_passes_hard_filters(l, s)
        if not ok:
            print(f"  [skip:{l.site}:{l.listing_id}] {reason} | '{l.title[:50]}'")
            filtered_count += 1
            continue
        score, breakdown = score_listing(l, s, scoring_cfg)
        l.score = score
        l.score_breakdown = breakdown
        l.matched_search = s.name
        candidates.append(l)

    print(f"[main] {matched_count} annonces matchées, {filtered_count} rejetées par filtres durs, {len(candidates)} candidates")
    for c in candidates[:5]:
        print(f"  [candidate] {c.site} | {c.title[:50]} | prix={c.price} | km={c.mileage} | score={c.score:+.1f}")

    # Tri par score décroissant
    candidates.sort(key=lambda x: x.score, reverse=True)

    # 4. Filtre seuil + déjà-vu + push
    sent = 0
    for l in candidates:
        if state.is_seen(l.uid):
            continue
        if l.score < scoring_cfg.min_score_to_notify:
            state.mark_seen(l.uid)
            continue

        if args.seed:
            state.mark_seen(l.uid)
            print(f"  [seed] {l.uid} {l.title[:50]} score={l.score:+.1f}")
            continue

        if args.dry_run:
            print(f"  [would-send] {l.uid} {l.title[:50]} score={l.score:+.1f}")
            continue

        if sent >= max_notifs:
            print(f"[main] max_notifs atteint ({max_notifs}), stop.")
            break

        ok = notifier.send(l, state) if notifier else False
        if ok:
            state.mark_seen(l.uid)
            sent += 1
            notifier.throttle(1.0)
        else:
            print(f"  [send-fail] {l.uid}")

    # 5. Save
    if not args.dry_run:
        state.save()

    print(f"[main] Terminé. {sent} notifications envoyées, {len(state.seen)} en mémoire.")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as e:
        # Filet ultime — aucun bug inattendu ne doit faire échouer le workflow
        msg = traceback.format_exc()
        print(f"[main] Exception non gérée :\n{msg}")
        _send_telegram_error(msg)
        sys.exit(0)  # ← toujours vert côté GitHub Actions
