# LlamaBot S3 CDN Deployment Guide

Complete guide for deploying LlamaBot frontend JavaScript modules to S3 CDN.

## Quick Deploy

```bash
cd /Users/kodykendall/SoftEngineering/LLMPress/LlamaBot
./scripts/deploy-frontend-to-s3.sh
```

This uploads all modules from `app/frontend/chat/` to both:
- **Versioned path**: `llamabot-chat-js-v0.2.19/` (immutable, 1-year cache)
- **Latest path**: `llamabot-chat-js-latest/` (auto-updates, 1-hour cache)

## Prerequisites

### 1. AWS CLI Installed

```bash
# Check if installed
which aws

# Install if needed (macOS)
brew install awscli

# Verify version
aws --version
```

### 2. AWS Credentials Configured

The `provision-llamapress` IAM user has the required permissions.

**Configure credentials:**

```bash
aws configure
```

**Enter:**
- AWS Access Key ID: `[from IAM user]`
- AWS Secret Access Key: `[from IAM user]`
- Default region: `us-east-1`
- Default output format: `json`

**Verify access:**

```bash
aws s3 ls s3://llamapress-cdn/
```

You should see existing `llamabot-chat-js-*` directories.

## S3 Bucket Configuration

**Bucket name:** `llamapress-cdn`
**Region:** `us-east-1`
**Purpose:** Public CDN for LlamaBot JavaScript modules

### Bucket Policy (Public Read Access)

```json
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Sid": "PublicReadGetObject",
      "Effect": "Allow",
      "Principal": "*",
      "Action": "s3:GetObject",
      "Resource": "arn:aws:s3:::llamapress-cdn/*"
    }
  ]
}
```

### CORS Configuration

```json
{
  "CORSRules": [
    {
      "AllowedHeaders": ["*"],
      "AllowedMethods": ["GET", "HEAD"],
      "AllowedOrigins": ["*"],
      "MaxAgeSeconds": 3600
    }
  ]
}
```

**Apply CORS:**

```bash
./scripts/setup-s3-bucket.sh
```

This script configures:
- Bucket policy for public read
- CORS rules for cross-origin loading
- Public access block settings

## Deployment Process

### Standard Deployment

**Deploy current version:**

```bash
cd /Users/kodykendall/SoftEngineering/LLMPress/LlamaBot
./scripts/deploy-frontend-to-s3.sh
```

**What happens:**
1. Validates AWS CLI is installed
2. Validates source directory exists
3. Syncs to versioned path: `llamabot-chat-js-v0.2.19/`
   - Cache: 1 year (`max-age=31536000, immutable`)
   - Excludes: `.md`, `README*`, `.DS_Store`
4. Syncs to latest path: `llamabot-chat-js-latest/`
   - Cache: 1 hour (`max-age=3600`)
   - Same exclusions
5. Displays CDN URLs

**Output:**

```
📦 Deploying LlamaBot frontend to S3...
   Source: app/frontend/chat
   Bucket: s3://llamapress-cdn/llamabot-chat-js-v0.2.19/

🚀 Uploading files...
upload: app/frontend/chat/index.js to s3://llamapress-cdn/llamabot-chat-js-v0.2.19/index.js
[... more files ...]

🔄 Updating 'latest' pointer...
upload: app/frontend/chat/index.js to s3://llamapress-cdn/llamabot-chat-js-latest/index.js
[... more files ...]

✅ Deployment complete!

📍 URLs:
   Versioned: https://llamapress-cdn.s3.amazonaws.com/llamabot-chat-js-v0.2.19/index.js
   Latest:    https://llamapress-cdn.s3.amazonaws.com/llamabot-chat-js-latest/index.js
```

### Version Bump Deployment

**When to bump version:**
- Breaking changes to API
- Major new features
- Significant refactors
- Production releases

**Steps:**

1. **Update version in deployment script:**

