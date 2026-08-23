from __future__ import annotations

import asyncio
import json
import math
import re
import unicodedata
from dataclasses import dataclass, replace
from typing import Any, Awaitable, Callable, Mapping, Sequence
from urllib.parse import urlencode
from urllib.request import Request, urlopen


NOMINATIM_SEARCH_URL = "https://nominatim.openstreetmap.org/search"
WIKIDATA_API_URL = "https://www.wikidata.org/w/api.php"
ARCGIS_SEARCH_URL = (
    "https://geocode.arcgis.com/arcgis/rest/services/World/GeocodeServer/"
    "findAddressCandidates"
)
DEFAULT_USER_AGENT = (
    "PATRA-Bali-place-search/1.0 "
    "(https://github.com/juranikr/cloudbali; travel-planner geocoder)"
)
DEFAULT_TIMEOUT_SECONDS = 4.0
DEFAULT_PER_SOURCE_LIMIT = 5
DEFAULT_RESULT_LIMIT = 10
MAX_TIMEOUT_SECONDS = 8.0
MAX_PER_SOURCE_LIMIT = 8
MAX_RESULT_LIMIT = 12
MAX_RESPONSE_BYTES = 1_500_000

# ArcGIS can also return administrative areas, postcodes, street centroids,
# intersections, and coordinate-system matches.  Those are useful in a general
# geocoder, but are too broad for a travel-place result and can create convincing
# false positives.  Keep only a named POI or a full/sub-address match.
ARCGIS_ALLOWED_MATCH_TYPES = frozenset(
    {
        "poi",
        "subaddress",
        "pointaddress",
        "streetaddress",
        "streetaddressext",
    }
)
ARCGIS_GENERIC_QUERY_TERMS = frozenset(
    {
        "address",
        "airport",
        "bali",
        "bar",
        "beach",
        "cafe",
        "dive",
        "food",
        "gili",
        "harbor",
        "harbour",
        "hotel",
        "indonesia",
        "jalan",
        "lombok",
        "museum",
        "nusa",
        "pantai",
        "place",
        "pura",
        "resort",
        "restaurant",
        "shop",
        "spa",
        "stay",
        "surf",
        "temple",
        "villa",
        "warung",
        "waterfall",
        "yoga",
        "공항",
        "다이빙",
        "리조트",
        "맛집",
        "박물관",
        "비치",
        "사원",
        "서핑",
        "숙소",
        "식당",
        "요가",
        "카페",
        "폭포",
        "해변",
        "호텔",
    }
)


@dataclass(frozen=True, slots=True)
class GeoBounds:
    """A WGS84 rectangle. Coordinate order is deliberately explicit."""

    west: float
    south: float
    east: float
    north: float
    name: str = "requested"

    def __post_init__(self) -> None:
        if not (-180 <= self.west < self.east <= 180):
            raise ValueError("invalid west/east bounds")
        if not (-90 <= self.south < self.north <= 90):
            raise ValueError("invalid south/north bounds")

    def contains(self, lat: float, lng: float) -> bool:
        return self.south <= lat <= self.north and self.west <= lng <= self.east

    def intersects(self, other: "GeoBounds") -> bool:
        return not (
            self.east < other.west
            or other.east < self.west
            or self.north < other.south
            or other.north < self.south
        )


# These intentionally form a union rather than one large rectangle. A broad
# Bali-to-Lombok box also covers parts of East Java and western Sumbawa, which
# must never leak into a travel search presented as local to this app.
SUPPORTED_AREA_BOUNDS: tuple[GeoBounds, ...] = (
    GeoBounds(114.42, -8.96, 115.74, -7.90, "bali"),
    GeoBounds(115.35, -8.90, 115.92, -8.45, "nusa-islands"),
    GeoBounds(115.79, -9.18, 116.72, -8.15, "lombok"),
    GeoBounds(115.98, -8.42, 116.13, -8.29, "gili-islands"),
)


