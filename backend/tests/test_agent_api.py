from __future__ import annotations

import hashlib
import json
from unittest.mock import patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from app import agent_models, extended_models, itinerary_models, models  # noqa: F401
from app.agent_api import router
from app.agent_models import AgentProposal
from app.auth import get_admin_user
from app.db import Base, get_db
from app.extended_models import PlaceChangeEvent, PlaceImage, PlaceInsight
from app.models import BatchRun, DiscoveryCandidate, Place, Region, User


def _region() -> Region:
    return Region(
        slug="ubud",
        name_ko="우붓",
        name_local="Ubud",
        island="Bali",
        kind="hub",
        center_lat=-8.51,
        center_lng=115.26,
        default_zoom=12,
        south=-8.70,
        west=115.05,
        north=-8.30,
        east=115.45,
        summary="여행자 권역",
        access_note="도로 이동",
        transport_mode="car",
        sort_order=1,
    )


def _client() -> tuple[TestClient, Session, User, Region]:
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    db = factory()
    admin = User(
        email="admin@example.test",
        display_name="Admin",
        password_hash="not-used",
    )
    region = _region()
    db.add_all([admin, region])
    db.commit()
    app = FastAPI()
    app.include_router(router)

    def database_override():
        yield db

    app.dependency_overrides[get_db] = database_override
    app.dependency_overrides[get_admin_user] = lambda: admin
    return TestClient(app), db, admin, region


def test_admin_can_queue_and_poll_a_scoped_curation_run() -> None:
    client, db, _, region = _client()
    try:
        with patch("app.agent_api.execute_queued_agent") as execute:
            response = client.post(
                "/api/admin/agent/run",
                json={"region_id": region.id, "mode": "discovery"},
            )
        assert response.status_code == 202
        body = response.json()
        assert body["region_id"] == region.id
        assert body["mode"] == "discovery"
        assert body["status"] == "queued"
        execute.assert_called_once_with(body["id"])

        busy = client.post(
            "/api/admin/agent/run",
            json={"region_id": region.id, "mode": "quality"},
        )
        assert busy.status_code == 409
        polled = client.get(f"/api/admin/agent/runs/{body['id']}")
        assert polled.status_code == 200
        assert polled.json()["objective"].startswith("새로운 여행 장소")
    finally:
        client.close()
        db.close()


def test_update_proposal_requires_review_and_records_an_audit_event() -> None:
    client, db, admin, region = _client()
    try:
        place = Place(
            region_id=region.id,
            creator_id=admin.id,
            category="nature",
            title="Campuhan Ridge Walk",
            local_name="",
            description="짧은 설명",
            area="Ubud",
            lat=-8.506,
            lng=115.255,
            duration_minutes=90,
            budget_level=1,
            best_time="",
            traveler_note="",
            tags="산책",
            source_url="",
            coordinate_source="manual",
            coordinate_crs="WGS84",
        )
        db.add(place)
        db.flush()
        payload = {
            "description": "공개 지도와 공식 방문 안내를 교차 확인한 능선 산책로입니다.",
            "best_time": "이른 아침",
            "source_url": "https://example.test/campuhan",
            "tags": ["산책", "일출"],
        }
        proposal = AgentProposal(
            region_id=region.id,
            place_id=place.id,
            action="update",
            title="장소 정보 보강",
            payload_json=json.dumps(payload, ensure_ascii=False),
            evidence="두 공개 출처 확인",
            source_urls_json=json.dumps([payload["source_url"]]),
            confidence=0.82,
            proposal_key=hashlib.sha256(b"update-campuhan").hexdigest(),
            status="pending",
        )
        db.add(proposal)
        db.commit()

        listed = client.get("/api/admin/agent/proposals")
        assert listed.status_code == 200
        assert listed.json()[0]["payload"]["best_time"] == "이른 아침"
        approved = client.post(
            f"/api/admin/agent/proposals/{proposal.id}/approve",
            json={"note": "출처 원문 확인", "force": False},
        )
        assert approved.status_code == 200
        assert approved.json()["status"] == "approved"
        db.refresh(place)
        assert place.best_time == "이른 아침"
        assert place.tags == "산책,일출"
        assert place.source_url == payload["source_url"]
        event = db.query(PlaceChangeEvent).filter(
            PlaceChangeEvent.place_id == place.id,
            PlaceChangeEvent.event_type == "agent_proposal_applied",
        ).one()
        assert "source_url" in event.metadata_json

        duplicate_decision = client.post(
            f"/api/admin/agent/proposals/{proposal.id}/approve",
            json={"note": "다시 승인"},
        )
        assert duplicate_decision.status_code == 409
    finally:
        client.close()
        db.close()


