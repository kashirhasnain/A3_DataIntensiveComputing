# What Changed

## New Preprocessing Lambda

Added:

```text
lambdas/preprocess/handler.py
```

This Lambda runs when a new review JSON file is uploaded to S3.

It does this:

1. Reads the review from S3.
2. Gets these fields:
   - `summary`
   - `reviewText`
   - `overall`
3. Tokenizes text with NLTK `word_tokenize`.
4. Removes English stop words.
5. Lemmatizes words with `WordNetLemmatizer`.
6. Writes the result to another S3 bucket.

Example output:

```json
{
  "preprocessed": {
    "summary": ["delish"],
    "reviewText": ["gift", "husband", "food"],
    "overall": ["5"]
  }
}
```

## New Dependency

Added:

```text
lambdas/preprocess/requirements.txt
```

It contains:

```text
nltk==3.9.1
```

## `run.sh` Changes

`run.sh` now also:

- creates `review-raw`
- creates `review-preprocessed`
- stores both bucket names in SSM
- packages and deploys the `preprocess` Lambda
- downloads required NLTK data
- connects `review-raw` uploads to the Lambda trigger

SSM parameters:

```text
/review-analysis/buckets/raw
/review-analysis/buckets/preprocessed
```

## How To Test

Start MiniStack, then run:

```bash
bash run.sh
```

Upload one review:

```bash
head -n 1 assets/reviews_devset.json > sample-review.json
python -m awscli --endpoint-url=http://localhost:4566 s3 cp sample-review.json s3://review-raw/sample-review.json
```

Check the result:

```bash
sleep 5
python -m awscli --endpoint-url=http://localhost:4566 s3 cp s3://review-preprocessed/sample-review.json -
```

## Note

`reviews_devset.json` has one review per line. Upload one line as one S3 object.
