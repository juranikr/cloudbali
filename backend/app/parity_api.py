from __future__ import annotations

import json
import uuid
from datetime import datetime, timezone
from pathlib import PurePath
from typing import Literal
from urllib.parse import urlsplit

from fastapi import APIRouter, Depends, HTTPException, Query, Response, status
from pydantic import BaseModel, ConfigDict, EmailStr, Field, field_validator, model_validator
from sqlalchemy import func, or_
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.agent_models import (
    AgentEvidence,
    AgentKnowledge,
    AgentLesson,
    AgentProposal,
    AgentQualityGap,
    AgentTask,
    AgentWorkItem,
)
from app.auth import get_admin_user, get_current_user
from app.collaboration import (
    can_edit_place,
    ensure_place_contributor,
    is_admin,
    notify_users,
    place_participant_ids,
)
from app.config import settings
from app.db import get_db
from app.extended_models import (
    PlaceChangeEvent,
    PlaceChain,
    PlaceContributor,
    PlaceImage,
    PlaceInsight,
    PlaceNote,
    UserMessage,
)
from app.itinerary_models import TravelPlan, TravelPlanDay, TravelPlanItem, TravelPlanMember
from app.image_storage import configured_public_image_url
from app.models import (
    ChatMessage,
    DiscoveryCandidate,
    Favorite,
    Place,
    Region,
    TripStop,
    User,
)
from app.operations_api import record_place_change_event
from app.place_identity import distance_m, duplicate_matches


router = APIRouter(tags=["place-collaboration"])


# Every direct Place foreign key in app.agent_models. Keeping the field-level
# keys in the merge snapshot allows one proposal row to be restored correctly
# even when more than one of its three place references pointed at the source.
_AGENT_PLACE_REFERENCES = (
    (AgentTask, AgentTask.place_id, "agent_tasks_place"),
    (AgentWorkItem, AgentWorkItem.place_id, "agent_work_items_place"),
    (AgentEvidence, AgentEvidence.place_id, "agent_evidence_place"),
    (AgentKnowledge, AgentKnowledge.place_id, "agent_knowledge_place"),
    (AgentProposal, AgentProposal.place_id, "agent_proposals_place"),
    (AgentProposal, AgentProposal.secondary_place_id, "agent_proposals_secondary_place"),
    (AgentProposal, AgentProposal.result_place_id, "agent_proposals_result_place"),
    (AgentLesson, AgentLesson.place_id, "agent_lessons_place"),
)
_AGENT_QUALITY_GAP_KEY = "agent_quality_gaps_place"


def _datetime_snapshot(value: datetime | None) -> str | None:
    return value.isoformat() if value is not None else None


def _quality_gap_snapshot(row: AgentQualityGap) -> dict:
    """Return a JSON-safe, lossless snapshot for a deduplicated quality gap."""

    return {
        "id": row.id,
        "region_id": row.region_id,
        "place_id": row.place_id,
        "gap_kind": row.gap_kind,
        "status": row.status,
        "reason": row.reason,
        "evidence_refs_json": row.evidence_refs_json,
        "condition_fingerprint": row.condition_fingerprint,
        "attempt_count": row.attempt_count,
        "retry_after": _datetime_snapshot(row.retry_after),
        "resolved_at": _datetime_snapshot(row.resolved_at),
        "created_at": _datetime_snapshot(row.created_at),
        "updated_at": _datetime_snapshot(row.updated_at),
    }


def _parse_snapshot_datetime(value: object) -> datetime | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise ValueError("invalid datetime snapshot")
    return datetime.fromisoformat(value)


def _restored_quality_gap_values(snapshot: dict, *, source_id: int) -> dict:
    if int(snapshot["place_id"]) != source_id:
        raise ValueError("quality gap snapshot belongs to another place")
    return {
        "id": int(snapshot["id"]),
        "region_id": int(snapshot["region_id"]),
        "place_id": source_id,
        "gap_kind": str(snapshot["gap_kind"]),
        "status": str(snapshot["status"]),
        "reason": str(snapshot["reason"]),
        "evidence_refs_json": str(snapshot["evidence_refs_json"]),
        "condition_fingerprint": str(snapshot["condition_fingerprint"]),
        "attempt_count": int(snapshot["attempt_count"]),
        "retry_after": _parse_snapshot_datetime(snapshot.get("retry_after")),
        "resolved_at": _parse_snapshot_datetime(snapshot.get("resolved_at")),
        "created_at": _parse_snapshot_datetime(snapshot.get("created_at")),
        "updated_at": _parse_snapshot_datetime(snapshot.get("updated_at")),
    }


def _place(
    db: Session,
    place_id: int,
    *,
    active: bool = True,
    for_update: bool = False,
) -> Place:
    query = db.query(Place).filter(Place.id == place_id)
    if active:
        query = query.filter(Place.merged_into_id.is_(None))
    if for_update:
        query = query.populate_existing().with_for_update()
    row = query.first()
    if row is None:
        raise HTTPException(status_code=404, detail="장소를 찾을 수 없습니다")
    return row


def _require_place_editor(db: Session, place: Place, user: User) -> None:
    if not can_edit_place(db, place, user):
        raise HTTPException(status_code=403, detail="장소 소유자·공동 편집자·관리자만 변경할 수 있습니다")


def _https_url(value: str, *, allow_blank: bool = False) -> str:
    cleaned = value.strip()
    if allow_blank and not cleaned:
        return ""
    try:
        parsed = urlsplit(cleaned)
    except ValueError as exc:
        raise ValueError("HTTPS URL만 사용할 수 있습니다") from exc
    if parsed.scheme.lower() != "https" or not parsed.hostname or parsed.username:
        raise ValueError("HTTPS URL만 사용할 수 있습니다")
    return cleaned


class ContributorInvite(BaseModel):
    email: EmailStr
    role: Literal["editor"] = "editor"


class ContributorOut(BaseModel):
    id: int | None
    user_id: int
    email: str
    display_name: str
    role: str
    created_at: datetime | None


def _contributor_out(db: Session, row: PlaceContributor) -> ContributorOut:
    user = db.get(User, row.user_id)
    return ContributorOut(
        id=row.id,
        user_id=row.user_id,
        email=user.email if user else "",
        display_name=user.display_name if user else "탈퇴한 사용자",
        role=row.role,
        created_at=row.created_at,
    )


@router.get("/api/places/{place_id}/contributors", response_model=list[ContributorOut])
def list_contributors(
    place_id: int,
    db: Session = Depends(get_db),
    _: User = Depends(get_current_user),
) -> list[ContributorOut]:
    place = _place(db, place_id)
    output: list[ContributorOut] = []
    if place.creator_id is not None:
        owner = db.get(User, place.creator_id)
        if owner is not None:
            output.append(ContributorOut(
                id=None,
                user_id=owner.id,
                email=owner.email,
                display_name=owner.display_name,
                role="owner",
                created_at=place.created_at,
            ))
    rows = db.query(PlaceContributor).filter(
        PlaceContributor.place_id == place.id,
        PlaceContributor.user_id != place.creator_id,
    ).order_by(PlaceContributor.id).all()
    output.extend(_contributor_out(db, row) for row in rows)
    return output


