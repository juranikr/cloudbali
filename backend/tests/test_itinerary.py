import os
import tempfile


TEST_DIR = tempfile.mkdtemp(prefix="patra-itinerary-tests-")
os.environ.setdefault("DATABASE_URL", "sqlite:///" + TEST_DIR.replace("\\", "/") + "/test.db")
os.environ.setdefault("JWT_SECRET", "test-secret")
os.environ.setdefault("SEED_PASSWORD_JOOHAN", "admin-test-password")

from fastapi.testclient import TestClient

from app.db import SessionLocal
from app.extended_models import PlaceChangeEvent
from app.itinerary_api import router as itinerary_router
from app.main import app


if not any(getattr(route, "path", "") == "/api/itineraries" for route in app.routes):
    app.include_router(itinerary_router)


def _login(client: TestClient, email: str, password: str) -> dict[str, str]:
    response = client.post("/api/auth/login", json={"email": email, "password": password})
    assert response.status_code == 200
    return {"Authorization": "Bearer " + response.json()["access_token"]}


def test_dated_shared_itinerary_permissions_and_crud() -> None:
    with TestClient(app) as client:
        owner = _login(client, "joohan92@naver.com", "admin-test-password")
        traveler = _login(client, "test@test.com", "test1234")
        place_id = client.get("/api/places", headers=owner).json()[0]["id"]

        created = client.post(
            "/api/itineraries",
            headers=owner,
            json={
                "title": "발리와 길리 3일",
                "description": "배편을 고려한 공유 일정",
                "start_date": "2026-09-10",
                "end_date": "2026-09-12",
            },
        )
        assert created.status_code == 201
        plan = created.json()
        plan_id = plan["id"]
        assert plan["timezone"] == "Asia/Makassar"
        assert plan["visibility"] == "private"
        assert plan["current_role"] == "owner"

        mine = client.get("/api/itineraries", headers=owner)
        assert mine.status_code == 200
        assert any(item["id"] == plan_id for item in mine.json())

        outside_day = client.post(
            f"/api/itineraries/{plan_id}/days",
            headers=owner,
            json={"calendar_date": "2026-09-13", "title": "범위 밖"},
        )
        assert outside_day.status_code == 422

        day_response = client.post(
            f"/api/itineraries/{plan_id}/days",
            headers=owner,
            json={"calendar_date": "2026-09-10", "title": "우붓 첫날", "note": "느슨하게 시작"},
        )
        assert day_response.status_code == 201
        day_id = day_response.json()["id"]

        item_response = client.post(
            f"/api/itineraries/{plan_id}/days/{day_id}/items",
            headers=owner,
            json={
                "place_id": place_id,
                "start_time": "09:00",
                "end_time": "10:30",
                "note": "교통 정체 전에 출발",
            },
        )
        assert item_response.status_code == 201
        item_id = item_response.json()["id"]
        assert item_response.json()["place"]["id"] == place_id

        bad_time = client.patch(
            f"/api/itineraries/{plan_id}/items/{item_id}",
            headers=owner,
            json={"start_time": "11:00", "end_time": "10:00"},
        )
        assert bad_time.status_code == 422

        shrink = client.patch(
            f"/api/itineraries/{plan_id}",
            headers=owner,
            json={"start_date": "2026-09-11"},
        )
        assert shrink.status_code == 409

        invited = client.post(
            f"/api/itineraries/{plan_id}/members",
            headers=owner,
            json={"email": "test@test.com", "role": "viewer"},
        )
        assert invited.status_code == 200
        member_id = invited.json()["id"]
        assert invited.json()["role"] == "viewer"
        assert client.get(f"/api/itineraries/{plan_id}", headers=traveler).status_code == 200
        assert client.patch(
            f"/api/itineraries/{plan_id}",
            headers=traveler,
            json={"title": "뷰어가 바꾸려는 제목"},
        ).status_code == 403

        promoted = client.post(
            f"/api/itineraries/{plan_id}/members",
            headers=owner,
            json={"email": "test@test.com", "role": "editor"},
        )
        assert promoted.status_code == 200
        assert promoted.json()["id"] == member_id
        assert promoted.json()["role"] == "editor"
        edited = client.patch(
            f"/api/itineraries/{plan_id}",
            headers=traveler,
            json={"title": "함께 다듬은 발리와 길리 3일"},
        )
        assert edited.status_code == 200
        assert edited.json()["title"].startswith("함께 다듬은")
        assert client.patch(
            f"/api/itineraries/{plan_id}",
            headers=traveler,
            json={"visibility": "public"},
        ).status_code == 403
        assert client.post(
            f"/api/itineraries/{plan_id}/days",
            headers=traveler,
            json={"calendar_date": "2026-09-11", "title": "편집자가 만든 둘째 날"},
        ).status_code == 201

        shared = client.post(f"/api/itineraries/{plan_id}/share", headers=owner)
        assert shared.status_code == 200
        token = shared.json()["share_token"]
        assert shared.json()["visibility"] == "shared"
        public_detail = client.get("/api/shared-itineraries/" + token)
        assert public_detail.status_code == 200
        assert public_detail.json()["members"] == []
        assert public_detail.json()["owner"]["email"] is None
        assert public_detail.json()["days"][0]["items"][0]["creator"]["email"] is None

        assert client.post(f"/api/itineraries/{plan_id}/share", headers=traveler).status_code == 403
        assert client.delete(f"/api/itineraries/{plan_id}/share", headers=owner).status_code == 204
        assert client.get("/api/shared-itineraries/" + token).status_code == 404

        assert client.delete(
            f"/api/itineraries/{plan_id}/members/{member_id}",
            headers=owner,
        ).status_code == 204
        assert client.get(f"/api/itineraries/{plan_id}", headers=traveler).status_code == 403
        assert client.delete(f"/api/itineraries/{plan_id}", headers=owner).status_code == 204
        assert client.get(f"/api/itineraries/{plan_id}", headers=owner).status_code == 404


