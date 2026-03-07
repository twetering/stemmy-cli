"""
Direct S3 upload for standalone operation.

Uses AWS_ACCESS_KEY_ID, AWS_SECRET_ACCESS_KEY, AWS_REGION, S3_BUCKET_NAME.
Compatible with surrounded format: audio/{subfolder}/{key}.mp3
"""

import os
from typing import Optional


def _get_s3_client():
    """Lazy boto3 client to avoid import when S3 not used."""
    import boto3
    return boto3.client(
        "s3",
        aws_access_key_id=os.getenv("AWS_ACCESS_KEY_ID"),
        aws_secret_access_key=os.getenv("AWS_SECRET_ACCESS_KEY"),
        region_name=os.getenv("AWS_REGION", "eu-north-1"),
    )


def is_s3_configured() -> bool:
    """Check if AWS credentials and bucket are set."""
    return bool(
        os.getenv("AWS_ACCESS_KEY_ID")
        and os.getenv("AWS_SECRET_ACCESS_KEY")
        and (os.getenv("S3_BUCKET_NAME") or os.getenv("AWS_S3_BUCKET"))
    )


def upload_audio_to_s3(
    file_path: str,
    s3_key: str,
    bucket_name: Optional[str] = None,
) -> str:
    """
    Upload audio file to S3 and return public URL.

    Args:
        file_path: Local path to MP3
        s3_key: S3 object key (e.g. audio/formats/{format_id}/{item_id}.mp3)
        bucket_name: Override bucket (default from env)

    Returns:
        Public URL: https://{bucket}.s3.{region}.amazonaws.com/{key}
    """
    bucket = bucket_name or os.getenv("S3_BUCKET_NAME") or os.getenv("AWS_S3_BUCKET", "voxpop")
    region = os.getenv("AWS_REGION", "eu-north-1")

    s3 = _get_s3_client()
    s3.upload_file(
        file_path,
        bucket,
        s3_key,
        ExtraArgs={"ContentType": "audio/mpeg"},
    )
    return f"https://{bucket}.s3.{region}.amazonaws.com/{s3_key}"