@router.post(
    "/api/places/{place_id}/contributors",
    response_model=ContributorOut,
    status_code=status.HTTP_201_CREATED,
)
def add_contributor(
    place_id: int,
    body: ContributorInvite,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
) -> ContributorOut:
    place = _place(db, place_id, for_update=True)
    if place.creator_id != user.id and not is_admin(user):
        raise HTTPException(status_code=403, detail="장소 소유자 또는 관리자만 편집자를 초대할 수 있습니다")
    invited = db.query(User).filter(func.lower(User.email) == str(body.email).lower()).first()
    if invited is None:
        raise HTTPException(status_code=404, detail="해당 이메일의 사용자를 찾을 수 없습니다")
    if invited.id == place.creator_id:
        raise HTTPException(status_code=409, detail="장소 소유자는 이미 편집 권한이 있습니다")
    existing = db.query(PlaceContributor).filter(
        PlaceContributor.place_id == place.id,
        PlaceContributor.user_id == invited.id,
    ).first()
    if existing is not None:
        raise HTTPException(status_code=409, detail="이미 등록된 공동 편집자입니다")
    row = ensure_place_contributor(
        db,
        place_id=place.id,
        user_id=invited.id,
        added_by_id=user.id,
        role=body.role,
    )
    event = record_place_change_event(
        db,
        place_id=place.id,
        actor_id=user.id,
        event_type="contributor_added",
        summary=f"공동 편집자 {invited.display_name} 추가",
        metadata={"user_id": invited.id, "role": row.role},
    )
    notify_users(
        db,
        [invited.id],
        kind="place_collaboration",
        title=f"{place.title} 공동 편집 초대",
        body="이 장소를 함께 수정할 수 있게 되었습니다.",
        place_id=place.id,
        related_event_id=event.id,
    )
    db.commit()
    db.refresh(row)
    return _contributor_out(db, row)


@router.delete("/api/places/{place_id}/contributors/{user_id}", status_code=status.HTTP_204_NO_CONTENT)
def remove_contributor(
    place_id: int,
    user_id: int,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
) -> Response:
    place = _place(db, place_id, for_update=True)
    if user_id != user.id and place.creator_id != user.id and not is_admin(user):
        raise HTTPException(status_code=403, detail="장소 소유자 또는 관리자만 편집자를 해제할 수 있습니다")
    row = db.query(PlaceContributor).filter(
        PlaceContributor.place_id == place.id,
        PlaceContributor.user_id == user_id,
    ).first()
    if row is None:
        raise HTTPException(status_code=404, detail="공동 편집자를 찾을 수 없습니다")
    event = record_place_change_event(
        db,
        place_id=place.id,
        actor_id=user.id,
        event_type="contributor_removed",
        summary="공동 편집자 권한 해제",
        metadata={"user_id": user_id, "role": row.role},
    )
    db.delete(row)
    if user_id != user.id:
        notify_users(
            db,
            [user_id],
            kind="place_collaboration",
            title=f"{place.title} 편집 권한 변경",
            body="이 장소의 공동 편집 권한이 해제되었습니다.",
            place_id=place.id,
            related_event_id=event.id,
        )
    db.commit()
    return Response(status_code=204)


class ChainCreate(BaseModel):
    name_local: str = Field(min_length=1, max_length=180)
    name_ko: str = Field(default="", max_length=180)
    category: str = Field(default="other", min_length=1, max_length=30)
    aliases: list[str] = Field(default_factory=list, max_length=30)
    description: str = Field(default="", max_length=4000)

    @field_validator("name_local", "name_ko", "category", "description")
    @classmethod
    def clean_text(cls, value: str) -> str:
        return value.strip()

    @field_validator("aliases")
    @classmethod
    def clean_aliases(cls, value: list[str]) -> list[str]:
        return list(dict.fromkeys(item.strip()[:180] for item in value if item.strip()))


class ChainUpdate(BaseModel):
    name_local: str | None = Field(default=None, min_length=1, max_length=180)
    name_ko: str | None = Field(default=None, max_length=180)
    category: str | None = Field(default=None, min_length=1, max_length=30)
    aliases: list[str] | None = Field(default=None, max_length=30)
    description: str | None = Field(default=None, max_length=4000)

    @model_validator(mode="after")
    def require_change(self) -> "ChainUpdate":
        if not self.model_fields_set:
            raise ValueError("변경할 체인 정보가 필요합니다")
        if any(getattr(self, field) is None for field in self.model_fields_set):
            raise ValueError("체인 수정 값은 null일 수 없습니다")
        return self


class ChainOut(BaseModel):
    id: int
    name_local: str
    name_ko: str
    category: str
    aliases: list[str]
    description: str
    branch_count: int
    created_at: datetime
    updated_at: datetime


def _chain_out(db: Session, row: PlaceChain) -> ChainOut:
    try:
        aliases = json.loads(row.aliases_json or "[]")
    except (json.JSONDecodeError, TypeError):
        aliases = []
    return ChainOut(
        id=row.id,
        name_local=row.name_local,
        name_ko=row.name_ko,
        category=row.category,
        aliases=[str(item) for item in aliases] if isinstance(aliases, list) else [],
        description=row.description,
        branch_count=db.query(func.count(Place.id)).filter(
            Place.chain_id == row.id,
            Place.merged_into_id.is_(None),
        ).scalar() or 0,
        created_at=row.created_at,
        updated_at=row.updated_at,
    )


@router.get("/api/chains", response_model=list[ChainOut])
def list_chains(
    q: str = "",
    category: str = "",
    db: Session = Depends(get_db),
    _: User = Depends(get_current_user),
) -> list[ChainOut]:
    query = db.query(PlaceChain)
    if q.strip():
        value = f"%{q.strip()}%"
        query = query.filter(or_(PlaceChain.name_local.ilike(value), PlaceChain.name_ko.ilike(value)))
    if category:
        query = query.filter(PlaceChain.category == category)
    return [_chain_out(db, row) for row in query.order_by(PlaceChain.name_local, PlaceChain.id).all()]


@router.get("/api/chains/{chain_id}", response_model=ChainOut)
def get_chain(
    chain_id: int,
    db: Session = Depends(get_db),
    _: User = Depends(get_current_user),
) -> ChainOut:
    row = db.get(PlaceChain, chain_id)
    if row is None:
        raise HTTPException(status_code=404, detail="체인을 찾을 수 없습니다")
    return _chain_out(db, row)


class ChainBranchOut(BaseModel):
    place_id: int
    region_id: int
    region_name: str
    title: str
    local_name: str
    branch_name: str
    category: str
    lat: float
    lng: float


@router.get("/api/chains/{chain_id}/branches", response_model=list[ChainBranchOut])
def chain_branches(
    chain_id: int,
    db: Session = Depends(get_db),
    _: User = Depends(get_current_user),
) -> list[ChainBranchOut]:
    if db.get(PlaceChain, chain_id) is None:
        raise HTTPException(status_code=404, detail="체인을 찾을 수 없습니다")
    rows = db.query(Place, Region).join(Region, Region.id == Place.region_id).filter(
        Place.chain_id == chain_id,
        Place.merged_into_id.is_(None),
    ).order_by(Region.sort_order, Place.branch_name, Place.title, Place.id).all()
    return [ChainBranchOut(
        place_id=place.id,
        region_id=region.id,
        region_name=region.name_ko,
        title=place.title,
        local_name=place.local_name,
        branch_name=place.branch_name,
        category=place.category,
        lat=place.lat,
        lng=place.lng,
    ) for place, region in rows]


@router.post("/api/chains", response_model=ChainOut, status_code=status.HTTP_201_CREATED)
def create_chain(
    body: ChainCreate,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
) -> ChainOut:
    existing = db.query(PlaceChain).filter(
        func.lower(PlaceChain.name_local) == body.name_local.lower()
    ).first()
    if existing is not None:
        raise HTTPException(status_code=409, detail="같은 현지 이름의 체인이 이미 있습니다")
    row = PlaceChain(
        name_local=body.name_local,
        name_ko=body.name_ko,
        category=body.category,
        aliases_json=json.dumps(body.aliases, ensure_ascii=False),
        description=body.description,
        created_by_id=user.id,
    )
    db.add(row)
    db.commit()
    db.refresh(row)
    return _chain_out(db, row)


