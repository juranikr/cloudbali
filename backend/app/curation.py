from __future__ import annotations

import hashlib
import ipaddress
import json
import math
import re
import socket
import threading
import time
import unicodedata
from datetime import datetime, timedelta, timezone
from html.parser import HTMLParser
from typing import Any, Callable, Iterable
from urllib.parse import urlencode, urljoin, urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener, urlopen

from sqlalchemy import func, or_
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.agent_models import (
    AgentCheckpoint,
    AgentEvidence,
    AgentKnowledge,
    AgentMission,
    AgentProposal,
    AgentRun,
    AgentRunStep,
    AgentTask,
    AgentWorkItem,
)
from app.config import settings
from app.discovery import DiscoveryBusyError, inactive_place_reason, run_discovery
from app.extended_models import PlaceImage
from app.models import DiscoveryCandidate, DiscoveryDecision, Place, Region


ACTIVE_SLOT = "bali_curation"
ACTIVE_STATUSES = frozenset({"queued", "running"})
TERMINAL_STATUSES = frozenset({"success", "partial", "failed", "cancelled"})
RUN_STALE_AFTER = timedelta(minutes=45)
VALID_MODES = frozenset({"full", "discovery", "quality", "verification"})
MAX_CANDIDATES_TO_ENRICH = 8
MAX_QUALITY_TASKS_PER_RUN = 12
MAX_DUPLICATE_PROPOSALS_PER_RUN = 10
MAX_LIFECYCLE_TASKS_PER_RUN = 24
LIFECYCLE_CLEAR_COOLDOWN = timedelta(days=30)
LIFECYCLE_FLAGGED_COOLDOWN = timedelta(days=14)
LIFECYCLE_INCONCLUSIVE_COOLDOWN = timedelta(days=7)
_NOMINATIM_LOCK = threading.Lock()
_LAST_NOMINATIM_CALL = 0.0

_OSM_TYPES = {"node": "N", "way": "W", "relation": "R"}
_LIFECYCLE_PREFIXES = frozenset({"disused", "abandoned", "demolished", "razed", "removed"})
_RELOCATION_KEYS = frozenset({"moved_to", "relocated_to"})
_NEGATIVE_SIGNAL_VALUES = frozenset({"", "0", "false", "no", "none", "operational", "active"})


class AgentBusyError(RuntimeError):
    def __init__(self, active_run_id: int):
        self.active_run_id = active_run_id
        super().__init__(f"운영 조사 실행 #{active_run_id}이 이미 진행 중입니다")


class AgentLeaseLostError(RuntimeError):
    """Raised when a timed-out worker is superseded by a newer attempt."""


class _PageFactsParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.title = ""
        self.description = ""
        self._in_title = False
        self._title_parts: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        lowered = tag.lower()
        values = {key.lower(): value or "" for key, value in attrs}
        if lowered == "title":
            self._in_title = True
        if lowered == "meta":
            name = (values.get("name") or values.get("property") or "").lower()
            if name in {"description", "og:description"} and not self.description:
                self.description = _clean_text(values.get("content", ""), 1200)

    def handle_endtag(self, tag: str) -> None:
        if tag.lower() == "title":
            self._in_title = False
            if not self.title:
                self.title = _clean_text(" ".join(self._title_parts), 300)

    def handle_data(self, data: str) -> None:
        if self._in_title:
            self._title_parts.append(data)


JsonFetcher = Callable[[str, dict[str, str], float], dict[str, Any] | list[Any]]
PageFetcher = Callable[[str, float], dict[str, str]]


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _as_utc(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), default=str)


def _loads(value: str, fallback: Any) -> Any:
    try:
        parsed = json.loads(value or "")
    except (TypeError, ValueError):
        return fallback
    return parsed


def _clean_text(value: Any, limit: int) -> str:
    if not isinstance(value, str):
        return ""
    return " ".join(value.split())[:limit]


def _safe_https_url(value: Any, *, require_public_host: bool = False) -> str:
    cleaned = _clean_text(value, 1000)
    if not cleaned:
        return ""
    parsed = urlsplit(cleaned)
    if parsed.scheme.lower() != "https" or not parsed.netloc:
        return ""
    if parsed.username or parsed.password:
        return ""
    if require_public_host:
        hostname = parsed.hostname or ""
        if not hostname or hostname.casefold() == "localhost" or hostname.endswith(".local"):
            return ""
        try:
            addresses = {
                item[4][0]
                for item in socket.getaddrinfo(hostname, parsed.port or 443, type=socket.SOCK_STREAM)
            }
        except OSError:
            return ""
        if not addresses:
            return ""
        for address in addresses:
            try:
                parsed_ip = ipaddress.ip_address(address)
            except ValueError:
                return ""
            if not parsed_ip.is_global:
                return ""
    return cleaned


def _fingerprint(*parts: Any) -> str:
    canonical = "\n".join(str(part or "").strip() for part in parts)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _normalized_name(value: str) -> str:
    normalized = unicodedata.normalize("NFKC", value or "").casefold()
    return "".join(character for character in normalized if character.isalnum())


def _distance_m(first: Place, second: Place) -> float:
    radius = 6_371_000.0
    phi1, phi2 = math.radians(first.lat), math.radians(second.lat)
    delta_phi = math.radians(second.lat - first.lat)
    delta_lng = math.radians(second.lng - first.lng)
    value = (
        math.sin(delta_phi / 2) ** 2
        + math.cos(phi1) * math.cos(phi2) * math.sin(delta_lng / 2) ** 2
    )
    return radius * 2 * math.atan2(math.sqrt(value), math.sqrt(max(0.0, 1 - value)))


def _coordinate_distance_m(first_lat: float, first_lng: float, second_lat: float, second_lng: float) -> float:
    radius = 6_371_000.0
    phi1, phi2 = math.radians(first_lat), math.radians(second_lat)
    delta_phi = math.radians(second_lat - first_lat)
    delta_lng = math.radians(second_lng - first_lng)
    value = (
        math.sin(delta_phi / 2) ** 2
        + math.cos(phi1) * math.cos(phi2) * math.sin(delta_lng / 2) ** 2
    )
    return radius * 2 * math.atan2(math.sqrt(value), math.sqrt(max(0.0, 1 - value)))


def fetch_json(url: str, params: dict[str, str], timeout: float = 8.0) -> dict[str, Any] | list[Any]:
    global _LAST_NOMINATIM_CALL
    if urlsplit(url).hostname == "nominatim.openstreetmap.org":
        with _NOMINATIM_LOCK:
            delay = 1.05 - (time.monotonic() - _LAST_NOMINATIM_CALL)
            if delay > 0:
                time.sleep(delay)
            _LAST_NOMINATIM_CALL = time.monotonic()
    target = url + ("?" + urlencode(params) if params else "")
    request = Request(
        target,
        headers={"User-Agent": settings.geocoder_user_agent, "Accept": "application/json"},
    )
    with urlopen(request, timeout=timeout) as response:
        if "json" not in (response.headers.get("Content-Type") or "").lower():
            raise ValueError("JSON이 아닌 외부 응답입니다")
        return json.loads(response.read(1_500_000).decode("utf-8"))


class _PublicHttpsRedirectHandler(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):  # type: ignore[no-untyped-def]
        if not _safe_https_url(urljoin(req.full_url, newurl), require_public_host=True):
            raise ValueError("공개 HTTPS 주소가 아닌 리다이렉트입니다")
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def fetch_page_facts(url: str, timeout: float = 8.0) -> dict[str, str]:
    safe_url = _safe_https_url(url, require_public_host=True)
    if not safe_url:
        raise ValueError("HTTPS 공개 페이지만 조사할 수 있습니다")
    request = Request(
        safe_url,
        headers={
            "User-Agent": settings.geocoder_user_agent,
            "Accept": "text/html,application/xhtml+xml;q=0.9,*/*;q=0.2",
        },
    )
    opener = build_opener(_PublicHttpsRedirectHandler())
    with opener.open(request, timeout=timeout) as response:
        content_type = (response.headers.get("Content-Type") or "").lower()
        if "html" not in content_type:
            raise ValueError("HTML 공개 페이지가 아닙니다")
        final_url = _safe_https_url(response.geturl(), require_public_host=True)
        if not final_url:
            raise ValueError("조사 페이지가 안전하지 않은 주소로 이동했습니다")
        raw = response.read(160_000)
    text = raw.decode("utf-8", errors="replace")
    parser = _PageFactsParser()
    parser.feed(text)
    return {
        "url": final_url,
        "title": parser.title,
        "description": parser.description,
    }


def _active_run(db: Session, *, now: datetime | None = None) -> int | None:
    row = (
        db.query(AgentRun)
        .filter(AgentRun.active_slot == ACTIVE_SLOT)
        .with_for_update()
        .first()
    )
    if row is None:
        return None
    if row.status in TERMINAL_STATUSES:
        row.active_slot = None
        db.flush()
        return None
    timestamp = now or _utcnow()
    started_at = _as_utc(row.started_at)
    if row.status not in ACTIVE_STATUSES or (
        started_at is not None and timestamp - started_at > RUN_STALE_AFTER
    ):
        row.status = "failed"
        row.summary = "45분 이상 진행되지 않아 중단된 운영 조사입니다"
        row.finished_at = timestamp
        row.active_slot = None
        db.flush()
        return None
    return row.id


