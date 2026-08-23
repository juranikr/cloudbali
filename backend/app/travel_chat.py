from __future__ import annotations

import asyncio
import hashlib
import json
import re
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from urllib.request import Request, urlopen

from sqlalchemy import case, func, or_, select, tuple_
from sqlalchemy.orm import Session, joinedload, selectinload

from app.config import settings
from app.agent_models import AgentProposal
from app.itinerary_models import TravelPlan, TravelPlanDay, TravelPlanItem, TravelPlanMember
from app.models import (
    BatchRun,
    ChatMessage,
    ChatWork,
    DiscoveryCandidate,
    Place,
    Region,
    RegionSnapshot,
    TripStop,
    User,
)
from app.place_identity import distance_m, strongest_duplicate
from app.search_service import GeoBounds, search_external_places


MAX_CHAT_PLACES = 40
MAX_SHARED_PLANS = 3
MAX_SHARED_PLAN_DAYS = 7
MAX_SHARED_PLAN_ITEMS = 6
MAX_PLAN_ITEM_NOTE_CHARS = 240
MAX_PLAN_CONTEXT_CHARS = 10_000
MAX_PLACE_CONTEXT_CHARS = 18_000
MAX_TRIP_CONTEXT_CHARS = 4_000
MAX_GROQ_INPUT_CHARS = 40_000
MAX_SYSTEM_PROMPT_CHARS = 34_000
MAX_RESEARCH_CANDIDATES = 5


def _ids(value: str) -> list[int]:
    return [int(item) for item in (value or "").split(",") if item.isdigit()]


def _json_value(value: str, fallback):
    try:
        parsed = json.loads(value or "")
    except (json.JSONDecodeError, TypeError):
        return fallback
    return parsed


def message_dict(row: ChatMessage) -> dict:
    sources = _json_value(row.sources, [])
    candidates = _json_value(row.candidates, [])
    return {
        "id": row.id,
        "region_id": row.region_id,
        "role": row.role,
        "content": row.content,
        "model": row.model,
        "place_ids": _ids(row.place_ids),
        "sources": sources if isinstance(sources, list) else [],
        "candidates": candidates if isinstance(candidates, list) else [],
        "created_at": row.created_at,
    }


def _fallback_answer(message: str, places: list[Place], region: Region | None) -> tuple[str, list[int]]:
    words = [word.casefold() for word in message.replace("?", " ").split() if len(word) >= 2]
    ranked = sorted(
        places,
        key=lambda place: sum(
            word in " ".join([place.title, place.local_name, place.category, place.tags]).casefold()
            for word in words
        ),
        reverse=True,
    )
    picks = ranked[:3]
    scope = region.name_ko if region else "발리와 주변 섬 전체"
    if not picks:
        return f"{scope}에서 조건에 맞는 장소를 아직 찾지 못했어요. 원하는 분위기와 이동 가능한 시간을 조금 더 알려주세요.", []
    names = ", ".join(place.title for place in picks)
    return (
        f"{scope}의 현재 지도 데이터에서는 {names} 순서로 살펴보세요. "
        "날씨·조수·배편 민감 표시가 있는 장소는 출발 당일 운영 정보를 다시 확인하는 것이 좋아요.",
        [place.id for place in picks],
    )


def _is_registration_request(message: str) -> bool:
    return "등록" in message.replace(" ", "")


def _is_research_request(message: str) -> bool:
    compact = message.replace(" ", "").casefold()
    explicit = (
        "새장소", "새로운장소", "신규장소", "지도에없는", "외부검색",
        "검색해서", "검색해줘", "검색해봐", "웹에서찾",
    )
    if any(token in compact for token in explicit):
        return True
    categories = ("맛집", "식당", "카페", "해변", "사원", "폭포", "숙소", "호텔", "서핑", "다이빙")
    return ("새로운" in compact or "신규" in compact) and any(token in compact for token in categories)


def _research_query(message: str, region: Region | None) -> str:
    category_terms = (
        (("맛집", "식당", "음식", "레스토랑"), "restaurant"),
        (("카페", "커피"), "cafe"),
        (("해변", "비치"), "beach"),
        (("사원", "절"), "temple"),
        (("폭포",), "waterfall"),
        (("숙소", "호텔", "리조트", "빌라"), "hotel"),
        (("서핑",), "surf"),
        (("다이빙", "스노클링"), "dive"),
        (("스파", "마사지"), "spa"),
    )
    quoted = re.search(r"[\"'“”]([^\"'“”]{2,80})[\"'“”]", message)
    subject = quoted.group(1).strip() if quoted else ""
    if not subject:
        subject = next(
            (english for terms, english in category_terms if any(term in message for term in terms)),
            "",
        )
    if not subject:
        subject = message
        for token in (
            "새로운", "신규", "새 장소", "장소", "지도에 없는", "외부", "웹에서",
            "검색해서", "검색해줘", "검색해 줘", "검색해봐", "찾아서", "찾아줘", "등록해줘", "등록",
        ):
            subject = subject.replace(token, " ")
        subject = " ".join(subject.split())[:80]
    location = region.name_local if region is not None else "Bali Lombok"
    return " ".join(value for value in (subject, location) if value).strip()[:100]


