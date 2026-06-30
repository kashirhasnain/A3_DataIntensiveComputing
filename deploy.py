

import io
import json
import os
import subprocess
import sys
import zipfile

import boto3
from botocore.exceptions import ClientError

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------
ENDPOINT = "http://localhost:4566"
REGION   = "us-east-1"

os.environ["AWS_ACCESS_KEY_ID"]     = "test"
os.environ["AWS_SECRET_ACCESS_KEY"] = "test"
os.environ["AWS_DEFAULT_REGION"]    = REGION

PROJECT_ROOT = os.path.dirname(os.path.abspath(__file__))

# ---------------------------------------------------------------------------
# boto3 clients
# ---------------------------------------------------------------------------
s3       = boto3.client("s3",       endpoint_url=ENDPOINT, region_name=REGION)
ssm      = boto3.client("ssm",      endpoint_url=ENDPOINT, region_name=REGION)
lam      = boto3.client("lambda",   endpoint_url=ENDPOINT, region_name=REGION)
dynamodb = boto3.client("dynamodb", endpoint_url=ENDPOINT, region_name=REGION)
s3res    = boto3.resource("s3",     endpoint_url=ENDPOINT, region_name=REGION)

ROLE = "arn:aws:iam::000000000000:role/lambda-role"
ALLOWED_ORIGINS = "http://localhost:4566,http://127.0.0.1:4566,https://lbd.tuwien.ac.at"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def header(msg):
    print(f"\n{'='*60}")
    print(f"  {msg}")
    print(f"{'='*60}")


def ok(msg):
    print(f"  ✅ {msg}")


def info(msg):
    print(f"  → {msg}")


def make_bucket(name):
    try:
        s3.create_bucket(Bucket=name)
        ok(f"Created bucket: {name}")
    except ClientError as e:
        if e.response["Error"]["Code"] in ("BucketAlreadyExists", "BucketAlreadyOwnedByYou"):
            info(f"Bucket already exists: {name}")
        else:
            raise


def put_ssm(name, value):
    ssm.put_parameter(Name=name, Value=value, Type="String", Overwrite=True)
    ok(f"SSM: {name} = {value}")


def create_table(table_name, key_name):
    try:
        dynamodb.create_table(
            TableName=table_name,
            AttributeDefinitions=[{"AttributeName": key_name, "AttributeType": "S"}],
            KeySchema=[{"AttributeName": key_name, "KeyType": "HASH"}],
            BillingMode="PAY_PER_REQUEST",
        )
        ok(f"DynamoDB table created: {table_name}")
    except ClientError as e:
        if e.response["Error"]["Code"] == "ResourceInUseException":
            info(f"DynamoDB table already exists: {table_name}")
        else:
            raise


def build_lambda_zip(lambda_dir: str, nltk_corpora: list = None) -> bytes:
    
    abs_dir  = os.path.join(PROJECT_ROOT, lambda_dir)
    pkg_dir  = os.path.join(abs_dir, "package")
    req_file = os.path.join(abs_dir, "requirements.txt")

    info(f"Packaging {lambda_dir} ...")

    # Clean previous build
    if os.path.isdir(pkg_dir):
        import shutil
        shutil.rmtree(pkg_dir)
    os.makedirs(pkg_dir, exist_ok=True)

    # Install pip packages
    if os.path.isfile(req_file):
        info("  Installing pip dependencies ...")
        subprocess.check_call(
            [sys.executable, "-m", "pip", "install",
             "-r", req_file, "-t", pkg_dir, "--quiet"],
        )

    # Download NLTK corpora
    if nltk_corpora:
        nltk_data_dir = os.path.join(pkg_dir, "nltk_data")
        os.makedirs(nltk_data_dir, exist_ok=True)
        for corpus in nltk_corpora:
            info(f"  Downloading NLTK corpus: {corpus} ...")
            env = os.environ.copy()
            env["PYTHONPATH"] = pkg_dir
            subprocess.check_call(
                [sys.executable, "-m", "nltk.downloader",
                 "-d", nltk_data_dir, corpus],
                env=env,
            )

    # Build zip in memory
    info("  Creating lambda.zip in memory ...")
    buf = io.BytesIO()
    skip_dirs = {"__pycache__", ".dist-info", ".pytest_cache"}
    skip_exts = {".pyc", ".pyo"}

    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        # Add handler.py at the root
        handler_path = os.path.join(abs_dir, "handler.py")
        zf.write(handler_path, "handler.py")
        info("  Added: handler.py")

        # Add everything from package/ at the root (not under package/)
        for root, dirs, files in os.walk(pkg_dir):
            # Skip useless dirs
            dirs[:] = [d for d in dirs
                       if d not in skip_dirs
                       and not d.endswith(".dist-info")]
            for filename in files:
                if os.path.splitext(filename)[1] in skip_exts:
                    continue
                filepath = os.path.join(root, filename)
                arcname  = os.path.relpath(filepath, pkg_dir)
                zf.write(filepath, arcname)

    size_kb = buf.tell() // 1024
    info(f"  zip size: {size_kb} KB")
    buf.seek(0)
    return buf.read()


