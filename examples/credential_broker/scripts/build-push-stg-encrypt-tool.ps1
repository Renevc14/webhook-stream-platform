# Build encrypt-tool with buildx (linux/amd64) and push to staging ECR stg-encrypt-tool:latest.
# Prereq: Docker buildx, AWS CLI, profile smarthub-staging with ECR push (account 262252232181, eu-west-2).
# Run from backend/encrypt-tool:
#   powershell -ExecutionPolicy Bypass -File scripts/build-push-stg-encrypt-tool.ps1
param(
  [string] $Region = "eu-west-2",
  [string] $Profile = "smarthub-staging",
  [string] $Repository = "stg-encrypt-tool"
)

$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $PSScriptRoot
Set-Location $Root

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
try { $vcs = (git -C $Root rev-parse --short HEAD 2>$null) } catch { }
if (-not $vcs) { $vcs = "local" }

docker buildx build --platform linux/amd64 -t $Image --provenance=false --sbom=false --push `
  --build-arg "BUILD_DATE=$(Get-Date -Format 'yyyy-MM-ddTHH:mm:ssZ')" --build-arg "VCS_REF=$vcs" .
if ($LASTEXITCODE -ne 0) { throw "Docker buildx push failed" }

Write-Host "Pushed $Image"