def create_agent_run(
    db: Session,
    *,
    region_id: int | None = None,
    mode: str = "full",
    trigger: str = "manual",
) -> AgentRun:
    normalized_mode = mode.strip().lower()
    if normalized_mode not in VALID_MODES:
        raise ValueError("지원하지 않는 운영 조사 모드입니다")
    if region_id is not None and db.get(Region, region_id) is None:
        raise LookupError("권역을 찾을 수 없습니다")
    try:
        active_id = _active_run(db)
        if active_id is not None:
            raise AgentBusyError(active_id)
        row = AgentRun(
            region_id=region_id,
            active_slot=ACTIVE_SLOT,
            mode=normalized_mode,
            trigger=trigger[:30],
            status="queued",
            objective={
                "full": "신규 장소 발굴과 기존 장소 품질·중복·최신성 점검",
                "discovery": "새로운 여행 장소를 찾아 출처를 교차 확인하고 검토 제안 생성",
                "quality": "기존 장소의 설명·출처·이미지·여행 정보 품질 보강",
                "verification": "기존 장소의 동일성·중복·최신 상태 재검증",
            }[normalized_mode],
            summary="운영 조사 작업이 대기 중입니다",
        )
        db.add(row)
        db.commit()
        db.refresh(row)
        return row
    except AgentBusyError:
        db.rollback()
        raise
    except IntegrityError as exc:
        db.rollback()
        active = db.query(AgentRun).filter(AgentRun.active_slot == ACTIVE_SLOT).first()
        if active is not None:
            raise AgentBusyError(active.id) from exc
        raise


def get_agent_run(db: Session, run_id: int) -> AgentRun | None:
    _active_run(db)
    db.commit()
    return db.get(AgentRun, run_id)


def prepare_agent_retry(db: Session, run_id: int) -> AgentRun:
    """Re-queue one interrupted/failed curation run for workflow retry."""

    try:
        active = (
            db.query(AgentRun)
            .filter(AgentRun.active_slot == ACTIVE_SLOT)
            .with_for_update()
            .first()
        )
        if active is not None and active.id != run_id:
            raise AgentBusyError(active.id)
        row = db.query(AgentRun).filter(AgentRun.id == run_id).with_for_update().first()
        if row is None:
            raise LookupError("운영 조사 실행을 찾을 수 없습니다")
        if row.status in {"success", "partial"}:
            db.commit()
            return row
        if row.status not in {"queued", "running", "failed"}:
            raise RuntimeError(f"재시도할 수 없는 운영 조사 상태입니다: {row.status}")
        row.active_slot = ACTIVE_SLOT
        row.status = "queued"
        row.summary = "내구성 워크플로가 운영 조사를 재시도합니다"
        row.started_at = _utcnow()
        row.finished_at = None
        db.commit()
        db.refresh(row)
        return row
    except Exception:
        db.rollback()
        raise


class _RunRecorder:
    def __init__(self, db: Session, run_id: int, claim_started_at: datetime):
        self.db = db
        self.run_id = run_id
        self.claim_started_at = claim_started_at
        self.sequence = (
            db.query(func.max(AgentRunStep.sequence)).filter(AgentRunStep.run_id == run_id).scalar() or 0
        )

    def assert_lease(self) -> None:
        current_lease = self.db.query(AgentRun.id).filter(
            AgentRun.id == self.run_id,
            AgentRun.status == "running",
            AgentRun.active_slot == ACTIVE_SLOT,
            AgentRun.started_at == self.claim_started_at,
        ).with_for_update().first()
        if current_lease is None:
            raise AgentLeaseLostError("새 실행이 운영 조사 임대를 인계해 이전 작업을 중단합니다")

    def step(
        self,
        phase: str,
        tool: str,
        outcome: str,
        detail: str,
        *,
        score_delta: float = 0.0,
        metadata: dict[str, Any] | None = None,
    ) -> AgentRunStep:
        self.assert_lease()
        self.sequence += 1
        row = AgentRunStep(
            run_id=self.run_id,
            sequence=self.sequence,
            phase=phase[:40],
            tool=tool[:100],
            outcome=outcome[:30],
            detail=_clean_text(detail, 5000),
            score_delta=score_delta,
            metadata_json=_json(metadata or {}),
        )
        self.db.add(row)
        self.db.commit()
        return row


def _regions_for_run(db: Session, run: AgentRun) -> list[Region]:
    query = db.query(Region).order_by(Region.sort_order, Region.id)
    if run.region_id is not None:
        query = query.filter(Region.id == run.region_id)
    return query.all()


def _ensure_mission(
    db: Session,
    *,
    region: Region,
    kind: str,
    title: str,
    objective: str,
    success_metric: str,
    run_id: int,
) -> AgentMission:
    mission = (
        db.query(AgentMission)
        .filter(
            AgentMission.region_id == region.id,
            AgentMission.kind == kind,
            AgentMission.status.in_(["active", "blocked"]),
        )
        .order_by(AgentMission.id.desc())
        .first()
    )
    if mission is None:
        mission = AgentMission(
            region_id=region.id,
            kind=kind,
            title=title,
            objective=objective,
            success_metric=success_metric,
            status="active",
        )
        db.add(mission)
        db.flush()
    mission.last_run_id = run_id
    mission.updated_at = _utcnow()
    return mission


def _ensure_work_item(
    db: Session,
    *,
    mission: AgentMission,
    region_id: int,
    target_key: str,
    target_type: str,
    title: str,
    goal: str,
    definition_of_done: str,
    place_id: int | None = None,
    priority: int = 50,
) -> AgentWorkItem:
    if place_id is not None:
        _lock_active_agent_places(db, {place_id})
    row = (
        db.query(AgentWorkItem)
        .filter(AgentWorkItem.mission_id == mission.id, AgentWorkItem.target_key == target_key)
        .first()
    )
    if row is None:
        row = AgentWorkItem(
            mission_id=mission.id,
            region_id=region_id,
            place_id=place_id,
            target_key=target_key,
            target_type=target_type,
            title=title,
            goal=goal,
            definition_of_done=definition_of_done,
            priority=priority,
        )
        db.add(row)
        db.flush()
    return row


def _ensure_task(
    db: Session,
    *,
    region_id: int,
    place_id: int | None,
    kind: str,
    target_key: str,
    title: str,
    detail: str,
    success_metric: str,
    priority: int,
) -> AgentTask:
    row = (
        db.query(AgentTask)
        .filter(
            AgentTask.region_id == region_id,
            AgentTask.kind == kind,
            AgentTask.target_key == target_key,
        )
        .first()
    )
    if row is None:
        row = AgentTask(
            region_id=region_id,
            place_id=place_id,
            kind=kind,
            target_key=target_key,
            title=title,
            detail=detail,
            success_metric=success_metric,
            priority=priority,
            status="pending",
            attempts=0,
        )
        db.add(row)
    elif row.status in {"completed", "waived"}:
        return row
    else:
        row.detail = detail
        row.success_metric = success_metric
        row.priority = max(row.priority, priority)
    return row


def _lock_active_agent_places(db: Session, place_ids: set[int]) -> None:
    ids = sorted(place_ids)
    if not ids:
        return
    # Agent approval and merge use Proposal -> Place ordering as well. Holding
    # these locks through the evidence/proposal commit means a concurrent merge
    # either moves the newly written rows afterward or wins first and makes the
    # active-place check fail; no evidence is stranded on a hidden source.
    db.query(AgentProposal).filter(or_(
        AgentProposal.place_id.in_(ids),
        AgentProposal.secondary_place_id.in_(ids),
        AgentProposal.result_place_id.in_(ids),
    )).order_by(AgentProposal.id).populate_existing().with_for_update().all()
    active_ids = {
        place_id for (place_id,) in db.query(Place.id).filter(
            Place.id.in_(ids),
            Place.merged_into_id.is_(None),
        ).order_by(Place.id).with_for_update().all()
    }
    if active_ids != set(ids):
        raise LookupError("대상 장소가 조사 중 삭제되거나 다른 장소에 병합되었습니다")


def _record_evidence(
    db: Session,
    *,
    region_id: int,
    run_id: int,
    source_type: str,
    url: str,
    title: str,
    claim: str,
    excerpt: str,
    confidence: float,
    source_status: str = "verified",
    place_id: int | None = None,
    mission_id: int | None = None,
    work_item_id: int | None = None,
    rejection_reason: str = "",
) -> AgentEvidence:
    if place_id is not None:
        _lock_active_agent_places(db, {place_id})
    fingerprint = _fingerprint(region_id, place_id, source_type, url, claim)
    existing = db.query(AgentEvidence).filter(AgentEvidence.fingerprint == fingerprint).first()
    if existing is not None:
        return existing
    row = AgentEvidence(
        region_id=region_id,
        mission_id=mission_id,
        work_item_id=work_item_id,
        place_id=place_id,
        run_id=run_id,
        source_type=source_type,
        url=_safe_https_url(url),
        title=_clean_text(title, 300),
        claim=_clean_text(claim, 4000),
        excerpt=_clean_text(excerpt, 2000),
        source_status=source_status,
        rejection_reason=_clean_text(rejection_reason, 2000),
        confidence=max(0.0, min(float(confidence), 1.0)),
        fingerprint=fingerprint,
    )
    db.add(row)
    db.flush()
    return row


