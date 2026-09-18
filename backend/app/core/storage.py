"""Raw file storage (MinIO, S3-compatible) for uploaded documents.

Only the backend ever talks to MinIO - it sits in the private Docker
network next to Postgres and Ollama, never exposed to the frontend or the
Internet (see docker-compose.yml). The extracted *text* of a document is
what gets embedded and searched (app/rag); the original file is kept here
purely so a Director/employee can download exactly what was uploaded.
"""

from functools import lru_cache
from io import BytesIO

from minio import Minio
from minio.error import S3Error

from app.core.config import get_settings

settings = get_settings()


@lru_cache
def get_minio_client() -> Minio:
    return Minio(
        settings.minio_endpoint,
        access_key=settings.minio_access_key,
        secret_key=settings.minio_secret_key,
        secure=settings.minio_secure,
    )


def ensure_bucket() -> None:
    client = get_minio_client()
    if not client.bucket_exists(settings.minio_bucket):
        client.make_bucket(settings.minio_bucket)


def upload_file(object_key: str, data: bytes, content_type: str) -> None:
    client = get_minio_client()
    client.put_object(
        settings.minio_bucket,
        object_key,
        data=BytesIO(data),
        length=len(data),
        content_type=content_type or "application/octet-stream",
    )


def download_file(object_key: str) -> bytes:
    client = get_minio_client()
    response = client.get_object(settings.minio_bucket, object_key)
    try:
        return response.read()
    finally:
        response.close()
        response.release_conn()


def delete_file(object_key: str) -> None:
    try:
        get_minio_client().remove_object(settings.minio_bucket, object_key)
    except S3Error:
        pass
