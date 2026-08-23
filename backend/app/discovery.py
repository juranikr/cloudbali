from __future__ import annotations

import argparse
import json
import math
import random
import re
import time
import unicodedata
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from urllib.parse import urlencode
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, joinedload

from app.db import SessionLocal, engine
from app.migrations import run_migrations
from app.models import (
    BatchRun,
    DiscoveryCandidate,
    DiscoveryDecision,
    DiscoveryJob,
    DiscoveryScanState,
    Place,
    Region,
    User,
)
from app.operations_api import record_place_change_event
from app.schemas import (
    BatchRunOut,
    DiscoveryCandidateOut,
    DiscoveryDecisionOut,
    DiscoveryRegionFailureOut,
    DiscoveryRunOut,
)


SOURCE = "openstreetmap"
OVERPASS_ENDPOINTS = (
    "https://overpass-api.de/api/interpreter",
    "https://overpass.kumi.systems/api/interpreter",
)
MAX_CANDIDATES_PER_RUN = 200
MAX_OSM_ELEMENTS_PER_REGION = 1000
MAX_BBOX_SPAN = 1.5
QUERY_PHASE_COUNT = 4
SPLIT_REGION_SLUGS = frozenset({"east-bali", "nusa-penida"})
SPLIT_REGION_GRID_SIZE = 2
MAX_REGION_FETCH_ATTEMPTS = 3
RETRY_BACKOFF_SECONDS = 2.0
MAX_RETRY_BACKOFF_SECONDS = 8.0
TRANSIENT_HTTP_STATUS_CODES = frozenset({408, 425, 429})
TRANSIENT_PROVIDER_ERROR_TYPES = frozenset({
    "BrokenPipeError",
    "ConnectionAbortedError",
    "ConnectionError",
    "ConnectionRefusedError",
    "ConnectionResetError",
    "IncompleteRead",
    "RemoteDisconnected",
    "TimeoutError",
    "URLError",
})
STALE_RUN_AFTER = timedelta(minutes=30)
ACTIVE_DISCOVERY_SLOT = "place_discovery"
ACTIVE_RUN_STATUSES = frozenset({"queued", "running"})
TERMINAL_RUN_STATUSES = frozenset({"success", "partial", "failed"})


class DiscoveryBusyError(RuntimeError):
    """Raised when another queued/running discovery job owns the global lease."""

    def __init__(self, active_run_id: int):
        self.active_run_id = active_run_id
        super().__init__(
            f"새 장소 발굴 실행 #{active_run_id}이 이미 대기 또는 실행 중입니다"
        )


class CandidateInactiveError(ValueError):
    """Raised after an authoritative source blocks and records an approval."""


@dataclass(frozen=True)
class OverpassSegment:
    """One bounded provider query within a discovery region."""

    name: str
    south: float
    west: float
    north: float
    east: float


@dataclass(frozen=True)
class DiscoveryRegionSnapshot:
    """Thread-safe scalar region data copied before the ORM session commits."""

    id: int
    slug: str
    name_ko: str
    south: float
    west: float
    north: float
    east: float


@dataclass(frozen=True)
class DiscoveryFetchSpec:
    """Provider inputs plus the scan-state version that selected them."""

    region: DiscoveryRegionSnapshot
    limit: int
    query_phase: int
    scan_state_id: int
    scan_query_phase: int
    scan_fetch_limit: int
    scan_count: int


@dataclass(frozen=True)
class DiscoveryFetchError:
    endpoint: str | None
    error_type: str
    status_code: int | None
    message: str
    segment: str
    attempt: int = 0

    def at_attempt(self, attempt: int) -> DiscoveryFetchError:
        return DiscoveryFetchError(
            endpoint=self.endpoint,
            error_type=self.error_type,
            status_code=self.status_code,
            message=self.message,
            segment=self.segment,
            attempt=attempt,
        )

    def as_dict(self) -> dict[str, object]:
        return {
            "endpoint": self.endpoint,
            "error_type": self.error_type,
            "status_code": self.status_code,
            "message": self.message,
            "segment": self.segment,
            "attempt": self.attempt,
        }


@dataclass(frozen=True)
class DiscoveryRegionFailure:
    region_id: int
    region_name: str
    attempts: int
    errors: tuple[DiscoveryFetchError, ...]

    def as_dict(self) -> dict[str, object]:
        return {
            "region_id": self.region_id,
            "region_name": self.region_name,
            "attempts": self.attempts,
            "errors": [error.as_dict() for error in self.errors],
        }


class OverpassRequestError(RuntimeError):
    """A bounded Overpass attempt failed, with no response bodies retained."""

    def __init__(self, errors: list[DiscoveryFetchError]):
        self.errors = tuple(errors)
        error_types = ", ".join(error.error_type for error in errors) or "unknown"
        super().__init__(f"Overpass request failed ({error_types})")


@dataclass(frozen=True)
class CandidateValues:
    external_id: str
    source_url: str
    title: str
    local_name: str
    description: str
    area: str
    category: str
    lat: float
    lng: float
    confidence: float
    evidence: str
    tags: str


