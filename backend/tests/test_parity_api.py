from __future__ import annotations

from collections.abc import Generator
import os
import tempfile

import pytest
from fastapi import Depends, FastAPI, Header, HTTPException, status
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool


# Keep this module order-independent: importing app.main initializes its global
# engine, so it must never fall back to the developer's real local database.
MAIN_TEST_DIR = tempfile.mkdtemp(prefix="patra-parity-main-")
os.environ.setdefault(
    "DATABASE_URL",
    "sqlite:///" + MAIN_TEST_DIR.replace("\\", "/") + "/main.db",
)

from app import main as main_module
from app import parity_api as parity_module
from app.agent_models import (
    AgentEvidence,
    AgentKnowledge,
    AgentLesson,
    AgentMission,
    AgentProposal,
    AgentQualityGap,
    AgentTask,
    AgentWorkItem,
)
from app.auth import get_admin_user, get_current_user
from app.config import settings
from app.db import Base, get_db
from app.extended_models import PlaceContributor, PlaceImage, PlaceNote, UserMessage
from app.main import (
    admin_delete_place,
    create_place,
    get_place,
    places,
    search,
    update_place,
)
from app.models import Favorite, Place, Region, User
from app.operations_api import router as operations_router
from app.parity_api import router as parity_router
from app.schemas import PlaceOut, SearchHit
from app.search_service import ExternalSearchHit


engine = create_engine(
    "sqlite://",
    connect_args={"check_same_thread": False},
    poolclass=StaticPool,
)
TestingSession = sessionmaker(bind=engine, autocommit=False, autoflush=False)


def override_db() -> Generator[Session, None, None]:
    with TestingSession() as db:
        yield db


def override_current_user(
    x_test_user: int = Header(default=2, alias="X-Test-User"),
    db: Session = Depends(override_db),
) -> User:
    user = db.get(User, x_test_user)
    if user is None:
        raise HTTPException(status_code=401, detail="테스트 사용자를 찾을 수 없습니다")
    return user


def override_admin_user(user: User = Depends(override_current_user)) -> User:
    if user.email.lower() not in settings.admin_email_list:
        raise HTTPException(status_code=403, detail="관리자만 접근할 수 있습니다")
    return user


parity_test_app = FastAPI()
parity_test_app.include_router(operations_router)
parity_test_app.include_router(parity_router)
parity_test_app.add_api_route(
    "/api/search",
    search,
    methods=["GET"],
    response_model=list[SearchHit],
)
parity_test_app.add_api_route(
    "/api/places",
    places,
    methods=["GET"],
    response_model=list[PlaceOut],
)
parity_test_app.add_api_route(
    "/api/places",
    create_place,
    methods=["POST"],
    response_model=PlaceOut,
    status_code=status.HTTP_201_CREATED,
)
parity_test_app.add_api_route(
    "/api/places/{place_id}",
    get_place,
    methods=["GET"],
    response_model=PlaceOut,
)
parity_test_app.add_api_route(
    "/api/places/{place_id}",
    update_place,
    methods=["PATCH"],
    response_model=PlaceOut,
)
parity_test_app.add_api_route(
    "/api/admin/places/{place_id}",
    admin_delete_place,
    methods=["DELETE"],
    status_code=status.HTTP_204_NO_CONTENT,
)
parity_test_app.dependency_overrides[get_db] = override_db
parity_test_app.dependency_overrides[get_current_user] = override_current_user
parity_test_app.dependency_overrides[get_admin_user] = override_admin_user


