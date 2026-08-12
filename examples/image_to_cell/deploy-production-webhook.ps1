# Build, push, and deploy Image-to-Cell v3 to the *live* ECS service (prod webhook API).
# Clones the current task definition so Secrets Manager key ARNs are preserved.
#
# Prerequisite: aws sso login --profile devroot-admin
#
# Actual AWS names (eu-west-2):
#   Cluster:  image-to-cell-cluster
#   Service:  image-to-cell-api-task-webhook-service-6ub9n1h9
#   Family:   image-to-cell-api-task-webhook
#   ECR:      780593604124.dkr.ecr.eu-west-2.amazonaws.com/image-to-cell-3:latest
#
# Task def: moves misplaced I2C_ADMIN_API_KEY (Secrets Manager ARN in env) -> secrets[].
# Service: sets healthCheckGracePeriodSeconds=90 (Attachment Counter–style; avoids ELB flapping during boot).

param(
    [string]$AwsProfile = "devroot-admin",
    [string]$Region = "eu-west-2",
    [string]$Cluster = "image-to-cell-cluster",
    [string]$Service = "image-to-cell-api-task-webhook-service-6ub9n1h9",
    [string]$TaskFamily = "image-to-cell-api-task-webhook",
    [string]$Image = "780593604124.dkr.ecr.eu-west-2.amazonaws.com/image-to-cell-3:latest",
    [int]$HealthCheckGracePeriodSeconds = 90,
    [switch]$SkipBuild
)

$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $Root

$Registry = "780593604124.dkr.ecr.eu-west-2.amazonaws.com"
$TmpClean = "C:\TempI2c\register-clean.json"
$TmpJson = "C:\TempI2c\register.json"
New-Item -ItemType Directory -Force -Path (Split-Path $TmpJson) | Out-Null
$utf8 = New-Object System.Text.UTF8Encoding $false

aws sts get-caller-identity --profile $Profile --region $Region | Out-Null
if ($LASTEXITCODE -ne 0) {
    Write-Host "Run: aws sso login --profile $Profile" -ForegroundColor Red
    exit 1
}

if (-not $SkipBuild) {
    aws ecr get-login-password --region $Region --profile $AwsProfile | docker login --username AWS --password-stdin $Registry
    docker buildx build --platform linux/amd64 --load -t $Image -f (Join-Path $Root "Dockerfile") $Root
    docker push $Image
}

$desc = aws ecs describe-task-definition --task-definition $TaskFamily --region $Region --profile $Profile --output json
$cleaned = $desc | python (Join-Path $Root "scripts\clean_ecs_task_def_for_register.py") $Image
[System.IO.File]::WriteAllText($TmpClean, $cleaned, $utf8)
$patched = Get-Content -Path $TmpClean -Raw -Encoding UTF8 | python (Join-Path $Root "scripts\fix_i2c_admin_env_to_secrets.py")
[System.IO.File]::WriteAllText($TmpJson, $patched, $utf8)

$rev = aws ecs register-task-definition --cli-input-json "file://C:/TempI2c/register.json" --region $Region --profile $AwsProfile --query "taskDefinition.revision" --output text
Write-Host "Registered $TaskFamily`:$rev (admin key via secrets[] if it was wrongly in env)" -ForegroundColor Green

aws ecs update-service `
    --cluster $Cluster `
    --service $Service `
    --task-definition "${TaskFamily}:$rev" `
    --health-check-grace-period-seconds $HealthCheckGracePeriodSeconds `
    --force-new-deployment `
    --region $Region `
    --profile $AwsProfile | Out-Null
Write-Host "Deployment started for $Service (ELB grace ${HealthCheckGracePeriodSeconds}s)" -ForegroundColor Green
