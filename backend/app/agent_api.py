from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any, Literal
from urllib.parse import urlsplit

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Query, status
from pydantic import BaseModel, Field, ValidationError
from sqlalchemy import and_, func, or_
from sqlalchemy.orm import Session

from app.agent_models import (
    AgentEvidence,
    AgentKnowledge,
    AgentLesson,
    AgentMission,
    AgentProposal,
    AgentQualityGap,
    AgentRun,
    AgentRunStep,
    AgentTask,
    AgentWorkItem,
)
from app.auth import get_admin_user
from app.agent_knowledge import rebuild_agent_knowledge
from app.curation import AgentBusyError, create_agent_run, execute_agent_run, get_agent_run
from app.curation_workflow import curation_workflow_enabled, start_curation_workflow
from app.db import SessionLocal, get_db
from app.discovery import CandidateInactiveError, approve_candidate
from app.extended_models import PlaceChangeEvent, PlaceContributor, PlaceImage, PlaceInsight, PlaceNote
from app.itinerary_models import TravelPlanItem
from app.models import DiscoveryCandidate, Favorite, Place, TripStop, User
from app.operations_api import PlaceRollbackSnapshot, record_place_change_event, rollback_place_event


router = APIRouter(prefix="/api/admin/agent", tags=["admin-curation"])


def _loads(raw: str, fallback: Any) -> Any:
    try:
        value = json.loads(raw or "")
    except (json.JSONDecodeError, TypeError):
        return fallback
    return value


def _https_url(value: Any, *, allow_blank: bool = False) -> str:
    cleaned = str(value or "").strip()
    if allow_blank and not cleaned:
        return ""
    try:
        parsed = urlsplit(cleaned)
    except ValueError as exc:
        raise ValueError("HTTPS 출처만 사용할 수 있습니다") from exc
    if parsed.scheme.lower() != "https" or not parsed.hostname or parsed.username:
        raise ValueError("HTTPS 출처만 사용할 수 있습니다")
    return cleaned


class AgentRunRequest(BaseModel):
    region_id: int | None = Field(default=None, gt=0)
    mode: Literal["full", "discovery", "quality", "verification"] = "full"


class AgentRunOut(BaseModel):
    id: int
    region_id: int | None
    mode: str
    trigger: str
    status: str
    objective: str
    score: float
    metrics: dict[str, Any]
    summary: str
    started_at: datetime
    finished_at: datetime | None


def _run_out(row: AgentRun) -> AgentRunOut:
    metrics = _loads(row.metrics_json, {})
    return AgentRunOut(
        id=row.id,
        region_id=row.region_id,
        mode=row.mode,
        trigger=row.trigger,
        status=row.status,
        objective=row.objective,
        score=row.score,
        metrics=metrics if isinstance(metrics, dict) else {},
        summary=row.summary,
        started_at=row.started_at,
        finished_at=row.finished_at,
    )


class AgentStepOut(BaseModel):
    id: int
    sequence: int
    phase: str
    tool: str
    outcome: str
    score_delta: float
    detail: str
    metadata: dict[str, Any]
    created_at: datetime


def _step_out(row: AgentRunStep) -> AgentStepOut:
    metadata = _loads(row.metadata_json, {})
    return AgentStepOut(
        id=row.id,
        sequence=row.sequence,
        phase=row.phase,
        tool=row.tool,
        outcome=row.outcome,
        score_delta=row.score_delta,
        detail=row.detail,
        metadata=metadata if isinstance(metadata, dict) else {},
        created_at=row.created_at,
    )


class AgentProposalOut(BaseModel):
    id: int
    region_id: int
    run_id: int | None
    place_id: int | None
    secondary_place_id: int | None
    result_place_id: int | None
    discovery_candidate_id: int | None
    action: str
    title: str
    payload: dict[str, Any]
    evidence: str
    source_urls: list[str]
    confidence: float
    status: str
    decision_note: str
    created_at: datetime
    decided_at: datetime | None


def _proposal_out(row: AgentProposal) -> AgentProposalOut:
    payload = _loads(row.payload_json, {})
    source_urls = _loads(row.source_urls_json, [])
    return AgentProposalOut(
        id=row.id,
        region_id=row.region_id,
        run_id=row.run_id,
        place_id=row.place_id,
        secondary_place_id=row.secondary_place_id,
        result_place_id=row.result_place_id,
        discovery_candidate_id=row.discovery_candidate_id,
        action=row.action,
        title=row.title,
        payload=payload if isinstance(payload, dict) else {},
        evidence=row.evidence,
        source_urls=source_urls if isinstance(source_urls, list) else [],
        confidence=row.confidence,
        status=row.status,
        decision_note=row.decision_note,
        created_at=row.created_at,
        decided_at=row.decided_at,
    )


class ProposalDecision(BaseModel):
    note: str = Field(default="", max_length=2000)
    force: bool = False


def execute_queued_agent(run_id: int) -> None:
    with SessionLocal() as db:
        execute_agent_run(db, run_id)


def _fail_dispatch(db: Session, row: AgentRun) -> None:
    current = db.query(AgentRun).filter(AgentRun.id == row.id).with_for_update().first()
    if current is not None and current.status == "queued":
        current.status = "failed"
        current.summary = "운영 조사 실행 인프라에 작업을 전달하지 못했습니다"
        current.finished_at = datetime.now(timezone.utc)
        current.active_slot = None
        db.commit()


@router.post("/run", response_model=AgentRunOut, status_code=status.HTTP_202_ACCEPTED)
def run_agent(
    body: AgentRunRequest,
    background_tasks: BackgroundTasks,
    _: User = Depends(get_admin_user),
    db: Session = Depends(get_db),
) -> AgentRunOut:
    try:
        row = create_agent_run(db, region_id=body.region_id, mode=body.mode, trigger="manual")
    except AgentBusyError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    if curation_workflow_enabled():
        try:
            start_curation_workflow(run_id=row.id, trigger=row.trigger)
        except Exception as exc:
            _fail_dispatch(db, row)
            raise HTTPException(status_code=502, detail="운영 조사 작업을 시작하지 못했습니다") from exc
    else:
        background_tasks.add_task(execute_queued_agent, row.id)
    return _run_out(row)


