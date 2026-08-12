# Registers stg-encrypt-tool ECS task definition after injecting Secrets Manager ARN.
# Prereq: secret stg/encrypt-tool-env exists; IAM roles and log group created.
# Usage (from repo root):
#   powershell -ExecutionPolicy Bypass -File backend/encrypt-tool/scripts/register-stg-task-definition.ps1
# PowerShell 7+ also: pwsh -File backend/encrypt-tool/scripts/register-stg-task-definition.ps1

param(
  [string] $Region = "eu-west-2",
  [string] $Profile = "smarthub-staging"
)

$ErrorActionPreference = "Stop"
$SecretId = "stg/encrypt-tool-env"
$Template = Join-Path $PSScriptRoot "..\ecs-task-definition.staging.template.json"
# Write next to template (avoids %TEMP% short paths like STEVEN~1 — AWS CLI on Windows rejects those for file://)
$OutFile = Join-Path (Split-Path -Parent $Template) "stg-encrypt-tool-taskdef.generated.json"

$arn = aws secretsmanager describe-secret --secret-id $SecretId --region $Region --profile $Profile --query "ARN" --output text
if (-not $arn) { throw "Could not resolve secret ARN for $SecretId" }

$raw = Get-Content -LiteralPath $Template -Raw -Encoding UTF8
$json = $raw.Replace("__SECRET_ARN__", $arn.Trim())
[System.IO.File]::WriteAllText($OutFile, $json, [System.Text.UTF8Encoding]::new($false))

# AWS CLI on Windows: file:///C:/... and paths with spaces/%20 often fail (Errno 22). Inline JSON breaks
# when aws.cmd splits on spaces inside the healthCheck command. Use 8.3 short path + file://C:/ (two slashes).
$fso = New-Object -ComObject Scripting.FileSystemObject
$resolved = (Resolve-Path -LiteralPath $OutFile).Path
$short = $fso.GetFile($resolved).ShortPath
if ($short -match '^([A-Za-z]):\\(.+)$') {
  $uriPath = "file://$($Matches[1]):/" + ($Matches[2] -replace "\\", "/")
} else {
  throw "Could not derive 8.3 short path for --cli-input-json (got: $short). Move the repo to a path without spaces or enable 8.3 names."
}

& aws ecs register-task-definition --region $Region --profile $Profile --cli-input-json $uriPath
if ($LASTEXITCODE -ne 0) {
  throw "aws ecs register-task-definition failed (exit $LASTEXITCODE). Generated JSON kept at: $OutFile"
}
Write-Host "Done. Task family: stg-encrypt-tool"
