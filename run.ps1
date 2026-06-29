# =============================================================================
# run.ps1  —  Windows PowerShell equivalent of run.sh
# Deploys the full Review Analysis pipeline to MiniStack (LocalStack)
#
# HOW TO RUN (from project root in PowerShell):
#   Set-ExecutionPolicy -Scope Process -ExecutionPolicy Bypass
#   .\run.ps1
# =============================================================================

$ErrorActionPreference = "Continue"   # keep going even if one step errors (e.g. bucket already exists)

# ---------------------------------------------------------------------------
# AWS credentials and endpoint
# ---------------------------------------------------------------------------
$env:AWS_ACCESS_KEY_ID     = "test"
$env:AWS_SECRET_ACCESS_KEY = "test"
$env:AWS_DEFAULT_REGION    = "us-east-1"
$ENDPOINT = "http://localhost:4566"

# Use python -m awscli so it works inside a venv without needing aws on PATH
$AWS_CMD = "python -m awscli --endpoint-url=$ENDPOINT"

function aws_run {
    # Runs an AWS CLI command via python -m awscli
    $joined = $args -join " "
    Write-Host "  > python -m awscli --endpoint-url=$ENDPOINT $joined" -ForegroundColor DarkGray
    Invoke-Expression "python -m awscli --endpoint-url=$ENDPOINT $joined"
}

function Make-LambdaZip {
    <#
    .SYNOPSIS
        Packages a Lambda function into lambda.zip.
        handler.py goes in at the root, plus all pip-installed packages.
    #>
    param(
        [string]$LambdaDir,           # e.g. "lambdas\preprocess"
        [string]$PipPackages = "",    # extra pip args, e.g. "--platform manylinux2014_x86_64 --only-binary=:all:"
        [string[]]$NltkCorpora = @() # e.g. @("punkt","stopwords","wordnet")
    )

    $absDir = Resolve-Path $LambdaDir

    Write-Host ""
    Write-Host "=== Packaging $LambdaDir ===" -ForegroundColor Cyan

    Push-Location $absDir

    # Clean previous build
    if (Test-Path "package")    { Remove-Item -Recurse -Force "package" }
    if (Test-Path "lambda.zip") { Remove-Item -Force "lambda.zip" }
    New-Item -ItemType Directory -Name "package" | Out-Null

    # Install pip dependencies into package/
    if (Test-Path "requirements.txt") {
        Write-Host "  Installing pip packages..." -ForegroundColor Yellow
        if ($PipPackages) {
            pip install -r requirements.txt -t package $PipPackages --quiet
        } else {
            pip install -r requirements.txt -t package --quiet
        }
    }

    # Download NLTK corpora into package/nltk_data/
    if ($NltkCorpora.Count -gt 0) {
        Write-Host "  Downloading NLTK data: $($NltkCorpora -join ', ')..." -ForegroundColor Yellow
        $env:PYTHONPATH = "package"
        $corporaStr = $NltkCorpora -join " "
        python -m nltk.downloader -d package/nltk_data $corporaStr
        Remove-Item Env:PYTHONPATH -ErrorAction SilentlyContinue
    }

    # Step 1: zip handler.py alone first
    Write-Host "  Creating lambda.zip with handler.py..." -ForegroundColor Yellow
    Compress-Archive -Path "handler.py" -DestinationPath "lambda.zip" -Force

    # Step 2: add everything inside package/ into the zip (at the root, not under package/)
    if ((Get-ChildItem "package" -ErrorAction SilentlyContinue).Count -gt 0) {
        Write-Host "  Adding packages to lambda.zip..." -ForegroundColor Yellow
        Push-Location "package"
        Get-ChildItem -Path "." -Recurse | Compress-Archive -DestinationPath "..\lambda.zip" -Update
        Pop-Location
    }

    Write-Host "  lambda.zip ready." -ForegroundColor Green
    Pop-Location
}

# ---------------------------------------------------------------------------
# STEP 1 — Create S3 buckets
# ---------------------------------------------------------------------------
Write-Host ""
Write-Host "======================================" -ForegroundColor Magenta
Write-Host " STEP 1: Creating S3 buckets"          -ForegroundColor Magenta
Write-Host "======================================" -ForegroundColor Magenta

aws_run s3 mb s3://ministack-thumbnails-app-images
aws_run s3 mb s3://ministack-thumbnails-app-resized
aws_run s3 mb s3://review-raw
aws_run s3 mb s3://review-preprocessed
aws_run s3 mb s3://review-profanity-checked
aws_run s3 mb s3://webapp

# ---------------------------------------------------------------------------
# STEP 2 — Store all names in SSM Parameter Store
# ---------------------------------------------------------------------------
Write-Host ""
Write-Host "======================================" -ForegroundColor Magenta
Write-Host " STEP 2: Writing SSM Parameters"       -ForegroundColor Magenta
Write-Host "======================================" -ForegroundColor Magenta

