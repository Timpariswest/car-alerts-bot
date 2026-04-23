"""
Bot d'alertes voitures — orchestration principale.

Pipeline :
  1. Lit la boîte Gmail (IMAP) pour les emails d'alerte des 3 sites
  2. Parse chaque email → liste d'annonces
  3. Pour chaque annonce : matche avec une recherche config, applique les filtres durs
  4. Score l'annonce (prix vs cote + keywords + km)
  5. Si score ≥ seuil et pas déjà vue → notifie Telegram
  6. Marque comme vue et sauvegarde state

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

from imap_client import fetch_all_accounts, load_accounts_from_env
from models import Listing, ScoringConfig, SearchConfig
from notifier import TelegramNotifier
from parsers import PARSERS
from scoring import listing_passes_hard_filters, score_listing
from state import State


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
    # Fallback : première recherche si aucune ne match strictement (annonces Leboncoin avec titre court)
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
    mailbox = cfg.get("mailbox", "INBOX")
    max_age_days = int(cfg.get("max_email_age_days", 3))
    label_filter = cfg.get("label_filter")

    print(f"[main] dry_run={args.dry_run} seed={args.seed} max_notifs={max_notifs}")
    print(f"[main] {len(searches)} recherches configurées")

    # 1. Fetch emails (multi-comptes)
    try:
        accounts = load_accounts_from_env()
    except RuntimeError as e:
        print(f"[main] ERREUR : {e}")
        return 1

    print(f"[main] {len(accounts)} compte(s) Gmail : {[a.user for a in accounts]}")

    try:
        emails = fetch_all_accounts(
            accounts,
            mailbox=mailbox,
            max_age_days=max_age_days,
            label_filter=label_filter,
        )
    except Exception as e:
        print(f"[main] Erreur IMAP fetch : {e}")
        traceback.print_exc()
        return 2

    print(f"[main] {len(emails)} emails d'alerte récupérés")

    # 2. Parse tous les emails → annonces
    all_listings: list[Listing] = []
    for em in emails:
        parser = PARSERS.get(em.site)
        if not parser:
            continue
        try:
            listings = parser(em)
            all_listings.extend(listings)
            print(f"  [{em.site}] {em.subject[:60]!r} → {len(listings)} annonces")
        except Exception as e:
            print(f"  [{em.site}] parse error : {e}")
            traceback.print_exc()

    print(f"[main] Total {len(all_listings)} annonces brutes")

    # Dédup par uid (les sites peuvent réenvoyer la même annonce sur plusieurs emails)
    dedup: dict[str, Listing] = {}
    for l in all_listings:
        if l.uid not in dedup:
            dedup[l.uid] = l
    all_listings = list(dedup.values())

    # 3-4. Pour chaque annonce : match + filtres durs + score
    state = State(str(STATE_PATH))
    notifier = TelegramNotifier() if not args.dry_run else None

    candidates: list[Listing] = []
    for l in all_listings:
        s = match_listing_to_search(l, searches)
        if not s:
            continue
        ok, reason = listing_passes_hard_filters(l, s)
        if not ok:
            print(f"  [skip:{l.site}:{l.listing_id}] {reason}")
            continue
        score, breakdown = score_listing(l, s, scoring_cfg)
        l.score = score
        l.score_breakdown = breakdown
        l.matched_search = s.name
        candidates.append(l)

    # Tri par score décroissant
    candidates.sort(key=lambda x: x.score, reverse=True)

    # 5. Filtre seuil + déjà-vu + push
    sent = 0
    for l in candidates:
        if state.is_seen(l.uid):
            continue
        if l.score < scoring_cfg.min_score_to_notify:
            state.mark_seen(l.uid)  # on marque quand même pour pas rescorer en boucle
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

        ok = notifier.send(l) if notifier else False
        if ok:
            state.mark_seen(l.uid)
            sent += 1
            notifier.throttle(1.0)
        else:
            print(f"  [send-fail] {l.uid}")

    # 6. Save
    if not args.dry_run:
        state.save()

    print(f"[main] Terminé. {sent} notifications envoyées, {len(state.seen)} en mémoire.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
