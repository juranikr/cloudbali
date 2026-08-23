import os
import tempfile
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

TEST_DIR = tempfile.mkdtemp(prefix="patra-discovery-tests-")
os.environ.setdefault("DATABASE_URL", "sqlite:///" + TEST_DIR.replace("\\", "/") + "/test.db")
os.environ.setdefault("JWT_SECRET", "test-secret")
os.environ.setdefault("SEED_PASSWORD_JOOHAN", "admin-test-password")

from fastapi.testclient import TestClient

from app.db import SessionLocal
from app.main import app
from app.discovery import _claim_discovery_run, create_discovery_run, get_discovery_run
from app.models import BatchRun, DiscoveryCandidate, DiscoveryJob, DiscoveryScanState, Region


def _login(client: TestClient, email: str, password: str) -> dict[str, str]:
    response = client.post("/api/auth/login", json={"email": email, "password": password})
    assert response.status_code == 200
    return {"Authorization": "Bearer " + response.json()["access_token"]}


def _osm_element(element_id: int, *, title: str, lat: float, lng: float) -> dict:
    return {
        "type": "node",
        "id": element_id,
        "lat": lat,
        "lon": lng,
        "tags": {
            "name": title,
            "tourism": "attraction",
            "description": "A locally mapped garden with a public walking path.",
            "website": "https://example.test/place",
            "addr:village": "Mas",
        },
    }


def _completed_run(client: TestClient, headers: dict[str, str], queued_response) -> dict:
    assert queued_response.status_code == 202
    queued = queued_response.json()
    assert queued["run"]["status"] == "queued"
    assert queued["created_count"] == 0
    result = client.get(
        f"/api/admin/discovery/runs/{queued['run']['id']}",
        headers=headers,
    )
    assert result.status_code == 200
    return result.json()


def test_admin_discovery_requires_review_and_keeps_decision_history() -> None:
    with TestClient(app) as client:
        regular = _login(client, "test@test.com", "test1234")
        assert client.get("/api/admin/discovery/candidates", headers=regular).status_code == 403
        admin = _login(client, "joohan92@naver.com", "admin-test-password")
        regions = client.get("/api/regions", headers=admin).json()
        ubud_id = next(item["id"] for item in regions if item["slug"] == "ubud")

        first = _osm_element(
            987654321,
            title="Subak Discovery Garden",
            lat=-8.5300,
            lng=115.2900,
        )
        with patch("app.discovery.fetch_osm_elements", return_value=[first]):
            run = client.post(
                "/api/admin/discovery/run",
                headers=admin,
                json={"region_id": ubud_id, "limit": 10},
            )
        completed = _completed_run(client, admin, run)
        assert completed["run"]["kind"] == "place_discovery"
        assert completed["run"]["status"] == "success"
        assert completed["created_count"] == 1

        candidates = client.get(
            "/api/admin/discovery/candidates",
            headers=admin,
            params={"status": "pending", "region_id": ubud_id},
        )
        assert candidates.status_code == 200
        candidate = next(item for item in candidates.json() if item["external_id"] == "node/987654321")
        assert candidate["source"] == "openstreetmap"
        assert candidate["result_place_id"] is None
        assert '"coordinate_crs":"WGS84"' in candidate["evidence"]

        before = client.get("/api/admin/places", headers=admin).json()
        assert all(item["title"] != "Subak Discovery Garden" for item in before)
        approved = client.post(
            f"/api/admin/discovery/candidates/{candidate['id']}/approve",
            headers=admin,
            json={"note": "OSM 원문과 좌표 확인 완료"},
        )
        assert approved.status_code == 200
        approved_body = approved.json()
        assert approved_body["status"] == "approved"
        assert approved_body["result_place_id"] is not None
        assert approved_body["decision_history"][-1]["action"] == "approved"
        assert approved_body["decision_history"][-1]["admin_email"] == "joohan92@naver.com"

        after = client.get("/api/admin/places", headers=admin).json()
        created_place = next(item for item in after if item["id"] == approved_body["result_place_id"])
        assert created_place["title"] == "Subak Discovery Garden"
        assert created_place["coordinate_source"] == "openstreetmap"
        assert created_place["coordinate_crs"] == "WGS84"

        near_existing_place = _osm_element(
            987654322,
            title="Another Garden Listing",
            lat=-8.52982,
            lng=115.2900,
        )
        with patch("app.discovery.fetch_osm_elements", return_value=[near_existing_place]):
            duplicate_run = client.post(
                "/api/admin/discovery/run",
                headers=admin,
                json={"region_id": ubud_id, "limit": 10},
            )
        duplicate_completed = _completed_run(client, admin, duplicate_run)
        assert duplicate_completed["duplicate_count"] == 1
        duplicate_candidates = client.get(
            "/api/admin/discovery/candidates",
            headers=admin,
            params={"status": "duplicate", "region_id": ubud_id},
        ).json()
        duplicate = next(item for item in duplicate_candidates if item["external_id"] == "node/987654322")
        assert duplicate["duplicate_place_id"] == created_place["id"]

        rejected = client.post(
            f"/api/admin/discovery/candidates/{duplicate['id']}/reject",
            headers=admin,
            json={"note": "이미 승인된 장소와 같은 위치"},
        )
        assert rejected.status_code == 200
        assert rejected.json()["status"] == "rejected"
        assert rejected.json()["decision_history"][-1]["action"] == "rejected"