def _proposal(
    db: Session,
    *,
    region_id: int,
    run_id: int,
    action: str,
    title: str,
    payload: dict[str, Any],
    evidence: str,
    source_urls: Iterable[str],
    confidence: float,
    place_id: int | None = None,
    secondary_place_id: int | None = None,
    discovery_candidate_id: int | None = None,
    stable_key: str,
) -> AgentProposal:
    key = _fingerprint(action, stable_key)
    _lock_active_agent_places(
        db,
        {value for value in (place_id, secondary_place_id) if value is not None},
    )
    row = db.query(AgentProposal).filter(
        AgentProposal.proposal_key == key
    ).populate_existing().with_for_update().first()
    if row is not None:
        if row.status == "pending":
            row.region_id = region_id
            row.run_id = run_id
            row.place_id = place_id
            row.secondary_place_id = secondary_place_id
            row.discovery_candidate_id = discovery_candidate_id
            row.title = title
            row.payload_json = _json(payload)
            row.evidence = evidence
            row.source_urls_json = _json(list(dict.fromkeys(filter(None, source_urls))))
            row.confidence = max(0.0, min(confidence, 1.0))
        return row
    row = AgentProposal(
        region_id=region_id,
        run_id=run_id,
        place_id=place_id,
        secondary_place_id=secondary_place_id,
        discovery_candidate_id=discovery_candidate_id,
        action=action,
        title=title,
        payload_json=_json(payload),
        evidence=evidence,
        source_urls_json=_json(list(dict.fromkeys(filter(None, source_urls)))),
        confidence=max(0.0, min(confidence, 1.0)),
        proposal_key=key,
    )
    db.add(row)
    db.flush()
    return row


def _candidate_source_urls(candidate: DiscoveryCandidate) -> list[str]:
    evidence = _loads(candidate.evidence, {})
    tags = evidence.get("osm_tags") if isinstance(evidence, dict) else {}
    tags = tags if isinstance(tags, dict) else {}
    urls = [_safe_https_url(candidate.source_url)]
    for key in ("website", "contact:website", "url"):
        urls.append(_safe_https_url(tags.get(key)))
    wikidata = _clean_text(tags.get("wikidata"), 40)
    if re.fullmatch(r"Q\d+", wikidata):
        urls.append(f"https://www.wikidata.org/wiki/{wikidata}")
    wikipedia = _clean_text(tags.get("wikipedia"), 200)
    if ":" in wikipedia:
        language, article = wikipedia.split(":", 1)
        if re.fullmatch(r"[a-z-]{2,12}", language):
            urls.append(f"https://{language}.wikipedia.org/wiki/{article.replace(' ', '_')}")
    return list(dict.fromkeys(url for url in urls if url))


def _groq_enrichment(candidate: DiscoveryCandidate, facts: list[dict[str, str]]) -> dict[str, Any]:
    if not settings.groq_api_key:
        return {}
    try:
        raw_evidence = _loads(candidate.evidence, {})
        compact = {
            "title": candidate.title,
            "local_name": candidate.local_name,
            "category": candidate.category,
            "area": candidate.area,
            "osm": raw_evidence.get("osm_tags", {}) if isinstance(raw_evidence, dict) else {},
            "pages": facts,
        }
        request = Request(
            "https://api.groq.com/openai/v1/chat/completions",
            data=json.dumps({
                "model": settings.groq_chat_model,
                "response_format": {"type": "json_object"},
                "temperature": 0.1,
                "max_completion_tokens": 900,
                "messages": [
                {
                    "role": "system",
                    "content": (
                        "발리와 인도네시아 섬 여행 장소 편집자다. 제공된 공개 근거만 사용해 JSON으로 답한다. "
                        "description_ko(80~500자), best_time(60자 이하), traveler_note(200자 이하), "
                        "tags(문자열 배열), confidence(0~1)를 반환한다. 근거에 없는 가격·시간·안전 정보를 만들지 말고, "
                        "불확실하면 빈 문자열로 둔다. 장소명은 현지 표기를 보존한다."
                    ),
                },
                {"role": "user", "content": _json(compact)[:14_000]},
                ],
            }, ensure_ascii=False).encode("utf-8"),
            headers={
                "Authorization": "Bearer " + settings.groq_api_key,
                "Content-Type": "application/json",
                "User-Agent": "cloudbali-curation/1.0",
            },
            method="POST",
        )
        with urlopen(request, timeout=min(30.0, settings.groq_timeout_seconds)) as response:
            result = json.loads(response.read(1_500_001).decode("utf-8"))
        content = str(result["choices"][0]["message"]["content"] or "{}")
        parsed = json.loads(content)
        if not isinstance(parsed, dict):
            return {}
        return {
            "description": _clean_text(parsed.get("description_ko"), 5000),
            "best_time": _clean_text(parsed.get("best_time"), 120),
            "traveler_note": _clean_text(parsed.get("traveler_note"), 5000),
            "tags": [
                _clean_text(value, 80)
                for value in (parsed.get("tags") or [])
                if _clean_text(value, 80)
            ][:12],
            "confidence": max(0.0, min(float(parsed.get("confidence", 0.5)), 1.0)),
        }
    except Exception:
        return {}


def _enrich_candidates(
    db: Session,
    run: AgentRun,
    recorder: _RunRecorder,
    *,
    page_fetcher: PageFetcher,
) -> tuple[int, int]:
    candidate_query = db.query(DiscoveryCandidate).filter(
        DiscoveryCandidate.status.in_(["pending", "duplicate"])
    )
    if run.region_id is not None:
        candidate_query = candidate_query.filter(DiscoveryCandidate.region_id == run.region_id)
    candidates = (
        candidate_query.order_by(DiscoveryCandidate.updated_at.desc(), DiscoveryCandidate.id.desc())
        .limit(MAX_CANDIDATES_TO_ENRICH)
        .all()
    )
    proposed = 0
    verified = 0
    for candidate in candidates:
        raw_evidence = _loads(candidate.evidence, {})
        osm_tags = raw_evidence.get("osm_tags") if isinstance(raw_evidence, dict) else {}
        osm_tags = osm_tags if isinstance(osm_tags, dict) else {}
        inactive_reason = inactive_place_reason(
            {str(key): str(value) for key, value in osm_tags.items()}
        )
        if inactive_reason:
            previous_status = candidate.status
            candidate.status = "rejected"
            candidate.decision_note = (
                "자동 상태 차단: 공개 지도의 명시적 폐업·철거 신호 " + inactive_reason
            )
            candidate.decided_at = _utcnow()
            db.add(DiscoveryDecision(
                candidate_id=candidate.id,
                admin_id=None,
                action="auto_rejected_inactive",
                from_status=previous_status,
                to_status="rejected",
                note=candidate.decision_note,
                place_id=None,
            ))
            _record_evidence(
                db,
                region_id=candidate.region_id,
                run_id=run.id,
                source_type="openstreetmap_lifecycle",
                url=candidate.source_url,
                title=candidate.title,
                claim="신규 장소 후보 비활성 신호",
                excerpt=inactive_reason,
                confidence=0.95,
                source_status="rejected",
                rejection_reason=inactive_reason,
            )
            recorder.assert_lease()
            db.commit()
            continue
        source_urls = _candidate_source_urls(candidate)
        facts: list[dict[str, str]] = []
        for url in source_urls[1:3]:
            try:
                fact = page_fetcher(url, 8.0)
                facts.append(fact)
                _record_evidence(
                    db,
                    region_id=candidate.region_id,
                    run_id=run.id,
                    source_type="official_or_reference_page",
                    url=fact.get("url", url),
                    title=fact.get("title", ""),
                    claim=f"{candidate.title} 장소와 관련된 공개 페이지",
                    excerpt=fact.get("description", ""),
                    confidence=0.72,
                )
            except Exception as exc:
                _record_evidence(
                    db,
                    region_id=candidate.region_id,
                    run_id=run.id,
                    source_type="web",
                    url=url,
                    title=candidate.title,
                    claim="후보 장소 교차 확인",
                    excerpt="",
                    confidence=0.0,
                    source_status="blocked",
                    rejection_reason=type(exc).__name__,
                )
        _record_evidence(
            db,
            region_id=candidate.region_id,
            run_id=run.id,
            source_type="openstreetmap",
            url=candidate.source_url,
            title=candidate.title,
            claim="공개 지도 객체와 WGS84 좌표",
            excerpt=candidate.evidence,
            confidence=candidate.confidence,
        )
        independent_sources = 1 + len(facts)
        if independent_sources >= 2:
            verified += 1
        enrichment = _groq_enrichment(candidate, facts)
        payload = {
            "candidate_id": candidate.id,
            "duplicate_place_id": candidate.duplicate_place_id,
            "requires_force": candidate.status == "duplicate",
            "title": candidate.title,
            "local_name": candidate.local_name,
            "description": enrichment.get("description") or candidate.description,
            "area": candidate.area,
            "category": candidate.category,
            "lat": candidate.lat,
            "lng": candidate.lng,
            "best_time": enrichment.get("best_time", ""),
            "traveler_note": enrichment.get("traveler_note", ""),
            "tags": enrichment.get("tags") or [item for item in candidate.tags.split(",") if item],
            "source_url": candidate.source_url,
            "coordinate_source": "openstreetmap",
            "coordinate_confidence": candidate.confidence,
            "independent_source_count": independent_sources,
        }
        confidence = min(
            0.96,
            candidate.confidence + (0.12 if independent_sources >= 2 else 0) + (0.04 if enrichment else 0),
        )
        _proposal(
            db,
            region_id=candidate.region_id,
            run_id=run.id,
            action="create",
            title=f"신규 장소: {candidate.title}",
            payload=payload,
            evidence=f"OSM 후보와 독립 공개 페이지 {len(facts)}건을 확인했습니다.",
            source_urls=source_urls,
            confidence=confidence,
            discovery_candidate_id=candidate.id,
            stable_key=f"candidate:{candidate.source}:{candidate.external_id}",
        )
        proposed += 1
        evidence = _loads(candidate.evidence, {})
        evidence = evidence if isinstance(evidence, dict) else {}
        evidence["curation"] = {
            "run_id": run.id,
            "source_urls": source_urls,
            "independent_source_count": independent_sources,
            "facts": facts,
            "enrichment": enrichment,
            "verified_at": _utcnow().isoformat(),
        }
        candidate.evidence = _json(evidence)
        candidate.confidence = confidence
        recorder.assert_lease()
        db.commit()
    recorder.step(
        "research",
        "cross_source_candidate_review",
        "ok" if candidates else "no_yield",
        f"후보 {len(candidates)}건을 검토하고 {verified}건을 교차 확인했습니다.",
        score_delta=verified * 1.5,
        metadata={"reviewed": len(candidates), "verified": verified, "proposals": proposed},
    )
    return verified, proposed


