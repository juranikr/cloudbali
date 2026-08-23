from __future__ import annotations

import hashlib
import json
from collections.abc import Generator

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from app import agent_models, extended_models, itinerary_models, models  # noqa: F401
from app.agent_api import router as agent_router
from app.agent_models import AgentKnowledge, AgentProposal, AgentRun, AgentTask
from app.auth import get_admin_user, get_current_user
from app.db import Base, get_db
from app.extended_models import PlaceChangeEvent
from app.main import add_favorite, list_favorites, remove_favorite
from app.models import BatchRun, DiscoveryCandidate, Favorite, Place, Region, User
from app.schemas import FavoriteOut, PlaceOut


engine = create_engine(
    "sqlite://",
    connect_args={"check_same_thread": False},
    poolclass=StaticPool,
)
TestingSession = sessionmaker(bind=engine, expire_on_commit=False)


def _db_override() -> Generator[Session, None, None]:
    with TestingSession() as db:
        yield db


app = FastAPI()
app.include_router(agent_router)
app.add_api_route("/api/favorites", list_favorites, methods=["GET"], response_model=list[PlaceOut])
app.add_api_route(
    "/api/favorites/{place_id}",
    add_favorite,
    methods=["POST"],
    response_model=FavoriteOut,
)
app.add_api_route(
    "/api/favorites/{place_id}",
    remove_favorite,
    methods=["DELETE"],
    response_model=FavoriteOut,
)
app.dependency_overrides[get_db] = _db_override


@pytest.fixture(autouse=True)
def reset_database() -> Generator[None, None, None]:
    Base.metadata.drop_all(engine)
    Base.metadata.create_all(engine)
    with TestingSession() as db:
        admin = User(id=1, email="admin@example.test", display_name="Admin", password_hash="unused")
        region = Region(
            id=1,
            slug="ubud",
            name_ko="우붓",
            name_local="Ubud",
            island="Bali",
            kind="hub",
            center_lat=-8.51,
            center_lng=115.26,
            default_zoom=12,
            south=-8.7,
            west=115.05,
            north=-8.3,
            east=115.45,
            summary="논과 사원이 이어지는 내륙 권역",
            access_note="도로 혼잡을 고려해야 합니다",
            transport_mode="car",
            sort_order=1,
        )
        db.add_all([admin, region])
        db.flush()
        db.add_all([
            Place(
                id=1,
                region_id=region.id,
                creator_id=admin.id,
                category="culture",
                title="첫 장소",
                local_name="First Place",
                description="현재 설명",
                lat=-8.51,
                lng=115.26,
                coordinate_crs="WGS84",
            ),
            Place(
                id=2,
                region_id=region.id,
                creator_id=admin.id,
                category="cafe",
                title="둘째 장소",
                local_name="Second Place",
                lat=-8.52,
                lng=115.27,
                coordinate_crs="WGS84",
            ),
        ])
        db.commit()
    with TestingSession() as db:
        admin = db.get(User, 1)
        assert admin is not None
        app.dependency_overrides[get_admin_user] = lambda: admin
        app.dependency_overrides[get_current_user] = lambda: admin
        yield
    app.dependency_overrides.pop(get_admin_user, None)
    app.dependency_overrides.pop(get_current_user, None)


def test_favorite_list_and_explicit_mutations_are_ordered_and_idempotent() -> None:
    with TestClient(app) as client:
        assert client.post("/api/favorites/1").json() == {"place_id": 1, "is_favorite": True}
        assert client.post("/api/favorites/1").json() == {"place_id": 1, "is_favorite": True}
        assert client.post("/api/favorites/2").status_code == 200

        listed = client.get("/api/favorites")
        assert listed.status_code == 200
        assert [row["id"] for row in listed.json()] == [2, 1]
        assert all(row["is_favorite"] for row in listed.json())

        assert client.delete("/api/favorites/2").json()["is_favorite"] is False
        assert client.delete("/api/favorites/2").status_code == 200
        assert [row["id"] for row in client.get("/api/favorites").json()] == [1]
        assert client.post("/api/favorites/999").status_code == 404


def test_agent_status_reports_active_work_and_scoped_review_counts() -> None:
    with TestingSession() as db:
        run = AgentRun(
            region_id=1,
            active_slot="global",
            mode="discovery",
            trigger="manual",
            status="queued",
            objective="신규 장소 조사",
            metrics_json="{}",
        )
        db.add(run)
        db.flush()
        db.add(AgentTask(
            region_id=1,
            kind="research",
            target_key="place:1",
            title="출처 재확인",
            status="pending",
        ))
        db.add(AgentProposal(
            region_id=1,
            run_id=run.id,
            action="update",
            title="설명 보강",
            payload_json="{}",
            proposal_key=hashlib.sha256(b"status-proposal").hexdigest(),
            status="pending",
        ))
        db.commit()

    with TestClient(app) as client:
        response = client.get("/api/admin/agent/run/status?region_id=1")
        assert response.status_code == 200
        body = response.json()
        assert body["is_running"] is True
        assert body["active_run"]["mode"] == "discovery"
        assert body["latest_run"]["id"] == body["active_run"]["id"]
        assert body["pending_proposals"] == 1
        assert body["pending_tasks"] == 1


