from __future__ import annotations

import json

from app.discovery_worker import _send_failure, _send_success


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
