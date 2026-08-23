from __future__ import annotations

import json
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from app import agent_models, extended_models, itinerary_models, models  # noqa: F401
from app.agent_models import (
    AgentCheckpoint,
    AgentEvidence,
    AgentKnowledge,
    AgentProposal,
    AgentRun,
    AgentRunStep,
    AgentTask,
    AgentWorkItem,
)
from app.curation import (
    ACTIVE_SLOT,
    AgentBusyError,
    AgentLeaseLostError,
    _lifecycle_signals,
    create_agent_run,
    execute_agent_run,
    prepare_agent_retry,
)
from app.db import Base
from app.models import BatchRun, DiscoveryCandidate, Place, Region


@pytest.fixture()
def db() -> Session:
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    session = factory()
    try:
        yield session
    finally:
        session.close()
        Base.metadata.drop_all(engine)
        engine.dispose()


def _region(slug: str, *, sort_order: int = 0) -> Region:
    return Region(
        slug=slug,
        name_ko=slug.upper(),
        name_local=slug.title(),
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
        sort_order=sort_order,
    )


def _candidate(db: Session, region: Region, *, external_id: str, title: str) -> DiscoveryCandidate:
    discovery_run = BatchRun(
        kind="place_discovery",
        status="success",
        trigger="manual",
        scanned_count=1,
        updated_count=1,
        summary="fixture",
    )
    db.add(discovery_run)
    db.flush()
    row = DiscoveryCandidate(
        discovery_run_id=discovery_run.id,
        region_id=region.id,
        source="openstreetmap",
        external_id=external_id,
        source_url=f"https://www.openstreetmap.org/node/{external_id}",
        title=title,
        local_name=title,
        description="공개 지도에서 수집한 장소 후보",
        area=region.slug,
        category="see",
        lat=region.center_lat,
        lng=region.center_lng,
        confidence=0.66,
        evidence=json.dumps(
            {
                "osm_tags": {
                    "website": f"https://{region.slug}.example.test/visit",
                    "tourism": "attraction",
                }
            }
        ),
        tags="현지,산책",
        status="pending",
    )
    db.add(row)
    db.commit()
    return row


@pytest.mark.parametrize(
    ("tags", "expected_kind"),
    [
        ({"disused:amenity": "restaurant"}, "disused"),
        ({"abandoned": "yes"}, "abandoned"),
        ({"demolished:building": "yes"}, "demolished"),
        ({"razed:building": "yes"}, "razed"),
        ({"opening_hours": "closed"}, "closed"),
        ({"opening_hours": "Mo-Su off"}, "closed"),
        ({"status": "inactive"}, "closed"),
        ({"operational_status": "non_operational"}, "closed"),
        ({"removed:amenity": "restaurant"}, "removed"),
        ({"moved_to": "New address"}, "relocated"),
        ({"contact:relocated_to": "New branch"}, "relocated"),
    ],
)
def test_lifecycle_signal_vocabulary(tags: dict[str, str], expected_kind: str) -> None:
    signals = _lifecycle_signals({"extratags": tags})

    assert [signal["kind"] for signal in signals] == [expected_kind]


def test_negative_lifecycle_values_are_not_risk_signals() -> None:
    signals = _lifecycle_signals({
        "extratags": {
            "disused:amenity": "no",
            "abandoned": "false",
            "demolished:building": "0",
            "razed:building": "none",
            "opening_hours": "Mo-Su 06:00-18:00",
        }
    })

    assert signals == []


def test_nominatim_top_level_lifecycle_class_is_a_risk_signal() -> None:
    signals = _lifecycle_signals({"category": "disused", "type": "restaurant"})

    assert signals == [{
        "kind": "disused",
        "key": "nominatim:category",
        "value": "disused",
        "severity": "high",
    }]


def test_only_one_active_curation_run_is_allowed(db: Session) -> None:
    region = _region("ubud")
    db.add(region)
    db.commit()

    first = create_agent_run(db, region_id=region.id, mode="discovery")

    with pytest.raises(AgentBusyError) as exc_info:
        create_agent_run(db, region_id=region.id, mode="quality")

    assert exc_info.value.active_run_id == first.id
    assert db.get(AgentRun, first.id).status == "queued"


def test_failed_curation_run_can_be_requeued_for_workflow_retry(db: Session) -> None:
    region = _region("ubud")
    db.add(region)
    db.commit()
    run = create_agent_run(db, region_id=region.id, mode="quality")
    run.status = "failed"
    run.summary = "provider failed"
    run.active_slot = None
    db.commit()

    retried = prepare_agent_retry(db, run.id)

    assert retried.id == run.id
    assert retried.status == "queued"
    assert retried.active_slot == ACTIVE_SLOT
    assert retried.finished_at is None