def test_create_proposal_rolls_back_candidate_and_place_when_apply_fails() -> None:
    client, db, admin, region = _client()
    try:
        run = BatchRun(kind="place_discovery", status="success", trigger="manual")
        db.add(run)
        db.flush()
        candidate = DiscoveryCandidate(
            discovery_run_id=run.id,
            region_id=region.id,
            source="openstreetmap",
            external_id="node/991001",
            source_url="https://www.openstreetmap.org/node/991001",
            title="Atomic Candidate Garden",
            local_name="",
            description="관리자 검토 후보",
            area="Ubud",
            category="nature",
            lat=-8.52,
            lng=115.28,
            confidence=0.8,
            evidence=json.dumps({"osm_tags": {"tourism": "attraction"}}),
            tags="정원",
            status="pending",
        )
        db.add(candidate)
        db.flush()
        proposal = AgentProposal(
            region_id=region.id,
            discovery_candidate_id=candidate.id,
            action="create",
            title="신규 장소 원자성 테스트",
            payload_json=json.dumps({
                "candidate_id": candidate.id,
                "title": candidate.title,
                "local_name": candidate.local_name,
                "description": candidate.description,
                "area": candidate.area,
                "category": candidate.category,
                "lat": candidate.lat,
                "lng": candidate.lng,
                "source_url": candidate.source_url,
                "coordinate_source": candidate.source,
                "coordinate_confidence": candidate.confidence,
            }),
            evidence="공개 출처 확인",
            source_urls_json=json.dumps([candidate.source_url]),
            confidence=0.8,
            proposal_key=hashlib.sha256(b"atomic-create").hexdigest(),
            status="pending",
        )
        db.add(proposal)
        db.commit()

        with patch("app.agent_api._apply_place_payload", side_effect=RuntimeError("apply failed")):
            with pytest.raises(RuntimeError, match="apply failed"):
                client.post(
                    f"/api/admin/agent/proposals/{proposal.id}/approve",
                    json={"note": "원문 확인"},
                )
        db.rollback()
        db.expire_all()
        assert db.get(DiscoveryCandidate, candidate.id).status == "pending"
        assert db.get(DiscoveryCandidate, candidate.id).result_place_id is None
        assert db.get(AgentProposal, proposal.id).status == "pending"
        assert db.query(Place).count() == 0
        assert db.query(PlaceChangeEvent).count() == 0
    finally:
        client.close()
        db.close()


