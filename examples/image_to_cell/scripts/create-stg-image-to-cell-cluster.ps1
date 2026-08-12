# One-time: create stg-image-to-cell-cluster (Fargate + Container Insights).
# Run: powershell -ExecutionPolicy Bypass -File backend/image-to-cell-3/scripts/create-stg-image-to-cell-cluster.ps1
param(
  [string] $Region = "eu-west-2",
  [string] $Profile = "smarthub-staging",
  [string] $ClusterName = "stg-image-to-cell-cluster"
)

$ErrorActionPreference = "Stop"

aws ecs create-cluster `
  --cluster-name $ClusterName `
  --capacity-providers FARGATE FARGATE_SPOT `
  --default-capacity-provider-strategy capacityProvider=FARGATE,weight=1 `
  --settings name=containerInsights,value=enabled `
  --region $Region `
  --profile $Profile

Write-Host "Cluster $ClusterName ready (or already existed - check AWS console for errors)."
