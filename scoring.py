"""
Scoring des annonces — détecte les bonnes affaires.

Facteurs :
  - Prix vs cote de marché (le facteur principal)
  - Mots-clés positifs (entretien, distribution faite, etc.)
  - Mots-clés négatifs (accident, moteur HS, etc.)
  - Kilométrage vs norme (15 000 km/an)

Score final = somme pondérée. Seuil minimum configurable.
"""
from __future__ import annotations

from datetime import datetime
from typing import List, Tuple

from models import Listing, ScoringConfig, SearchConfig


NORMAL_KM_PER_YEAR = 15000


def score_listing(
    listing: Listing,
    search: SearchConfig,
    scoring: ScoringConfig,
) -> Tuple[float, List[str]]:
    """Retourne (score, breakdown_lignes).
    Le breakdown explique pourquoi le score est ce qu'il est."""
    score = 0.0
    breakdown: List[str] = []

    text_blob = " ".join(filter(None, [
        listing.title or "",
        listing.description or "",
    ])).lower()

    # --- 1. Prix vs cote de marché ---
    if listing.price and search.market_price_ref and search.market_price_ref > 0:
        deviation = (listing.price - search.market_price_ref) / search.market_price_ref
        # deviation négatif = prix sous la cote = bonne affaire
        price_score = -deviation * scoring.price_vs_market_weight
        score += price_score
        breakdown.append(
            f"Prix {listing.price}€ vs cote {search.market_price_ref}€ "
            f"({deviation*100:+.0f}%) → {price_score:+.1f}"
        )
    elif listing.price is None:
        breakdown.append("Prix inconnu → 0")

    # --- 2. Mots-clés positifs ---
    if search.keywords_nice:
        nice_hits = [k for k in search.keywords_nice if k.lower() in text_blob]
        if nice_hits:
            bonus = min(len(nice_hits) * scoring.nice_keyword_bonus, scoring.nice_keyword_cap)
            score += bonus
            breakdown.append(f"Mots+ {nice_hits} → +{bonus:.1f}")

    # --- 3. Mots-clés négatifs ---
    if search.keywords_bad:
        bad_hits = [k for k in search.keywords_bad if k.lower() in text_blob]
        if bad_hits:
            penalty = len(bad_hits) * scoring.bad_keyword_penalty
            score -= penalty
            breakdown.append(f"Mots- {bad_hits} → -{penalty:.1f}")

    # --- 4. Kilométrage vs norme ---
    if listing.year and listing.mileage:
        current_year = datetime.now().year
        age = max(1, current_year - listing.year)
        expected_km = age * NORMAL_KM_PER_YEAR
        km_deviation = listing.mileage - expected_km
        if km_deviation < -expected_km * 0.2:  # 20% sous la norme = bien entretenu
            score += scoring.km_under_normal_bonus
            breakdown.append(
                f"Km {listing.mileage} < norme {expected_km} → +{scoring.km_under_normal_bonus:.1f}"
            )
        elif km_deviation > expected_km * 0.3:  # 30% au-dessus = fatigue
            score -= scoring.km_over_normal_penalty
            breakdown.append(
                f"Km {listing.mileage} > norme {expected_km} → -{scoring.km_over_normal_penalty:.1f}"
            )

    return score, breakdown


def listing_passes_hard_filters(listing: Listing, search: SearchConfig) -> Tuple[bool, str]:
    """Filtre dur : rejette les annonces qui ne respectent pas les bornes.
    Retourne (passe, raison_si_rejet)."""
    # Mots-clés obligatoires
    if search.keywords_must:
        blob = " ".join(filter(None, [listing.title or "", listing.description or ""])).lower()
        if not any(k.lower() in blob for k in search.keywords_must):
            return False, f"ne matche pas keywords_must={search.keywords_must}"

    if listing.price is not None:
        if search.price_min is not None and listing.price < search.price_min:
            return False, f"prix {listing.price} < min {search.price_min}"
        if search.price_max is not None and listing.price > search.price_max:
            return False, f"prix {listing.price} > max {search.price_max}"

    if listing.year is not None:
        if search.year_min is not None and listing.year < search.year_min:
            return False, f"année {listing.year} < min {search.year_min}"
        if search.year_max is not None and listing.year > search.year_max:
            return False, f"année {listing.year} > max {search.year_max}"

    if listing.mileage is not None and search.mileage_max is not None:
        if listing.mileage > search.mileage_max:
            return False, f"km {listing.mileage} > max {search.mileage_max}"

    return True, ""