aws_run ssm put-parameter --name /ministack-thumbnail-app/buckets/images  --type String --value ministack-thumbnails-app-images --overwrite
aws_run ssm put-parameter --name /ministack-thumbnail-app/buckets/resized --type String --value ministack-thumbnails-app-resized --overwrite
aws_run ssm put-parameter --name /review-analysis/buckets/raw              --type String --value review-raw               --overwrite
aws_run ssm put-parameter --name /review-analysis/buckets/preprocessed     --type String --value review-preprocessed      --overwrite
aws_run ssm put-parameter --name /review-analysis/buckets/profanity-checked --type String --value review-profanity-checked --overwrite
aws_run ssm put-parameter --name /review-analysis/tables/reviews            --type String --value review-results           --overwrite
aws_run ssm put-parameter --name /review-analysis/tables/impolite-counts    --type String --value review-impolite-counts   --overwrite
aws_run ssm put-parameter --name /review-analysis/tables/banned-customers   --type String --value review-banned-customers  --overwrite

# ---------------------------------------------------------------------------
# STEP 3 — Create DynamoDB tables
# ---------------------------------------------------------------------------
Write-Host ""
Write-Host "======================================" -ForegroundColor Magenta
Write-Host " STEP 3: Creating DynamoDB tables"     -ForegroundColor Magenta
Write-Host "======================================" -ForegroundColor Magenta

aws_run dynamodb create-table `
    --table-name review-results `
    --attribute-definitions AttributeName=reviewId,AttributeType=S `
    --key-schema AttributeName=reviewId,KeyType=HASH `
    --billing-mode PAY_PER_REQUEST

aws_run dynamodb create-table `
    --table-name review-impolite-counts `
    --attribute-definitions AttributeName=reviewerID,AttributeType=S `
    --key-schema AttributeName=reviewerID,KeyType=HASH `
    --billing-mode PAY_PER_REQUEST

aws_run dynamodb create-table `
    --table-name review-banned-customers `
    --attribute-definitions AttributeName=reviewerID,AttributeType=S `
    --key-schema AttributeName=reviewerID,KeyType=HASH `
    --billing-mode PAY_PER_REQUEST

# ---------------------------------------------------------------------------
# STEP 4 — Package and deploy Lambda functions
# ---------------------------------------------------------------------------
Write-Host ""
Write-Host "======================================" -ForegroundColor Magenta
Write-Host " STEP 4: Deploying Lambda functions"   -ForegroundColor Magenta
Write-Host "======================================" -ForegroundColor Magenta

$ROLE = "arn:aws:iam::000000000000:role/lambda-role"
$ALLOWED_ORIGINS = "http://localhost:4566,http://127.0.0.1:4566,https://lbd.tuwien.ac.at"

### presign Lambda (no packages needed)
Make-LambdaZip -LambdaDir "lambdas\presign"
$presignZip = (Resolve-Path "lambdas\presign\lambda.zip").Path
aws_run lambda create-function `
    --function-name presign `
    --runtime python3.11 `
    --timeout 10 `
    --zip-file "fileb://$presignZip" `
    --handler handler.handler `
    --role $ROLE `
    --environment "{`"Variables`":{`"STAGE`":`"local`",`"ALLOWED_ORIGINS`":`"$ALLOWED_ORIGINS`"}}"

aws_run lambda create-function-url-config --function-name presign --auth-type NONE

### list Lambda (no packages needed)
Make-LambdaZip -LambdaDir "lambdas\list"
$listZip = (Resolve-Path "lambdas\list\lambda.zip").Path
aws_run lambda create-function `
    --function-name list `
    --runtime python3.11 `
    --timeout 10 `
    --zip-file "fileb://$listZip" `
    --handler handler.handler `
    --role $ROLE `
    --environment "{`"Variables`":{`"STAGE`":`"local`",`"ALLOWED_ORIGINS`":`"$ALLOWED_ORIGINS`"}}"

aws_run lambda create-function-url-config --function-name list --auth-type NONE

### resize Lambda (needs manylinux Pillow)
Make-LambdaZip -LambdaDir "lambdas\resize" -PipPackages "--platform manylinux2014_x86_64 --only-binary=:all:"
$resizeZip = (Resolve-Path "lambdas\resize\lambda.zip").Path
aws_run lambda create-function `
    --function-name resize `
    --runtime python3.11 `
    --timeout 10 `
    --zip-file "fileb://$resizeZip" `
    --handler handler.handler `
    --role $ROLE `
    --environment "{`"Variables`":{`"STAGE`":`"local`"}}"

$RESIZE_ARN = python -m awscli --endpoint-url=$ENDPOINT lambda get-function `
    --function-name resize `
    --query "Configuration.FunctionArn" `
    --output text

aws_run s3api put-bucket-notification-configuration `
    --bucket ministack-thumbnails-app-images `
    --notification-configuration "{`"LambdaFunctionConfigurations`":[{`"LambdaFunctionArn`":`"$RESIZE_ARN`",`"Events`":[`"s3:ObjectCreated:*`"]}]}"