@dataclass(frozen=True, slots=True)
class ExternalSearchHit:
    key: str
    source: str
    title: str
    display_name: str
    lat: float
    lng: float
    category: str
    external_id: str
    source_url: str
    coordinate_source: str
    confidence: float
    cross_checked: bool = False
    sources: tuple[str, ...] = ()
    external_ids: tuple[tuple[str, str], ...] = ()
    source_urls: tuple[str, ...] = ()
    storage_allowed: bool = True
    license: str = ""
    attribution: str = ""

    def as_dict(self) -> dict[str, Any]:
        """Return a JSON/Pydantic-friendly representation for API callers."""

        return {
            "key": self.key,
            "source": self.source,
            "title": self.title,
            "display_name": self.display_name,
            "lat": self.lat,
            "lng": self.lng,
            "category": self.category,
            "external_id": self.external_id,
            "source_url": self.source_url,
            "coordinate_source": self.coordinate_source,
            "confidence": self.confidence,
            "cross_checked": self.cross_checked,
            "sources": list(self.sources or (self.source,)),
            "external_ids": dict(self.external_ids),
            "source_urls": list(self.source_urls or (self.source_url,)),
            "storage_allowed": self.storage_allowed,
            "license": self.license,
            "attribution": self.attribution,
        }


JsonFetcher = Callable[
    [str, Mapping[str, str], Mapping[str, str], float],
    Awaitable[Any],
]


def is_supported_coordinate(
    lat: float,
    lng: float,
    *,
    bounds: GeoBounds | None = None,
) -> bool:
    """Return true only inside the supported island union and caller bounds."""

    if not (math.isfinite(lat) and math.isfinite(lng)):
        return False
    if bounds is not None and not bounds.contains(lat, lng):
        return False
    return any(area.contains(lat, lng) for area in SUPPORTED_AREA_BOUNDS)


def _blocking_fetch_json(
    url: str,
    params: Mapping[str, str],
    headers: Mapping[str, str],
    timeout_seconds: float,
) -> Any:
    query = urlencode(params)
    request = Request(
        url + ("&" if "?" in url else "?") + query,
        headers=dict(headers),
    )
    with urlopen(request, timeout=timeout_seconds) as response:
        payload = response.read(MAX_RESPONSE_BYTES + 1)
    if len(payload) > MAX_RESPONSE_BYTES:
        raise ValueError("external search response is too large")
    return json.loads(payload.decode("utf-8"))


async def _default_fetch_json(
    url: str,
    params: Mapping[str, str],
    headers: Mapping[str, str],
    timeout_seconds: float,
) -> Any:
    return await asyncio.to_thread(
        _blocking_fetch_json,
        url,
        params,
        headers,
        timeout_seconds,
    )


async def _request_json(
    fetch_json: JsonFetcher,
    url: str,
    params: Mapping[str, str],
    headers: Mapping[str, str],
    timeout_seconds: float,
) -> Any:
    return await asyncio.wait_for(
        fetch_json(url, params, headers, timeout_seconds),
        timeout=timeout_seconds + 0.25,
    )


def _viewbox(bounds: GeoBounds | None) -> str:
    selected = _search_bounds(bounds)
    # Nominatim expects left,top,right,bottom rather than west,south,east,north.
    return f"{selected.west},{selected.north},{selected.east},{selected.south}"


def _search_bounds(bounds: GeoBounds | None) -> GeoBounds:
    if bounds is not None:
        return bounds
    return GeoBounds(
        min(item.west for item in SUPPORTED_AREA_BOUNDS),
        min(item.south for item in SUPPORTED_AREA_BOUNDS),
        max(item.east for item in SUPPORTED_AREA_BOUNDS),
        max(item.north for item in SUPPORTED_AREA_BOUNDS),
        "supported-overall",
    )


def _arcgis_extent(bounds: GeoBounds | None) -> str:
    selected = _search_bounds(bounds)
    # ArcGIS uses lower-left,upper-right (west,south,east,north) in WGS84.
    return f"{selected.west},{selected.south},{selected.east},{selected.north}"


def _arcgis_search_text(query: str, bounds: GeoBounds | None) -> str:
    normalized_query = _normal_name(query)
    context = ""
    if bounds is not None and bounds.name not in {"", "requested"}:
        context = re.sub(r"[-_]+", " ", bounds.name).strip()
    elif any(
        token in normalized_query
        for token in ("lombok", "gili", "trawangan", "롬복", "길리")
    ):
        context = "Lombok"
    else:
        context = "Bali"
    parts = [query]
    if context and _normal_name(context) not in normalized_query:
        parts.append(context)
    if "indonesia" not in normalized_query and "인도네시아" not in normalized_query:
        parts.append("Indonesia")
    return ", ".join(parts)