def _sync_quality_tasks(db: Session, regions: list[Region], recorder: _RunRecorder) -> int:
    created_or_open = 0
    region_ids = [region.id for region in regions]
    place_ids = [
        place_id for (place_id,) in db.query(Place.id).filter(
            Place.region_id.in_(region_ids), Place.merged_into_id.is_(None)
        ).order_by(Place.id).all()
    ]
    _lock_active_agent_places(db, set(place_ids))
    places = (
        db.query(Place)
        .filter(Place.id.in_(place_ids), Place.merged_into_id.is_(None))
        .order_by(Place.id)
        .populate_existing()
        .all()
    )
    image_counts = {
        place_id: count
        for place_id, count in (
            db.query(PlaceImage.place_id, func.count(PlaceImage.id))
            .filter(PlaceImage.place_id.in_([place.id for place in places] or [-1]))
            .group_by(PlaceImage.place_id)
            .all()
        )
    }
    for place in places:
        gaps: list[tuple[str, str, str, int]] = []
        if len((place.description or "").strip()) < 80:
            gaps.append(("description", "한국어 설명과 여행 맥락 보강", "근거 URL이 있는 80자 이상 설명", 80))
        if not (place.source_url or "").strip():
            gaps.append(("source", "공개 출처와 좌표 근거 확인", "HTTPS 출처와 제공자 확인", 90))
        if not (place.local_name or "").strip():
            gaps.append(("local_name", "현지 표기 확인", "인도네시아어 또는 현지 통용 명칭 확인", 65))
        if image_counts.get(place.id, 0) == 0:
            gaps.append(("image", "자유 라이선스 대표 이미지 조사", "정확한 장소 사진과 라이선스 근거", 55))
        if not (place.best_time or "").strip():
            gaps.append(("visit", "방문 시간·접근 조건 확인", "출처 기반 방문 조건", 60))
        for kind, detail, metric, priority in gaps:
            task = _ensure_task(
                db,
                region_id=place.region_id,
                place_id=place.id,
                kind=f"quality_{kind}",
                target_key=f"place:{place.id}:{kind}",
                title=f"{place.title} · {detail}",
                detail=detail,
                success_metric=metric,
                priority=priority,
            )
            if task.status in {"pending", "blocked", "running"}:
                created_or_open += 1
    recorder.assert_lease()
    db.commit()
    return created_or_open


def _sync_lifecycle_tasks(db: Session, regions: list[Region], recorder: _RunRecorder) -> int:
    """Create or re-open durable place-status checks after their cooldown."""

    now = _utcnow()
    region_ids = [region.id for region in regions]
    place_ids = [
        place_id for (place_id,) in db.query(Place.id).filter(
            Place.region_id.in_(region_ids), Place.merged_into_id.is_(None)
        ).order_by(Place.id).all()
    ]
    _lock_active_agent_places(db, set(place_ids))
    places = (
        db.query(Place)
        .filter(Place.id.in_(place_ids), Place.merged_into_id.is_(None))
        .order_by(Place.id)
        .populate_existing()
        .all()
    )
    open_count = 0
    for place in places:
        task = _ensure_task(
            db,
            region_id=place.region_id,
            place_id=place.id,
            kind="verification_lifecycle",
            target_key=f"place:{place.id}:lifecycle",
            title=f"{place.title} · 폐업·이전 상태 재검증",
            detail="공개 OSM/Nominatim에서 폐업·철거·이전 lifecycle 신호를 점검",
            success_metric="공개 출처 근거와 체크포인트를 남기고 위험 신호는 관리자 검토 제안으로 전환",
            priority=100,
        )
        retry_after = _as_utc(task.retry_after)
        if task.status == "completed" and (retry_after is None or retry_after <= now):
            pending_review = (
                db.query(AgentProposal.id)
                .filter(
                    AgentProposal.place_id == place.id,
                    AgentProposal.action == "update",
                    AgentProposal.status == "pending",
                    AgentProposal.title.like("운영 상태 경고 검토:%"),
                )
                .first()
            )
            if pending_review is not None:
                task.retry_after = now + LIFECYCLE_FLAGGED_COOLDOWN
                continue
            task.status = "pending"
            task.result = "정기 재검증 주기가 도래했습니다"
            task.retry_after = None
            task.completed_at = None
        if task.status in {"pending", "blocked", "running"}:
            open_count += 1
    recorder.assert_lease()
    db.commit()
    return open_count


def _osm_reference(place: Place) -> tuple[str, str] | None:
    source = _clean_text(place.source_url, 1000)
    if source:
        parsed = urlsplit(source)
        hostname = (parsed.hostname or "").casefold()
        if hostname in {"openstreetmap.org", "www.openstreetmap.org"}:
            match = re.fullmatch(r"/(node|way|relation)/(\d+)/?", parsed.path.casefold())
            if match and int(match.group(2)) > 0:
                return match.group(1), match.group(2)

    external_id = _clean_text(place.coordinate_external_id, 200).casefold()
    match = re.fullmatch(r"(node|way|relation)[/:](\d+)", external_id)
    if match and int(match.group(2)) > 0:
        return match.group(1), match.group(2)
    compact = re.fullmatch(r"([nwr])(\d+)", external_id)
    if compact and int(compact.group(2)) > 0:
        osm_type = {"n": "node", "w": "way", "r": "relation"}[compact.group(1)]
        return osm_type, compact.group(2)
    return None


def _canonical_osm_url(osm_type: Any, osm_id: Any) -> str:
    normalized_type = _clean_text(osm_type, 20).casefold()
    normalized_id = _clean_text(str(osm_id), 30)
    if normalized_type not in _OSM_TYPES or not normalized_id.isdigit() or int(normalized_id) <= 0:
        return ""
    return f"https://www.openstreetmap.org/{normalized_type}/{normalized_id}"


def _candidate_names(row: dict[str, Any]) -> set[str]:
    details = row.get("namedetails") if isinstance(row.get("namedetails"), dict) else {}
    values = [row.get("name"), str(row.get("display_name") or "").split(",", 1)[0]]
    values.extend(details.values())
    return {
        normalized
        for value in values
        if (normalized := _normalized_name(_clean_text(value, 300)))
    }


def _select_search_match(place: Place, rows: list[dict[str, Any]]) -> tuple[dict[str, Any] | None, float]:
    expected_names = {
        normalized
        for value in (place.title, place.local_name)
        if (normalized := _normalized_name(value))
    }
    best: dict[str, Any] | None = None
    best_score = 0.0
    for row in rows:
        try:
            latitude = float(row.get("lat"))
            longitude = float(row.get("lon"))
        except (TypeError, ValueError):
            continue
        if not (-90 <= latitude <= 90 and -180 <= longitude <= 180):
            continue
        distance = _coordinate_distance_m(place.lat, place.lng, latitude, longitude)
        names = _candidate_names(row)
        exact_name = bool(expected_names & names)
        partial_name = any(
            min(len(expected), len(candidate)) >= 5
            and (expected in candidate or candidate in expected)
            for expected in expected_names
            for candidate in names
        )
        if exact_name and distance <= 5_000:
            score = 0.84 if distance <= 500 else 0.74
        elif partial_name and distance <= 1_000:
            score = 0.66
        elif distance <= 75:
            score = 0.62
        else:
            continue
        if score > best_score:
            best, best_score = row, score
    return best, best_score


