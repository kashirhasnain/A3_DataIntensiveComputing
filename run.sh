#!/usr/bin/env bash
export AWS_ACCESS_KEY_ID=test
export AWS_SECRET_ACCESS_KEY=test
export AWS_DEFAULT_REGION=us-east-1
export ALLOWED_ORIGINS="${ALLOWED_ORIGINS:-http://localhost:4566,http://127.0.0.1:4566,https://lbd.tuwien.ac.at}"
export MINISTACK_ENDPOINT=http://localhost:4566
export MSYS_NO_PATHCONV=1

if aws --version >/dev/null 2>&1; then
  AWS="aws --endpoint-url=${MINISTACK_ENDPOINT}"
elif python -m awscli --version >/dev/null 2>&1; then
  AWS="python -m awscli --endpoint-url=${MINISTACK_ENDPOINT}"
else
  echo "Could not find aws CLI. Install awscli or activate the project environment."
  exit 1
fi

### This check is necessary to allow running the sript both locally and on the LBD cluster over the browser
ON_LBD_PROXY=0
if [ -n "${JUPYTERHUB_USER:-}" ] || [ -n "${JUPYTERHUB_SERVICE_PREFIX:-}" ]; then
  ON_LBD_PROXY=1
fi

if [ -z "${S3_ENDPOINT_URL:-}" ]; then
  if [ "${ON_LBD_PROXY}" -eq 1 ] && [ -n "${USER:-}" ] && [ "${USER}" != "root" ]; then
    export S3_ENDPOINT_URL="https://lbd.tuwien.ac.at/user/${USER}/proxy/4566"
  else
    export S3_ENDPOINT_URL=""
  fi
fi

if [ -z "${PUBLIC_BASE_URL:-}" ]; then
  if [ "${ON_LBD_PROXY}" -eq 1 ] && [ -n "${USER:-}" ] && [ "${USER}" != "root" ]; then
    export PUBLIC_BASE_URL="https://lbd.tuwien.ac.at/user/${USER}/proxy/4566"
  else
    export PUBLIC_BASE_URL="http://localhost:4566"
  fi
fi

### some cors error prevention in the browser
PRESIGN_LIST_ENV="{\"STAGE\":\"local\",\"ALLOWED_ORIGINS\":\"${ALLOWED_ORIGINS}\""
if [ -n "${S3_ENDPOINT_URL}" ]; then
  PRESIGN_LIST_ENV="${PRESIGN_LIST_ENV},\"S3_ENDPOINT_URL\":\"${S3_ENDPOINT_URL}\""
fi
PRESIGN_LIST_ENV="${PRESIGN_LIST_ENV}}"


### Create the buckets
##### The names are completely configurable via SSM:
${AWS} s3 mb s3://ministack-thumbnails-app-images
${AWS} s3 mb s3://ministack-thumbnails-app-resized
${AWS} s3 mb s3://review-raw
${AWS} s3 mb s3://review-preprocessed

### Put the bucket names into the parameter store
${AWS} ssm put-parameter --name /ministack-thumbnail-app/buckets/images --type "String" --value "ministack-thumbnails-app-images"
${AWS} ssm put-parameter --name /ministack-thumbnail-app/buckets/resized --type "String" --value "ministack-thumbnails-app-resized"
${AWS} ssm put-parameter --name /review-analysis/buckets/raw --type "String" --value "review-raw" --overwrite
${AWS} ssm put-parameter --name /review-analysis/buckets/preprocessed --type "String" --value "review-preprocessed" --overwrite

### Create the lambdas
#### S3 pre-signed POST URL generator
##### This Lambda is responsible for generating pre-signed POST URLs to upload files to an S3 bucket.
(cd lambdas/presign; rm -f lambda.zip; zip lambda.zip handler.py)
${AWS} lambda create-function \
 --function-name presign \
 --runtime python3.11 \
 --timeout 10 \
 --zip-file fileb://lambdas/presign/lambda.zip \
 --handler handler.handler \
 --role arn:aws:iam::000000000000:role/lambda-role \
 --environment "{\"Variables\":${PRESIGN_LIST_ENV}}"

