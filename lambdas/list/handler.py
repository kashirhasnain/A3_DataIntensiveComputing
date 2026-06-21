# adapted from https://docs.aws.amazon.com/lambda/latest/dg/with-s3-tutorial.html
import json
import os
import typing

import boto3

if typing.TYPE_CHECKING:
    from mypy_boto3_s3 import S3Client
    from mypy_boto3_ssm import SSMClient

# Internal AWS API access should stay on the local MiniStack endpoint.
internal_endpoint_url = None
if os.getenv("STAGE") == "local":
    internal_endpoint_url = "http://localhost:4566"

# Public browser-facing S3 URLs can be overridden separately for proxied environments.
public_s3_endpoint_url = os.getenv("S3_ENDPOINT_URL") or internal_endpoint_url

s3_internal: "S3Client" = boto3.client("s3", endpoint_url=internal_endpoint_url)
s3_public: "S3Client" = boto3.client("s3", endpoint_url=public_s3_endpoint_url)
ssm: "SSMClient" = boto3.client("ssm", endpoint_url=internal_endpoint_url)

DEFAULT_ALLOWED_ORIGINS = "http://localhost:4566,http://127.0.0.1:4566,https://lbd.tuwien.ac.at"


def get_bucket_name_images() -> str:
    parameter = ssm.get_parameter(Name="/ministack-thumbnail-app/buckets/images")
    return parameter["Parameter"]["Value"]


def get_bucket_name_resized() -> str:
    parameter = ssm.get_parameter(Name="/ministack-thumbnail-app/buckets/resized")
    return parameter["Parameter"]["Value"]


def get_allowed_origins() -> set[str]:
    configured = os.getenv("ALLOWED_ORIGINS", DEFAULT_ALLOWED_ORIGINS)
    return {origin.strip() for origin in configured.split(",") if origin.strip()}


def get_cors_headers(event) -> dict[str, str]:
    origin = (event or {}).get("headers", {}).get("origin")
    allowed_origins = get_allowed_origins()
    allow_origin = origin if origin in allowed_origins else next(iter(allowed_origins), "*")
    return {
        "Access-Control-Allow-Origin": allow_origin,
        "Access-Control-Allow-Methods": "GET,POST,OPTIONS",
        "Access-Control-Allow-Headers": "*",
        "Vary": "Origin",
    }


def is_http_event(event) -> bool:
    return isinstance(event, dict) and (
        "requestContext" in event or "rawPath" in event or "headers" in event
    )


def handler(event, context):
    event = event or {}
    cors_headers = get_cors_headers(event)

    if event.get("requestContext", {}).get("http", {}).get("method") == "OPTIONS":
        return {"statusCode": 204, "headers": cors_headers, "body": ""}

    images_bucket = get_bucket_name_images()
    images = s3_internal.list_objects(Bucket=images_bucket)

    if not images.get("Contents"):
        print(f"Bucket {images_bucket} is empty")
        if is_http_event(event):
            return {"statusCode": 200, "headers": cors_headers, "body": "[]"}
        return []

    result = {}
    # collect the original images
    for obj in images["Contents"]:
        result[obj["Key"]] = {
            "Name": obj["Key"],
            "Timestamp": obj["LastModified"].isoformat(),
            "Original": {
                "Size": obj["Size"],
                "URL": s3_public.generate_presigned_url(
                    ClientMethod="get_object",
                    Params={"Bucket": images_bucket, "Key": obj["Key"]},
                    ExpiresIn=3600,
                ),
            },
        }

    # collect the associated resized images
    resized_bucket = get_bucket_name_resized()
    images = s3_internal.list_objects(Bucket=resized_bucket)
    for obj in images.get("Contents", []):
        if obj["Key"] not in result:
            continue
        result[obj["Key"]]["Resized"] = {
            "Size": obj["Size"],
            "URL": s3_public.generate_presigned_url(
                ClientMethod="get_object",
                Params={"Bucket": resized_bucket, "Key": obj["Key"]},
                ExpiresIn=3600,
            ),
        }

    payload = list(sorted(result.values(), key=lambda k: k["Timestamp"], reverse=True))
    if is_http_event(event):
        return {
            "statusCode": 200,
            "headers": cors_headers,
            "body": json.dumps(payload),
        }
    return payload


if __name__ == "__main__":
    print(handler(None, None))