def _region_for_coordinate(db: Session, lat: float, lng: float, selected: Region | None) -> Region | None:
    if selected is not None and selected.south <= lat <= selected.north and selected.west <= lng <= selected.east:
        return selected
    regions = db.query(Region).order_by(Region.sort_order, Region.id).all()
    containing = [
        region for region in regions
        if region.south <= lat <= region.north and region.west <= lng <= region.east
    ]
    if containing:
        return min(containing, key=lambda row: (row.north - row.south) * (row.east - row.west))
    return min(
        regions,
        key=lambda row: distance_m(lat, lng, row.center_lat, row.center_lng),
        default=None,
    )


def _research_candidates(db: Session, message: str, region: Region | None) -> tuple[str, list[dict]]:
    query = _research_query(message, region)
    bounds = (
        GeoBounds(region.west, region.south, region.east, region.north, region.name_local)
        if region is not None else None
    )
    try:
        hits = asyncio.run(search_external_places(
            query,
            bounds=bounds,
            user_agent=settings.geocoder_user_agent,
            timeout_seconds=min(settings.geocoder_timeout_seconds, 6.0),
            per_source_limit=5,
            result_limit=MAX_RESEARCH_CANDIDATES,
        ))
    except Exception:
        hits = []
    output: list[dict] = []
    for hit in hits[:MAX_RESEARCH_CANDIDATES]:
        hit_region = _region_for_coordinate(db, hit.lat, hit.lng, region)
        if hit_region is None:
            continue
        values = hit.as_dict()
        source_urls = [
            str(url) for url in values.get("source_urls", [])
            if str(url).startswith("https://")
        ]
        source_urls = list(dict.fromkeys([hit.source_url, *source_urls]))
        output.append({
            "key": str(values.get("key") or f"{hit.source}:{hit.external_id}"),
            "title": hit.title,
            "display_name": hit.display_name,
            "region_id": hit_region.id,
            "category": hit.category,
            "status": "grounded",
            "source": hit.source,
            "source_urls": source_urls[:6],
            "external_id": hit.external_id,
            "external_ids": values.get("external_ids", {}),
            "lat": hit.lat,
            "lng": hit.lng,
            "confidence": hit.confidence,
            "cross_checked": hit.cross_checked,
            "storage_allowed": hit.storage_allowed,
            "license": hit.license,
            "attribution": hit.attribution,
            "proposal_id": None,
        })
    return query, output


def _latest_chat_work(db: Session, user_id: int, region_id: int | None) -> ChatWork | None:
    query = db.query(ChatWork).filter(ChatWork.user_id == user_id, ChatWork.status == "active")
    if region_id is None:
        query = query.filter(ChatWork.region_id.is_(None))
    else:
        query = query.filter(ChatWork.region_id == region_id)
    return query.order_by(ChatWork.updated_at.desc(), ChatWork.id.desc()).first()


def _work_payload(row: ChatWork | None) -> dict:
    if row is None:
        return {}
    state = _json_value(row.state, {})
    return {
        "id": row.id,
        "status": row.status,
        "action": row.action,
        "scope": row.scope,
        "subject": row.subject,
        "goal": row.goal,
        "requested_count": row.requested_count,
        "state": state if isinstance(state, dict) else {},
        "updated_at": row.updated_at,
    }


def _save_research_work(
    db: Session,
    *,
    user: User,
    region: Region | None,
    query: str,
    message: str,
    candidates: list[dict],
) -> ChatWork:
    row = _latest_chat_work(db, user.id, region.id if region else None)
    if row is None:
        row = ChatWork(
            user_id=user.id,
            region_id=region.id if region else None,
            status="active",
            scope="region" if region else "all_islands",
        )
        db.add(row)
    row.action = "research"
    row.subject = query
    row.goal = message.strip()
    row.requested_count = len(candidates)
    row.state = json.dumps({"candidates": candidates}, ensure_ascii=False, separators=(",", ":"))
    db.commit()
    db.refresh(row)
    return row


