from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from urllib.request import Request, urlopen

from sqlalchemy.orm import Session, joinedload

from app.config import settings
from app.models import ChatMessage, Place, Region, TripStop, User


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

    query = db.query(Place).options(joinedload(Place.region))
    if region:
        query = query.filter(Place.region_id == region.id)
    places = query.order_by(Place.region_id, Place.category, Place.title).limit(40).all()
    trip_rows = (
        db.query(TripStop)
        .options(joinedload(TripStop.place).joinedload(Place.region))
        .filter(TripStop.user_id == user.id)
        .order_by(TripStop.day_number, TripStop.sort_order)
        .all()
    )
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
            "note": place.traveler_note,
        }
        for place in places
    ]
    recent = (
        db.query(ChatMessage)
        .filter(ChatMessage.user_id == user.id)
        .order_by(ChatMessage.id.desc())
        .limit(10)
        .all()
    )[::-1]
    scope = region.name_ko if region else "발리·누사 페니다·롬복·길리 전체"
    system = (
        "당신은 한국인 여행자를 위한 인도네시아 섬 여행 도우미입니다. "
        "제공된 지도 데이터와 일정만 사실로 단정하고, 운항·가격·날씨처럼 바뀌는 정보는 당일 재확인을 명시하세요. "
        "실제로 제공된 장소를 추천할 때는 이름을 정확히 쓰고 4곳 이내로 간결하게 답하세요. "
        "지역은 절대적인 경계가 아니라 검색 필터입니다. 전체 이동 동선을 먼저 생각하세요.\n"
        f"현지 날짜: {datetime.now(timezone(timedelta(hours=8))).date().isoformat()}\n"
        f"현재 범위: {scope}\n선택 장소: {selected.title if selected else '없음'}\n"
        f"내 일정: {json.dumps([{'day': row.day_number, 'place': row.place.title, 'island': row.place.region.island} for row in trip_rows], ensure_ascii=False)}\n"
        f"지도 장소: {json.dumps(place_context, ensure_ascii=False)}"
    )
    model_messages = [{"role": "system", "content": system}] + [
        {"role": row.role, "content": row.content} for row in recent
    ]

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