@router.patch("/api/chains/{chain_id}", response_model=ChainOut)
def update_chain(
    chain_id: int,
    body: ChainUpdate,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
) -> ChainOut:
    row = db.get(PlaceChain, chain_id)
    if row is None:
        raise HTTPException(status_code=404, detail="체인을 찾을 수 없습니다")
    if row.created_by_id != user.id and not is_admin(user):
        raise HTTPException(status_code=403, detail="체인 등록자 또는 관리자만 수정할 수 있습니다")
    values = body.model_dump(exclude_unset=True)
    if "name_local" in values:
        conflict = db.query(PlaceChain.id).filter(
            func.lower(PlaceChain.name_local) == values["name_local"].strip().lower(),
            PlaceChain.id != row.id,
        ).first()
        if conflict is not None:
            raise HTTPException(status_code=409, detail="같은 현지 이름의 체인이 이미 있습니다")
    if "aliases" in values:
        values["aliases_json"] = json.dumps(
            list(dict.fromkeys(item.strip()[:180] for item in values.pop("aliases") if item.strip())),
            ensure_ascii=False,
        )
    for key, value in values.items():
        setattr(row, key, value.strip() if isinstance(value, str) else value)
    try:
        db.commit()
    except IntegrityError as exc:
        db.rollback()
        raise HTTPException(status_code=409, detail="같은 현지 이름의 체인이 이미 있습니다") from exc
    return _chain_out(db, row)


class PlaceChainAssignment(BaseModel):
    chain_id: int = Field(gt=0)
    branch_name: str = Field(default="", max_length=120)


@router.put("/api/places/{place_id}/chain", response_model=ChainOut)
def assign_place_chain(
    place_id: int,
    body: PlaceChainAssignment,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
) -> ChainOut:
    place = _place(db, place_id, for_update=True)
    _require_place_editor(db, place, user)
    chain = db.get(PlaceChain, body.chain_id)
    if chain is None:
        raise HTTPException(status_code=404, detail="체인을 찾을 수 없습니다")
    before = {"chain_id": place.chain_id, "branch_name": place.branch_name}
    place.chain_id = chain.id
    place.branch_name = body.branch_name.strip()
    record_place_change_event(
        db,
        place_id=place.id,
        actor_id=user.id,
        event_type="chain_assigned",
        summary=f"{chain.name_local} 체인 지점으로 연결",
        metadata={"before": before, "after": {"chain_id": chain.id, "branch_name": place.branch_name}},
    )
    db.commit()
    return _chain_out(db, chain)


@router.delete("/api/places/{place_id}/chain", status_code=status.HTTP_204_NO_CONTENT)
def unassign_place_chain(
    place_id: int,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
) -> Response:
    place = _place(db, place_id, for_update=True)
    _require_place_editor(db, place, user)
    before = {"chain_id": place.chain_id, "branch_name": place.branch_name}
    place.chain_id = None
    place.branch_name = ""
    record_place_change_event(
        db,
        place_id=place.id,
        actor_id=user.id,
        event_type="chain_unassigned",
        summary="장소의 체인 연결 해제",
        metadata={"before": before, "after": {"chain_id": None, "branch_name": ""}},
    )
    db.commit()
    return Response(status_code=204)


class InsightCreate(BaseModel):
    kind: Literal["location", "history", "visit", "tip"]
    title: str = Field(min_length=1, max_length=200)
    content: str = Field(min_length=1, max_length=5000)
    year_label: str = Field(default="", max_length=50)
    source_url: str = Field(min_length=1, max_length=2000)
    source_title: str = Field(default="", max_length=300)
    confidence: float = Field(default=0.7, ge=0, le=1)
    sort_order: int | None = Field(default=None, ge=0, le=100000)

    @field_validator("source_url")
    @classmethod
    def source_must_be_https(cls, value: str) -> str:
        return _https_url(value)


class InsightUpdate(BaseModel):
    kind: Literal["location", "history", "visit", "tip"] | None = None
    title: str | None = Field(default=None, min_length=1, max_length=200)
    content: str | None = Field(default=None, min_length=1, max_length=5000)
    year_label: str | None = Field(default=None, max_length=50)
    source_url: str | None = Field(default=None, min_length=1, max_length=2000)
    source_title: str | None = Field(default=None, max_length=300)
    confidence: float | None = Field(default=None, ge=0, le=1)
    sort_order: int | None = Field(default=None, ge=0, le=100000)

    @field_validator("source_url")
    @classmethod
    def source_must_be_https(cls, value: str | None) -> str | None:
        return None if value is None else _https_url(value)

    @model_validator(mode="after")
    def require_change(self) -> "InsightUpdate":
        if not self.model_fields_set:
            raise ValueError("변경할 인사이트 정보가 필요합니다")
        if any(getattr(self, field) is None for field in self.model_fields_set):
            raise ValueError("인사이트 수정 값은 null일 수 없습니다")
        return self


class InsightOut(BaseModel):
    id: int
    place_id: int
    kind: str
    title: str
    content: str
    year_label: str
    source_url: str
    source_title: str
    confidence: float
    sort_order: int
    created_by_id: int | None
    verified_at: datetime | None
    can_edit: bool
    created_at: datetime
    updated_at: datetime


def _insight_out(db: Session, row: PlaceInsight, user: User) -> InsightOut:
    place = db.get(Place, row.place_id)
    return InsightOut(
        id=row.id,
        place_id=row.place_id,
        kind=row.kind,
        title=row.title,
        content=row.content,
        year_label=row.year_label,
        source_url=row.source_url,
        source_title=row.source_title,
        confidence=row.confidence,
        sort_order=row.sort_order,
        created_by_id=row.created_by_id,
        verified_at=row.verified_at,
        can_edit=bool(place and can_edit_place(db, place, user)),
        created_at=row.created_at,
        updated_at=row.updated_at,
    )


@router.get("/api/places/{place_id}/insights", response_model=list[InsightOut])
def list_insights(
    place_id: int,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
) -> list[InsightOut]:
    _place(db, place_id)
    rows = db.query(PlaceInsight).filter(PlaceInsight.place_id == place_id).order_by(
        PlaceInsight.kind, PlaceInsight.sort_order, PlaceInsight.id
    ).all()
    return [_insight_out(db, row, user) for row in rows]


@router.post(
    "/api/places/{place_id}/insights",
    response_model=InsightOut,
    status_code=status.HTTP_201_CREATED,
)
def create_insight(
    place_id: int,
    body: InsightCreate,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
) -> InsightOut:
    place = _place(db, place_id, for_update=True)
    _require_place_editor(db, place, user)
    values = body.model_dump()
    if values["sort_order"] is None:
        values["sort_order"] = (
            db.query(func.max(PlaceInsight.sort_order)).filter(PlaceInsight.place_id == place.id).scalar() or 0
        ) + 10
    row = PlaceInsight(
        place_id=place.id,
        created_by_id=user.id,
        verified_at=datetime.now(timezone.utc) if is_admin(user) else None,
        **values,
    )
    db.add(row)
    db.flush()
    record_place_change_event(
        db,
        place_id=place.id,
        actor_id=user.id,
        event_type="insight_added",
        summary=f"출처가 있는 {row.kind} 인사이트 추가",
        metadata={"insight_id": row.id, "source_url": row.source_url},
    )
    db.commit()
    return _insight_out(db, row, user)


@router.patch("/api/place-insights/{insight_id}", response_model=InsightOut)
def update_insight(
    insight_id: int,
    body: InsightUpdate,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
) -> InsightOut:
    initial = db.get(PlaceInsight, insight_id)
    if initial is None:
        raise HTTPException(status_code=404, detail="인사이트를 찾을 수 없습니다")
    place = _place(db, initial.place_id, for_update=True)
    row = db.query(PlaceInsight).filter(
        PlaceInsight.id == insight_id,
        PlaceInsight.place_id == place.id,
    ).populate_existing().with_for_update().first()
    if row is None:
        raise HTTPException(status_code=404, detail="인사이트를 찾을 수 없습니다")
    _require_place_editor(db, place, user)
    values = body.model_dump(exclude_unset=True)
    before = {key: getattr(row, key) for key in values}
    for key, value in values.items():
        setattr(row, key, value)
    if is_admin(user):
        row.verified_at = datetime.now(timezone.utc)
    record_place_change_event(
        db,
        place_id=place.id,
        actor_id=user.id,
        event_type="insight_updated",
        summary="장소 인사이트 수정",
        metadata={"insight_id": row.id, "before": before, "after": values},
    )
    db.commit()
    return _insight_out(db, row, user)