@router.get("/runs", response_model=list[AgentRunOut])
def list_agent_runs(
    limit: int = Query(default=40, ge=1, le=200),
    _: User = Depends(get_admin_user),
    db: Session = Depends(get_db),
) -> list[AgentRunOut]:
    rows = db.query(AgentRun).order_by(AgentRun.id.desc()).limit(limit).all()
    return [_run_out(row) for row in rows]


class AgentStatusOut(BaseModel):
    is_running: bool
    active_run: AgentRunOut | None
    latest_run: AgentRunOut | None
    pending_proposals: int
    pending_tasks: int
    blocked_tasks: int


@router.get("/run/status", response_model=AgentStatusOut)
def agent_run_status(
    region_id: int | None = Query(default=None, gt=0),
    _: User = Depends(get_admin_user),
    db: Session = Depends(get_db),
) -> AgentStatusOut:
    """Compact polling status for the admin operations screen."""

    run_query = db.query(AgentRun)
    if region_id is not None:
        run_query = run_query.filter(AgentRun.region_id == region_id)
    latest = run_query.order_by(AgentRun.id.desc()).first()
    active = run_query.filter(AgentRun.status.in_(("queued", "running"))).order_by(
        AgentRun.id.desc()
    ).first()

    proposal_query = db.query(func.count(AgentProposal.id)).filter(AgentProposal.status == "pending")
    task_query = db.query(func.count(AgentTask.id))
    if region_id is not None:
        proposal_query = proposal_query.filter(AgentProposal.region_id == region_id)
        task_query = task_query.filter(AgentTask.region_id == region_id)
    pending_tasks = task_query.filter(AgentTask.status.in_(("pending", "ready"))).scalar() or 0
    blocked_query = db.query(func.count(AgentTask.id)).filter(AgentTask.status == "blocked")
    if region_id is not None:
        blocked_query = blocked_query.filter(AgentTask.region_id == region_id)
    return AgentStatusOut(
        is_running=active is not None,
        active_run=_run_out(active) if active is not None else None,
        latest_run=_run_out(latest) if latest is not None else None,
        pending_proposals=proposal_query.scalar() or 0,
        pending_tasks=pending_tasks,
        blocked_tasks=blocked_query.scalar() or 0,
    )


@router.get("/runs/{run_id}", response_model=AgentRunOut)
def agent_run(
    run_id: int,
    _: User = Depends(get_admin_user),
    db: Session = Depends(get_db),
) -> AgentRunOut:
    row = get_agent_run(db, run_id)
    if row is None:
        raise HTTPException(status_code=404, detail="운영 조사 실행을 찾을 수 없습니다")
    return _run_out(row)


@router.get("/runs/{run_id}/steps", response_model=list[AgentStepOut])
def agent_run_steps(
    run_id: int,
    _: User = Depends(get_admin_user),
    db: Session = Depends(get_db),
) -> list[AgentStepOut]:
    if db.get(AgentRun, run_id) is None:
        raise HTTPException(status_code=404, detail="운영 조사 실행을 찾을 수 없습니다")
    rows = db.query(AgentRunStep).filter(AgentRunStep.run_id == run_id).order_by(
        AgentRunStep.sequence
    ).all()
    return [_step_out(row) for row in rows]


@router.get("/tasks")
def agent_tasks(
    task_status: str | None = Query(default=None, alias="status", max_length=30),
    region_id: int | None = Query(default=None, gt=0),
    limit: int = Query(default=100, ge=1, le=300),
    _: User = Depends(get_admin_user),
    db: Session = Depends(get_db),
) -> list[dict[str, Any]]:
    query = db.query(AgentTask)
    if task_status:
        query = query.filter(AgentTask.status == task_status)
    if region_id is not None:
        query = query.filter(AgentTask.region_id == region_id)
    rows = query.order_by(AgentTask.priority.desc(), AgentTask.updated_at.desc()).limit(limit).all()
    return [{
        "id": row.id, "region_id": row.region_id, "place_id": row.place_id,
        "kind": row.kind, "title": row.title, "detail": row.detail,
        "success_metric": row.success_metric, "priority": row.priority,
        "status": row.status, "attempts": row.attempts, "result": row.result,
        "retry_after": row.retry_after, "created_at": row.created_at, "updated_at": row.updated_at,
    } for row in rows]


@router.get("/missions")
def agent_missions(
    mission_status: str | None = Query(default=None, alias="status", max_length=30),
    region_id: int | None = Query(default=None, gt=0),
    limit: int = Query(default=100, ge=1, le=300),
    _: User = Depends(get_admin_user),
    db: Session = Depends(get_db),
) -> list[dict[str, Any]]:
    query = db.query(AgentMission)
    if mission_status:
        query = query.filter(AgentMission.status == mission_status)
    if region_id is not None:
        query = query.filter(AgentMission.region_id == region_id)
    rows = query.order_by(AgentMission.priority.desc(), AgentMission.updated_at.desc()).limit(limit).all()
    return [{
        "id": row.id, "region_id": row.region_id, "task_id": row.task_id,
        "kind": row.kind, "title": row.title, "objective": row.objective,
        "success_metric": row.success_metric, "status": row.status, "priority": row.priority,
        "strategy": _loads(row.strategy_json, {}), "progress": _loads(row.progress_json, {}),
        "last_run_id": row.last_run_id, "created_at": row.created_at,
        "updated_at": row.updated_at, "completed_at": row.completed_at,
    } for row in rows]