def _lifecycle_signals(row: dict[str, Any]) -> list[dict[str, str]]:
    tags = row.get("extratags") if isinstance(row.get("extratags"), dict) else {}
    observed: dict[str, Any] = dict(tags)
    shared_tags = {str(key): str(value) for key, value in tags.items() if value is not None}
    for field in ("category", "class", "type"):
        top_level_value = _clean_text(row.get(field), 80)
        if top_level_value.casefold() in _LIFECYCLE_PREFIXES:
            observed[f"nominatim:{field}"] = top_level_value
            shared_tags[top_level_value.casefold()] = "yes"

    signals: list[dict[str, str]] = []
    for raw_key, raw_value in observed.items():
        key = _clean_text(raw_key, 160).casefold().replace("-", "_")
        value = _clean_text(str(raw_value), 500)
        normalized_value = value.casefold().strip()
        if normalized_value in _NEGATIVE_SIGNAL_VALUES:
            continue
        prefix = key.split(":", 1)[0]
        suffix = key.rsplit(":", 1)[-1]
        if prefix in _LIFECYCLE_PREFIXES or (
            key.startswith("nominatim:") and normalized_value in _LIFECYCLE_PREFIXES
        ):
            kind = prefix if prefix in _LIFECYCLE_PREFIXES else normalized_value
            signals.append({
                "kind": kind,
                "key": key,
                "value": value,
                "severity": "critical" if kind in {"demolished", "razed"} else "high",
            })
        elif key == "opening_hours" and normalized_value in {"closed", "permanently closed"}:
            signals.append({
                "kind": "closed",
                "key": key,
                "value": value,
                "severity": "high",
            })
        elif key in _RELOCATION_KEYS or suffix in _RELOCATION_KEYS:
            signals.append({
                "kind": "relocated",
                "key": key,
                "value": value,
                "severity": "high",
            })

    # Reuse the stricter discovery approval parser so published-place checks
    # recognize the same explicit lifecycle vocabulary (for example
    # ``Mo-Su off``, ``status=inactive`` and ``operational_status=non_operational``).
    inactive_reason = inactive_place_reason(shared_tags)
    if inactive_reason:
        reason_key, _, reason_value = inactive_reason.partition("=")
        normalized_key = reason_key.casefold().replace("-", "_")
        prefix = normalized_key.split(":", 1)[0]
        kind = prefix if prefix in _LIFECYCLE_PREFIXES else "closed"
        signal = {
            "kind": kind,
            "key": normalized_key,
            "value": reason_value,
            "severity": "critical" if kind in {"demolished", "razed", "removed"} else "high",
        }
        if not any(item["kind"] == signal["kind"] for item in signals):
            signals.append(signal)
    return sorted(signals, key=lambda item: (item["kind"], item["key"], item["value"]))


def _lifecycle_observation(
    region: Region,
    place: Place,
    *,
    json_fetcher: JsonFetcher,
) -> dict[str, Any]:
    reference = _osm_reference(place)
    match: dict[str, Any] | None = None
    identity_confidence = 0.0
    method = "bounded_name_search"
    if reference is not None:
        osm_type, osm_id = reference
        result = json_fetcher(
            "https://nominatim.openstreetmap.org/lookup",
            {
                "format": "jsonv2",
                "osm_ids": f"{_OSM_TYPES[osm_type]}{osm_id}",
                "addressdetails": "1",
                "namedetails": "1",
                "extratags": "1",
            },
            8.0,
        )
        rows = [row for row in result if isinstance(row, dict)] if isinstance(result, list) else []
        expected_osm_id = int(osm_id)
        matching_rows = [
            row
            for row in rows
            if _clean_text(row.get("osm_type"), 20).casefold() == osm_type
            and str(row.get("osm_id")) == str(expected_osm_id)
        ]
        if matching_rows:
            match = matching_rows[0]
            identity_confidence = 0.92
        method = "osm_object_lookup"
    else:
        result = _nominatim_candidates(region, place, json_fetcher=json_fetcher)
        rows = [row for row in result if isinstance(row, dict)]
        match, identity_confidence = _select_search_match(place, rows)

    source_url = ""
    if match is not None:
        source_url = _canonical_osm_url(match.get("osm_type"), match.get("osm_id"))
    if not source_url and reference is not None:
        source_url = _canonical_osm_url(*reference)
    if not source_url:
        source_url = "https://nominatim.openstreetmap.org/"

    return {
        "method": method,
        "matched": match is not None,
        "identity_confidence": identity_confidence,
        "source_url": source_url,
        "signals": _lifecycle_signals(match) if match is not None else [],
        "observation": {
            "osm_type": match.get("osm_type") if match else None,
            "osm_id": match.get("osm_id") if match else None,
            "display_name": _clean_text(match.get("display_name"), 500) if match else "",
            "category": _clean_text(match.get("category"), 80) if match else "",
            "type": _clean_text(match.get("type"), 80) if match else "",
            "extratags": match.get("extratags", {}) if match else {},
        },
    }


def _lifecycle_warning(place: Place, signals: list[dict[str, str]]) -> tuple[str, list[str]]:
    labels = {
        "disused": "미사용",
        "abandoned": "방치·폐업 가능성",
        "demolished": "철거",
        "razed": "철거",
        "removed": "삭제·철거",
        "closed": "폐업",
        "relocated": "이전",
    }
    kinds = list(dict.fromkeys(signal["kind"] for signal in signals))
    summary = "·".join(labels.get(kind, kind) for kind in kinds)
    warning = (
        f"⚠️ 운영 상태 재확인 필요: 공개 지도에 {summary} 신호가 있어 관리자 확인 중입니다. "
        "방문 전 공식 채널이나 현지 최신 정보를 확인하세요."
    )
    existing_note = (place.traveler_note or "").strip()
    traveler_note = existing_note
    if "⚠️ 운영 상태 재확인 필요:" not in existing_note:
        traveler_note = "\n\n".join(part for part in (existing_note, warning) if part)
    tags = [part.strip() for part in re.split(r"[,\n]", place.tags or "") if part.strip()]
    if "운영상태 재확인 필요" not in tags:
        tags.append("운영상태 재확인 필요")
    return traveler_note, tags