@router.delete("/api/place-insights/{insight_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_insight(
    insight_id: int,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
) -> Response:
    initial = db.get(PlaceInsight, insight_id)
    if initial is None:
        raise HTTPException(status_code=404, detail="인사이트를 찾을 수 없습니다")
    place = _place(db, initial.place_id, for_update=True)
    row = db.query(PlaceInsight).filter(
        PlaceInsight.id == insight_id,
        PlaceInsight.place_id == place.id,
    ).populate_existing().with_for_update().first()
    if row is None:
        raise HTTPException(status_code=404, detail="인사이트를 찾을 수 없습니다")
    _require_place_editor(db, place, user)
    record_place_change_event(
        db,
        place_id=place.id,
        actor_id=user.id,
        event_type="insight_deleted",
        summary="장소 인사이트 삭제",
        metadata={"insight_id": row.id, "source_url": row.source_url},
    )
    db.delete(row)
    db.commit()
    return Response(status_code=204)


class MessageOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    place_id: int | None
    related_event_id: int | None
    kind: str
    title: str
    body: str
    read_at: datetime | None
    created_at: datetime


def _message_out(row: UserMessage) -> MessageOut:
    return MessageOut.model_validate(row, from_attributes=True)


@router.get("/api/messages", response_model=list[MessageOut])
def list_messages(
    unread_only: bool = False,
    limit: int = Query(default=80, ge=1, le=200),
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
) -> list[MessageOut]:
    query = db.query(UserMessage).filter(UserMessage.user_id == user.id)
    if unread_only:
        query = query.filter(UserMessage.read_at.is_(None))
    rows = query.order_by(UserMessage.created_at.desc(), UserMessage.id.desc()).limit(limit).all()
    return [_message_out(row) for row in rows]


@router.get("/api/messages/unread-count")
def unread_message_count(
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
) -> dict[str, int]:
    count = db.query(func.count(UserMessage.id)).filter(
        UserMessage.user_id == user.id,
        UserMessage.read_at.is_(None),
    ).scalar() or 0
    return {"count": count}


@router.post("/api/messages/{message_id}/read", response_model=MessageOut)
def read_message(
    message_id: int,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
) -> MessageOut:
    row = db.query(UserMessage).filter(
        UserMessage.id == message_id,
        UserMessage.user_id == user.id,
    ).first()
    if row is None:
        raise HTTPException(status_code=404, detail="메시지를 찾을 수 없습니다")
    if row.read_at is None:
        row.read_at = datetime.now(timezone.utc)
        db.commit()
    return _message_out(row)


@router.post("/api/messages/read-all")
def read_all_messages(
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
) -> dict[str, int]:
    now = datetime.now(timezone.utc)
    count = db.query(UserMessage).filter(
        UserMessage.user_id == user.id,
        UserMessage.read_at.is_(None),
    ).update({UserMessage.read_at: now}, synchronize_session=False)
    db.commit()
    return {"updated": count}


class DuplicateOut(BaseModel):
    place_id: int
    title: str
    local_name: str
    region_id: int
    category: str
    distance_m: float
    confidence: float
    reason: str


@router.get("/api/places/duplicate-candidates", response_model=list[DuplicateOut])
def find_duplicate_candidates(
    title: str = Query(min_length=1, max_length=180),
    local_name: str = Query(default="", max_length=180),
    lat: float = Query(ge=-90, le=90),
    lng: float = Query(ge=-180, le=180),
    category: str = Query(default="", max_length=30),
    region_id: int | None = Query(default=None, gt=0),
    exclude_place_id: int | None = Query(default=None, gt=0),
    db: Session = Depends(get_db),
    _: User = Depends(get_current_user),
) -> list[DuplicateOut]:
    matches = duplicate_matches(
        db,
        title=title,
        local_name=local_name,
        lat=lat,
        lng=lng,
        category=category,
        region_id=region_id,
        exclude_place_ids={exclude_place_id} if exclude_place_id else set(),
    )
    return [DuplicateOut(
        place_id=match.place.id,
        title=match.place.title,
        local_name=match.place.local_name,
        region_id=match.place.region_id,
        category=match.place.category,
        distance_m=round(match.distance_m, 1),
        confidence=match.confidence,
        reason=match.reason,
    ) for match in matches]


class MergeRequest(BaseModel):
    target_place_id: int = Field(gt=0)
    note: str = Field(default="", max_length=2000)
    force: bool = False


class MergeOut(BaseModel):
    source_place_id: int
    target_place_id: int
    event_id: int
    moved_counts: dict[str, int]
    status: str


def _move_unique_rows(
    db: Session,
    model,
    *,
    source_id: int,
    target_id: int,
    user_field,
    snapshot_fields: tuple[str, ...],
) -> tuple[list[int], list[dict]]:
    moved_ids: list[int] = []
    deduplicated: list[dict] = []
    for row in db.query(model).filter(model.place_id == source_id).with_for_update().all():
        user_id = getattr(row, user_field.key)
        existing = db.query(model).filter(
            model.place_id == target_id,
            user_field == user_id,
        ).first()
        if existing is not None:
            deduplicated.append({field: getattr(row, field) for field in snapshot_fields})
            db.delete(row)
        else:
            row.place_id = target_id
            moved_ids.append(row.id)
    return moved_ids, deduplicated


def _lock_merge_references(db: Session, place_ids: set[int]) -> None:
    """Use the same Proposal -> Candidate -> Place lock order as agent approval."""

    ids = sorted(place_ids)
    db.query(AgentProposal).filter(or_(
        AgentProposal.place_id.in_(ids),
        AgentProposal.secondary_place_id.in_(ids),
        AgentProposal.result_place_id.in_(ids),
    )).order_by(AgentProposal.id).populate_existing().with_for_update().all()
    db.query(DiscoveryCandidate).filter(or_(
        DiscoveryCandidate.duplicate_place_id.in_(ids),
        DiscoveryCandidate.result_place_id.in_(ids),
    )).order_by(DiscoveryCandidate.id).populate_existing().with_for_update().all()


