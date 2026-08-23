"""Strict deletion helpers for images uploaded into the private place bucket."""

from __future__ import annotations

from urllib.parse import quote, urlsplit

from app.config import settings


class ManagedImageStorageError(RuntimeError):
    """A recorded direct upload cannot be safely mapped to configured storage."""


class ManagedImageStorageNotConfigured(ManagedImageStorageError):
    """The database records a direct upload but its bucket is unavailable."""


class ManagedImageDeleteError(ManagedImageStorageError):
    """S3 rejected or failed the object deletion request."""


def _s3_client():
    import boto3

    return boto3.client("s3", region_name=settings.aws_region)


def _safe_object_key(value: str) -> str:
    key = value.strip()
    parts = key.split("/")
    if (
        not key
        or len(key) > 700
        or key.startswith("/")
        or "\\" in key
        or not key.startswith("places/")
        or any(part in {"", ".", ".."} for part in parts)
    ):
        raise ManagedImageStorageError("invalid managed image object key")
    return key


def _validate_https_url(value: str) -> str:
    try:
        parsed = urlsplit(value)
    except ValueError as exc:
        raise ManagedImageStorageError("invalid managed image URL") from exc
    if (
        parsed.scheme.lower() != "https"
        or not parsed.hostname
        or parsed.username
        or parsed.password
        or parsed.query
        or parsed.fragment
    ):
        raise ManagedImageStorageError("invalid managed image URL")
    return value


def configured_public_image_url(object_key: str) -> str:
    """Return the exact URL emitted by the upload completion API."""

    key = _safe_object_key(object_key)
    base = settings.s3_public_base_url.rstrip("/")
    if base:
        _validate_https_url(base)
        return f"{base}/{quote(key, safe='/')}"
    bucket = settings.s3_bucket.strip()
    if not bucket:
        raise ManagedImageStorageNotConfigured("managed image bucket is not configured")
    return (
        f"https://{bucket}.s3.{settings.aws_region}.amazonaws.com/"
        f"{quote(key, safe='/')}"
    )


def delete_recorded_upload_object(
    *,
    image_url: str,
    object_key: str,
    recorded_bucket: str = "",
    recorded_public_url: str = "",
    client=None,
) -> str:
    """Delete one proven direct-upload object and return its normalized key.

    Callers must only invoke this after finding the image's append-only
    ``image_uploaded`` audit event. URL-only images are deliberately outside
    this function. The exact recorded/current bucket and public URL checks
    prevent an arbitrary HTTPS image record from becoming an S3 delete target.
    """

    bucket = settings.s3_bucket.strip()
    if not bucket:
        raise ManagedImageStorageNotConfigured("managed image bucket is not configured")
    if recorded_bucket and recorded_bucket != bucket:
        raise ManagedImageStorageError("recorded image bucket differs from configured bucket")

    key = _safe_object_key(object_key)
    actual_url = _validate_https_url(image_url.strip())
    if recorded_public_url:
        expected_url = _validate_https_url(recorded_public_url.strip())
    else:
        # Backward compatibility for direct uploads completed before bucket and
        # public URL were added to the append-only audit metadata.
        expected_url = configured_public_image_url(key)
    encoded_key_path = "/" + quote(key, safe="/")
    if not urlsplit(expected_url).path.endswith(encoded_key_path):
        raise ManagedImageStorageError("recorded image URL does not contain its object key")
    if actual_url != expected_url:
        raise ManagedImageStorageError("image URL does not match its direct-upload audit record")

    try:
        (client or _s3_client()).delete_object(Bucket=bucket, Key=key)
    except Exception as exc:
        raise ManagedImageDeleteError("managed image object deletion failed") from exc
    return key


__all__ = [
    "ManagedImageDeleteError",
    "ManagedImageStorageError",
    "ManagedImageStorageNotConfigured",
    "configured_public_image_url",
    "delete_recorded_upload_object",
]