def _process_lifecycle_tasks(
    db: Session,
    run: AgentRun,
    regions: list[Region],
    recorder: _RunRecorder,
    *,
    json_fetcher: JsonFetcher,
) -> tuple[int, int, int]:
    region_map = {region.id: region for region in regions}
    now = _utcnow()
    tasks = (
        db.query(AgentTask)
        .filter(
            AgentTask.region_id.in_(list(region_map)),
            AgentTask.kind == "verification_lifecycle",
            AgentTask.status.in_(["pending", "blocked", "running"]),
            (AgentTask.retry_after.is_(None) | (AgentTask.retry_after <= now)),
        )
        .order_by(AgentTask.priority.desc(), AgentTask.created_at, AgentTask.id)
        .limit(MAX_LIFECYCLE_TASKS_PER_RUN)
        .all()
    )
    checked = 0
    flagged = 0
    proposals = 0
    for task in tasks:
        place = db.get(Place, task.place_id) if task.place_id else None
        region = region_map.get(task.region_id)
        if place is None or region is None or place.merged_into_id is not None:
            task.status = "completed"
            task.result = "대상 장소 또는 권역이 없어 종료"
            task.retry_after = now + LIFECYCLE_CLEAR_COOLDOWN
            task.completed_at = now
            recorder.assert_lease()
            db.commit()
            continue
        mission = _ensure_mission(
            db,
            region=region,
            kind="lifecycle_verification",
            title=f"{region.name_ko} 장소 운영 상태 재검증",
            objective="기존 장소의 폐업·철거·이전 가능성을 공개 지도 근거로 주기적으로 확인합니다.",
            success_metric="장소별 근거와 체크포인트, 위험 시 관리자 승인 대기 경고 제안",
            run_id=run.id,
        )
        work_item = _ensure_work_item(
            db,
            mission=mission,
            region_id=region.id,
            target_key=task.target_key,
            target_type=task.kind,
            title=task.title,
            goal=task.detail,
            definition_of_done=task.success_metric,
            place_id=place.id,
            priority=task.priority,
        )
        task.status = "running"
        task.attempts += 1
        task.retry_after = None
        work_item.status = "running"
        work_item.stage = "observe"
        work_item.attempts += 1
        work_item.last_run_id = run.id
        work_item.completed_at = None
        work_item.blocked_reason = ""
        recorder.assert_lease()
        db.commit()
        task_id = task.id
        work_item_id = work_item.id
        mission_id = mission.id
        try:
            observation = _lifecycle_observation(
                region,
                place,
                json_fetcher=json_fetcher,
            )
            signals = observation["signals"]
            matched = bool(observation["matched"])
            source_url = observation["source_url"]
            if signals:
                claim = f"{place.title}의 공개 지도 객체에서 lifecycle 위험 신호를 확인했습니다"
                source_status = "risk_signal"
                confidence = max(0.55, min(float(observation["identity_confidence"]), 0.92))
            elif matched:
                claim = (
                    f"{place.title} 공개 지도 객체에서 폐업·철거·이전 lifecycle 신호를 발견하지 못했습니다. "
                    "이는 현재 영업 중임을 보증하지 않습니다"
                )
                source_status = "verified"
                confidence = max(0.5, min(float(observation["identity_confidence"]), 0.85))
            else:
                claim = (
                    f"{place.title}와 동일하다고 확정할 공개 지도 객체를 찾지 못했습니다. "
                    "위험 신호 미발견은 현재 영업 중이라는 뜻이 아닙니다"
                )
                source_status = "inconclusive"
                confidence = 0.3
            evidence = _record_evidence(
                db,
                region_id=region.id,
                run_id=run.id,
                mission_id=mission.id,
                work_item_id=work_item.id,
                place_id=place.id,
                source_type="nominatim_openstreetmap_status",
                url=source_url,
                title=f"{place.title} 운영 상태 재검증",
                claim=f"{claim} (실행 #{run.id})",
                excerpt=_json({
                    "method": observation["method"],
                    "matched": matched,
                    "identity_confidence": observation["identity_confidence"],
                    "signals": signals,
                    "observation": observation["observation"],
                }),
                confidence=confidence,
                source_status=source_status,
            )
            proposal: AgentProposal | None = None
            if signals:
                traveler_note, tags = _lifecycle_warning(place, signals)
                payload = {
                    "traveler_note": traveler_note,
                    "tags": tags,
                    "lifecycle_review": {
                        "warning": "방문 전 운영 상태 재확인 필요",
                        "signals": signals,
                        "source_url": source_url,
                        "observed_at": now.isoformat(),
                        "automatic_removal": False,
                    },
                }
                signal_fingerprint = _fingerprint(
                    *[f"{signal['key']}={signal['value']}" for signal in signals]
                )
                stable_key = f"place:{place.id}:lifecycle:{signal_fingerprint}"
                prior_key = _fingerprint("update", stable_key)
                prior = db.query(AgentProposal).filter(AgentProposal.proposal_key == prior_key).first()
                if prior is not None and prior.status != "pending":
                    stable_key = f"{stable_key}:recheck:{run.id}"
                proposal = _proposal(
                    db,
                    region_id=region.id,
                    run_id=run.id,
                    action="update",
                    title=f"운영 상태 경고 검토: {place.title}",
                    payload=payload,
                    evidence=(
                        "OpenStreetMap/Nominatim 공개 객체에서 폐업·철거·이전 가능성 신호를 확인했습니다. "
                        "자동 삭제하지 않으며 관리자가 실제 상태와 여행자 경고를 검토해야 합니다."
                    ),
                    source_urls=[source_url],
                    confidence=confidence,
                    place_id=place.id,
                    stable_key=stable_key,
                )
                if proposal.status == "pending":
                    proposal.payload_json = _json(payload)
                flagged += 1
                proposals += 1
                task.result = "lifecycle 위험 신호를 관리자 검토 제안으로 전환했습니다"
                task.retry_after = now + LIFECYCLE_FLAGGED_COOLDOWN
                checkpoint_outcome = "proposed"
                decision = "자동 변경 없이 관리자 검토 요청"
                next_action = {"action": "admin_review", "proposal_id": proposal.id}
            else:
                task.result = (
                    "공개 객체에서 lifecycle 위험 신호 없음(영업 중 보증 아님)"
                    if matched
                    else "동일 객체 확인 불가; 공개 응답상 위험 신호 없음(영업 중 보증 아님)"
                )
                task.retry_after = now + (
                    LIFECYCLE_CLEAR_COOLDOWN if matched else LIFECYCLE_INCONCLUSIVE_COOLDOWN
                )
                checkpoint_outcome = "verified" if matched else "inconclusive"
                decision = "공개 변경 없이 정기 재검증 예약"
                next_action = {
                    "action": "reverify_after",
                    "days": (
                        LIFECYCLE_CLEAR_COOLDOWN.days
                        if matched
                        else LIFECYCLE_INCONCLUSIVE_COOLDOWN.days
                    ),
                }
            checked += 1
            task.status = "completed"
            task.completed_at = now
            work_item.status = "completed"
            work_item.stage = "review" if signals else "verify"
            work_item.state_summary = task.result
            work_item.evidence_summary = claim
            work_item.next_action_json = _json(next_action)
            work_item.completed_at = now
            db.add(AgentCheckpoint(
                mission_id=mission.id,
                work_item_id=work_item.id,
                run_id=run.id,
                sequence=work_item.attempts,
                state_summary=task.result,
                decision=decision,
                new_facts_json=_json({
                    "evidence_id": evidence.id,
                    "matched": matched,
                    "signals": signals,
                    "source_url": source_url,
                }),
                next_action_json=_json(next_action),
                outcome=checkpoint_outcome,
            ))
            recorder.assert_lease()
            db.commit()
        except AgentLeaseLostError:
            db.rollback()
            raise
        except Exception as exc:
            error_result = f"{type(exc).__name__}: {_clean_text(str(exc), 300)}"
            db.rollback()
            task = db.get(AgentTask, task_id)
            work_item = db.get(AgentWorkItem, work_item_id)
            if task is None or work_item is None:
                raise
            task.status = "blocked"
            task.result = error_result
            task.retry_after = _utcnow() + timedelta(hours=24)
            work_item.status = "blocked"
            work_item.blocked_reason = task.result
            work_item.retry_condition = "24시간 뒤 공개 OSM/Nominatim 상태 조회 재시도"
            db.add(AgentCheckpoint(
                mission_id=mission_id,
                work_item_id=work_item.id,
                run_id=run.id,
                sequence=work_item.attempts,
                state_summary=task.result,
                decision="외부 조회 냉각 후 재시도",
                next_action_json=_json({"action": "retry_after", "hours": 24}),
                outcome="blocked",
            ))
            recorder.assert_lease()
            db.commit()
    recorder.step(
        "verification",
        "lifecycle_status_check",
        "ok" if checked else ("blocked" if tasks else "no_yield"),
        f"장소 상태 {len(tasks)}건을 점검해 {flagged}건의 위험 신호를 관리자 검토로 보냈습니다.",
        score_delta=checked * 0.5 + flagged,
        metadata={
            "selected": len(tasks),
            "checked": checked,
            "flagged": flagged,
            "proposals": proposals,
            "clear_cooldown_days": LIFECYCLE_CLEAR_COOLDOWN.days,
            "flagged_cooldown_days": LIFECYCLE_FLAGGED_COOLDOWN.days,
        },
    )
    return checked, flagged, proposals


def _nominatim_candidates(
    region: Region,
    place: Place,
    *,
    json_fetcher: JsonFetcher,
) -> list[dict[str, Any]]:
    result = json_fetcher(
        "https://nominatim.openstreetmap.org/search",
        {
            "format": "jsonv2",
            "q": f"{place.local_name or place.title}, {region.name_local or region.island}, Indonesia",
            "bounded": "1",
            "viewbox": f"{region.west},{region.north},{region.east},{region.south}",
            "limit": "4",
            "addressdetails": "1",
            "namedetails": "1",
            "extratags": "1",
        },
        8.0,
    )
    return result if isinstance(result, list) else []


def _wikimedia_image(
    place: Place,
    *,
    json_fetcher: JsonFetcher,
) -> dict[str, Any] | None:
    result = json_fetcher(
        "https://commons.wikimedia.org/w/api.php",
        {
            "action": "query",
            "format": "json",
            "generator": "search",
            "gsrsearch": place.local_name or place.title,
            "gsrnamespace": "6",
            "gsrlimit": "5",
            "prop": "imageinfo",
            "iiprop": "url|mime|extmetadata",
        },
        10.0,
    )
    pages = result.get("query", {}).get("pages", {}) if isinstance(result, dict) else {}
    for page in pages.values() if isinstance(pages, dict) else []:
        info_rows = page.get("imageinfo") or []
        info = info_rows[0] if info_rows else {}
        mime = _clean_text(info.get("mime"), 80)
        image_url = _safe_https_url(info.get("url"))
        source_url = _safe_https_url(info.get("descriptionurl"))
        if image_url and source_url and mime in {"image/jpeg", "image/png", "image/webp"}:
            metadata = info.get("extmetadata") or {}
            license_name = _clean_text((metadata.get("LicenseShortName") or {}).get("value"), 120)
            artist = _clean_text(re.sub("<[^>]+>", "", (metadata.get("Artist") or {}).get("value", "")), 200)
            return {
                "image_url": image_url,
                "source_url": source_url,
                "caption": _clean_text(page.get("title", "").removeprefix("File:"), 300),
                "license": license_name,
                "artist": artist,
            }
    return None


