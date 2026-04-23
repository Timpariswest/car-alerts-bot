"""
Dataclasses du bot d'alertes voitures.
"""
from __future__ import annotations

from dataclasses import dataclass, field, asdict
from typing import Optional, List


@dataclass
class SearchConfig:
    name: str
    keywords_must: List[str] = field(default_factory=list)
    keywords_nice: List[str] = field(default_factory=list)
    keywords_bad: List[str] = field(default_factory=list)
    price_min: Optional[int] = None
    price_max: Optional[int] = None
    year_min: Optional[int] = None
    year_max: Optional[int] = None
    mileage_max: Optional[int] = None
    market_price_ref: Optional[int] = None


@dataclass
class ScoringConfig:
    price_vs_market_weight: float = 40.0
    nice_keyword_bonus: float = 2.0
    nice_keyword_cap: float = 6.0
    bad_keyword_penalty: float = 5.0
    km_under_normal_bonus: float = 3.0
    km_over_normal_penalty: float = 2.0
    min_score_to_notify: float = 2.0


@dataclass
class Listing:
    """Une annonce extraite d'un email d'alerte officiel."""
    site: str              # "leboncoin" | "lacentrale" | "autoscout24"
    listing_id: str        # ID unique côté site (extrait de l'URL)
    title: str
    price: Optional[int]   # en €
    year: Optional[int]
    mileage: Optional[int] # en km
    location: Optional[str]
    url: str
    image_url: Optional[str] = None
    description: Optional[str] = None
    # Metadata de scoring (rempli ensuite)
    score: float = 0.0
    score_breakdown: List[str] = field(default_factory=list)
    matched_search: Optional[str] = None

    @property
    def uid(self) -> str:
        return f"{self.site}:{self.listing_id}"

    def to_dict(self) -> dict:
        return asdict(self)
