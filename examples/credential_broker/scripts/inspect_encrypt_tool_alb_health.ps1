<#
.SYNOPSIS
  Reads AWS for encrypt-tool ECS → target group(s) → ALB health checks (no console clicks).

.DESCRIPTION
  Uses AWS CLI with your profile to print:
  - ECS service load-balancer attachment (container port ↔ target group)
  - Target group health check path, protocol, matcher, interval
  - ALB DNS name(s) for manual curl checks
  - Reminders for hub REACT_APP_ENCRYPT_* and /health

.PARAMETER Profile
  AWS CLI profile (e.g. devroot-admin). If omitted, uses default credential chain.

.EXAMPLE
  .\inspect_encrypt_tool_alb_health.ps1 -Profile devroot-admin

.EXAMPLE
  .\inspect_encrypt_tool_alb_health.ps1 -Region eu-west-2 -Profile devroot-admin -Cluster encrypt-tool-cluster -Service encrypt-tool-task-service-x8iz3lw7
#>
param(
    [string]$Region = "eu-west-2",
    [string]$Profile = "",
    [string]$Cluster = "encrypt-tool-cluster",
    [string]$Service = "encrypt-tool-task-service-x8iz3lw7",
    [switch]$Json
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
    # AWS CLI may write to stderr; avoid PowerShell treating it as a terminating error
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

Write-Host "== ECS: $Cluster / $Service ($Region) ==" -ForegroundColor Cyan
$svcJson = Invoke-AwsJson @(
    "ecs", "describe-services",
    "--cluster", $Cluster,
    "--services", $Service,
    "--output", "json"
)
if ($Json) {
    Write-Host $svcJson
    exit 0
}
$svcWrap = $svcJson | ConvertFrom-Json
$svc = $svcWrap.services | Select-Object -First 1
if (-not $svc) {
    throw "Service not found or empty response."
}

Write-Host "  Status: $($svc.status)  Desired: $($svc.desiredCount)  Running: $($svc.runningCount)"
Write-Host "  Task definition: $($svc.taskDefinition)"

$lbs = @($svc.loadBalancers)
if ($lbs.Count -eq 0) {
    Write-Host "  No loadBalancers on this service (is it behind NLB only, or not registered?)." -ForegroundColor Yellow
    exit 0
}

$tgArns = @($lbs | ForEach-Object { $_.targetGroupArn } | Select-Object -Unique)

Write-Host "`n== ELB target group(s) ==" -ForegroundColor Cyan
$tgArgs = @("elbv2", "describe-target-groups", "--target-group-arns") + $tgArns + @("--output", "json")
$tgJson = Invoke-AwsJson $tgArgs
$tgs = ($tgJson | ConvertFrom-Json).TargetGroups

foreach ($lbEntry in $lbs) {
    $ctPort = $lbEntry.containerPort
    $ctName = $lbEntry.containerName
    $tgArn = $lbEntry.targetGroupArn
    $tg = $tgs | Where-Object { $_.TargetGroupArn -eq $tgArn } | Select-Object -First 1
    if (-not $tg) { continue }

    Write-Host "`n  Container: $ctName  port: $ctPort"
    Write-Host "  Target group: $($tg.TargetGroupName)"
    Write-Host "  TG port (traffic to tasks): $($tg.Port)  Protocol: $($tg.Protocol)  Target type: $($tg.TargetType)"
    Write-Host "  Health check --"
    Write-Host "    Path:     $($tg.HealthCheckPath)"
    Write-Host "    Protocol: $($tg.HealthCheckProtocol)  Port: $($tg.HealthCheckPort)"
    Write-Host "    Interval: $($tg.HealthCheckIntervalSeconds)s  Timeout: $($tg.HealthCheckTimeoutSeconds)s  Healthy threshold: $($tg.HealthyThresholdCount)"
    Write-Host "    Matcher:  HTTP $($tg.Matcher.HttpCode)"
    $hint = if ($tg.HealthCheckPath -eq "/health") { "OK: matches encrypt-tool GET /health" } elseif ($tg.HealthCheckPath -eq "/") { "OK for legacy GET /; consider /health for JSON probe" } else { "Verify task serves this path with 200" }
    Write-Host "    Note:     $hint" -ForegroundColor DarkGray
}

$allLbArns = @($tgs | ForEach-Object { $_.LoadBalancerArns } | ForEach-Object { $_ } | Select-Object -Unique)
if ($allLbArns.Count -gt 0) {
    Write-Host "`n== Load balancer DNS (for manual checks) ==" -ForegroundColor Cyan
    $elbArgs = @("elbv2", "describe-load-balancers", "--load-balancer-arns") + $allLbArns + @("--output", "json")
    $elbJson = Invoke-AwsJson $elbArgs
    $elbs = ($elbJson | ConvertFrom-Json).LoadBalancers
    foreach ($e in $elbs) {
        $scheme = $e.Scheme
        $dns = $e.DNSName
        Write-Host "  $($e.LoadBalancerName)  ($scheme)"
        Write-Host "    DNS: $dns"
        $proto = if ($e.Scheme -eq "internal") { "http" } else { "https" }
        Write-Host "    Example (path depends on listener rules): ${proto}://${dns}<your-path-prefix>/health"
    }
}

Write-Host "`n== Hub / System status (frontend build env) ==" -ForegroundColor Cyan
Write-Host "  REACT_APP_ENCRYPT_URL = public base (e.g. https://api.prodactivelabs.com/encrypt)"
Write-Host "  REACT_APP_ENCRYPT_HEALTH_PATH = /health   (default in app; returns JSON status ok)"
Write-Host "  Or REACT_APP_ENCRYPT_HEALTH_URL = full URL if health lives elsewhere"
Write-Host "`n  encrypt-tool endpoints: GET /health (preferred), GET / (legacy text)"
Write-Host ""