@router.get("/missions/{mission_id}/work-items")
def mission_work_items(
    mission_id: int,
    _: User = Depends(get_admin_user),
    db: Session = Depends(get_db),
) -> list[dict[str, Any]]:
    if db.get(AgentMission, mission_id) is None:
        raise HTTPException(status_code=404, detail="운영 조사 미션을 찾을 수 없습니다")
    rows = db.query(AgentWorkItem).filter(AgentWorkItem.mission_id == mission_id).order_by(
        AgentWorkItem.priority.desc(), AgentWorkItem.id
    ).all()
    return [{
        "id": row.id, "region_id": row.region_id, "place_id": row.place_id,
        "target_type": row.target_type, "target_key": row.target_key, "title": row.title,
        "goal": row.goal, "definition_of_done": row.definition_of_done,
        "stage": row.stage, "status": row.status, "priority": row.priority,
        "state_summary": row.state_summary, "current_hypothesis": row.current_hypothesis,
        "next_action": _loads(row.next_action_json, {}), "blocked_reason": row.blocked_reason,
        "evidence_summary": row.evidence_summary, "attempts": row.attempts,
        "last_run_id": row.last_run_id, "updated_at": row.updated_at,
    } for row in rows]


@router.get("/evidence")
def agent_evidence(
    run_id: int | None = Query(default=None, gt=0),
    place_id: int | None = Query(default=None, gt=0),
    limit: int = Query(default=150, ge=1, le=500),
    _: User = Depends(get_admin_user),
    db: Session = Depends(get_db),
) -> list[dict[str, Any]]:
    query = db.query(AgentEvidence)
    if run_id is not None:
        query = query.filter(AgentEvidence.run_id == run_id)
    if place_id is not None:
        query = query.filter(AgentEvidence.place_id == place_id)
    rows = query.order_by(AgentEvidence.observed_at.desc(), AgentEvidence.id.desc()).limit(limit).all()
    return [{
        "id": row.id, "region_id": row.region_id, "run_id": row.run_id,
        "mission_id": row.mission_id, "work_item_id": row.work_item_id,
        "place_id": row.place_id, "source_type": row.source_type, "url": row.url,
        "title": row.title, "claim": row.claim, "excerpt": row.excerpt,
        "source_status": row.source_status, "rejection_reason": row.rejection_reason,
        "confidence": row.confidence, "observed_at": row.observed_at,
    } for row in rows]


@router.get("/knowledge")
def agent_knowledge(
    region_id: int | None = Query(default=None, gt=0),
    limit: int = Query(default=100, ge=1, le=300),
    _: User = Depends(get_admin_user),
    db: Session = Depends(get_db),
) -> list[dict[str, Any]]:
    query = db.query(AgentKnowledge).filter(AgentKnowledge.status == "active")
    if region_id is not None:
        query = query.filter(AgentKnowledge.region_id == region_id)
    rows = query.order_by(AgentKnowledge.updated_at.desc()).limit(limit).all()
    return [{
        "id": row.id, "topic": row.topic, "title": row.title, "content": _loads(row.content, row.content),
        "scope": row.scope, "region_id": row.region_id, "place_id": row.place_id,
        "category": row.category, "summary": row.summary,
        "principles": _loads(row.principles_json, []), "next_actions": _loads(row.next_actions_json, []),
        "source_refs": _loads(row.source_refs_json, []), "evidence_count": row.evidence_count,
        "quality_score": row.quality_score, "version": row.version, "updated_at": row.updated_at,
    } for row in rows]


@router.post("/knowledge/rebuild")
def rebuild_knowledge(
    _: User = Depends(get_admin_user),
    db: Session = Depends(get_db),
) -> dict[str, int]:
    """Refresh managed Bali playbooks without erasing learned evidence."""

    return rebuild_agent_knowledge(db)


class AgentActionOut(BaseModel):
    id: int
    place_id: int | None
    place_title: str
    action: str
    event_type: str
    summary: str
    proposal_id: int | None
    rolled_back: bool
    can_rollback: bool
    created_at: datetime


class AgentRollbackRequest(BaseModel):
    note: str = Field(default="", max_length=1000)


class AgentRollbackOut(BaseModel):
    ok: bool
    rollback_event_id: int
    message: str


def _event_metadata(row: PlaceChangeEvent) -> dict[str, Any]:
    value = _loads(row.metadata_json, {})
    return value if isinstance(value, dict) else {}


_AGENT_IMAGE_SNAPSHOT_FIELDS = (
    "place_id", "user_id", "image_url", "caption", "source_url", "sort_order",
)
_AGENT_INSIGHT_SNAPSHOT_FIELDS = (
    "place_id", "kind", "title", "content", "year_label", "source_url",
    "source_title", "confidence", "sort_order", "created_by_id",
)


def _row_snapshot(row: Any, fields: tuple[str, ...]) -> dict[str, Any]:
    return {field: getattr(row, field) for field in fields}


def _matches_creation_snapshot(row: Any, snapshot: Any, fields: tuple[str, ...]) -> bool:
    return isinstance(snapshot, dict) and set(snapshot) == set(fields) and all(
        getattr(row, field) == snapshot[field] for field in fields
    )


def _agent_action_name(row: PlaceChangeEvent, metadata: dict[str, Any]) -> str:
    if row.event_type == "agent_proposal_applied":
        return "update"
    if row.event_type == "agent_image_approved":
        return "image"
    if row.event_type == "agent_insight_approved":
        return "insight"
    if row.event_type == "place_created" and metadata.get("candidate_id"):
        return "create"
    if row.event_type == "place_merged" and metadata.get("agent_proposal_id"):
        return "merge"
    return row.event_type


