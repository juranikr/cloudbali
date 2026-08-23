import os
import tempfile


TEST_DIR = tempfile.mkdtemp(prefix="patra-tests-")
os.environ["DATABASE_URL"] = "sqlite:///" + TEST_DIR.replace("\\", "/") + "/test.db"
os.environ["JWT_SECRET"] = "test-secret"
os.environ["SEED_PASSWORD_JOOHAN"] = "admin-test-password"

from fastapi.testclient import TestClient

from app.main import app


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
        admin_headers = {"Authorization": "Bearer " + login.json()["access_token"]}
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