def _safe_float(value: Any) -> float | None:
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    return result if math.isfinite(result) else None


def _clean_text(value: Any, limit: int = 500) -> str:
    return " ".join(str(value or "").split())[:limit]


def _category(*values: Any) -> str:
    text = " ".join(_clean_text(value).lower() for value in values)
    rules: tuple[tuple[str, tuple[str, ...]], ...] = (
        ("transport", ("airport", "harbour", "harbor", "port", "terminal", "ferry", "pelabuhan")),
        ("surf", ("surf", "wave break")),
        ("dive", ("dive", "diving", "snorkel", "reef")),
        ("beach", ("beach", "pantai", "coast", "bay", "shore")),
        ("food", ("restaurant", "cafe", "coffee", "warung", "bar", "food")),
        ("stay", ("hotel", "resort", "villa", "hostel", "lodging", "guest house")),
        ("culture", ("temple", "pura", "museum", "palace", "monument", "historic", "culture")),
        ("nature", ("waterfall", "mountain", "volcano", "forest", "park", "garden", "nature")),
        ("wellness", ("spa", "yoga", "wellness", "massage")),
        ("shop", ("market", "mall", "shop", "boutique")),
    )
    for category, keywords in rules:
        if any(keyword in text for keyword in keywords):
            return category
    return "other"


def _normal_name(value: str) -> str:
    folded = unicodedata.normalize("NFKD", value).casefold()
    return "".join(character for character in folded if character.isalnum())


def _arcgis_query_matches(query: str, *candidate_values: str) -> bool:
    """Reject high-scoring ArcGIS fallbacks unrelated to the requested entity."""

    candidate = _normal_name(" ".join(candidate_values))
    normalized_query = _normal_name(query)
    if not candidate or not normalized_query:
        return False
    if normalized_query in candidate or candidate in normalized_query:
        return True
    terms = [
        _normal_name(term)
        for term in re.findall(r"[^\W_]+", query, flags=re.UNICODE)
    ]
    distinctive = [
        term
        for term in terms
        if len(term) >= 3 and term not in ARCGIS_GENERIC_QUERY_TERMS
    ]
    # Generic searches such as "beach" or "호텔" intentionally ask for a
    # category, so provider category filtering is the appropriate relevance
    # check.  For a named entity, its longest distinctive term must survive.
    return not distinctive or max(distinctive, key=len) in candidate


def _distance_m(left: ExternalSearchHit, right: ExternalSearchHit) -> float:
    radius_m = 6_371_000.0
    lat1, lat2 = math.radians(left.lat), math.radians(right.lat)
    delta_lat = lat2 - lat1
    delta_lng = math.radians(right.lng - left.lng)
    value = (
        math.sin(delta_lat / 2) ** 2
        + math.cos(lat1) * math.cos(lat2) * math.sin(delta_lng / 2) ** 2
    )
    return radius_m * 2 * math.atan2(math.sqrt(value), math.sqrt(max(0.0, 1 - value)))


def _same_place(left: ExternalSearchHit, right: ExternalSearchHit) -> bool:
    if left.source == right.source:
        return False
    distance = _distance_m(left, right)
    left_name = _normal_name(left.title)
    right_name = _normal_name(right.title)
    if not left_name or not right_name:
        return False
    names_overlap = left_name in right_name or right_name in left_name
    return distance <= 140 and names_overlap


