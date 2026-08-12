# Registers stg-image-to-cell ECS task definition (injects Secrets Manager ARN for stg/image-to-cell-env).
# Prereq: secret stg/image-to-cell-env (must include REDIS_URL for Phase 1); IAM roles; log group /ecs/smarthub-stg/image-to-cell.
# See backend/image-to-cell-3/infrastructure/ecs-task-definition.redis-env.snippet.json
# Usage (from repo root):
#   powershell -ExecutionPolicy Bypass -File backend/image-to-cell-3/scripts/register-stg-task-definition.ps1
#   powershell ... -File ... -Profile smarthub-staging -Region eu-west-2

param(
  [string] $Region = "eu-west-2",
  [string] $Profile = "smarthub-staging"
)

$ErrorActionPreference = "Stop"
$RepoRoot = Split-Path -Parent (Split-Path -Parent (Split-Path -Parent $PSScriptRoot))
. (Join-Path $RepoRoot "scripts\staging-taskdef-substitutions.ps1")
$SecretId = "stg/image-to-cell-env"
$Template = Join-Path $PSScriptRoot "..\ecs-task-definition.staging.template.json"
$OutFile = Join-Path (Split-Path -Parent $Template) "stg-image-to-cell-taskdef.generated.json"

$arn = aws secretsmanager describe-secret --secret-id $SecretId --region $Region --profile $Profile --query "ARN" --output text
if (-not $arn) { throw "Could not resolve secret ARN for $SecretId" }

$secretRaw = aws secretsmanager get-secret-value --secret-id $SecretId --region $Region --profile $Profile --query "SecretString" --output text
if (-not $secretRaw) { throw "Empty SecretString from $SecretId" }
$secretObj = $secretRaw | ConvertFrom-Json
if (-not $secretObj.REDIS_URL -or [string]::IsNullOrWhiteSpace([string]$secretObj.REDIS_URL)) {
  Write-Warning "Secret $SecretId has no REDIS_URL. Phase 1 will run degraded until you add it to the secret JSON."
}

$raw = Get-Content -LiteralPath $Template -Raw -Encoding UTF8
$json = Apply-StagingTaskDefSubstitutions -TemplateJson $raw -SecretArn $arn
[System.IO.File]::WriteAllText($OutFile, $json, [System.Text.UTF8Encoding]::new($false))

$fso = New-Object -ComObject Scripting.FileSystemObject
$resolved = (Resolve-Path -LiteralPath $OutFile).Path
$short = $fso.GetFile($resolved).ShortPath
if ($short -match '^([A-Za-z]):\\(.+)$') {
  $uriPath = "file://$($Matches[1]):/" + ($Matches[2] -replace "\\", "/")
} else {
  throw "Could not derive 8.3 short path for --cli-input-json (got: $short)."
}

& aws ecs register-task-definition --region $Region --profile $Profile --cli-input-json $uriPath
if ($LASTEXITCODE -ne 0) {
  throw "aws ecs register-task-definition failed (exit $LASTEXITCODE). Generated JSON kept at: $OutFile"
}
Write-Host "Done. Task family: stg-image-to-cell"
