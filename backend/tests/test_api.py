import json
import os
import tempfile
from datetime import datetime, timedelta, timezone

import pytest


TEST_DIR = tempfile.mkdtemp(prefix="patra-tests-")
os.environ["DATABASE_URL"] = "sqlite:///" + TEST_DIR.replace("\\", "/") + "/test.db"
os.environ["JWT_SECRET"] = "test-secret"
os.environ["SEED_PASSWORD_JOOHAN"] = "admin-test-password"

from fastapi.testclient import TestClient

from app import batch as batch_module
from app.auth import hash_password, verify_password
from app.db import SessionLocal
from app.main import app
from app.config import settings
from app.models import BatchRun, ChatMessage, Place, Region, RegionSnapshot, User
from app.seed import seed_data
from app.travel_chat import (
    MAX_GROQ_INPUT_CHARS,
    MAX_PLAN_CONTEXT_CHARS,
    MAX_PLAN_ITEM_NOTE_CHARS,
    _balanced_places,
    _bounded_model_messages,
    _bounded_plan_context_json,
    _trim_text,
)


def auth_headers(client: TestClient) -> dict[str, str]:
    response = client.post(
        "/api/auth/login",
        json={"email": "test@test.com", "password": "test1234"},
    )
    assert response.status_code == 200
    return {"Authorization": "Bearer " + response.json()["access_token"]}


def test_seeded_archipelago_and_health() -> None:
    with TestClient(app) as client:
        headers = auth_headers(client)
        health = client.get("/api/health").json()
        assert health["status"] == "ok"
        assert health["coordinate_crs"] == "WGS84"
        assert client.get("/api/auth/me", headers=headers).json()["is_admin"] is False
        assert settings.groq_timeout_seconds < 30
        regions = client.get("/api/regions", headers=headers)
        assert regions.status_code == 200
        islands = {item["island"] for item in regions.json()}
        assert {"Bali", "Nusa Penida", "Lombok", "Gili Trawangan"} <= islands


def test_place_filters_favorites_and_trip() -> None:
    with TestClient(app) as client:
        headers = auth_headers(client)
        places = client.get("/api/places?condition=ferry", headers=headers)
        assert places.status_code == 200
        assert places.json()
        place_id = places.json()[0]["id"]
        favorite = client.put("/api/places/" + str(place_id) + "/favorite", headers=headers)
        assert favorite.json()["is_favorite"] is True
        favorites = client.get("/api/places?favorites_only=true", headers=headers).json()
        assert any(item["id"] == place_id for item in favorites)
        added = client.post("/api/trip", headers=headers, json={"place_id": place_id, "day_number": 2})
        assert added.status_code == 201
        assert added.json()["day_number"] == 2
        trip = client.get("/api/trip", headers=headers).json()
        assert len(trip) == 1


def test_local_search() -> None:
    with TestClient(app) as client:
        headers = auth_headers(client)
        response = client.get("/api/search", params={"q": "울루와뚜"}, headers=headers)
        assert response.status_code == 200
        assert response.json()[0]["source"] == "local"


def test_admin_can_manage_places_and_regular_user_cannot() -> None:
    with TestClient(app) as client:
        regular_headers = auth_headers(client)
        assert client.get("/api/admin/summary", headers=regular_headers).status_code == 403
        login = client.post(
            "/api/auth/login",
            json={"email": "joohan92@naver.com", "password": "admin-test-password"},
        )
        assert login.status_code == 200
        assert login.json()["user"]["is_admin"] is True
        admin_headers = {"Authorization": "Bearer " + login.json()["access_token"]}
        assert client.get("/api/auth/me", headers=admin_headers).json()["is_admin"] is True
        summary = client.get("/api/admin/summary", headers=admin_headers)
        assert summary.status_code == 200
        assert summary.json()["region_count"] == 10
        places = client.get("/api/admin/places", headers=admin_headers)
        assert places.status_code == 200
        assert len(places.json()) >= 25


def test_global_exploration_and_grounded_chat_history() -> None:
    with TestClient(app) as client:
        headers = auth_headers(client)
        places = client.get("/api/places", headers=headers)
        assert places.status_code == 200
        assert len({item["region_id"] for item in places.json()}) == 10
        response = client.post(
            "/api/chat",
            headers=headers,
            json={"message": "해변과 카페를 함께 가고 싶어"},
        )
        assert response.status_code == 200
        assert response.json()["message"]["role"] == "assistant"
        assert response.json()["message"]["content"]
        history = client.get("/api/chat", headers=headers)
        assert [item["role"] for item in history.json()[-2:]] == ["user", "assistant"]
        assert client.delete("/api/chat", headers=headers).status_code == 204
        assert client.get("/api/chat", headers=headers).json() == []


def test_global_chat_place_sample_stays_balanced_after_one_region_grows() -> None:
    with TestClient(app):
        with SessionLocal() as db:
            regions = db.query(Region).order_by(Region.sort_order, Region.id).all()
            ubud = next(region for region in regions if region.slug == "ubud")
            db.add_all([
                Place(
                    region_id=ubud.id,
                    category="cafe",
                    title=f"우붓 확장 카페 {index:02d}",
                    lat=ubud.center_lat,
                    lng=ubud.center_lng,
                )
                for index in range(60)
            ])
            db.flush()

            sampled = _balanced_places(db)
            represented = {place.region_id for place in sampled}
            assert len(sampled) == 40
            assert represented == {region.id for region in regions}
            assert {place.region_id for place in sampled[: len(regions)]} == represented
            db.rollback()


