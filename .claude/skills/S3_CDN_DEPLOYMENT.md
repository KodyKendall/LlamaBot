# LlamaBot S3 CDN Deployment Guide

## 🎉 Deployed Successfully!

LlamaBot's frontend JavaScript modules are now hosted on S3 and accessible via CDN!

## 📍 CDN URLs

### Versioned (Stable - Recommended for Production)
```
https://llamapress-cdn.s3.amazonaws.com/llamabot-chat-js-v0.2.19a/index.js
```

### Latest (Auto-updates)
```
https://llamapress-cdn.s3.amazonaws.com/llamabot-chat-js-latest/index.js
```

## 🚀 How to Use in Your Project

### In Leonardo (or any Rails/HTML project):

```html
<script type="module">
  waitForCableConnection(async (consumer) => {
    // Import from S3 CDN
    const { default: LlamaBot } = await import('https://llamapress-cdn.s3.amazonaws.com/llamabot-chat-js-v0.2.19/index.js');

    const sessionId = crypto.randomUUID();

    // Initialize with ActionCable
    const chat = LlamaBot.create('[data-llamabot="chat-container"]', {
      actionCable: {
        consumer: consumer,
        channel: 'LlamaBotRails::ChatChannel',
        session_id: sessionId
      },
      agent: {
        name: 'rails_agent'
      }
    });
  });
</script>
```

## 📦 What's Deployed

All LlamaBot chat modules:
- ✅ `index.js` - Main entry point
- ✅ `config.js` - Configuration
- ✅ `websocket/WebSocketManager.js` - Connection management
- ✅ `websocket/ActionCableAdapter.js` - **NEW!** ActionCable support
- ✅ `messages/MessageRenderer.js` - Message rendering
- ✅ `state/AppState.js` - State management
- ✅ `ui/` - All UI components
- ✅ ... and 15+ more modules

Total size: ~123KB uncompressed

## 🔄 Deploying Updates

### Quick Deploy

```bash
cd /Users/kodykendall/SoftEngineering/LLMPress/LlamaBot
./scripts/deploy-frontend-to-s3.sh
```

### With Version Bump

1. Update version in `scripts/deploy-frontend-to-s3.sh`:
   ```bash
   VERSION="v0.2.20"
   ```

2. Deploy:
   ```bash
   ./scripts/deploy-frontend-to-s3.sh
   ```

3. Update Leonardo to use new version:
   ```javascript
   await import('https://llamapress-cdn.s3.amazonaws.com/llamabot-chat-js-v0.2.20/index.js');
   ```

## 🏗️ Architecture

```
┌─────────────────────────────────┐
│  S3 Bucket: llamapress-cdn      │
│  ┌──────────────────────────┐   │
│  │  llamabot-chat-js-v0.2.19│   │
│  │  ├── index.js            │   │
│  │  ├── config.js           │   │
│  │  ├── websocket/          │   │
│  │  │   ├── WebSocketManager│   │
│  │  │   └── ActionCableAdapter│  │
│  │  ├── messages/           │   │
│  │  ├── state/              │   │
│  │  └── ui/                 │   │
│  └──────────────────────────┘   │
│  ┌──────────────────────────┐   │
│  │  llamabot-chat-js-latest │   │
│  │  (same structure)        │   │
│  └──────────────────────────┘   │
└─────────────────────────────────┘
           ↓
    Public HTTPS Access
           ↓
┌─────────────────────────────────┐
│  Leonardo Project               │
│  ├── chat.html.erb              │
│  │   └── imports from S3 ✅    │
│  └── NO local copy needed       │
└─────────────────────────────────┘
```

## ✅ Benefits

1. **Zero Maintenance** - No need to copy files to each Leonardo project
2. **Instant Updates** - Update S3, all projects get new code
3. **Version Control** - Pin to specific versions for stability
4. **Performance** - S3 CDN caching
5. **DRY Principle** - Single source of truth

## 🔐 Security & Access

- **Bucket Policy**: Public read access enabled
- **CORS**: Configured to allow cross-origin requests
- **IAM User**: `provision-llamapress` has full access to upload/manage

## 📊 Cost Estimate

- **Storage**: ~0.1MB = ~$0.002/month
- **Requests**: 1000 downloads/month = ~$0.004/month
- **Total**: < $0.01/month (essentially free)

## 🎯 Migration Checklist

For each Leonardo project:

- [x] Remove local `llamabot_chat/` folder
- [x] Remove importmap config
- [x] Update chat.html.erb import to S3 URL
- [ ] Test in development environment
- [ ] Deploy to production
- [ ] Monitor for errors

## 🐛 Troubleshooting

### CORS Errors
If you see CORS errors in console:
```bash
./scripts/setup-s3-bucket.sh
```

### 404 Not Found
- Check the URL is correct
- Verify files uploaded: `aws s3 ls s3://llamapress-cdn/llamabot-chat-js-v0.2.19/`

### Module Import Errors
- Ensure all relative imports in JavaScript use correct paths
- Check browser console for specific missing modules

## 📚 Related Documentation

- [LLAMABOT_JAVASCRIPT_REUSABILITY.md](LLAMABOT_JAVASCRIPT_REUSABILITY.md) - Full integration guide
- [app/frontend/chat/STYLING.md](app/frontend/chat/STYLING.md) - Styling paradigm
- [app/frontend/chat/EXAMPLE_USAGE.js](app/frontend/chat/EXAMPLE_USAGE.js) - Usage examples

## 🚀 Next Steps

1. ✅ S3 deployment complete
2. ✅ Leonardo updated to use S3
3. ⏳ Test in Leonardo development environment
4. ⏳ Apply to other Leonardo instances
5. ⏳ Document version upgrade process

---

**Deployed on:** 2025-11-09
**Version:** v0.2.19
**CDN Bucket:** llamapress-cdn (us-east-1)
