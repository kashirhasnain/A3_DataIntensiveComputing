

import json
import os
import time
import typing
import uuid

import boto3
import pytest

if typing.TYPE_CHECKING:
    from mypy_boto3_dynamodb import DynamoDBServiceResource
    from mypy_boto3_s3 import S3Client
    from mypy_boto3_ssm import SSMClient
    from mypy_boto3_lambda import LambdaClient




os.environ.setdefault("AWS_DEFAULT_REGION", "us-east-1")
os.environ.setdefault("AWS_ACCESS_KEY_ID",  "test")
os.environ.setdefault("AWS_SECRET_ACCESS_KEY", "test")

MINISTACK_ENDPOINT = "http://localhost:4566"

s3:        "S3Client"               = boto3.client("s3",     endpoint_url=MINISTACK_ENDPOINT)
ssm:       "SSMClient"              = boto3.client("ssm",    endpoint_url=MINISTACK_ENDPOINT)
awslambda: "LambdaClient"           = boto3.client("lambda", endpoint_url=MINISTACK_ENDPOINT)
dynamodb:  "DynamoDBServiceResource" = boto3.resource(
    "dynamodb", endpoint_url=MINISTACK_ENDPOINT, region_name="us-east-1"
)

# SSM parameter paths — these match what run.sh stores
SSM_RAW_BUCKET        = "/review-analysis/buckets/raw"
SSM_PREPROCESSED      = "/review-analysis/buckets/preprocessed"
SSM_PROFANITY_BUCKET  = "/review-analysis/buckets/profanity-checked"
SSM_REVIEWS_TABLE     = "/review-analysis/tables/reviews"
SSM_IMPOLITE_TABLE    = "/review-analysis/tables/impolite-counts"
SSM_BANNED_TABLE      = "/review-analysis/tables/banned-customers"

# Wait times for each async Lambda hop (seconds)
S3_WAIT_TIMEOUT     = 60   # seconds per S3 hop (one Lambda)
DYNAMO_WAIT_TIMEOUT = 90   # seconds to wait for DynamoDB item after all hops




def get_ssm_value(param_name: str) -> str:
    """Read a value from AWS SSM Parameter Store."""
    return ssm.get_parameter(Name=param_name)["Parameter"]["Value"]


def upload_review_to_s3(bucket: str, key: str, review: dict) -> None:
    """
    Upload a single review as a JSON file to an S3 bucket.
    This is what triggers the preprocess Lambda automatically.
    """
    print(f"\n  → Uploading review to s3://{bucket}/{key}")
    s3.put_object(
        Bucket=bucket,
        Key=key,
        Body=json.dumps(review).encode("utf-8"),
        ContentType="application/json",
    )