def _cli_limit(value: str) -> int:
    try:
        parsed = int(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("limit must be an integer") from exc
    if not 1 <= parsed <= MAX_CANDIDATES_PER_RUN:
        raise argparse.ArgumentTypeError(f"limit must be between 1 and {MAX_CANDIDATES_PER_RUN}")
    return parsed


def _valid_bbox(region: Region | DiscoveryRegionSnapshot) -> bool:
    return (
        -90 <= region.south < region.north <= 90
        and -180 <= region.west < region.east <= 180
        and region.north - region.south <= MAX_BBOX_SPAN
        and region.east - region.west <= MAX_BBOX_SPAN
    )


def _region_snapshot(region: Region) -> DiscoveryRegionSnapshot:
    return DiscoveryRegionSnapshot(
        id=region.id,
        slug=region.slug,
        name_ko=region.name_ko,
        south=region.south,
        west=region.west,
        north=region.north,
        east=region.east,
    )


def _overpass_segments(
    region: Region | DiscoveryRegionSnapshot,
    query_phase: int = 0,
) -> tuple[OverpassSegment, ...]:
    """Split only known heavy regions into a bounded 2x2 grid.

    The category phase remains separate, so no tile query expands back into an
    all-category Overpass scan. Other regions retain their single-query path.
    """

    if not _valid_bbox(region):
        raise ValueError(f"안전 범위를 벗어난 권역 bbox: {region.slug}")
    phase = int(query_phase) % QUERY_PHASE_COUNT
    if region.slug not in SPLIT_REGION_SLUGS:
        return (
            OverpassSegment(
                name=f"full:phase-{phase}",
                south=region.south,
                west=region.west,
                north=region.north,
                east=region.east,
            ),
        )

    lat_step = (region.north - region.south) / SPLIT_REGION_GRID_SIZE
    lng_step = (region.east - region.west) / SPLIT_REGION_GRID_SIZE
    segments: list[OverpassSegment] = []
    for row in range(SPLIT_REGION_GRID_SIZE):
        for column in range(SPLIT_REGION_GRID_SIZE):
            south = region.south + row * lat_step
            north = region.north if row == SPLIT_REGION_GRID_SIZE - 1 else south + lat_step
            west = region.west + column * lng_step
            east = region.east if column == SPLIT_REGION_GRID_SIZE - 1 else west + lng_step
            segments.append(
                OverpassSegment(
                    name=(
                        f"tile-{row + 1}-{column + 1}/"
                        f"{SPLIT_REGION_GRID_SIZE}x{SPLIT_REGION_GRID_SIZE}:phase-{phase}"
                    ),
                    south=south,
                    west=west,
                    north=north,
                    east=east,
                )
            )
    return tuple(segments)


def _overpass_query(
    region: Region | DiscoveryRegionSnapshot,
    limit: int,
    query_phase: int = 0,
    *,
    segment: OverpassSegment | None = None,
) -> str:
    if not _valid_bbox(region):
        raise ValueError(f"안전 범위를 벗어난 권역 bbox: {region.slug}")
    active_segment = segment or _overpass_segments(region, query_phase)[0]
    bbox = (
        f"{active_segment.south:.6f},{active_segment.west:.6f},"
        f"{active_segment.north:.6f},{active_segment.east:.6f}"
    )
    safe_limit = max(1, min(int(limit), MAX_OSM_ELEMENTS_PER_REGION))
    phase = int(query_phase) % QUERY_PHASE_COUNT
    selectors = (
        f'''nwr["tourism"~"^(attraction|viewpoint|museum|gallery|artwork|zoo|aquarium|theme_park)$"]({bbox});
  nwr["natural"~"^(beach|waterfall|peak|hot_spring|reef)$"]({bbox});
  nwr["historic"~"^(temple|ruins|archaeological_site|monument|memorial)$"]({bbox});''',
        f'''nwr["amenity"~"^(cafe|restaurant|food_court|marketplace)$"]({bbox});''',
        f'''nwr["amenity"~"^(bar|ferry_terminal|place_of_worship)$"]({bbox});
  nwr["leisure"~"^(marina|water_park|nature_reserve|spa)$"]({bbox});''',
        f'''nwr["sport"~"^(surfing|scuba_diving|diving|snorkelling)$"]({bbox});
  nwr["shop"="scuba_diving"]({bbox});''',
    )[phase]
    return f'''[out:json][timeout:25];
(
  {selectors}
);
out center {safe_limit};'''


def _safe_provider_error_message(exc: Exception) -> str:
    """Return a short diagnostic without storing provider response bodies."""

    if isinstance(exc, HTTPError):
        raw_message = exc.reason or f"HTTP {exc.code}"
    else:
        raw_message = getattr(exc, "reason", None) or str(exc) or type(exc).__name__
    message = " ".join(str(raw_message).split())
    return (message or "provider request failed")[:240]


def _provider_error(
    *,
    endpoint: str,
    segment: OverpassSegment,
    exc: Exception,
) -> DiscoveryFetchError:
    raw_status = getattr(exc, "code", None)
    status_code = raw_status if isinstance(raw_status, int) else None
    return DiscoveryFetchError(
        endpoint=endpoint,
        error_type=type(exc).__name__[:80],
        status_code=status_code,
        message=_safe_provider_error_message(exc),
        segment=segment.name,
    )


def _fetch_osm_segment(
    region: Region | DiscoveryRegionSnapshot,
    limit: int,
    query_phase: int,
    segment: OverpassSegment,
) -> list[dict]:
    query = _overpass_query(region, limit, query_phase, segment=segment)
    payload = urlencode({"data": query}).encode("utf-8")
    errors: list[DiscoveryFetchError] = []
    for endpoint in OVERPASS_ENDPOINTS:
        request = Request(
            endpoint,
            data=payload,
            headers={
                "User-Agent": "cloudbali-place-discovery/1.0 (admin-reviewed candidates)",
                "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
            },
            method="POST",
        )
        try:
            with urlopen(request, timeout=35) as response:
                result = json.loads(response.read().decode("utf-8"))
            elements = result.get("elements", [])
            if not isinstance(elements, list):
                raise ValueError("Overpass elements 응답이 배열이 아닙니다")
            return elements[: max(1, min(int(limit), MAX_OSM_ELEMENTS_PER_REGION))]
        except Exception as exc:
            errors.append(_provider_error(endpoint=endpoint, segment=segment, exc=exc))
    raise OverpassRequestError(errors)


def fetch_osm_elements(
    region: Region | DiscoveryRegionSnapshot,
    limit: int,
    query_phase: int = 0,
) -> list[dict]:
    """Fetch a bounded, de-duplicated OSM result set.

    Heavy regions divide the same per-region limit across four spatial tiles;
    the function never multiplies the requested result cap by the tile count.
    """

    safe_limit = max(1, min(int(limit), MAX_OSM_ELEMENTS_PER_REGION))
    segments = _overpass_segments(region, query_phase)
    base_limit, extra = divmod(safe_limit, len(segments))
    seen: set[tuple[str, object]] = set()
    combined: list[dict] = []
    for index, segment in enumerate(segments):
        segment_limit = base_limit + (1 if index < extra else 0)
        if segment_limit <= 0:
            continue
        elements = _fetch_osm_segment(region, segment_limit, query_phase, segment)
        for element in elements:
            key = (str(element.get("type", "")), element.get("id"))
            if key in seen:
                continue
            seen.add(key)
            combined.append(element)
    return combined[:safe_limit]


def _fetch_errors_for_attempt(
    exc: Exception,
    *,
    query_phase: int,
    attempt: int,
) -> list[DiscoveryFetchError]:
    if isinstance(exc, OverpassRequestError):
        return [error.at_attempt(attempt) for error in exc.errors]
    return [
        DiscoveryFetchError(
            endpoint=None,
            error_type=type(exc).__name__[:80],
            status_code=None,
            message=_safe_provider_error_message(exc),
            segment=f"region:phase-{int(query_phase) % QUERY_PHASE_COUNT}",
            attempt=attempt,
        )
    ]


def _sleep_before_fetch_retry(failed_attempt: int) -> None:
    """Apply a short, bounded exponential delay with positive jitter."""

    base_delay = min(
        MAX_RETRY_BACKOFF_SECONDS,
        RETRY_BACKOFF_SECONDS * (2 ** max(0, failed_attempt - 1)),
    )
    upper_bound = min(MAX_RETRY_BACKOFF_SECONDS, base_delay * 1.25)
    time.sleep(random.uniform(base_delay, upper_bound))


def _provider_error_is_transient(error: DiscoveryFetchError) -> bool:
    if error.status_code is not None:
        return (
            error.status_code in TRANSIENT_HTTP_STATUS_CODES
            or 500 <= error.status_code <= 599
        )
    return error.error_type in TRANSIENT_PROVIDER_ERROR_TYPES


def _fetch_exception_is_transient(exc: Exception) -> bool:
    if isinstance(exc, OverpassRequestError):
        return any(_provider_error_is_transient(error) for error in exc.errors)
    if isinstance(exc, HTTPError):
        return exc.code in TRANSIENT_HTTP_STATUS_CODES or 500 <= exc.code <= 599
    return isinstance(exc, (TimeoutError, ConnectionError, URLError))


def _fetch_region_with_retry(
    region: Region | DiscoveryRegionSnapshot,
    limit: int,
    query_phase: int,
) -> tuple[list[dict] | None, DiscoveryRegionFailure | None]:
    """Fetch one region at most three times without touching other regions.

    A split region is the retry unit: if one tile fails, the bounded tile set is
    fetched again on the next attempt. This deliberately avoids mixing a
    partial provider snapshot into the candidate transaction.
    """

    errors: list[DiscoveryFetchError] = []
    for attempt in range(1, MAX_REGION_FETCH_ATTEMPTS + 1):
        try:
            return fetch_osm_elements(region, limit, query_phase), None
        except Exception as exc:
            errors.extend(
                _fetch_errors_for_attempt(
                    exc,
                    query_phase=query_phase,
                    attempt=attempt,
                )
            )
            if not _fetch_exception_is_transient(exc) or attempt >= MAX_REGION_FETCH_ATTEMPTS:
                return None, DiscoveryRegionFailure(
                    region_id=region.id,
                    region_name=region.name_ko,
                    attempts=attempt,
                    errors=tuple(errors),
                )
            _sleep_before_fetch_retry(attempt)
    raise AssertionError("bounded discovery retry loop did not return")


def _category(tags: dict[str, str]) -> tuple[str, str] | None:
    amenity = tags.get("amenity", "").lower()
    tourism = tags.get("tourism", "").lower()
    natural = tags.get("natural", "").lower()
    historic = tags.get("historic", "").lower()
    leisure = tags.get("leisure", "").lower()
    sport = tags.get("sport", "").lower()
    shop = tags.get("shop", "").lower()

    if amenity == "ferry_terminal" or leisure == "marina":
        return "transport", f"amenity/leisure={amenity or leisure}"
    if sport == "surfing":
        return "surf", "sport=surfing"
    if sport in {"scuba_diving", "diving", "snorkelling"} or shop == "scuba_diving":
        return "dive", f"sport/shop={sport or shop}"
    if natural == "beach":
        return "beach", "natural=beach"
    if leisure == "spa":
        return "wellness", "leisure=spa"
    if amenity == "cafe":
        return "cafe", "amenity=cafe"
    if amenity in {"restaurant", "food_court"}:
        return "food", f"amenity={amenity}"
    if amenity == "bar":
        return "nightlife", "amenity=bar"
    if amenity == "marketplace":
        return "other", "amenity=marketplace"
    if amenity == "place_of_worship" or historic or tourism in {"museum", "gallery", "artwork"}:
        return "culture", f"cultural={amenity or historic or tourism}"
    if natural or leisure in {"nature_reserve", "water_park"} or tourism in {"viewpoint", "zoo", "aquarium"}:
        return "nature", f"nature={natural or leisure or tourism}"
    if tourism in {"attraction", "theme_park"}:
        return "other", f"tourism={tourism}"
    return None


def _coordinates(element: dict) -> tuple[float, float] | None:
    raw_lat = element.get("lat")
    raw_lng = element.get("lon")
    if raw_lat is None or raw_lng is None:
        center = element.get("center") or {}
        raw_lat = center.get("lat")
        raw_lng = center.get("lon")
    try:
        lat, lng = float(raw_lat), float(raw_lng)
    except (TypeError, ValueError):
        return None
    if not (math.isfinite(lat) and math.isfinite(lng) and -90 <= lat <= 90 and -180 <= lng <= 180):
        return None
    return lat, lng


def _inside_region(region: Region, lat: float, lng: float) -> bool:
    return region.south <= lat <= region.north and region.west <= lng <= region.east


def _clean_text(value: object, max_length: int) -> str:
    if not isinstance(value, str):
        return ""
    return " ".join(value.split())[:max_length]


_HANGUL_PATTERN = re.compile(r"[\uac00-\ud7a3]")

_CATEGORY_LABELS_KO = {
    "transport": "교통 시설",
    "surf": "서핑 관련 장소",
    "dive": "다이빙·스노클링 관련 장소",
    "beach": "해변",
    "wellness": "스파·웰니스 시설",
    "cafe": "카페",
    "food": "음식점",
    "nightlife": "바·나이트라이프 장소",
    "culture": "문화 명소",
    "nature": "자연 명소",
    "other": "관광 명소",
}

_CUISINE_LABELS_KO = {
    "asian": "아시아 요리",
    "balinese": "발리 요리",
    "burger": "버거",
    "chinese": "중식",
    "coffee_shop": "커피",
    "ice_cream": "아이스크림",
    "indonesian": "인도네시아 요리",
    "italian": "이탈리아 요리",
    "japanese": "일식",
    "pizza": "피자",
    "seafood": "해산물",
    "vegan": "비건 요리",
    "vegetarian": "채식 요리",
}


def _osm_type_label_ko(tags: dict[str, str], category: str) -> str:
    """Translate controlled OSM enum tags without translating free-text claims."""

    amenity = tags.get("amenity", "").casefold()
    tourism = tags.get("tourism", "").casefold()
    natural = tags.get("natural", "").casefold()
    historic = tags.get("historic", "").casefold()
    leisure = tags.get("leisure", "").casefold()
    sport = tags.get("sport", "").casefold()
    shop = tags.get("shop", "").casefold()

    if amenity == "ferry_terminal":
        return "여객선 터미널"
    if leisure == "marina":
        return "마리나·선착장"
    if sport == "surfing":
        return "서핑 관련 장소"
    if sport in {"scuba_diving", "diving", "snorkelling"} or shop == "scuba_diving":
        return "다이빙·스노클링 관련 장소"
    if natural == "beach":
        return "해변"
    if leisure == "spa":
        return "스파·웰니스 시설"
    if amenity == "cafe":
        return "카페"
    if amenity == "restaurant":
        return "음식점"
    if amenity == "food_court":
        return "푸드코트"
    if amenity == "bar":
        return "바"
    if amenity == "marketplace":
        return "시장"
    if amenity == "place_of_worship":
        religions = {
            "buddhist": "불교",
            "christian": "기독교",
            "hindu": "힌두교",
            "muslim": "이슬람교",
        }
        religion = religions.get(tags.get("religion", "").casefold())
        return f"{religion} 종교 시설" if religion else "종교 시설"

    historic_labels = {
        "archaeological_site": "고고학 유적",
        "memorial": "추모 시설",
        "monument": "기념물",
        "ruins": "유적",
        "temple": "역사적 사원",
    }
    if historic in historic_labels:
        return historic_labels[historic]
    tourism_labels = {
        "aquarium": "수족관",
        "artwork": "예술 작품",
        "attraction": "관광 명소",
        "gallery": "갤러리",
        "museum": "박물관",
        "theme_park": "테마파크",
        "viewpoint": "전망대",
        "zoo": "동물원",
    }
    if tourism in tourism_labels:
        return tourism_labels[tourism]
    natural_labels = {
        "hot_spring": "온천",
        "peak": "산봉우리",
        "reef": "산호초",
        "waterfall": "폭포",
    }
    if natural in natural_labels:
        return natural_labels[natural]
    leisure_labels = {
        "nature_reserve": "자연보호구역",
        "water_park": "워터파크",
    }
    if leisure in leisure_labels:
        return leisure_labels[leisure]
    return _CATEGORY_LABELS_KO.get(category, "여행 장소")


def _candidate_description_ko(
    region: Region,
    tags: dict[str, str],
    category: str,
    *,
    source_label: str = "OpenStreetMap",
) -> str:
    """Build a concise Korean summary from sourced, controlled enum tags."""

    source_description = (
        _clean_text(tags.get("description:ko"), 5000)
        or _clean_text(tags.get("description"), 5000)
    )
    if _HANGUL_PATTERN.search(source_description):
        return source_description

    region_name = _clean_text(region.name_ko, 100) or "선택한"
    place_type = _osm_type_label_ko(tags, category)
    description = (
        f"{region_name} 권역에 있는 장소로, "
        f"{source_label}에는 {place_type} 유형으로 등록되어 있습니다."
    )
    cuisine_values = re.split(r"[;,]", tags.get("cuisine", "").casefold())
    cuisines = list(dict.fromkeys(
        _CUISINE_LABELS_KO[value.strip()]
        for value in cuisine_values
        if value.strip() in _CUISINE_LABELS_KO
    ))
    if cuisines:
        description += f" 요리 태그에는 {', '.join(cuisines[:3])} 정보가 포함되어 있습니다."
    return description


_INACTIVE_LIFECYCLE_PREFIXES = ("disused", "abandoned", "demolished", "razed", "removed")
_INACTIVE_EXPLICIT_VALUES = {
    "1", "yes", "true", "closed", "permanently_closed", "permanently closed",
    "temporarily_closed", "temporarily closed", "temporary_closed", "inactive",
    "non_operational", "non-operational", "not_operational",
    "disused", "abandoned", "demolished", "razed", "removed",
}
_INACTIVE_FALSE_VALUES = {"", "0", "no", "false", "none", "open", "active", "operational"}


def inactive_place_reason(tags: dict[str, str]) -> str:
    """Return one conservative OSM lifecycle reason that blocks a new place.

    Only explicit inactive tags are accepted here. Ambiguous access limits or
    missing opening hours remain reviewable, while a literal permanent-closure
    or lifecycle namespace can never slip through merely because other tags
    make the candidate score look strong.
    """

    normalized = {
        str(key).strip().casefold(): str(value).strip().casefold()
        for key, value in tags.items()
        if value is not None
    }
    opening_hours = normalized.get("opening_hours", "")
    always_off = bool(re.match(
        r"^(?:mo\s*-\s*su|mo\s*,\s*tu\s*,\s*we\s*,\s*th\s*,\s*fr\s*,\s*sa\s*,\s*su|24/7)\s+off(?:\s*;|$)",
        opening_hours,
    ))
    if opening_hours in {"closed", "off", "permanently_closed", "permanently closed"} or always_off:
        return f"opening_hours={opening_hours}"
    for key in ("status", "operational_status"):
        value = normalized.get(key, "")
        if value in _INACTIVE_EXPLICIT_VALUES:
            return f"{key}={value}"
    for prefix in _INACTIVE_LIFECYCLE_PREFIXES:
        direct = normalized.get(prefix, "")
        if direct and direct not in _INACTIVE_FALSE_VALUES:
            return f"{prefix}={direct}"
        for key, value in normalized.items():
            if key.startswith(prefix + ":") and value not in _INACTIVE_FALSE_VALUES:
                return f"{key}={value}"
    return ""


def _candidate_inactive_reason(candidate: DiscoveryCandidate) -> str:
    try:
        evidence = json.loads(candidate.evidence or "{}")
    except (TypeError, json.JSONDecodeError):
        return ""
    tags = evidence.get("osm_tags") if isinstance(evidence, dict) else None
    if not isinstance(tags, dict):
        return ""
    return inactive_place_reason({str(key): str(value) for key, value in tags.items()})


def _official_json(url: str, *, timeout: float = 8.0) -> dict:
    request = Request(
        url,
        headers={
            "User-Agent": "cloudbali-place-approval/1.0 (admin-reviewed candidates)",
            "Accept": "application/json",
        },
    )
    with urlopen(request, timeout=timeout) as response:
        if "json" not in (response.headers.get("Content-Type") or "").lower():
            raise ValueError("공식 출처가 JSON으로 응답하지 않았습니다")
        payload = json.loads(response.read(1_500_001).decode("utf-8"))
    if not isinstance(payload, dict):
        raise ValueError("공식 출처 응답 형식이 올바르지 않습니다")
    return payload


def _candidate_references(candidate: DiscoveryCandidate) -> list[tuple[str, str]]:
    references = [(candidate.source.strip().casefold(), candidate.external_id.strip())]
    try:
        evidence = json.loads(candidate.evidence or "{}")
    except (TypeError, json.JSONDecodeError):
        evidence = {}
    chat_research = evidence.get("chat_research") if isinstance(evidence, dict) else None
    external_ids = chat_research.get("external_ids") if isinstance(chat_research, dict) else None
    if isinstance(external_ids, dict):
        references.extend(
            (str(source).strip().casefold(), str(external_id).strip())
            for source, external_id in external_ids.items()
        )
    return list(dict.fromkeys(
        (source, external_id)
        for source, external_id in references
        if source in {"openstreetmap", "wikidata"} and external_id
    ))


def _coordinate_revalidation(
    candidate: DiscoveryCandidate,
    latitude: object,
    longitude: object,
    *,
    max_drift_m: float = 300.0,
) -> dict | None:
    try:
        live_lat = float(latitude)
        live_lng = float(longitude)
        candidate_lat = float(candidate.lat)
        candidate_lng = float(candidate.lng)
    except (AttributeError, TypeError, ValueError):
        return None
    if not (-90 <= live_lat <= 90 and -180 <= live_lng <= 180):
        return None
    drift_m = _distance_m(candidate_lat, candidate_lng, live_lat, live_lng)
    return {
        "lat": live_lat,
        "lng": live_lng,
        "drift_m": round(drift_m, 1),
        "verified": drift_m <= max_drift_m,
        "max_drift_m": max_drift_m,
    }


def _wikidata_coordinate(claims: dict) -> tuple[object, object]:
    coordinate_claims = claims.get("P625")
    if not isinstance(coordinate_claims, list):
        return None, None
    for claim in coordinate_claims:
        try:
            value = claim["mainsnak"]["datavalue"]["value"]
            if isinstance(value, dict):
                return value.get("latitude"), value.get("longitude")
        except (KeyError, TypeError):
            continue
    return None, None


def revalidate_candidate_lifecycle(candidate: DiscoveryCandidate) -> tuple[str, dict]:
    """Fail closed unless every storable identity is live at approval time."""

    checked_at = datetime.now(timezone.utc).isoformat()
    checks: list[dict] = []
    verified_coordinate: dict | None = None
    references = _candidate_references(candidate)
    if not references:
        raise RuntimeError("최신 상태를 확인할 수 있는 저장 가능 출처가 없습니다")
    for source, external_id in references:
        if source == "openstreetmap":
            match = re.fullmatch(r"(node|way|relation)/(\d+)", external_id)
            if match is None:
                raise RuntimeError("OpenStreetMap 객체 식별자가 올바르지 않습니다")
            osm_type, osm_id_text = match.groups()
            url = f"https://api.openstreetmap.org/api/0.6/{osm_type}/{osm_id_text}.json"
            try:
                payload = _official_json(url)
            except HTTPError as exc:
                if exc.code in {404, 410}:
                    return f"OpenStreetMap 객체가 삭제되었거나 더 이상 공개되지 않음 ({exc.code})", {
                        "checked_at": checked_at,
                        "checks": checks + [{"source": source, "external_id": external_id, "url": url, "active": False}],
                    }
                raise
            elements = payload.get("elements")
            element = next((
                item for item in elements if isinstance(item, dict)
                and item.get("type") == osm_type and str(item.get("id")) == osm_id_text
            ), None) if isinstance(elements, list) else None
            if element is None:
                return "OpenStreetMap 원문에서 동일 객체를 확인하지 못함", {
                    "checked_at": checked_at,
                    "checks": checks + [{"source": source, "external_id": external_id, "url": url, "active": False}],
                }
            raw_tags = element.get("tags") if isinstance(element.get("tags"), dict) else {}
            tags = {str(key): str(value) for key, value in raw_tags.items() if value is not None}
            reason = inactive_place_reason(tags)
            center = element.get("center") if isinstance(element.get("center"), dict) else {}
            coordinate_check = _coordinate_revalidation(
                candidate,
                element.get("lat", center.get("lat")),
                element.get("lon", center.get("lon")),
            )
            check = {
                "source": source,
                "external_id": external_id,
                "url": url,
                "active": not bool(reason),
                "inactive_reason": reason,
                "osm_tags": tags,
                "coordinate": coordinate_check,
            }
            checks.append(check)
            if reason:
                return reason, {"checked_at": checked_at, "checks": checks}
            if coordinate_check is not None and not coordinate_check["verified"]:
                check["active"] = False
                check["inactive_reason"] = "원문 좌표가 후보 위치에서 크게 이동함"
                return check["inactive_reason"], {"checked_at": checked_at, "checks": checks}
            if coordinate_check is not None and source == str(candidate.source).strip().casefold():
                verified_coordinate = {"source": source, **coordinate_check}
            continue

        if not re.fullmatch(r"Q\d+", external_id, flags=re.IGNORECASE):
            raise RuntimeError("Wikidata 객체 식별자가 올바르지 않습니다")
        entity_id = external_id.upper()
        url = f"https://www.wikidata.org/wiki/Special:EntityData/{entity_id}.json"
        payload = _official_json(url)
        entities = payload.get("entities")
        entity = entities.get(entity_id) if isinstance(entities, dict) else None
        if not isinstance(entity, dict) or "missing" in entity:
            return "Wikidata 원문에서 동일 장소를 확인하지 못함", {
                "checked_at": checked_at,
                "checks": checks + [{"source": source, "external_id": entity_id, "url": url, "active": False}],
            }
        claims = entity.get("claims") if isinstance(entity.get("claims"), dict) else {}
        inactive_properties = [property_id for property_id in ("P576", "P582") if claims.get(property_id)]
        has_coordinates = bool(claims.get("P625"))
        live_lat, live_lng = _wikidata_coordinate(claims)
        coordinate_check = _coordinate_revalidation(candidate, live_lat, live_lng)
        reason = (
            "Wikidata에 폐지·철거 또는 종료 시점이 기록됨 (" + ", ".join(inactive_properties) + ")"
            if inactive_properties else ""
        )
        if not reason and not has_coordinates:
            reason = "Wikidata 원문에서 장소 좌표가 제거됨"
        check = {
            "source": source,
            "external_id": entity_id,
            "url": url,
            "active": not bool(reason),
            "inactive_reason": reason,
            "coordinate_claim_present": has_coordinates,
            "coordinate": coordinate_check,
        }
        checks.append(check)
        if reason:
            return reason, {"checked_at": checked_at, "checks": checks}
        if coordinate_check is not None and not coordinate_check["verified"]:
            check["active"] = False
            check["inactive_reason"] = "원문 좌표가 후보 위치에서 크게 이동함"
            return check["inactive_reason"], {"checked_at": checked_at, "checks": checks}
        if coordinate_check is not None and source == str(candidate.source).strip().casefold():
            verified_coordinate = {"source": source, **coordinate_check}
    return "", {
        "checked_at": checked_at,
        "checks": checks,
        "verified_coordinate": verified_coordinate,
    }


def _confidence(tags: dict[str, str]) -> float:
    score = 0.54
    if tags.get("name:en") or tags.get("name:id") or tags.get("name:ko"):
        score += 0.06
    if tags.get("website") or tags.get("contact:website") or tags.get("wikidata") or tags.get("wikipedia"):
        score += 0.12
    if tags.get("opening_hours"):
        score += 0.07
    if tags.get("phone") or tags.get("contact:phone"):
        score += 0.05
    if tags.get("description") or tags.get("description:en") or tags.get("description:ko"):
        score += 0.07
    if tags.get("addr:street") or tags.get("addr:city") or tags.get("addr:village"):
        score += 0.04
    return round(min(score, 0.95), 2)


def _candidate_values(region: Region, element: dict) -> CandidateValues | None:
    element_type = str(element.get("type", ""))
    element_id = element.get("id")
    if element_type not in {"node", "way", "relation"} or not isinstance(element_id, int):
        return None
    tags = element.get("tags") or {}
    if not isinstance(tags, dict):
        return None
    tags = {str(key): str(value) for key, value in tags.items() if value is not None}
    if inactive_place_reason(tags):
        return None
    mapped = _category(tags)
    coordinates = _coordinates(element)
    name = _clean_text(tags.get("name"), 180)
    title = _clean_text(tags.get("name:ko"), 180) or name or _clean_text(tags.get("name:en"), 180)
    if mapped is None or coordinates is None or not title:
        return None
    category, matched_rule = mapped
    lat, lng = coordinates
    if not _inside_region(region, lat, lng):
        return None
    local_name = _clean_text(tags.get("name:id"), 180) or name
    if local_name.casefold() == title.casefold():
        local_name = ""
    description = _candidate_description_ko(region, tags, category)
    area = (
        _clean_text(tags.get("addr:suburb"), 100)
        or _clean_text(tags.get("addr:village"), 100)
        or _clean_text(tags.get("addr:town"), 100)
        or _clean_text(tags.get("addr:city"), 100)
    )
    external_id = f"{element_type}/{element_id}"
    source_url = f"https://www.openstreetmap.org/{element_type}/{element_id}"
    useful_tags = [
        value for value in (
            category,
            tags.get("tourism"),
            tags.get("amenity"),
            tags.get("natural"),
            tags.get("historic"),
            tags.get("leisure"),
            tags.get("sport"),
            tags.get("cuisine"),
        ) if value
    ]
    evidence = json.dumps(
        {
            "matched_rule": matched_rule,
            "osm_tags": tags,
            "region_bbox": [region.south, region.west, region.north, region.east],
            "coordinate_crs": "WGS84",
            "fetched_at": datetime.now(timezone.utc).isoformat(),
        },
        ensure_ascii=False,
        separators=(",", ":"),
    )
    return CandidateValues(
        external_id=external_id,
        source_url=source_url,
        title=title,
        local_name=local_name,
        description=description,
        area=area,
        category=category,
        lat=lat,
        lng=lng,
        confidence=_confidence(tags),
        evidence=evidence,
        tags=",".join(dict.fromkeys(_clean_text(item, 80) for item in useful_tags if _clean_text(item, 80))),
    )


def _normalized_name(value: str) -> str:
    normalized = unicodedata.normalize("NFKC", value or "").casefold()
    return "".join(character for character in normalized if character.isalnum())


def _distance_m(lat1: float, lng1: float, lat2: float, lng2: float) -> float:
    radius = 6_371_000.0
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    delta_phi = math.radians(lat2 - lat1)
    delta_lng = math.radians(lng2 - lng1)
    value = (
        math.sin(delta_phi / 2) ** 2
        + math.cos(phi1) * math.cos(phi2) * math.sin(delta_lng / 2) ** 2
    )
    return radius * 2 * math.atan2(math.sqrt(value), math.sqrt(max(0.0, 1 - value)))


def find_duplicate_place(values: CandidateValues, places: list[Place]) -> Place | None:
    names = {_normalized_name(values.title), _normalized_name(values.local_name)} - {""}
    nearest: tuple[float, Place] | None = None
    for place in places:
        distance = _distance_m(values.lat, values.lng, place.lat, place.lng)
        place_names = {_normalized_name(place.title), _normalized_name(place.local_name)} - {""}
        same_name = bool(names & place_names)
        if same_name and distance <= 500:
            return place
        if place.category == values.category and distance <= 25 and (nearest is None or distance < nearest[0]):
            nearest = (distance, place)
    return nearest[1] if nearest else None


def _candidate_duplicates(values: CandidateValues, candidates: list[DiscoveryCandidate]) -> bool:
    # Only an identical provider object is safe to suppress before review.
    # Same-name branches and replacement businesses can sit a few metres apart;
    # those must remain durable candidates with duplicate evidence instead of
    # disappearing because an older (even rejected) candidate happened to be near.
    return any(
        candidate.source == SOURCE and candidate.external_id == values.external_id
        for candidate in candidates
    )


def _allocate_limits(regions: list[Region], total_limit: int) -> dict[int, int]:
    total_limit = max(1, min(total_limit, MAX_CANDIDATES_PER_RUN))
    if len(regions) == 1:
        return {regions[0].id: total_limit}
    base, extra = divmod(total_limit, len(regions))
    return {
        region.id: base + (1 if index < extra else 0)
        for index, region in enumerate(regions)
        if base + (1 if index < extra else 0) > 0
    }


def _discovery_regions(db: Session, region_id: int | None) -> list[Region]:
    query = db.query(Region).order_by(Region.sort_order, Region.id)
    if region_id is not None:
        query = query.filter(Region.id == region_id)
    regions = query.all()
    if not regions:
        raise LookupError("권역을 찾을 수 없습니다")
    return regions


def _serialize_failure_details(failures: list[DiscoveryRegionFailure]) -> str:
    return json.dumps(
        [failure.as_dict() for failure in failures],
        ensure_ascii=False,
        separators=(",", ":"),
    )


def _deserialize_failure_details(value: str | None) -> list[DiscoveryRegionFailureOut]:
    """Read only schema-valid failure rows so corrupt legacy data cannot break polling."""

    try:
        raw_items = json.loads(value or "[]")
    except (TypeError, json.JSONDecodeError):
        return []
    if not isinstance(raw_items, list):
        return []
    failures: list[DiscoveryRegionFailureOut] = []
    for raw_item in raw_items:
        try:
            failures.append(DiscoveryRegionFailureOut.model_validate(raw_item))
        except (TypeError, ValueError):
            continue
    return failures


def _discovery_run_out(run: BatchRun, job: DiscoveryJob) -> DiscoveryRunOut:
    return DiscoveryRunOut(
        run=BatchRunOut.model_validate(run),
        created_count=run.updated_count,
        duplicate_count=job.duplicate_count,
        invalid_count=job.invalid_count,
        failures=_deserialize_failure_details(job.failure_details),
    )


def _as_utc(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _active_discovery_lease(
    db: Session,
    *,
    now: datetime | None = None,
) -> int | None:
    """Lock and inspect the single global lease, reaping stale ownership.

    The caller owns the surrounding transaction and must commit or roll it
    back. That lets stale cleanup and a replacement lease be committed as one
    hand-off, while the unique active_slot constraint remains the final guard
    when two transactions both initially observe an empty slot.
    """

    job = (
        db.query(DiscoveryJob)
        .filter(DiscoveryJob.active_slot == ACTIVE_DISCOVERY_SLOT)
        .with_for_update()
        .first()
    )
    if job is None:
        return None

    run = db.get(BatchRun, job.batch_run_id)
    timestamp = now or datetime.now(timezone.utc)
    if run is None or run.kind != "place_discovery":
        job.active_slot = None
        db.flush()
        return None
    if run.status in TERMINAL_RUN_STATUSES:
        job.active_slot = None
        db.flush()
        return None
    if run.status not in ACTIVE_RUN_STATUSES:
        run.status = "failed"
        run.summary = "알 수 없는 실행 상태로 발굴 lease를 해제했습니다"
        run.finished_at = timestamp
        job.active_slot = None
        db.flush()
        return None

    lease_started_at = _as_utc(run.started_at) or _as_utc(job.created_at)
    if lease_started_at is not None and timestamp - lease_started_at > STALE_RUN_AFTER:
        run.status = "failed"
        run.summary = "30분 이상 응답이 없어 중단된 발굴 작업입니다. 다시 실행해 주세요"
        run.finished_at = timestamp
        job.active_slot = None
        db.flush()
        return None
    return run.id


def create_discovery_run(
    db: Session,
    *,
    region_id: int | None = None,
    limit: int = 80,
    trigger: str = "manual",
) -> DiscoveryRunOut:
    """Persist a run before any remote work so the API can return immediately."""

    limit = max(1, min(int(limit), MAX_CANDIDATES_PER_RUN))
    _discovery_regions(db, region_id)
    try:
        active_run_id = _active_discovery_lease(db)
        if active_run_id is not None:
            raise DiscoveryBusyError(active_run_id)

        run = BatchRun(
            kind="place_discovery",
            trigger=trigger,
            status="queued",
            summary="새 장소 발굴 작업이 대기 중입니다",
        )
        db.add(run)
        db.flush()
        job = DiscoveryJob(
            batch_run_id=run.id,
            region_id=region_id,
            active_slot=ACTIVE_DISCOVERY_SLOT,
            requested_limit=limit,
        )
        db.add(job)
        db.commit()
        db.refresh(run)
        db.refresh(job)
        return _discovery_run_out(run, job)
    except DiscoveryBusyError:
        db.rollback()
        raise
    except IntegrityError as exc:
        # Two callers may both observe an empty slot. The nullable unique
        # constraint chooses one winner; translate the loser's collision into
        # the same stable busy contract instead of leaking a database error.
        db.rollback()
        active_job = (
            db.query(DiscoveryJob)
            .filter(DiscoveryJob.active_slot == ACTIVE_DISCOVERY_SLOT)
            .first()
        )
        active_run_id = active_job.batch_run_id if active_job is not None else None
        db.rollback()
        if active_run_id is not None:
            raise DiscoveryBusyError(active_run_id) from exc
        raise


def get_discovery_run(db: Session, run_id: int) -> DiscoveryRunOut | None:
    # Polling doubles as a stale-worker reaper. End the lock transaction before
    # serializing the requested run so a waiting replacement can acquire it.
    _active_discovery_lease(db)
    db.commit()
    db.expire_all()
    job = db.get(DiscoveryJob, run_id)
    run = db.get(BatchRun, run_id)
    if job is None or run is None or run.kind != "place_discovery":
        return None
    return _discovery_run_out(run, job)


def _locked_scan_state(db: Session, region_id: int) -> DiscoveryScanState:
    state = (
        db.query(DiscoveryScanState)
        .filter(
            DiscoveryScanState.source == SOURCE,
            DiscoveryScanState.region_id == region_id,
        )
        .with_for_update()
        .first()
    )
    if state is not None:
        return state
    state = DiscoveryScanState(source=SOURCE, region_id=region_id)
    try:
        with db.begin_nested():
            db.add(state)
            db.flush()
        return state
    except IntegrityError:
        return (
            db.query(DiscoveryScanState)
            .filter(
                DiscoveryScanState.source == SOURCE,
                DiscoveryScanState.region_id == region_id,
            )
            .with_for_update()
            .one()
        )


def _advance_scan_state(
    state: DiscoveryScanState,
    *,
    requested_limit: int,
    result_count: int,
) -> None:
    state.scan_count += 1
    state.last_result_count = result_count
    if result_count >= requested_limit and requested_limit < MAX_OSM_ELEMENTS_PER_REGION:
        state.fetch_limit = min(
            MAX_OSM_ELEMENTS_PER_REGION,
            requested_limit + max(20, requested_limit // 2),
        )
        return
    # A short result exhausted this category phase. At the hard cap we also
    # rotate instead of querying the same leading objects forever.
    state.query_phase = (state.query_phase + 1) % QUERY_PHASE_COUNT
    state.fetch_limit = 0


def _claim_discovery_run(
    db: Session,
    run_id: int,
) -> tuple[BatchRun, DiscoveryJob, bool]:
    """Atomically move one leased run from queued to running.

    Returning claimed=False is intentional for duplicate background delivery:
    only the worker whose conditional UPDATE affected one row may call
    Overpass. A terminal run is also safe to deliver more than once.
    """

    active_run_id = _active_discovery_lease(db)
    db.commit()
    db.expire_all()

    job = db.get(DiscoveryJob, run_id)
    run = db.get(BatchRun, run_id)
    if job is None or run is None or run.kind != "place_discovery":
        raise LookupError("발굴 실행 이력을 찾을 수 없습니다")
    if run.status in TERMINAL_RUN_STATUSES:
        if job.active_slot is not None:
            job.active_slot = None
            db.commit()
        return run, job, False
    if active_run_id != run_id or job.active_slot != ACTIVE_DISCOVERY_SLOT:
        # A late background callback must not revive a run whose stale lease
        # has already been handed to another job.
        if run.status == "queued":
            run.status = "failed"
            run.summary = "실행 lease가 없어 발굴 작업을 시작하지 않았습니다"
            run.finished_at = datetime.now(timezone.utc)
            db.commit()
        return run, job, False

    timestamp = datetime.now(timezone.utc)
    claimed = (
        db.query(BatchRun)
        .filter(
            BatchRun.id == run_id,
            BatchRun.kind == "place_discovery",
            BatchRun.status == "queued",
        )
        .update(
            {
                BatchRun.status: "running",
                BatchRun.summary: "OpenStreetMap에서 검토 후보를 찾고 있습니다",
                BatchRun.started_at: timestamp,
            },
            synchronize_session=False,
        )
    )
    db.commit()
    db.expire_all()
    job = db.get(DiscoveryJob, run_id)
    run = db.get(BatchRun, run_id)
    if job is None or run is None:
        raise LookupError("발굴 실행 이력을 찾을 수 없습니다")
    return run, job, claimed == 1


def _locked_active_places_by_region(
    db: Session,
    region_ids: list[int],
) -> dict[int, list[Place]]:
    """Lock one global Place-ID sequence, then group it for duplicate checks."""

    ids = sorted(set(region_ids))
    grouped = {region_id: [] for region_id in ids}
    if not ids:
        return grouped
    rows = (
        db.query(Place)
        .filter(Place.region_id.in_(ids), Place.merged_into_id.is_(None))
        .order_by(Place.id)
        .populate_existing()
        .with_for_update()
        .all()
    )
    for row in rows:
        grouped[row.region_id].append(row)
    return grouped


def _execute_discovery_run(db: Session, run_id: int) -> DiscoveryRunOut:
    run, job, claimed = _claim_discovery_run(db, run_id)
    if not claimed:
        return _discovery_run_out(run, job)

    try:
        regions = _discovery_regions(db, job.region_id)
        region_ids = [region.id for region in regions]
        limit = max(1, min(int(job.requested_limit), MAX_CANDIDATES_PER_RUN))
        creation_allocations = _allocate_limits(regions, limit)
        fetch_regions = [region for region in regions if region.id in creation_allocations]
        scan_states = {region.id: _locked_scan_state(db, region.id) for region in fetch_regions}
        fetch_specs: dict[int, DiscoveryFetchSpec] = {}
        for region in fetch_regions:
            state = scan_states[region.id]
            minimum = max(20, creation_allocations[region.id] * 3)
            fetch_specs[region.id] = DiscoveryFetchSpec(
                region=_region_snapshot(region),
                limit=min(
                    MAX_OSM_ELEMENTS_PER_REGION,
                    max(minimum, state.fetch_limit),
                ),
                query_phase=state.query_phase % QUERY_PHASE_COUNT,
                scan_state_id=state.id,
                scan_query_phase=state.query_phase,
                scan_fetch_limit=state.fetch_limit,
                scan_count=state.scan_count,
            )

        # Provider calls can take minutes under overload. Commit the short scan
        # snapshot transaction before starting any network request or retry
        # backoff, releasing every DiscoveryScanState row lock. ORM Region rows
        # expire here, so workers receive only immutable scalar snapshots.
        db.commit()
        db.expire_all()
        if db.in_transaction():  # pragma: no cover - defensive SQLAlchemy invariant
            raise RuntimeError("provider fetch started with an open database transaction")

        raw_by_region: dict[int, list[dict]] = {}
        failures_by_region: dict[int, DiscoveryRegionFailure] = {}
        with ThreadPoolExecutor(max_workers=min(2, len(fetch_specs))) as executor:
            futures = {
                executor.submit(
                    _fetch_region_with_retry,
                    spec.region,
                    spec.limit,
                    spec.query_phase,
                ): spec
                for spec in fetch_specs.values()
            }
            for future in as_completed(futures):
                spec = futures[future]
                try:
                    raw, failure = future.result()
                    if failure is not None:
                        failures_by_region[spec.region.id] = failure
                        continue
                    raw_by_region[spec.region.id] = (raw or [])[:spec.limit]
                except Exception as exc:
                    failures_by_region[spec.region.id] = DiscoveryRegionFailure(
                        region_id=spec.region.id,
                        region_name=spec.region.name_ko,
                        attempts=1,
                        errors=tuple(
                            _fetch_errors_for_attempt(
                                exc,
                                query_phase=spec.query_phase,
                                attempt=1,
                            )
                        ),
                    )

        # Futures complete out of order; persist and display failures in the
        # same stable region order used by the discovery run.
        failures = [
            failures_by_region[region_id]
            for region_id in fetch_specs
            if region_id in failures_by_region
        ]

        # Re-enter the database only after all provider I/O and backoff ends.
        # Match the stale reaper's job -> run order and validate the lease before
        # touching scan state or candidate data. Holding these locks through the
        # final commit makes scan advancement and candidate writes atomic, while
        # a stale late worker returns without mutating either.
        with db.no_autoflush:
            job = (
                db.query(DiscoveryJob)
                .filter(DiscoveryJob.batch_run_id == run_id)
                .with_for_update()
                .populate_existing()
                .one()
            )
            run = (
                db.query(BatchRun)
                .filter(BatchRun.id == run_id)
                .with_for_update()
                .populate_existing()
                .one()
            )
        if job.active_slot != ACTIVE_DISCOVERY_SLOT or run.status != "running":
            db.rollback()
            db.expire_all()
            lost_job = db.get(DiscoveryJob, run_id)
            lost_run = db.get(BatchRun, run_id)
            if lost_job is None or lost_run is None:
                raise LookupError("발굴 실행 이력을 찾을 수 없습니다")
            return _discovery_run_out(lost_run, lost_job)

        regions = _discovery_regions(db, job.region_id)
        if [region.id for region in regions] != region_ids:
            raise RuntimeError("발굴 중 권역 구성이 변경되어 결과를 저장하지 않았습니다")

        successful_scan_states: dict[int, DiscoveryScanState] = {}
        for region_id, spec in fetch_specs.items():
            if region_id not in raw_by_region:
                continue
            state = _locked_scan_state(db, region_id)
            if (
                state.id != spec.scan_state_id
                or state.query_phase != spec.scan_query_phase
                or state.fetch_limit != spec.scan_fetch_limit
                or state.scan_count != spec.scan_count
            ):
                raise RuntimeError(
                    f"발굴 중 스캔 상태가 변경되어 결과를 저장하지 않았습니다: {spec.region.slug}"
                )
            successful_scan_states[region_id] = state

        for region_id, state in successful_scan_states.items():
            spec = fetch_specs[region_id]
            _advance_scan_state(
                state,
                requested_limit=spec.limit,
                result_count=len(raw_by_region[region_id]),
            )

        # Lock existing candidate rows before active Places in the same order
        # used by merge, then build duplicate links from a current snapshot. A
        # merge that wins first is visible; a waiting merge will re-scan links.
        existing_candidate_rows = db.query(DiscoveryCandidate).filter(
            DiscoveryCandidate.source == SOURCE
        ).order_by(DiscoveryCandidate.id).populate_existing().with_for_update().all()
        existing_candidates_by_source_id = {
            row.external_id: row
            for row in existing_candidate_rows
        }
        candidates_by_region = {
            region.id: [row for row in existing_candidate_rows if row.region_id == region.id]
            for region in regions
        }
        places_by_region = _locked_active_places_by_region(
            db, [region.id for region in regions]
        )
        scanned = 0
        created = 0
        duplicates = 0
        invalid = 0

        for region in regions:
            region_created = 0
            for element in raw_by_region.get(region.id, []):
                if region_created >= creation_allocations.get(region.id, 0):
                    break
                scanned += 1
                values = _candidate_values(region, element)
                if values is None:
                    invalid += 1
                    continue
                existing_candidate = existing_candidates_by_source_id.get(values.external_id)
                if existing_candidate is not None:
                    if (
                        existing_candidate.status == "rejected"
                        and (existing_candidate.decision_note or "").startswith("자동 상태 차단:")
                    ):
                        duplicate_place = find_duplicate_place(values, places_by_region[region.id])
                        next_status = "duplicate" if duplicate_place else "pending"
                        previous_status = existing_candidate.status
                        existing_candidate.discovery_run_id = run.id
                        existing_candidate.region_id = region.id
                        existing_candidate.source_url = values.source_url
                        existing_candidate.title = values.title
                        existing_candidate.local_name = values.local_name
                        existing_candidate.description = values.description
                        existing_candidate.area = values.area
                        existing_candidate.category = values.category
                        existing_candidate.lat = values.lat
                        existing_candidate.lng = values.lng
                        existing_candidate.confidence = values.confidence
                        existing_candidate.evidence = values.evidence
                        existing_candidate.tags = values.tags
                        existing_candidate.status = next_status
                        existing_candidate.duplicate_place_id = duplicate_place.id if duplicate_place else None
                        existing_candidate.result_place_id = None
                        existing_candidate.decision_note = "공개 지도에서 활성 상태를 다시 확인해 검토 대기로 복원했습니다"
                        existing_candidate.decided_by_id = None
                        existing_candidate.decided_at = None
                        db.add(DiscoveryDecision(
                            candidate_id=existing_candidate.id,
                            admin_id=None,
                            action="auto_reactivated",
                            from_status=previous_status,
                            to_status=next_status,
                            note=existing_candidate.decision_note,
                            place_id=duplicate_place.id if duplicate_place else None,
                        ))
                        if duplicate_place:
                            duplicates += 1
                        created += 1
                        region_created += 1
                        continue
                    # Refresh only legacy blank/English summaries when this
                    # exact source object is encountered again. A Korean text
                    # already reviewed by an administrator is never replaced.
                    if (
                        values.description
                        and not _HANGUL_PATTERN.search(existing_candidate.description or "")
                    ):
                        existing_candidate.description = values.description
                    duplicates += 1
                    continue
                if _candidate_duplicates(values, candidates_by_region[region.id]):
                    duplicates += 1
                    continue
                duplicate_place = find_duplicate_place(values, places_by_region[region.id])
                candidate_status = "duplicate" if duplicate_place else "pending"
                if duplicate_place:
                    duplicates += 1
                candidate = DiscoveryCandidate(
                    discovery_run_id=run.id,
                    region_id=region.id,
                    source=SOURCE,
                    external_id=values.external_id,
                    source_url=values.source_url,
                    title=values.title,
                    local_name=values.local_name,
                    description=values.description,
                    area=values.area,
                    category=values.category,
                    lat=values.lat,
                    lng=values.lng,
                    confidence=values.confidence,
                    evidence=values.evidence,
                    tags=values.tags,
                    status=candidate_status,
                    duplicate_place_id=duplicate_place.id if duplicate_place else None,
                )
                try:
                    with db.begin_nested():
                        db.add(candidate)
                        db.flush()
                except IntegrityError:
                    # Another overlapping run stored this source object first.
                    duplicates += 1
                    continue
                candidates_by_region[region.id].append(candidate)
                existing_candidates_by_source_id[values.external_id] = candidate
                created += 1
                region_created += 1

        final_status = "success" if not failures else ("partial" if raw_by_region else "failed")
        final_summary = (
            f"OSM {scanned}건 조회, 후보 {created}건 저장, 중복 {duplicates}건, 제외 {invalid}건"
        )
        if failures:
            failure_labels: list[str] = []
            for failure in failures:
                last_error = failure.errors[-1] if failure.errors else None
                if last_error is None:
                    reason = "UnknownError"
                elif last_error.status_code is not None:
                    reason = f"{last_error.error_type} HTTP {last_error.status_code}"
                else:
                    reason = last_error.error_type
                failure_labels.append(
                    f"{failure.region_name}: {reason} ({failure.attempts}회 시도)"
                )
            final_summary += "; 실패 " + ", ".join(failure_labels)

        run.scanned_count = scanned
        run.updated_count = created
        job.duplicate_count = duplicates
        job.invalid_count = invalid
        job.failure_details = _serialize_failure_details(failures)
        run.status = final_status
        run.summary = final_summary
        run.finished_at = datetime.now(timezone.utc)
        job.active_slot = None
        db.commit()
        db.refresh(run)
        db.refresh(job)
        return _discovery_run_out(run, job)
    except Exception as exc:
        db.rollback()
        with db.no_autoflush:
            failed_job = (
                db.query(DiscoveryJob)
                .filter(DiscoveryJob.batch_run_id == run_id)
                .with_for_update()
                .first()
            )
            failed_run = (
                db.query(BatchRun)
                .filter(BatchRun.id == run_id)
                .with_for_update()
                .first()
            )
        if failed_run is not None and failed_job is not None:
            if failed_run.status in ACTIVE_RUN_STATUSES:
                failed_run.status = "failed"
                failed_run.summary = f"새 장소 발굴 실패: {type(exc).__name__}"
                failed_run.finished_at = datetime.now(timezone.utc)
            if failed_job.active_slot == ACTIVE_DISCOVERY_SLOT:
                failed_job.active_slot = None
            db.commit()
        raise


def prepare_discovery_retry(db: Session, run_id: int) -> DiscoveryRunOut:
    """Safely re-queue the same run before a Step Functions task retry.

    Infrastructure retries can arrive after a Fargate task was stopped while
    the database still says ``queued``/``running``, or after a provider failure
    was finalized as ``failed``. A successful/partial run is never reopened and
    another run's global lease is never stolen.
    """

    try:
        active_job = (
            db.query(DiscoveryJob)
            .filter(DiscoveryJob.active_slot == ACTIVE_DISCOVERY_SLOT)
            .with_for_update()
            .first()
        )
        if active_job is not None and active_job.batch_run_id != run_id:
            raise DiscoveryBusyError(active_job.batch_run_id)

        job = (
            db.query(DiscoveryJob)
            .filter(DiscoveryJob.batch_run_id == run_id)
            .with_for_update()
            .first()
        )
        run = (
            db.query(BatchRun)
            .filter(BatchRun.id == run_id, BatchRun.kind == "place_discovery")
            .with_for_update()
            .first()
        )
        if job is None or run is None:
            raise LookupError("발굴 실행 이력을 찾을 수 없습니다")
        if run.status in {"success", "partial"}:
            db.commit()
            return _discovery_run_out(run, job)
        if run.status not in {"queued", "running", "failed"}:
            raise RuntimeError(f"재시도할 수 없는 발굴 상태입니다: {run.status}")

        job.active_slot = ACTIVE_DISCOVERY_SLOT
        job.duplicate_count = 0
        job.invalid_count = 0
        job.failure_details = "[]"
        run.status = "queued"
        run.scanned_count = 0
        run.updated_count = 0
        run.summary = "내구성 워크플로가 장소 발굴 작업을 재시도합니다"
        # A workflow retry renews the lease. Keeping the original timestamp
        # would make a run that timed out after 30 minutes become stale again
        # before the replacement Fargate task can claim it.
        run.started_at = datetime.now(timezone.utc)
        run.finished_at = None
        db.commit()
        db.refresh(run)
        db.refresh(job)
        return _discovery_run_out(run, job)
    except Exception:
        db.rollback()
        raise


def finalize_discovery_dispatch_failure(
    db: Session,
    run_id: int,
    *,
    summary: str = "내구성 발굴 워크플로를 시작하지 못했습니다",
) -> DiscoveryRunOut:
    """Release a queued run when Step Functions could not be dispatched.

    This is deliberately conditional: an ambiguous HTTP timeout can race with
    a worker that actually started, and the API must never overwrite a run
    that has already finished.
    """

    try:
        job = (
            db.query(DiscoveryJob)
            .filter(DiscoveryJob.batch_run_id == run_id)
            .with_for_update()
            .first()
        )
        run = (
            db.query(BatchRun)
            .filter(BatchRun.id == run_id, BatchRun.kind == "place_discovery")
            .with_for_update()
            .first()
        )
        if job is None or run is None:
            raise LookupError("발굴 실행 이력을 찾을 수 없습니다")
        if run.status == "queued":
            run.status = "failed"
            run.summary = summary
            run.finished_at = datetime.now(timezone.utc)
            if job.active_slot == ACTIVE_DISCOVERY_SLOT:
                job.active_slot = None
            db.commit()
            db.refresh(run)
            db.refresh(job)
        else:
            db.rollback()
        return _discovery_run_out(run, job)
    except Exception:
        db.rollback()
        raise


def execute_discovery_run(db: Session, run_id: int) -> DiscoveryRunOut:
    """Execute one pre-created run and preserve failures for workflow polling."""

    return _execute_discovery_run(db, run_id)


def execute_queued_discovery(run_id: int) -> None:
    """FastAPI background-task entry point with an independent DB session."""

    with SessionLocal() as db:
        try:
            execute_discovery_run(db, run_id)
        except Exception:
            # _execute_discovery_run already persisted a concise failed state.
            return


def run_discovery(
    db: Session,
    *,
    region_id: int | None = None,
    limit: int = 80,
    trigger: str = "manual",
) -> DiscoveryRunOut:
    """Synchronous CLI/scheduled entry point; the HTTP API queues instead."""

    queued = create_discovery_run(
        db,
        region_id=region_id,
        limit=limit,
        trigger=trigger,
    )
    return _execute_discovery_run(db, queued.run.id)


def _effective_candidate_description(candidate: DiscoveryCandidate) -> str:
    """Return a Korean display/publish fallback for legacy candidate rows."""

    stored = _clean_text(candidate.description, 5000)
    if _HANGUL_PATTERN.search(stored):
        return stored
    try:
        evidence = json.loads(candidate.evidence or "{}")
    except (TypeError, json.JSONDecodeError):
        evidence = {}
    raw_tags = evidence.get("osm_tags") if isinstance(evidence, dict) else None
    tags = (
        {str(key): str(value) for key, value in raw_tags.items() if value is not None}
        if isinstance(raw_tags, dict)
        else {}
    )
    source_labels = {
        "openstreetmap": "OpenStreetMap",
        "wikidata": "Wikidata",
    }
    source_label = source_labels.get(
        str(candidate.source or "").strip().casefold(),
        "공개 출처",
    )
    return _candidate_description_ko(
        candidate.region,
        tags,
        candidate.category,
        source_label=source_label,
    )


def _values_from_candidate(candidate: DiscoveryCandidate) -> CandidateValues:
    return CandidateValues(
        external_id=candidate.external_id,
        source_url=candidate.source_url,
        title=candidate.title,
        local_name=candidate.local_name,
        description=_effective_candidate_description(candidate),
        area=candidate.area,
        category=candidate.category,
        lat=candidate.lat,
        lng=candidate.lng,
        confidence=candidate.confidence,
        evidence=candidate.evidence,
        tags=candidate.tags,
    )


def _candidate_query(db: Session):
    return db.query(DiscoveryCandidate).options(
        joinedload(DiscoveryCandidate.region),
        joinedload(DiscoveryCandidate.decided_by),
        joinedload(DiscoveryCandidate.decisions).joinedload(DiscoveryDecision.admin),
    )


def get_candidate(db: Session, candidate_id: int) -> DiscoveryCandidate | None:
    return _candidate_query(db).filter(DiscoveryCandidate.id == candidate_id).first()


def candidate_out(candidate: DiscoveryCandidate) -> DiscoveryCandidateOut:
    return DiscoveryCandidateOut(
        id=candidate.id,
        discovery_run_id=candidate.discovery_run_id,
        region_id=candidate.region_id,
        region_name=candidate.region.name_ko,
        island=candidate.region.island,
        source=candidate.source,
        external_id=candidate.external_id,
        source_url=candidate.source_url,
        title=candidate.title,
        local_name=candidate.local_name,
        description=_effective_candidate_description(candidate),
        area=candidate.area,
        category=candidate.category,
        lat=candidate.lat,
        lng=candidate.lng,
        confidence=candidate.confidence,
        evidence=candidate.evidence,
        tags=[item.strip() for item in (candidate.tags or "").split(",") if item.strip()],
        status=candidate.status,
        duplicate_place_id=candidate.duplicate_place_id,
        result_place_id=candidate.result_place_id,
        decision_note=candidate.decision_note,
        decided_by_email=candidate.decided_by.email if candidate.decided_by else None,
        decided_at=candidate.decided_at,
        decision_history=[
            DiscoveryDecisionOut(
                id=decision.id,
                action=decision.action,
                from_status=decision.from_status,
                to_status=decision.to_status,
                note=decision.note,
                place_id=decision.place_id,
                admin_email=decision.admin.email if decision.admin else None,
                created_at=decision.created_at,
            )
            for decision in candidate.decisions
        ],
        created_at=candidate.created_at,
        updated_at=candidate.updated_at,
    )


def list_candidates(
    db: Session,
    *,
    status: str | None = None,
    region_id: int | None = None,
    limit: int = 100,
) -> list[DiscoveryCandidateOut]:
    query = _candidate_query(db)
    if status:
        query = query.filter(DiscoveryCandidate.status == status)
    if region_id:
        query = query.filter(DiscoveryCandidate.region_id == region_id)
    rows = query.order_by(DiscoveryCandidate.created_at.desc(), DiscoveryCandidate.id.desc()).limit(
        max(1, min(limit, 200))
    ).all()
    return [candidate_out(row) for row in rows]


def _append_decision(
    db: Session,
    candidate: DiscoveryCandidate,
    admin: User,
    *,
    action: str,
    from_status: str,
    to_status: str,
    note: str,
    place_id: int | None,
) -> None:
    db.add(DiscoveryDecision(
        candidate_id=candidate.id,
        admin_id=admin.id,
        action=action,
        from_status=from_status,
        to_status=to_status,
        note=note,
        place_id=place_id,
    ))


def approve_candidate(
    db: Session,
    candidate_id: int,
    admin: User,
    *,
    note: str = "",
    force: bool = False,
    commit: bool = True,
) -> DiscoveryCandidateOut:
    candidate = (
        db.query(DiscoveryCandidate)
        .filter(DiscoveryCandidate.id == candidate_id)
        .populate_existing()
        .with_for_update()
        .first()
    )
    if candidate is None:
        raise LookupError("후보를 찾을 수 없습니다")
    if candidate.status not in {"pending", "duplicate"}:
        raise ValueError("대기 또는 중복 후보만 승인할 수 있습니다")
    inactive_reason = _candidate_inactive_reason(candidate)
    if inactive_reason:
        raise ValueError(
            "공개 지도에 폐업·철거 등 비활성 신호가 있어 승인할 수 없습니다: "
            + inactive_reason
        )
    try:
        live_inactive_reason, live_evidence = revalidate_candidate_lifecycle(candidate)
    except Exception as exc:
        raise ValueError(
            "최신 운영 상태를 확인하지 못해 승인하지 않았습니다. 잠시 후 다시 시도해 주세요"
        ) from exc
    try:
        evidence = json.loads(candidate.evidence or "{}")
    except (TypeError, json.JSONDecodeError):
        evidence = {}
    evidence = evidence if isinstance(evidence, dict) else {}
    evidence["approval_revalidation"] = live_evidence
    candidate.evidence = json.dumps(evidence, ensure_ascii=False, separators=(",", ":"))
    if live_inactive_reason:
        previous = candidate.status
        candidate.status = "rejected"
        candidate.decision_note = "자동 상태 차단: " + live_inactive_reason
        candidate.decided_by_id = admin.id
        candidate.decided_at = datetime.now(timezone.utc)
        _append_decision(
            db,
            candidate,
            admin,
            action="approval_blocked_inactive",
            from_status=previous,
            to_status="rejected",
            note=candidate.decision_note,
            place_id=None,
        )
        if commit:
            db.commit()
        else:
            db.flush()
        raise CandidateInactiveError(
            "공식 원문에 비활성 또는 장소 식별 변경 신호가 있어 승인할 수 없습니다: "
            + live_inactive_reason
        )

    verified_coordinate = live_evidence.get("verified_coordinate") if isinstance(live_evidence, dict) else None
    if isinstance(verified_coordinate, dict) and verified_coordinate.get("verified"):
        candidate.lat = float(verified_coordinate["lat"])
        candidate.lng = float(verified_coordinate["lng"])

    values = _values_from_candidate(candidate)
    # Serialize approvals within one travel region. Candidate row locks alone
    # cannot protect an empty duplicate-search range when two different source
    # objects for the same place are approved concurrently.
    region = (
        db.query(Region)
        .filter(Region.id == candidate.region_id)
        .with_for_update()
        .one()
    )
    places = db.query(Place).filter(
        Place.region_id == candidate.region_id,
        Place.merged_into_id.is_(None),
    ).order_by(Place.id).populate_existing().with_for_update().all()
    duplicate = find_duplicate_place(values, places)
    if duplicate and not force:
        previous = candidate.status
        candidate.status = "duplicate"
        candidate.duplicate_place_id = duplicate.id
        candidate.decision_note = note or "기존 장소와 중복되어 승인을 보류했습니다"
        candidate.decided_by_id = admin.id
        candidate.decided_at = datetime.now(timezone.utc)
        if previous != "duplicate":
            _append_decision(
                db, candidate, admin,
                action="duplicate_detected", from_status=previous, to_status="duplicate",
                note=candidate.decision_note, place_id=duplicate.id,
            )
        if commit:
            db.commit()
        else:
            db.flush()
        return candidate_out(get_candidate(db, candidate.id))

    category_defaults = {
        "beach": 150, "culture": 90, "nature": 150, "food": 90, "cafe": 75,
        "surf": 180, "dive": 180, "wellness": 120, "nightlife": 120,
        "transport": 60, "stay": 60, "shop": 75, "other": 90,
    }
    water_sensitive = candidate.category in {"beach", "surf", "dive"}
    weather_sensitive = candidate.category in {"beach", "nature", "surf", "dive"}
    place = Place(
        region_id=candidate.region_id,
        creator_id=admin.id,
        category=candidate.category,
        title=candidate.title,
        local_name=candidate.local_name,
        description=_effective_candidate_description(candidate),
        area=candidate.area,
        lat=candidate.lat,
        lng=candidate.lng,
        duration_minutes=category_defaults.get(candidate.category, 90),
        budget_level=1,
        best_time="",
        access_type="road",
        booking_required=False,
        weather_sensitive=weather_sensitive,
        tide_sensitive=water_sensitive,
        ferry_sensitive="ferry" in region.transport_mode or region.kind in {"island", "small_island"},
        traveler_note="자동 발굴된 장소입니다. 방문 전 최신 영업·접근·안전 정보를 원문에서 확인하세요.",
        tags=candidate.tags,
        source_url=candidate.source_url,
        coordinate_source=(candidate.source or "public_source")[:60],
        coordinate_external_id=candidate.external_id,
        coordinate_confidence=max(0.0, min(candidate.confidence, 1.0)),
        coordinate_verified_at=(
            datetime.now(timezone.utc)
            if isinstance(verified_coordinate, dict) and verified_coordinate.get("verified")
            else None
        ),
        coordinate_crs="WGS84",
    )
    db.add(place)
    db.flush()
    record_place_change_event(
        db,
        place_id=place.id,
        actor_id=admin.id,
        event_type="place_created",
        summary="관리자가 자동 발굴 후보를 승인했습니다",
        metadata={"source": candidate.source or SOURCE, "candidate_id": candidate.id},
    )
    previous = candidate.status
    candidate.status = "approved"
    candidate.result_place_id = place.id
    candidate.decision_note = note
    candidate.decided_by_id = admin.id
    candidate.decided_at = datetime.now(timezone.utc)
    _append_decision(
        db, candidate, admin,
        action="approved_forced" if force and duplicate else "approved",
        from_status=previous, to_status="approved", note=note, place_id=place.id,
    )
    if commit:
        db.commit()
    else:
        db.flush()
    return candidate_out(get_candidate(db, candidate.id))


def reject_candidate(
    db: Session,
    candidate_id: int,
    admin: User,
    *,
    note: str = "",
) -> DiscoveryCandidateOut:
    candidate = (
        db.query(DiscoveryCandidate)
        .filter(DiscoveryCandidate.id == candidate_id)
        .with_for_update()
        .first()
    )
    if candidate is None:
        raise LookupError("후보를 찾을 수 없습니다")
    if candidate.status not in {"pending", "duplicate"}:
        raise ValueError("대기 또는 중복 후보만 반려할 수 있습니다")
    previous = candidate.status
    candidate.status = "rejected"
    candidate.decision_note = note
    candidate.decided_by_id = admin.id
    candidate.decided_at = datetime.now(timezone.utc)
    _append_decision(
        db, candidate, admin,
        action="rejected", from_status=previous, to_status="rejected", note=note, place_id=None,
    )
    db.commit()
    return candidate_out(get_candidate(db, candidate.id))


def main() -> None:
    parser = argparse.ArgumentParser(description="Discover Bali-area place candidates from OpenStreetMap")
    parser.add_argument("--region", help="region slug; omit to scan all configured regions")
    parser.add_argument("--limit", type=_cli_limit, default=80)
    args = parser.parse_args()
    run_migrations(engine)
    with SessionLocal() as db:
        region_id = None
        if args.region:
            region = db.query(Region).filter(Region.slug == args.region).first()
            if region is None:
                raise SystemExit(f"unknown region slug: {args.region}")
            region_id = region.id
        try:
            result = run_discovery(db, region_id=region_id, limit=args.limit, trigger="cli")
        except DiscoveryBusyError as exc:
            raise SystemExit(str(exc)) from exc
        print(result.model_dump_json())
        if result.run.status == "failed":
            raise SystemExit(1)


if __name__ == "__main__":
    main()
