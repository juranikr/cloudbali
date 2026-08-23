from __future__ import annotations

import json
from datetime import datetime, timezone

from app.config import settings
from app.curation_workflow import start_curation_workflow


ARN = "arn:aws:states:ap-northeast-2:123456789012:stateMachine:cloudbali-curation"


class AlreadyExists(Exception):
    def __init__(self) -> None:
        self.response = {"Error": {"Code": "ExecutionAlreadyExists"}}


class FakeClient:
    def __init__(self, *, duplicate: bool = False) -> None:
        self.duplicate = duplicate
        self.started: list[dict] = []
        self.described: list[str] = []

    def start_execution(self, **kwargs):
        self.started.append(kwargs)
        if self.duplicate:
            raise AlreadyExists()
        return {
            "executionArn": (
                "arn:aws:states:ap-northeast-2:123456789012:execution:"
                "cloudbali-curation:curation-run-17"
            ),
            "startDate": datetime(2026, 8, 23, tzinfo=timezone.utc),
        }

    def describe_execution(self, *, executionArn: str):
        self.described.append(executionArn)
        return {
            "executionArn": executionArn,
            "status": "SUCCEEDED",
            "output": '{"run_id":17,"status":"success"}',
        }


def test_curation_dispatch_has_stable_execution_contract(monkeypatch) -> None:
    monkeypatch.setattr(settings, "curation_state_machine_arn", ARN)
    client = FakeClient()

    result = start_curation_workflow(run_id=17, client=client)

    assert result.status == "RUNNING"
    request = client.started[0]
    assert request["name"] == "curation-run-17"
    assert json.loads(request["input"]) == {"run_id": 17, "trigger": "manual"}


def test_duplicate_curation_dispatch_resolves_existing_execution(monkeypatch) -> None:
    monkeypatch.setattr(settings, "curation_state_machine_arn", ARN)
    client = FakeClient(duplicate=True)

    result = start_curation_workflow(run_id=17, client=client)

    assert result.status == "SUCCEEDED"
    assert result.output == {"run_id": 17, "status": "success"}
    assert client.described == [
        "arn:aws:states:ap-northeast-2:123456789012:execution:"
        "cloudbali-curation:curation-run-17"
    ]
