from __future__ import annotations

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Query, status
from sqlalchemy.orm import Session

from app.auth import get_admin_user
from app.db import get_db
from app.discovery import (
    DiscoveryBusyError,
    approve_candidate,
    create_discovery_run,
    execute_queued_discovery,
    get_discovery_run,
    list_candidates,
    reject_candidate,
)
from app.models import User
from app.schemas import (
    DiscoveryApproveRequest,
    DiscoveryCandidateOut,
    DiscoveryDecisionRequest,
    DiscoveryRunOut,
    DiscoveryRunRequest,
)


router = APIRouter(prefix="/api/admin/discovery", tags=["admin-discovery"])


@router.post("/run", response_model=DiscoveryRunOut, status_code=status.HTTP_202_ACCEPTED)
def admin_run_discovery(
    body: DiscoveryRunRequest,
    background_tasks: BackgroundTasks,
    _: User = Depends(get_admin_user),
    db: Session = Depends(get_db),
) -> DiscoveryRunOut:
    try:
        queued = create_discovery_run(
            db,
            region_id=body.region_id,
            limit=body.limit,
            trigger="manual",
        )
        background_tasks.add_task(execute_queued_discovery, queued.run.id)
        return queued
    except DiscoveryBusyError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.get("/runs/{run_id}", response_model=DiscoveryRunOut)
def admin_discovery_run(
    run_id: int,
    _: User = Depends(get_admin_user),
    db: Session = Depends(get_db),
) -> DiscoveryRunOut:
    result = get_discovery_run(db, run_id)
    if result is None:
        raise HTTPException(status_code=404, detail="발굴 실행 이력을 찾을 수 없습니다")
    return result


@router.get("/candidates", response_model=list[DiscoveryCandidateOut])
def admin_discovery_candidates(
    candidate_status: str | None = Query(
        default=None,
        alias="status",
        pattern="^(pending|duplicate|approved|rejected)$",
    ),
    region_id: int | None = Query(default=None, gt=0),
    limit: int = Query(default=100, ge=1, le=200),
    _: User = Depends(get_admin_user),
    db: Session = Depends(get_db),
) -> list[DiscoveryCandidateOut]:
    return list_candidates(
        db,
        status=candidate_status,
        region_id=region_id,
        limit=limit,
    )


@router.post("/candidates/{candidate_id}/approve", response_model=DiscoveryCandidateOut)
def admin_approve_discovery_candidate(
    candidate_id: int,
    body: DiscoveryApproveRequest,
    admin: User = Depends(get_admin_user),
    db: Session = Depends(get_db),
) -> DiscoveryCandidateOut:
    try:
        return approve_candidate(
            db,
            candidate_id,
            admin,
            note=body.note.strip(),
            force=body.force,
        )
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@router.post("/candidates/{candidate_id}/reject", response_model=DiscoveryCandidateOut)
def admin_reject_discovery_candidate(
    candidate_id: int,
    body: DiscoveryDecisionRequest,
    admin: User = Depends(get_admin_user),
    db: Session = Depends(get_db),
) -> DiscoveryCandidateOut:
    try:
        return reject_candidate(db, candidate_id, admin, note=body.note.strip())
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