def _merge_places(
    source_place_id: int,
    body: MergeRequest,
    *,
    db: Session,
    admin: User,
    commit: bool,
) -> MergeOut:
    if source_place_id == body.target_place_id:
        raise HTTPException(status_code=422, detail="같은 장소끼리는 병합할 수 없습니다")
    _lock_merge_references(db, {source_place_id, body.target_place_id})
    places = db.query(Place).filter(
        Place.id.in_([source_place_id, body.target_place_id])
    ).order_by(Place.id).populate_existing().with_for_update().all()
    by_id = {place.id: place for place in places}
    source = by_id.get(source_place_id)
    target = by_id.get(body.target_place_id)
    if source is None or target is None:
        raise HTTPException(status_code=404, detail="병합할 장소를 찾을 수 없습니다")
    if source.merged_into_id is not None or target.merged_into_id is not None:
        raise HTTPException(status_code=409, detail="이미 병합된 장소는 다시 병합할 수 없습니다")

    detected = next((
        match for match in duplicate_matches(
            db,
            title=source.title,
            local_name=source.local_name,
            lat=source.lat,
            lng=source.lng,
            category=source.category,
            exclude_place_ids={source.id},
            limit=20,
        )
        if match.place.id == target.id
    ), None)
    if detected is None and not body.force:
        raise HTTPException(
            status_code=409,
            detail="자동 중복 판정 근거가 없습니다. 검토 메모와 force=true가 필요합니다",
        )
    if detected is None and len(body.note.strip()) < 5:
        raise HTTPException(status_code=422, detail="강제 병합 사유를 5자 이상 입력해 주세요")

    participants = place_participant_ids(db, source) | place_participant_ids(db, target)
    favorite_ids, favorite_deleted = _move_unique_rows(
        db,
        Favorite,
        source_id=source.id,
        target_id=target.id,
        user_field=Favorite.user_id,
        snapshot_fields=("user_id",),
    )
    trip_ids, trip_deleted = _move_unique_rows(
        db,
        TripStop,
        source_id=source.id,
        target_id=target.id,
        user_field=TripStop.user_id,
        snapshot_fields=("user_id", "day_number", "sort_order", "note"),
    )
    contributor_ids, contributor_deleted = _move_unique_rows(
        db,
        PlaceContributor,
        source_id=source.id,
        target_id=target.id,
        user_field=PlaceContributor.user_id,
        snapshot_fields=("user_id", "role", "added_by_id"),
    )

    moved: dict[str, list[int]] = {
        "favorites": favorite_ids,
        "trip_stops": trip_ids,
        "contributors": contributor_ids,
        "notes": [],
        "images": [],
        "insights": [],
        "plan_items": [],
        "candidate_duplicates": [],
        "candidate_results": [],
        **{key: [] for _, _, key in _AGENT_PLACE_REFERENCES},
        _AGENT_QUALITY_GAP_KEY: [],
    }
    for model, key in (
        (PlaceNote, "notes"),
        (PlaceImage, "images"),
        (PlaceInsight, "insights"),
    ):
        rows = db.query(model).filter(model.place_id == source.id).with_for_update().all()
        for row in rows:
            row.place_id = target.id
            moved[key].append(row.id)
    plan_items = db.query(TravelPlanItem).filter(
        TravelPlanItem.place_id == source.id
    ).with_for_update().all()
    for row in plan_items:
        row.place_id = target.id
        moved["plan_items"].append(row.id)
    duplicate_candidates = db.query(DiscoveryCandidate).filter(
        DiscoveryCandidate.duplicate_place_id == source.id
    ).with_for_update().all()
    for row in duplicate_candidates:
        row.duplicate_place_id = target.id
        moved["candidate_duplicates"].append(row.id)
    result_candidates = db.query(DiscoveryCandidate).filter(
        DiscoveryCandidate.result_place_id == source.id
    ).with_for_update().all()
    for row in result_candidates:
        row.result_place_id = target.id
        moved["candidate_results"].append(row.id)

    for model, field, key in _AGENT_PLACE_REFERENCES:
        rows = db.query(model).filter(field == source.id).with_for_update().all()
        for row in rows:
            setattr(row, field.key, target.id)
            moved[key].append(row.id)

    quality_gap_deleted: list[dict] = []
    source_gaps = db.query(AgentQualityGap).filter(
        AgentQualityGap.place_id == source.id
    ).with_for_update().all()
    for gap in source_gaps:
        target_gap = db.query(AgentQualityGap.id).filter(
            AgentQualityGap.place_id == target.id,
            AgentQualityGap.gap_kind == gap.gap_kind,
        ).with_for_update().first()
        if target_gap is None:
            gap.place_id = target.id
            moved[_AGENT_QUALITY_GAP_KEY].append(gap.id)
        else:
            quality_gap_deleted.append(_quality_gap_snapshot(gap))
            db.delete(gap)

    source.merged_into_id = target.id
    source.merged_by_id = admin.id
    source.merged_at = datetime.now(timezone.utc)
    snapshot = {
        "source_place_id": source.id,
        "target_place_id": target.id,
        "duplicate_evidence": {
            "confidence": detected.confidence,
            "reason": detected.reason,
            "distance_m": round(detected.distance_m, 1),
        } if detected is not None else {"forced": True},
        "moved": moved,
        "deduplicated": {
            "favorites": favorite_deleted,
            "trip_stops": trip_deleted,
            "contributors": contributor_deleted,
            "agent_quality_gaps": quality_gap_deleted,
        },
    }
    event = record_place_change_event(
        db,
        place_id=source.id,
        actor_id=admin.id,
        event_type="place_merged",
        summary=f"{source.title}을(를) {target.title}에 병합",
        metadata={"merge_snapshot": snapshot, "note": body.note.strip()},
    )
    notify_users(
        db,
        participants,
        kind="place_merged",
        title="중복 장소가 정리되었습니다",
        body=f"{source.title} 정보가 {target.title} 장소로 합쳐졌습니다.",
        place_id=target.id,
        related_event_id=event.id,
    )
    try:
        if commit:
            db.commit()
        else:
            db.flush()
    except IntegrityError as exc:
        db.rollback()
        raise HTTPException(status_code=409, detail="병합 중 데이터 충돌이 발생했습니다") from exc
    moved_counts = {key: len(value) for key, value in moved.items()}
    moved_counts.update({f"deduplicated_{key}": len(value) for key, value in snapshot["deduplicated"].items()})
    return MergeOut(
        source_place_id=source.id,
        target_place_id=target.id,
        event_id=event.id,
        moved_counts=moved_counts,
        status="merged",
    )


@router.post("/api/admin/places/{source_place_id}/merge", response_model=MergeOut)
def merge_places(
    source_place_id: int,
    body: MergeRequest,
    db: Session = Depends(get_db),
    admin: User = Depends(get_admin_user),
) -> MergeOut:
    return _merge_places(
        source_place_id,
        body,
        db=db,
        admin=admin,
        commit=True,
    )


