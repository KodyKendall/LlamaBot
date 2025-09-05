set -e

read -p "IP Address: " IPADDRESS
export INSTANCE=spot
export DOMAIN=llamapress.ai.
export ZONE_ID=$(aws route53 list-hosted-zones-by-name \
  --dns-name "$DOMAIN" --query 'HostedZones[0].Id' --output text | sed 's|/hostedzone/||')
echo $ZONE_ID

TARGET_FQDN=$INSTANCE.llamapress.ai.
RAILS_TARGET_FQDN=rails-$TARGET_FQDN

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
    }
  ]
}
EOF

aws route53 change-resource-record-sets \
  --hosted-zone-id "$ZONE_ID" \
  --change-batch file://new-a-record.json

echo "Instance created! Now, waiting to open port 443..."