##### Create the function URL:
${AWS} lambda create-function-url-config \
 --function-name presign \
 --auth-type NONE

#### Image lister lambda
##### The list Lambda is very similar:
(cd lambdas/list; rm -f lambda.zip; zip lambda.zip handler.py)
${AWS} lambda create-function \
 --function-name list \
 --handler handler.handler \
 --zip-file fileb://lambdas/list/lambda.zip \
 --runtime python3.11 \
 --role arn:aws:iam::000000000000:role/lambda-role \
 --environment "{\"Variables\":${PRESIGN_LIST_ENV}}"

##### Create the function URL:
${AWS} lambda create-function-url-config \
 --function-name list \
 --auth-type NONE

#### Resizer Lambda
(
 cd lambdas/resize
 rm -rf package lambda.zip
 mkdir package
 pip install -r requirements.txt -t package --platform manylinux2014_x86_64 --only-binary=:all:
 zip lambda.zip handler.py
 cd package
 zip -r ../lambda.zip *;
)
${AWS} lambda create-function \
 --function-name resize \
 --runtime python3.11 \
 --timeout 10 \
 --zip-file fileb://lambdas/resize/lambda.zip \
 --handler handler.handler \
 --role arn:aws:iam::000000000000:role/lambda-role \
 --environment "{\"Variables\":{\"STAGE\":\"local\"}}"

RESIZE_ARN=$(${AWS} lambda get-function \
 --function-name resize \
 --query 'Configuration.FunctionArn' \
 --output text)

### Connect the S3 bucket to the resizer lambda
${AWS} s3api put-bucket-notification-configuration \
 --bucket ministack-thumbnails-app-images \
 --notification-configuration "{\"LambdaFunctionConfigurations\":
[{\"LambdaFunctionArn\": \"${RESIZE_ARN}\", \"Events\":
[\"s3:ObjectCreated:*\"]}]}"

#### Review preprocessing Lambda
##### This Lambda preprocesses a single review uploaded to the raw reviews bucket.
(
 cd lambdas/preprocess
 rm -rf package lambda.zip
 mkdir package
 pip install -r requirements.txt -t package
 PYTHONPATH=package python -m nltk.downloader -d package/nltk_data punkt punkt_tab stopwords wordnet omw-1.4
 zip lambda.zip handler.py
 cd package
 zip -r ../lambda.zip *;
)
${AWS} lambda create-function \
 --function-name preprocess \
 --runtime python3.11 \
 --timeout 30 \
 --zip-file fileb://lambdas/preprocess/lambda.zip \
 --handler handler.handler \
 --role arn:aws:iam::000000000000:role/lambda-role \
 --environment "{\"Variables\":{\"STAGE\":\"local\",\"NLTK_DATA\":\"/var/task/nltk_data\"}}"

PREPROCESS_ARN=$(${AWS} lambda get-function \
 --function-name preprocess \
 --query 'Configuration.FunctionArn' \
 --output text)

### Connect the raw review S3 bucket to the preprocessing lambda
${AWS} s3api put-bucket-notification-configuration \
 --bucket review-raw \
 --notification-configuration "{\"LambdaFunctionConfigurations\":
[{\"LambdaFunctionArn\": \"${PREPROCESS_ARN}\", \"Events\":
[\"s3:ObjectCreated:*\"]}]}"

### Create the static s3 webapp
${AWS} s3 mb s3://webapp
${AWS} s3 website s3://webapp --index-document index.html
${AWS} s3 sync --delete ./website s3://webapp   --exclude ".ipynb_checkpoints/*"

echo
echo "Visit the following URL to access the web app:"
echo "Public web app URL: ${PUBLIC_BASE_URL}/webapp/index.html"
echo "Public presign URL: ${PUBLIC_BASE_URL}/2015-03-31/functions/presign/invocations"
echo "Public list URL: ${PUBLIC_BASE_URL}/2015-03-31/functions/list/invocations"
if [ -n "${S3_ENDPOINT_URL}" ]; then
  echo "Public S3 endpoint URL: ${S3_ENDPOINT_URL}"
fi
