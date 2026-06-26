import json
import os
import typing
from functools import lru_cache
from urllib.parse import unquote_plus

import boto3
from profanityfilter import ProfanityFilter

if typing.TYPE_CHECKING:
    from mypy_boto3_s3 import S3Client
    from mypy_boto3_ssm import SSMClient
    from mypy_boto3_dynamodb import DynamoDBServiceResource

endpoint_url = None
if os.getenv("STAGE") == "local":
    endpoint_url = "http://localhost:4566"

s3: "S3Client" = boto3.client("s3", endpoint_url=endpoint_url)
ssm: "SSMClient" = boto3.client("ssm", endpoint_url=endpoint_url)
dynamodb: "DynamoDBServiceResource" = boto3.resource("dynamodb", endpoint_url=endpoint_url)

PREPROCESSED_BUCKET_PARAMETER = os.getenv(
    "PREPROCESSED_BUCKET_PARAMETER",
    "/review-analysis/buckets/preprocessed",
)
PROFANITY_BUCKET_PARAMETER = os.getenv(
    "PROFANITY_BUCKET_PARAMETER",
    "/review-analysis/buckets/profanity-checked",
)
IMPOLITE_TABLE_PARAMETER = os.getenv(
    "IMPOLITE_TABLE_PARAMETER",
    "/review-analysis/tables/impolite-counts",
)
BANNED_TABLE_PARAMETER = os.getenv(
    "BANNED_TABLE_PARAMETER",
    "/review-analysis/tables/banned-customers",
)

IMPOLITE_THRESHOLD = 3


def get_ssm_parameter(name: str) -> str:
    parameter = ssm.get_parameter(Name=name)
    return parameter["Parameter"]["Value"]


def get_resource_names() -> dict:
    return {
        "preprocessed_bucket": get_ssm_parameter(PREPROCESSED_BUCKET_PARAMETER),
        "profanity_bucket": get_ssm_parameter(PROFANITY_BUCKET_PARAMETER),
        "impolite_table": get_ssm_parameter(IMPOLITE_TABLE_PARAMETER),
        "banned_table": get_ssm_parameter(BANNED_TABLE_PARAMETER),
    }


@lru_cache(maxsize=1)
def get_profanity_filter() -> ProfanityFilter:
    return ProfanityFilter()


def iter_s3_records(event):
    if isinstance(event, dict):
        if event.get("Event") == "s3:TestEvent":
            return []
        if isinstance(event.get("Records"), list):
            return event["Records"]
        if "s3" in event:
            return [event]
    if isinstance(event, list):
        return event
    raise ValueError(f"unsupported S3 event payload: {event!r}")


def read_preprocessed_review(bucket: str, key: str) -> dict:
    response = s3.get_object(Bucket=bucket, Key=key)
    payload = response["Body"].read().decode("utf-8")
    return json.loads(payload)


def check_profanity(text: str) -> bool:
    pf = get_profanity_filter()
    return not pf.is_clean(text)


def increment_impolite_count(table_name: str, reviewer_id: str) -> int:
    table = dynamodb.Table(table_name)
    response = table.update_item(
        Key={"reviewerID": reviewer_id},
        UpdateExpression="ADD impolite_count :inc",
        ExpressionAttributeValues={":inc": 1},
        ReturnValues="UPDATED_NEW",
    )
    return int(response["Attributes"]["impolite_count"])


def ban_customer(table_name: str, reviewer_id: str) -> None:
    table = dynamodb.Table(table_name)
    table.put_item(Item={"reviewerID": reviewer_id, "banned": True})


def write_profanity_result(target_bucket: str, key: str, payload: dict) -> None:
    s3.put_object(
        Bucket=target_bucket,
        Key=key,
        Body=json.dumps(payload).encode("utf-8"),
        ContentType="application/json",
    )


def handler(event, context):
    resources = get_resource_names()
    preprocessed_bucket = resources["preprocessed_bucket"]
    profanity_bucket = resources["profanity_bucket"]
    impolite_table = resources["impolite_table"]
    banned_table = resources["banned_table"]

    processed = []

    for record in iter_s3_records(event):
        bucket = record["s3"]["bucket"]["name"]
        key = unquote_plus(record["s3"]["object"]["key"])

        if bucket != preprocessed_bucket:
            raise ValueError(
                f"unexpected source bucket {bucket!r}; expected SSM-configured preprocessed bucket"
            )

        data = read_preprocessed_review(bucket, key)

        review = data.get("review", {})
        summary = review.get("summary", "")
        review_text = review.get("reviewText", "")

        source_key = data.get("sourceKey", key)
        reviewer_id = data.get("reviewerID") or source_key

        summary_profane = check_profanity(summary)
        review_text_profane = check_profanity(review_text)
        is_profane = summary_profane or review_text_profane

        banned = False
        impolite_count = None

        if is_profane:
            impolite_count = increment_impolite_count(impolite_table, reviewer_id)
            if impolite_count > IMPOLITE_THRESHOLD:
                ban_customer(banned_table, reviewer_id)
                banned = True

        result_payload = {
            **data,
            "profanity": {
                "is_profane": is_profane,
                "summary_profane": summary_profane,
                "reviewText_profane": review_text_profane,
                "reviewer_id": reviewer_id,
                "impolite_count": impolite_count,
                "banned": banned,
            },
        }

        write_profanity_result(profanity_bucket, key, result_payload)

        print(
            json.dumps(
                {
                    "message": "profanity check complete",
                    "bucket": bucket,
                    "key": key,
                    "targetBucket": profanity_bucket,
                    "targetKey": key,
                    "is_profane": is_profane,
                    "reviewer_id": reviewer_id,
                    "impolite_count": impolite_count,
                    "banned": banned,
                }
            )
        )

        processed.append(
            {
                "bucket": bucket,
                "key": key,
                "targetBucket": profanity_bucket,
                "targetKey": key,
                "is_profane": is_profane,
                "reviewer_id": reviewer_id,
                "banned": banned,
            }
        )

    return {"statusCode": 200, "processed": processed}