def _selected_candidates(message: str, candidates: list[dict]) -> list[dict]:
    if not candidates:
        return []
    compact = message.replace(" ", "")
    if any(token in compact for token in ("전부등록", "모두등록", "다등록")):
        return candidates[:MAX_RESEARCH_CANDIDATES]
    for index, item in enumerate(candidates, start=1):
        if str(index) + "번" in compact or str(item.get("title") or "").casefold() in message.casefold():
            return [item]
    ordinal_tokens = (("첫", 0), ("두번째", 1), ("둘째", 1), ("세번째", 2), ("셋째", 2))
    for token, index in ordinal_tokens:
        if token in compact and index < len(candidates):
            return [candidates[index]]
    return [candidates[0]]


def _proposal_key(source: str, external_id: str) -> str:
    stable_key = f"candidate:{source}:{external_id}"
    return hashlib.sha256(f"create\n{stable_key}".encode("utf-8")).hexdigest()


def _register_candidates(
    db: Session,
    *,
    user: User,
    work: ChatWork,
    selected: list[dict],
) -> list[dict]:
    batch = BatchRun(
        kind="chat_place_research",
        status="running",
        trigger="manual",
        scanned_count=len(selected),
        updated_count=0,
        summary="대화에서 선택한 공개 출처 후보를 관리자 검토 대기로 전환 중",
    )
    db.add(batch)
    db.flush()
    updated = 0
    output: list[dict] = []

    # One chat turn can register candidates from several regions. Acquire every
    # existing row in the same global order used by agent approval/merge before
    # processing any one item; otherwise item 1 can hold a Place while item 2
    # waits for a Candidate that a merge already holds while waiting for item
    # 1's Place. Region locks serialize absent-row creation between chat turns.
    prepared: list[tuple[dict, Region | None, str, str, list[str], str]] = []
    for raw in selected:
        item = dict(raw)
        region = db.get(Region, item.get("region_id"))
        source = str(item.get("source") or "").strip()[:40]
        external_id = str(item.get("external_id") or "").strip()[:120]
        source_urls = [
            str(url) for url in item.get("source_urls", [])
            if str(url).startswith("https://")
        ]
        if (
            region is None or not source or not external_id or not source_urls
            or item.get("storage_allowed") is not True
            or not isinstance(item.get("lat"), (int, float))
            or not isinstance(item.get("lng"), (int, float))
        ):
            item["status"] = "not_storable"
            prepared.append((item, None, source, external_id, source_urls, ""))
        else:
            prepared.append((
                item,
                region,
                source,
                external_id,
                source_urls,
                _proposal_key(source, external_id),
            ))

    valid = [entry for entry in prepared if entry[1] is not None]
    proposal_keys = sorted({entry[5] for entry in valid})
    proposals = (
        db.query(AgentProposal)
        .filter(AgentProposal.proposal_key.in_(proposal_keys))
        .order_by(AgentProposal.id)
        .populate_existing()
        .with_for_update()
        .all()
        if proposal_keys else []
    )
    proposal_by_key = {row.proposal_key: row for row in proposals}

    candidate_keys = sorted({(entry[2], entry[3]) for entry in valid})
    candidates = (
        db.query(DiscoveryCandidate)
        .filter(tuple_(DiscoveryCandidate.source, DiscoveryCandidate.external_id).in_(candidate_keys))
        .order_by(DiscoveryCandidate.id)
        .populate_existing()
        .with_for_update()
        .all()
        if candidate_keys else []
    )
    candidate_by_key = {(row.source, row.external_id): row for row in candidates}

    region_ids = sorted({entry[1].id for entry in valid if entry[1] is not None})
    locked_regions = (
        db.query(Region)
        .filter(Region.id.in_(region_ids))
        .order_by(Region.id)
        .populate_existing()
        .with_for_update()
        .all()
        if region_ids else []
    )
    region_by_id = {row.id: row for row in locked_regions}

    # A discovery worker does not take Region locks, but it does take Candidate
    # then Place locks. Re-read absent identities after the Region barrier and
    # again after the global Place barrier so whichever writer won is visible.
    missing_proposals = [key for key in proposal_keys if key not in proposal_by_key]
    if missing_proposals:
        for row in db.query(AgentProposal).filter(AgentProposal.proposal_key.in_(missing_proposals)).all():
            proposal_by_key[row.proposal_key] = row
    missing_candidates = [key for key in candidate_keys if key not in candidate_by_key]
    if missing_candidates:
        for row in db.query(DiscoveryCandidate).filter(
            tuple_(DiscoveryCandidate.source, DiscoveryCandidate.external_id).in_(missing_candidates)
        ).all():
            candidate_by_key[(row.source, row.external_id)] = row

    if region_ids:
        db.query(Place).filter(
            Place.region_id.in_(region_ids),
            Place.merged_into_id.is_(None),
        ).order_by(Place.id).populate_existing().with_for_update().all()

    missing_proposals = [key for key in proposal_keys if key not in proposal_by_key]
    if missing_proposals:
        for row in db.query(AgentProposal).filter(AgentProposal.proposal_key.in_(missing_proposals)).all():
            proposal_by_key[row.proposal_key] = row
    missing_candidates = [key for key in candidate_keys if key not in candidate_by_key]
    if missing_candidates:
        for row in db.query(DiscoveryCandidate).filter(
            tuple_(DiscoveryCandidate.source, DiscoveryCandidate.external_id).in_(missing_candidates)
        ).all():
            candidate_by_key[(row.source, row.external_id)] = row

    for item, preview_region, source, external_id, source_urls, proposal_key in prepared:
        if preview_region is None:
            output.append(item)
            continue
        region = region_by_id.get(preview_region.id)
        if region is None:
            item["status"] = "not_storable"
            output.append(item)
            continue
        proposal = proposal_by_key.get(proposal_key)
        candidate_key = (source, external_id)
        candidate = candidate_by_key.get(candidate_key)
        if candidate is None:
            duplicate = strongest_duplicate(
                db=db,
                title=str(item.get("title") or ""),
                lat=float(item["lat"]),
                lng=float(item["lng"]),
                category=str(item.get("category") or "other"),
                region_id=region.id,
            )
            candidate = DiscoveryCandidate(
                discovery_run_id=batch.id,
                region_id=region.id,
                source=source,
                external_id=external_id,
                source_url=source_urls[0],
                title=str(item.get("title") or "")[:180],
                local_name=str(item.get("title") or "")[:180],
                description="공개 지도·지식 출처에서 대화형 검색으로 찾은 장소 후보입니다.",
                area=region.name_ko,
                category=str(item.get("category") or "other")[:30],
                lat=float(item["lat"]),
                lng=float(item["lng"]),
                confidence=max(0.0, min(float(item.get("confidence") or 0.5), 1.0)),
                evidence=json.dumps({
                    "coordinate_crs": "WGS84",
                    "chat_research": {
                        "requested_by_user_id": user.id,
                        "source_urls": source_urls,
                        "external_ids": item.get("external_ids", {}),
                        "cross_checked": bool(item.get("cross_checked")),
                        "license": item.get("license", ""),
                        "attribution": item.get("attribution", ""),
                    },
                }, ensure_ascii=False, separators=(",", ":")),
                tags="대화검색," + source,
                status="duplicate" if duplicate else "pending",
                duplicate_place_id=duplicate.place.id if duplicate else None,
            )
            db.add(candidate)
            db.flush()
            candidate_by_key[candidate_key] = candidate
        if candidate.status == "approved" and candidate.result_place_id:
            item["status"] = "registered"
            item["place_id"] = candidate.result_place_id
            output.append(item)
            continue
        if candidate.status == "rejected":
            item["status"] = "rejected"
            output.append(item)
            continue
        if proposal is None:
            payload = {
                "candidate_id": candidate.id,
                "duplicate_place_id": candidate.duplicate_place_id,
                "requires_force": candidate.status == "duplicate",
                "title": candidate.title,
                "local_name": candidate.local_name,
                "description": candidate.description,
                "area": candidate.area,
                "category": candidate.category,
                "lat": candidate.lat,
                "lng": candidate.lng,
                "tags": ["대화검색", source],
                "source_url": candidate.source_url,
                "coordinate_source": source,
                "coordinate_external_id": external_id,
                "coordinate_confidence": candidate.confidence,
                "independent_source_count": len(source_urls),
            }
            proposal = AgentProposal(
                region_id=region.id,
                discovery_candidate_id=candidate.id,
                action="create",
                title=f"대화에서 찾은 신규 장소: {candidate.title}",
                payload_json=json.dumps(payload, ensure_ascii=False, separators=(",", ":")),
                evidence="사용자가 선택한 외부 검색 결과입니다. 공개 전 관리자가 위치와 최신 운영 정보를 검토합니다.",
                source_urls_json=json.dumps(source_urls, ensure_ascii=False, separators=(",", ":")),
                confidence=candidate.confidence,
                proposal_key=proposal_key,
                status="pending",
            )
            db.add(proposal)
            db.flush()
            proposal_by_key[proposal_key] = proposal
            updated += 1
        item["proposal_id"] = proposal.id
        item["status"] = "proposed" if proposal.status == "pending" else proposal.status
        output.append(item)
    batch.updated_count = updated
    batch.status = "success" if updated or output else "partial"
    batch.summary = f"관리자 검토 제안 {updated}건 생성"
    batch.finished_at = datetime.now(timezone.utc)
    state = _json_value(work.state, {})
    all_candidates = state.get("candidates", []) if isinstance(state, dict) else []
    by_key = {str(item.get("key") or ""): item for item in output}
    work.state = json.dumps({
        "candidates": [by_key.get(str(item.get("key") or ""), item) for item in all_candidates]
    }, ensure_ascii=False, separators=(",", ":"))
    work.action = "review"
    db.commit()
    db.refresh(work)
    return output


