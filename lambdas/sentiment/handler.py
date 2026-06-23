import json
import os
import typing
from urllib.parse import unquote_plus

import boto3
from nltk.sentiment import SentimentIntensityAnalyzer

if typing.TYPE_CHECKING:
    from mypy_boto3_s3 import S3Client
    from mypy_boto3_ssm import SSMClient

endpoint_url = None
if os.getenv("STAGE") == "local":
    endpoint_url = "http://localhost:4566"

s3: "S3Client" = boto3.client("s3", endpoint_url=endpoint_url)
ssm: "SSMClient" = boto3.client("ssm", endpoint_url=endpoint_url)

sia = SentimentIntensityAnalyzer()



PROFANITY_BUCKET_PARAMETER = os.getenv(
    "PROFANITY_BUCKET_PARAMETER",
    "/review-analysis/buckets/profanity",
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



def classify_sentiment(text: str) -> str:
    scores = sia.polarity_scores(text)
    compound = scores["compound"]

    if compound >= 0.05:
        return "positive"
    elif compound <= -0.05:
        return "negative"
    else:
        return "neutral"
    

def handler(event, context):
    print(json.dumps(event))

    return {
        "statusCode": 200,
        "message": "Sentiment Lambda triggered"
    }