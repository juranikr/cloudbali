from __future__ import annotations

import pytest

from app.config import settings
from app.image_storage import (
    ManagedImageStorageError,
    configured_public_image_url,
    delete_recorded_upload_object,
)


class FakeS3:
    def __init__(self) -> None:
        self.calls: list[dict[str, str]] = []

    def delete_object(self, **kwargs) -> None:
        self.calls.append(kwargs)


def test_managed_image_delete_requires_exact_bucket_url_and_safe_key(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(settings, "s3_bucket", "private-images")
    monkeypatch.setattr(settings, "s3_public_base_url", "https://images.example.test")
    client = FakeS3()
    key = "places/42/uploads/7/photo.webp"
    url = configured_public_image_url(key)

    assert delete_recorded_upload_object(
        image_url=url,
        object_key=key,
        recorded_bucket="private-images",
        recorded_public_url=url,
        client=client,
    ) == key
    assert client.calls == [{"Bucket": "private-images", "Key": key}]

    for values in (
        {"object_key": "places/42/../private.txt"},
        {"recorded_bucket": "different-bucket"},
        {"image_url": "https://external.example.test/photo.webp"},
    ):
        arguments = {
            "image_url": url,
            "object_key": key,
            "recorded_bucket": "private-images",
            "recorded_public_url": url,
            "client": client,
            **values,
        }
        with pytest.raises(ManagedImageStorageError):
            delete_recorded_upload_object(**arguments)
    assert len(client.calls) == 1


def test_legacy_direct_upload_uses_exact_current_regional_s3_url(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(settings, "s3_bucket", "private-images")
    monkeypatch.setattr(settings, "s3_public_base_url", "")
    monkeypatch.setattr(settings, "aws_region", "ap-southeast-1")
    client = FakeS3()
    key = "places/3/uploads/9/legacy.jpg"
    url = "https://private-images.s3.ap-southeast-1.amazonaws.com/" + key

    delete_recorded_upload_object(
        image_url=url,
        object_key=key,
        client=client,
    )
    assert client.calls == [{"Bucket": "private-images", "Key": key}]
