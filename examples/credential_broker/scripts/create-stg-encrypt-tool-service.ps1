# Creates ECS service stg-encrypt-tool-service (after task definition and TG exist).
# Run: powershell -ExecutionPolicy Bypass -File backend/encrypt-tool/scripts/create-stg-encrypt-tool-service.ps1
# Defaults match Smarthub staging VPC from infra docs; override with parameters if needed.
param(
  [string] $Region = "eu-west-2",
  [string] $Profile = "smarthub-staging",
  [string] $Cluster = "stg-encrypt-tool-cluster",
  [string] $ServiceName = "stg-encrypt-tool-service",
  [string] $TaskFamily = "stg-encrypt-tool",
  [string] $TargetGroupName = "stg-encrypt-tool-tg",
  [string] $Subnet1 = "subnet-0afbd069383af4b67",
  [string] $Subnet2 = "subnet-0afa34c2182cda8e3",
  [string] $EcsSecurityGroup = "sg-02bffc5e5deaf7c5c"
)

$ErrorActionPreference = "Stop"

$tgArn = aws elbv2 describe-target-groups --names $TargetGroupName --region $Region --profile $Profile --query "TargetGroups[0].TargetGroupArn" --output text
if (-not $tgArn -or $tgArn -eq "None") { throw "Target group $TargetGroupName not found. Create it first (port 3051, IP, VPC = ALB VPC)." }

$net = "awsvpcConfiguration={subnets=[$Subnet1,$Subnet2],securityGroups=[$EcsSecurityGroup],assignPublicIp=DISABLED}"
$lb = "targetGroupArn=$tgArn,containerName=encrypt-tool,containerPort=3051"

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
  --health-check-grace-period-seconds 90

Write-Host "Service $ServiceName created. If it already exists, run: aws ecs update-service --cluster $Cluster --service $ServiceName --force-new-deployment --region $Region --profile $Profile"
