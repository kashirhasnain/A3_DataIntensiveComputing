import json
import os
import typing
from functools import lru_cache
from urllib.parse import unquote_plus

import boto3
import nltk
from nltk.corpus import stopwords
from nltk.stem import WordNetLemmatizer
from nltk.tokenize import word_tokenize

if typing.TYPE_CHECKING:
    from mypy_boto3_s3 import S3Client
    from mypy_boto3_ssm import SSMClient


endpoint_url = None
if os.getenv("STAGE") == "local":
    endpoint_url = "http://localhost:4566"

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
nltk.data.path.extend(
    [
        os.path.join(BASE_DIR, "nltk_data"),
        os.path.join(BASE_DIR, "package", "nltk_data"),
        "/var/task/nltk_data",
        "/var/task/package/nltk_data",
        "/opt/nltk_data",
        "/tmp/nltk_data",
    ]
)

s3: "S3Client" = boto3.client("s3", endpoint_url=endpoint_url)
ssm: "SSMClient" = boto3.client("ssm", endpoint_url=endpoint_url)

REQUIRED_REVIEW_FIELDS = ("summary", "reviewText", "overall")
RAW_BUCKET_PARAMETER = os.getenv(
    "RAW_BUCKET_PARAMETER",
    "/review-analysis/buckets/raw",
)
PREPROCESSED_BUCKET_PARAMETER = os.getenv(
    "PREPROCESSED_BUCKET_PARAMETER",
    "/review-analysis/buckets/preprocessed",
)


@lru_cache(maxsize=1)
def get_stop_words() -> set[str]:
    return set(stopwords.words("english"))


@lru_cache(maxsize=1)
def get_lemmatizer() -> WordNetLemmatizer:
    return WordNetLemmatizer()


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


def read_review(bucket: str, key: str) -> dict:
    response = s3.get_object(Bucket=bucket, Key=key)
    payload = response["Body"].read().decode("utf-8")
    review = json.loads(payload)

    if not isinstance(review, dict):
        raise ValueError(f"{bucket}/{key} must contain one review JSON object")

    return review


def get_ssm_parameter(name: str) -> str:
    parameter = ssm.get_parameter(Name=name)
    return parameter["Parameter"]["Value"]


def get_bucket_names() -> dict:
    return {
        "raw": get_ssm_parameter(RAW_BUCKET_PARAMETER),
        "preprocessed": get_ssm_parameter(PREPROCESSED_BUCKET_PARAMETER),
    }


def extract_review_fields(review: dict) -> dict:
    missing_fields = [field for field in REQUIRED_REVIEW_FIELDS if field not in review]
    if missing_fields:
        raise ValueError(f"review is missing required fields: {', '.join(missing_fields)}")

    return {field: review[field] for field in REQUIRED_REVIEW_FIELDS}


def preprocess_review_fields(review_fields: dict) -> dict:
    stop_words = get_stop_words()
    lemmatizer = get_lemmatizer()

    preprocessed = {}
    for field, value in review_fields.items():
        if field == "overall" and isinstance(value, (int, float)):
            preprocessed[field] = [str(int(value)) if float(value).is_integer() else str(value)]
            continue

        preprocessed[field] = [
            lemmatizer.lemmatize(token.lower())
            for token in word_tokenize("" if value is None else str(value))
            if token.isalnum() and token.lower() not in stop_words
        ]

    return preprocessed


def write_preprocessed_review(
    target_bucket: str,
    target_key: str,
    source_bucket: str,
    source_key: str,
    original_review: dict,
    review_fields: dict,
    preprocessed_fields: dict,
) -> None:
    payload = {
        "sourceBucket": source_bucket,
        "sourceKey": source_key,
        # Full original review so downstream lambdas have reviewerID, asin, etc.
        "review": original_review,
        # Only the three NLP-relevant fields, preprocessed
        "preprocessed": preprocessed_fields,
    }
    s3.put_object(
        Bucket=target_bucket,
        Key=target_key,
        Body=json.dumps(payload).encode("utf-8"),
        ContentType="application/json",
    )


def handler(event, context):
    bucket_names = get_bucket_names()
    source_bucket = bucket_names["raw"]
    target_bucket = bucket_names["preprocessed"]
    processed = []

    for record in iter_s3_records(event):
        bucket = record["s3"]["bucket"]["name"]
        key = unquote_plus(record["s3"]["object"]["key"])

        if bucket != source_bucket:
            raise ValueError(
                f"unexpected source bucket {bucket!r}; expected SSM-configured raw bucket"
            )

        review = read_review(bucket, key)
        review_fields = extract_review_fields(review)
        preprocessed_fields = preprocess_review_fields(review_fields)
        write_preprocessed_review(
            target_bucket=target_bucket,
            target_key=key,
            source_bucket=bucket,
            source_key=key,
            original_review=review,
            review_fields=review_fields,
            preprocessed_fields=preprocessed_fields,
        )

        print(
            json.dumps(
                {
                    "message": "received review",
                    "bucket": bucket,
                    "key": key,
                    "targetBucket": target_bucket,
                    "targetKey": key,
                    "summary": review_fields["summary"],
                    "reviewText": review_fields["reviewText"],
                    "overall": review_fields["overall"],
                    "preprocessed": preprocessed_fields,
                }
            )
        )
        processed.append(
            {
                "bucket": bucket,
                "key": key,
                "targetBucket": target_bucket,
                "targetKey": key,
                "review": review_fields,
                "preprocessed": preprocessed_fields,
            }
        )

    return {"statusCode": 200, "processed": processed}