def deploy_lambda(name, zip_bytes, handler="handler.handler",
                  timeout=30, environment=None):
    env_vars = environment or {"STAGE": "local"}
    try:
        lam.create_function(
            FunctionName=name,
            Runtime="python3.11",
            Role=ROLE,
            Handler=handler,
            Code={"ZipFile": zip_bytes},
            Timeout=timeout,
            Environment={"Variables": env_vars},
        )
        ok(f"Lambda deployed: {name}")
    except ClientError as e:
        if e.response["Error"]["Code"] == "ResourceConflictException":
            info(f"Lambda exists, updating code: {name}")
            lam.update_function_code(FunctionName=name, ZipFile=zip_bytes)
            ok(f"Lambda code updated: {name}")
        else:
            raise


def get_lambda_arn(name):
    resp = lam.get_function(FunctionName=name)
    return resp["Configuration"]["FunctionArn"]


def connect_s3_to_lambda(bucket, lambda_arn):
    s3.put_bucket_notification_configuration(
        Bucket=bucket,
        NotificationConfiguration={
            "LambdaFunctionConfigurations": [
                {
                    "LambdaFunctionArn": lambda_arn,
                    "Events": ["s3:ObjectCreated:*"],
                }
            ]
        },
    )
    ok(f"S3 trigger: s3://{bucket} → {lambda_arn.split(':')[-1]}")


def create_function_url(name):
    try:
        lam.create_function_url_config(FunctionName=name, AuthType="NONE")
        ok(f"Function URL created: {name}")
    except ClientError as e:
        if "already exists" in str(e).lower():
            info(f"Function URL already exists: {name}")
        else:
            raise


# ---------------------------------------------------------------------------
# MAIN DEPLOYMENT
# ---------------------------------------------------------------------------