def _process_quality_tasks(
    db: Session,
    run: AgentRun,
    regions: list[Region],
    recorder: _RunRecorder,
    *,
    json_fetcher: JsonFetcher,
) -> tuple[int, int]:
    region_map = {region.id: region for region in regions}
    tasks = (
        db.query(AgentTask)
        .filter(
            AgentTask.region_id.in_(list(region_map)),
            AgentTask.kind.like("quality_%"),
            AgentTask.status.in_(["pending", "blocked"]),
            (AgentTask.retry_after.is_(None) | (AgentTask.retry_after <= _utcnow())),
        )
        .order_by(AgentTask.priority.desc(), AgentTask.created_at, AgentTask.id)
        .limit(MAX_QUALITY_TASKS_PER_RUN)
        .all()
    )
    completed = 0
    proposals = 0
    for task in tasks:
        place = db.get(Place, task.place_id) if task.place_id else None
        region = region_map.get(task.region_id)
        if place is None or region is None:
            task.status = "completed"
            task.result = "대상 장소 또는 권역이 없어 종료"
            task.completed_at = _utcnow()
            recorder.assert_lease()
            db.commit()
            continue
        mission = _ensure_mission(
            db,
            region=region,
            kind="quality_repair",
            title=f"{region.name_ko} 장소 품질 보강",
            objective="여행자에게 보이는 정보·출처·이미지 공백을 근거 기반으로 줄입니다.",
            success_metric="관리자 검토 가능한 근거 제안 또는 명시적 보류 사유",
            run_id=run.id,
        )
        work_item = _ensure_work_item(
            db,
            mission=mission,
            region_id=region.id,
            target_key=task.target_key,
            target_type=task.kind,
            title=task.title,
            goal=task.detail,
            definition_of_done=task.success_metric,
            place_id=place.id,
            priority=task.priority,
        )
        task.status = "running"
        task.attempts += 1
        work_item.status = "running"
        work_item.attempts += 1
        work_item.last_run_id = run.id
        recorder.assert_lease()
        db.commit()
        try:
            if task.kind == "quality_image":
                image = _wikimedia_image(place, json_fetcher=json_fetcher)
                if image is None:
                    raise LookupError("정확한 자유 라이선스 이미지 후보가 없습니다")
                _record_evidence(
                    db,
                    region_id=region.id,
                    run_id=run.id,
                    mission_id=mission.id,
                    work_item_id=work_item.id,
                    place_id=place.id,
                    source_type="wikimedia_commons",
                    url=image["source_url"],
                    title=image["caption"],
                    claim=f"{place.title} 대표 이미지 후보와 라이선스",
                    excerpt=f"{image['license']} · {image['artist']}",
                    confidence=0.72,
                )
                _proposal(
                    db,
                    region_id=region.id,
                    run_id=run.id,
                    action="image",
                    title=f"대표 이미지: {place.title}",
                    payload=image,
                    evidence="Wikimedia Commons 파일 설명과 라이선스를 확인했습니다.",
                    source_urls=[image["source_url"]],
                    confidence=0.72,
                    place_id=place.id,
                    stable_key=f"place:{place.id}:image:{image['source_url']}",
                )
            else:
                hits = _nominatim_candidates(region, place, json_fetcher=json_fetcher)
                if not hits:
                    raise LookupError("권역 안에서 동일 장소 후보를 찾지 못했습니다")
                active_hits = [hit for hit in hits if not _lifecycle_signals(hit)]
                best, identity_confidence = _select_search_match(place, active_hits)
                if best is None:
                    if any(_lifecycle_signals(hit) for hit in hits):
                        raise LookupError("폐업·철거·이전 신호가 없는 동일 장소 후보를 찾지 못했습니다")
                    raise LookupError("권역 안에서 동일 장소로 확인할 수 있는 후보를 찾지 못했습니다")
                source_url = _safe_https_url(
                    f"https://www.openstreetmap.org/{best.get('osm_type')}/{best.get('osm_id')}"
                )
                names = best.get("namedetails") or {}
                extratags = best.get("extratags") or {}
                payload: dict[str, Any] = {
                    "source_url": source_url,
                    "coordinate_source": "nominatim_openstreetmap",
                    "coordinate_external_id": f"{best.get('osm_type')}/{best.get('osm_id')}",
                    "coordinate_confidence": identity_confidence,
                }
                local_name = _clean_text(names.get("name:id") or names.get("name"), 180)
                if local_name and not place.local_name:
                    payload["local_name"] = local_name
                if task.kind == "quality_visit":
                    hours = _clean_text(extratags.get("opening_hours"), 120)
                    if hours:
                        payload["best_time"] = f"운영시간 원문 확인: {hours}"
                _record_evidence(
                    db,
                    region_id=region.id,
                    run_id=run.id,
                    mission_id=mission.id,
                    work_item_id=work_item.id,
                    place_id=place.id,
                    source_type="nominatim_openstreetmap",
                    url=source_url,
                    title=_clean_text(best.get("display_name"), 300),
                    claim=f"{place.title}의 현지 표기와 위치 후보",
                    excerpt=_json({"namedetails": names, "extratags": extratags})[:2000],
                    confidence=identity_confidence,
                )
                _proposal(
                    db,
                    region_id=region.id,
                    run_id=run.id,
                    action="update",
                    title=f"장소 정보 보강: {place.title}",
                    payload=payload,
                    evidence="권역 제한 위치 검색으로 동일 장소 후보를 확인했습니다.",
                    source_urls=[source_url],
                    confidence=identity_confidence,
                    place_id=place.id,
                    stable_key=f"place:{place.id}:{task.kind}:{source_url}",
                )
            proposals += 1
            completed += 1
            task.status = "completed"
            task.result = "관리자 검토 제안을 생성했습니다"
            task.completed_at = _utcnow()
            work_item.status = "completed"
            work_item.stage = "review"
            work_item.state_summary = task.result
            work_item.completed_at = _utcnow()
            checkpoint = AgentCheckpoint(
                mission_id=mission.id,
                work_item_id=work_item.id,
                run_id=run.id,
                sequence=work_item.attempts,
                state_summary=task.result,
                decision="검토 제안 생성",
                new_facts_json=_json(["공개 출처 후보 확인"]),
                next_action_json=_json({"action": "admin_review"}),
                outcome="proposed",
            )
            db.add(checkpoint)
            recorder.assert_lease()
            db.commit()
        except AgentLeaseLostError:
            db.rollback()
            raise
        except Exception as exc:
            task.status = "blocked"
            task.result = f"{type(exc).__name__}: {_clean_text(str(exc), 300)}"
            task.retry_after = _utcnow() + timedelta(hours=24)
            work_item.status = "blocked"
            work_item.blocked_reason = task.result
            work_item.retry_condition = "24시간 뒤 또는 장소/출처 정보가 바뀌면 재시도"
            db.add(AgentCheckpoint(
                mission_id=mission.id,
                work_item_id=work_item.id,
                run_id=run.id,
                sequence=work_item.attempts,
                state_summary=task.result,
                decision="냉각 후 재시도",
                next_action_json=_json({"action": "retry_after", "hours": 24}),
                outcome="blocked",
            ))
            recorder.assert_lease()
            db.commit()
    recorder.step(
        "quality",
        "process_quality_backlog",
        "ok" if completed else ("blocked" if tasks else "no_yield"),
        f"품질 작업 {len(tasks)}건 중 {completed}건에서 검토 제안을 만들었습니다.",
        score_delta=completed,
        metadata={"selected": len(tasks), "completed": completed, "proposals": proposals},
    )
    return completed, proposals


def _duplicate_proposals(
    db: Session,
    run: AgentRun,
    regions: list[Region],
    recorder: _RunRecorder,
) -> int:
    created = 0
    for region in regions:
        places = (
            db.query(Place)
            .filter(Place.region_id == region.id, Place.merged_into_id.is_(None))
            .order_by(Place.id)
            .all()
        )
        for index, first in enumerate(places):
            first_names = {_normalized_name(first.title), _normalized_name(first.local_name)} - {""}
            for second in places[index + 1 :]:
                distance = _distance_m(first, second)
                second_names = {_normalized_name(second.title), _normalized_name(second.local_name)} - {""}
                same_name = bool(first_names & second_names)
                same_point = distance <= 20 and first.category == second.category
                if not ((same_name and distance <= 600) or same_point):
                    continue
                confidence = 0.86 if same_name and distance <= 100 else 0.68
                _proposal(
                    db,
                    region_id=region.id,
                    run_id=run.id,
                    action="merge",
                    title=f"중복 검토: {first.title} ↔ {second.title}",
                    payload={
                        "canonical_place_id": first.id,
                        "duplicate_place_id": second.id,
                        "distance_m": round(distance, 1),
                        "same_normalized_name": same_name,
                    },
                    evidence="정규화 명칭과 좌표 거리를 이용한 1차 중복 신호입니다. 관리자가 실제 동일성을 확인해야 합니다.",
                    source_urls=[first.source_url, second.source_url],
                    confidence=confidence,
                    place_id=first.id,
                    secondary_place_id=second.id,
                    stable_key=f"merge:{min(first.id, second.id)}:{max(first.id, second.id)}",
                )
                created += 1
                if created >= MAX_DUPLICATE_PROPOSALS_PER_RUN:
                    break
            if created >= MAX_DUPLICATE_PROPOSALS_PER_RUN:
                break
        if created >= MAX_DUPLICATE_PROPOSALS_PER_RUN:
            break
    recorder.assert_lease()
    db.commit()
    recorder.step(
        "verification",
        "duplicate_scan",
        "ok" if created else "no_yield",
        f"중복 검토 제안 {created}건을 만들었습니다.",
        score_delta=created * 0.5,
        metadata={"proposals": created},
    )
    return created