def test_chat_prompt_context_has_structural_and_overall_budgets() -> None:
    oversized_context = [
        {
            "title": f"공유 계획 {plan_index}",
            "days": [
                {
                    "date": f"2026-09-{day_index + 1:02d}",
                    "items": [
                        {"place": f"장소 {item_index}", "note": "긴 메모 " * 200}
                        for item_index in range(12)
                    ],
                }
                for day_index in range(14)
            ],
        }
        for plan_index in range(5)
    ]
    rendered = _bounded_plan_context_json(oversized_context)
    assert len(rendered) <= MAX_PLAN_CONTEXT_CHARS
    assert isinstance(json.loads(rendered), list)
    assert len(_trim_text("긴 메모 " * 200, MAX_PLAN_ITEM_NOTE_CHARS)) <= MAX_PLAN_ITEM_NOTE_CHARS

    recent = [
        ChatMessage(role="assistant", content="이전 답변 " * 2000),
        ChatMessage(role="user", content="가장 최근 질문"),
    ]
    messages = _bounded_model_messages("시스템 문맥 " * 10_000, recent)
    serialized = json.dumps(messages, ensure_ascii=False, separators=(",", ":"))
    assert len(serialized) <= MAX_GROQ_INPUT_CHARS
    assert messages[-1] == {"role": "user", "content": "가장 최근 질문"}


def test_seed_does_not_overwrite_an_existing_account_password() -> None:
    with TestClient(app):
        with SessionLocal() as db:
            user = db.query(User).filter(User.email == "joohan92@naver.com").one()
            original_name = user.display_name
            original_hash = user.password_hash
            user.display_name = "관리자 변경 이름"
            user.password_hash = hash_password("rotated-password")
            db.commit()

            seed_data(db)
            db.refresh(user)
            assert user.display_name == "관리자 변경 이름"
            assert verify_password("rotated-password", user.password_hash)

            user.display_name = original_name
            user.password_hash = original_hash
            db.commit()


def test_batch_run_persists_failed_status_after_unexpected_database_error(monkeypatch) -> None:
    with TestClient(app):
        with SessionLocal() as db:
            observed_at = batch_module.datetime.now(batch_module.timezone.utc)
            monkeypatch.setattr(
                batch_module,
                "fetch_weather",
                lambda _region: {
                    "temperature_c": 28.0,
                    "precipitation_mm": 0.0,
                    "wind_kph": 5.0,
                    "weather_code": 0,
                    "summary": "맑음",
                    "source_url": "https://open-meteo.com/",
                    "observed_at": observed_at,
                },
            )
            original_commit = db.commit
            commit_count = 0

            def fail_result_commit_once() -> None:
                nonlocal commit_count
                commit_count += 1
                if commit_count == 2:
                    raise RuntimeError("simulated database error")
                original_commit()

            monkeypatch.setattr(db, "commit", fail_result_commit_once)

            with pytest.raises(RuntimeError, match="simulated database error"):
                batch_module.run_batch(db, trigger="test-failure")

            failed_run = (
                db.query(BatchRun)
                .filter(BatchRun.trigger == "test-failure")
                .order_by(BatchRun.id.desc())
                .first()
            )
            assert failed_run is not None
            assert failed_run.status == "failed"
            assert "RuntimeError" in failed_run.summary
            assert failed_run.finished_at is not None


def test_conditions_mark_old_observations_as_stale() -> None:
    with TestClient(app) as client:
        headers = auth_headers(client)
        with SessionLocal() as db:
            region = db.query(Region).order_by(Region.id).first()
            assert region is not None
            region_id = region.id
            snapshot = RegionSnapshot(
                region_id=region_id,
                temperature_c=27,
                precipitation_mm=0,
                wind_kph=8,
                weather_code=1,
                summary="대체로 맑음",
                observed_at=datetime.now(timezone.utc) - timedelta(hours=13),
            )
            db.add(snapshot)
            db.commit()

        response = client.get("/api/conditions", headers=headers)
        assert response.status_code == 200
        item = next(row for row in response.json() if row["region_id"] == region_id)
        assert item["is_stale"] is True


def test_place_update_creates_a_rollback_capable_audit_event() -> None:
    with TestClient(app) as client:
        regular_headers = auth_headers(client)
        ubud = next(region for region in client.get("/api/regions", headers=regular_headers).json() if region["slug"] == "ubud")
        created = client.post(
            "/api/places",
            headers=regular_headers,
            json={
                "region_id": ubud["id"],
                "category": "cafe",
                "title": "감사 이력 테스트 장소",
                "lat": ubud["center_lat"],
                "lng": ubud["center_lng"],
            },
        )
        assert created.status_code == 201
        place_id = created.json()["id"]
        updated = client.patch(
            f"/api/places/{place_id}",
            headers=regular_headers,
            json={"title": "수정된 감사 이력 테스트 장소"},
        )
        assert updated.status_code == 200

        events = client.get(f"/api/places/{place_id}/events", headers=regular_headers).json()
        update_event = next(event for event in events if event["event_type"] == "place_updated")
        assert update_event["metadata"]["before"]["title"] == "감사 이력 테스트 장소"

        admin_login = client.post(
            "/api/auth/login",
            json={"email": "joohan92@naver.com", "password": "admin-test-password"},
        )
        assert admin_login.status_code == 200
        admin_headers = {"Authorization": "Bearer " + admin_login.json()["access_token"]}
        rollback = client.post(
            f"/api/admin/place-events/{update_event['id']}/rollback",
            headers=admin_headers,
        )
        assert rollback.status_code == 200
        restored = client.get(f"/api/places/{place_id}", headers=regular_headers)
        assert restored.json()["title"] == "감사 이력 테스트 장소"
