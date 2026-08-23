from __future__ import annotations

from unittest.mock import AsyncMock, patch

from sqlalchemy import create_engine, event
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app import agent_models, extended_models, itinerary_models, models  # noqa: F401
from app.agent_models import AgentProposal
from app.config import settings
from app.db import Base
from app.models import ChatWork, DiscoveryCandidate, Place, Region, User
from app.search_service import ExternalSearchHit
from app.travel_chat import _register_candidates, answer_chat, message_dict


def _database():
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    return engine, sessionmaker(bind=engine, expire_on_commit=False)()


def _seed(db):
    user = User(email="traveler@example.test", display_name="Traveler", password_hash="unused")
    region = Region(
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
        summary="예술과 논 풍경",
        access_note="차량 이동",
        transport_mode="car",
        sort_order=1,
    )
    db.add_all([user, region])
    db.commit()
    return user, region


def test_chat_keeps_grounded_candidates_for_a_followup_registration_request() -> None:
    engine, db = _database()
    try:
        user, region = _seed(db)
        hit = ExternalSearchHit(
            key="osm-node-123",
            source="openstreetmap",
            title="Kopi Baru Ubud",
            display_name="Kopi Baru Ubud, Gianyar, Bali",
            lat=-8.512,
            lng=115.261,
            category="food",
            external_id="node/123",
            source_url="https://www.openstreetmap.org/node/123",
            coordinate_source="openstreetmap",
            confidence=0.78,
            sources=("openstreetmap", "wikidata"),
            external_ids=(("openstreetmap", "node/123"), ("wikidata", "Q123")),
            source_urls=(
                "https://www.openstreetmap.org/node/123",
                "https://www.wikidata.org/wiki/Q123",
            ),
            cross_checked=True,
            storage_allowed=True,
            license="ODbL 1.0 + CC0 1.0",
            attribution="OpenStreetMap · Wikidata",
        )
        lookup = AsyncMock(return_value=[hit])

        with patch("app.travel_chat.search_external_places", lookup):
            assistant, grounded, work_state = answer_chat(
                db,
                user=user,
                message="우붓의 새로운 카페를 검색해줘",
                region_id=region.id,
                selected_place_id=None,
            )

        assert grounded == []
        first = message_dict(assistant)
        assert first["model"] == "external-grounded"
        assert first["sources"] == list(hit.source_urls)
        assert first["candidates"][0]["title"] == hit.title
        assert first["candidates"][0]["cross_checked"] is True
        assert work_state["action"] == "research"
        assert db.query(Place).count() == 0
        assert db.query(AgentProposal).count() == 0

        registered, grounded, work_state = answer_chat(
            db,
            user=user,
            message="1번 등록해줘",
            region_id=region.id,
            selected_place_id=None,
        )

        assert grounded == []
        second = message_dict(registered)
        assert second["candidates"][0]["status"] == "proposed"
        assert second["candidates"][0]["proposal_id"] is not None
        assert "관리자 검토 대기" in second["content"]
        candidate = db.query(DiscoveryCandidate).one()
        assert candidate.source == "openstreetmap"
        assert candidate.external_id == "node/123"
        proposal = db.query(AgentProposal).one()
        assert proposal.discovery_candidate_id == candidate.id
        assert proposal.status == "pending"
        assert db.query(Place).count() == 0
        assert work_state["action"] == "review"
        assert db.query(ChatWork).count() == 1
    finally:
        db.close()
        Base.metadata.drop_all(engine)
        engine.dispose()


