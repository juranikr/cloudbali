from __future__ import annotations

import math
import unicodedata
from dataclasses import dataclass

from sqlalchemy.orm import Session

from app.models import Place


@dataclass(frozen=True)
class DuplicateMatch:
    place: Place
    confidence: float
    reason: str
    distance_m: float


def normalize_place_name(value: str) -> str:
    folded = unicodedata.normalize("NFKC", value or "").casefold()
    return "".join(character for character in folded if character.isalnum())


def distance_m(lat1: float, lng1: float, lat2: float, lng2: float) -> float:
    radius = 6_371_000.0
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    delta_phi = math.radians(lat2 - lat1)
    delta_lng = math.radians(lng2 - lng1)
    value = (
        math.sin(delta_phi / 2) ** 2
        + math.cos(phi1) * math.cos(phi2) * math.sin(delta_lng / 2) ** 2
    )
    return radius * 2 * math.atan2(math.sqrt(value), math.sqrt(max(0.0, 1 - value)))


def duplicate_matches(
    db: Session,
    *,
    title: str,
    local_name: str = "",
    lat: float,
    lng: float,
    category: str = "",
    region_id: int | None = None,
    exclude_place_ids: set[int] | None = None,
    limit: int = 5,
) -> list[DuplicateMatch]:
    """Return conservative, explainable duplicate candidates.

    A same normalized name is considered probable only within 750 metres. A
    very close coordinate still needs the same category, avoiding false merges
    in dense temple, market and beach areas.
    """

    names = {normalize_place_name(title), normalize_place_name(local_name)} - {""}
    query = db.query(Place).filter(Place.merged_into_id.is_(None))
    if region_id is not None:
        query = query.filter(Place.region_id == region_id)
    excluded = exclude_place_ids or set()
    matches: list[DuplicateMatch] = []
    for place in query.all():
        if place.id in excluded:
            continue
        distance = distance_m(lat, lng, place.lat, place.lng)
        other_names = {
            normalize_place_name(place.title),
            normalize_place_name(place.local_name),
        } - {""}
        same_name = bool(names & other_names)
        if same_name and distance <= 75:
            matches.append(DuplicateMatch(place, 0.98, "same_name_nearby", distance))
        elif same_name and distance <= 750:
            matches.append(DuplicateMatch(place, 0.9, "same_name_region", distance))
        elif category and place.category == category and distance <= 18:
            matches.append(DuplicateMatch(place, 0.86, "same_category_same_coordinate", distance))
    matches.sort(key=lambda match: (-match.confidence, match.distance_m, match.place.id))
    return matches[: max(1, min(limit, 20))]


def strongest_duplicate(**kwargs) -> DuplicateMatch | None:
    matches = duplicate_matches(**kwargs)
    return matches[0] if matches else None
