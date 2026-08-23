from __future__ import annotations

import json
from collections import Counter
from typing import Any

from sqlalchemy.orm import Session

from app.agent_models import AgentKnowledge
from app.models import Place, Region


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def _managed_documents(db: Session) -> list[dict[str, Any]]:
    """Build the small, deterministic playbooks used by Bali curation.

    Rebuilding knowledge must not invent live opening hours or erase evidence
    learned by previous runs.  These documents therefore contain only stable
    editorial rules and summaries derived from the configured regions and
    current active-place coverage.
    """

    documents: list[dict[str, Any]] = [
        {
            "topic": "core:editorial-standard",
            "title": "발리 장소 정보 편집 기준",
            "category": "quality",
            "summary": "여행자가 현장에서 판단할 수 있는 짧고 검증 가능한 정보만 공개 장소에 반영합니다.",
            "principles": [
                "좌표는 WGS84로 저장하고 원본 공급자와 외부 식별자를 함께 보존합니다.",
                "영업시간·가격·운항 정보처럼 변하는 사실은 출처와 확인 시점을 우선합니다.",
                "조사 과정과 실행 로그는 장소 설명이 아니라 운영 이력에 남깁니다.",
                "새 장소·병합·공개 정보 수정은 관리자의 검토 전에는 게시하지 않습니다.",
            ],
            "next_actions": [],
            "quality_score": 1.0,
        },
        {
            "topic": "core:island-travel-safety",
            "title": "섬 여행 안전·이동 검증 원칙",
            "category": "safety",
            "summary": "발리와 주변 섬은 날씨·파도·조수·선박 운항 변화가 동선에 직접 영향을 주므로 방문 직전에 다시 확인합니다.",
            "principles": [
                "해변·서핑·다이빙 장소는 날씨와 해상 상태를 함께 확인합니다.",
                "누사·길리·롬복 이동은 선착장, 운항사 공지와 결항 가능성을 별도로 확인합니다.",
                "권역 간 육로 이동 시간은 거리만으로 확정하지 않고 혼잡과 도로 상태를 고려합니다.",
                "안전 경고는 출처가 확인된 최신 정보만 여행자 안내에 반영합니다.",
            ],
            "next_actions": [],
            "quality_score": 1.0,
        },
        {
            "topic": "core:identity-and-duplicates",
            "title": "장소 식별·중복 처리 원칙",
            "category": "data_model",
            "summary": "같은 장소의 표기 차이는 병합하되 같은 브랜드의 서로 다른 지점과 서로 다른 선착장은 별도 장소로 유지합니다.",
            "principles": [
                "이름 유사도만으로 병합하지 않고 좌표·카테고리·출처 식별자를 교차 확인합니다.",
                "체인과 브랜드는 지점을 삭제해 합치지 않고 체인 관계로 연결합니다.",
                "현지명, 한국어 표기와 검색 별칭을 함께 보존합니다.",
                "병합은 이동된 데이터와 중복 제거 내역을 기록하고 되돌릴 수 있어야 합니다.",
            ],
            "next_actions": [],
            "quality_score": 1.0,
        },
    ]

    regions = db.query(Region).order_by(Region.sort_order, Region.id).all()
    coverage_rows = (
        db.query(Place.region_id, Place.category)
        .filter(Place.merged_into_id.is_(None))
        .all()
    )
    coverage: dict[int, Counter[str]] = {}
    for region_id, category in coverage_rows:
        coverage.setdefault(region_id, Counter())[str(category or "other")] += 1

    for region in regions:
        counts = coverage.get(region.id, Counter())
        total = sum(counts.values())
        distribution = ", ".join(
            f"{category} {count}곳"
            for category, count in sorted(counts.items(), key=lambda item: (-item[1], item[0]))
        ) or "아직 공개 장소 없음"
        next_actions: list[str] = []
        if total == 0:
            next_actions.append("공식·공개 출처를 교차 확인해 대표 장소 후보를 수집합니다.")
        else:
            next_actions.append("정보가 오래됐거나 출처가 약한 공개 장소를 우선 재검증합니다.")
        if not counts.get("transport") and (
            "ferry" in (region.transport_mode or "").casefold()
            or region.kind in {"island", "small_island"}
        ):
            next_actions.append("선착장과 섬 이동 정보를 별도 장소·교통 정보로 보강합니다.")

        context = " · ".join(
            value.strip()
            for value in [region.summary or "", region.access_note or ""]
            if value and value.strip()
        )
        documents.append(
            {
                "topic": f"region:{region.slug}:playbook",
                "title": f"{region.name_ko} 조사 플레이북",
                "category": "region",
                "region_id": region.id,
                "summary": (
                    f"{region.name_local or region.name_ko} 권역의 현재 공개 장소는 {total}곳이며 "
                    f"카테고리 분포는 {distribution}입니다."
                ),
                "principles": [
                    f"권역 경계 안의 실제 방문 좌표만 {region.name_ko}에 배정합니다.",
                    f"기본 이동 맥락은 {region.transport_mode or '현지 이동수단 확인'}이며 방문 전 최신 상태를 확인합니다.",
                    context or "권역의 실제 여행 동선과 접근성을 기준으로 장소를 분류합니다.",
                ],
                "next_actions": next_actions,
                "quality_score": 0.8,
            }
        )
    return documents


def rebuild_agent_knowledge(db: Session) -> dict[str, int]:
    """Upsert managed playbooks while preserving run-learned knowledge."""

    documents = _managed_documents(db)
    managed_topics = {str(item["topic"]) for item in documents}
    existing = {
        row.topic: row
        for row in db.query(AgentKnowledge).filter(AgentKnowledge.topic.in_(managed_topics)).all()
    }
    created = 0
    updated = 0
    unchanged = 0
    for item in documents:
        topic = str(item["topic"])
        principles = _json(item["principles"])
        next_actions = _json(item["next_actions"])
        content = _json(
            {
                "summary": item["summary"],
                "principles": item["principles"],
                "next_actions": item["next_actions"],
            }
        )
        values = {
            "title": str(item["title"]),
            "content": content,
            "scope": "region" if item.get("region_id") else "global",
            "region_id": item.get("region_id"),
            "place_id": None,
            "category": str(item["category"]),
            "summary": str(item["summary"]),
            "principles_json": principles,
            "next_actions_json": next_actions,
            "keywords_json": "[]",
            "source_refs_json": "[]",
            "evidence_count": 0,
            "quality_score": float(item["quality_score"]),
            "status": "active",
        }
        row = existing.get(topic)
        if row is None:
            db.add(AgentKnowledge(topic=topic, version=1, **values))
            created += 1
            continue
        changed = any(getattr(row, field) != value for field, value in values.items())
        if not changed:
            unchanged += 1
            continue
        for field, value in values.items():
            setattr(row, field, value)
        row.version = max(1, int(row.version or 1)) + 1
        updated += 1

    learned_count = db.query(AgentKnowledge).filter(~AgentKnowledge.topic.in_(managed_topics)).count()
    db.commit()
    return {
        "created": created,
        "updated": updated,
        "unchanged": unchanged,
        "managed": len(documents),
        "preserved": learned_count,
        "active": db.query(AgentKnowledge).filter(AgentKnowledge.status == "active").count(),
    }
