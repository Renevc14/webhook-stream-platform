# Build and push image-to-cell-3 Docker image to AWS ECR
# Region: eu-west-2
# Profile: devroot-admin
# Repository: 780593604124.dkr.ecr.eu-west-2.amazonaws.com/image-to-cell-3
#
# Build context is the repo root because the Dockerfile copies shared/smarthub_core/
# alongside backend/image-to-cell-3/. Run from anywhere in the repo:
#   powershell -ExecutionPolicy Bypass -File backend/image-to-cell-3/build-and-push.ps1

$ErrorActionPreference = "Stop"

$ECR_REGISTRY = "780593604124.dkr.ecr.eu-west-2.amazonaws.com"
$IMAGE_NAME = "image-to-cell-3"
$IMAGE_TAG = "latest"
$FULL_IMAGE_NAME = "${ECR_REGISTRY}/${IMAGE_NAME}:${IMAGE_TAG}"

# Resolve repo root. $PSScriptRoot = backend/image-to-cell-3/, so go up 2 levels.
$RepoRoot = Resolve-Path (Join-Path $PSScriptRoot "..\..")
Set-Location $RepoRoot

$DockerfilePath = "backend/image-to-cell-3/Dockerfile"

Write-Host "Logging into AWS ECR..." -ForegroundColor Cyan
aws ecr get-login-password --region eu-west-2 --profile devroot-admin | docker login --username AWS --password-stdin $ECR_REGISTRY

if ($LASTEXITCODE -ne 0) {
    Write-Host "Failed to login to ECR" -ForegroundColor Red
    exit 1
}

Write-Host "Successfully logged into ECR" -ForegroundColor Green

Write-Host "`nBuilding Docker image using buildx (build context = $RepoRoot)..." -ForegroundColor Cyan
docker buildx build --platform linux/amd64 --load -t $FULL_IMAGE_NAME -f $DockerfilePath .

if ($LASTEXITCODE -ne 0) {
    Write-Host "Failed to build Docker image" -ForegroundColor Red
    exit 1
}

Write-Host "Successfully built Docker image" -ForegroundColor Green

Write-Host "`nPushing image to ECR..." -ForegroundColor Cyan
docker push $FULL_IMAGE_NAME

if ($LASTEXITCODE -ne 0) {
    Write-Host "Failed to push Docker image" -ForegroundColor Red
    exit 1
}

Write-Host "`nSuccessfully pushed ${FULL_IMAGE_NAME} to ECR!" -ForegroundColor Green