@pytest.fixture(autouse=True)
def reset_database(monkeypatch: pytest.MonkeyPatch) -> Generator[None, None, None]:
    monkeypatch.setattr(settings, "admin_emails", "admin@example.com")
    Base.metadata.drop_all(bind=engine)
    Base.metadata.create_all(bind=engine)
    with TestingSession() as db:
        db.add_all([
            User(id=1, email="admin@example.com", display_name="관리자", password_hash="unused"),
            User(id=2, email="owner@example.com", display_name="장소 소유자", password_hash="unused"),
            User(id=3, email="editor@example.com", display_name="공동 편집자", password_hash="unused"),
            User(id=4, email="other@example.com", display_name="다른 여행자", password_hash="unused"),
        ])
        db.add(Region(
            id=1,
            slug="ubud",
            name_ko="우붓",
            name_local="Ubud",
            island="Bali",
            kind="hub",
            center_lat=-8.5069,
            center_lng=115.2625,
            default_zoom=12,
            south=-8.65,
            west=115.1,
            north=-8.4,
            east=115.4,
            summary="",
            access_note="",
            transport_mode="car",
            sort_order=1,
        ))
        db.flush()
        db.add_all([
            Place(
                id=1,
                region_id=1,
                creator_id=2,
                category="culture",
                title="테스트 사원",
                local_name="Pura Test",
                lat=-8.5100,
                lng=115.2600,
                coordinate_crs="WGS84",
            ),
            Place(
                id=2,
                region_id=1,
                creator_id=4,
                category="culture",
                title="테스트 사원",
                local_name="Pura Test Temple",
                lat=-8.5101,
                lng=115.2601,
                coordinate_crs="WGS84",
            ),
            Place(
                id=3,
                region_id=1,
                creator_id=4,
                category="cafe",
                title="추천 카페",
                local_name="Recommendation Cafe",
                lat=-8.53,
                lng=115.28,
                coordinate_crs="WGS84",
            ),
        ])
        db.commit()
    yield


def headers(user_id: int) -> dict[str, str]:
    return {"X-Test-User": str(user_id)}


def test_agent_place_reference_merge_registry_is_complete() -> None:
    place_field_names = {"place_id", "secondary_place_id", "result_place_id"}
    discovered = {
        (mapper.class_.__tablename__, column.name)
        for mapper in Base.registry.mappers
        if mapper.class_.__module__ == "app.agent_models"
        for column in mapper.columns
        if column.name in place_field_names
    }
    registered = {
        (model.__tablename__, field.key)
        for model, field, _ in parity_module._AGENT_PLACE_REFERENCES
    }
    registered.add((AgentQualityGap.__tablename__, AgentQualityGap.place_id.key))
    assert registered == discovered