def main():
    print("\n🚀 Starting deployment to MiniStack ...")
    print(f"   Endpoint: {ENDPOINT}")
    print(f"   Project:  {PROJECT_ROOT}")

    # ── STEP 1: S3 Buckets ──────────────────────────────────────────────────
    header("STEP 1: Creating S3 Buckets")
    for bucket in [
        "ministack-thumbnails-app-images",
        "ministack-thumbnails-app-resized",
        "review-raw",
        "review-preprocessed",
        "review-profanity-checked",
        "webapp",
    ]:
        make_bucket(bucket)

    # ── STEP 2: SSM Parameters ──────────────────────────────────────────────
    header("STEP 2: Writing SSM Parameters")
    put_ssm("/ministack-thumbnail-app/buckets/images",   "ministack-thumbnails-app-images")
    put_ssm("/ministack-thumbnail-app/buckets/resized",  "ministack-thumbnails-app-resized")
    put_ssm("/review-analysis/buckets/raw",              "review-raw")
    put_ssm("/review-analysis/buckets/preprocessed",     "review-preprocessed")
    put_ssm("/review-analysis/buckets/profanity-checked","review-profanity-checked")
    put_ssm("/review-analysis/tables/reviews",           "review-results")
    put_ssm("/review-analysis/tables/impolite-counts",   "review-impolite-counts")
    put_ssm("/review-analysis/tables/banned-customers",  "review-banned-customers")

    # ── STEP 3: DynamoDB Tables ─────────────────────────────────────────────
    header("STEP 3: Creating DynamoDB Tables")
    create_table("review-results",          "reviewId")
    create_table("review-impolite-counts",  "reviewerID")
    create_table("review-banned-customers", "reviewerID")

    # ── STEP 4: Lambda — presign ─────────────────────────────────────────────
    header("STEP 4a: Lambda — presign")
    presign_zip = build_lambda_zip("lambdas/presign")
    deploy_lambda("presign", presign_zip, timeout=10,
                  environment={"STAGE": "local", "ALLOWED_ORIGINS": ALLOWED_ORIGINS})
    create_function_url("presign")

    # ── STEP 5: Lambda — list ────────────────────────────────────────────────
    header("STEP 4b: Lambda — list")
    list_zip = build_lambda_zip("lambdas/list")
    deploy_lambda("list", list_zip, timeout=10,
                  environment={"STAGE": "local", "ALLOWED_ORIGINS": ALLOWED_ORIGINS})
    create_function_url("list")

    # ── STEP 6: Lambda — resize ──────────────────────────────────────────────
    header("STEP 4c: Lambda — resize")
    resize_zip = build_lambda_zip("lambdas/resize")
    deploy_lambda("resize", resize_zip, timeout=10)
    resize_arn = get_lambda_arn("resize")
    connect_s3_to_lambda("ministack-thumbnails-app-images", resize_arn)

    # ── STEP 7: Lambda — preprocess ──────────────────────────────────────────
    header("STEP 4d: Lambda — preprocess  (downloads NLTK data, takes ~1 min)")
    preprocess_zip = build_lambda_zip(
        "lambdas/preprocess",
        nltk_corpora=["punkt", "punkt_tab", "stopwords", "wordnet", "omw-1.4"],
    )
    deploy_lambda("preprocess", preprocess_zip, timeout=30,
                  environment={"STAGE": "local", "NLTK_DATA": "/var/task/nltk_data"})
    preprocess_arn = get_lambda_arn("preprocess")
    connect_s3_to_lambda("review-raw", preprocess_arn)

    # ── STEP 8: Lambda — sentiment ───────────────────────────────────────────
    header("STEP 4e: Lambda — sentiment  (downloads VADER lexicon)")
    sentiment_zip = build_lambda_zip(
        "lambdas/sentiment",
        nltk_corpora=["vader_lexicon"],
    )
    deploy_lambda("sentiment", sentiment_zip, timeout=30,
                  environment={"STAGE": "local", "NLTK_DATA": "/var/task/nltk_data"})
    sentiment_arn = get_lambda_arn("sentiment")

    # ── STEP 9: Lambda — profanity_check ─────────────────────────────────────
    header("STEP 4f: Lambda — profanity_check")
    profanity_zip = build_lambda_zip("lambdas/profanity_check")
    deploy_lambda("profanity_check", profanity_zip, timeout=30)
    profanity_arn = get_lambda_arn("profanity_check")
    connect_s3_to_lambda("review-preprocessed",    profanity_arn)
    connect_s3_to_lambda("review-profanity-checked", sentiment_arn)

    # ── STEP 10: Upload webapp ───────────────────────────────────────────────
    header("STEP 5: Uploading web app")
    website_dir = os.path.join(PROJECT_ROOT, "website")
    if os.path.isdir(website_dir):
        s3.put_bucket_website(
            Bucket="webapp",
            WebsiteConfiguration={"IndexDocument": {"Suffix": "index.html"}},
        )
        for fname in os.listdir(website_dir):
            fpath = os.path.join(website_dir, fname)
            if os.path.isfile(fpath):
                content_type = (
                    "text/html"       if fname.endswith(".html") else
                    "application/javascript" if fname.endswith(".js") else
                    "text/css"        if fname.endswith(".css")  else
                    "image/x-icon"    if fname.endswith(".ico")  else
                    "application/octet-stream"
                )
                with open(fpath, "rb") as f:
                    s3.put_object(Bucket="webapp", Key=fname,
                                  Body=f.read(), ContentType=content_type)
                ok(f"Uploaded: {fname}")
    else:
        info("No website/ folder found, skipping.")

    # ── Done ─────────────────────────────────────────────────────────────────
    print()
    print("=" * 60)
    print("  ✅ DEPLOYMENT COMPLETE!")
    print("=" * 60)
    print(f"  Web app:  {ENDPOINT}/webapp/index.html")
    print()
    print("  Now run the integration tests:")
    print("    pytest tests\\test_integration.py -v -s")
    print()


if __name__ == "__main__":
    main()