def test_item_reorder_requires_the_complete_day_and_updates_atomically() -> None:
    with TestClient(app) as client:
        owner = _login(client, "joohan92@naver.com", "admin-test-password")
        place_ids = [row["id"] for row in client.get("/api/places", headers=owner).json()[:3]]
        assert len(place_ids) == 3

        plan_response = client.post(
            "/api/itineraries",
            headers=owner,
            json={
                "title": "순서 변경 회귀 테스트",
                "start_date": "2026-10-01",
                "end_date": "2026-10-01",
            },
        )
        assert plan_response.status_code == 201
        plan_id = plan_response.json()["id"]
        day_response = client.post(
            f"/api/itineraries/{plan_id}/days",
            headers=owner,
            json={"calendar_date": "2026-10-01"},
        )
        assert day_response.status_code == 201
        day_id = day_response.json()["id"]

        item_ids: list[int] = []
        for index, place_id in enumerate(place_ids):
            item = client.post(
                f"/api/itineraries/{plan_id}/days/{day_id}/items",
                headers=owner,
                json={"place_id": place_id, "note": f"원래 순서 {index + 1}"},
            )
            assert item.status_code == 201
            item_ids.append(item.json()["id"])

        incomplete = client.put(
            f"/api/itineraries/{plan_id}/days/{day_id}/items/reorder",
            headers=owner,
            json={"item_ids": item_ids[:2]},
        )
        assert incomplete.status_code == 409
        assert "새로고침" in incomplete.json()["detail"]
        unchanged = client.get(f"/api/itineraries/{plan_id}", headers=owner).json()
        assert [item["id"] for item in unchanged["days"][0]["items"]] == item_ids

        requested_order = list(reversed(item_ids))
        reordered = client.put(
            f"/api/itineraries/{plan_id}/days/{day_id}/items/reorder",
            headers=owner,
            json={"item_ids": requested_order},
        )
        assert reordered.status_code == 200
        assert [item["id"] for item in reordered.json()] == requested_order
        assert [item["sort_order"] for item in reordered.json()] == [10, 20, 30]

        detail = client.get(f"/api/itineraries/{plan_id}", headers=owner)
        assert detail.status_code == 200
        assert [item["id"] for item in detail.json()["days"][0]["items"]] == requested_order
        assert client.delete(f"/api/itineraries/{plan_id}", headers=owner).status_code == 204


def test_place_used_by_itinerary_returns_conflict_for_user_and_admin_delete() -> None:
    with TestClient(app) as client:
        owner = _login(client, "joohan92@naver.com", "admin-test-password")
        region = client.get("/api/regions", headers=owner).json()[0]
        place_response = client.post(
            "/api/places",
            headers=owner,
            json={
                "region_id": region["id"],
                "category": "other",
                "title": "일정 참조 삭제 충돌 테스트 장소",
                "lat": region["center_lat"],
                "lng": region["center_lng"],
            },
        )
        assert place_response.status_code == 201
        place_id = place_response.json()["id"]

        plan_response = client.post(
            "/api/itineraries",
            headers=owner,
            json={
                "title": "장소 삭제 보호 테스트",
                "start_date": "2026-10-03",
                "end_date": "2026-10-03",
            },
        )
        plan_id = plan_response.json()["id"]
        day_response = client.post(
            f"/api/itineraries/{plan_id}/days",
            headers=owner,
            json={"calendar_date": "2026-10-03"},
        )
        day_id = day_response.json()["id"]
        item_response = client.post(
            f"/api/itineraries/{plan_id}/days/{day_id}/items",
            headers=owner,
            json={"place_id": place_id},
        )
        assert item_response.status_code == 201
        item_id = item_response.json()["id"]

        user_delete = client.delete(f"/api/places/{place_id}", headers=owner)
        assert user_delete.status_code == 409
        assert "여행 일정에서 사용 중" in user_delete.json()["detail"]

        admin_delete = client.delete(f"/api/admin/places/{place_id}", headers=owner)
        assert admin_delete.status_code == 409
        assert "여행 일정에서 사용 중" in admin_delete.json()["detail"]
        assert client.get(f"/api/places/{place_id}", headers=owner).status_code == 200

        assert client.delete(
            f"/api/itineraries/{plan_id}/items/{item_id}",
            headers=owner,
        ).status_code == 204
        assert client.delete(f"/api/admin/places/{place_id}", headers=owner).status_code == 204
        with SessionLocal() as db:
            deletion = (
                db.query(PlaceChangeEvent)
                .filter(PlaceChangeEvent.event_type == "place_deleted")
                .order_by(PlaceChangeEvent.id.desc())
                .first()
            )
            assert deletion is not None
            assert "deleted_place" in deletion.metadata_json
        assert client.delete(f"/api/itineraries/{plan_id}", headers=owner).status_code == 204