def test_discovery_persists_an_expanding_scan_window() -> None:
    requested: list[tuple[int, int]] = []

    def full_window(_region, limit: int, phase: int) -> list[dict]:
        requested.append((limit, phase))
        return [
            _osm_element(
                800_000_000 + index,
                title=f"Expanding discovery {index}",
                lat=-8.57 + index * 0.003,
                lng=115.205,
            )
            for index in range(limit)
        ]

    with TestClient(app) as client:
        admin = _login(client, "joohan92@naver.com", "admin-test-password")
        regions = client.get("/api/regions", headers=admin).json()
        ubud_id = next(item["id"] for item in regions if item["slug"] == "ubud")
        with SessionLocal() as db:
            db.query(DiscoveryScanState).filter(DiscoveryScanState.region_id == ubud_id).delete()
            db.commit()
        with patch("app.discovery.fetch_osm_elements", side_effect=full_window):
            first = client.post(
                "/api/admin/discovery/run",
                headers=admin,
                json={"region_id": ubud_id, "limit": 1},
            )
            _completed_run(client, admin, first)
            second = client.post(
                "/api/admin/discovery/run",
                headers=admin,
                json={"region_id": ubud_id, "limit": 1},
            )
            _completed_run(client, admin, second)

    assert requested[:2] == [(20, 0), (40, 0)]


def test_unexpected_discovery_failure_is_finalized() -> None:
    with TestClient(app) as client:
        admin = _login(client, "joohan92@naver.com", "admin-test-password")
        regions = client.get("/api/regions", headers=admin).json()
        ubud_id = next(item["id"] for item in regions if item["slug"] == "ubud")
        element = _osm_element(
            812_345_678,
            title="Failure finalization candidate",
            lat=-8.55,
            lng=115.22,
        )
        with (
            patch("app.discovery.fetch_osm_elements", return_value=[element]),
            patch("app.discovery._candidate_values", side_effect=RuntimeError("unexpected")),
        ):
            queued = client.post(
                "/api/admin/discovery/run",
                headers=admin,
                json={"region_id": ubud_id, "limit": 1},
            )
        completed = _completed_run(client, admin, queued)
        assert completed["run"]["status"] == "failed"
        assert completed["run"]["finished_at"] is not None
        assert "RuntimeError" in completed["run"]["summary"]
        with SessionLocal() as db:
            failed_job = db.get(DiscoveryJob, completed["run"]["id"])
            assert failed_job is not None
            assert failed_job.active_slot is None


def test_duplicate_candidate_requires_explicit_force_and_cannot_be_approved_twice() -> None:
    with TestClient(app) as client:
        admin = _login(client, "joohan92@naver.com", "admin-test-password")
        regions = client.get("/api/regions", headers=admin).json()
        ubud_id = next(item["id"] for item in regions if item["slug"] == "ubud")
        duplicate_element = _osm_element(
            823_456_789,
            title="우붓 왕궁",
            lat=-8.5067,
            lng=115.2622,
        )
        with patch("app.discovery.fetch_osm_elements", return_value=[duplicate_element]):
            queued = client.post(
                "/api/admin/discovery/run",
                headers=admin,
                json={"region_id": ubud_id, "limit": 1},
            )
        _completed_run(client, admin, queued)
        candidates = client.get(
            "/api/admin/discovery/candidates",
            headers=admin,
            params={"status": "duplicate", "region_id": ubud_id},
        ).json()
        candidate = next(item for item in candidates if item["external_id"] == "node/823456789")

        held = client.post(
            f"/api/admin/discovery/candidates/{candidate['id']}/approve",
            headers=admin,
            json={"note": "중복 여부 재확인"},
        )
        assert held.status_code == 200
        assert held.json()["status"] == "duplicate"
        assert held.json()["result_place_id"] is None

        forced = client.post(
            f"/api/admin/discovery/candidates/{candidate['id']}/approve",
            headers=admin,
            json={"note": "서로 다른 장소임을 원문에서 확인", "force": True},
        )
        assert forced.status_code == 200
        assert forced.json()["status"] == "approved"
        assert forced.json()["decision_history"][-1]["action"] == "approved_forced"

        repeated = client.post(
            f"/api/admin/discovery/candidates/{candidate['id']}/approve",
            headers=admin,
            json={"force": True},
        )
        assert repeated.status_code == 409