def _has_user_place_dependencies(db: Session, place_id: int) -> bool:
    """Protect traveler-authored data from a create rollback."""

    user_models = (
        (Favorite, Favorite.place_id),
        (TripStop, TripStop.place_id),
        (TravelPlanItem, TravelPlanItem.place_id),
        (PlaceNote, PlaceNote.place_id),
        (PlaceImage, PlaceImage.place_id),
        (PlaceContributor, PlaceContributor.place_id),
        (PlaceInsight, PlaceInsight.place_id),
    )
    return any(db.query(model).filter(field == place_id).first() is not None for model, field in user_models)


def _has_unreverted_later_place_events(db: Session, event: PlaceChangeEvent) -> bool:
    later_ids = [
        event_id for (event_id,) in db.query(PlaceChangeEvent.id).filter(
            PlaceChangeEvent.place_id == event.place_id,
            PlaceChangeEvent.id > event.id,
            PlaceChangeEvent.event_type != "rollback",
        ).all()
    ]
    if not later_ids:
        return False
    reverted_ids = {
        event_id for (event_id,) in db.query(PlaceChangeEvent.rollback_of_event_id).filter(
            PlaceChangeEvent.rollback_of_event_id.in_(later_ids)
        ).all()
        if event_id is not None
    }
    return any(event_id not in reverted_ids for event_id in later_ids)


def _agent_created_place_is_pristine(
    db: Session,
    event: PlaceChangeEvent,
    metadata: dict[str, Any],
    place: Place,
) -> bool:
    candidate_id = int(metadata.get("candidate_id") or 0)
    candidate = db.get(DiscoveryCandidate, candidate_id) if candidate_id else None
    has_merged_children = db.query(Place.id).filter(Place.merged_into_id == place.id).first() is not None
    return (
        place.merged_into_id is None
        and not has_merged_children
        and candidate is not None
        and candidate.status == "approved"
        and candidate.result_place_id == place.id
        and not _has_user_place_dependencies(db, place.id)
        and not _has_unreverted_later_place_events(db, event)
    )


def _can_rollback_agent_event(
    db: Session,
    row: PlaceChangeEvent,
    metadata: dict[str, Any],
    *,
    rolled_back: bool,
) -> bool:
    if rolled_back or row.place_id is None:
        return False
    place = db.get(Place, row.place_id)
    if place is None or place.merged_into_id is not None:
        return False
    if row.event_type == "agent_proposal_applied":
        try:
            before = PlaceRollbackSnapshot.model_validate(metadata.get("before")).model_dump(exclude_unset=True)
            after = PlaceRollbackSnapshot.model_validate(metadata.get("after")).model_dump(exclude_unset=True)
        except ValidationError:
            return False
        if set(before) != set(after):
            return False
        if isinstance(after.get("tags"), list):
            after["tags"] = ",".join(dict.fromkeys(after["tags"]))
        return all(getattr(place, field) == value for field, value in after.items())
    if row.event_type == "agent_image_approved":
        image_id = int(metadata.get("image_id") or 0)
        image = db.get(PlaceImage, image_id) if image_id > 0 else None
        return image is not None and _matches_creation_snapshot(
            image, metadata.get("creation_snapshot"), _AGENT_IMAGE_SNAPSHOT_FIELDS
        )
    if row.event_type == "agent_insight_approved":
        insight_id = int(metadata.get("insight_id") or 0)
        insight = db.get(PlaceInsight, insight_id) if insight_id > 0 else None
        return insight is not None and _matches_creation_snapshot(
            insight, metadata.get("creation_snapshot"), _AGENT_INSIGHT_SNAPSHOT_FIELDS
        )
    if row.event_type == "place_created" and metadata.get("candidate_id"):
        return place is not None and _agent_created_place_is_pristine(db, row, metadata, place)
    if row.event_type == "place_merged" and metadata.get("agent_proposal_id"):
        snapshot = metadata.get("merge_snapshot")
        return (
            place is not None
            and place.merged_into_id is not None
            and isinstance(snapshot, dict)
            and bool(snapshot)
        )
    return False


@router.get("/actions", response_model=list[AgentActionOut])
def agent_actions(
    region_id: int | None = Query(default=None, gt=0),
    limit: int = Query(default=50, ge=1, le=200),
    _: User = Depends(get_admin_user),
    db: Session = Depends(get_db),
) -> list[AgentActionOut]:
    """List applied agent actions separately from general user audit events."""

    query = db.query(PlaceChangeEvent).filter(
        or_(
            PlaceChangeEvent.event_type.in_((
                "agent_proposal_applied",
                "agent_image_approved",
                "agent_insight_approved",
            )),
            and_(
                PlaceChangeEvent.event_type == "place_created",
                PlaceChangeEvent.metadata_json.like('%"candidate_id"%'),
            ),
            and_(
                PlaceChangeEvent.event_type == "place_merged",
                PlaceChangeEvent.metadata_json.like('%"agent_proposal_id"%'),
            ),
        )
    )
    if region_id is not None:
        query = query.join(Place, Place.id == PlaceChangeEvent.place_id).filter(
            Place.region_id == region_id
        )
    rows = query.order_by(PlaceChangeEvent.id.desc()).limit(limit).all()
    event_ids = [row.id for row in rows]
    rolled_back_ids = {
        int(value)
        for (value,) in db.query(PlaceChangeEvent.rollback_of_event_id).filter(
            PlaceChangeEvent.rollback_of_event_id.in_(event_ids)
        ).all()
        if value is not None
    } if event_ids else set()
    result: list[AgentActionOut] = []
    for row in rows:
        metadata = _event_metadata(row)
        place = db.get(Place, row.place_id) if row.place_id else None
        deleted = metadata.get("deleted_place") if isinstance(metadata.get("deleted_place"), dict) else {}
        proposal_id = int(metadata.get("agent_proposal_id") or 0) or None
        rolled_back = row.id in rolled_back_ids
        result.append(AgentActionOut(
            id=row.id,
            place_id=row.place_id,
            place_title=place.title if place is not None else str(deleted.get("title") or "삭제된 장소"),
            action=_agent_action_name(row, metadata),
            event_type=row.event_type,
            summary=row.summary,
            proposal_id=proposal_id,
            rolled_back=rolled_back,
            can_rollback=_can_rollback_agent_event(
                db, row, metadata, rolled_back=rolled_back
            ),
            created_at=row.created_at,
        ))
    return result