def _merge_hit(left: ExternalSearchHit, right: ExternalSearchHit) -> ExternalSearchHit:
    # An anonymous ArcGIS request is explicitly non-stored.  It may corroborate
    # a coordinate, but must not become the representative value when an ODbL or
    # CC0 result is available.  A merged hit remains storable only through that
    # independently storable representative.
    storable = [item for item in (left, right) if item.storage_allowed]
    preferred = max(storable or [left, right], key=lambda item: item.confidence)
    sources = tuple(dict.fromkeys((preferred.source, *left.sources, left.source, *right.sources, right.source)))
    external_ids = tuple(dict((*left.external_ids, *right.external_ids)).items())
    source_urls = tuple(dict.fromkeys((
        preferred.source_url,
        *left.source_urls,
        left.source_url,
        *right.source_urls,
        right.source_url,
    )))
    license_names = " + ".join(dict.fromkeys(item for item in (left.license, right.license) if item))
    attributions = " · ".join(dict.fromkeys(item for item in (left.attribution, right.attribution) if item))
    return replace(
        preferred,
        coordinate_source="+".join(sources),
        confidence=round(min(0.97, max(left.confidence, right.confidence) + 0.11), 3),
        cross_checked=True,
        sources=sources,
        external_ids=external_ids,
        source_urls=source_urls,
        storage_allowed=bool(storable),
        license=license_names,
        attribution=attributions,
    )


def _merge_cross_checked(hits: Sequence[ExternalSearchHit]) -> list[ExternalSearchHit]:
    merged: list[ExternalSearchHit] = []
    for hit in sorted(hits, key=lambda item: item.confidence, reverse=True):
        match_index = next(
            (index for index, candidate in enumerate(merged) if _same_place(candidate, hit)),
            None,
        )
        if match_index is None:
            merged.append(hit)
        else:
            merged[match_index] = _merge_hit(merged[match_index], hit)
    return sorted(
        merged,
        key=lambda item: (
            item.cross_checked,
            item.storage_allowed,
            item.confidence,
            item.title.casefold(),
        ),
        reverse=True,
    )


def _osm_identity(item: Mapping[str, Any]) -> tuple[str, str] | None:
    osm_type = _clean_text(item.get("osm_type"), 16).lower()
    osm_type = {"n": "node", "w": "way", "r": "relation"}.get(osm_type, osm_type)
    osm_id = _clean_text(item.get("osm_id"), 30)
    if osm_type in {"node", "way", "relation"} and osm_id.isdigit():
        external_id = f"{osm_type}/{osm_id}"
        return external_id, f"https://www.openstreetmap.org/{external_id}"
    place_id = _clean_text(item.get("place_id"), 30)
    if place_id.isdigit():
        return (
            f"nominatim/{place_id}",
            f"https://nominatim.openstreetmap.org/ui/details.html?place_id={place_id}",
        )
    return None


async def _nominatim_hits(
    query: str,
    *,
    bounds: GeoBounds | None,
    limit: int,
    headers: Mapping[str, str],
    timeout_seconds: float,
    fetch_json: JsonFetcher,
) -> list[ExternalSearchHit]:
    payload = await _request_json(
        fetch_json,
        NOMINATIM_SEARCH_URL,
        {
            "q": query,
            "format": "jsonv2",
            "limit": str(min(MAX_PER_SOURCE_LIMIT, limit * 2)),
            "countrycodes": "id",
            "accept-language": "ko,en,id",
            "addressdetails": "1",
            "namedetails": "1",
            "extratags": "1",
            "viewbox": _viewbox(bounds),
            "bounded": "1",
        },
        headers,
        timeout_seconds,
    )
    if not isinstance(payload, list):
        return []
    results: list[ExternalSearchHit] = []
    for item in payload:
        if not isinstance(item, Mapping):
            continue
        lat = _safe_float(item.get("lat"))
        lng = _safe_float(item.get("lon"))
        identity = _osm_identity(item)
        if lat is None or lng is None or identity is None:
            continue
        if not is_supported_coordinate(lat, lng, bounds=bounds):
            continue
        namedetails = item.get("namedetails") if isinstance(item.get("namedetails"), Mapping) else {}
        address = item.get("address") if isinstance(item.get("address"), Mapping) else {}
        title = _clean_text(
            namedetails.get("name:ko")
            or namedetails.get("name")
            or namedetails.get("name:en")
            or item.get("name")
            or _clean_text(item.get("display_name")).split(",")[0],
            200,
        )
        if not title:
            continue
        importance = _safe_float(item.get("importance")) or 0.0
        query_name = _normal_name(query)
        confidence = 0.62 + min(1.0, max(0.0, importance)) * 0.22
        if query_name and query_name in _normal_name(title):
            confidence += 0.04
        external_id, source_url = identity
        results.append(
            ExternalSearchHit(
                key="osm-" + external_id.replace("/", "-"),
                source="openstreetmap",
                title=title,
                display_name=_clean_text(item.get("display_name"), 500),
                lat=lat,
                lng=lng,
                category=_category(item.get("class"), item.get("type"), title, *address.values()),
                external_id=external_id,
                source_url=source_url,
                coordinate_source="openstreetmap",
                confidence=round(min(0.9, confidence), 3),
                sources=("openstreetmap",),
                external_ids=(("openstreetmap", external_id),),
                source_urls=(source_url,),
                storage_allowed=True,
                license="ODbL 1.0",
                attribution="© OpenStreetMap contributors",
            )
        )
        if len(results) >= limit:
            break
    return results


