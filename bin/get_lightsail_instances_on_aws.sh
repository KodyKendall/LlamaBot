#!/bin/bash

#REGION=us-east-1
REGION=us-east-2

INSTANCE=$1   # Take instance name from first argument

if [ -z "$INSTANCE" ]; then
  echo "Usage: $0 <INSTANCE_NAME>"
  exit 1
fi

# LIST ALL INSTANCES:
aws lightsail get-instances \
  --region $REGION \
  --query "instances[?name=='$INSTANCE'].[name,publicIpAddress]" \
  --output table

