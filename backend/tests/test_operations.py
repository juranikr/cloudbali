from __future__ import annotations

from collections.abc import Generator

import pytest
from fastapi import Depends, FastAPI, Header, HTTPException
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from app.auth import get_admin_user, get_current_user, verify_password
from app.config import settings
from app.db import Base, get_db
from app.models import Place, Region, User
from app.operations_api import record_place_change_event, router


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
    x_test_user: int = Header(default=3, alias="X-Test-User"),
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


operations_test_app = FastAPI()
operations_test_app.include_router(router)
operations_test_app.dependency_overrides[get_db] = override_db
operations_test_app.dependency_overrides[get_current_user] = override_current_user
operations_test_app.dependency_overrides[get_admin_user] = override_admin_user


@pytest.fixture(autouse=True)
def reset_database(monkeypatch: pytest.MonkeyPatch) -> Generator[None, None, None]:
    monkeypatch.setattr(settings, "admin_emails", "admin1@example.com,admin2@example.com")
    Base.metadata.drop_all(bind=engine)
    Base.metadata.create_all(bind=engine)
    with TestingSession() as db:
        db.add_all([
            User(id=1, email="admin1@example.com", display_name="관리자 하나", password_hash="unused"),
            User(id=2, email="admin2@example.com", display_name="관리자 둘", password_hash="unused"),
            User(id=3, email="traveler@example.com", display_name="여행자", password_hash="unused"),
            User(id=4, email="other@example.com", display_name="다른 사용자", password_hash="unused"),
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
        db.add(Place(
            id=1,
            region_id=1,
            creator_id=3,
            category="culture",
            title="테스트 사원",
            local_name="Pura Test",
            lat=-8.51,
            lng=115.26,
            coordinate_crs="WGS84",
        ))
        db.commit()
    yield


def headers(user_id: int) -> dict[str, str]:
    return {"X-Test-User": str(user_id)}


def test_admin_user_lifecycle_hashes_password_and_blocks_self_delete() -> None:
    with TestClient(operations_test_app) as client:
        forbidden = client.post(
            "/api/admin/users",
            headers=headers(3),
            json={"email": "new@example.com", "display_name": "새 사용자", "password": "password-123"},
        )
        assert forbidden.status_code == 403

        created = client.post(
            "/api/admin/users",
            headers=headers(1),
            json={"email": "NEW@example.com", "display_name": "새 사용자", "password": "password-123"},
        )
        assert created.status_code == 201
        created_body = created.json()
        assert created_body["email"] == "new@example.com"
        assert "password" not in created_body
        assert created_body["owned_plan_count"] == 0
        assert created_body["note_count"] == 0
        with TestingSession() as db:
            stored = db.get(User, created_body["id"])
            assert stored is not None
            assert stored.password_hash != "password-123"
            assert verify_password("password-123", stored.password_hash)

        duplicate = client.post(
            "/api/admin/users",
            headers=headers(1),
            json={"email": "new@example.com", "display_name": "중복", "password": "password-123"},
        )
        assert duplicate.status_code == 409

        updated = client.patch(
            f"/api/admin/users/{created_body['id']}",
            headers=headers(1),
            json={"display_name": "수정 사용자", "password": "changed-456"},
        )
        assert updated.status_code == 200
        assert updated.json()["display_name"] == "수정 사용자"
        with TestingSession() as db:
            assert verify_password("changed-456", db.get(User, created_body["id"]).password_hash)

        assert client.delete("/api/admin/users/1", headers=headers(1)).status_code == 409
        protected_note = client.post(
            "/api/places/1/notes",
            headers=headers(created_body["id"]),
            json={"content": "삭제 전에 보존해야 하는 사용자 메모"},
        )
        assert protected_note.status_code == 201
        listed = client.get("/api/admin/users", headers=headers(1))
        listed_user = next(item for item in listed.json() if item["id"] == created_body["id"])
        assert listed_user["note_count"] == 1
        assert listed_user["owned_plan_count"] == 0
        assert listed_user["image_count"] == 0
        assert listed_user["appeal_count"] == 0
        blocked = client.delete(f"/api/admin/users/{created_body['id']}", headers=headers(1))
        assert blocked.status_code == 409
        assert "사용자 콘텐츠 보존" in blocked.json()["detail"]
        assert client.delete(
            f"/api/notes/{protected_note.json()['id']}", headers=headers(1)
        ).status_code == 204
        assert client.delete(f"/api/admin/users/{created_body['id']}", headers=headers(1)).status_code == 204


def test_notes_and_https_images_enforce_owner_or_admin_permissions() -> None:
    with TestClient(operations_test_app) as client:
        note = client.post(
            "/api/places/1/notes",
            headers=headers(3),
            json={"content": "비 오는 날 계단이 미끄러워요."},
        )
        assert note.status_code == 201
        note_id = note.json()["id"]
        assert note.json()["is_mine"] is True
        assert client.patch(
            f"/api/notes/{note_id}", headers=headers(4), json={"content": "권한 없는 변경"}
        ).status_code == 403
        admin_edit = client.patch(
            f"/api/notes/{note_id}", headers=headers(1), json={"content": "관리자가 확인한 메모"}
        )
        assert admin_edit.status_code == 200
        assert admin_edit.json()["content"] == "관리자가 확인한 메모"

        insecure = client.post(
            "/api/places/1/images",
            headers=headers(3),
            json={"image_url": "http://example.com/photo.jpg"},
        )
        assert insecure.status_code == 422

        first = client.post(
            "/api/places/1/images",
            headers=headers(3),
            json={
                "image_url": "https://images.example.com/one.jpg",
                "caption": "정문",
                "source_url": "https://example.com/source-one",
            },
        )
        second = client.post(
            "/api/places/1/images",
            headers=headers(3),
            json={"image_url": "https://images.example.com/two.jpg", "caption": "안뜰"},
        )
        assert first.status_code == second.status_code == 201
        first_id, second_id = first.json()["id"], second.json()["id"]
        caption_only = client.patch(
            f"/api/place-images/{first_id}",
            headers=headers(3),
            json={"caption": "새로 확인한 정문"},
        )
        assert caption_only.status_code == 200
        assert caption_only.json()["image_url"] == "https://images.example.com/one.jpg"
        assert client.patch(
            f"/api/place-images/{first_id}",
            headers=headers(3),
            json={"caption": None},
        ).status_code == 422
        reordered = client.put(
            "/api/places/1/images/order",
            headers=headers(3),
            json={"image_ids": [second_id, first_id]},
        )
        assert reordered.status_code == 200
        assert [item["id"] for item in reordered.json()] == [second_id, first_id]
        assert client.delete(f"/api/place-images/{first_id}", headers=headers(4)).status_code == 403
        assert client.delete(f"/api/place-images/{first_id}", headers=headers(1)).status_code == 204

        events = client.get("/api/places/1/events", headers=headers(3))
        assert events.status_code == 200
        assert {item["event_type"] for item in events.json()} >= {
            "note_added", "note_updated", "image_added", "images_reordered", "image_deleted"
        }
        assert client.delete(f"/api/notes/{note_id}", headers=headers(3)).status_code == 204


def test_change_event_appeals_are_private_to_owner_and_admin_resolves_once() -> None:
    with TestClient(operations_test_app) as client:
        client.post(
            "/api/places/1/images",
            headers=headers(3),
            json={"image_url": "https://images.example.com/evidence.jpg"},
        )
        event_id = client.get("/api/places/1/events", headers=headers(3)).json()[0]["id"]
        appeal = client.post(
            "/api/appeals",
            headers=headers(3),
            json={"event_id": event_id, "reason": "사진 오류", "detail": "이 사진은 다른 사원입니다."},
        )
        assert appeal.status_code == 201
        appeal_id = appeal.json()["id"]
        assert appeal.json()["status"] == "open"
        assert client.post(
            "/api/appeals",
            headers=headers(3),
            json={"event_id": event_id, "reason": "중복", "detail": "다시 제출"},
        ).status_code == 409

        mine = client.get("/api/appeals/mine", headers=headers(3))
        assert [item["id"] for item in mine.json()] == [appeal_id]
        assert client.get("/api/appeals/mine", headers=headers(4)).json() == []
        assert client.get("/api/admin/appeals", headers=headers(3)).status_code == 403
        queue = client.get("/api/admin/appeals", headers=headers(1))
        assert [item["id"] for item in queue.json()] == [appeal_id]

        resolved = client.patch(
            f"/api/admin/appeals/{appeal_id}/resolve",
            headers=headers(1),
            json={"status": "resolved", "resolution": "잘못 연결된 사진을 삭제했습니다."},
        )
        assert resolved.status_code == 200
        assert resolved.json()["resolved_by_id"] == 1
        assert resolved.json()["status"] == "resolved"
        assert client.patch(
            f"/api/admin/appeals/{appeal_id}/resolve",
            headers=headers(1),
            json={"status": "dismissed", "resolution": "재검토"},
        ).status_code == 409
        assert client.get("/api/admin/appeals?status=open", headers=headers(1)).json() == []


def test_admin_rolls_back_validated_place_snapshot_only_once() -> None:
    with TestingSession() as db:
        place = db.get(Place, 1)
        before = {"title": place.title, "budget_level": place.budget_level, "tags": ["사원", "우천주의"]}
        place.title = "잘못 바뀐 장소명"
        place.budget_level = 4
        place.tags = "오류"
        event = record_place_change_event(
            db,
            place_id=place.id,
            actor_id=3,
            event_type="place_updated",
            summary="장소 기본 정보 수정",
            field_name="title,budget_level,tags",
            metadata={
                "before": before,
                "after": {"title": place.title, "budget_level": place.budget_level, "tags": place.tags},
            },
        )
        db.commit()
        event_id = event.id

    with TestClient(operations_test_app) as client:
        assert client.post(
            f"/api/admin/place-events/{event_id}/rollback", headers=headers(3)
        ).status_code == 403
        rolled_back = client.post(
            f"/api/admin/place-events/{event_id}/rollback", headers=headers(1)
        )
        assert rolled_back.status_code == 200
        assert rolled_back.json()["event_type"] == "rollback"
        assert rolled_back.json()["rollback_of_event_id"] == event_id
        with TestingSession() as db:
            place = db.get(Place, 1)
            assert place.title == "테스트 사원"
            assert place.budget_level == 1
            assert place.tags == "사원,우천주의"
        assert client.post(
            f"/api/admin/place-events/{event_id}/rollback", headers=headers(1)
        ).status_code == 409