def test_regular_local_recommendation_does_not_trigger_external_research() -> None:
    engine, db = _database()
    try:
        user, region = _seed(db)
        place = Place(
            region_id=region.id,
            creator_id=user.id,
            category="nature",
            title="Ubud Rainy Day Museum",
            local_name="",
            description="실내 전시 공간",
            area="Ubud",
            lat=-8.51,
            lng=115.26,
            duration_minutes=90,
            budget_level=1,
            best_time="비 오는 오후",
            traveler_note="실내 관람",
            tags="비,실내",
            source_url="https://example.test/museum",
            coordinate_source="manual",
            coordinate_crs="WGS84",
        )
        db.add(place)
        db.commit()
        lookup = AsyncMock()

        with (
            patch("app.travel_chat.search_external_places", lookup),
            patch.object(settings, "groq_api_key", ""),
        ):
            assistant, grounded, work_state = answer_chat(
                db,
                user=user,
                message="비 오는 날 갈 곳 추천해줘",
                region_id=region.id,
                selected_place_id=None,
            )

        lookup.assert_not_awaited()
        assert grounded[0].id == place.id
        assert message_dict(assistant)["sources"] == [place.source_url]
        assert work_state == {}
    finally:
        db.close()
        Base.metadata.drop_all(engine)
        engine.dispose()


def test_chat_batch_prelocks_all_selected_rows_in_global_merge_order() -> None:
    engine, db = _database()
    try:
        user, first_region = _seed(db)
        second_region = Region(
            slug="south-lombok",
            name_ko="남부 롬복",
            name_local="South Lombok",
            island="Lombok",
            kind="island",
            center_lat=-8.85,
            center_lng=116.28,
            default_zoom=10,
            south=-9.0,
            west=116.0,
            north=-8.5,
            east=116.6,
            summary="해변과 서핑",
            access_note="차량 이동",
            transport_mode="car",
            sort_order=2,
        )
        db.add(second_region)
        db.flush()
        existing_places = [
            Place(
                region_id=first_region.id,
                category="food",
                title="Existing Ubud Cafe",
                local_name="",
                description="기존 장소",
                area="Ubud",
                lat=-8.512,
                lng=115.261,
            ),
            Place(
                region_id=second_region.id,
                category="beach",
                title="Existing Lombok Beach",
                local_name="",
                description="기존 장소",
                area="South Lombok",
                lat=-8.900,
                lng=116.300,
            ),
        ]
        work = ChatWork(user_id=user.id, status="active", action="research")
        db.add_all([*existing_places, work])
        db.commit()

        selected = [
            {
                "key": "osm-node-222",
                "region_id": second_region.id,
                "source": "openstreetmap",
                "external_id": "node/222",
                "title": "Existing Lombok Beach",
                "lat": -8.900,
                "lng": 116.300,
                "category": "beach",
                "confidence": 0.8,
                "source_urls": ["https://www.openstreetmap.org/node/222"],
                "storage_allowed": True,
            },
            {
                "key": "osm-node-111",
                "region_id": first_region.id,
                "source": "openstreetmap",
                "external_id": "node/111",
                "title": "Existing Ubud Cafe",
                "lat": -8.512,
                "lng": 115.261,
                "category": "food",
                "confidence": 0.8,
                "source_urls": ["https://www.openstreetmap.org/node/111"],
                "storage_allowed": True,
            },
        ]

        locked_entities: list[type] = []

        def record_lock(execute_state) -> None:
            statement = execute_state.statement
            if getattr(statement, "_for_update_arg", None) is None:
                return
            descriptions = getattr(statement, "column_descriptions", [])
            entity = descriptions[0].get("entity") if descriptions else None
            if entity is not None:
                locked_entities.append(entity)

        event.listen(db, "do_orm_execute", record_lock)
        try:
            output = _register_candidates(
                db,
                user=user,
                work=work,
                selected=selected,
            )
        finally:
            event.remove(db, "do_orm_execute", record_lock)

        assert locked_entities == [AgentProposal, DiscoveryCandidate, Region, Place]
        assert [item["status"] for item in output] == ["proposed", "proposed"]
        candidates = db.query(DiscoveryCandidate).order_by(DiscoveryCandidate.external_id).all()
        assert [row.status for row in candidates] == ["duplicate", "duplicate"]
        assert {row.duplicate_place_id for row in candidates} == {row.id for row in existing_places}
        assert db.query(Place).count() == 2
        assert db.query(AgentProposal).count() == 2
    finally:
        db.close()
        Base.metadata.drop_all(engine)
        engine.dispose()
