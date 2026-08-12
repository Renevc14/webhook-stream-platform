# Deploy image-to-cell-3 to ECS Fargate
# Registers a new task definition revision and forces a new deployment

$CLUSTER = "image-to-cell"
$SERVICE = "image-to-cell-3"
$REGION = "eu-west-2"
$PROFILE = "devroot-admin"
$TASK_DEF_FILE = "ecs-task-definition.json"

Write-Host ("=" * 70) -ForegroundColor Cyan
Write-Host "Deploying Image-to-Cell v3 to ECS" -ForegroundColor Cyan
Write-Host ("=" * 70) -ForegroundColor Cyan
Write-Host ""

Write-Host "Deployment Configuration:" -ForegroundColor Yellow
Write-Host "  Cluster:  $CLUSTER"
Write-Host "  Service:  $SERVICE"
Write-Host "  Region:   $REGION"
Write-Host "  Profile:  $PROFILE"
Write-Host ""

# Step 1: Register new task definition
Write-Host "Step 1: Registering new task definition..." -ForegroundColor Yellow

aws ecs register-task-definition `
    --cli-input-json "file://$TASK_DEF_FILE" `
    --region $REGION `
    --profile $PROFILE

if ($LASTEXITCODE -ne 0) {
    Write-Host "Failed to register task definition" -ForegroundColor Red
    exit 1
}

Write-Host "Task definition registered" -ForegroundColor Green
Write-Host ""

# Step 2: Force new deployment
Write-Host "Step 2: Forcing new ECS deployment..." -ForegroundColor Yellow

aws ecs update-service `
    --cluster $CLUSTER `
    --service $SERVICE `
    --force-new-deployment `
    --region $REGION `
    --profile $PROFILE

if ($LASTEXITCODE -ne 0) {
    Write-Host "ECS deployment failed" -ForegroundColor Red
    exit 1
}

Write-Host "Deployment started" -ForegroundColor Green
Write-Host ""

# Step 3: Monitor deployment
Write-Host "Step 3: Monitoring deployment progress..." -ForegroundColor Yellow
$startTime = Get-Date
$timeout = 180

while (((Get-Date) - $startTime).TotalSeconds -lt $timeout) {
    $status = aws ecs describe-services `
        --cluster $CLUSTER `
        --services $SERVICE `
        --region $REGION `
        --profile $PROFILE `
        --query 'services[0].deployments[0].{status:status,running:runningCount,desired:desiredCount}' `
        --output json | ConvertFrom-Json

    $running = $status.running
    $desired = $status.desired
    $deployStatus = $status.status

    Write-Host "  Status: $deployStatus | Running: $running/$desired" -ForegroundColor Cyan

    if ($running -eq $desired -and $deployStatus -eq "PRIMARY") {
        Write-Host ""
        Write-Host "Deployment successful!" -ForegroundColor Green
        break
    }

    Start-Sleep -Seconds 15
}

Write-Host ""
Write-Host ("=" * 70) -ForegroundColor Cyan
Write-Host "DEPLOYMENT COMPLETE" -ForegroundColor Green
Write-Host ("=" * 70) -ForegroundColor Cyan
Write-Host ""
Write-Host "Monitor logs:" -ForegroundColor Yellow
Write-Host "  aws logs tail /ecs/image-to-cell-3 --follow --region $REGION --profile $PROFILE" -ForegroundColor Gray
Write-Host ""
