import json
import os
import tempfile
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import patch

from sqlalchemy import event
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

TEST_DIR = tempfile.mkdtemp(prefix="patra-discovery-tests-")
os.environ.setdefault("DATABASE_URL", "sqlite:///" + TEST_DIR.replace("\\", "/") + "/test.db")
os.environ.setdefault("JWT_SECRET", "test-secret")
os.environ.setdefault("SEED_PASSWORD_JOOHAN", "admin-test-password")

from fastapi.testclient import TestClient

from app.db import SessionLocal
from app.main import app
from app.discovery import (
    _candidate_values,
    _claim_discovery_run,
    _locked_active_places_by_region,
    create_discovery_run,
    get_discovery_run,
    inactive_place_reason,
    prepare_discovery_retry,
    revalidate_candidate_lifecycle,
)
from app.models import BatchRun, DiscoveryCandidate, DiscoveryJob, DiscoveryScanState, Place, Region


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
        assert "우붓 권역" in candidate["description"]
        assert "관광 명소" in candidate["description"]
        assert "A locally mapped garden" not in candidate["description"]
        assert '"coordinate_crs":"WGS84"' in candidate["evidence"]
        assert "A locally mapped garden" in candidate["evidence"]

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


def test_candidate_description_uses_controlled_osm_tags_in_korean() -> None:
    region = SimpleNamespace(
        name_ko="우붓",
        south=-8.58,
        west=115.20,
        north=-8.40,
        east=115.36,
    )
    element = {
        "type": "node",
        "id": 801_234_590,
        "lat": -8.52,
        "lon": 115.28,
        "tags": {
            "name": "Sample Warung",
            "amenity": "restaurant",
            "cuisine": "indonesian;seafood;unmapped_value",
            "description:en": "A popular hidden gem according to an editor.",
        },
    }

    values = _candidate_values(region, element)

    assert values is not None
    assert values.description == (
        "우붓 권역에 있는 장소로, OpenStreetMap에는 음식점 유형으로 등록되어 있습니다. "
        "요리 태그에는 인도네시아 요리, 해산물 정보가 포함되어 있습니다."
    )
    assert "hidden gem" not in values.description
    assert "A popular hidden gem" in values.evidence


def test_candidate_description_keeps_a_korean_source_description() -> None:
    region = SimpleNamespace(
        name_ko="우붓",
        south=-8.58,
        west=115.20,
        north=-8.40,
        east=115.36,
    )
    element = {
        "type": "node",
        "id": 801_234_591,
        "lat": -8.52,
        "lon": 115.28,
        "tags": {
            "name": "Sample Museum",
            "tourism": "museum",
            "description:ko": "지역 공예품을 전시하는 작은 박물관입니다.",
            "description:en": "A small craft museum.",
        },
    }

    values = _candidate_values(region, element)

    assert values is not None
    assert values.description == "지역 공예품을 전시하는 작은 박물관입니다."


def test_candidate_description_covers_observed_discovery_types() -> None:
    region = SimpleNamespace(
        name_ko="우붓",
        south=-8.58,
        west=115.20,
        north=-8.40,
        east=115.36,
    )
    cases = [
        ({"tourism": "artwork"}, "예술 작품"),
        ({"tourism": "attraction"}, "관광 명소"),
        ({"tourism": "museum"}, "박물관"),
        ({"tourism": "viewpoint"}, "전망대"),
        ({"natural": "peak"}, "산봉우리"),
        ({"historic": "monument"}, "기념물"),
        ({"amenity": "place_of_worship", "religion": "hindu"}, "힌두교 종교 시설"),
        ({"amenity": "restaurant"}, "음식점"),
    ]

    for offset, (type_tags, expected_label) in enumerate(cases):
        values = _candidate_values(region, {
            "type": "node",
            "id": 801_234_600 + offset,
            "lat": -8.52,
            "lon": 115.28,
            "tags": {"name": f"Observed type {offset}", **type_tags},
        })
        assert values is not None
        assert expected_label in values.description