def test_shared_editing_private_notes_insights_chains_and_inbox() -> None:
    with TestClient(parity_test_app) as client:
        invited = client.post(
            "/api/places/1/contributors",
            headers=headers(2),
            json={"email": "editor@example.com", "role": "editor"},
        )
        assert invited.status_code == 201
        assert invited.json()["role"] == "editor"

        edited = client.patch(
            "/api/places/1",
            headers=headers(3),
            json={"description": "공동 편집자가 보완한 설명"},
        )
        assert edited.status_code == 200
        assert edited.json()["description"] == "공동 편집자가 보완한 설명"
        assert client.patch(
            "/api/places/1",
            headers=headers(4),
            json={"description": "권한 없는 변경"},
        ).status_code == 403

        private_note = client.post(
            "/api/places/1/notes",
            headers=headers(2),
            json={"content": "나만 볼 메모", "visibility": "private"},
        )
        shared_note = client.post(
            "/api/places/1/notes",
            headers=headers(4),
            json={"content": "모두가 볼 메모", "visibility": "shared"},
        )
        assert private_note.status_code == shared_note.status_code == 201
        owner_notes = client.get("/api/places/1/notes", headers=headers(2)).json()
        other_notes = client.get("/api/places/1/notes", headers=headers(4)).json()
        admin_notes = client.get("/api/places/1/notes", headers=headers(1)).json()
        assert {item["content"] for item in owner_notes} == {"나만 볼 메모", "모두가 볼 메모"}
        assert [item["content"] for item in other_notes] == ["모두가 볼 메모"]
        assert {item["content"] for item in admin_notes} == {"나만 볼 메모", "모두가 볼 메모"}

        chain = client.post(
            "/api/chains",
            headers=headers(3),
            json={
                "name_local": "Bali Buda",
                "name_ko": "발리 부다",
                "category": "cafe",
                "aliases": ["BaliBuda", "Bali Buda"],
            },
        )
        assert chain.status_code == 201
        assigned = client.put(
            "/api/places/1/chain",
            headers=headers(3),
            json={"chain_id": chain.json()["id"], "branch_name": "Ubud"},
        )
        assert assigned.status_code == 200
        place = client.get("/api/places/1", headers=headers(3)).json()
        assert place["chain_id"] == chain.json()["id"]
        assert place["branch_name"] == "Ubud"

        insecure = client.post(
            "/api/places/1/insights",
            headers=headers(3),
            json={
                "kind": "history",
                "title": "역사",
                "content": "출처 없는 사실처럼 보이면 안 됩니다.",
                "source_url": "http://example.com/history",
            },
        )
        assert insecure.status_code == 422
        insight = client.post(
            "/api/places/1/insights",
            headers=headers(3),
            json={
                "kind": "history",
                "title": "창건 배경",
                "content": "공개 자료에서 확인한 설명입니다.",
                "source_url": "https://example.com/history",
                "source_title": "공식 역사 자료",
                "confidence": 0.8,
            },
        )
        assert insight.status_code == 201
        assert insight.json()["verified_at"] is None
        verified = client.patch(
            f"/api/place-insights/{insight.json()['id']}",
            headers=headers(1),
            json={"confidence": 0.9},
        )
        assert verified.status_code == 200
        assert verified.json()["verified_at"] is not None

        inbox = client.get("/api/messages", headers=headers(3))
        assert inbox.status_code == 200
        assert any(item["kind"] == "place_collaboration" for item in inbox.json())
        assert client.get("/api/messages/unread-count", headers=headers(3)).json()["count"] >= 1
        read_all = client.post("/api/messages/read-all", headers=headers(3))
        assert read_all.status_code == 200
        assert client.get("/api/messages/unread-count", headers=headers(3)).json() == {"count": 0}


