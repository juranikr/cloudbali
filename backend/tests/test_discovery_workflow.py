from __future__ import annotations

import json
from datetime import datetime, timezone

import pytest

from app.config import settings
from app.discovery_workflow import (
    describe_discovery_workflow,
    start_discovery_workflow,
)


STATE_MACHINE_ARN = (
    "arn:aws:states:ap-northeast-2:123456789012:stateMachine:cloudbali-discovery"
)


class ExecutionAlreadyExists(Exception):
    def __init__(self) -> None:
        self.response = {"Error": {"Code": "ExecutionAlreadyExists"}}


class FakeStepFunctions:
    def __init__(self, *, duplicate: bool = False, ambiguous_timeout: bool = False) -> None:
        self.duplicate = duplicate
        self.ambiguous_timeout = ambiguous_timeout
        self.started: list[dict] = []
        self.described: list[str] = []

    def start_execution(self, **kwargs):
        self.started.append(kwargs)
        if self.duplicate:
            raise ExecutionAlreadyExists()
        if self.ambiguous_timeout:
            raise TimeoutError("response was lost")
        return {
            "executionArn": (
                "arn:aws:states:ap-northeast-2:123456789012:execution:"
                "cloudbali-discovery:manual-run-73"
            ),
            "startDate": datetime(2026, 8, 23, tzinfo=timezone.utc),
        }

    def describe_execution(self, *, executionArn: str):
        self.described.append(executionArn)
        return {
            "executionArn": executionArn,
            "status": "SUCCEEDED",
            "startDate": datetime(2026, 8, 23, tzinfo=timezone.utc),
            "stopDate": datetime(2026, 8, 23, 0, 2, tzinfo=timezone.utc),
            "output": '{"run_id":73,"status":"success"}',
        }


def test_start_workflow_uses_stable_manual_name_and_bounded_input(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(settings, "discovery_state_machine_arn", STATE_MACHINE_ARN)
    monkeypatch.setattr(settings, "discovery_worker_mode", "candidate_discovery")
    client = FakeStepFunctions()

    result = start_discovery_workflow(
        run_id=73,
        region_id=None,
        limit=999,
        client=client,
    )

    assert result.status == "RUNNING"
    request = client.started[0]
    assert request["name"] == "manual-run-73"
    assert json.loads(request["input"]) == {
        "limit": 200,
        "region_id": 0,
        "run_id": 73,
        "trigger": "manual",
        "worker_mode": "candidate_discovery",
    }


def test_duplicate_dispatch_resolves_existing_standard_execution(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(settings, "discovery_state_machine_arn", STATE_MACHINE_ARN)
    client = FakeStepFunctions(duplicate=True)

    result = start_discovery_workflow(
        run_id=73,
        region_id=4,
        limit=20,
        client=client,
    )

    assert result.status == "SUCCEEDED"
    assert result.output == {"run_id": 73, "status": "success"}
    assert client.described == [
        "arn:aws:states:ap-northeast-2:123456789012:execution:"
        "cloudbali-discovery:manual-run-73"
    ]


def test_ambiguous_start_timeout_resolves_deterministic_execution(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(settings, "discovery_state_machine_arn", STATE_MACHINE_ARN)
    client = FakeStepFunctions(ambiguous_timeout=True)

    result = start_discovery_workflow(
        run_id=73,
        region_id=4,
        limit=20,
        client=client,
    )

    assert result.status == "SUCCEEDED"
    assert client.described == [
        "arn:aws:states:ap-northeast-2:123456789012:execution:"
        "cloudbali-discovery:manual-run-73"
    ]


def test_describe_requires_configured_workflow(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "discovery_state_machine_arn", "")
    with pytest.raises(RuntimeError, match="not configured"):
        describe_discovery_workflow("arn:unused", client=FakeStepFunctions())
