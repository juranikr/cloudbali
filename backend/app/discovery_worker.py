"""Fargate entry point used by the durable discovery workflow.

The Terraform task command is configurable, so a fuller curator can replace
this candidate worker later without changing the Step Functions contract.
"""

from __future__ import annotations

import json
import os
from typing import Any

from app.db import SessionLocal, engine
from app.discovery import (
    ACTIVE_DISCOVERY_SLOT,
    execute_discovery_run,
    prepare_discovery_retry,
    run_discovery,
)
from app.models import BatchRun, DiscoveryJob
from app.migrations import run_migrations


def _integer_environment(name: str, *, default: int, minimum: int, maximum: int) -> int:
    raw = os.getenv(name, "").strip()
    try:
        value = int(raw) if raw else default
    except ValueError as exc:
        raise RuntimeError(f"{name} must be an integer") from exc
    if not minimum <= value <= maximum:
        raise RuntimeError(f"{name} must be between {minimum} and {maximum}")
    return value


def _step_functions_client():
    import boto3

    return boto3.client(
        "stepfunctions", region_name=os.getenv("AWS_REGION", "ap-northeast-2")
    )


def _send_success(task_token: str, payload: dict[str, Any], *, client=None) -> None:
    if not task_token:
        return
    (client or _step_functions_client()).send_task_success(
        taskToken=task_token,
        output=json.dumps(payload, ensure_ascii=False),
    )


def _send_failure(
    task_token: str,
    *,
    error: str,
    cause: str,
    client=None,
) -> None:
    if not task_token:
        return
    (client or _step_functions_client()).send_task_failure(
        taskToken=task_token,
        error=error[:256],
        cause=cause[:32_768],
    )


def _result_payload(result: Any) -> dict[str, Any]:
    """Build the callback and structured-log contract for one finished run.

    ``failures`` was added after the durable worker shipped. Keeping the
    attribute optional lets an older API contract finish safely during a
    rolling deployment; the partial-status metric filter does not depend on
    this counter to emit an alert.
    """

    failures = getattr(result, "failures", None)
    failed_region_count = len(failures) if failures is not None else 0
    return {
        "run_id": result.run.id,
        "status": result.run.status,
        "created": result.created_count,
        "duplicates": result.duplicate_count,
        "invalid": result.invalid_count,
        "failed_region_count": failed_region_count,
        "summary": result.run.summary,
    }


def main() -> None:
    task_token = os.getenv("SFN_TASK_TOKEN", "").strip()
    try:
        run_migrations(engine)
        run_id = _integer_environment(
            "DISCOVERY_RUN_ID", default=0, minimum=0, maximum=2_147_483_647
        )
        region_id = _integer_environment(
            "DISCOVERY_REGION_ID", default=0, minimum=0, maximum=2_147_483_647
        )
        limit = _integer_environment(
            "DISCOVERY_LIMIT", default=60, minimum=1, maximum=200
        )
        retry_count = _integer_environment(
            "DISCOVERY_RETRY_COUNT", default=0, minimum=0, maximum=10
        )
        trigger = os.getenv("DISCOVERY_TRIGGER", "schedule").strip() or "schedule"
        worker_mode = (
            os.getenv("DISCOVERY_WORKER_MODE", "candidate_discovery").strip()
            or "candidate_discovery"
        )
        if worker_mode != "candidate_discovery":
            raise RuntimeError(f"unsupported discovery worker mode: {worker_mode}")

        with SessionLocal() as db:
            # A scheduled execution does not know the database sequence id in
            # advance. If Fargate was stopped after claiming its run, a Step
            # Functions retry adopts only the still-active *scheduled* lease.
            # It never steals a manual administrator run.
            if not run_id and retry_count and trigger == "schedule":
                active_job = (
                    db.query(DiscoveryJob)
                    .filter(DiscoveryJob.active_slot == ACTIVE_DISCOVERY_SLOT)
                    .first()
                )
                active_run = (
                    db.get(BatchRun, active_job.batch_run_id)
                    if active_job is not None
                    else None
                )
                if active_run is not None and active_run.trigger == "schedule":
                    run_id = active_run.id
            if run_id:
                if retry_count:
                    prepare_discovery_retry(db, run_id)
                result = execute_discovery_run(db, run_id)
            else:
                result = run_discovery(
                    db,
                    region_id=region_id or None,
                    limit=limit,
                    trigger=trigger,
                )
    except Exception as exc:
        _send_failure(
            task_token,
            error="DiscoveryWorkerFailed",
            cause=f"{type(exc).__name__}: durable discovery worker stopped",
        )
        raise

    payload = _result_payload(result)
    print(json.dumps(payload, ensure_ascii=False))
    if result.run.status == "failed":
        _send_failure(
            task_token,
            error="DiscoveryRunFailed",
            cause=result.run.summary or "discovery run failed",
        )
        raise SystemExit(1)
    _send_success(task_token, payload)


if __name__ == "__main__":
    main()