def test_superseded_curation_worker_cannot_overwrite_new_attempt(db: Session) -> None:
    region = _region("lease-ubud")
    db.add(region)
    db.commit()
    run = create_agent_run(db, region_id=region.id, mode="discovery")
    discovery_result = SimpleNamespace(
        run=SimpleNamespace(id=901, status="success", summary="후보 없음"),
        created_count=0,
        duplicate_count=0,
        invalid_count=0,
    )

    def supersede_attempt(*_args, **_kwargs):
        row = db.get(AgentRun, run.id)
        row.started_at = datetime.now(timezone.utc)
        row.status = "running"
        row.active_slot = ACTIVE_SLOT
        db.commit()
        return 0, 0

    with (
        patch("app.curation.run_discovery", return_value=discovery_result),
        patch("app.curation._enrich_candidates", side_effect=supersede_attempt),
    ):
        with pytest.raises(AgentLeaseLostError):
            execute_agent_run(db, run.id)

    db.expire_all()
    current = db.get(AgentRun, run.id)
    assert current.status == "running"
    assert current.active_slot == ACTIVE_SLOT
    assert current.finished_at is None


def test_discovery_curation_cross_checks_sources_and_stays_in_selected_region(db: Session) -> None:
    ubud = _region("ubud")
    lombok = _region("lombok", sort_order=1)
    db.add_all([ubud, lombok])
    db.commit()
    selected = _candidate(db, ubud, external_id="101", title="Subak Walk")
    ignored = _candidate(db, lombok, external_id="202", title="Lombok Hill")
    run = create_agent_run(db, region_id=ubud.id, mode="discovery")
    discovery_result = SimpleNamespace(
        run=SimpleNamespace(id=91, status="success", summary="신규 OSM 후보 수집 완료"),
        created_count=0,
        duplicate_count=0,
        invalid_count=0,
    )

    def page_fetcher(url: str, _timeout: float) -> dict[str, str]:
        return {
            "url": url,
            "title": "Official visitor information",
            "description": "Walking route and access details published by the operator.",
        }

    with (
        patch("app.curation.run_discovery", return_value=discovery_result),
        patch("app.curation._groq_enrichment", return_value={}),
    ):
        completed = execute_agent_run(db, run.id, page_fetcher=page_fetcher)

    assert completed.status == "success"
    assert completed.active_slot is None
    assert json.loads(completed.metrics_json)["verified_candidates"] == 1
    proposals = db.query(AgentProposal).order_by(AgentProposal.id).all()
    assert [proposal.discovery_candidate_id for proposal in proposals] == [selected.id]
    assert proposals[0].action == "create"
    assert len(json.loads(proposals[0].source_urls_json)) == 2
    assert db.query(AgentEvidence).filter(AgentEvidence.region_id == ubud.id).count() == 2
    assert db.query(AgentEvidence).filter(AgentEvidence.region_id == lombok.id).count() == 0
    assert "curation" in json.loads(db.get(DiscoveryCandidate, selected.id).evidence)
    assert "curation" not in json.loads(db.get(DiscoveryCandidate, ignored.id).evidence)
    assert db.query(AgentRunStep).filter(AgentRunStep.run_id == run.id).count() >= 4
    knowledge = db.query(AgentKnowledge).filter(AgentKnowledge.region_id == ubud.id).one()
    assert knowledge.evidence_count == 1
    assert knowledge.version == 1


def test_discovery_curation_rejects_existing_inactive_candidate(db: Session) -> None:
    ubud = _region("inactive-ubud")
    db.add(ubud)
    db.commit()
    candidate = _candidate(db, ubud, external_id="303", title="Closed Museum")
    evidence = json.loads(candidate.evidence)
    evidence["osm_tags"]["opening_hours"] = "closed"
    candidate.evidence = json.dumps(evidence)
    db.commit()
    run = create_agent_run(db, region_id=ubud.id, mode="discovery")
    discovery_result = SimpleNamespace(
        run=SimpleNamespace(id=92, status="success", summary="신규 OSM 후보 수집 완료"),
        created_count=0,
        duplicate_count=0,
        invalid_count=0,
    )

    with patch("app.curation.run_discovery", return_value=discovery_result):
        completed = execute_agent_run(
            db,
            run.id,
            page_fetcher=lambda *_args: pytest.fail("inactive candidate must not fetch pages"),
        )

    assert completed.status == "partial"
    db.refresh(candidate)
    assert candidate.status == "rejected"
    assert "opening_hours=closed" in candidate.decision_note
    assert db.query(AgentProposal).count() == 0
    assert db.query(DiscoveryCandidate).filter(DiscoveryCandidate.status == "pending").count() == 0