def _research_answer(candidates: list[dict], region: Region | None) -> str:
    scope = region.name_ko if region else "발리·누사·롬복·길리 전체"
    if not candidates:
        return f"{scope}의 공개 지도와 지식 출처에서 새 후보를 찾지 못했어요. 장소명이나 종류를 더 구체적으로 알려주세요."
    lines = [
        f"{index}. {item['title']} · {item.get('category', 'other')}"
        + (" · 두 출처 이상 교차 확인" if item.get("cross_checked") else "")
        for index, item in enumerate(candidates, start=1)
    ]
    return (
        f"{scope}에서 공개 출처로 확인 가능한 후보 {len(candidates)}곳을 찾았어요.\n"
        + "\n".join(lines)
        + "\n지도에는 아직 공개하지 않았습니다. ‘1번 등록해줘’처럼 말하면 관리자 검토 대기로 올릴게요."
    )


def _registration_answer(candidates: list[dict]) -> str:
    proposed = [item for item in candidates if item.get("status") == "proposed"]
    registered = [item for item in candidates if item.get("status") == "registered"]
    blocked = [item for item in candidates if item.get("status") not in {"proposed", "registered", "approved"}]
    parts: list[str] = []
    if proposed:
        parts.append(f"{', '.join(item['title'] for item in proposed)} 후보를 관리자 검토 대기에 올렸어요")
    if registered:
        parts.append(f"{', '.join(item['title'] for item in registered)}은(는) 이미 지도에 등록되어 있어요")
    if blocked:
        parts.append("나머지 후보는 출처·저장 조건을 충족하지 않아 등록하지 않았어요")
    return ". ".join(parts) + ". 승인 전에는 공개 지도에 나타나지 않습니다."


