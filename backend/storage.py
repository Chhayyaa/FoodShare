"""Image storage: Amazon S3 in the cloud (UPLOAD_BUCKET set), local folder otherwise."""
import os
import re
import uuid
from pathlib import Path

ALLOWED_TYPES = {"image/jpeg": "jpg", "image/png": "png", "image/webp": "webp"}
KEY_RE = re.compile(r"^listings/[0-9a-f]{32}\.(jpg|png|webp)$")
MAX_BYTES = 5 * 1024 * 1024


def using_s3():
    return bool(os.environ.get("UPLOAD_BUCKET"))


def valid_key(key):
    return isinstance(key, str) and bool(KEY_RE.match(key))


def local_path(key):
    """Filesystem path for a key in local mode, or None if the key is invalid."""
    if not valid_key(key):
        return None
    root = Path(os.environ.get("LOCAL_UPLOAD_DIR", "local_data/uploads"))
    return root / key


_s3 = None


def _client():
    global _s3
    if _s3 is None:
        import boto3
        from botocore.config import Config

        _s3 = boto3.client(
            "s3",
            region_name=os.environ.get("AWS_REGION"),
            config=Config(signature_version="s3v4"),
        )
    return _s3


def presign_upload(content_type):
    ext = ALLOWED_TYPES.get(content_type)
    if not ext:
        raise ValueError("Only JPEG, PNG or WebP images are allowed")
    key = f"listings/{uuid.uuid4().hex}.{ext}"
    if using_s3():
        url = _client().generate_presigned_url(
            "put_object",
            Params={
                "Bucket": os.environ["UPLOAD_BUCKET"],
                "Key": key,
                "ContentType": content_type,
            },
            ExpiresIn=300,
        )
    else:
        url = f"/local-uploads/{key}"
    return {"key": key, "upload_url": url, "method": "PUT", "headers": {"Content-Type": content_type}}


def image_url(key):
    if not key:
        return None
    if using_s3():
        return _client().generate_presigned_url(
            "get_object",
            Params={"Bucket": os.environ["UPLOAD_BUCKET"], "Key": key},
            ExpiresIn=3600,
        )
    return f"/local-files/{key}"
