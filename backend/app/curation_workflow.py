"""Dispatch and inspect the durable multi-source curation workflow."""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from app.config import settings


@dataclass(frozen=True)
class CurationWorkflowExecution:
    execution_arn: str
    status: str
    started_at: datetime | None = None
    stopped_at: datetime | None = None
    output: dict[str, Any] | None = None


def curation_workflow_enabled() -> bool:
    return bool(settings.curation_state_machine_arn.strip())


def _client():
    import boto3

    return boto3.client("stepfunctions", region_name=settings.aws_region)


def _execution_arn(state_machine_arn: str, name: str) -> str:
    prefix, state_machine_name = state_machine_arn.replace(
        ":stateMachine:", ":execution:", 1
    ).rsplit(":", 1)
    return f"{prefix}:{state_machine_name}:{name}"


def _output(raw: object) -> dict[str, Any] | None:
    if not isinstance(raw, str) or not raw:
        return raw if isinstance(raw, dict) else None
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError:
        return {"raw": raw}
    return parsed if isinstance(parsed, dict) else {"value": parsed}


def describe_curation_workflow(
    execution_arn: str, *, client=None
) -> CurationWorkflowExecution:
    if not curation_workflow_enabled():
        raise RuntimeError("durable curation workflow is not configured")
    response = (client or _client()).describe_execution(executionArn=execution_arn)
    return CurationWorkflowExecution(
        execution_arn=response["executionArn"],
        status=response["status"],
        started_at=response.get("startDate"),
        stopped_at=response.get("stopDate"),
        output=_output(response.get("output") or response.get("cause")),
    )


def start_curation_workflow(
    *, run_id: int, trigger: str = "manual", client=None
) -> CurationWorkflowExecution:
    """Idempotently start the pre-created ``AgentRun`` by database id."""

    state_machine_arn = settings.curation_state_machine_arn.strip()
    if not state_machine_arn:
        raise RuntimeError("durable curation workflow is not configured")
    if run_id <= 0:
        raise ValueError("run_id must be positive")
    if trigger not in {"manual", "schedule"}:
        raise ValueError("unsupported curation trigger")
    name = f"curation-run-{run_id}"[:80]
    execution_arn = _execution_arn(state_machine_arn, name)
    step_functions = client or _client()
    try:
        response = step_functions.start_execution(
            stateMachineArn=state_machine_arn,
            name=name,
            input=json.dumps(
                {"run_id": run_id, "trigger": trigger},
                ensure_ascii=False,
                sort_keys=True,
            ),
        )
        return CurationWorkflowExecution(
            execution_arn=response["executionArn"],
            status="RUNNING",
            started_at=response.get("startDate"),
        )
    except Exception as exc:
        code = getattr(exc, "response", {}).get("Error", {}).get("Code")
        if code == "ExecutionAlreadyExists":
            return describe_curation_workflow(execution_arn, client=step_functions)
        try:
            return describe_curation_workflow(execution_arn, client=step_functions)
        except Exception:
            raise exc
