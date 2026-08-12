# Build encrypt-tool with buildx (linux/amd64 for ECS Fargate) and optionally push to ECR.
param(
    [string]$Region = "eu-west-2",
    [string]$AccountId = "780593604124",
    [string]$Repository = "encrypt-tool",
    [string]$Tag = "latest",
    [switch]$Push,
    [switch]$AlsoTagDate
)

$ErrorActionPreference = "Stop"
$Root = Split-Path $PSScriptRoot -Parent
Set-Location $Root

$LocalTag = "encrypt-tool:local"
Write-Host "== docker buildx build --platform linux/amd64 -> $LocalTag =="
docker buildx build --platform linux/amd64 --tag $LocalTag --load .

if (-not $Push) {
    Write-Host "Done (local only). Add -Push to login and push to ECR."
    exit 0
}

$Registry = "${AccountId}.dkr.ecr.${Region}.amazonaws.com"
$Remote = "${Registry}/${Repository}:${Tag}"

Write-Host "== ECR login $Registry =="
aws ecr get-login-password --region $Region | docker login --username AWS --password-stdin $Registry

docker tag $LocalTag $Remote
Write-Host "== docker push $Remote =="
docker push $Remote

if ($AlsoTagDate) {
    $Dated = "${Registry}/${Repository}:build-$(Get-Date -Format 'yyyyMMdd-HHmm')"
    docker tag $LocalTag $Dated
    docker push $Dated
    Write-Host "Also pushed $Dated"
}

Write-Host "Image: $Remote"
Write-Host "Update ECS task definition image URI, then deploy new revision."