@router.post("/api/admin/place-merges/{event_id}/undo", response_model=MergeOut)
def undo_place_merge(
    event_id: int,
    db: Session = Depends(get_db),
    admin: User = Depends(get_admin_user),
) -> MergeOut:
    event = db.query(PlaceChangeEvent).filter(
        PlaceChangeEvent.id == event_id,
        PlaceChangeEvent.event_type == "place_merged",
    ).populate_existing().with_for_update().first()
    if event is None:
        raise HTTPException(status_code=404, detail="병합 이력을 찾을 수 없습니다")
    if db.query(PlaceChangeEvent.id).filter(PlaceChangeEvent.rollback_of_event_id == event.id).first():
        raise HTTPException(status_code=409, detail="이미 취소된 병합입니다")
    try:
        metadata = json.loads(event.metadata_json or "{}")
        snapshot = metadata["merge_snapshot"]
        source_id = int(snapshot["source_place_id"])
        target_id = int(snapshot["target_place_id"])
        moved = snapshot["moved"]
        deduplicated = snapshot["deduplicated"]
    except (KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
        raise HTTPException(status_code=422, detail="복원 가능한 병합 스냅샷이 없습니다") from exc
    _lock_merge_references(db, {source_id, target_id})
    places = db.query(Place).filter(Place.id.in_([source_id, target_id])).order_by(
        Place.id
    ).populate_existing().with_for_update().all()
    by_id = {place.id: place for place in places}
    source, target = by_id.get(source_id), by_id.get(target_id)
    if source is None or target is None or source.merged_into_id != target.id:
        raise HTTPException(status_code=409, detail="현재 장소 상태가 병합 이력과 다릅니다")

    tracked_rows = (
        (Favorite, "favorites"),
        (TripStop, "trip_stops"),
        (PlaceContributor, "contributors"),
        (PlaceNote, "notes"),
        (PlaceImage, "images"),
        (PlaceInsight, "insights"),
    )
    for model, key in tracked_rows:
        ids = [int(value) for value in moved.get(key, [])]
        if ids and db.query(func.count(model.id)).filter(
            model.id.in_(ids), model.place_id == target.id
        ).scalar() != len(ids):
            raise HTTPException(
                status_code=409,
                detail=f"병합 후 {key} 데이터가 변경되어 자동 복원할 수 없습니다",
            )
    plan_item_ids = [int(value) for value in moved.get("plan_items", [])]
    if plan_item_ids and db.query(func.count(TravelPlanItem.id)).filter(
        TravelPlanItem.id.in_(plan_item_ids), TravelPlanItem.place_id == target.id
    ).scalar() != len(plan_item_ids):
        raise HTTPException(status_code=409, detail="병합 후 일정 데이터가 변경되어 자동 복원할 수 없습니다")
    for field, key in (
        (DiscoveryCandidate.duplicate_place_id, "candidate_duplicates"),
        (DiscoveryCandidate.result_place_id, "candidate_results"),
    ):
        ids = [int(value) for value in moved.get(key, [])]
        if ids and db.query(func.count(DiscoveryCandidate.id)).filter(
            DiscoveryCandidate.id.in_(ids), field == target.id
        ).scalar() != len(ids):
            raise HTTPException(status_code=409, detail="병합 후 발굴 후보가 변경되어 자동 복원할 수 없습니다")

    for model, field, key in _AGENT_PLACE_REFERENCES:
        ids = [int(value) for value in moved.get(key, [])]
        if ids and db.query(func.count(model.id)).filter(
            model.id.in_(ids), field == target.id
        ).scalar() != len(ids):
            raise HTTPException(
                status_code=409,
                detail=f"병합 후 {key} 에이전트 참조가 변경되어 자동 복원할 수 없습니다",
            )
    quality_gap_ids = [int(value) for value in moved.get(_AGENT_QUALITY_GAP_KEY, [])]
    if quality_gap_ids and db.query(func.count(AgentQualityGap.id)).filter(
        AgentQualityGap.id.in_(quality_gap_ids),
        AgentQualityGap.place_id == target.id,
    ).scalar() != len(quality_gap_ids):
        raise HTTPException(
            status_code=409,
            detail="병합 후 장소 품질 갭이 변경되어 자동 복원할 수 없습니다",
        )
    try:
        restored_quality_gaps = [
            _restored_quality_gap_values(value, source_id=source.id)
            for value in deduplicated.get("agent_quality_gaps", [])
        ]
    except (KeyError, TypeError, ValueError) as exc:
        raise HTTPException(status_code=422, detail="복원 가능한 품질 갭 스냅샷이 없습니다") from exc
    for values in restored_quality_gaps:
        if db.get(AgentQualityGap, values["id"]) is not None:
            raise HTTPException(status_code=409, detail="품질 갭 ID가 이미 사용되어 복원할 수 없습니다")
        if db.query(AgentQualityGap.id).filter(
            AgentQualityGap.place_id == source.id,
            AgentQualityGap.gap_kind == values["gap_kind"],
        ).first():
            raise HTTPException(status_code=409, detail="원본 장소에 같은 품질 갭이 생겨 복원할 수 없습니다")

    for model, key in tracked_rows:
        ids = [int(value) for value in moved.get(key, [])]
        if ids:
            db.query(model).filter(model.id.in_(ids), model.place_id == target.id).update(
                {model.place_id: source.id}, synchronize_session=False
            )
    if plan_item_ids:
        db.query(TravelPlanItem).filter(
            TravelPlanItem.id.in_(plan_item_ids),
            TravelPlanItem.place_id == target.id,
        ).update({TravelPlanItem.place_id: source.id}, synchronize_session=False)
    duplicate_ids = [int(value) for value in moved.get("candidate_duplicates", [])]
    if duplicate_ids:
        db.query(DiscoveryCandidate).filter(DiscoveryCandidate.id.in_(duplicate_ids)).update(
            {DiscoveryCandidate.duplicate_place_id: source.id}, synchronize_session=False
        )
    result_ids = [int(value) for value in moved.get("candidate_results", [])]
    if result_ids:
        db.query(DiscoveryCandidate).filter(DiscoveryCandidate.id.in_(result_ids)).update(
            {DiscoveryCandidate.result_place_id: source.id}, synchronize_session=False
        )
    for model, field, key in _AGENT_PLACE_REFERENCES:
        ids = [int(value) for value in moved.get(key, [])]
        if ids:
            db.query(model).filter(model.id.in_(ids), field == target.id).update(
                {field: source.id}, synchronize_session=False
            )
    if quality_gap_ids:
        db.query(AgentQualityGap).filter(
            AgentQualityGap.id.in_(quality_gap_ids),
            AgentQualityGap.place_id == target.id,
        ).update({AgentQualityGap.place_id: source.id}, synchronize_session=False)

    for values in deduplicated.get("favorites", []):
        if not db.query(Favorite.id).filter(
            Favorite.user_id == int(values["user_id"]), Favorite.place_id == source.id
        ).first():
            db.add(Favorite(user_id=int(values["user_id"]), place_id=source.id))
    for values in deduplicated.get("trip_stops", []):
        if not db.query(TripStop.id).filter(
            TripStop.user_id == int(values["user_id"]), TripStop.place_id == source.id
        ).first():
            db.add(TripStop(
                user_id=int(values["user_id"]),
                place_id=source.id,
                day_number=int(values.get("day_number") or 1),
                sort_order=int(values.get("sort_order") or 0),
                note=str(values.get("note") or ""),
            ))
    for values in deduplicated.get("contributors", []):
        ensure_place_contributor(
            db,
            place_id=source.id,
            user_id=int(values["user_id"]),
            role=str(values.get("role") or "editor"),
            added_by_id=values.get("added_by_id"),
        )
    for values in restored_quality_gaps:
        db.add(AgentQualityGap(**values))
    source.merged_into_id = None
    source.merged_by_id = None
    source.merged_at = None
    participants = place_participant_ids(db, source) | place_participant_ids(db, target)
    undo_event = record_place_change_event(
        db,
        place_id=source.id,
        actor_id=admin.id,
        event_type="merge_undone",
        summary=f"{source.title} 병합 취소",
        metadata={"source_event_id": event.id, "merge_snapshot": snapshot},
        rollback_of_event_id=event.id,
    )
    notify_users(
        db,
        participants,
        kind="merge_undone",
        title="장소 병합이 취소되었습니다",
        body=f"{source.title} 장소가 다시 분리되었습니다.",
        place_id=source.id,
        related_event_id=undo_event.id,
    )
    try:
        db.commit()
    except IntegrityError as exc:
        db.rollback()
        raise HTTPException(status_code=409, detail="병합을 안전하게 복원할 수 없습니다") from exc
    return MergeOut(
        source_place_id=source.id,
        target_place_id=target.id,
        event_id=undo_event.id,
        moved_counts={key: len(value) for key, value in moved.items()},
        status="restored",
    )


class AuditEventOut(BaseModel):
    id: int
    place_id: int | None
    place_title: str
    actor_id: int | None
    actor_name: str
    rollback_of_event_id: int | None
    event_type: str
    field_name: str
    old_value: str
    new_value: str
    summary: str
    metadata: dict
    created_at: datetime


def _audit_out(db: Session, row: PlaceChangeEvent) -> AuditEventOut:
    place = db.get(Place, row.place_id) if row.place_id else None
    actor = db.get(User, row.actor_id) if row.actor_id else None
    try:
        metadata = json.loads(row.metadata_json or "{}")
    except (json.JSONDecodeError, TypeError):
        metadata = {}
    if not isinstance(metadata, dict):
        metadata = {}
    deleted = metadata.get("deleted_place") if isinstance(metadata.get("deleted_place"), dict) else {}
    return AuditEventOut(
        id=row.id,
        place_id=row.place_id,
        place_title=place.title if place else str(deleted.get("title") or "삭제된 장소"),
        actor_id=row.actor_id,
        actor_name=actor.display_name if actor else "시스템",
        rollback_of_event_id=row.rollback_of_event_id,
        event_type=row.event_type,
        field_name=row.field_name,
        old_value=row.old_value,
        new_value=row.new_value,
        summary=row.summary,
        metadata=metadata,
        created_at=row.created_at,
    )


@router.get("/api/admin/place-events", response_model=list[AuditEventOut])
def global_place_events(
    place_id: int | None = Query(default=None, gt=0),
    event_type: str = Query(default="", max_length=40),
    actor_id: int | None = Query(default=None, gt=0),
    deleted_only: bool = False,
    limit: int = Query(default=100, ge=1, le=500),
    db: Session = Depends(get_db),
    _: User = Depends(get_admin_user),
) -> list[AuditEventOut]:
    query = db.query(PlaceChangeEvent)
    if place_id is not None:
        query = query.filter(PlaceChangeEvent.place_id == place_id)
    if event_type:
        query = query.filter(PlaceChangeEvent.event_type == event_type)
    if actor_id is not None:
        query = query.filter(PlaceChangeEvent.actor_id == actor_id)
    if deleted_only:
        query = query.filter(PlaceChangeEvent.event_type == "place_deleted")
    rows = query.order_by(PlaceChangeEvent.created_at.desc(), PlaceChangeEvent.id.desc()).limit(limit).all()
    return [_audit_out(db, row) for row in rows]


@router.get("/api/admin/place-events/{event_id}", response_model=AuditEventOut)
def global_place_event(
    event_id: int,
    db: Session = Depends(get_db),
    _: User = Depends(get_admin_user),
) -> AuditEventOut:
    row = db.get(PlaceChangeEvent, event_id)
    if row is None:
        raise HTTPException(status_code=404, detail="변경 이벤트를 찾을 수 없습니다")
    return _audit_out(db, row)


class TravelSignalOut(BaseModel):
    key: str
    label: str
    score: float
    evidence_count: int


class TravelAnchorOut(BaseModel):
    place_id: int
    title: str
    region: str
    lat: float
    lng: float
    sources: list[str]


class TravelRecommendationOut(BaseModel):
    place_id: int
    title: str
    category: str
    region: str
    score: float
    reason: str
    distance_km: float | None


class TravelProfileOut(BaseModel):
    user_id: int
    region_id: int | None
    signals: list[TravelSignalOut]
    anchors: list[TravelAnchorOut]
    recommendations: list[TravelRecommendationOut]
    category_scores: dict[str, float]
    region_scores: dict[str, float]
    evidence: dict[str, int]


def _add_profile_place(
    place: Place,
    weight: float,
    source: str,
    *,
    category_scores: dict[str, float],
    region_scores: dict[int, float],
    anchors: dict[int, dict],
) -> None:
    category_scores[place.category] = category_scores.get(place.category, 0.0) + weight
    region_scores[place.region_id] = region_scores.get(place.region_id, 0.0) + weight
    entry = anchors.setdefault(place.id, {"place": place, "score": 0.0, "sources": set()})
    entry["score"] += weight
    entry["sources"].add(source)


@router.get("/api/travel-profile", response_model=TravelProfileOut)
def travel_profile(
    region_id: int | None = Query(default=None, gt=0),
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
) -> TravelProfileOut:
    if region_id is not None and db.get(Region, region_id) is None:
        raise HTTPException(status_code=404, detail="권역을 찾을 수 없습니다")
    category_scores: dict[str, float] = {}
    region_scores: dict[int, float] = {}
    anchors: dict[int, dict] = {}
    evidence = {"favorites": 0, "trip_stops": 0, "plan_items": 0, "created_places": 0, "chat_messages": 0}

    favorites = db.query(Place).join(Favorite, Favorite.place_id == Place.id).filter(
        Favorite.user_id == user.id,
        Place.merged_into_id.is_(None),
    ).all()
    for place in favorites:
        _add_profile_place(place, 3.0, "favorite", category_scores=category_scores, region_scores=region_scores, anchors=anchors)
    evidence["favorites"] = len(favorites)
    trip_places = db.query(Place).join(TripStop, TripStop.place_id == Place.id).filter(
        TripStop.user_id == user.id,
        Place.merged_into_id.is_(None),
    ).all()
    for place in trip_places:
        _add_profile_place(place, 4.0, "quick_trip", category_scores=category_scores, region_scores=region_scores, anchors=anchors)
    evidence["trip_stops"] = len(trip_places)
    plan_places = db.query(Place).join(TravelPlanItem, TravelPlanItem.place_id == Place.id).join(
        TravelPlanDay, TravelPlanDay.id == TravelPlanItem.day_id
    ).join(TravelPlan, TravelPlan.id == TravelPlanDay.plan_id).outerjoin(
        TravelPlanMember,
        TravelPlanMember.plan_id == TravelPlan.id,
    ).filter(
        or_(TravelPlan.owner_id == user.id, TravelPlanMember.user_id == user.id),
        Place.merged_into_id.is_(None),
    ).distinct().all()
    for place in plan_places:
        _add_profile_place(place, 5.0, "dated_plan", category_scores=category_scores, region_scores=region_scores, anchors=anchors)
    evidence["plan_items"] = len(plan_places)
    created = db.query(Place).filter(
        Place.creator_id == user.id,
        Place.merged_into_id.is_(None),
    ).all()
    for place in created:
        _add_profile_place(place, 1.5, "created", category_scores=category_scores, region_scores=region_scores, anchors=anchors)
    evidence["created_places"] = len(created)
    chat_rows = db.query(ChatMessage).filter(
        ChatMessage.user_id == user.id,
        ChatMessage.role == "user",
    ).order_by(ChatMessage.id.desc()).limit(60).all()
    evidence["chat_messages"] = len(chat_rows)
    chat_text = " ".join(row.content.casefold() for row in chat_rows)
    known_categories = {row[0] for row in db.query(Place.category).distinct().all()}
    category_labels = {
        "beach": ("해변", "비치", "바다"), "surf": ("서핑",), "dive": ("다이빙", "스노클링"),
        "culture": ("문화", "사원", "역사"), "nature": ("자연", "폭포", "하이킹"),
        "food": ("음식", "맛집", "식당"), "cafe": ("카페",), "wellness": ("스파", "웰니스"),
        "nightlife": ("나이트", "클럽", "바 "), "transport": ("교통", "항구", "페리"),
    }
    for category in known_categories:
        hits = sum(chat_text.count(term) for term in category_labels.get(category, (category.casefold(),)))
        if hits:
            category_scores[category] = category_scores.get(category, 0.0) + min(6.0, hits * 1.5)

    regions = {row.id: row for row in db.query(Region).all()}
    ranked_anchors = sorted(anchors.values(), key=lambda value: (-value["score"], value["place"].id))[:8]
    anchor_out = [TravelAnchorOut(
        place_id=value["place"].id,
        title=value["place"].title,
        region=regions[value["place"].region_id].name_ko,
        lat=value["place"].lat,
        lng=value["place"].lng,
        sources=sorted(value["sources"]),
    ) for value in ranked_anchors]
    selected_ids = set(anchors)
    candidate_query = db.query(Place).filter(
        Place.merged_into_id.is_(None),
        ~Place.id.in_(selected_ids) if selected_ids else True,
    )
    if region_id is not None:
        candidate_query = candidate_query.filter(Place.region_id == region_id)
    recommendations: list[tuple[float, Place, str, float | None]] = []
    top_anchor = ranked_anchors[0]["place"] if ranked_anchors else None
    for place in candidate_query.all():
        category_score = category_scores.get(place.category, 0.0)
        region_score = region_scores.get(place.region_id, 0.0)
        score = category_score * 1.4 + region_score * 0.35
        if score <= 0:
            continue
        km = distance_m(top_anchor.lat, top_anchor.lng, place.lat, place.lng) / 1000 if top_anchor else None
        proximity = max(0.0, 3.0 - (km or 0) / 8) if top_anchor else 0.0
        score += proximity
        reason = f"선호 {place.category} 경향"
        if region_score:
            reason += f" · {regions[place.region_id].name_ko} 동선"
        recommendations.append((score, place, reason, km))
    recommendations.sort(key=lambda value: (-value[0], value[1].title, value[1].id))
    recommendation_out = [TravelRecommendationOut(
        place_id=place.id,
        title=place.title,
        category=place.category,
        region=regions[place.region_id].name_ko,
        score=round(score, 2),
        reason=reason,
        distance_km=round(km, 1) if km is not None else None,
    ) for score, place, reason, km in recommendations[:12]]
    signals = [TravelSignalOut(
        key=f"category:{category}",
        label=category,
        score=round(score, 2),
        evidence_count=sum(1 for value in anchors.values() if value["place"].category == category),
    ) for category, score in sorted(category_scores.items(), key=lambda item: (-item[1], item[0]))[:10]]
    return TravelProfileOut(
        user_id=user.id,
        region_id=region_id,
        signals=signals,
        anchors=anchor_out,
        recommendations=recommendation_out,
        category_scores={key: round(value, 2) for key, value in category_scores.items()},
        region_scores={regions[key].name_ko: round(value, 2) for key, value in region_scores.items()},
        evidence=evidence,
    )


ALLOWED_IMAGE_TYPES = {"image/jpeg": ".jpg", "image/png": ".png", "image/webp": ".webp"}


def _s3_client():
    try:
        import boto3
    except ImportError as exc:  # pragma: no cover - installed in production image
        raise RuntimeError("boto3가 설치되지 않았습니다") from exc
    return boto3.client("s3", region_name=settings.aws_region)


def _public_image_url(key: str) -> str:
    return configured_public_image_url(key)


class UploadPresignRequest(BaseModel):
    filename: str = Field(min_length=1, max_length=240)
    content_type: str = Field(max_length=100)
    size_bytes: int = Field(gt=0)

    @model_validator(mode="after")
    def validate_file(self) -> "UploadPresignRequest":
        if self.content_type.lower() not in ALLOWED_IMAGE_TYPES:
            raise ValueError("JPEG, PNG, WebP 이미지만 업로드할 수 있습니다")
        if self.size_bytes > settings.image_upload_max_bytes:
            raise ValueError("이미지 파일이 업로드 제한을 초과합니다")
        return self


class UploadPresignOut(BaseModel):
    upload_url: str
    method: str = "PUT"
    headers: dict[str, str]
    s3_key: str
    public_url: str
    expires_in: int


class UploadCompleteRequest(BaseModel):
    s3_key: str = Field(min_length=1, max_length=700)
    caption: str = Field(default="", max_length=300)
    source_url: str = Field(default="", max_length=2000)

    @field_validator("source_url")
    @classmethod
    def source_must_be_https(cls, value: str) -> str:
        return _https_url(value, allow_blank=True)


class UploadedImageOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    place_id: int
    image_url: str
    caption: str
    source_url: str
    sort_order: int
    created_at: datetime


@router.post("/api/places/{place_id}/images/presign", response_model=UploadPresignOut)
def presign_place_image(
    place_id: int,
    body: UploadPresignRequest,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
) -> UploadPresignOut:
    place = _place(db, place_id)
    _require_place_editor(db, place, user)
    if not settings.s3_bucket:
        raise HTTPException(status_code=503, detail="이미지 업로드 저장소가 설정되지 않았습니다")
    extension = ALLOWED_IMAGE_TYPES[body.content_type.lower()]
    key = f"places/{place.id}/uploads/{user.id}/{uuid.uuid4().hex}{extension}"
    expires_in = max(60, min(int(settings.image_presign_expire_seconds), 15 * 60))
    signed_metadata = {"declared-size": str(body.size_bytes), "uploader-id": str(user.id)}
    try:
        upload_url = _s3_client().generate_presigned_url(
            "put_object",
            Params={
                "Bucket": settings.s3_bucket,
                "Key": key,
                "ContentType": body.content_type.lower(),
                "ContentLength": body.size_bytes,
                "Metadata": signed_metadata,
            },
            ExpiresIn=expires_in,
        )
    except Exception as exc:
        raise HTTPException(status_code=502, detail="업로드 URL을 만들지 못했습니다") from exc
    return UploadPresignOut(
        upload_url=upload_url,
        headers={
            "Content-Type": body.content_type.lower(),
            "x-amz-meta-declared-size": signed_metadata["declared-size"],
            "x-amz-meta-uploader-id": signed_metadata["uploader-id"],
        },
        s3_key=key,
        public_url=_public_image_url(key),
        expires_in=expires_in,
    )


@router.post(
    "/api/places/{place_id}/images/complete",
    response_model=UploadedImageOut,
    status_code=status.HTTP_201_CREATED,
)
def complete_place_image(
    place_id: int,
    body: UploadCompleteRequest,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
) -> UploadedImageOut:
    # Serializing completion per place makes repeated completion calls
    # idempotent even when two application instances receive them together.
    place = _place(db, place_id, for_update=True)
    _require_place_editor(db, place, user)
    expected_prefix = f"places/{place.id}/uploads/{user.id}/"
    if not body.s3_key.startswith(expected_prefix) or ".." in PurePath(body.s3_key).parts:
        raise HTTPException(status_code=403, detail="이 업로드 키를 완료할 권한이 없습니다")
    if not settings.s3_bucket:
        raise HTTPException(status_code=503, detail="이미지 업로드 저장소가 설정되지 않았습니다")
    try:
        head = _s3_client().head_object(Bucket=settings.s3_bucket, Key=body.s3_key)
    except Exception as exc:
        raise HTTPException(status_code=409, detail="업로드된 파일을 확인할 수 없습니다") from exc
    content_type = str(head.get("ContentType") or "").lower()
    content_length = int(head.get("ContentLength") or 0)
    metadata = head.get("Metadata") if isinstance(head.get("Metadata"), dict) else {}
    if content_type not in ALLOWED_IMAGE_TYPES or not 0 < content_length <= settings.image_upload_max_bytes:
        raise HTTPException(status_code=422, detail="업로드된 이미지 형식 또는 크기가 올바르지 않습니다")
    try:
        declared_size = int(metadata.get("declared-size", ""))
    except (TypeError, ValueError):
        declared_size = -1
    if declared_size != content_length:
        raise HTTPException(status_code=422, detail="서명 요청과 업로드된 이미지 크기가 일치하지 않습니다")
    if str(metadata.get("uploader-id") or "") != str(user.id):
        raise HTTPException(status_code=403, detail="업로드 소유자가 일치하지 않습니다")
    image_url = _public_image_url(body.s3_key)
    existing = db.query(PlaceImage).filter(PlaceImage.image_url == image_url).first()
    if existing is not None:
        if existing.place_id != place.id or existing.user_id != user.id:
            raise HTTPException(status_code=409, detail="이미 완료 처리된 업로드입니다")
        return UploadedImageOut.model_validate(existing, from_attributes=True)
    sort_order = (
        db.query(func.max(PlaceImage.sort_order)).filter(PlaceImage.place_id == place.id).scalar() or 0
    ) + 10
    row = PlaceImage(
        place_id=place.id,
        user_id=user.id,
        image_url=image_url,
        caption=body.caption.strip(),
        source_url=body.source_url,
        sort_order=sort_order,
    )
    db.add(row)
    db.flush()
    event = record_place_change_event(
        db,
        place_id=place.id,
        actor_id=user.id,
        event_type="image_uploaded",
        summary="직접 업로드한 장소 이미지 추가",
        metadata={
            "image_id": row.id,
            "s3_key": body.s3_key,
            "s3_bucket": settings.s3_bucket,
            "public_url": image_url,
            "content_type": content_type,
            "content_length": content_length,
        },
    )
    db.commit()
    db.refresh(row)
    return UploadedImageOut.model_validate(row, from_attributes=True)
