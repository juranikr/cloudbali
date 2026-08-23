from __future__ import annotations

import json

import pytest

from app.curation_worker import _arguments, _send_failure, _send_success


class FakeClient:
    def __init__(self) -> None:
        self.success: dict | None = None
        self.failure: dict | None = None

    def send_task_success(self, **kwargs) -> None:
        self.success = kwargs

    def send_task_failure(self, **kwargs) -> None:
        self.failure = kwargs


def test_curation_worker_accepts_explicit_run_id() -> None:
    args = _arguments(["--run-id", "91", "--retry-count", "2"])
    assert args.run_id == 91
    assert args.retry_count == 2


def test_curation_worker_rejects_missing_run_id(monkeypatch) -> None:
    monkeypatch.delenv("CURATION_RUN_ID", raising=False)
    with pytest.raises(RuntimeError, match="between"):
        _arguments([])


def test_curation_worker_callbacks_are_structured_and_bounded() -> None:
    client = FakeClient()
    _send_success("token", {"run_id": 91, "status": "partial"}, client=client)
    _send_failure("token", error="e" * 400, cause="c" * 40_000, client=client)

    assert client.success is not None
    assert json.loads(client.success["output"])["run_id"] == 91
    assert client.failure is not None
    assert len(client.failure["error"]) == 256
    assert len(client.failure["cause"]) == 32_768
