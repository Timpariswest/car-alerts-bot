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


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true", help="Pas d'envoi, pas de save state.")
    ap.add_argument("--seed", action="store_true", help="Marque tout comme vu sans envoyer.")
    args = ap.parse_args()

    cfg = load_config()
    searches = build_searches(cfg)
    scoring_cfg = build_scoring(cfg)
    max_notifs = int(cfg.get("max_notifications_per_run", 10))

    print(f"[main] dry_run={args.dry_run} seed={args.seed} max_notifs={max_notifs}")
    print(f"[main] {len(searches)} recherches configurées")

    # 0. Traitement des callbacks Telegram (boutons cliqués depuis le dernier run)
    state_early = State(str(STATE_PATH))
    if not args.dry_run and not args.seed:
        process_callbacks(state_early)
        state_early.save()

    # 1. Scraping direct des sites
    try:
        all_listings = fetch_listings()
    except Exception as e:
        print(f"[main] Erreur scraping : {e}")
        traceback.print_exc()
        return 2

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
    sys.exit(main())