def _wikidata_language(query: str) -> str:
    return "ko" if re.search(r"[\uac00-\ud7a3]", query) else "en"


def _wikidata_coordinate(entity: Mapping[str, Any]) -> tuple[float, float] | None:
    claims = entity.get("claims") if isinstance(entity.get("claims"), Mapping) else {}
    coordinate_claims = claims.get("P625") if isinstance(claims.get("P625"), list) else []
    for claim in coordinate_claims:
        try:
            value = claim["mainsnak"]["datavalue"]["value"]
            lat = _safe_float(value.get("latitude"))
            lng = _safe_float(value.get("longitude"))
        except (KeyError, TypeError):
            continue
        if lat is not None and lng is not None:
            return lat, lng
    return None


def _localized_value(container: Any, *languages: str) -> str:
    if not isinstance(container, Mapping):
        return ""
    for language in languages:
        row = container.get(language)
        if isinstance(row, Mapping) and row.get("value"):
            return _clean_text(row["value"], 500)
    return ""


async def _wikidata_hits(
    query: str,
    *,
    bounds: GeoBounds | None,
    limit: int,
    headers: Mapping[str, str],
    timeout_seconds: float,
    fetch_json: JsonFetcher,
) -> list[ExternalSearchHit]:
    language = _wikidata_language(query)
    search_payload = await _request_json(
        fetch_json,
        WIKIDATA_API_URL,
        {
            "action": "wbsearchentities",
            "format": "json",
            "search": query,
            "language": language,
            "uselang": language,
            "type": "item",
            "limit": str(min(MAX_PER_SOURCE_LIMIT, limit * 2)),
            "origin": "*",
        },
        headers,
        timeout_seconds,
    )
    search_rows = search_payload.get("search") if isinstance(search_payload, Mapping) else []
    if not isinstance(search_rows, list):
        return []
    ids = [
        _clean_text(item.get("id"), 24)
        for item in search_rows
        if isinstance(item, Mapping) and re.fullmatch(r"Q[1-9]\d*", _clean_text(item.get("id"), 24))
    ][: min(MAX_PER_SOURCE_LIMIT, limit * 2)]
    if not ids:
        return []
    entity_payload = await _request_json(
        fetch_json,
        WIKIDATA_API_URL,
        {
            "action": "wbgetentities",
            "format": "json",
            "ids": "|".join(ids),
            "props": "claims|labels|descriptions",
            "languages": "ko|en|id",
            "languagefallback": "1",
            "origin": "*",
        },
        headers,
        timeout_seconds,
    )
    entities = entity_payload.get("entities") if isinstance(entity_payload, Mapping) else {}
    if not isinstance(entities, Mapping):
        return []
    results: list[ExternalSearchHit] = []
    for rank, entity_id in enumerate(ids):
        entity = entities.get(entity_id)
        if not isinstance(entity, Mapping):
            continue
        coordinate = _wikidata_coordinate(entity)
        if coordinate is None or not is_supported_coordinate(*coordinate, bounds=bounds):
            continue
        title = _localized_value(entity.get("labels"), "ko", "en", "id")
        description = _localized_value(entity.get("descriptions"), "ko", "en", "id")
        if not title:
            continue
        source_url = f"https://www.wikidata.org/wiki/{entity_id}"
        confidence = max(0.62, 0.78 - rank * 0.025)
        results.append(
            ExternalSearchHit(
                key=f"wikidata-{entity_id}",
                source="wikidata",
                title=title,
                display_name=" · ".join(item for item in (title, description) if item),
                lat=coordinate[0],
                lng=coordinate[1],
                category=_category(title, description),
                external_id=entity_id,
                source_url=source_url,
                coordinate_source="wikidata",
                confidence=round(confidence, 3),
                sources=("wikidata",),
                external_ids=(("wikidata", entity_id),),
                source_urls=(source_url,),
                storage_allowed=True,
                license="CC0 1.0",
                attribution="Wikidata contributors",
            )
        )
        if len(results) >= limit:
            break
    return results