def wait_for_s3_result(bucket: str, key: str, stage_name: str,
                       timeout: int = S3_WAIT_TIMEOUT) -> dict:
    """
    Poll S3 until the Lambda has written its output file, then return
    the parsed JSON contents. Prints progress so you can see what's happening.
    """
    print(f"  ⏳ Waiting for {stage_name} result in s3://{bucket}/{key} ...")
    waiter = s3.get_waiter("object_exists")
    waiter.wait(
        Bucket=bucket,
        Key=key,
        WaiterConfig={"Delay": 2, "MaxAttempts": timeout // 2},
    )
    response = s3.get_object(Bucket=bucket, Key=key)
    data = json.loads(response["Body"].read().decode("utf-8"))
    print(f"  ✅ {stage_name} result received.")
    return data


def wait_for_dynamodb_item(table_name: str, key_field: str, key_value: str,
                           timeout: int = DYNAMO_WAIT_TIMEOUT) -> dict:
   
    print(f"  ⏳ Waiting for DynamoDB item  [{key_field}={key_value!r}]  in table '{table_name}' ...")
    table = dynamodb.Table(table_name)
    deadline = time.time() + timeout
    while time.time() < deadline:
        response = table.get_item(Key={key_field: key_value})
        item = response.get("Item")
        if item:
            print(f"  ✅ DynamoDB item found: {item}")
            return item
        time.sleep(2)
    raise TimeoutError(
        f"Timed out after {timeout}s — item [{key_field}={key_value!r}] "
        f"never appeared in DynamoDB table '{table_name}'"
    )


def delete_s3_objects(*bucket_key_pairs: tuple) -> None:
    """Remove test S3 objects after a test. Errors are ignored."""
    for bucket, key in bucket_key_pairs:
        try:
            s3.delete_object(Bucket=bucket, Key=key)
        except Exception:
            pass


def delete_dynamodb_item(table_name: str, key_field: str, key_value: str) -> None:
    """Remove a test DynamoDB item after a test. Errors are ignored."""
    try:
        dynamodb.Table(table_name).delete_item(Key={key_field: key_value})
    except Exception:
        pass




@pytest.fixture(scope="session")
def resources() -> dict:
    """
    Reads all bucket and table names from SSM Parameter Store once,
    and shares them with every test in the session.
    This means no bucket name is hardcoded — everything comes from SSM.
    """
    print("\n\n📋 Reading resource names from SSM Parameter Store...")
    r = {
        "raw_bucket":        get_ssm_value(SSM_RAW_BUCKET),
        "preprocessed_bucket": get_ssm_value(SSM_PREPROCESSED),
        "profanity_bucket":  get_ssm_value(SSM_PROFANITY_BUCKET),
        "reviews_table":     get_ssm_value(SSM_REVIEWS_TABLE),
        "impolite_table":    get_ssm_value(SSM_IMPOLITE_TABLE),
        "banned_table":      get_ssm_value(SSM_BANNED_TABLE),
    }
    print(f"   Raw bucket:          {r['raw_bucket']}")
    print(f"   Preprocessed bucket: {r['preprocessed_bucket']}")
    print(f"   Profanity bucket:    {r['profanity_bucket']}")
    print(f"   Reviews table:       {r['reviews_table']}")
    print(f"   Impolite table:      {r['impolite_table']}")
    print(f"   Banned table:        {r['banned_table']}")
    return r


@pytest.fixture(autouse=True, scope="session")
def wait_for_lambdas_to_be_ready():
    """
    Blocks all tests from starting until all three Lambda functions
    are deployed and in Active state. Prevents flaky failures on first run.
    """
    print("\n🔄 Waiting for Lambda functions to be ready...")
    for fn_name in ("preprocess", "profanity_check", "sentiment"):
        print(f"   Checking: {fn_name} ...")
        awslambda.get_waiter("function_active").wait(FunctionName=fn_name)
        print(f"   ✅ {fn_name} is active.")



# TEST 1 — PREPROCESSING


class TestPreprocessing:
 

    def test_tokens_stopwords_and_lemmatization(self, resources):
        print("\n" + "="*60)
        print("TEST 1 — Preprocessing: tokens, stop words, lemmatization")
        print("="*60)

        key = f"test-preprocess-{uuid.uuid4()}.json"

        
        review = {
            "reviewerID": "TEST_USER_PREPROCESS",
            "asin":       "TEST_PRODUCT",
            "summary":    "Delish",
            "reviewText": "Great running shoes",
            "overall":    5.0,
        }

        raw_bucket          = resources["raw_bucket"]
        preprocessed_bucket = resources["preprocessed_bucket"]

        try:
            # Step 1: Upload the review — this triggers the preprocess Lambda
            upload_review_to_s3(raw_bucket, key, review)

            # Step 2: Wait for the preprocessed result to appear in the next bucket
            result = wait_for_s3_result(
                preprocessed_bucket, key, stage_name="Preprocessing"
            )
            preprocessed = result["preprocessed"]
            print(f"\n  Preprocessed output: {json.dumps(preprocessed, indent=4)}")

            # --- CHECK 1: overall should be stored as ["5"] ---
            assert preprocessed["overall"] == ["5"], (
                f"FAIL: overall should be ['5'] but got {preprocessed['overall']}"
            )
            print("  ✔ overall rating stored correctly as ['5']")

            # --- CHECK 2: stop words must be gone ---
            all_tokens = preprocessed.get("summary", []) + preprocessed.get("reviewText", [])
            stop_words_present = [t for t in all_tokens if t in {"is", "a", "the", "very", "it"}]
            assert not stop_words_present, (
                f"FAIL: stop words still in output — {stop_words_present}"
            )
            print("  ✔ Stop words correctly removed")

            # --- CHECK 3: all tokens must be lowercase ---
            uppercase_tokens = [t for t in all_tokens if t != t.lower()]
            assert not uppercase_tokens, (
                f"FAIL: tokens are not lowercase — {uppercase_tokens}"
            )
            print("  ✔ All tokens are lowercase")

            # --- CHECK 4: lemmatization — "shoes" must become "shoe" ---
            review_tokens = preprocessed.get("reviewText", [])
            assert "shoe" in review_tokens, (
                f"FAIL: expected lemmatized 'shoe' in reviewText tokens, got {review_tokens}"
            )
            print("  ✔ Lemmatization correct: 'shoes' → 'shoe'")

            # --- CHECK 5: "delish" must be in summary tokens ---
            summary_tokens = preprocessed.get("summary", [])
            assert "delish" in summary_tokens, (
                f"FAIL: expected 'delish' in summary tokens, got {summary_tokens}"
            )
            print("  ✔ Summary token 'delish' preserved correctly")

        finally:
            delete_s3_objects((raw_bucket, key), (preprocessed_bucket, key))



# TEST 2 — PROFANITY CHECK


class TestProfanityCheck:
  

    def test_review_with_bad_words_is_flagged(self, resources):
        print("\n" + "="*60)
        print("TEST 2A — Profanity: bad review should be flagged")
        print("="*60)

        key = f"test-profane-{uuid.uuid4()}.json"

        review = {
            "reviewerID": "TEST_USER_PROFANE",
            "asin":       "TEST_PRODUCT",
            "summary":    "This is damn crap",
            "reviewText": "Absolute crap product, never buy this shit",
            "overall":    1.0,
        }

        raw_bucket     = resources["raw_bucket"]
        preprocessed   = resources["preprocessed_bucket"]
        profanity_bucket = resources["profanity_bucket"]

        try:
            upload_review_to_s3(raw_bucket, key, review)

            # Wait for two Lambda hops: preprocess → profanity_check
            result = wait_for_s3_result(
                profanity_bucket, key, stage_name="Profanity Check", timeout=60
            )

            profanity_info = result.get("profanity", {})
            print(f"\n  Profanity result: {json.dumps(profanity_info, indent=4)}")

            assert profanity_info.get("is_profane") is True, (
                f"FAIL: review with bad words should have is_profane=True, "
                f"got {profanity_info}"
            )
            print("  ✔ Profane review correctly flagged as is_profane=True")

        finally:
            delete_s3_objects(
                (raw_bucket, key), (preprocessed, key), (profanity_bucket, key)
            )

    def test_clean_review_is_not_flagged(self, resources):
        print("\n" + "="*60)
        print("TEST 2B — Profanity: clean review should NOT be flagged")
        print("="*60)

        key = f"test-clean-{uuid.uuid4()}.json"

        review = {
            "reviewerID": "TEST_USER_CLEAN",
            "asin":       "TEST_PRODUCT",
            "summary":    "Excellent product",
            "reviewText": "Highly recommend this to everyone. Works perfectly.",
            "overall":    5.0,
        }

        raw_bucket      = resources["raw_bucket"]
        preprocessed    = resources["preprocessed_bucket"]
        profanity_bucket = resources["profanity_bucket"]

        try:
            upload_review_to_s3(raw_bucket, key, review)

            result = wait_for_s3_result(
                profanity_bucket, key, stage_name="Profanity Check", timeout=60
            )

            profanity_info = result.get("profanity", {})
            print(f"\n  Profanity result: {json.dumps(profanity_info, indent=4)}")

            assert profanity_info.get("is_profane") is False, (
                f"FAIL: clean review should have is_profane=False, got {profanity_info}"
            )
            print("  ✔ Clean review correctly passed through as is_profane=False")

        finally:
            delete_s3_objects(
                (raw_bucket, key), (preprocessed, key), (profanity_bucket, key)
            )



# TEST 3 — SENTIMENT ANALYSIS


class TestSentimentAnalysis:
   

    def _run_full_pipeline_and_get_dynamo_item(
        self, resources: dict, review: dict, key: str
    ) -> dict:
       
        raw_bucket       = resources["raw_bucket"]
        preprocessed     = resources["preprocessed_bucket"]
        profanity_bucket = resources["profanity_bucket"]
        reviews_table    = resources["reviews_table"]

        try:
            # Hop 1
            upload_review_to_s3(raw_bucket, key, review)

            # Hop 2: wait for preprocess to finish
            wait_for_s3_result(
                preprocessed, key,
                stage_name="Preprocessing (hop 1/3)",
                timeout=S3_WAIT_TIMEOUT,
            )

            # Hop 3: wait for profanity_check to finish
            wait_for_s3_result(
                profanity_bucket, key,
                stage_name="Profanity Check (hop 2/3)",
                timeout=S3_WAIT_TIMEOUT,
            )

            # Hop 4: wait for sentiment Lambda to write to DynamoDB
            print("  Waiting for Sentiment Lambda -> DynamoDB (hop 3/3) ...")
            item = wait_for_dynamodb_item(
                reviews_table, "reviewId", key,
                timeout=DYNAMO_WAIT_TIMEOUT,
            )
            return item

        finally:
            delete_s3_objects(
                (raw_bucket, key), (preprocessed, key), (profanity_bucket, key)
            )
            delete_dynamodb_item(reviews_table, "reviewId", key)

    def test_positive_review_gets_positive_sentiment(self, resources):
        print("\n" + "="*60)
        print("TEST 3A — Sentiment: clearly positive review → 'positive'")
        print("="*60)

        key = f"test-positive-{uuid.uuid4()}.json"
        review = {
            "reviewerID": "TEST_USER_POS",
            "asin":       "TEST_PRODUCT",
            "summary":    "Absolutely love this! Outstanding quality!",
            "reviewText": (
                "This is fantastic! Best purchase I have ever made. "
                "Highly recommend to everyone. Wonderful, amazing, superb!"
            ),
            "overall": 5.0,
        }

        item = self._run_full_pipeline_and_get_dynamo_item(resources, review, key)
        print(f"\n  DynamoDB item: {json.dumps(dict(item), indent=4, default=str)}")

        assert item.get("sentiment") == "positive", (
            f"FAIL: expected sentiment='positive' for a clearly positive review, "
            f"got '{item.get('sentiment')}'"
        )
        print("  ✔ Positive review correctly classified as 'positive'")

    def test_negative_review_gets_negative_sentiment(self, resources):
        print("\n" + "="*60)
        print("TEST 3B — Sentiment: clearly negative review → 'negative'")
        print("="*60)

        key = f"test-negative-{uuid.uuid4()}.json"
        review = {
            "reviewerID": "TEST_USER_NEG",
            "asin":       "TEST_PRODUCT",
            "summary":    "Terrible product, complete waste of money",
            "reviewText": (
                "Absolutely horrible. Broke on the first day. "
                "Worst purchase ever. Disgusting quality, totally useless."
            ),
            "overall": 1.0,
        }

        item = self._run_full_pipeline_and_get_dynamo_item(resources, review, key)
        print(f"\n  DynamoDB item: {json.dumps(dict(item), indent=4, default=str)}")

        assert item.get("sentiment") == "negative", (
            f"FAIL: expected sentiment='negative' for a clearly negative review, "
            f"got '{item.get('sentiment')}'"
        )
        print("  ✔ Negative review correctly classified as 'negative'")

    def test_dynamo_item_has_all_required_fields(self, resources):
        print("\n" + "="*60)
        print("TEST 3C — Sentiment: DynamoDB item has all required fields")
        print("="*60)

        key = f"test-fields-{uuid.uuid4()}.json"
        review = {
            "reviewerID": "TEST_USER_FIELDS",
            "asin":       "TEST_PRODUCT",
            "summary":    "Pretty decent product",
            "reviewText": "It works okay. Nothing special but does the job.",
            "overall":    3.0,
        }

        raw_bucket       = resources["raw_bucket"]
        preprocessed     = resources["preprocessed_bucket"]
        profanity_bucket = resources["profanity_bucket"]
        reviews_table    = resources["reviews_table"]

        try:
            upload_review_to_s3(raw_bucket, key, review)
            item = wait_for_dynamodb_item(reviews_table, "reviewId", key)
            print(f"\n  DynamoDB item: {json.dumps(dict(item), indent=4, default=str)}")

            # Check: 'sentiment' field exists and is valid
            assert "sentiment" in item, (
                f"FAIL: 'sentiment' field missing from DynamoDB item: {item}"
            )
            assert item["sentiment"] in ("positive", "neutral", "negative"), (
                f"FAIL: 'sentiment' has unexpected value: {item['sentiment']!r}"
            )
            print(f"  ✔ 'sentiment' field present: {item['sentiment']!r}")

            # Check: 'profanityFlag' field exists and is boolean
            assert "profanityFlag" in item, (
                f"FAIL: 'profanityFlag' field missing from DynamoDB item: {item}"
            )
            assert isinstance(item["profanityFlag"], bool), (
                f"FAIL: 'profanityFlag' should be True/False, got {type(item['profanityFlag'])}"
            )
            print(f"  ✔ 'profanityFlag' field present: {item['profanityFlag']}")

        finally:
            delete_s3_objects(
                (raw_bucket, key), (preprocessed, key), (profanity_bucket, key)
            )
            delete_dynamodb_item(reviews_table, "reviewId", key)


# TEST 4 — IMPOLITE REVIEW COUNTER


class TestImpoliteCount:
   

    REVIEWER_ID = "TEST_IMPOLITE_USER_001"

    def _make_profane_review(self) -> dict:
        return {
            "reviewerID": self.REVIEWER_ID,
            "asin":       "TEST_PRODUCT",
            "summary":    "This is crap",
            "reviewText": "Absolute crap, total crap product",
            "overall":    1.0,
        }

    def test_three_profane_reviews_give_count_of_three(self, resources):
        print("\n" + "="*60)
        print("TEST 4 — Impolite counter: 3 bad reviews → count = 3")
        print("="*60)

        raw_bucket       = resources["raw_bucket"]
        preprocessed     = resources["preprocessed_bucket"]
        profanity_bucket = resources["profanity_bucket"]
        impolite_table   = resources["impolite_table"]

        # Reset any leftover count from previous test runs
        delete_dynamodb_item(impolite_table, "reviewerID", self.REVIEWER_ID)
        print(f"\n  Reviewer ID being tested: {self.REVIEWER_ID}")

        keys_uploaded = []
        try:
            for review_number in range(1, 4):   # 1, 2, 3
                key = f"test-impolite-review-{review_number}-{uuid.uuid4()}.json"
                keys_uploaded.append(key)

                print(f"\n  ── Uploading profane review #{review_number} of 3 ──")
                upload_review_to_s3(raw_bucket, key, self._make_profane_review())

                # Wait for this review to reach the profanity bucket before uploading the next
                wait_for_s3_result(
                    profanity_bucket, key,
                    stage_name=f"Profanity Check (review {review_number})",
                    timeout=60,
                )
                time.sleep(2)  # give the Lambda a moment to write DynamoDB

            # Now check the counter in DynamoDB
            print("\n  Checking impolite_count in DynamoDB...")
            item = wait_for_dynamodb_item(
                impolite_table, "reviewerID", self.REVIEWER_ID, timeout=30
            )
            count = int(item.get("impolite_count", 0))
            print(f"\n  impolite_count = {count}")

            assert count == 3, (
                f"FAIL: expected impolite_count=3 after 3 profane reviews, got {count}"
            )
            print("  ✔ impolite_count correctly reached 3 after 3 bad reviews")

        finally:
            for k in keys_uploaded:
                delete_s3_objects(
                    (raw_bucket, k), (preprocessed, k), (profanity_bucket, k)
                )
            delete_dynamodb_item(impolite_table, "reviewerID", self.REVIEWER_ID)


# TEST 5 — BAN LOGIC

class TestBanLogic:
   
    REVIEWER_ID = "TEST_BAN_USER_001"

    def _make_profane_review(self) -> dict:
        return {
            "reviewerID": self.REVIEWER_ID,
            "asin":       "TEST_PRODUCT",
            "summary":    "Absolute crap",
            "reviewText": "This is crap and totally useless crap",
            "overall":    1.0,
        }

    def test_fourth_profane_review_bans_the_reviewer(self, resources):
        print("\n" + "="*60)
        print("TEST 5 — Ban logic: 4th bad review → reviewer is banned")
        print("="*60)

        raw_bucket       = resources["raw_bucket"]
        preprocessed     = resources["preprocessed_bucket"]
        profanity_bucket = resources["profanity_bucket"]
        impolite_table   = resources["impolite_table"]
        banned_table     = resources["banned_table"]

        # Reset any leftover state from previous test runs
        delete_dynamodb_item(impolite_table, "reviewerID", self.REVIEWER_ID)
        delete_dynamodb_item(banned_table,   "reviewerID", self.REVIEWER_ID)
        print(f"\n  Reviewer ID being tested: {self.REVIEWER_ID}")

        keys_uploaded = []
        try:
            for review_number in range(1, 5):   # 1, 2, 3, 4
                key = f"test-ban-review-{review_number}-{uuid.uuid4()}.json"
                keys_uploaded.append(key)

                print(f"\n  ── Uploading profane review #{review_number} of 4 ──")
                upload_review_to_s3(raw_bucket, key, self._make_profane_review())

                wait_for_s3_result(
                    profanity_bucket, key,
                    stage_name=f"Profanity Check (review {review_number})",
                    timeout=60,
                )
                time.sleep(2)

            # Check 1: reviewer must be in the banned table
            print("\n  Checking banned-customers table in DynamoDB...")
            banned_item = wait_for_dynamodb_item(
                banned_table, "reviewerID", self.REVIEWER_ID, timeout=30
            )
            print(f"  Banned item: {banned_item}")

            assert banned_item.get("banned") is True, (
                f"FAIL: expected banned=True after 4th profane review, "
                f"got item={banned_item}"
            )
            print("  ✔ Reviewer correctly marked as banned=True")

            # Check 2: impolite count must be >= 4
            print("\n  Checking impolite-counts table in DynamoDB...")
            impolite_item = wait_for_dynamodb_item(
                impolite_table, "reviewerID", self.REVIEWER_ID, timeout=10
            )
            count = int(impolite_item.get("impolite_count", 0))
            print(f"  impolite_count = {count}")

            assert count >= 4, (
                f"FAIL: expected impolite_count >= 4 when reviewer is banned, got {count}"
            )
            print(f"  ✔ impolite_count = {count} (>= 4 as expected)")

        finally:
            for k in keys_uploaded:
                delete_s3_objects(
                    (raw_bucket, k), (preprocessed, k), (profanity_bucket, k)
                )
            delete_dynamodb_item(impolite_table, "reviewerID", self.REVIEWER_ID)
            delete_dynamodb_item(banned_table,   "reviewerID", self.REVIEWER_ID)
