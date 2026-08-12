# Build image-to-cell-3 with buildx (linux/amd64) and push to staging ECR stg-image-to-cell:latest.
#
# Build context is the repo root because the Dockerfile copies shared/smarthub_core/
# alongside backend/image-to-cell-3/.
#
# Prereq: Docker buildx, AWS CLI, profile smarthub-staging with ECR push.
# Run from anywhere in the repo:
#   powershell -ExecutionPolicy Bypass -File backend/image-to-cell-3/scripts/build-push-stg-image-to-cell.ps1
param(
  [string] $Region = "eu-west-2",
  [string] $Profile = "smarthub-staging",
  [string] $Repository = "stg-image-to-cell"
)

$ErrorActionPreference = "Stop"

# Resolve repo root. $PSScriptRoot = backend/image-to-cell-3/scripts/, so go up 3 levels.
$RepoRoot = Resolve-Path (Join-Path $PSScriptRoot "..\..\..")
Set-Location $RepoRoot

$DockerfilePath = "backend/image-to-cell-3/Dockerfile"

$AccountId = aws sts get-caller-identity --profile $Profile --query Account --output text
if (-not $AccountId) { throw "Could not resolve account (check --profile $Profile)" }

$Registry = "${AccountId}.dkr.ecr.${Region}.amazonaws.com"
$Image = "${Registry}/${Repository}:latest"

$repoExists = $true
try { aws ecr describe-repositories --repository-names $Repository --region $Region --profile $Profile 2>$null | Out-Null } catch { $repoExists = $false }
if ($LASTEXITCODE -ne 0) { $repoExists = $false }
if (-not $repoExists) {
  Write-Host "Creating ECR repository $Repository (scan on push)..."
  aws ecr create-repository `
    --repository-name $Repository `
    --region $Region `
    --profile $Profile `
    --image-scanning-configuration scanOnPush=true `
    --encryption-configuration encryptionType=AES256
  if ($LASTEXITCODE -ne 0) { throw "ecr create-repository failed" }
}

aws ecr get-login-password --region $Region --profile $Profile | docker login --username AWS --password-stdin $Registry
if ($LASTEXITCODE -ne 0) { throw "ECR login failed" }

$vcs = "local"
try { $vcs = (git -C $RepoRoot rev-parse --short HEAD 2>$null) } catch { }
if (-not $vcs) { $vcs = "local" }

docker buildx build --platform linux/amd64 -t $Image --provenance=false --sbom=false --push `
  -f $DockerfilePath `
  --build-arg "BUILD_DATE=$(Get-Date -Format 'yyyy-MM-ddTHH:mm:ssZ')" `
  --build-arg "VCS_REF=$vcs" `
  .
if ($LASTEXITCODE -ne 0) { throw "Docker buildx push failed" }

Write-Host "Pushed $Image"