async def _arcgis_hits(
    query: str,
    *,
    bounds: GeoBounds | None,
    limit: int,
    headers: Mapping[str, str],
    timeout_seconds: float,
    fetch_json: JsonFetcher,
) -> list[ExternalSearchHit]:
    """Return non-stored ArcGIS World Geocoding display candidates.

    The public/enhanced endpoint is queried with ``forStorage=false``.  ArcGIS
    results can therefore be displayed and used to cross-check another source,
    but an ArcGIS-only result is never represented as safe to persist.
    """

    payload = await _request_json(
        fetch_json,
        ARCGIS_SEARCH_URL,
        {
            "SingleLine": _arcgis_search_text(query, bounds),
            "f": "json",
            "category": "Address,POI",
            "outFields": (
                "Match_addr,Addr_type,PlaceName,Place_addr,ShortLabel,Type,MatchID"
            ),
            "maxLocations": str(min(MAX_PER_SOURCE_LIMIT, limit * 2)),
            "forStorage": "false",
            "sourceCountry": "IDN",
            "searchExtent": _arcgis_extent(bounds),
            "outSR": "4326",
            "langCode": "ko",
            "returnPrimaryMatchID": "true",
        },
        headers,
        timeout_seconds,
    )
    candidates = payload.get("candidates") if isinstance(payload, Mapping) else []
    if not isinstance(candidates, list):
        return []

    results: list[ExternalSearchHit] = []
    for item in candidates:
        if not isinstance(item, Mapping):
            continue
        location = item.get("location") if isinstance(item.get("location"), Mapping) else {}
        attributes = (
            item.get("attributes") if isinstance(item.get("attributes"), Mapping) else {}
        )
        lat = _safe_float(location.get("y"))
        lng = _safe_float(location.get("x"))
        score = _safe_float(item.get("score"))
        match_type = _clean_text(attributes.get("Addr_type"), 40)
        match_id = _clean_text(attributes.get("MatchID"), 300)
        if (
            lat is None
            or lng is None
            or score is None
            or match_type.casefold() not in ARCGIS_ALLOWED_MATCH_TYPES
            or not match_id
            or not is_supported_coordinate(lat, lng, bounds=bounds)
        ):
            continue

        matched_address = _clean_text(
            attributes.get("Match_addr") or item.get("address"),
            500,
        )
        place_address = _clean_text(attributes.get("Place_addr"), 500)
        title = _clean_text(
            attributes.get("PlaceName")
            or attributes.get("ShortLabel")
            or (matched_address.split(",", 1)[0] if matched_address else ""),
            200,
        )
        if not title:
            continue
        display_name = matched_address or place_address or title
        if (
            match_type.casefold() == "poi"
            and place_address
            and _normal_name(place_address) not in _normal_name(display_name)
        ):
            display_name = _clean_text(f"{display_name}, {place_address}", 500)
        relevance_values = (
            (title, matched_address)
            if match_type.casefold() == "poi"
            else (title, matched_address, place_address)
        )
        if not _arcgis_query_matches(query, *relevance_values):
            continue

        normalized_score = min(1.0, max(0.0, score / 100.0))
        precision_weight = {
            "poi": 0.96,
            "subaddress": 1.0,
            "pointaddress": 1.0,
            "streetaddress": 0.96,
            "streetaddressext": 0.94,
        }[match_type.casefold()]
        # Provider scores describe token matching, not independent truth or a
        # storage licence.  Keep anonymous ArcGIS-only hits visibly tentative;
        # confidence rises later only when a storable source corroborates them.
        confidence = 0.46 + normalized_score * 0.30 * precision_weight
        results.append(
            ExternalSearchHit(
                key=f"arcgis-{match_id}",
                source="arcgis",
                title=title,
                display_name=display_name,
                lat=lat,
                lng=lng,
                category=_category(attributes.get("Type"), match_type, title, display_name),
                external_id=match_id,
                source_url=ARCGIS_SEARCH_URL,
                coordinate_source="arcgis",
                confidence=round(min(0.78, confidence), 3),
                sources=("arcgis",),
                external_ids=(("arcgis", match_id),),
                source_urls=(ARCGIS_SEARCH_URL,),
                storage_allowed=False,
                license="Esri service terms; display-only (forStorage=false)",
                attribution="Powered by Esri ArcGIS World Geocoding Service",
            )
        )
        if len(results) >= limit:
            break
    return results


