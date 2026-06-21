# adapted from https://docs.aws.amazon.com/lambda/latest/dg/with-s3-tutorial.html
import json
import os
import typing
from base64 import b64decode

import boto3
from botocore.exceptions import ClientError

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

def get_bucket_name() -> str:
    parameter = ssm.get_parameter(Name="/ministack-thumbnail-app/buckets/images")
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


def get_request_key(event) -> str:
    if not is_http_event(event):
        return (event or {}).get("key", "")

    body = (event or {}).get("body")
    if not body:
        return ""

    if event.get("isBase64Encoded"):
        body = b64decode(body).decode("utf-8")

    payload = json.loads(body)
    return payload.get("key", "")


def handler(event, context):
    event = event or {}
    cors_headers = get_cors_headers(event)

    if event.get("requestContext", {}).get("http", {}).get("method") == "OPTIONS":
        return {"statusCode": 204, "headers": cors_headers, "body": ""}

    bucket = get_bucket_name()
    key = get_request_key(event)

    if not key:
        if is_http_event(event):
            return {
                "statusCode": 400,
                "headers": {
                    **cors_headers,
                    "Content-Type": "application/json",
                },
                "body": json.dumps({"error": "no key given"}),
            }
        raise ValueError("no key given")

    # make sure the bucket exists
    try: 
        s3_internal.head_bucket(Bucket=bucket)
    except Exception:
        s3_internal.create_bucket(Bucket=bucket)

    # make sure the object does not exist
    try:
        s3_internal.head_object(Bucket=bucket, Key=key)
        if is_http_event(event):
            return {
                "statusCode": 409,
                "headers": cors_headers,
                "body": f"{bucket}/{key} already exists",
            }
        return {"error": f"{bucket}/{key} already exists"}
    except ClientError as e:
        if e.response["ResponseMetadata"]["HTTPStatusCode"] != 404:
            raise

    # generate the pre-signed POST url
    url = s3_public.generate_presigned_post(Bucket=bucket, Key=key)

    if is_http_event(event):
        return {
            "statusCode": 200,
            "headers": {
                **cors_headers,
                "Content-Type": "application/json",
            },
            "body": json.dumps(url),
        }
    return url


if __name__ == "__main__":
    print(handler(None, None))