def test_verification_prioritizes_lifecycle_checks_and_duplicate_merge_proposal(db: Session) -> None:
    region = _region("canggu")
    db.add(region)
    db.commit()
    shared = {
        "region_id": region.id,
        "category": "eat",
        "local_name": "Kedai Pantai",
        "description": "",
        "area": "Berawa",
        "duration_minutes": 60,
        "budget_level": 2,
        "best_time": "",
        "traveler_note": "",
        "tags": "카페",
        "source_url": "",
        "coordinate_source": "manual",
        "coordinate_crs": "WGS84",
    }
    first = Place(title="Beach Cafe", lat=-8.66000, lng=115.14000, **shared)
    second = Place(title="Beach Cafe", lat=-8.66005, lng=115.14005, **shared)
    db.add_all([first, second])
    db.commit()
    run = create_agent_run(db, region_id=region.id, mode="verification")

    def no_external_results(url: str, _params: dict[str, str], _timeout: float):
        return {} if "wikimedia.org" in url else []

    completed = execute_agent_run(db, run.id, json_fetcher=no_external_results)

    assert completed.status == "success"
    metrics = json.loads(completed.metrics_json)
    assert metrics["quality_tasks"] == 0
    assert metrics["lifecycle_tasks"] == 2
    assert metrics["lifecycle_checked"] == 2
    assert metrics["duplicate_proposals"] == 1
    merge = db.query(AgentProposal).filter(AgentProposal.action == "merge").one()
    assert {merge.place_id, merge.secondary_place_id} == {first.id, second.id}
    assert json.loads(merge.payload_json)["distance_m"] < 20
    tools = [
        row.tool
        for row in db.query(AgentRunStep)
        .filter(AgentRunStep.run_id == run.id)
        .order_by(AgentRunStep.sequence)
        .all()
    ]
    assert tools.index("lifecycle_status_check") < tools.index("duplicate_scan")
    assert "process_quality_backlog" not in tools


def test_verification_flags_osm_lifecycle_signals_without_mutating_place(db: Session) -> None:
    region = _region("seminyak")
    db.add(region)
    db.commit()
    place = Place(
        region_id=region.id,
        category="eat",
        title="Sunset Warung",
        local_name="Warung Sunset",
        description="오랫동안 여행자가 찾던 해변 식당입니다.",
        area="Seminyak",
        lat=-8.69,
        lng=115.16,
        duration_minutes=60,
        budget_level=2,
        best_time="저녁",
        traveler_note="예약 가능 여부를 확인하세요.",
        tags="식당,노을",
        source_url="https://www.openstreetmap.org/node/98765",
        coordinate_source="openstreetmap",
        coordinate_external_id="node/98765",
        coordinate_crs="WGS84",
    )
    db.add(place)
    db.commit()
    run = create_agent_run(db, region_id=region.id, mode="verification")
    calls: list[tuple[str, dict[str, str]]] = []

    def lifecycle_result(url: str, params: dict[str, str], _timeout: float):
        calls.append((url, params))
        assert url == "https://nominatim.openstreetmap.org/lookup"
        assert params["osm_ids"] == "N98765"
        return [{
            "osm_type": "node",
            "osm_id": 98765,
            "lat": "-8.6900",
            "lon": "115.1600",
            "display_name": "Warung Sunset, Seminyak, Bali",
            "namedetails": {"name": "Warung Sunset"},
            "category": "amenity",
            "type": "restaurant",
            "extratags": {
                "abandoned:amenity": "restaurant",
                "demolished:building": "no",
                "opening_hours": "closed",
                "relocated_to": "Jalan Baru 10",
            },
        }]

    completed = execute_agent_run(db, run.id, json_fetcher=lifecycle_result)

    assert completed.status == "success"
    assert len(calls) == 1
    metrics = json.loads(completed.metrics_json)
    assert metrics["lifecycle_checked"] == 1
    assert metrics["lifecycle_flagged"] == 1
    assert metrics["lifecycle_proposals"] == 1
    db.refresh(place)
    assert place.traveler_note == "예약 가능 여부를 확인하세요."
    assert place.tags == "식당,노을"
    proposal = db.query(AgentProposal).filter(AgentProposal.action == "update").one()
    payload = json.loads(proposal.payload_json)
    assert "⚠️ 운영 상태 재확인 필요" in payload["traveler_note"]
    assert "운영상태 재확인 필요" in payload["tags"]
    assert payload["lifecycle_review"]["automatic_removal"] is False
    assert payload["lifecycle_review"]["source_url"] == (
        "https://www.openstreetmap.org/node/98765"
    )
    kinds = {signal["kind"] for signal in payload["lifecycle_review"]["signals"]}
    assert kinds == {"abandoned", "closed", "relocated"}
    assert "demolished" not in kinds
    assert json.loads(proposal.source_urls_json) == [
        "https://www.openstreetmap.org/node/98765"
    ]
    task = db.query(AgentTask).filter(AgentTask.kind == "verification_lifecycle").one()
    assert task.status == "completed"
    assert task.retry_after is not None
    evidence = db.query(AgentEvidence).filter(
        AgentEvidence.source_type == "nominatim_openstreetmap_status"
    ).one()
    assert evidence.source_status == "risk_signal"
    checkpoint = db.query(AgentCheckpoint).one()
    assert checkpoint.outcome == "proposed"
    assert db.query(AgentWorkItem).one().stage == "review"

    task.retry_after = datetime(2000, 1, 1, tzinfo=timezone.utc)
    db.commit()
    review_pending_run = create_agent_run(db, region_id=region.id, mode="verification")

    def should_not_refetch(_url: str, _params: dict[str, str], _timeout: float):
        raise AssertionError("관리자 검토가 대기 중이면 같은 위험 신호를 다시 조회하면 안 됩니다")

    pending = execute_agent_run(db, review_pending_run.id, json_fetcher=should_not_refetch)

    assert json.loads(pending.metrics_json)["lifecycle_tasks"] == 0
    assert db.query(AgentProposal).count() == 1


