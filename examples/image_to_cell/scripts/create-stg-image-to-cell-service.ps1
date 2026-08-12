# Creates ECS service stg-image-to-cell-service on stg-image-to-cell-cluster.
# ALB attaches to API port 3011.
# Prereq: task definition registered; target group stg-image-to-cell-tg (port 3011, IP, ALB VPC).
# Run: powershell -ExecutionPolicy Bypass -File backend/image-to-cell-3/scripts/create-stg-image-to-cell-service.ps1
param(
  [string] $Region = "eu-west-2",
  [string] $Profile = "smarthub-staging",
  [string] $Cluster = "stg-image-to-cell-cluster",
  [string] $ServiceName = "stg-image-to-cell-service",
  [string] $TaskFamily = "stg-image-to-cell",
  [string] $TargetGroupName = "stg-image-to-cell-tg",
  [string] $Subnet1 = "subnet-0afbd069383af4b67",
  [string] $Subnet2 = "subnet-0afa34c2182cda8e3",
  [string] $EcsSecurityGroup = "sg-02bffc5e5deaf7c5c"
)

$ErrorActionPreference = "Stop"

$tgArn = aws elbv2 describe-target-groups --names $TargetGroupName --region $Region --profile $Profile --query "TargetGroups[0].TargetGroupArn" --output text
if (-not $tgArn -or $tgArn -eq "None") { throw "Target group $TargetGroupName not found. Create it first (port 3011, IP, VPC = ALB VPC)." }

$net = "awsvpcConfiguration={subnets=[$Subnet1,$Subnet2],securityGroups=[$EcsSecurityGroup],assignPublicIp=DISABLED}"
$lb = "targetGroupArn=$tgArn,containerName=image-to-cell-3,containerPort=3011"

aws ecs create-service `
  --region $Region `
  --profile $Profile `
  --cluster $Cluster `
  --service-name $ServiceName `
  --task-definition $TaskFamily `
  --desired-count 1 `
  --launch-type FARGATE `
  --network-configuration $net `
  --load-balancers $lb `
  --health-check-grace-period-seconds 120

Write-Host "Service $ServiceName created. If it already exists: aws ecs update-service --cluster $Cluster --service $ServiceName --force-new-deployment --region $Region --profile $Profile"
