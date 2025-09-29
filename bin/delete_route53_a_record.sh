#!/bin/bash
set -e

if [ -z "$1" ]; then
  echo "Usage: $0 <INSTANCE_NAME>"
  echo "e.g.: $0 LP-Test5"
  exit 1
fi

INSTANCE=$(echo "$1" | tr '[:upper:]' '[:lower:]')

export DOMAIN=llamapress.ai.
export ZONE_ID=$(aws route53 list-hosted-zones-by-name \
  --dns-name "$DOMAIN" --query 'HostedZones[0].Id' --output text | sed 's|/hostedzone/||')

if [ -z "$ZONE_ID" ]; then
  echo "Could not find hosted zone for domain $DOMAIN"
  exit 1
fi

echo "Found Zone ID: $ZONE_ID for domain $DOMAIN"

TARGET_FQDN=$INSTANCE.llamapress.ai.
RAILS_TARGET_FQDN=rails-$TARGET_FQDN

IPADDRESS=$(aws route53 list-resource-record-sets --hosted-zone-id "$ZONE_ID" \
   --start-record-name "$TARGET_FQDN" \
   --max-items 1 \
   --query "ResourceRecordSets[0].ResourceRecords[0].Value" \
   --output text)

if [ -z "$IPADDRESS" ]; then
  echo "No A record found for $TARGET_FQDN. Nothing to delete."
  exit 0
fi

echo "Found IP Address $IPADDRESS for $TARGET_FQDN"

JSON_FILE="delete-a-record.json"

cat > $JSON_FILE <<EOF
{
  "Comment": "Delete A records for $TARGET_FQDN for LlamaBot Agent Deploy",
  "Changes": [
    {
      "Action": "DELETE",
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
      "Action": "DELETE",
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

echo "Prepared the following change batch:"
cat $JSON_FILE

read -p "Do you want to apply this change? (y/N) " confirm
if [[ "$confirm" != "y" && "$confirm" != "Y" ]]; then
  echo "Aborting."
  rm $JSON_FILE
  exit 0
fi

aws route53 change-resource-record-sets \
  --hosted-zone-id "$ZONE_ID" \
  --change-batch file://$JSON_FILE

# Clean up the JSON file
rm $JSON_FILE

echo "DNS records for $INSTANCE submitted for deletion."