def test_clear_lifecycle_check_records_evidence_and_obeys_cooldown(db: Session) -> None:
    region = _region("sanur")
    db.add(region)
    db.commit()
    place = Place(
        region_id=region.id,
        category="see",
        title="Sanur Garden",
        local_name="Taman Sanur",
        description="산책하기 좋은 공개 정원입니다.",
        area="Sanur",
        lat=-8.70,
        lng=115.26,
        duration_minutes=60,
        budget_level=0,
        best_time="아침",
        traveler_note="",
        tags="정원",
        source_url="https://www.openstreetmap.org/way/4567",
        coordinate_source="openstreetmap",
        coordinate_external_id="way/4567",
        coordinate_crs="WGS84",
    )
    db.add(place)
    db.commit()
    first_run = create_agent_run(db, region_id=region.id, mode="verification")

    def active_result(_url: str, params: dict[str, str], _timeout: float):
        assert params["osm_ids"] == "W4567"
        return [{
            "osm_type": "way",
            "osm_id": 4567,
            "lat": "-8.7000",
            "lon": "115.2600",
            "display_name": "Taman Sanur, Bali",
            "namedetails": {"name": "Taman Sanur"},
            "category": "leisure",
            "type": "garden",
            "extratags": {"opening_hours": "Mo-Su 06:00-18:00"},
        }]

    completed = execute_agent_run(db, first_run.id, json_fetcher=active_result)

    metrics = json.loads(completed.metrics_json)
    assert metrics["lifecycle_checked"] == 1
    assert metrics["lifecycle_flagged"] == 0
    assert db.query(AgentProposal).count() == 0
    evidence = db.query(AgentEvidence).one()
    assert evidence.source_status == "verified"
    assert "영업 중임을 보증하지 않습니다" in evidence.claim
    task = db.query(AgentTask).filter(AgentTask.kind == "verification_lifecycle").one()
    assert task.status == "completed"
    assert task.retry_after is not None
    checkpoint = db.query(AgentCheckpoint).one()
    assert checkpoint.outcome == "verified"
    assert "정기 재검증" in checkpoint.decision

    second_run = create_agent_run(db, region_id=region.id, mode="verification")

    def should_not_fetch(_url: str, _params: dict[str, str], _timeout: float):
        raise AssertionError("30일 cooldown 안에는 lifecycle 출처를 다시 조회하면 안 됩니다")

    cooled_down = execute_agent_run(db, second_run.id, json_fetcher=should_not_fetch)

    second_metrics = json.loads(cooled_down.metrics_json)
    assert second_metrics["lifecycle_tasks"] == 0
    assert second_metrics["lifecycle_checked"] == 0
    assert db.query(AgentEvidence).count() == 1
    assert db.query(AgentCheckpoint).count() == 1

    task.retry_after = datetime(2000, 1, 1, tzinfo=timezone.utc)
    db.commit()
    third_run = create_agent_run(db, region_id=region.id, mode="verification")

    reverified = execute_agent_run(db, third_run.id, json_fetcher=active_result)

    assert json.loads(reverified.metrics_json)["lifecycle_checked"] == 1
    assert db.query(AgentEvidence).count() == 2
    assert db.query(AgentCheckpoint).count() == 2
    db.refresh(task)
    assert task.attempts == 2