def test_merge_proposal_rolls_back_places_when_proposal_finalization_fails() -> None:
    client, db, admin, region = _client()
    try:
        places = [
            Place(
                region_id=region.id,
                creator_id=admin.id,
                category="nature",
                title="Twin Waterfall",
                local_name="Air Terjun Kembar",
                description="중복 후보",
                area="Ubud",
                lat=-8.51,
                lng=115.26 + index * 0.00001,
                duration_minutes=90,
                budget_level=1,
                best_time="오전",
                traveler_note="",
                tags="폭포",
                source_url="",
                coordinate_source="manual",
                coordinate_crs="WGS84",
            )
            for index in range(2)
        ]
        db.add_all(places)
        db.flush()
        source, target = places
        proposal = AgentProposal(
            region_id=region.id,
            place_id=target.id,
            secondary_place_id=source.id,
            action="merge",
            title="중복 장소 병합",
            payload_json=json.dumps({
                "duplicate_place_id": source.id,
                "canonical_place_id": target.id,
            }),
            evidence="이름과 좌표 일치",
            source_urls_json="[]",
            confidence=0.95,
            proposal_key=hashlib.sha256(b"atomic-merge").hexdigest(),
            status="pending",
        )
        db.add(proposal)
        db.commit()

        with patch("app.agent_api._event_metadata", side_effect=RuntimeError("finalization failed")):
            with pytest.raises(RuntimeError, match="finalization failed"):
                client.post(
                    f"/api/admin/agent/proposals/{proposal.id}/approve",
                    json={"note": "두 원문을 확인했습니다", "force": True},
                )
        db.rollback()
        db.expire_all()
        assert db.get(Place, source.id).merged_into_id is None
        assert db.get(Place, target.id).merged_into_id is None
        assert db.get(AgentProposal, proposal.id).status == "pending"
        assert db.query(PlaceChangeEvent).count() == 0
    finally:
        client.close()
        db.close()


def test_rejecting_a_proposal_preserves_the_decision_note() -> None:
    client, db, _, region = _client()
    try:
        proposal = AgentProposal(
            region_id=region.id,
            action="create",
            title="근거 부족 후보",
            payload_json="{}",
            evidence="단일 출처",
            source_urls_json="[]",
            confidence=0.3,
            proposal_key=hashlib.sha256(b"reject-me").hexdigest(),
            status="pending",
        )
        db.add(proposal)
        db.commit()

        response = client.post(
            f"/api/admin/agent/proposals/{proposal.id}/reject",
            json={"note": "공식 출처를 찾지 못함"},
        )

        assert response.status_code == 200
        assert response.json()["status"] == "rejected"
        assert response.json()["decision_note"] == "공식 출처를 찾지 못함"
    finally:
        client.close()
        db.close()


def test_agent_image_rollback_refuses_an_image_edited_after_approval() -> None:
    client, db, admin, region = _client()
    try:
        place = Place(
            region_id=region.id, creator_id=admin.id, category="nature",
            title="Sourced Garden", local_name="", description="정원", area="Ubud",
            lat=-8.51, lng=115.26, duration_minutes=60, budget_level=1,
            best_time="", traveler_note="", tags="정원", source_url="",
            coordinate_source="manual", coordinate_crs="WGS84",
        )
        db.add(place)
        db.flush()
        proposal = AgentProposal(
            region_id=region.id, place_id=place.id, action="image",
            title="대표 이미지", payload_json=json.dumps({
                "image_url": "https://images.example.test/garden.jpg",
                "source_url": "https://images.example.test/source",
                "caption": "원래 캡션",
            }),
            evidence="라이선스 원문 확인", source_urls_json="[]", confidence=0.8,
            proposal_key=hashlib.sha256(b"image-snapshot").hexdigest(), status="pending",
        )
        db.add(proposal)
        db.commit()

        approved = client.post(f"/api/admin/agent/proposals/{proposal.id}/approve", json={})
        assert approved.status_code == 200
        image = db.query(PlaceImage).one()
        event = db.query(PlaceChangeEvent).filter(
            PlaceChangeEvent.event_type == "agent_image_approved"
        ).one()
        image.caption = "승인 뒤 사람이 수정한 캡션"
        db.commit()

        blocked = client.post(f"/api/admin/agent/actions/{event.id}/rollback", json={})
        assert blocked.status_code == 409
        assert db.get(PlaceImage, image.id) is not None
    finally:
        client.close()
        db.close()


