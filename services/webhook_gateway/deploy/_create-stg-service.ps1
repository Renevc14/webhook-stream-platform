$ErrorActionPreference = "Stop"
$env:AWS_CONFIG_FILE = "C:\Users\Rene1\.aws\config"
$env:AWS_SHARED_CREDENTIALS_FILE = "C:\Users\Rene1\.aws\credentials"
$Region = "eu-west-2"
$Profile = "smarthub-staging"
$TgArn = "arn:aws:elasticloadbalancing:eu-west-2:262252232181:targetgroup/stg-webhook-gateway-tg/89d3062f12d81fe6"

$net = "awsvpcConfiguration={subnets=[subnet-0afbd069383af4b67,subnet-0afa34c2182cda8e3],securityGroups=[sg-02bffc5e5deaf7c5c],assignPublicIp=DISABLED}"
$lb = "targetGroupArn=$TgArn,containerName=webhook-gateway,containerPort=3020"

aws ecs create-service `
  --cluster stg-webhook-gateway-cluster `
  --service-name stg-webhook-gateway-service `
  --task-definition stg-webhook-gateway:1 `
  --desired-count 1 `
  --launch-type FARGATE `
  --platform-version LATEST `
  --network-configuration $net `
  --load-balancers $lb `
  --health-check-grace-period-seconds 60 `
  --region $Region `
  --profile $Profile `
  --query "service.{name:serviceName,status:status,desired:desiredCount}" `
  --output json

Write-Host "SERVICE_CREATE_OK"
