#requires -Version 5.1
<#
.SYNOPSIS
  1) Patch ECS task def: move I2C_ADMIN_API_KEY from bogus env ARN -> secrets[] (real injection).
  2) Align image-to-cell-tg ALB health check with Attachment Counter pattern (HTTP /health, 200).
  3) Set ECS service healthCheckGracePeriodSeconds=90 (ACI-style; was 0 -> ELB flapping).
  4) Register task revision + force new deployment.

  Prerequisite: aws sso login --profile devroot-admin

  Safe defaults: same subnets/SG/TG as existing service; does not recreate ALB or listener.
#>
param(
    [string]$AwsProfile = "devroot-admin",
    [string]$Region = "eu-west-2",
    [string]$Cluster = "image-to-cell-cluster",
    [string]$Service = "image-to-cell-api-task-webhook-service-6ub9n1h9",
    [string]$TaskFamily = "image-to-cell-api-task-webhook",
    [string]$TargetGroupName = "image-to-cell-tg",
    [string]$Image = "780593604124.dkr.ecr.eu-west-2.amazonaws.com/image-to-cell-3:latest",
    [string]$HealthCheckPath = "/health",
    [int]$HealthCheckGracePeriodSeconds = 90,
    [switch]$SkipTaskRegister,
    [switch]$SkipAlbUpdate,
    [switch]$SkipServiceUpdate
)

$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $PSScriptRoot
Set-Location $Root

function Invoke-AwsOk {
    param([string[]]$CliArgs)
    & aws @CliArgs
    if ($LASTEXITCODE -ne 0) { throw "aws failed: $($CliArgs -join ' ')" }
}

Write-Host "=== Image-to-Cell: admin secret + ALB health + ECS grace ===" -ForegroundColor Cyan

Invoke-AwsOk -CliArgs @("sts", "get-caller-identity", "--profile", $AwsProfile, "--region", $Region)

# --- Target group: match attachment-counter-tg style (HTTP, path, 200) ---
if (-not $SkipAlbUpdate) {
    $tgArn = aws elbv2 describe-target-groups --names $TargetGroupName --region $Region --profile $AwsProfile --query "TargetGroups[0].TargetGroupArn" --output text
    if (-not $tgArn -or $tgArn -eq "None") { throw "Target group not found: $TargetGroupName" }

    Write-Host "Updating target group health check: $TargetGroupName -> $HealthCheckPath (HTTP 200), same intervals as attachment-counter-tg" -ForegroundColor Yellow
    Invoke-AwsOk -CliArgs @(
        "elbv2", "modify-target-group",
        "--target-group-arn", $tgArn,
        "--health-check-path", $HealthCheckPath,
        "--health-check-protocol", "HTTP",
        "--matcher", "HttpCode=200",
        "--health-check-interval-seconds", "30",
        "--health-check-timeout-seconds", "5",
        "--healthy-threshold-count", "5",
        "--unhealthy-threshold-count", "2",
        "--region", $Region,
        "--profile", $AwsProfile
    )
    Write-Host "ALB target group updated." -ForegroundColor Green
}

# --- Task definition: clone + fix admin secret + same image ---
if (-not $SkipTaskRegister) {
    New-Item -ItemType Directory -Force -Path "C:\TempI2c" | Out-Null
    $tmpClean = "C:\TempI2c\i2c-clean.json"
    $tmpFinal = "C:\TempI2c\i2c-register.json"

    $desc = aws ecs describe-task-definition --task-definition $TaskFamily --region $Region --profile $AwsProfile --output json
    if ($LASTEXITCODE -ne 0) { throw "describe-task-definition failed" }

    $cleaned = $desc | python (Join-Path $Root "scripts\clean_ecs_task_def_for_register.py") $Image
    $utf8 = New-Object System.Text.UTF8Encoding $false
    [System.IO.File]::WriteAllText($tmpClean, $cleaned, $utf8)

    $patched = Get-Content -Path $tmpClean -Raw -Encoding UTF8 | python (Join-Path $Root "scripts\fix_i2c_admin_env_to_secrets.py")
    [System.IO.File]::WriteAllText($tmpFinal, $patched, $utf8)

    $rev = aws ecs register-task-definition --cli-input-json "file://C:/TempI2c/i2c-register.json" --region $Region --profile $AwsProfile --query "taskDefinition.revision" --output text
    if ($LASTEXITCODE -ne 0) { throw "register-task-definition failed" }
    Write-Host "Registered $TaskFamily`:$rev (I2C_ADMIN_API_KEY should be under secrets[])" -ForegroundColor Green
    $script:NewRevision = $rev
}
else {
    $script:NewRevision = aws ecs describe-task-definition --task-definition $TaskFamily --region $Region --profile $AwsProfile --query "taskDefinition.revision" --output text
}

# --- Service: grace period (critical) + deploy ---
if (-not $SkipServiceUpdate) {
    Write-Host "Updating service: healthCheckGracePeriodSeconds=$HealthCheckGracePeriodSeconds (was often 0 -> ELB marks unhealthy during boot)" -ForegroundColor Yellow
    Invoke-AwsOk -CliArgs @(
        "ecs", "update-service",
        "--cluster", $Cluster,
        "--service", $Service,
        "--task-definition", "$TaskFamily`:$NewRevision",
        "--health-check-grace-period-seconds", "$HealthCheckGracePeriodSeconds",
        "--force-new-deployment",
        "--region", $Region,
        "--profile", $AwsProfile
    )
    Write-Host "Service update submitted." -ForegroundColor Green
}

Write-Host "`nMonitor:" -ForegroundColor Cyan
Write-Host "  aws elbv2 describe-target-health --target-group-arn <arn> --region $Region --profile $AwsProfile"
Write-Host "  aws ecs describe-services --cluster $Cluster --services $Service --region $Region --profile $AwsProfile"