def _custom_rollback_event(
    db: Session,
    *,
    event: PlaceChangeEvent,
    admin: User,
    note: str,
    summary: str,
    metadata: dict[str, Any],
) -> PlaceChangeEvent:
    return record_place_change_event(
        db,
        place_id=event.place_id,
        actor_id=admin.id,
        event_type="rollback",
        summary=summary,
        metadata={"source_event_id": event.id, "admin_note": note.strip(), **metadata},
        rollback_of_event_id=event.id,
    )


@router.post("/actions/{event_id}/rollback", response_model=AgentRollbackOut)
def rollback_agent_action(
    event_id: int,
    body: AgentRollbackRequest,
    admin: User = Depends(get_admin_user),
    db: Session = Depends(get_db),
) -> AgentRollbackOut:
    event = db.query(PlaceChangeEvent).filter(
        PlaceChangeEvent.id == event_id
    ).populate_existing().with_for_update().first()
    if event is None:
        raise HTTPException(status_code=404, detail="운영 조사 조치 이력을 찾을 수 없습니다")
    metadata = _event_metadata(event)
    if _agent_action_name(event, metadata) == event.event_type:
        raise HTTPException(status_code=422, detail="운영 조사 조치로 확인되지 않는 이력입니다")
    rolled_back = db.query(PlaceChangeEvent.id).filter(
        PlaceChangeEvent.rollback_of_event_id == event.id
    ).first() is not None
    if not _can_rollback_agent_event(db, event, metadata, rolled_back=rolled_back):
        raise HTTPException(
            status_code=409,
            detail="이미 취소됐거나 여행자 데이터가 연결되어 안전하게 롤백할 수 없습니다",
        )

    if event.event_type == "agent_proposal_applied":
        rollback_out = rollback_place_event(event.id, db=db, admin=admin)
        rollback = db.get(PlaceChangeEvent, rollback_out.id)
        if rollback is None:  # pragma: no cover - rollback endpoint just committed it
            raise HTTPException(status_code=409, detail="롤백 이력을 확인하지 못했습니다")
        rollback_metadata = _event_metadata(rollback)
        if body.note.strip():
            rollback_metadata["admin_note"] = body.note.strip()
            rollback.metadata_json = json.dumps(
                rollback_metadata, ensure_ascii=False, separators=(",", ":")
            )
            db.commit()
        return AgentRollbackOut(
            ok=True,
            rollback_event_id=rollback_out.id,
            message=rollback_out.summary,
        )

    if event.event_type == "agent_image_approved":
        place = db.query(Place).filter(
            Place.id == event.place_id,
            Place.merged_into_id.is_(None),
        ).populate_existing().with_for_update().first()
        if place is None:
            raise HTTPException(status_code=409, detail="장소가 삭제 또는 병합되어 이미지를 롤백할 수 없습니다")
        image = db.query(PlaceImage).filter(
            PlaceImage.id == int(metadata["image_id"]),
            PlaceImage.place_id == place.id,
        ).populate_existing().with_for_update().first()
        if image is None:
            raise HTTPException(status_code=409, detail="이미 삭제된 이미지입니다")
        if not _matches_creation_snapshot(
            image, metadata.get("creation_snapshot"), _AGENT_IMAGE_SNAPSHOT_FIELDS
        ):
            raise HTTPException(status_code=409, detail="승인 후 이미지가 수정되어 자동 롤백할 수 없습니다")
        rollback = _custom_rollback_event(
            db,
            event=event,
            admin=admin,
            note=body.note,
            summary="운영 조사 이미지 승인을 취소했습니다",
            metadata={"deleted_image_id": image.id, "image_url": image.image_url},
        )
        db.delete(image)
    elif event.event_type == "agent_insight_approved":
        place = db.query(Place).filter(
            Place.id == event.place_id,
            Place.merged_into_id.is_(None),
        ).populate_existing().with_for_update().first()
        if place is None:
            raise HTTPException(status_code=409, detail="장소가 삭제 또는 병합되어 인사이트를 롤백할 수 없습니다")
        insight = db.query(PlaceInsight).filter(
            PlaceInsight.id == int(metadata["insight_id"]),
            PlaceInsight.place_id == place.id,
        ).populate_existing().with_for_update().first()
        if insight is None:
            raise HTTPException(status_code=409, detail="이미 삭제된 인사이트입니다")
        if not _matches_creation_snapshot(
            insight, metadata.get("creation_snapshot"), _AGENT_INSIGHT_SNAPSHOT_FIELDS
        ):
            raise HTTPException(status_code=409, detail="승인 후 인사이트가 수정되어 자동 롤백할 수 없습니다")
        rollback = _custom_rollback_event(
            db,
            event=event,
            admin=admin,
            note=body.note,
            summary="운영 조사 인사이트 승인을 취소했습니다",
            metadata={"deleted_insight_id": insight.id, "title": insight.title},
        )
        db.delete(insight)
    elif event.event_type == "place_created":
        candidate = db.query(DiscoveryCandidate).filter(
            DiscoveryCandidate.id == int(metadata["candidate_id"])
        ).populate_existing().with_for_update().first()
        if candidate is None:
            raise HTTPException(status_code=409, detail="연결된 신규 장소 후보가 없어 롤백할 수 없습니다")
        place = db.query(Place).filter(Place.id == event.place_id).populate_existing().with_for_update().first()
        if place is None:
            raise HTTPException(status_code=409, detail="이미 삭제된 장소입니다")
        if not _agent_created_place_is_pristine(db, event, metadata, place):
            raise HTTPException(
                status_code=409,
                detail="후속 변경·병합 또는 여행자 데이터가 있어 먼저 역순으로 취소해야 합니다",
            )
        rollback = _custom_rollback_event(
            db,
            event=event,
            admin=admin,
            note=body.note,
            summary="운영 조사 신규 장소 승인을 취소했습니다",
            metadata={
                "deleted_place": {
                    "id": place.id,
                    "region_id": place.region_id,
                    "title": place.title,
                    "local_name": place.local_name,
                    "lat": place.lat,
                    "lng": place.lng,
                }
            },
        )
        if candidate.result_place_id == place.id:
            candidate.result_place_id = None
            candidate.status = "rejected"
            candidate.decision_note = (
                (candidate.decision_note + " · ") if candidate.decision_note else ""
            ) + "관리자가 장소 생성을 롤백했습니다"
            candidate.decided_by_id = admin.id
            candidate.decided_at = datetime.now(timezone.utc)
        db.delete(place)
    elif event.event_type == "place_merged":
        from app.parity_api import undo_place_merge

        result = undo_place_merge(event.id, db=db, admin=admin)
        return AgentRollbackOut(
            ok=True,
            rollback_event_id=result.event_id,
            message="운영 조사 병합을 취소했습니다",
        )
    else:  # pragma: no cover - guarded by _can_rollback_agent_event
        raise HTTPException(status_code=422, detail="지원하지 않는 롤백 유형입니다")

    try:
        db.commit()
    except Exception as exc:
        db.rollback()
        raise HTTPException(status_code=409, detail="운영 조사 조치를 안전하게 취소하지 못했습니다") from exc
    db.refresh(rollback)
    return AgentRollbackOut(ok=True, rollback_event_id=rollback.id, message=rollback.summary)


