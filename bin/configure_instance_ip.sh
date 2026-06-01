#!/bin/bash
set -e

INSTANCE="${1:-}"
IPADDRESS="${2:-}"

if [ -z "$INSTANCE" ]; then
  read -p "Instance name (e.g. eumir): " INSTANCE
fi
if [ -z "$IPADDRESS" ]; then
  read -p "IP Address: " IPADDRESS
fi

export INSTANCE
export DOMAIN=llamapress.ai.
export ZONE_ID=$(aws route53 list-hosted-zones-by-name \
  --dns-name "$DOMAIN" --query 'HostedZones[0].Id' --output text | sed 's|/hostedzone/||')
echo $ZONE_ID

TARGET_FQDN=$INSTANCE.llamapress.ai.
RAILS_TARGET_FQDN=rails-$TARGET_FQDN
VSCODE_TARGET_FQDN=vscode-$TARGET_FQDN

cat > new-a-record.json <<EOF
{
  "Comment": "Add A records for $TARGET_FQDN for LlamaBot Agent Deploy",
  "Changes": [
    {
      "Action": "UPSERT",
      "ResourceRecordSet": {
        "Name": "${TARGET_FQDN}",
        "Type": "A",
        "TTL": 60,
        "ResourceRecords": [
          { "Value": "${IPADDRESS}" }
        ]
      }
    },
    {
      "Action": "UPSERT",
      "ResourceRecordSet": {
        "Name": "${RAILS_TARGET_FQDN}",
        "Type": "A",
        "TTL": 60,
        "ResourceRecords": [
          { "Value": "${IPADDRESS}" }
        ]
      }
    },
    {
      "Action": "UPSERT",
      "ResourceRecordSet": {
        "Name": "${VSCODE_TARGET_FQDN}",
        "Type": "A",
        "TTL": 60,
        "ResourceRecords": [
          { "Value": "${IPADDRESS}" }
        ]
      }
    }
  ]
}
EOF

aws route53 change-resource-record-sets \
  --hosted-zone-id "$ZONE_ID" \
  --change-batch file://new-a-record.json

echo "Instance created! Now, waiting to open port 443..."