```bash
# Edit scripts/deploy-frontend-to-s3.sh
VERSION="v0.2.20"  # Increment from v0.2.19
```

2. **Deploy:**

```bash
./scripts/deploy-frontend-to-s3.sh
```

3. **Update consuming projects (Leonardo):**

```javascript
// Edit Leonardo/rails/app/views/public/chat.html.erb
// Change:
await import('https://llamapress-cdn.s3.amazonaws.com/llamabot-chat-js-v0.2.19/index.js');

// To:
await import('https://llamapress-cdn.s3.amazonaws.com/llamabot-chat-js-v0.2.20/index.js');
```

4. **Test in consuming projects:**

```bash
# Hard refresh browser (Cmd+Shift+R)
# Verify chat works correctly
# Check console for errors
```

5. **Commit version changes:**

```bash
git add scripts/deploy-frontend-to-s3.sh
git commit -m "Bump LlamaBot frontend version to v0.2.20"
```

## IAM Permissions

The `provision-llamapress` IAM user requires these S3 permissions:

```json
{
  "Sid": "LlamaBotCDNFullAccess",
  "Effect": "Allow",
  "Action": [
    "s3:PutObject",
    "s3:PutObjectAcl",
    "s3:GetObject",
    "s3:GetObjectAcl",
    "s3:DeleteObject",
    "s3:ListBucket",
    "s3:ListBucketVersions",
    "s3:GetBucketLocation",
    "s3:GetBucketPolicy",
    "s3:PutBucketPolicy",
    "s3:PutBucketCORS",
    "s3:GetBucketCORS",
    "s3:PutBucketWebsite",
    "s3:GetBucketWebsite",
    "s3:PutPublicAccessBlock",
    "s3:GetPublicAccessBlock"
  ],
  "Resource": [
    "arn:aws:s3:::llamapress-cdn",
    "arn:aws:s3:::llamapress-cdn/*"
  ]
}
```

**Full policy:** See `scripts/iam-policy-updated.json`

**Apply policy:**
1. AWS Console → IAM → Users → provision-llamapress
2. Permissions → Add inline policy
3. Paste JSON from `scripts/iam-policy-updated.json`
4. Name: `LlamaBotCDNAccess`
5. Create policy

## Deployment Script Reference

**File:** `scripts/deploy-frontend-to-s3.sh`

**Key variables:**

```bash
BUCKET="llamapress-cdn"           # S3 bucket name
VERSION="v0.2.19"                  # Current version (UPDATE THIS FOR BUMPS)
S3_PATH="llamabot-chat-js-$VERSION" # Versioned path
SOURCE_DIR="app/frontend/chat"    # Source directory
REGION="us-east-1"                 # AWS region
```

**Cache control headers:**

