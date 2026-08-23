from __future__ import annotations

import json
from unittest.mock import patch

from app.config import settings
from app.curation import _groq_enrichment
from app.models import DiscoveryCandidate


def test_groq_enrichment_uses_the_configured_key_without_an_optional_sdk() -> None:
    candidate = DiscoveryCandidate(
        discovery_run_id=1,
        region_id=1,
        source="openstreetmap",
        external_id="node/1",
        source_url="https://www.openstreetmap.org/node/1",
        title="Grounded Cafe",
        local_name="Grounded Cafe",
        description="",
        area="Ubud",
        category="cafe",
        lat=-8.51,
        lng=115.26,
        confidence=0.7,
        evidence='{"osm_tags":{"amenity":"cafe"}}',
        tags="",
        status="pending",
    )
    response_payload = {
        "choices": [{
            "message": {
                "content": json.dumps({
                    "description_ko": "공개 출처에서 확인한 카페 설명입니다.",
                    "best_time": "오전",
                    "traveler_note": "방문 전 영업시간 확인",
                    "tags": ["카페", "우붓"],
                    "confidence": 0.81,
                }, ensure_ascii=False)
            }
        }]
    }

    class FakeResponse:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def read(self, _limit: int) -> bytes:
            return json.dumps(response_payload, ensure_ascii=False).encode("utf-8")

    with (
        patch.object(settings, "groq_api_key", "test-key"),
        patch("app.curation.urlopen", return_value=FakeResponse()) as request,
    ):
        enriched = _groq_enrichment(
            candidate,
            [{"url": "https://example.test", "title": "Official", "description": "Facts"}],
        )

    assert enriched["best_time"] == "오전"
    assert enriched["tags"] == ["카페", "우붓"]
    sent_request = request.call_args.args[0]
    assert sent_request.full_url == "https://api.groq.com/openai/v1/chat/completions"
    assert sent_request.get_header("Authorization") == "Bearer test-key"