async def search_external_places(
    query: str,
    *,
    bounds: GeoBounds | None = None,
    user_agent: str = DEFAULT_USER_AGENT,
    timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
    per_source_limit: int = DEFAULT_PER_SOURCE_LIMIT,
    result_limit: int = DEFAULT_RESULT_LIMIT,
    fetch_json: JsonFetcher | None = None,
) -> list[ExternalSearchHit]:
    """Search Nominatim, Wikidata, and ArcGIS concurrently for local hits.

    Failures are isolated per provider, so a slow or unavailable secondary
    source does not erase successful results from the other provider.
    """

    normalized_query = _clean_text(query, 100)
    if len(normalized_query) < 2:
        return []
    if bounds is not None and not any(bounds.intersects(area) for area in SUPPORTED_AREA_BOUNDS):
        return []
    timeout = min(MAX_TIMEOUT_SECONDS, max(0.5, float(timeout_seconds)))
    source_limit = min(MAX_PER_SOURCE_LIMIT, max(1, int(per_source_limit)))
    total_limit = min(MAX_RESULT_LIMIT, max(1, int(result_limit)))
    safe_user_agent = _clean_text(user_agent, 300)
    if len(safe_user_agent) < 12:
        safe_user_agent = DEFAULT_USER_AGENT
    headers = {
        "User-Agent": safe_user_agent,
        "Accept": "application/json",
        "Accept-Language": "ko,en;q=0.9,id;q=0.8",
    }
    provider = fetch_json or _default_fetch_json

    async def safe_lookup(awaitable: Awaitable[list[ExternalSearchHit]]) -> list[ExternalSearchHit]:
        try:
            return await awaitable
        except (asyncio.CancelledError, KeyboardInterrupt):
            raise
        except Exception:
            return []

    tasks = (
        asyncio.create_task(
            safe_lookup(
                _nominatim_hits(
                    normalized_query,
                    bounds=bounds,
                    limit=source_limit,
                    headers=headers,
                    timeout_seconds=timeout,
                    fetch_json=provider,
                )
            )
        ),
        asyncio.create_task(
            safe_lookup(
                _wikidata_hits(
                    normalized_query,
                    bounds=bounds,
                    limit=source_limit,
                    headers=headers,
                    timeout_seconds=timeout,
                    fetch_json=provider,
                )
            )
        ),
        asyncio.create_task(
            safe_lookup(
                _arcgis_hits(
                    normalized_query,
                    bounds=bounds,
                    limit=source_limit,
                    headers=headers,
                    timeout_seconds=timeout,
                    fetch_json=provider,
                )
            )
        ),
    )
    done, pending = await asyncio.wait(tasks, timeout=min(12.0, timeout * 2.35))
    for task in pending:
        task.cancel()
    if pending:
        await asyncio.gather(*pending, return_exceptions=True)
    hits: list[ExternalSearchHit] = []
    for task in done:
        try:
            hits.extend(task.result())
        except Exception:
            continue
    return _merge_cross_checked(hits)[:total_limit]


__all__ = [
    "ARCGIS_SEARCH_URL",
    "DEFAULT_USER_AGENT",
    "ExternalSearchHit",
    "GeoBounds",
    "SUPPORTED_AREA_BOUNDS",
    "is_supported_coordinate",
    "search_external_places",
]