def test_unique_candidate_collision_does_not_fail_the_run() -> None:
    original_flush = Session.flush
    collision_raised = False

    def flush_with_one_collision(session: Session, objects=None) -> None:
        nonlocal collision_raised
        if not collision_raised and any(isinstance(row, DiscoveryCandidate) for row in session.new):
            collision_raised = True
            raise IntegrityError("candidate insert", {}, RuntimeError("unique collision"))
        original_flush(session, objects)

    with TestClient(app) as client:
        admin = _login(client, "joohan92@naver.com", "admin-test-password")
        regions = client.get("/api/regions", headers=admin).json()
        ubud_id = next(item["id"] for item in regions if item["slug"] == "ubud")
        element = _osm_element(
            834_567_890,
            title="Concurrent source candidate",
            lat=-8.545,
            lng=115.235,
        )
        with (
            patch("app.discovery.fetch_osm_elements", return_value=[element]),
            patch.object(Session, "flush", new=flush_with_one_collision),
        ):
            queued = client.post(
                "/api/admin/discovery/run",
                headers=admin,
                json={"region_id": ubud_id, "limit": 1},
            )
        completed = _completed_run(client, admin, queued)
        assert collision_raised
        assert completed["run"]["status"] == "success"
        assert completed["created_count"] == 0
        assert completed["duplicate_count"] == 1


def test_global_discovery_lease_returns_active_run_in_http_conflict() -> None:
    active_run_id: int | None = None
    with TestClient(app) as client:
        admin = _login(client, "joohan92@naver.com", "admin-test-password")
        regions = client.get("/api/regions", headers=admin).json()
        ubud_id = next(item["id"] for item in regions if item["slug"] == "ubud")
        try:
            with SessionLocal() as db:
                active = create_discovery_run(db, region_id=ubud_id, limit=1)
                active_run_id = active.run.id

            conflict = client.post(
                "/api/admin/discovery/run",
                headers=admin,
                json={"region_id": ubud_id, "limit": 1},
            )

            assert conflict.status_code == 409
            assert f"#{active_run_id}" in conflict.json()["detail"]
        finally:
            if active_run_id is not None:
                with SessionLocal() as db:
                    run = db.get(BatchRun, active_run_id)
                    job = db.get(DiscoveryJob, active_run_id)
                    if run is not None:
                        run.status = "failed"
                        run.finished_at = datetime.now(timezone.utc)
                    if job is not None:
                        job.active_slot = None
                    db.commit()


def test_only_one_worker_can_claim_the_same_queued_run() -> None:
    run_id: int | None = None
    with TestClient(app):
        try:
            with SessionLocal() as db:
                region = db.query(Region).filter(Region.slug == "ubud").one()
                queued = create_discovery_run(db, region_id=region.id, limit=1)
                run_id = queued.run.id

            with SessionLocal() as first_worker:
                first_run, _, first_claimed = _claim_discovery_run(first_worker, run_id)
                assert first_claimed is True
                assert first_run.status == "running"

            with SessionLocal() as duplicate_worker:
                duplicate_run, _, duplicate_claimed = _claim_discovery_run(
                    duplicate_worker, run_id
                )
                assert duplicate_claimed is False
                assert duplicate_run.status == "running"
        finally:
            if run_id is not None:
                with SessionLocal() as db:
                    run = db.get(BatchRun, run_id)
                    job = db.get(DiscoveryJob, run_id)
                    if run is not None:
                        run.status = "failed"
                        run.finished_at = datetime.now(timezone.utc)
                    if job is not None:
                        job.active_slot = None
                    db.commit()


def test_stale_queued_discovery_run_is_finalized() -> None:
    with TestClient(app):
        with SessionLocal() as db:
            region = db.query(Region).filter(Region.slug == "ubud").one()
            queued = create_discovery_run(db, region_id=region.id, limit=1)
            queued_row = db.get(BatchRun, queued.run.id)
            assert queued_row is not None
            queued_row.started_at = datetime.now(timezone.utc) - timedelta(minutes=31)
            db.commit()

            result = get_discovery_run(db, queued_row.id)

            assert result is not None
            assert result.run.status == "failed"
            assert result.run.finished_at is not None
            assert "다시 실행" in result.run.summary
            stale_job = db.get(DiscoveryJob, queued_row.id)
            assert stale_job is not None
            assert stale_job.active_slot is None

            replacement = create_discovery_run(db, region_id=region.id, limit=1)
            assert replacement.run.id != queued_row.id
            replacement_run = db.get(BatchRun, replacement.run.id)
            replacement_job = db.get(DiscoveryJob, replacement.run.id)
            assert replacement_run is not None
            assert replacement_job is not None
            replacement_run.status = "failed"
            replacement_run.finished_at = datetime.now(timezone.utc)
            replacement_job.active_slot = None
            db.commit()
