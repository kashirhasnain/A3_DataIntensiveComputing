
import os
import boto3
from botocore.exceptions import ClientError

ENDPOINT  = "http://localhost:4566"
REGION    = "us-east-1"

os.environ["AWS_ACCESS_KEY_ID"]     = "test"
os.environ["AWS_SECRET_ACCESS_KEY"] = "test"
os.environ["AWS_DEFAULT_REGION"]    = REGION

s3  = boto3.client("s3",     endpoint_url=ENDPOINT, region_name=REGION)
lam = boto3.client("lambda", endpoint_url=ENDPOINT, region_name=REGION)

def get_arn(function_name):
    return lam.get_function(FunctionName=function_name)["Configuration"]["FunctionArn"]

def wire(bucket, function_name):
    arn = get_arn(function_name)
    s3.put_bucket_notification_configuration(
        Bucket=bucket,
        NotificationConfiguration={
            "LambdaFunctionConfigurations": [{
                "LambdaFunctionArn": arn,
                "Events": ["s3:ObjectCreated:*"],
            }]
        },
    )
    print(f"  OK  s3://{bucket}  ->  {function_name}")

print("\nRe-wiring S3 triggers ...")
wire("review-raw",              "preprocess")
wire("review-preprocessed",     "profanity_check")
wire("review-profanity-checked","sentiment")
wire("ministack-thumbnails-app-images", "resize")
print("\nDone! All S3 triggers are connected.")
print("Now run:  pytest tests\\test_integration.py -v -s\n")
