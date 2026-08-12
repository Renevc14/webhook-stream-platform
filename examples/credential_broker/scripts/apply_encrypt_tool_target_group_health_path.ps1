<#
.SYNOPSIS
  Sets encrypt-tool ALB target group health check path to /health (aligns with ECS image + hub Status page).

.PARAMETER DryRun
  Print the AWS command only; do not modify.

.EXAMPLE
  .\apply_encrypt_tool_target_group_health_path.ps1 -Profile devroot-admin

.EXAMPLE
  .\apply_encrypt_tool_target_group_health_path.ps1 -Profile devroot-admin -DryRun
#>
param(
    [string]$Region = "eu-west-2",
    [string]$Profile = "",
    [string]$TargetGroupName = "encrypt-tool-tg",
    [string]$HealthCheckPath = "/health",
    [switch]$DryRun
)

$ErrorActionPreference = "Stop"

function Invoke-AwsJson {
    param([Parameter(Mandatory = $true)][string[]]$Arguments)
    $all = [System.Collections.Generic.List[string]]::new()
    foreach ($a in $Arguments) { [void]$all.Add($a) }
    if ($Profile) {
        [void]$all.Add("--profile")
        [void]$all.Add($Profile)
    }
    [void]$all.Add("--region")
    [void]$all.Add($Region)
    [void]$all.Add("--no-cli-pager")
    $oldEap = $ErrorActionPreference
    $ErrorActionPreference = "SilentlyContinue"
    $out = & aws @($all.ToArray()) 2>&1
    $code = $LASTEXITCODE
    $ErrorActionPreference = $oldEap
    if ($code -ne 0) {
        $msg = ($out | ForEach-Object { if ($_ -is [System.Management.Automation.ErrorRecord]) { $_.Exception.Message } else { $_ } }) -join "`n"
        throw "aws failed ($code): $msg"
    }
    return ($out | ForEach-Object { if ($_ -is [System.Management.Automation.ErrorRecord]) { $_.Exception.Message } else { $_ } }) -join "`n"
}

Write-Host "== Resolve target group: $TargetGroupName ($Region) ==" -ForegroundColor Cyan
$tgJson = Invoke-AwsJson @("elbv2", "describe-target-groups", "--names", $TargetGroupName, "--output", "json")
$tg = (($tgJson | ConvertFrom-Json).TargetGroups | Select-Object -First 1)
if (-not $tg) {
    throw "Target group '$TargetGroupName' not found."
}
$arn = $tg.TargetGroupArn
Write-Host "  ARN: $arn"
Write-Host "  Current health path: $($tg.HealthCheckPath)"

if ($tg.HealthCheckPath -eq $HealthCheckPath) {
    Write-Host "  Already set to $HealthCheckPath; no change." -ForegroundColor Green
    exit 0
}

$modifyArgs = @(
    "elbv2", "modify-target-group",
    "--target-group-arn", $arn,
    "--health-check-path", $HealthCheckPath,
    "--matcher", "HttpCode=200"
)

if ($DryRun) {
    Write-Host "`n[DRY RUN] Would run: aws $($modifyArgs -join ' ') --region $Region $(if ($Profile) { "--profile $Profile" })" -ForegroundColor Yellow
    exit 0
}

Write-Host ""
Write-Host "== Applying health check path: $HealthCheckPath ==" -ForegroundColor Cyan
Invoke-AwsJson $modifyArgs | Out-Null
Write-Host 'Done. Run inspect_encrypt_tool_alb_health.ps1 to verify.' -ForegroundColor Green
