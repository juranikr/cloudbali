from __future__ import annotations

import json
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from urllib.request import Request, urlopen

from sqlalchemy import case, func, or_, select
from sqlalchemy.orm import Session, joinedload, selectinload

from app.config import settings
from app.itinerary_models import TravelPlan, TravelPlanDay, TravelPlanItem, TravelPlanMember
from app.models import ChatMessage, Place, Region, RegionSnapshot, TripStop, User


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


def _ids(value: str) -> list[int]:
    return [int(item) for item in (value or "").split(",") if item.isdigit()]


def message_dict(row: ChatMessage) -> dict:
    return {
        "id": row.id,
        "region_id": row.region_id,
        "role": row.role,
        "content": row.content,
        "model": row.model,
        "place_ids": _ids(row.place_ids),
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
            .filter(Place.region_id == region.id)
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
) -> tuple[ChatMessage, list[Place]]:
    region = db.get(Region, region_id) if region_id else None
    if region_id and region is None:
        raise ValueError("선택한 여행권역을 찾을 수 없습니다")
    selected = db.get(Place, selected_place_id) if selected_place_id else None
    if selected_place_id and selected is None:
        raise ValueError("선택한 장소를 찾을 수 없습니다")

    user_row = ChatMessage(user_id=user.id, region_id=region_id, role="user", content=message.strip())
    db.add(user_row)
    db.commit()

    if region:
        ordering = []
        if selected_place_id:
            ordering.append(case((Place.id == selected_place_id, 0), else_=1))
        ordering.extend((Place.category, Place.title, Place.id))
        places = (
            db.query(Place)
            .options(joinedload(Place.region))
            .filter(Place.region_id == region.id)
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
    )
    db.add(assistant)
    db.commit()
    db.refresh(assistant)
    return assistant, grounded