def _upsert_run_knowledge(
    db: Session,
    *,
    regions: list[Region],
    metrics: dict[str, Any],
    recorder: _RunRecorder,
) -> None:
    for region in regions:
        topic = f"region:{region.id}:curation_strategy"
        row = db.query(AgentKnowledge).filter(AgentKnowledge.topic == topic).first()
        if row is None:
            row = AgentKnowledge(
                topic=topic,
                title=f"{region.name_ko} 운영 조사 전략",
                scope="region",
                region_id=region.id,
                category="research_strategy",
                evidence_count=0,
                version=0,
            )
            db.add(row)
        row.content = _json({
            "last_metrics": metrics,
            "region_bbox": [region.south, region.west, region.north, region.east],
            "traveler_context": region.summary,
            "access_context": region.access_note,
        })
        row.summary = "신규 장소 발굴, 출처 교차 확인, 기존 장소 품질 공백을 순환 점검합니다."
        row.principles_json = _json([
            "관리자 승인 전에는 장소를 공개하지 않는다",
            "명칭과 좌표만으로 동일 장소를 단정하지 않는다",
            "방문·안전 정보는 공개 출처가 없으면 비워 둔다",
            "발리·주변 섬의 권역 경계와 이동 현실을 지킨다",
        ])
        row.next_actions_json = _json([
            "pending 품질 작업 우선 처리",
            "교차 출처가 부족한 후보 재검증",
            "오래된 접근·운영 정보 재확인",
        ])
        row.evidence_count = (row.evidence_count or 0) + int(metrics.get("verified_candidates", 0))
        row.version = (row.version or 0) + 1
    recorder.assert_lease()
    db.commit()


def _claim_run(db: Session, run_id: int) -> tuple[AgentRun, bool]:
    _active_run(db)
    db.commit()
    claim_started_at = _utcnow()
    affected = (
        db.query(AgentRun)
        .filter(
            AgentRun.id == run_id,
            AgentRun.status == "queued",
            AgentRun.active_slot == ACTIVE_SLOT,
        )
        .update({
            AgentRun.status: "running",
            AgentRun.summary: "운영 조사 실행 중",
            AgentRun.started_at: claim_started_at,
            AgentRun.finished_at: None,
        }, synchronize_session=False)
    )
    db.commit()
    db.expire_all()
    row = db.get(AgentRun, run_id)
    if row is None:
        raise LookupError("운영 조사 실행을 찾을 수 없습니다")
    return row, affected == 1


def execute_agent_run(
    db: Session,
    run_id: int,
    *,
    json_fetcher: JsonFetcher = fetch_json,
    page_fetcher: PageFetcher = fetch_page_facts,
) -> AgentRun:
    run, claimed = _claim_run(db, run_id)
    if not claimed:
        return run
    recorder = _RunRecorder(db, run.id, run.started_at)
    metrics: dict[str, Any] = {
        "discovery_candidates": 0,
        "verified_candidates": 0,
        "lifecycle_tasks": 0,
        "lifecycle_checked": 0,
        "lifecycle_flagged": 0,
        "lifecycle_proposals": 0,
        "quality_tasks": 0,
        "quality_completed": 0,
        "proposals": 0,
        "duplicate_proposals": 0,
    }
    try:
        regions = _regions_for_run(db, run)
        if not regions:
            raise LookupError("조사할 권역이 없습니다")
        recorder.step(
            "plan",
            "select_regions",
            "ok",
            f"{len(regions)}개 여행권역을 선택했습니다.",
            metadata={"region_ids": [region.id for region in regions], "mode": run.mode},
        )
        if run.mode in {"full", "discovery"}:
            try:
                discovery = run_discovery(
                    db,
                    region_id=run.region_id,
                    limit=80 if run.region_id is None else 40,
                    trigger="agent",
                )
                metrics["discovery_candidates"] = discovery.created_count
                recorder.step(
                    "discovery",
                    "overpass_scan",
                    "ok" if discovery.run.status in {"success", "partial"} else "failed",
                    discovery.run.summary,
                    score_delta=float(discovery.created_count),
                    metadata={
                        "batch_run_id": discovery.run.id,
                        "created": discovery.created_count,
                        "duplicates": discovery.duplicate_count,
                        "invalid": discovery.invalid_count,
                    },
                )
            except DiscoveryBusyError as exc:
                recorder.step(
                    "discovery",
                    "overpass_scan",
                    "blocked",
                    str(exc),
                    metadata={"active_discovery_run_id": exc.active_run_id},
                )
            verified, proposals = _enrich_candidates(
                db, run, recorder, page_fetcher=page_fetcher
            )
            recorder.assert_lease()
            metrics["verified_candidates"] = verified
            metrics["proposals"] += proposals
        if run.mode in {"full", "verification"}:
            metrics["lifecycle_tasks"] = _sync_lifecycle_tasks(db, regions, recorder)
            recorder.step(
                "verification",
                "sync_lifecycle_backlog",
                "ok",
                f"현재 운영 상태 재검증 작업 {metrics['lifecycle_tasks']}건을 동기화했습니다.",
                metadata={"open_tasks": metrics["lifecycle_tasks"]},
            )
            checked, flagged, proposals = _process_lifecycle_tasks(
                db,
                run,
                regions,
                recorder,
                json_fetcher=json_fetcher,
            )
            recorder.assert_lease()
            metrics["lifecycle_checked"] = checked
            metrics["lifecycle_flagged"] = flagged
            metrics["lifecycle_proposals"] = proposals
            metrics["proposals"] += proposals
        if run.mode in {"full", "quality"}:
            metrics["quality_tasks"] = _sync_quality_tasks(db, regions, recorder)
            recorder.step(
                "quality",
                "sync_quality_backlog",
                "ok",
                f"현재 품질 공백 작업 {metrics['quality_tasks']}건을 동기화했습니다.",
                metadata={"open_tasks": metrics["quality_tasks"]},
            )
            completed, proposals = _process_quality_tasks(
                db,
                run,
                regions,
                recorder,
                json_fetcher=json_fetcher,
            )
            recorder.assert_lease()
            metrics["quality_completed"] = completed
            metrics["proposals"] += proposals
        if run.mode in {"full", "verification"}:
            duplicate_count = _duplicate_proposals(db, run, regions, recorder)
            recorder.assert_lease()
            metrics["duplicate_proposals"] = duplicate_count
            metrics["proposals"] += duplicate_count
        _upsert_run_knowledge(db, regions=regions, metrics=metrics, recorder=recorder)
        recorder.step(
            "learn",
            "update_knowledge",
            "ok",
            "권역별 조사 전략과 다음 작업을 갱신했습니다.",
            score_delta=0.5,
        )
        run = (
            db.query(AgentRun)
            .filter(
                AgentRun.id == run_id,
                AgentRun.status == "running",
                AgentRun.active_slot == ACTIVE_SLOT,
                AgentRun.started_at == recorder.claim_started_at,
            )
            .with_for_update()
            .first()
        )
        if run is None:
            raise AgentLeaseLostError("새 실행이 운영 조사 임대를 인계했습니다")
        total_yield = (
            int(metrics["verified_candidates"])
            + int(metrics["lifecycle_checked"])
            + int(metrics["quality_completed"])
            + int(metrics["duplicate_proposals"])
        )
        run.status = "success" if total_yield > 0 else "partial"
        run.score = float(
            metrics["discovery_candidates"]
            + metrics["verified_candidates"] * 1.5
            + metrics["lifecycle_checked"] * 0.5
            + metrics["lifecycle_flagged"]
            + metrics["quality_completed"]
            + metrics["duplicate_proposals"] * 0.5
        )
        run.metrics_json = _json(metrics)
        run.summary = (
            f"신규 후보 {metrics['discovery_candidates']}건, 교차 확인 {metrics['verified_candidates']}건, "
            f"상태 재검증 {metrics['lifecycle_checked']}건(위험 {metrics['lifecycle_flagged']}건), "
            f"품질 제안 {metrics['quality_completed']}건, 중복 제안 {metrics['duplicate_proposals']}건"
        )
        run.finished_at = _utcnow()
        run.active_slot = None
        db.commit()
        db.refresh(run)
        return run
    except Exception as exc:
        db.rollback()
        if isinstance(exc, AgentLeaseLostError):
            raise
        failed = db.query(AgentRun).filter(AgentRun.id == run_id).with_for_update().first()
        if failed is not None and failed.status in ACTIVE_STATUSES:
            failed.status = "failed"
            failed.summary = f"운영 조사 실패: {type(exc).__name__}"
            failed.metrics_json = _json(metrics)
            failed.finished_at = _utcnow()
            failed.active_slot = None
            db.commit()
        raise