def test_candidate_list_supplies_korean_fallback_for_legacy_blank_description() -> None:
    with TestClient(app) as client:
        admin = _login(client, "joohan92@naver.com", "admin-test-password")
        regions = client.get("/api/regions", headers=admin).json()
        ubud_id = next(item["id"] for item in regions if item["slug"] == "ubud")
        with SessionLocal() as db:
            run = BatchRun(kind="place_discovery", status="success", trigger="manual")
            db.add(run)
            db.flush()
            candidate = DiscoveryCandidate(
                discovery_run_id=run.id,
                region_id=ubud_id,
                source="openstreetmap",
                external_id="node/801234699",
                source_url="https://www.openstreetmap.org/node/801234699",
                title="Legacy Blank Artwork",
                local_name="",
                description="",
                area="",
                category="culture",
                lat=-8.52,
                lng=115.28,
                confidence=0.7,
                evidence=json.dumps({
                    "matched_rule": "cultural=artwork",
                    "osm_tags": {"name": "Legacy Blank Artwork", "tourism": "artwork"},
                }),
                tags="culture,artwork",
                status="pending",
            )
            db.add(candidate)
            db.commit()
            candidate_id = candidate.id

        response = client.get(
            "/api/admin/discovery/candidates",
            headers=admin,
            params={"region_id": ubud_id, "limit": 200},
        )

        assert response.status_code == 200
        row = next(item for item in response.json() if item["id"] == candidate_id)
        assert row["description"] == (
            "우붓 권역에 있는 장소로, OpenStreetMap에는 예술 작품 유형으로 등록되어 있습니다."
        )


def test_discovery_locks_multi_region_places_with_one_global_id_query() -> None:
    with TestClient(app):
        with SessionLocal() as db:
            regions = db.query(Region).order_by(Region.id).limit(2).all()
            assert len(regions) == 2
            lock_statements: list[str] = []

            def record_lock(execute_state) -> None:
                statement = execute_state.statement
                if getattr(statement, "_for_update_arg", None) is None:
                    return
                descriptions = getattr(statement, "column_descriptions", [])
                entity = descriptions[0].get("entity") if descriptions else None
                if entity is Place:
                    lock_statements.append(str(statement))

            event.listen(db, "do_orm_execute", record_lock)
            try:
                grouped = _locked_active_places_by_region(
                    db,
                    [regions[1].id, regions[0].id],
                )
            finally:
                event.remove(db, "do_orm_execute", record_lock)

            assert set(grouped) == {regions[0].id, regions[1].id}
            assert len(lock_statements) == 1
            assert "ORDER BY places.id" in lock_statements[0]
            assert "FOR UPDATE" in lock_statements[0]


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


def test_discovery_excludes_explicitly_closed_osm_candidate() -> None:
    with TestClient(app) as client:
        admin = _login(client, "joohan92@naver.com", "admin-test-password")
        regions = client.get("/api/regions", headers=admin).json()
        ubud_id = next(item["id"] for item in regions if item["slug"] == "ubud")
        closed = _osm_element(
            801_234_567,
            title="Permanently Closed Museum",
            lat=-8.51,
            lng=115.27,
        )
        closed["tags"]["opening_hours"] = "closed"

        with patch("app.discovery.fetch_osm_elements", return_value=[closed]):
            queued = client.post(
                "/api/admin/discovery/run",
                headers=admin,
                json={"region_id": ubud_id, "limit": 1},
            )
        completed = _completed_run(client, admin, queued)

        assert completed["created_count"] == 0
        assert completed["invalid_count"] == 1
        candidates = client.get(
            "/api/admin/discovery/candidates",
            headers=admin,
            params={"region_id": ubud_id, "limit": 200},
        ).json()
        assert all(item["external_id"] != "node/801234567" for item in candidates)


def test_inactive_lifecycle_parser_blocks_explicit_status_and_full_week_off() -> None:
    assert inactive_place_reason({"status": "inactive"}) == "status=inactive"
    assert inactive_place_reason({"operational_status": "non_operational"}) == "operational_status=non_operational"
    assert inactive_place_reason({"opening_hours": "Mo-Su off"}) == "opening_hours=mo-su off"
    assert inactive_place_reason({"opening_hours": "Mo-Fr 09:00-17:00; PH off"}) == ""


def test_exact_osm_approval_revalidation_reads_current_lifecycle_tags() -> None:
    payload = json.dumps({
        "elements": [{
            "type": "node",
            "id": 801_234_568,
            "tags": {"name": "Closed after discovery", "opening_hours": "Mo-Su off"},
        }]
    }).encode("utf-8")

    class JsonResponse:
        headers = {"Content-Type": "application/json; charset=utf-8"}

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def read(self, _limit: int) -> bytes:
            return payload

    candidate = SimpleNamespace(
        source="openstreetmap",
        external_id="node/801234568",
        evidence="{}",
    )
    with patch("app.discovery.urlopen", return_value=JsonResponse()):
        reason, evidence = revalidate_candidate_lifecycle(candidate)

    assert reason == "opening_hours=mo-su off"
    assert evidence["checks"][0]["active"] is False
    assert evidence["checks"][0]["external_id"] == "node/801234568"


