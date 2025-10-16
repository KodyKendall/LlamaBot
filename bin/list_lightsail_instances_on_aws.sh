REGION=us-east-2

aws lightsail get-instances \
  --region $REGION \
  --query 'instances[*].[name,publicIpAddress]' \
  --output table