### preprocess Lambda (needs nltk punkt, stopwords, wordnet)
Make-LambdaZip -LambdaDir "lambdas\preprocess" `
    -NltkCorpora @("punkt", "punkt_tab", "stopwords", "wordnet", "omw-1.4")

$preprocessZip = (Resolve-Path "lambdas\preprocess\lambda.zip").Path
aws_run lambda create-function `
    --function-name preprocess `
    --runtime python3.11 `
    --timeout 30 `
    --zip-file "fileb://$preprocessZip" `
    --handler handler.handler `
    --role $ROLE `
    --environment "{`"Variables`":{`"STAGE`":`"local`",`"NLTK_DATA`":`"/var/task/nltk_data`"}}"

$PREPROCESS_ARN = python -m awscli --endpoint-url=$ENDPOINT lambda get-function `
    --function-name preprocess `
    --query "Configuration.FunctionArn" `
    --output text

aws_run s3api put-bucket-notification-configuration `
    --bucket review-raw `
    --notification-configuration "{`"LambdaFunctionConfigurations`":[{`"LambdaFunctionArn`":`"$PREPROCESS_ARN`",`"Events`":[`"s3:ObjectCreated:*`"]}]}"

### profanity_check Lambda (needs profanityfilter)
Make-LambdaZip -LambdaDir "lambdas\profanity_check"
$profanityZip = (Resolve-Path "lambdas\profanity_check\lambda.zip").Path
aws_run lambda create-function `
    --function-name profanity_check `
    --runtime python3.11 `
    --timeout 30 `
    --zip-file "fileb://$profanityZip" `
    --handler handler.handler `
    --role $ROLE `
    --environment "{`"Variables`":{`"STAGE`":`"local`"}}"

$PROFANITY_ARN = python -m awscli --endpoint-url=$ENDPOINT lambda get-function `
    --function-name profanity_check `
    --query "Configuration.FunctionArn" `
    --output text

aws_run s3api put-bucket-notification-configuration `
    --bucket review-preprocessed `
    --notification-configuration "{`"LambdaFunctionConfigurations`":[{`"LambdaFunctionArn`":`"$PROFANITY_ARN`",`"Events`":[`"s3:ObjectCreated:*`"]}]}"

### sentiment Lambda (needs nltk vader_lexicon)
Make-LambdaZip -LambdaDir "lambdas\sentiment" -NltkCorpora @("vader_lexicon")
$sentimentZip = (Resolve-Path "lambdas\sentiment\lambda.zip").Path
aws_run lambda create-function `
    --function-name sentiment `
    --runtime python3.11 `
    --timeout 30 `
    --zip-file "fileb://$sentimentZip" `
    --handler handler.handler `
    --role $ROLE `
    --environment "{`"Variables`":{`"STAGE`":`"local`",`"NLTK_DATA`":`"/var/task/nltk_data`"}}"

$SENTIMENT_ARN = python -m awscli --endpoint-url=$ENDPOINT lambda get-function `
    --function-name sentiment `
    --query "Configuration.FunctionArn" `
    --output text

aws_run s3api put-bucket-notification-configuration `
    --bucket review-profanity-checked `
    --notification-configuration "{`"LambdaFunctionConfigurations`":[{`"LambdaFunctionArn`":`"$SENTIMENT_ARN`",`"Events`":[`"s3:ObjectCreated:*`"]}]}"

# ---------------------------------------------------------------------------
# STEP 5 — Deploy the static web app
# ---------------------------------------------------------------------------
Write-Host ""
Write-Host "======================================" -ForegroundColor Magenta
Write-Host " STEP 5: Uploading web app to S3"      -ForegroundColor Magenta
Write-Host "======================================" -ForegroundColor Magenta

aws_run s3 website s3://webapp --index-document index.html
aws_run s3 sync --delete ./website s3://webapp --exclude ".ipynb_checkpoints/*"

# ---------------------------------------------------------------------------
# Done!
# ---------------------------------------------------------------------------
Write-Host ""
Write-Host "========================================"  -ForegroundColor Green
Write-Host " DEPLOYMENT COMPLETE!"                     -ForegroundColor Green
Write-Host "========================================"  -ForegroundColor Green
Write-Host ""
Write-Host " Web app:   http://localhost:4566/webapp/index.html" -ForegroundColor Cyan
Write-Host " Presign:   http://localhost:4566/2015-03-31/functions/presign/invocations" -ForegroundColor Cyan
Write-Host " List:      http://localhost:4566/2015-03-31/functions/list/invocations" -ForegroundColor Cyan
Write-Host ""
Write-Host " Now run the integration tests:"         -ForegroundColor Yellow
Write-Host "   pytest tests\test_integration.py -v -s" -ForegroundColor Yellow
Write-Host ""