def test_exact_osm_approval_revalidation_rejects_large_coordinate_drift() -> None:
    payload = json.dumps({
        "elements": [{
            "type": "node",
            "id": 801_234_580,
            "lat": -8.40,
            "lon": 115.40,
            "tags": {"name": "Moved Garden", "tourism": "attraction"},
        }]
    }).encode("utf-8")

    class JsonResponse:
        headers = {"Content-Type": "application/json"}

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def read(self, _limit: int) -> bytes:
            return payload

    candidate = SimpleNamespace(
        source="openstreetmap",
        external_id="node/801234580",
        evidence="{}",
        lat=-8.52,
        lng=115.28,
    )
    with patch("app.discovery.urlopen", return_value=JsonResponse()):
        reason, evidence = revalidate_candidate_lifecycle(candidate)

    assert "좌표" in reason
    assert evidence["checks"][0]["active"] is False
    assert evidence["checks"][0]["coordinate"]["verified"] is False


def test_exact_wikidata_approval_revalidation_blocks_dissolved_entity() -> None:
    payload = json.dumps({
        "entities": {
            "Q991001": {
                "claims": {
                    "P625": [{"mainsnak": {"datavalue": {"value": {"latitude": -8.5, "longitude": 115.2}}}}],
                    "P576": [{"mainsnak": {"datavalue": {"value": {"time": "+2025-01-01T00:00:00Z"}}}}],
                }
            }
        }
    }).encode("utf-8")

    class JsonResponse:
        headers = {"Content-Type": "application/json"}

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def read(self, _limit: int) -> bytes:
            return payload

    candidate = SimpleNamespace(source="wikidata", external_id="Q991001", evidence="{}")
    with patch("app.discovery.urlopen", return_value=JsonResponse()):
        reason, evidence = revalidate_candidate_lifecycle(candidate)

    assert "P576" in reason
    assert evidence["checks"][0]["active"] is False


def test_live_inactive_candidate_is_rejected_then_reactivated_when_source_reopens() -> None:
    with TestClient(app) as client:
        admin = _login(client, "joohan92@naver.com", "admin-test-password")
        regions = client.get("/api/regions", headers=admin).json()
        ubud_id = next(item["id"] for item in regions if item["slug"] == "ubud")
        element = _osm_element(
            801_234_569,
            title="Lifecycle Reopen Garden",
            lat=-8.565,
            lng=115.345,
        )
        with patch("app.discovery.fetch_osm_elements", return_value=[element]):
            queued = client.post(
                "/api/admin/discovery/run", headers=admin,
                json={"region_id": ubud_id, "limit": 1},
            )
        _completed_run(client, admin, queued)
        candidates = client.get(
            "/api/admin/discovery/candidates", headers=admin,
            params={"region_id": ubud_id, "limit": 200},
        ).json()
        candidate = next(item for item in candidates if item["external_id"] == "node/801234569")
        places_before = client.get("/api/admin/summary", headers=admin).json()["place_count"]

        with patch(
            "app.discovery.revalidate_candidate_lifecycle",
            return_value=("status=inactive", {"checked_at": "2026-08-23T00:00:00+00:00", "checks": []}),
        ):
            blocked = client.post(
                f"/api/admin/discovery/candidates/{candidate['id']}/approve",
                headers=admin,
                json={"note": "승인 전 원문 재확인"},
            )
        assert blocked.status_code == 409
        rejected = client.get(
            "/api/admin/discovery/candidates", headers=admin,
            params={"status": "rejected", "region_id": ubud_id, "limit": 200},
        ).json()
        rejected_candidate = next(item for item in rejected if item["id"] == candidate["id"])
        assert rejected_candidate["decision_history"][-1]["action"] == "approval_blocked_inactive"
        assert client.get("/api/admin/summary", headers=admin).json()["place_count"] == places_before

        with patch("app.discovery.fetch_osm_elements", return_value=[element]):
            reopened_run = client.post(
                "/api/admin/discovery/run", headers=admin,
                json={"region_id": ubud_id, "limit": 1},
            )
        assert _completed_run(client, admin, reopened_run)["created_count"] == 1
        pending = client.get(
            "/api/admin/discovery/candidates", headers=admin,
            params={"status": "pending", "region_id": ubud_id, "limit": 200},
        ).json()
        reopened = next(item for item in pending if item["id"] == candidate["id"])
        assert reopened["decision_history"][-1]["action"] == "auto_reactivated"

        with patch("app.discovery.revalidate_candidate_lifecycle", side_effect=TimeoutError("provider timeout")):
            unavailable = client.post(
                f"/api/admin/discovery/candidates/{candidate['id']}/approve",
                headers=admin,
                json={"note": "원문 재조회"},
            )
        assert unavailable.status_code == 409
        still_pending = client.get(
            "/api/admin/discovery/candidates", headers=admin,
            params={"status": "pending", "region_id": ubud_id, "limit": 200},
        ).json()
        assert any(item["id"] == candidate["id"] for item in still_pending)