def test_knowledge_rebuild_is_bali_scoped_idempotent_and_preserves_learned_rows() -> None:
    with TestingSession() as db:
        db.add(AgentKnowledge(
            topic="learned:recent-verification",
            title="최근 검증에서 얻은 지식",
            content="운영 실행이 남긴 근거",
            scope="global",
            category="workflow",
            summary="보존해야 합니다",
            status="active",
        ))
        db.commit()

    with TestClient(app) as client:
        first = client.post("/api/admin/agent/knowledge/rebuild")
        assert first.status_code == 200
        assert first.json()["created"] == 4
        assert first.json()["preserved"] == 1
        second = client.post("/api/admin/agent/knowledge/rebuild")
        assert second.status_code == 200
        assert second.json()["created"] == 0
        assert second.json()["unchanged"] == 4

        rows = client.get("/api/admin/agent/knowledge").json()
        topics = {row["topic"] for row in rows}
        assert "core:island-travel-safety" in topics
        assert "region:ubud:playbook" in topics
        assert "learned:recent-verification" in topics


def test_agent_update_action_history_can_be_rolled_back_once() -> None:
    with TestingSession() as db:
        place = db.get(Place, 1)
        place.description = "현재 설명"
        place.coordinate_external_id = "node/12345"
        place.coordinate_confidence = 0.7
        event = PlaceChangeEvent(
            place_id=1,
            actor_id=1,
            event_type="agent_proposal_applied",
            summary="운영 조사가 설명을 수정했습니다",
            metadata_json=json.dumps(
                {
                    "before": {
                        "description": "이전 설명",
                        "coordinate_external_id": "",
                        "coordinate_confidence": None,
                    },
                    "after": {
                        "description": "현재 설명",
                        "coordinate_external_id": "node/12345",
                        "coordinate_confidence": 0.7,
                    },
                },
                ensure_ascii=False,
            ),
        )
        db.add(event)
        db.commit()
        event_id = event.id

    with TestClient(app) as client:
        actions = client.get("/api/admin/agent/actions").json()
        assert actions[0]["id"] == event_id
        assert actions[0]["action"] == "update"
        assert actions[0]["can_rollback"] is True

        rolled_back = client.post(
            f"/api/admin/agent/actions/{event_id}/rollback",
            json={"note": "원문과 달라 취소"},
        )
        assert rolled_back.status_code == 200
        assert rolled_back.json()["ok"] is True
        assert client.post(
            f"/api/admin/agent/actions/{event_id}/rollback", json={}
        ).status_code == 409
        refreshed = client.get("/api/admin/agent/actions").json()[0]
        assert refreshed["rolled_back"] is True
        assert refreshed["can_rollback"] is False

    with TestingSession() as db:
        place = db.get(Place, 1)
        assert place.description == "이전 설명"
        assert place.coordinate_external_id == ""
        assert place.coordinate_confidence is None


def test_agent_update_rollback_refuses_to_overwrite_a_later_edit() -> None:
    with TestingSession() as db:
        place = db.get(Place, 1)
        place.description = "운영 조사 설명"
        event = PlaceChangeEvent(
            place_id=place.id,
            actor_id=1,
            event_type="agent_proposal_applied",
            summary="운영 조사 설명 수정",
            metadata_json=json.dumps({
                "before": {"description": "원래 설명"},
                "after": {"description": "운영 조사 설명"},
            }, ensure_ascii=False),
        )
        db.add(event)
        db.commit()
        event_id = event.id
        place.description = "여행자가 나중에 보완한 설명"
        db.commit()

    with TestClient(app) as client:
        action = next(item for item in client.get("/api/admin/agent/actions").json() if item["id"] == event_id)
        assert action["can_rollback"] is False
        blocked = client.post(f"/api/admin/agent/actions/{event_id}/rollback", json={})
        assert blocked.status_code == 409

    with TestingSession() as db:
        assert db.get(Place, 1).description == "여행자가 나중에 보완한 설명"


def test_agent_created_place_rollback_refuses_user_data_then_removes_safe_candidate() -> None:
    with TestingSession() as db:
        run = BatchRun(kind="place_discovery", status="completed", trigger="manual")
        db.add(run)
        db.flush()
        candidate = DiscoveryCandidate(
            discovery_run_id=run.id,
            region_id=1,
            source="osm",
            external_id="node/123",
            category="nature",
            title="조사 후보",
            local_name="Research Candidate",
            lat=-8.53,
            lng=115.28,
            status="approved",
            result_place_id=2,
        )
        db.add(candidate)
        db.flush()
        event = PlaceChangeEvent(
            place_id=2,
            actor_id=1,
            event_type="place_created",
            summary="관리자가 자동 발굴 후보를 승인했습니다",
            metadata_json=json.dumps({"source": "osm", "candidate_id": candidate.id}),
        )
        db.add_all([event, Favorite(user_id=1, place_id=2)])
        db.commit()
        candidate_id = candidate.id
        event_id = event.id

    with TestClient(app) as client:
        action = client.get("/api/admin/agent/actions").json()[0]
        assert action["action"] == "create"
        assert action["can_rollback"] is False
        blocked = client.post(f"/api/admin/agent/actions/{event_id}/rollback", json={})
        assert blocked.status_code == 409

    with TestingSession() as db:
        db.query(Favorite).filter(Favorite.place_id == 2).delete()
        db.commit()

    with TestClient(app) as client:
        assert client.get("/api/admin/agent/actions").json()[0]["can_rollback"] is True
        response = client.post(
            f"/api/admin/agent/actions/{event_id}/rollback",
            json={"note": "후보 검증 실패"},
        )
        assert response.status_code == 200

    with TestingSession() as db:
        assert db.get(Place, 2) is None
        candidate = db.get(DiscoveryCandidate, candidate_id)
        assert candidate is not None
        assert candidate.status == "rejected"
        assert candidate.result_place_id is None