@router.get("/quality-gaps")
def quality_gaps(
    gap_status: str | None = Query(default=None, alias="status", max_length=30),
    limit: int = Query(default=100, ge=1, le=300),
    _: User = Depends(get_admin_user),
    db: Session = Depends(get_db),
) -> list[dict[str, Any]]:
    query = db.query(AgentQualityGap)
    if gap_status:
        query = query.filter(AgentQualityGap.status == gap_status)
    rows = query.order_by(AgentQualityGap.updated_at.desc()).limit(limit).all()
    return [{
        "id": row.id, "region_id": row.region_id, "place_id": row.place_id,
        "gap_kind": row.gap_kind, "status": row.status, "reason": row.reason,
        "attempt_count": row.attempt_count, "retry_after": row.retry_after,
        "resolved_at": row.resolved_at, "updated_at": row.updated_at,
    } for row in rows]


@router.get("/lessons")
def agent_lessons(
    lesson_status: str | None = Query(default=None, alias="status", max_length=30),
    limit: int = Query(default=100, ge=1, le=300),
    _: User = Depends(get_admin_user),
    db: Session = Depends(get_db),
) -> list[dict[str, Any]]:
    query = db.query(AgentLesson)
    if lesson_status:
        query = query.filter(AgentLesson.status == lesson_status)
    rows = query.order_by(AgentLesson.updated_at.desc()).limit(limit).all()
    return [{
        "id": row.id, "lesson_key": row.lesson_key, "scope": row.scope,
        "region_id": row.region_id, "place_id": row.place_id, "category": row.category,
        "trigger": row.trigger, "action": row.action, "expected_effect": row.expected_effect,
        "status": row.status, "confidence": row.confidence,
        "observation_count": row.observation_count, "success_count": row.success_count,
        "failure_count": row.failure_count, "updated_at": row.updated_at,
    } for row in rows]


@router.get("/proposals", response_model=list[AgentProposalOut])
def agent_proposals(
    proposal_status: str | None = Query(default="pending", alias="status", max_length=30),
    region_id: int | None = Query(default=None, gt=0),
    limit: int = Query(default=150, ge=1, le=500),
    _: User = Depends(get_admin_user),
    db: Session = Depends(get_db),
) -> list[AgentProposalOut]:
    query = db.query(AgentProposal)
    if proposal_status:
        query = query.filter(AgentProposal.status == proposal_status)
    if region_id is not None:
        query = query.filter(AgentProposal.region_id == region_id)
    rows = query.order_by(AgentProposal.confidence.desc(), AgentProposal.id.desc()).limit(limit).all()
    return [_proposal_out(row) for row in rows]


_PLACE_UPDATE_FIELDS = {
    "title", "local_name", "description", "area", "category", "best_time",
    "traveler_note", "source_url", "coordinate_source", "coordinate_external_id",
    "coordinate_confidence",
}


def _validate_place_payload(payload: dict[str, Any]) -> None:
    if payload.get("source_url"):
        _https_url(payload["source_url"])
    if payload.get("coordinate_confidence") is not None:
        float(payload["coordinate_confidence"])


def _apply_place_payload(db: Session, place: Place, payload: dict[str, Any], admin: User) -> None:
    before: dict[str, Any] = {}
    after: dict[str, Any] = {}
    for key in _PLACE_UPDATE_FIELDS:
        if key not in payload or payload[key] is None:
            continue
        value = payload[key]
        if key == "source_url":
            value = _https_url(value, allow_blank=True)
        if isinstance(value, str):
            value = value.strip()
            if not value:
                continue
        if key == "coordinate_confidence":
            value = max(0.0, min(float(value), 1.0))
        current = getattr(place, key)
        if current != value:
            before[key] = current
            after[key] = value
            setattr(place, key, value)
    if "tags" in payload:
        value = payload["tags"]
        tags = ",".join(str(item).strip() for item in value if str(item).strip()) if isinstance(value, list) else str(value).strip()
        if tags and tags != place.tags:
            before["tags"] = place.tags
            after["tags"] = tags
            place.tags = tags
    if any(key.startswith("coordinate_") for key in after):
        place.coordinate_verified_at = datetime.now(timezone.utc)
    if after:
        record_place_change_event(
            db,
            place_id=place.id,
            actor_id=admin.id,
            event_type="agent_proposal_applied",
            summary="관리자가 출처 기반 운영 조사 제안을 승인했습니다",
            metadata={"before": before, "after": after},
        )