def test_distinct_nearby_candidates_are_not_dropped_by_coordinates_alone() -> None:
    with TestClient(app) as client:
        admin = _login(client, "joohan92@naver.com", "admin-test-password")
        regions = client.get("/api/regions", headers=admin).json()
        ubud_id = next(item["id"] for item in regions if item["slug"] == "ubud")
        first = _osm_element(801_234_570, title="North Courtyard Studio", lat=-8.57000, lng=115.35000)
        second = _osm_element(801_234_571, title="South Courtyard Gallery", lat=-8.57002, lng=115.35002)
        with patch("app.discovery.fetch_osm_elements", return_value=[first, second]):
            queued = client.post(
                "/api/admin/discovery/run", headers=admin,
                json={"region_id": ubud_id, "limit": 2},
            )
        completed = _completed_run(client, admin, queued)
        assert completed["created_count"] == 2
        candidates = client.get(
            "/api/admin/discovery/candidates", headers=admin,
            params={"region_id": ubud_id, "limit": 200},
        ).json()
        external_ids = {item["external_id"] for item in candidates}
        assert {"node/801234570", "node/801234571"}.issubset(external_ids)


def test_same_name_nearby_provider_objects_remain_separate_review_candidates() -> None:
    with TestClient(app) as client:
        admin = _login(client, "joohan92@naver.com", "admin-test-password")
        regions = client.get("/api/regions", headers=admin).json()
        ubud_id = next(item["id"] for item in regions if item["slug"] == "ubud")
        first = _osm_element(801_234_581, title="Kopi Branch", lat=-8.5600, lng=115.3300)
        second = _osm_element(801_234_582, title="Kopi Branch", lat=-8.5592, lng=115.3300)
        with patch("app.discovery.fetch_osm_elements", return_value=[first, second]):
            queued = client.post(
                "/api/admin/discovery/run", headers=admin,
                json={"region_id": ubud_id, "limit": 2},
            )
        completed = _completed_run(client, admin, queued)
        assert completed["created_count"] == 2
        candidates = client.get(
            "/api/admin/discovery/candidates", headers=admin,
            params={"region_id": ubud_id, "limit": 200},
        ).json()
        external_ids = {item["external_id"] for item in candidates}
        assert {"node/801234581", "node/801234582"}.issubset(external_ids)


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


def test_workflow_retry_requeues_the_same_failed_run() -> None:
    with TestClient(app):
        with SessionLocal() as db:
            region = db.query(Region).filter(Region.slug == "ubud").one()
            queued = create_discovery_run(db, region_id=region.id, limit=3)
            run = db.get(BatchRun, queued.run.id)
            job = db.get(DiscoveryJob, queued.run.id)
            assert run is not None and job is not None
            run.status = "failed"
            run.summary = "provider failed"
            run.finished_at = datetime.now(timezone.utc)
            job.active_slot = None
            db.commit()

            retried = prepare_discovery_retry(db, run.id)

            assert retried.run.id == run.id
            assert retried.run.status == "queued"
            assert retried.run.finished_at is None
            refreshed_job = db.get(DiscoveryJob, run.id)
            assert refreshed_job is not None
            assert refreshed_job.active_slot == "place_discovery"

            run = db.get(BatchRun, run.id)
            assert run is not None
            run.status = "failed"
            run.finished_at = datetime.now(timezone.utc)
            refreshed_job.active_slot = None
            db.commit()


def test_manual_workflow_dispatch_failure_is_finalized_and_releases_lease() -> None:
    with TestClient(app) as client:
        admin = _login(client, "joohan92@naver.com", "admin-test-password")
        regions = client.get("/api/regions", headers=admin).json()
        ubud_id = next(item["id"] for item in regions if item["slug"] == "ubud")
        with (
            patch("app.discovery_api.discovery_workflow_enabled", return_value=True),
            patch(
                "app.discovery_api.start_discovery_workflow",
                side_effect=RuntimeError("AWS unavailable"),
            ),
        ):
            response = client.post(
                "/api/admin/discovery/run",
                headers=admin,
                json={"region_id": ubud_id, "limit": 1},
            )

        assert response.status_code == 502
        with SessionLocal() as db:
            failed = (
                db.query(BatchRun)
                .filter(BatchRun.kind == "place_discovery", BatchRun.trigger == "manual")
                .order_by(BatchRun.id.desc())
                .first()
            )
            assert failed is not None
            assert failed.status == "failed"
            assert failed.finished_at is not None
            job = db.get(DiscoveryJob, failed.id)
            assert job is not None
            assert job.active_slot is None