- **Versioned path:** `public, max-age=31536000, immutable`
  - 1 year cache
  - Immutable (browsers won't revalidate)
  - Safe because version never changes

- **Latest path:** `public, max-age=3600`
  - 1 hour cache
  - Revalidates after expiry
  - Gets updates within an hour

**Exclusions:**

```bash
--exclude "*.md"      # Markdown docs
--exclude "README*"   # README files
--exclude ".DS_Store" # macOS files
```

## Verification After Deployment

### 1. Check Files Uploaded

```bash
aws s3 ls s3://llamapress-cdn/llamabot-chat-js-v0.2.19/
```

Expected output:

```
2025-11-10 PRE websocket/
2025-11-10 PRE messages/
2025-11-10 PRE state/
2025-11-10 PRE ui/
2025-11-10 PRE threads/
2025-11-10 PRE utils/
2025-11-10 16540 index.js
2025-11-10  2789 config.js
...
```

### 2. Test HTTP Access

```bash
curl -I https://llamapress-cdn.s3.amazonaws.com/llamabot-chat-js-v0.2.19/index.js
```

Expected headers:

```
HTTP/1.1 200 OK
Content-Type: application/javascript
Cache-Control: public, max-age=31536000, immutable
Access-Control-Allow-Origin: *
Access-Control-Allow-Methods: GET, HEAD
```

### 3. Test in Browser

```javascript
// Open browser console on any page
const { default: LlamaBot } = await import('https://llamapress-cdn.s3.amazonaws.com/llamabot-chat-js-v0.2.19/index.js');
console.log(LlamaBot); // Should show object with create(), defaultConfig
```

### 4. Test in Leonardo

```bash
cd /path/to/Leonardo
# Start Leonardo
# Visit http://localhost:3000/chat
# Verify:
#   - Green connection dot
#   - Messages send/receive
#   - No console errors
```

## Rollback Procedure

### If Deployment Has Issues

**Option 1: Revert to Previous Version**

Consuming projects can instantly rollback by changing import URL:

```javascript
// Leonardo chat.html.erb
// Change:
await import('https://llamapress-cdn.s3.amazonaws.com/llamabot-chat-js-v0.2.20/index.js');

// Back to:
await import('https://llamapress-cdn.s3.amazonaws.com/llamabot-chat-js-v0.2.19/index.js');
```

Hard refresh browser (Cmd+Shift+R) and chat works with old version.

**Option 2: Re-deploy Previous Version**

If `latest` is broken:

```bash
# 1. Checkout previous commit
git log --oneline app/frontend/chat/  # Find good commit
git checkout <commit-hash> app/frontend/chat/

# 2. Deploy
./scripts/deploy-frontend-to-s3.sh

# 3. Return to current branch
git checkout - app/frontend/chat/
```

**Option 3: Delete Bad Version**

```bash
aws s3 rm s3://llamapress-cdn/llamabot-chat-js-v0.2.20/ --recursive
```

## Troubleshooting

### Permission Denied

**Error:**

```
upload failed: An error occurred (AccessDenied) when calling the PutObject operation
```

**Solution:**
1. Verify AWS credentials: `aws configure list`
2. Check IAM policy has `s3:PutObject` on `llamapress-cdn/*`
3. Verify bucket exists: `aws s3 ls s3://llamapress-cdn/`

### CORS Errors After Deployment

**Error in browser console:**

```
Access to script at 'https://llamapress-cdn.s3.amazonaws.com/...'
from origin 'http://localhost:3000' has been blocked by CORS policy
```

**Solution:**

```bash
./scripts/setup-s3-bucket.sh
```

Then verify CORS:

```bash
aws s3api get-bucket-cors --bucket llamapress-cdn
```

### Files Not Updating

**Problem:** Changes not appearing even after deployment

**Causes:**
1. **Browser cache** - Hard refresh (Cmd+Shift+R)
2. **CDN cache** - Wait up to 1 hour for `latest`, or use versioned path
3. **Wrong path** - Check you're importing correct version

**Verification:**

```bash
# Check file modification time
aws s3 ls s3://llamapress-cdn/llamabot-chat-js-latest/index.js

# Check file content
aws s3 cp s3://llamapress-cdn/llamabot-chat-js-latest/index.js - | head -20
```

### Upload Very Slow

**Problem:** Deployment takes several minutes

**Causes:**
- Large number of unchanged files being checked
- Slow internet connection
- AWS throttling

**Solution:** Use `--size-only` flag (already in script)

```bash
# The script already uses aws s3 sync which:
# - Only uploads changed files
# - Compares size and modification time
# - Skips identical files
```

## Cost Estimation

**Current usage (estimated):**

- **Storage:** ~0.2MB total = **$0.005/month**
- **GET requests:** 10,000/month = **$0.004/month**
- **Data transfer:** 1GB/month = **$0.09/month**

**Total:** ~**$0.10/month** (essentially free)

**At scale (100 projects, 1M requests/month):**

- **Storage:** Same (single copy)
- **GET requests:** 1,000,000/month = **$0.40/month**
- **Data transfer:** 100GB/month = **$9/month**

**Total:** ~**$10/month**

## Monitoring

### Check Deployment History

```bash
# List all versions
aws s3 ls s3://llamapress-cdn/ | grep llamabot-chat-js

# Check version contents
aws s3 ls s3://llamapress-cdn/llamabot-chat-js-v0.2.19/ --recursive --human-readable

# Compare versions
aws s3 ls s3://llamapress-cdn/llamabot-chat-js-v0.2.19/ --recursive > v19.txt
aws s3 ls s3://llamapress-cdn/llamabot-chat-js-v0.2.20/ --recursive > v20.txt
diff v19.txt v20.txt
```

### Monitor Access

**CloudWatch metrics (if enabled):**
- GET request count
- 4xx/5xx error rates
- Data transfer volume

**Enable S3 access logging (optional):**

```bash
aws s3api put-bucket-logging \
  --bucket llamapress-cdn \
  --bucket-logging-status '{
    "LoggingEnabled": {
      "TargetBucket": "llamapress-logs",
      "TargetPrefix": "cdn-access/"
    }
  }'
```

## Automation (Future)

### GitHub Actions (Proposed)

```yaml
# .github/workflows/deploy-s3.yml
name: Deploy to S3 CDN

on:
  push:
    branches: [main]
    paths:
      - 'app/frontend/chat/**'

jobs:
  deploy:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v3

      - name: Configure AWS Credentials
        uses: aws-actions/configure-aws-credentials@v1
        with:
          aws-access-key-id: ${{ secrets.AWS_ACCESS_KEY_ID }}
          aws-secret-access-key: ${{ secrets.AWS_SECRET_ACCESS_KEY }}
          aws-region: us-east-1

      - name: Deploy to S3
        run: ./scripts/deploy-frontend-to-s3.sh
```

### Automated Version Bumping (Proposed)

```bash
#!/bin/bash
# scripts/bump-version.sh

CURRENT=$(grep 'VERSION=' scripts/deploy-frontend-to-s3.sh | cut -d'"' -f2)
NEW_VERSION=$1

if [ -z "$NEW_VERSION" ]; then
  echo "Usage: ./scripts/bump-version.sh v0.2.20"
  exit 1
fi

# Update deployment script
sed -i '' "s/VERSION=\"$CURRENT\"/VERSION=\"$NEW_VERSION\"/" scripts/deploy-frontend-to-s3.sh

# Deploy
./scripts/deploy-frontend-to-s3.sh

# Commit
git add scripts/deploy-frontend-to-s3.sh
git commit -m "Bump version to $NEW_VERSION"

echo "✅ Version bumped to $NEW_VERSION and deployed"
```

## Related Documentation

- **[LLAMABOT_JAVASCRIPT_REUSABILITY.md](LLAMABOT_JAVASCRIPT_REUSABILITY.md)** - Integration guide for consuming projects
- **[.claude/skills/llamabot-s3-cdn.md](.claude/skills/llamabot-s3-cdn.md)** - Complete architecture reference
- **[app/frontend/chat/STYLING.md](app/frontend/chat/STYLING.md)** - Styling guide
- **[scripts/setup-s3-bucket.sh](scripts/setup-s3-bucket.sh)** - Bucket configuration script
- **[scripts/iam-policy-updated.json](scripts/iam-policy-updated.json)** - IAM permissions

## Support

**Issues with deployment?**

1. Check AWS credentials: `aws sts get-caller-identity`
2. Verify bucket access: `aws s3 ls s3://llamapress-cdn/`
3. Test CORS: `curl -I https://llamapress-cdn.s3.amazonaws.com/llamabot-chat-js-latest/index.js`
4. Review script output for specific errors
5. Check CloudWatch logs (if enabled)

**Questions?**

Contact the LlamaBot maintainers or check GitHub issues.

---

**Last Updated:** November 2025
**Current Version:** v0.2.19
**CDN Bucket:** llamapress-cdn (us-east-1)