def _approve_create(db: Session, proposal: AgentProposal, payload: dict[str, Any], admin: User, decision: ProposalDecision) -> int:
    if proposal.discovery_candidate_id is None:
        raise HTTPException(status_code=422, detail="연결된 신규 장소 후보가 없습니다")
    try:
        _validate_place_payload(payload)
    except (TypeError, ValueError) as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    candidate = db.query(DiscoveryCandidate).filter(
        DiscoveryCandidate.id == proposal.discovery_candidate_id
    ).populate_existing().with_for_update().first()
    if candidate is None:
        raise HTTPException(status_code=404, detail="연결된 신규 장소 후보를 찾을 수 없습니다")
    if candidate.status == "approved" and candidate.result_place_id is not None:
        result_place_id = candidate.result_place_id
    else:
        try:
            result = approve_candidate(
                db,
                proposal.discovery_candidate_id,
                admin,
                note=decision.note.strip() or "운영 조사 제안 승인",
                force=decision.force,
                commit=False,
            )
        except LookupError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except CandidateInactiveError as exc:
            proposal.status = "rejected"
            proposal.decision_note = str(exc)
            proposal.decided_by_id = admin.id
            proposal.decided_at = datetime.now(timezone.utc)
            db.commit()
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        if result.result_place_id is None:
            payload["requires_force"] = True
            payload["duplicate_place_id"] = result.duplicate_place_id
            proposal.payload_json = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
            proposal.evidence = (
                proposal.evidence.rstrip() + " 기존 장소와 중복 가능성이 새로 확인되어 강제 등록 검토가 필요합니다."
            ).strip()
            db.commit()
            raise HTTPException(status_code=409, detail="기존 장소와 중복 가능성이 있어 강제 승인 여부를 검토해 주세요")
        result_place_id = result.result_place_id
    place = db.query(Place).filter(
        Place.id == result_place_id,
        Place.merged_into_id.is_(None),
    ).populate_existing().with_for_update().first()
    if place is None:
        raise HTTPException(status_code=409, detail="승인된 장소가 삭제 또는 병합되어 제안을 적용할 수 없습니다")
    _apply_place_payload(db, place, payload, admin)
    return place.id


def _approve_update(db: Session, proposal: AgentProposal, payload: dict[str, Any], admin: User) -> int:
    place = db.query(Place).filter(
        Place.id == proposal.place_id, Place.merged_into_id.is_(None)
    ).populate_existing().with_for_update().first()
    if place is None:
        raise HTTPException(status_code=404, detail="수정할 장소를 찾을 수 없습니다")
    try:
        _validate_place_payload(payload)
        _apply_place_payload(db, place, payload, admin)
    except (TypeError, ValueError) as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return place.id


def _approve_image(db: Session, proposal: AgentProposal, payload: dict[str, Any], admin: User) -> int:
    place = db.query(Place).filter(
        Place.id == proposal.place_id, Place.merged_into_id.is_(None)
    ).populate_existing().with_for_update().first()
    if place is None:
        raise HTTPException(status_code=404, detail="이미지를 연결할 장소를 찾을 수 없습니다")
    image_url = _https_url(payload.get("image_url"))
    source_url = _https_url(payload.get("source_url"))
    existing = db.query(PlaceImage).filter(
        PlaceImage.place_id == place.id, PlaceImage.image_url == image_url
    ).first()
    if existing is None:
        sort_order = db.query(func.max(PlaceImage.sort_order)).filter(
            PlaceImage.place_id == place.id
        ).scalar() or 0
        caption_parts = [str(payload.get("caption") or "").strip()]
        license_name = str(payload.get("license") or "").strip()
        artist = str(payload.get("artist") or "").strip()
        if license_name or artist:
            caption_parts.append(" · ".join(value for value in [license_name, artist] if value))
        existing = PlaceImage(
            place_id=place.id,
            user_id=admin.id,
            image_url=image_url,
            source_url=source_url,
            caption=" | ".join(value for value in caption_parts if value)[:300],
            sort_order=sort_order + 10,
        )
        db.add(existing)
        db.flush()
        record_place_change_event(
            db,
            place_id=place.id,
            actor_id=admin.id,
            event_type="agent_image_approved",
            summary="관리자가 출처·라이선스가 있는 이미지 제안을 승인했습니다",
            metadata={
                "image_id": existing.id,
                "source_url": source_url,
                "creation_snapshot": _row_snapshot(existing, _AGENT_IMAGE_SNAPSHOT_FIELDS),
            },
        )
    return place.id


def _approve_insight(db: Session, proposal: AgentProposal, payload: dict[str, Any], admin: User) -> int:
    place = db.query(Place).filter(
        Place.id == proposal.place_id, Place.merged_into_id.is_(None)
    ).populate_existing().with_for_update().first()
    if place is None:
        raise HTTPException(status_code=404, detail="인사이트를 연결할 장소를 찾을 수 없습니다")
    source_url = _https_url(payload.get("source_url"))
    title = str(payload.get("title") or proposal.title).strip()[:200]
    content = str(payload.get("content") or "").strip()
    if not title or not content:
        raise HTTPException(status_code=422, detail="인사이트 제목과 내용이 필요합니다")
    row = PlaceInsight(
        place_id=place.id,
        kind=str(payload.get("kind") or "local_tip")[:20],
        title=title,
        content=content,
        year_label=str(payload.get("year_label") or "")[:50],
        source_url=source_url,
        source_title=str(payload.get("source_title") or "")[:300],
        confidence=max(0.0, min(float(payload.get("confidence", proposal.confidence)), 1.0)),
        sort_order=int(payload.get("sort_order") or 0),
        created_by_id=admin.id,
        verified_at=datetime.now(timezone.utc),
    )
    db.add(row)
    db.flush()
    record_place_change_event(
        db, place_id=place.id, actor_id=admin.id, event_type="agent_insight_approved",
        summary="관리자가 출처가 있는 장소 인사이트 제안을 승인했습니다",
        metadata={
            "insight_id": row.id,
            "source_url": source_url,
            "creation_snapshot": _row_snapshot(row, _AGENT_INSIGHT_SNAPSHOT_FIELDS),
        },
    )
    return place.id