def test_agent_insight_rollback_deletes_only_an_untouched_creation() -> None:
    client, db, admin, region = _client()
    try:
        place = Place(
            region_id=region.id, creator_id=admin.id, category="culture",
            title="Temple Walk", local_name="", description="사원", area="Ubud",
            lat=-8.51, lng=115.26, duration_minutes=60, budget_level=1,
            best_time="", traveler_note="", tags="문화", source_url="",
            coordinate_source="manual", coordinate_crs="WGS84",
        )
        db.add(place)
        db.flush()
        proposal = AgentProposal(
            region_id=region.id, place_id=place.id, action="insight",
            title="방문 팁", payload_json=json.dumps({
                "kind": "tip", "title": "사롱 준비", "content": "입장 전 복장을 확인하세요.",
                "source_url": "https://example.test/temple-guide",
            }, ensure_ascii=False),
            evidence="공식 방문 안내", source_urls_json="[]", confidence=0.8,
            proposal_key=hashlib.sha256(b"insight-snapshot").hexdigest(), status="pending",
        )
        db.add(proposal)
        db.commit()

        approved = client.post(f"/api/admin/agent/proposals/{proposal.id}/approve", json={})
        assert approved.status_code == 200
        insight = db.query(PlaceInsight).one()
        event = db.query(PlaceChangeEvent).filter(
            PlaceChangeEvent.event_type == "agent_insight_approved"
        ).one()
        rolled_back = client.post(f"/api/admin/agent/actions/{event.id}/rollback", json={})
        assert rolled_back.status_code == 200
        assert db.get(PlaceInsight, insight.id) is None
    finally:
        client.close()
        db.close()


def test_create_proposal_cannot_apply_to_an_already_merged_candidate_result() -> None:
    client, db, admin, region = _client()
    try:
        source = Place(
            region_id=region.id, creator_id=admin.id, category="nature",
            title="Hidden Source", local_name="", description="원본", area="Ubud",
            lat=-8.51, lng=115.26, duration_minutes=60, budget_level=1,
            best_time="", traveler_note="", tags="", source_url="",
            coordinate_source="manual", coordinate_crs="WGS84",
        )
        target = Place(
            region_id=region.id, creator_id=admin.id, category="nature",
            title="Canonical Place", local_name="", description="대표", area="Ubud",
            lat=-8.5101, lng=115.2601, duration_minutes=60, budget_level=1,
            best_time="", traveler_note="", tags="", source_url="",
            coordinate_source="manual", coordinate_crs="WGS84",
        )
        db.add_all([source, target])
        db.flush()
        source.merged_into_id = target.id
        run = BatchRun(kind="place_discovery", status="success", trigger="manual")
        db.add(run)
        db.flush()
        candidate = DiscoveryCandidate(
            discovery_run_id=run.id, region_id=region.id, source="openstreetmap",
            external_id="node/991099", source_url="https://www.openstreetmap.org/node/991099",
            title=source.title, local_name="", description=source.description, area="Ubud",
            category="nature", lat=source.lat, lng=source.lng, confidence=0.8,
            evidence="{}", tags="", status="approved", result_place_id=source.id,
        )
        db.add(candidate)
        db.flush()
        proposal = AgentProposal(
            region_id=region.id, discovery_candidate_id=candidate.id, action="create",
            title="이미 승인된 후보 보강", payload_json=json.dumps({
                "title": "Stale payload must not apply",
                "category": "nature", "lat": source.lat, "lng": source.lng,
            }),
            evidence="", source_urls_json="[]", confidence=0.8,
            proposal_key=hashlib.sha256(b"merged-approved-result").hexdigest(), status="pending",
        )
        db.add(proposal)
        db.commit()

        response = client.post(f"/api/admin/agent/proposals/{proposal.id}/approve", json={})
        assert response.status_code == 409
        db.refresh(proposal)
        db.refresh(source)
        assert proposal.status == "pending"
        assert source.title == "Hidden Source"
    finally:
        client.close()
        db.close()
