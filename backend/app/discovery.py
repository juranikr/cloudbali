from __future__ import annotations

import argparse
import json
import math
import unicodedata
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, joinedload

from app.db import Base, SessionLocal, engine
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


def _valid_bbox(region: Region) -> bool:
    return (
        -90 <= region.south < region.north <= 90
        and -180 <= region.west < region.east <= 180
        and region.north - region.south <= MAX_BBOX_SPAN
        and region.east - region.west <= MAX_BBOX_SPAN
    )


def _overpass_query(region: Region, limit: int, query_phase: int = 0) -> str:
    if not _valid_bbox(region):
        raise ValueError(f"안전 범위를 벗어난 권역 bbox: {region.slug}")
    bbox = f"{region.south:.6f},{region.west:.6f},{region.north:.6f},{region.east:.6f}"
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


def fetch_osm_elements(region: Region, limit: int, query_phase: int = 0) -> list[dict]:
    """Fetch a bounded OSM result set. No candidate is published by this function."""

    query = _overpass_query(region, limit, query_phase)
    payload = urlencode({"data": query}).encode("utf-8")
    errors: list[str] = []
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
            errors.append(type(exc).__name__)
    raise RuntimeError("Overpass 요청 실패: " + ", ".join(errors))


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
    description = (
        _clean_text(tags.get("description:ko"), 5000)
        or _clean_text(tags.get("description:en"), 5000)
        or _clean_text(tags.get("description"), 5000)
    )
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
    names = {_normalized_name(values.title), _normalized_name(values.local_name)} - {""}
    for candidate in candidates:
        distance = _distance_m(values.lat, values.lng, candidate.lat, candidate.lng)
        other_names = {_normalized_name(candidate.title), _normalized_name(candidate.local_name)} - {""}
        if names & other_names and distance <= 300:
            return True
        if candidate.category == values.category and distance <= 15:
            return True
    return False


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


def _discovery_run_out(run: BatchRun, job: DiscoveryJob) -> DiscoveryRunOut:
    return DiscoveryRunOut(
        run=BatchRunOut.model_validate(run),
        created_count=run.updated_count,
        duplicate_count=job.duplicate_count,
        invalid_count=job.invalid_count,
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


def _execute_discovery_run(db: Session, run_id: int) -> DiscoveryRunOut:
    run, job, claimed = _claim_discovery_run(db, run_id)
    if not claimed:
        return _discovery_run_out(run, job)

    try:
        regions = _discovery_regions(db, job.region_id)
        limit = max(1, min(int(job.requested_limit), MAX_CANDIDATES_PER_RUN))
        creation_allocations = _allocate_limits(regions, limit)
        fetch_regions = [region for region in regions if region.id in creation_allocations]
        scan_states = {region.id: _locked_scan_state(db, region.id) for region in fetch_regions}
        fetch_specs: dict[int, tuple[int, int]] = {}
        for region in fetch_regions:
            state = scan_states[region.id]
            minimum = max(20, creation_allocations[region.id] * 3)
            fetch_specs[region.id] = (
                min(MAX_OSM_ELEMENTS_PER_REGION, max(minimum, state.fetch_limit)),
                state.query_phase % QUERY_PHASE_COUNT,
            )

        raw_by_region: dict[int, list[dict]] = {}
        failures: list[str] = []
        # Locking scan-state rows serializes overlapping runs for the same
        # regions; two workers only parallelize independent Overpass requests.
        with ThreadPoolExecutor(max_workers=min(2, len(fetch_regions))) as executor:
            futures = {
                executor.submit(fetch_osm_elements, region, *fetch_specs[region.id]): region
                for region in fetch_regions
            }
            for future in as_completed(futures):
                region = futures[future]
                try:
                    fetch_limit, _ = fetch_specs[region.id]
                    raw = future.result()[:fetch_limit]
                    raw_by_region[region.id] = raw
                    _advance_scan_state(
                        scan_states[region.id],
                        requested_limit=fetch_limit,
                        result_count=len(raw),
                    )
                except Exception as exc:
                    failures.append(f"{region.name_ko}: {type(exc).__name__}")

        places_by_region = {
            region.id: db.query(Place).filter(Place.region_id == region.id).all()
            for region in regions
        }
        existing_source_ids = {
            row[0]
            for row in db.query(DiscoveryCandidate.external_id).filter(
                DiscoveryCandidate.source == SOURCE
            ).all()
        }
        candidates_by_region = {
            region.id: db.query(DiscoveryCandidate).filter(
                DiscoveryCandidate.region_id == region.id
            ).all()
            for region in regions
        }
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
                if values.external_id in existing_source_ids or _candidate_duplicates(
                    values, candidates_by_region[region.id]
                ):
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
                    existing_source_ids.add(values.external_id)
                    continue
                candidates_by_region[region.id].append(candidate)
                existing_source_ids.add(values.external_id)
                created += 1
                region_created += 1

        final_status = "success" if not failures else ("partial" if raw_by_region else "failed")
        final_summary = (
            f"OSM {scanned}건 조회, 후보 {created}건 저장, 중복 {duplicates}건, 제외 {invalid}건"
        )
        if failures:
            final_summary += "; 실패 " + ", ".join(failures[:5])

        # Lock in the same job -> run order as the stale reaper. If that reaper
        # already revoked this worker's lease, roll back all still-uncommitted
        # candidates instead of letting a late worker resurrect a failed run.
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

        run.scanned_count = scanned
        run.updated_count = created
        job.duplicate_count = duplicates
        job.invalid_count = invalid
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


def execute_queued_discovery(run_id: int) -> None:
    """FastAPI background-task entry point with an independent DB session."""

    with SessionLocal() as db:
        try:
            _execute_discovery_run(db, run_id)
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


def _values_from_candidate(candidate: DiscoveryCandidate) -> CandidateValues:
    return CandidateValues(
        external_id=candidate.external_id,
        source_url=candidate.source_url,
        title=candidate.title,
        local_name=candidate.local_name,
        description=candidate.description,
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
        description=candidate.description,
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
        raise ValueError("대기 또는 중복 후보만 승인할 수 있습니다")

    values = _values_from_candidate(candidate)
    places = db.query(Place).filter(Place.region_id == candidate.region_id).all()
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
        db.commit()
        return candidate_out(get_candidate(db, candidate.id))

    category_defaults = {
        "beach": 150, "culture": 90, "nature": 150, "food": 90, "cafe": 75,
        "surf": 180, "dive": 180, "wellness": 120, "nightlife": 120,
        "transport": 60, "other": 90,
    }
    water_sensitive = candidate.category in {"beach", "surf", "dive"}
    weather_sensitive = candidate.category in {"beach", "nature", "surf", "dive"}
    region = candidate.region
    place = Place(
        region_id=candidate.region_id,
        creator_id=admin.id,
        category=candidate.category,
        title=candidate.title,
        local_name=candidate.local_name,
        description=candidate.description or "OpenStreetMap 공개 데이터에서 발굴해 관리자가 승인한 장소입니다.",
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
        coordinate_source="openstreetmap",
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
        metadata={"source": SOURCE, "candidate_id": candidate.id},
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
    db.commit()
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
    Base.metadata.create_all(bind=engine)
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
