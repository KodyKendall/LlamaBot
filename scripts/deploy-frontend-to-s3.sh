#!/bin/bash
# Deploy LlamaBot frontend JavaScript to S3
# Usage: ./scripts/deploy-frontend-to-s3.sh

set -e

# Configuration
BUCKET="llamapress-cdn"
VERSION="v0.2.19a"
S3_PATH="llamabot-chat-js-$VERSION"
SOURCE_DIR="app/frontend/chat"
REGION="us-east-1"  # Change this to your bucket's region if different

echo "📦 Deploying LlamaBot frontend to S3..."
echo "   Source: $SOURCE_DIR"
echo "   Bucket: s3://$BUCKET/$S3_PATH/"
echo ""

# Check if AWS CLI is installed
if ! command -v aws &> /dev/null; then
    echo "❌ Error: AWS CLI is not installed"
    echo "   Install with: brew install awscli"
    exit 1
fi

# Check if source directory exists
if [ ! -d "$SOURCE_DIR" ]; then
    echo "❌ Error: Source directory not found: $SOURCE_DIR"
    exit 1
fi

# Upload to S3 with versioned path
echo "🚀 Uploading files..."
aws s3 sync "$SOURCE_DIR/" "s3://$BUCKET/$S3_PATH/" \
  --region "$REGION" \
  --cache-control "public, max-age=31536000, immutable" \
  --exclude "*.md" \
  --exclude "README*" \
  --exclude ".DS_Store"

# Also create/update a 'latest' pointer
echo "🔄 Updating 'latest' pointer..."
aws s3 sync "$SOURCE_DIR/" "s3://$BUCKET/llamabot-chat-js-latest/" \
  --region "$REGION" \
  --cache-control "public, max-age=3600" \
  --exclude "*.md" \
  --exclude "README*" \
  --exclude ".DS_Store"

echo ""
echo "✅ Deployment complete!"
echo ""
echo "📍 URLs:"
echo "   Versioned: https://$BUCKET.s3.amazonaws.com/$S3_PATH/index.js"
echo "   Latest:    https://$BUCKET.s3.amazonaws.com/llamabot-chat-js-latest/index.js"
echo ""
echo "📝 To use in Leonardo, change the import to:"
echo "   const { default: LlamaBot } = await import('https://$BUCKET.s3.amazonaws.com/$S3_PATH/index.js');"
echo ""
