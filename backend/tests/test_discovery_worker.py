from __future__ import annotations

import json
from types import SimpleNamespace

from app.discovery_worker import _result_payload, _send_failure, _send_success


class FakeCallbackClient:
    def __init__(self) -> None:
        self.success: dict | None = None
        self.failure: dict | None = None

    def send_task_success(self, **kwargs) -> None:
        self.success = kwargs

    def send_task_failure(self, **kwargs) -> None:
        self.failure = kwargs


def test_worker_reports_structured_success_to_step_functions() -> None:
    client = FakeCallbackClient()
    _send_success("task-token", {"run_id": 9, "status": "success"}, client=client)

    assert client.success is not None
    assert client.success["taskToken"] == "task-token"
    assert json.loads(client.success["output"]) == {"run_id": 9, "status": "success"}


def test_worker_reports_bounded_failure_to_step_functions() -> None:
    client = FakeCallbackClient()
    _send_failure(
        "task-token",
        error="e" * 400,
        cause="c" * 40_000,
        client=client,
    )

    assert client.failure is not None
    assert len(client.failure["error"]) == 256
    assert len(client.failure["cause"]) == 32_768


def test_worker_payload_reports_partial_status_and_failure_count() -> None:
    result = SimpleNamespace(
        run=SimpleNamespace(id=19, status="partial", summary="일부 권역 실패"),
        created_count=14,
        duplicate_count=1,
        invalid_count=4,
        failures=[SimpleNamespace(region_id=2), SimpleNamespace(region_id=7)],
    )

    assert _result_payload(result) == {
        "run_id": 19,
        "status": "partial",
        "created": 14,
        "duplicates": 1,
        "invalid": 4,
        "failed_region_count": 2,
        "summary": "일부 권역 실패",
    }


def test_worker_payload_supports_contract_without_failures() -> None:
    result = SimpleNamespace(
        run=SimpleNamespace(id=20, status="partial", summary="legacy result"),
        created_count=3,
        duplicate_count=0,
        invalid_count=1,
    )

    payload = _result_payload(result)

    assert payload["status"] == "partial"
    assert payload["failed_region_count"] == 0