@router.post("/proposals/{proposal_id}/approve", response_model=AgentProposalOut)
def approve_agent_proposal(
    proposal_id: int,
    body: ProposalDecision,
    admin: User = Depends(get_admin_user),
    db: Session = Depends(get_db),
) -> AgentProposalOut:
    # Merge approvals can overlap the same proposal set. Pre-read only to find
    # that set, then lock every related proposal in deterministic ID order
    # before taking the exact row lock; two merge approvals can no longer each
    # hold their own row while waiting for the other.
    preview = db.query(AgentProposal).filter(
        AgentProposal.id == proposal_id
    ).populate_existing().first()
    preview_merge_ids: set[int] = set()
    if preview is not None and preview.action == "merge":
        preview_payload = _loads(preview.payload_json, {})
        if isinstance(preview_payload, dict):
            try:
                preview_source_id = preview.secondary_place_id or int(
                    preview_payload.get("duplicate_place_id") or 0
                )
                preview_target_id = preview.place_id or int(
                    preview_payload.get("canonical_place_id") or 0
                )
            except (TypeError, ValueError):
                preview_source_id = preview_target_id = 0
            preview_merge_ids = {value for value in (preview_source_id, preview_target_id) if value}
        if preview_merge_ids:
            from app.parity_api import _lock_merge_references

            _lock_merge_references(db, preview_merge_ids)
    proposal = db.query(AgentProposal).filter(
        AgentProposal.id == proposal_id
    ).populate_existing().with_for_update().first()
    if proposal is None:
        raise HTTPException(status_code=404, detail="운영 조사 제안을 찾을 수 없습니다")
    if proposal.status != "pending":
        raise HTTPException(status_code=409, detail="대기 중인 제안만 승인할 수 있습니다")
    payload = _loads(proposal.payload_json, {})
    if not isinstance(payload, dict):
        raise HTTPException(status_code=422, detail="제안 데이터 형식이 올바르지 않습니다")
    if proposal.action == "merge":
        try:
            current_merge_ids = {
                value for value in (
                    proposal.secondary_place_id or int(payload.get("duplicate_place_id") or 0),
                    proposal.place_id or int(payload.get("canonical_place_id") or 0),
                ) if value
            }
        except (TypeError, ValueError) as exc:
            raise HTTPException(status_code=422, detail="병합할 장소 정보가 올바르지 않습니다") from exc
        if current_merge_ids != preview_merge_ids:
            db.rollback()
            raise HTTPException(
                status_code=409,
                detail="병합 제안 대상이 변경되었습니다. 목록을 새로고침한 뒤 다시 승인해 주세요",
            )
    if proposal.action == "create":
        result_place_id = _approve_create(db, proposal, payload, admin, body)
    elif proposal.action == "update":
        result_place_id = _approve_update(db, proposal, payload, admin)
    elif proposal.action == "image":
        result_place_id = _approve_image(db, proposal, payload, admin)
    elif proposal.action == "insight":
        result_place_id = _approve_insight(db, proposal, payload, admin)
    elif proposal.action == "merge":
        source_id = proposal.secondary_place_id or int(payload.get("duplicate_place_id") or 0)
        target_id = proposal.place_id or int(payload.get("canonical_place_id") or 0)
        if not source_id or not target_id:
            raise HTTPException(status_code=422, detail="병합할 장소 정보가 없습니다")
        from app.parity_api import MergeRequest, _merge_places

        merge_result = _merge_places(
            source_id,
            MergeRequest(
                target_place_id=target_id,
                note=body.note.strip() or "운영 조사 중복 제안 승인",
                force=body.force,
            ),
            db=db,
            admin=admin,
            commit=False,
        )
        merge_event = db.get(PlaceChangeEvent, merge_result.event_id)
        if merge_event is not None:
            merge_metadata = _event_metadata(merge_event)
            merge_metadata["agent_proposal_id"] = proposal.id
            merge_event.metadata_json = json.dumps(
                merge_metadata, ensure_ascii=False, separators=(",", ":")
            )
        result_place_id = target_id
    else:
        raise HTTPException(status_code=422, detail="지원하지 않는 제안 작업입니다")
    proposal.status = "approved"
    proposal.result_place_id = result_place_id
    proposal.decision_note = body.note.strip()
    proposal.decided_by_id = admin.id
    proposal.decided_at = datetime.now(timezone.utc)
    db.commit()
    db.refresh(proposal)
    return _proposal_out(proposal)


@router.post("/proposals/{proposal_id}/reject", response_model=AgentProposalOut)
def reject_agent_proposal(
    proposal_id: int,
    body: ProposalDecision,
    admin: User = Depends(get_admin_user),
    db: Session = Depends(get_db),
) -> AgentProposalOut:
    proposal = db.query(AgentProposal).filter(AgentProposal.id == proposal_id).with_for_update().first()
    if proposal is None:
        raise HTTPException(status_code=404, detail="운영 조사 제안을 찾을 수 없습니다")
    if proposal.status != "pending":
        raise HTTPException(status_code=409, detail="대기 중인 제안만 거절할 수 있습니다")
    proposal.status = "rejected"
    proposal.decision_note = body.note.strip()
    proposal.decided_by_id = admin.id
    proposal.decided_at = datetime.now(timezone.utc)
    db.commit()
    db.refresh(proposal)
    return _proposal_out(proposal)
