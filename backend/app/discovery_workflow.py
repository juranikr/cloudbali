"""Dispatch and inspect the durable discovery Step Functions workflow."""

from __future__ import annotations

import json
import re
import uuid
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from app.config import settings


_EXECUTION_NAME_PATTERN = re.compile(r"[^A-Za-z0-9_-]+")


@dataclass(frozen=True)
class DiscoveryWorkflowExecution:
    execution_arn: str
    status: str
    started_at: datetime | None = None
    stopped_at: datetime | None = None
    output: dict[str, Any] | None = None


def discovery_workflow_enabled() -> bool:
    return bool(settings.discovery_state_machine_arn.strip())


def _step_functions_client():
    import boto3

    return boto3.client("stepfunctions", region_name=settings.aws_region)


def _execution_name(run_id: int | None) -> str:
    raw = f"manual-run-{run_id}" if run_id else f"manual-{uuid.uuid4().hex}"
    return _EXECUTION_NAME_PATTERN.sub("-", raw)[:80]


def _execution_arn(state_machine_arn: str, name: str) -> str:
    prefix, state_machine_name = state_machine_arn.replace(
        ":stateMachine:", ":execution:", 1
    ).rsplit(":", 1)
    return f"{prefix}:{state_machine_name}:{name}"


def _json_object(raw: object) -> dict[str, Any] | None:
    if isinstance(raw, dict):
        return raw
    if not isinstance(raw, str) or not raw:
        return None
    try:
        value = json.loads(raw)
    except json.JSONDecodeError:
        return {"raw": raw}
    return value if isinstance(value, dict) else {"value": value}


def start_discovery_workflow(
    *,
    run_id: int,
    region_id: int | None,
    limit: int,
    trigger: str = "manual",
    client=None,
) -> DiscoveryWorkflowExecution:
    """Start one idempotently named execution for a pre-created DB run.

    The caller creates the ``BatchRun``/``DiscoveryJob`` first and passes its
    positive id. Repeating the API request for the same run resolves to the
    same Standard Workflow execution instead of launching a duplicate.
    """

    state_machine_arn = settings.discovery_state_machine_arn.strip()
    if not state_machine_arn:
        raise RuntimeError("durable discovery workflow is not configured")
    if run_id <= 0:
        raise ValueError("run_id must be positive")
    if trigger not in {"manual", "schedule"}:
        raise ValueError("unsupported discovery trigger")

    name = _execution_name(run_id)
    payload = {
        "run_id": run_id,
        "region_id": region_id or 0,
        "limit": max(1, min(int(limit), 200)),
        "trigger": trigger,
        "worker_mode": settings.discovery_worker_mode,
    }
    step_functions = client or _step_functions_client()
    try:
        response = step_functions.start_execution(
            stateMachineArn=state_machine_arn,
            name=name,
            input=json.dumps(payload, ensure_ascii=False, sort_keys=True),
        )
        return DiscoveryWorkflowExecution(
            execution_arn=response["executionArn"],
            status="RUNNING",
            started_at=response.get("startDate"),
        )
    except Exception as exc:
        error = getattr(exc, "response", {}).get("Error", {})
        execution_arn = _execution_arn(state_machine_arn, name)
        if error.get("Code") == "ExecutionAlreadyExists":
            # Standard Workflows keep an execution name unique for 90 days.
            return describe_discovery_workflow(execution_arn, client=step_functions)
        # A client timeout can be ambiguous: StartExecution may have reached
        # AWS even though the response did not reach the API. Resolve the
        # deterministic execution ARN before declaring dispatch failure.
        try:
            return describe_discovery_workflow(execution_arn, client=step_functions)
        except Exception:
            raise exc


def describe_discovery_workflow(
    execution_arn: str,
    *,
    client=None,
) -> DiscoveryWorkflowExecution:
    if not discovery_workflow_enabled():
        raise RuntimeError("durable discovery workflow is not configured")
    response = (client or _step_functions_client()).describe_execution(
        executionArn=execution_arn
    )
    return DiscoveryWorkflowExecution(
        execution_arn=response["executionArn"],
        status=response["status"],
        started_at=response.get("startDate"),
        stopped_at=response.get("stopDate"),
        output=_json_object(response.get("output") or response.get("cause")),
    )
