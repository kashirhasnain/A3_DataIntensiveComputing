import json
from decimal import Decimal

import os
import typing
import nltk
from urllib.parse import unquote_plus
from functools import lru_cache

import boto3
from nltk.sentiment import SentimentIntensityAnalyzer

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

dynamodb = boto3.resource(
    "dynamodb",
    endpoint_url=endpoint_url,
    region_name="us-east-1",
)




PROFANITY_BUCKET_PARAMETER = os.getenv(
    "PROFANITY_BUCKET_PARAMETER",
    "/review-analysis/buckets/profanity-checked",
)

DYNAMODB_TABLE_PARAMETER = os.getenv(
    "DYNAMODB_TABLE_PARAMETER",
    "/review-analysis/tables/reviews",
)

def get_ssm_parameter(name: str) -> str:
    parameter = ssm.get_parameter(Name=name)
    return parameter["Parameter"]["Value"]

def read_review(bucket: str, key: str) -> dict:
    response = s3.get_object(Bucket=bucket, Key=key)
    payload = response["Body"].read().decode("utf-8")
    return json.loads(payload)


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


def extract_review_text(review_data: dict) -> str:
    review = review_data.get("review", {})

    summary = review.get("summary", "")
    review_text = review.get("reviewText", "")

    return f"{summary} {review_text}"


def extract_review_metadata(review_data: dict) -> dict:
    review = review_data.get("review", {})

    return {
        "reviewerID": review.get("reviewerID"),
        "asin": review.get("asin"),
        "overall": review.get("overall"),
    }

@lru_cache(maxsize=1)
def get_sentiment_analyzer() -> SentimentIntensityAnalyzer:
    return SentimentIntensityAnalyzer()

def classify_sentiment(text: str) -> str:
    scores = get_sentiment_analyzer().polarity_scores(text)
    compound = scores["compound"]

    if compound >= 0.05:
        return "positive"
    elif compound <= -0.05:
        return "negative"
    else:
        return "neutral"
    
def get_bucket_names() -> dict:
    return {
        "profanity": get_ssm_parameter(PROFANITY_BUCKET_PARAMETER),
    }

def get_table_name() -> str:
    return get_ssm_parameter(DYNAMODB_TABLE_PARAMETER)


def save_to_dynamodb(
    table_name: str,
    key: str,
    metadata: dict,
    sentiment: str,
    profanity_flag: bool,
) -> None:
    table = dynamodb.Table(table_name)

    overall = metadata.get("overall")
    overall_decimal = Decimal(str(overall)) if overall is not None else None

    table.put_item(
        Item={
            "reviewId": key,
            "reviewerID": metadata.get("reviewerID"),
            "asin": metadata.get("asin"),
            "overall": overall_decimal,
            "sentiment": sentiment,
            "profanityFlag": profanity_flag,
        }
    )
    

def handler(event, context):
    print(json.dumps(event))

    processed = []
    expected_bucket = get_bucket_names()["profanity"]

    for record in iter_s3_records(event):
        bucket = record["s3"]["bucket"]["name"]
        key = unquote_plus(record["s3"]["object"]["key"])

        if bucket != expected_bucket:
            raise ValueError(
                f"unexpected source bucket {bucket!r}; expected {expected_bucket!r}"
    )

        review_data = read_review(bucket, key)

        text = extract_review_text(review_data)
        sentiment = classify_sentiment(text)
        metadata = extract_review_metadata(review_data)
        profanity_flag = review_data.get("profanity", {}).get("is_profane", False)
        table_name = get_table_name()

        save_to_dynamodb(
            table_name=table_name,
            key=key,
            metadata=metadata,
            sentiment=sentiment,
            profanity_flag=profanity_flag,
        )

        print(
            json.dumps(
                {
                    "bucket": bucket,
                    "key": key,
                    "text": text,
                    "sentiment": sentiment,
                }
            )
        )

        processed.append(
            {
                "bucket": bucket,
                "key": key,
                "sentiment": sentiment,
                "overall": metadata.get("overall"),
                "profanityFlag": profanity_flag,
            }
        )

    return {
        "statusCode": 200,
        "processed": processed,
    }