def _groq_answer(messages: list[dict[str, str]]) -> str:
    payload = json.dumps(
        {
            "model": settings.groq_chat_model,
            "messages": messages,
            "temperature": 0.25,
            "max_completion_tokens": 1200,
        }
    ).encode("utf-8")
    request = Request(
        "https://api.groq.com/openai/v1/chat/completions",
        data=payload,
        headers={
            "Authorization": "Bearer " + settings.groq_api_key,
            "Content-Type": "application/json",
            "User-Agent": "cloudbali-travel-chat/1.0",
        },
        method="POST",
    )
    with urlopen(request, timeout=settings.groq_timeout_seconds) as response:
        result = json.loads(response.read().decode("utf-8"))
    return str(result["choices"][0]["message"]["content"]).strip()


def _trim_text(value: str, limit: int) -> str:
    cleaned = " ".join((value or "").split())
    if len(cleaned) <= limit:
        return cleaned
    return cleaned[: max(0, limit - 1)].rstrip() + "…"


def _compact_json(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def _bounded_list_json(values: list[dict], limit: int) -> str:
    bounded = list(values)
    rendered = _compact_json(bounded)
    while bounded and len(rendered) > limit:
        bounded.pop()
        rendered = _compact_json(bounded)
    return rendered


def _bounded_plan_context_json(values: list[dict], limit: int = MAX_PLAN_CONTEXT_CHARS) -> str:
    """Keep valid JSON while pruning the least-recent plan details first."""

    bounded = deepcopy(values)
    rendered = _compact_json(bounded)
    while bounded and len(rendered) > limit:
        last_plan = bounded[-1]
        days = last_plan.get("days", [])
        if days:
            items = days[-1].get("items", [])
            if items:
                items.pop()
            else:
                days.pop()
        else:
            bounded.pop()
        rendered = _compact_json(bounded)
    return rendered


def _balanced_places(
    db: Session,
    *,
    selected_place_id: int | None = None,
    limit: int = MAX_CHAT_PLACES,
) -> list[Place]:
    """Return a deterministic round-robin sample so every configured region is represented."""

    if limit <= 0:
        return []
    regions = db.query(Region).order_by(Region.sort_order, Region.id).all()
    buckets: list[list[Place]] = []
    for region in regions:
        ordering = []
        if selected_place_id:
            ordering.append(case((Place.id == selected_place_id, 0), else_=1))
        ordering.extend((Place.category, Place.title, Place.id))
        buckets.append(
            db.query(Place)
            .options(joinedload(Place.region))
            .filter(Place.region_id == region.id, Place.merged_into_id.is_(None))
            .order_by(*ordering)
            .limit(limit)
            .all()
        )

    selected: list[Place] = []
    bucket_index = 0
    while len(selected) < limit:
        appended = False
        for bucket in buckets:
            if bucket_index < len(bucket):
                selected.append(bucket[bucket_index])
                appended = True
                if len(selected) == limit:
                    break
        if not appended:
            break
        bucket_index += 1
    return selected


def _bounded_model_messages(system: str, recent: list[ChatMessage]) -> list[dict[str, str]]:
    """Keep the newest conversation turns within Groq's explicit character budget."""

    bounded_system = system[:MAX_SYSTEM_PROMPT_CHARS]
    system_message = {"role": "system", "content": bounded_system}
    history: list[dict[str, str]] = []
    for row in reversed(recent):
        message = {"role": row.role, "content": row.content}
        candidate_history = [message, *history]
        candidate = [system_message, *candidate_history]
        if len(_compact_json(candidate)) <= MAX_GROQ_INPUT_CHARS:
            history = candidate_history
    messages = [system_message, *history]
    if len(_compact_json(messages)) > MAX_GROQ_INPUT_CHARS:
        available = max(0, MAX_GROQ_INPUT_CHARS - len(_compact_json([{**system_message, "content": ""}])))
        messages[0]["content"] = bounded_system[:available]
    return messages


def answer_chat(
    db: Session,
    *,
    user: User,
    message: str,
    region_id: int | None,
    selected_place_id: int | None,
) -> tuple[ChatMessage, list[Place], dict]:
    region = db.get(Region, region_id) if region_id else None
    if region_id and region is None:
        raise ValueError("선택한 여행권역을 찾을 수 없습니다")
    selected = (
        db.query(Place).filter(
            Place.id == selected_place_id,
            Place.merged_into_id.is_(None),
        ).first()
        if selected_place_id else None
    )
    if selected_place_id and selected is None:
        raise ValueError("선택한 장소를 찾을 수 없습니다")

    user_row = ChatMessage(user_id=user.id, region_id=region_id, role="user", content=message.strip())
    db.add(user_row)
    db.commit()

    registration_requested = _is_registration_request(message)
    research_requested = _is_research_request(message)
    work = _latest_chat_work(db, user.id, region_id)
    if research_requested:
        query, candidates = _research_candidates(db, message, region)
        work = _save_research_work(
            db,
            user=user,
            region=region,
            query=query,
            message=message,
            candidates=candidates,
        )
        selected_candidates = _selected_candidates(message, candidates) if registration_requested else []
        if selected_candidates:
            displayed = _register_candidates(db, user=user, work=work, selected=selected_candidates)
            answer = _registration_answer(displayed)
            candidates_for_message = candidates
            updated_by_key = {str(item.get("key") or ""): item for item in displayed}
            candidates_for_message = [
                updated_by_key.get(str(item.get("key") or ""), item)
                for item in candidates_for_message
            ]
        else:
            displayed = candidates
            candidates_for_message = candidates
            answer = _research_answer(candidates, region)
        sources = list(dict.fromkeys(
            url for item in displayed for url in item.get("source_urls", [])
            if str(url).startswith("https://")
        ))
        assistant = ChatMessage(
            user_id=user.id,
            region_id=region_id,
            role="assistant",
            content=answer,
            model="external-grounded",
            place_ids=",".join(
                str(item["place_id"]) for item in displayed if item.get("place_id")
            ),
            sources=json.dumps(sources, ensure_ascii=False),
            candidates=json.dumps(candidates_for_message, ensure_ascii=False, separators=(",", ":")),
            tool_trace=json.dumps([{
                "tool": "external_place_search",
                "query": query,
                "outcome": "ok" if candidates else "no_yield",
                "candidate_count": len(candidates),
                "source_urls": sources,
            }], ensure_ascii=False, separators=(",", ":")),
        )
        db.add(assistant)
        db.commit()
        db.refresh(assistant)
        return assistant, [], _work_payload(work)

    if registration_requested:
        state = _json_value(work.state, {}) if work is not None else {}
        candidates = state.get("candidates", []) if isinstance(state, dict) else []
        selected_candidates = _selected_candidates(message, candidates)
        if work is not None and selected_candidates:
            displayed = _register_candidates(db, user=user, work=work, selected=selected_candidates)
            answer = _registration_answer(displayed)
            sources = list(dict.fromkeys(
                url for item in displayed for url in item.get("source_urls", [])
                if str(url).startswith("https://")
            ))
            assistant = ChatMessage(
                user_id=user.id,
                region_id=region_id,
                role="assistant",
                content=answer,
                model="external-grounded",
                place_ids=",".join(
                    str(item["place_id"]) for item in displayed if item.get("place_id")
                ),
                sources=json.dumps(sources, ensure_ascii=False),
                candidates=json.dumps(displayed, ensure_ascii=False, separators=(",", ":")),
                tool_trace=json.dumps([{
                    "tool": "queue_grounded_candidate_review",
                    "outcome": "ok" if displayed else "no_yield",
                    "candidate_keys": [item.get("key") for item in displayed],
                }], ensure_ascii=False, separators=(",", ":")),
            )
        else:
            assistant = ChatMessage(
                user_id=user.id,
                region_id=region_id,
                role="assistant",
                content="먼저 ‘새로운 카페 검색해줘’처럼 공개 출처에서 후보를 찾아 달라고 해주세요. 확인된 후보만 등록 검토로 넘길 수 있어요.",
                model="external-grounded",
                sources="[]",
                candidates="[]",
                tool_trace='[{"tool":"resume_research","outcome":"no_candidate"}]',
            )
        db.add(assistant)
        db.commit()
        db.refresh(assistant)
        return assistant, [], _work_payload(work)

    if region:
        ordering = []
        if selected_place_id:
            ordering.append(case((Place.id == selected_place_id, 0), else_=1))
        ordering.extend((Place.category, Place.title, Place.id))
        places = (
            db.query(Place)
            .options(joinedload(Place.region))
            .filter(Place.region_id == region.id, Place.merged_into_id.is_(None))
            .order_by(*ordering)
            .limit(MAX_CHAT_PLACES)
            .all()
        )
    else:
        places = _balanced_places(db, selected_place_id=selected_place_id)
    trip_rows = (
        db.query(TripStop)
        .options(joinedload(TripStop.place).joinedload(Place.region))
        .filter(TripStop.user_id == user.id)
        .order_by(TripStop.day_number, TripStop.sort_order)
        .all()
    )
    latest_condition_ids = (
        select(func.max(RegionSnapshot.id))
        .group_by(RegionSnapshot.region_id)
    )
    condition_query = (
        db.query(RegionSnapshot)
        .options(joinedload(RegionSnapshot.region))
        .filter(RegionSnapshot.id.in_(latest_condition_ids))
    )
    if region:
        condition_query = condition_query.filter(RegionSnapshot.region_id == region.id)
    weather_context = [
        {
            "region": snapshot.region.name_ko,
            "temperature_c": snapshot.temperature_c,
            "precipitation_mm": snapshot.precipitation_mm,
            "wind_kph": snapshot.wind_kph,
            "summary": snapshot.summary,
            "observed_at": snapshot.observed_at.isoformat(),
        }
        for snapshot in condition_query.order_by(RegionSnapshot.region_id).all()
    ]
    travel_plans = (
        db.query(TravelPlan)
        .outerjoin(TravelPlanMember, TravelPlanMember.plan_id == TravelPlan.id)
        .options(
            selectinload(TravelPlan.days)
            .selectinload(TravelPlanDay.items)
            .joinedload(TravelPlanItem.place)
            .joinedload(Place.region)
        )
        .filter(or_(TravelPlan.owner_id == user.id, TravelPlanMember.user_id == user.id))
        .distinct()
        .order_by(TravelPlan.updated_at.desc(), TravelPlan.id.desc())
        .limit(MAX_SHARED_PLANS)
        .all()
    )
    plan_context = [
        {
            "title": plan.title,
            "start_date": plan.start_date.isoformat(),
            "end_date": plan.end_date.isoformat(),
            "timezone": plan.timezone,
            "days": [
                {
                    "date": day.calendar_date.isoformat(),
                    "title": day.title,
                    "items": [
                        {
                            "place": item.place.title,
                            "region": item.place.region.name_ko,
                            "island": item.place.region.island,
                            "start_time": item.start_time.isoformat(timespec="minutes") if item.start_time else None,
                            "end_time": item.end_time.isoformat(timespec="minutes") if item.end_time else None,
                            "note": _trim_text(item.note, MAX_PLAN_ITEM_NOTE_CHARS),
                        }
                        for item in day.items[:MAX_SHARED_PLAN_ITEMS]
                    ],
                }
                for day in plan.days[:MAX_SHARED_PLAN_DAYS]
            ],
        }
        for plan in travel_plans
    ]
    plan_context_json = _bounded_plan_context_json(plan_context)
    place_context = [
        {
            "id": place.id,
            "name": place.title,
            "region": place.region.name_ko,
            "island": place.region.island,
            "category": place.category,
            "best_time": place.best_time,
            "duration_minutes": place.duration_minutes,
            "conditions": [
                key
                for key, active in {
                    "weather": place.weather_sensitive,
                    "tide": place.tide_sensitive,
                    "ferry": place.ferry_sensitive,
                    "booking": place.booking_required,
                }.items()
                if active
            ],
            "note": _trim_text(place.traveler_note, MAX_PLAN_ITEM_NOTE_CHARS),
        }
        for place in places
    ]
    place_context_json = _bounded_list_json(place_context, MAX_PLACE_CONTEXT_CHARS)
    recent = (
        db.query(ChatMessage)
        .filter(ChatMessage.user_id == user.id)
        .order_by(ChatMessage.id.desc())
        .limit(10)
        .all()
    )[::-1]
    scope = region.name_ko if region else "발리·누사 페니다·롬복·길리 전체"
    trip_context_json = _bounded_list_json(
        [
            {"day": row.day_number, "place": row.place.title, "island": row.place.region.island}
            for row in trip_rows
        ],
        MAX_TRIP_CONTEXT_CHARS,
    )
    system = (
        "당신은 한국인 여행자를 위한 인도네시아 섬 여행 도우미입니다. "
        "제공된 지도 데이터·일정·권역 날씨만 사실로 단정하고, 운항·가격처럼 바뀌는 정보는 당일 재확인을 명시하세요. "
        "권역 날씨는 관측 시각이 있는 현재 상태일 뿐 예보가 아니므로 일정 날짜의 날씨처럼 표현하지 마세요. "
        "비·강풍이 관측되면 날씨·조수·배편 민감 장소에 구체적인 재확인 경고를 붙이세요. "
        "실제로 제공된 장소를 추천할 때는 이름을 정확히 쓰고 4곳 이내로 간결하게 답하세요. "
        "지역은 절대적인 경계가 아니라 검색 필터입니다. 전체 이동 동선을 먼저 생각하세요.\n"
        f"현지 날짜: {datetime.now(timezone(timedelta(hours=8))).date().isoformat()}\n"
        f"현재 범위: {scope}\n선택 장소: {selected.title if selected else '없음'}\n"
        f"빠른 DAY 보관함: {trip_context_json}\n"
        f"날짜가 있는 공유 여행 계획: {plan_context_json}\n"
        f"권역 현재 날씨: {_compact_json(weather_context)}\n"
        f"지도 장소: {place_context_json}"
    )
    model_messages = _bounded_model_messages(system, recent)

    answer = ""
    model = "local-grounded"
    if settings.groq_api_key:
        try:
            answer = _groq_answer(model_messages)
            model = settings.groq_chat_model
        except Exception:
            answer = ""
    fallback_ids: list[int] = []
    if not answer:
        answer, fallback_ids = _fallback_answer(message, places, region)

    grounded = [place for place in places if place.title.casefold() in answer.casefold()]
    if not grounded and fallback_ids:
        grounded = [place for place in places if place.id in fallback_ids]
    grounded = grounded[:4]
    assistant = ChatMessage(
        user_id=user.id,
        region_id=region_id,
        role="assistant",
        content=answer,
        model=model,
        place_ids=",".join(str(place.id) for place in grounded),
        sources=json.dumps(
            list(dict.fromkeys(place.source_url for place in grounded if place.source_url)),
            ensure_ascii=False,
        ),
        candidates="[]",
        tool_trace=json.dumps([{
            "tool": "local_travel_context",
            "outcome": "ok" if grounded else "no_yield",
            "place_ids": [place.id for place in grounded],
        }], ensure_ascii=False, separators=(",", ":")),
    )
    db.add(assistant)
    db.commit()
    db.refresh(assistant)
    return assistant, grounded, _work_payload(work)