def test_search_combines_local_and_cross_checked_open_sources(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def fake_external_search(*args, **kwargs) -> list[ExternalSearchHit]:
        assert args[0] == "테스트 사원"
        assert kwargs["bounds"] is not None
        return [ExternalSearchHit(
            key="wikidata-Q123",
            source="wikidata",
            title="테스트 사원",
            display_name="테스트 사원 · 발리의 사원",
            lat=-8.51001,
            lng=115.26001,
            category="culture",
            external_id="Q123",
            source_url="https://www.wikidata.org/wiki/Q123",
            coordinate_source="wikidata",
            confidence=0.78,
            sources=("wikidata",),
            external_ids=(("wikidata", "Q123"),),
            source_urls=("https://www.wikidata.org/wiki/Q123",),
            storage_allowed=True,
            license="CC0 1.0",
            attribution="Wikidata contributors",
        )]

    monkeypatch.setattr(main_module, "search_external_places", fake_external_search)
    with TestClient(parity_test_app) as client:
        response = client.get(
            "/api/search",
            headers=headers(2),
            params={"q": "테스트 사원", "region_id": 1},
        )
        assert response.status_code == 200
        local = next(item for item in response.json() if item["place_id"] == 1)
        assert local["source"] == "local"
        assert local["cross_checked"] is True
        assert local["confidence"] >= 0.92
        assert "wikidata" in local["sources"]
        assert local["external_ids"]["wikidata"] == "Q123"
        assert all(url.startswith("https://") for url in local["source_urls"])
        assert client.get(
            "/api/search",
            headers=headers(2),
            params={"q": "테스트 사원", "region_id": 999},
        ).status_code == 404


def test_duplicate_guard_merge_audit_notifications_and_undo() -> None:
    with TestClient(parity_test_app) as client:
        assert client.post(
            "/api/admin/places/3/merge",
            headers=headers(1),
            json={"target_place_id": 2, "note": "서로 다른 장소"},
        ).status_code == 409
        assert client.post(
            "/api/admin/places/3/merge",
            headers=headers(1),
            json={"target_place_id": 2, "note": "짧음", "force": True},
        ).status_code == 422
        candidates = client.get(
            "/api/places/duplicate-candidates",
            headers=headers(2),
            params={
                "title": "테스트 사원",
                "local_name": "Pura Test",
                "lat": -8.5100,
                "lng": 115.2600,
                "category": "culture",
                "region_id": 1,
                "exclude_place_id": 1,
            },
        )
        assert candidates.status_code == 200
        assert candidates.json()[0]["place_id"] == 2
        assert candidates.json()[0]["confidence"] >= 0.9

        blocked = client.post(
            "/api/places",
            headers=headers(2),
            json={
                "region_id": 1,
                "category": "culture",
                "title": "테스트 사원",
                "local_name": "Pura Test",
                "lat": -8.51002,
                "lng": 115.26002,
            },
        )
        assert blocked.status_code == 409
        assert blocked.json()["detail"]["duplicate"]["place_id"] in {1, 2}

        client.post(
            "/api/places/1/contributors",
            headers=headers(2),
            json={"email": "editor@example.com"},
        )
        note = client.post(
            "/api/places/1/notes",
            headers=headers(2),
            json={"content": "병합 후에도 보존될 메모"},
        )
        assert note.status_code == 201
        with TestingSession() as db:
            db.add_all([
                Favorite(user_id=3, place_id=1),
                Favorite(user_id=3, place_id=2),
            ])
            task = AgentTask(
                region_id=1,
                place_id=1,
                kind="research",
                target_key="merge-source-task",
                title="원본 조사",
            )
            db.add(task)
            db.flush()
            mission = AgentMission(
                region_id=1,
                task_id=task.id,
                kind="research",
                title="병합 참조 테스트",
            )
            db.add(mission)
            db.flush()
            work_item = AgentWorkItem(
                mission_id=mission.id,
                region_id=1,
                place_id=1,
                target_key="merge-source-work",
                title="원본 장소 작업",
            )
            evidence = AgentEvidence(
                region_id=1,
                mission_id=mission.id,
                work_item_id=None,
                place_id=1,
                source_type="web",
                fingerprint="merge-source-evidence",
            )
            knowledge = AgentKnowledge(
                topic="merge-source-knowledge",
                title="원본 장소 지식",
                place_id=1,
            )
            proposal = AgentProposal(
                region_id=1,
                place_id=1,
                secondary_place_id=1,
                result_place_id=1,
                action="merge",
                title="원본 장소 제안",
                proposal_key="merge-source-proposal",
            )
            moved_gap = AgentQualityGap(
                region_id=1,
                place_id=1,
                gap_kind="coordinates",
                reason="좌표 재검증 필요",
                condition_fingerprint="merge-source-gap-moved",
            )
            deduplicated_gap = AgentQualityGap(
                region_id=1,
                place_id=1,
                gap_kind="description",
                reason="원본 설명 보강 필요",
                evidence_refs_json='["source-evidence"]',
                condition_fingerprint="merge-source-gap-deduplicated",
                attempt_count=3,
            )
            target_gap = AgentQualityGap(
                region_id=1,
                place_id=2,
                gap_kind="description",
                reason="대상 설명 보강 필요",
                condition_fingerprint="merge-target-gap",
            )
            lesson = AgentLesson(
                lesson_key="merge-source-lesson",
                place_id=1,
                trigger="중복 장소 발견",
                action="참조를 대표 장소로 이동",
            )
            db.add_all([
                work_item,
                evidence,
                knowledge,
                proposal,
                moved_gap,
                deduplicated_gap,
                target_gap,
                lesson,
            ])
            db.commit()
            agent_ids = {
                "task": task.id,
                "work_item": work_item.id,
                "evidence": evidence.id,
                "knowledge": knowledge.id,
                "proposal": proposal.id,
                "moved_gap": moved_gap.id,
                "deduplicated_gap": deduplicated_gap.id,
                "target_gap": target_gap.id,
                "lesson": lesson.id,
            }

        merged = client.post(
            "/api/admin/places/1/merge",
            headers=headers(1),
            json={"target_place_id": 2, "note": "동일 장소 확인"},
        )
        assert merged.status_code == 200
        merge_event_id = merged.json()["event_id"]
        assert merged.json()["status"] == "merged"
        assert merged.json()["moved_counts"]["notes"] == 1
        assert merged.json()["moved_counts"]["agent_proposals_place"] == 1
        assert merged.json()["moved_counts"]["agent_proposals_secondary_place"] == 1
        assert merged.json()["moved_counts"]["agent_proposals_result_place"] == 1
        assert merged.json()["moved_counts"]["agent_quality_gaps_place"] == 1
        assert merged.json()["moved_counts"]["deduplicated_agent_quality_gaps"] == 1
        assert client.get("/api/places/1", headers=headers(2)).status_code == 404
        active_ids = {item["id"] for item in client.get("/api/places", headers=headers(2)).json()}
        assert 1 not in active_ids and 2 in active_ids
        with TestingSession() as db:
            assert db.get(Place, 1).merged_into_id == 2
            assert db.get(PlaceNote, note.json()["id"]).place_id == 2
            assert db.query(Favorite).filter(Favorite.user_id == 3, Favorite.place_id == 2).count() == 1
            assert db.query(PlaceContributor).filter(PlaceContributor.place_id == 2).count() == 1
            assert db.get(AgentTask, agent_ids["task"]).place_id == 2
            assert db.get(AgentWorkItem, agent_ids["work_item"]).place_id == 2
            assert db.get(AgentEvidence, agent_ids["evidence"]).place_id == 2
            assert db.get(AgentKnowledge, agent_ids["knowledge"]).place_id == 2
            merged_proposal = db.get(AgentProposal, agent_ids["proposal"])
            assert (
                merged_proposal.place_id,
                merged_proposal.secondary_place_id,
                merged_proposal.result_place_id,
            ) == (2, 2, 2)
            assert db.get(AgentQualityGap, agent_ids["moved_gap"]).place_id == 2
            assert db.get(AgentQualityGap, agent_ids["deduplicated_gap"]) is None
            assert db.get(AgentQualityGap, agent_ids["target_gap"]).place_id == 2
            assert db.get(AgentLesson, agent_ids["lesson"]).place_id == 2

        events = client.get("/api/admin/place-events", headers=headers(1))
        assert events.status_code == 200
        event = next(item for item in events.json() if item["id"] == merge_event_id)
        assert event["event_type"] == "place_merged"
        assert event["metadata"]["merge_snapshot"]["duplicate_evidence"]["confidence"] >= 0.9
        owner_inbox = client.get("/api/messages", headers=headers(2)).json()
        assert any(item["kind"] == "place_merged" for item in owner_inbox)

        restored = client.post(
            f"/api/admin/place-merges/{merge_event_id}/undo",
            headers=headers(1),
        )
        assert restored.status_code == 200
        assert restored.json()["status"] == "restored"
        assert client.get("/api/places/1", headers=headers(2)).status_code == 200
        with TestingSession() as db:
            assert db.get(Place, 1).merged_into_id is None
            assert db.get(PlaceNote, note.json()["id"]).place_id == 1
            assert db.query(Favorite).filter(Favorite.user_id == 3, Favorite.place_id == 1).count() == 1
            assert db.query(PlaceContributor).filter(PlaceContributor.place_id == 1).count() == 1
            assert db.get(AgentTask, agent_ids["task"]).place_id == 1
            assert db.get(AgentWorkItem, agent_ids["work_item"]).place_id == 1
            assert db.get(AgentEvidence, agent_ids["evidence"]).place_id == 1
            assert db.get(AgentKnowledge, agent_ids["knowledge"]).place_id == 1
            restored_proposal = db.get(AgentProposal, agent_ids["proposal"])
            assert (
                restored_proposal.place_id,
                restored_proposal.secondary_place_id,
                restored_proposal.result_place_id,
            ) == (1, 1, 1)
            assert db.get(AgentQualityGap, agent_ids["moved_gap"]).place_id == 1
            restored_gap = db.get(AgentQualityGap, agent_ids["deduplicated_gap"])
            assert restored_gap.place_id == 1
            assert restored_gap.reason == "원본 설명 보강 필요"
            assert restored_gap.evidence_refs_json == '["source-evidence"]'
            assert restored_gap.attempt_count == 3
            assert db.get(AgentQualityGap, agent_ids["target_gap"]).place_id == 2
            assert db.get(AgentLesson, agent_ids["lesson"]).place_id == 1
        assert client.post(
            f"/api/admin/place-merges/{merge_event_id}/undo",
            headers=headers(1),
        ).status_code == 409


def test_travel_profile_deleted_audit_and_upload_completion(monkeypatch: pytest.MonkeyPatch) -> None:
    class FakeS3:
        def __init__(self) -> None:
            self.presign_params: dict | None = None

        def generate_presigned_url(self, operation: str, *, Params: dict, ExpiresIn: int) -> str:
            assert operation == "put_object"
            self.presign_params = {"Params": Params, "ExpiresIn": ExpiresIn}
            return "https://signed.example.test/upload"

        def head_object(self, *, Bucket: str, Key: str) -> dict:
            assert Bucket == "private-images"
            assert Key.startswith("places/1/uploads/2/")
            return {
                "ContentType": "image/webp",
                "ContentLength": 1024,
                "Metadata": {"declared-size": "1024", "uploader-id": "2"},
            }

    fake_s3 = FakeS3()
    monkeypatch.setattr(settings, "s3_bucket", "private-images")
    monkeypatch.setattr(settings, "s3_public_base_url", "https://images.example.test")
    monkeypatch.setattr(settings, "image_upload_max_bytes", 2048)
    monkeypatch.setattr(settings, "image_presign_expire_seconds", 5000)
    monkeypatch.setattr(parity_module, "_s3_client", lambda: fake_s3)

    with TestClient(parity_test_app) as client:
        with TestingSession() as db:
            db.add(Favorite(user_id=2, place_id=1))
            db.commit()
        profile = client.get("/api/travel-profile", headers=headers(2))
        assert profile.status_code == 200
        assert profile.json()["evidence"]["favorites"] == 1
        assert profile.json()["anchors"][0]["place_id"] == 1
        assert profile.json()["recommendations"]

        presigned = client.post(
            "/api/places/1/images/presign",
            headers=headers(2),
            json={"filename": "temple.webp", "content_type": "image/webp", "size_bytes": 1024},
        )
        assert presigned.status_code == 200
        assert presigned.json()["expires_in"] == 900
        assert presigned.json()["headers"]["x-amz-meta-uploader-id"] == "2"
        assert fake_s3.presign_params is not None
        assert fake_s3.presign_params["Params"]["ContentLength"] == 1024
        assert "Content-Length" not in presigned.json()["headers"]
        key = presigned.json()["s3_key"]
        completed = client.post(
            "/api/places/1/images/complete",
            headers=headers(2),
            json={"s3_key": key, "caption": "직접 업로드"},
        )
        assert completed.status_code == 201
        assert completed.json()["image_url"].startswith("https://images.example.test/places/1/")
        repeated = client.post(
            "/api/places/1/images/complete",
            headers=headers(2),
            json={"s3_key": key, "caption": "중복 완료 호출"},
        )
        assert repeated.status_code == 201
        assert repeated.json()["id"] == completed.json()["id"]
        with TestingSession() as db:
            assert db.query(PlaceImage).count() == 1

        assert client.delete("/api/admin/places/3", headers=headers(1)).status_code == 204
        deleted = client.get(
            "/api/admin/place-events",
            headers=headers(1),
            params={"deleted_only": True},
        )
        assert deleted.status_code == 200
        assert deleted.json()[0]["place_title"] == "추천 카페"
