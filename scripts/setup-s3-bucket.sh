#!/bin/bash
# Setup S3 bucket for LlamaBot frontend hosting
# This only needs to be run once

set -e

BUCKET="llamapress-cdn"
REGION="us-east-1"

echo "🪣 Setting up S3 bucket: $BUCKET"
echo ""

# Check if AWS CLI is installed
if ! command -v aws &> /dev/null; then
    echo "❌ Error: AWS CLI is not installed"
    echo "   Install with: brew install awscli"
    exit 1
fi

# Create bucket
echo "📦 Creating bucket..."
if aws s3 mb "s3://$BUCKET" --region "$REGION" 2>/dev/null; then
    echo "✅ Bucket created successfully"
else
    echo "⚠️  Bucket already exists or you don't have permission"
fi

# Enable public access settings (required for public-read ACL)
echo "🔓 Configuring public access..."
aws s3api put-public-access-block \
    --bucket "$BUCKET" \
    --region "$REGION" \
    --public-access-block-configuration \
        "BlockPublicAcls=false,IgnorePublicAcls=false,BlockPublicPolicy=false,RestrictPublicBuckets=false" \
    2>/dev/null || echo "⚠️  Could not configure public access (may already be set or need permissions)"

# Set bucket policy to allow public read
echo "📝 Setting bucket policy for public read..."
cat > /tmp/bucket-policy.json <<EOF
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Sid": "PublicReadGetObject",
      "Effect": "Allow",
      "Principal": "*",
      "Action": "s3:GetObject",
      "Resource": "arn:aws:s3:::$BUCKET/*"
    }
  ]
}
EOF

aws s3api put-bucket-policy \
    --bucket "$BUCKET" \
    --region "$REGION" \
    --policy file:///tmp/bucket-policy.json \
    2>/dev/null || echo "⚠️  Could not set bucket policy (may need permissions)"

rm /tmp/bucket-policy.json

# Enable static website hosting (optional, but useful)
echo "🌐 Enabling static website hosting..."
aws s3 website "s3://$BUCKET" \
    --index-document index.html \
    --region "$REGION" \
    2>/dev/null || echo "⚠️  Could not enable website hosting (may need permissions)"

# Set CORS configuration to allow loading from any domain
echo "🔗 Setting CORS configuration..."
cat > /tmp/cors-config.json <<EOF
{
  "CORSRules": [
    {
      "AllowedHeaders": ["*"],
      "AllowedMethods": ["GET", "HEAD"],
      "AllowedOrigins": ["*"],
      "ExposeHeaders": [],
      "MaxAgeSeconds": 3600
    }
  ]
}
EOF

aws s3api put-bucket-cors \
    --bucket "$BUCKET" \
    --region "$REGION" \
    --cors-configuration file:///tmp/cors-config.json \
    2>/dev/null || echo "⚠️  Could not set CORS (may need permissions)"

rm /tmp/cors-config.json

echo ""
echo "✅ S3 bucket setup complete!"
echo ""
echo "📍 Bucket URL: https://$BUCKET.s3.amazonaws.com/"
echo "📍 Website URL: http://$BUCKET.s3-website-$REGION.amazonaws.com/"
echo ""
echo "Next step: Run ./scripts/deploy-frontend-to-s3.sh to upload files"
echo ""
