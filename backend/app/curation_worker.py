"""Fargate callback worker for auditable multi-source Bali curation."""

from __future__ import annotations

import argparse
import json
import os
from typing import Any, Sequence

from app.agent_models import AgentRun
from app.curation import execute_agent_run, prepare_agent_retry
from app.db import SessionLocal, engine
from app.migrations import run_migrations


def _integer(value: str | int, *, name: str, minimum: int, maximum: int) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError) as exc:
        raise RuntimeError(f"{name} must be an integer") from exc
    if not minimum <= parsed <= maximum:
        raise RuntimeError(f"{name} must be between {minimum} and {maximum}")
    return parsed


def _callback_client():
    import boto3

    return boto3.client(
        "stepfunctions", region_name=os.getenv("AWS_REGION", "ap-northeast-2")
    )


def _send_success(token: str, payload: dict[str, Any], *, client=None) -> None:
    if token:
        (client or _callback_client()).send_task_success(
            taskToken=token,
            output=json.dumps(payload, ensure_ascii=False),
        )


def _send_failure(token: str, *, error: str, cause: str, client=None) -> None:
    if token:
        (client or _callback_client()).send_task_failure(
            taskToken=token,
            error=error[:256],
            cause=cause[:32_768],
        )


def _arguments(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run one durable Bali curation job")
    parser.add_argument(
        "--run-id",
        default=os.getenv("CURATION_RUN_ID", "0"),
        help="pre-created AgentRun id (or CURATION_RUN_ID)",
    )
    parser.add_argument(
        "--retry-count",
        default=os.getenv("CURATION_RETRY_COUNT", "0"),
        help="Step Functions retry count (or CURATION_RETRY_COUNT)",
    )
    values = parser.parse_args(argv)
    values.run_id = _integer(
        values.run_id, name="run_id", minimum=1, maximum=2_147_483_647
    )
    values.retry_count = _integer(
        values.retry_count, name="retry_count", minimum=0, maximum=10
    )
    return values


def _payload(run: AgentRun) -> dict[str, Any]:
    try:
        metrics = json.loads(run.metrics_json or "{}")
    except (TypeError, json.JSONDecodeError):
        metrics = {}
    return {
        "run_id": run.id,
        "status": run.status,
        "score": run.score,
        "summary": run.summary,
        "metrics": metrics if isinstance(metrics, dict) else {},
    }


def main(argv: Sequence[str] | None = None) -> None:
    token = os.getenv("SFN_TASK_TOKEN", "").strip()
    try:
        args = _arguments(argv)
        run_migrations(engine)
        with SessionLocal() as db:
            if args.retry_count:
                prepare_agent_retry(db, args.run_id)
            result = execute_agent_run(db, args.run_id)
        payload = _payload(result)
        print(json.dumps(payload, ensure_ascii=False))
        if result.status not in {"success", "partial"}:
            _send_failure(
                token,
                error="CurationRunFailed",
                cause=result.summary or f"curation ended as {result.status}",
            )
            raise SystemExit(1)
        _send_success(token, payload)
    except SystemExit:
        raise
    except Exception as exc:
        _send_failure(
            token,
            error="CurationWorkerFailed",
            cause=f"{type(exc).__name__}: durable curation worker stopped",
        )
        raise


if __name__ == "__main__":
    main()
