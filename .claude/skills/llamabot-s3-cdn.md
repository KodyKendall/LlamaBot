# LlamaBot S3 CDN Architecture

This skill provides comprehensive documentation on how LlamaBot's frontend JavaScript modules are distributed via S3 CDN and consumed by Leonardo (and other Rails projects).

## Architecture Overview

```
┌─────────────────────────────────────────────────────────────┐
│  LlamaBot Repository                                        │
│  /Users/kodykendall/SoftEngineering/LLMPress/LlamaBot       │
│                                                              │
│  ├── app/frontend/chat/              (Source of Truth)     │
│  │   ├── index.js                    Main entry point       │
│  │   ├── config.js                   Configuration          │
│  │   ├── websocket/                                         │
│  │   │   ├── WebSocketManager.js     Dual connection support│
│  │   │   └── ActionCableAdapter.js   Rails ActionCable     │
│  │   ├── messages/                   Message rendering      │
│  │   ├── state/                      State management       │
│  │   └── ui/                         UI components          │
│  │                                                           │
│  ├── scripts/                                               │
│  │   └── deploy-frontend-to-s3.sh   Deployment script      │
│  │                                                           │
│  └── llama_bot_rails_symlink/        Rails gem (symlinked) │
│      └── Links to: ../../llama_bot_rails/                  │
│                                                              │
└─────────────────────────────────────────────────────────────┘
                            ↓
                    [Deploy to S3]
                            ↓
┌─────────────────────────────────────────────────────────────┐
│  S3 Bucket: llamapress-cdn (us-east-1)                     │
│                                                              │
│  ├── llamabot-chat-js-v0.2.19/       Versioned (stable)    │
│  │   ├── index.js                                           │
│  │   ├── config.js                                          │
│  │   ├── websocket/                                         │
│  │   └── ... (all modules)                                  │
│  │                                                           │
│  └── llamabot-chat-js-latest/        Latest (auto-updates) │
│      └── (same structure)                                   │
│                                                              │
│  Public Access: HTTPS with CORS enabled                    │
└─────────────────────────────────────────────────────────────┘
                            ↓
                    [HTTPS Import]
                            ↓
┌─────────────────────────────────────────────────────────────┐
│  Leonardo Project (Rails)                                   │
│  /Users/kodykendall/SoftEngineering/LLMPress/Leonardo/rails │
│                                                              │
│  ├── app/views/public/chat.html.erb  Chat interface        │
│  │   └── Imports from S3 CDN via dynamic import()          │
│  │                                                           │
│  └── Gemfile                                                │
│      └── gem 'llama_bot_rails', path: '../llama_bot_rails' │
│                                                              │
└─────────────────────────────────────────────────────────────┘
```

## Key Components

### 1. LlamaBot Frontend Modules (`app/frontend/chat/`)

**Purpose**: Reusable, framework-agnostic JavaScript chat interface

**Key Files**:
- `index.js` - Main entry point with `LlamaBot.create()` API
- `config.js` - Configuration with `actionCable` support
- `websocket/WebSocketManager.js` - Handles both native WebSocket and ActionCable
- `websocket/ActionCableAdapter.js` - Adapts ActionCable to WebSocket-like interface

**Design Principles**:
- ES6 modules (no bundler required)
- CSS-framework agnostic using `data-llamabot` attributes
- Dual connection support: native WebSocket OR Rails ActionCable
- Auto-initialization for standalone use (`<script type="module">`)
- Manual initialization for integrated use (`import()`)

**Auto-Initialization Logic**:
```javascript
// Only auto-init if:
// 1. Chat elements exist: document.querySelector('[data-llamabot="message-history"]')
// 2. Not disabled: !window.LlamaBot.skipAutoInit
// 3. Loading via script tag: document.readyState === 'loading' | 'interactive'

const shouldAutoInit =
  !window.LlamaBot.skipAutoInit &&
  document.querySelector('[data-llamabot="message-history"]') &&
  (document.readyState === 'loading' || document.readyState === 'interactive');
```

### 2. S3 CDN Deployment

**Bucket**: `llamapress-cdn` (us-east-1)
**IAM User**: `provision-llamapress`
**Permissions**: See `scripts/iam-policy-updated.json`

**Versioning Strategy**:
- **Versioned path** (`v0.2.19`): Pin to specific stable version
- **Latest path** (`latest`): Auto-updates with each deployment

**Deployment Script**: `scripts/deploy-frontend-to-s3.sh`
```bash
#!/bin/bash
BUCKET="llamapress-cdn"
VERSION="v0.2.19"
S3_PATH="llamabot-chat-js-$VERSION"
SOURCE_DIR="app/frontend/chat"

# Upload versioned (immutable, 1-year cache)
aws s3 sync "$SOURCE_DIR/" "s3://$BUCKET/$S3_PATH/" \
  --cache-control "public, max-age=31536000, immutable" \
  --exclude "*.md" --exclude "README*" --exclude ".DS_Store"

# Upload latest (1-hour cache)
aws s3 sync "$SOURCE_DIR/" "s3://$BUCKET/llamabot-chat-js-latest/" \
  --cache-control "public, max-age=3600" \
  --exclude "*.md" --exclude "README*" --exclude ".DS_Store"
```

**CORS Configuration**: Allows cross-origin module loading
```json
{
  "CORSRules": [{
    "AllowedHeaders": ["*"],
    "AllowedMethods": ["GET", "HEAD"],
    "AllowedOrigins": ["*"],
    "MaxAgeSeconds": 3600
  }]
}
```

### 3. Leonardo Integration

**File**: `/Users/kodykendall/SoftEngineering/LLMPress/Leonardo/rails/app/views/public/chat.html.erb`

**Key Integration Pattern**:
```html
<script type="module">
  // Wait for ActionCable to be ready
  function waitForCableConnection(callback) {
    const interval = setInterval(() => {
      if (window.LlamaBotRails && LlamaBotRails.cable) {
        clearInterval(interval);
        callback(LlamaBotRails.cable);
      }
    }, 50);
  }

  waitForCableConnection(async (consumer) => {
    // Import from S3 CDN (dynamic import)
    const { default: LlamaBot } = await import('https://llamapress-cdn.s3.amazonaws.com/llamabot-chat-js-v0.2.19/index.js');

    const sessionId = crypto.randomUUID();

    // Initialize with ActionCable configuration
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

**Why This Works**:
1. `waitForCableConnection()` ensures ActionCable consumer is ready
2. Dynamic `import()` loads LlamaBot from S3 at runtime
3. `document.readyState` is already `'complete'` → auto-init skipped
4. Manual `LlamaBot.create()` with ActionCable config
5. No duplicate initialization

**HTML Structure**: Uses `data-llamabot` attributes for framework-agnostic styling
```html
<div data-llamabot="chat-container">
  <div data-llamabot="message-history"></div>
  <div data-llamabot="thinking-area"></div>
  <input data-llamabot="message-input" />
  <button data-llamabot="send-button">Send</button>
</div>
```

### 4. llama_bot_rails Gem (Symlink)

**Location**: `/Users/kodykendall/SoftEngineering/LLMPress/LlamaBot/llama_bot_rails_symlink/`
**Symlink Target**: `../../llama_bot_rails/`
**Actual Gem**: `/Users/kodykendall/SoftEngineering/LLMPress/llama_bot_rails/`

**Purpose**: Rails adapter layer providing:
- ActionCable channel (`LlamaBotRails::ChatChannel`)
- Authentication & tenant scoping
- Message routing between Rails and LlamaBot backend
- JavaScript helpers (`LlamaBotRails.cable`)

**Why Symlink?**
- Allows LlamaBot to reference gem during development
- Gem is shared across multiple Leonardo projects
- Single source of truth for Rails integration code

**Gemfile Usage in Leonardo**:
```ruby
gem 'llama_bot_rails', path: '../llama_bot_rails'
```

**How It Works**:
1. Leonardo includes `llama_bot_rails` gem
2. Gem provides `LlamaBotRails::ChatChannel` ActionCable channel
3. Gem exposes `window.LlamaBotRails.cable` (ActionCable consumer)
4. Leonardo's chat.html.erb imports LlamaBot from S3
5. LlamaBot's `ActionCableAdapter` wraps the consumer
6. Messages flow: Browser ↔ ActionCable ↔ LlamaBot Backend

## Common Workflows

### Deploy Frontend Updates to S3

**From LlamaBot repository**:
```bash
cd /Users/kodykendall/SoftEngineering/LLMPress/LlamaBot
./scripts/deploy-frontend-to-s3.sh
```

**What happens**:
1. Syncs `app/frontend/chat/` to S3
2. Updates versioned path (`v0.2.19`)
3. Updates latest path (`latest`)
4. Sets cache headers (1 year for versioned, 1 hour for latest)

**After deployment**:
- Leonardo auto-picks up changes (hard refresh: Cmd+Shift+R)
- No need to modify Leonardo code if using same version

### Version Bump

**1. Update version in deployment script**:
```bash
# Edit scripts/deploy-frontend-to-s3.sh
VERSION="v0.2.20"  # Increment version
```

**2. Deploy to S3**:
```bash
./scripts/deploy-frontend-to-s3.sh
```

**3. Update Leonardo to use new version**:
```javascript
// Edit Leonardo/rails/app/views/public/chat.html.erb
await import('https://llamapress-cdn.s3.amazonaws.com/llamabot-chat-js-v0.2.20/index.js');
```

### Add New Features to LlamaBot

**1. Edit source files**:
```bash
cd /Users/kodykendall/SoftEngineering/LLMPress/LlamaBot
# Edit app/frontend/chat/... files
```

**2. Test locally in LlamaBot**:
```bash
# LlamaBot loads from local files via <script type="module">
# Open http://localhost:3001/frontend/chat.html
```

**3. Deploy to S3**:
```bash
./scripts/deploy-frontend-to-s3.sh
```

**4. Test in Leonardo**:
```bash
# Leonardo loads from S3 via import()
# Open http://localhost:3000/chat
# Hard refresh: Cmd+Shift+R
```

### Modify llama_bot_rails Gem

**Location**: `/Users/kodykendall/SoftEngineering/LLMPress/llama_bot_rails/`

**Common changes**:
- Update `LlamaBotRails::ChatChannel`
- Modify authentication logic
- Add new ActionCable helpers

**After changes**:
```bash
# Restart Leonardo Rails server
cd /Users/kodykendall/SoftEngineering/LLMPress/Leonardo/rails
bundle install  # If gem structure changed
rails restart   # Or Ctrl+C and restart
```

## Troubleshooting

### Leonardo Shows Red Dot / Not Connecting

**Check**:
1. ActionCable connection: Look for `LlamaBotRails.cable` in console
2. S3 module loading: Check Network tab for 200 OK on index.js
3. CORS errors: Should not see "blocked by CORS policy"

**Fix**:
```bash
# Verify CORS on S3
aws s3api get-bucket-cors --bucket llamapress-cdn

# Re-apply if needed
cd /Users/kodykendall/SoftEngineering/LLMPress/LlamaBot
./scripts/setup-s3-bucket.sh
```

### LlamaBot's Own Chat Not Loading

**Check**:
1. Auto-initialization should trigger
2. Look for console error: "WebSocket connection failed"
3. Verify `document.readyState` during load

**Debug**:
```javascript
// Add to app/frontend/chat/index.js (before auto-init)
console.log('Document state:', document.readyState);
console.log('Should auto-init:', shouldAutoInit);
```

### Duplicate Initialization in Leonardo

**Symptom**: Two chat instances, one trying native WebSocket

**Cause**: Auto-init not properly disabled

**Fix**: Verify `document.readyState === 'complete'` during dynamic import
```javascript
// In index.js auto-init check
const shouldAutoInit =
  !window.LlamaBot.skipAutoInit &&
  document.querySelector('[data-llamabot="message-history"]') &&
  (document.readyState === 'loading' || document.readyState === 'interactive');
```

### Infinite Recursion in mergeConfig

**Symptom**: "Maximum call stack size exceeded" error

**Cause**: ActionCable Consumer object has circular references

**Fix**: Already implemented in `index.js` - skips deep merge for:
- DOM elements
- ActionCable objects (`Consumer`, `Subscription`)
- Arrays

### S3 Upload Permission Denied

**Error**: "User is not authorized to perform: s3:PutObject"

**Fix**: Update IAM policy
```bash
# Use policy from scripts/iam-policy-updated.json
# Apply via AWS Console → IAM → Users → provision-llamapress → Permissions
```

## Complete Configuration Reference

### All Config Options

```javascript
const chat = LlamaBot.create('[data-llamabot="chat-container"]', {
  // ============================================
  // CONNECTION (choose one)
  // ============================================

  // Option A: Native WebSocket
  websocketUrl: 'ws://localhost:8080/ws',

  // Option B: ActionCable (Rails projects)
  actionCable: {
    consumer: LlamaBotRails.cable,        // ActionCable consumer
    channel: 'LlamaBotRails::ChatChannel', // Channel name
    session_id: crypto.randomUUID()        // Unique session ID
  },

  // ============================================
  // AGENT CONFIGURATION
  // ============================================

  agent: {
    name: 'rails_agent',           // Agent name
    type: 'default'                // Agent type
  },

  // Agent mode mappings (optional)
  agentModes: {
    prototype: 'rails_frontend_starter_agent',
    engineer: 'rails_agent',
    ai_builder: 'rails_ai_builder_agent',
    testing: 'rails_testing_agent'
  },

  // ============================================
  // CSS CLASS CUSTOMIZATION (NEW!)
  // ============================================

  cssClasses: {
    // Message styling (Tailwind/Bootstrap/DaisyUI)
    humanMessage: 'bg-indigo-100 text-indigo-900 p-3 rounded-lg mb-2 max-w-3xl',
    aiMessage: 'bg-gray-100 text-gray-900 p-3 rounded-lg mb-2 max-w-4xl prose prose-sm',
    errorMessage: 'bg-red-100 text-red-800 p-3 rounded-lg mb-2',
    queuedMessage: 'bg-yellow-50 text-yellow-800 p-3 rounded-lg mb-2',

    // Connection status styling
    connectionStatusConnected: 'h-3 w-3 rounded-full bg-green-400 transition-colors',
    connectionStatusDisconnected: 'h-3 w-3 rounded-full bg-red-400 transition-colors'
  },

  // ============================================
  // CUSTOM CALLBACKS
  // ============================================

  onMessageReceived: (data) => {
    console.log('Message received:', data);
    // data contains: { type, content, baseMessage }
  },

  onToolResult: (result) => {
    console.log('Tool result:', result);
    // result contains: { tool_name, args, output }
  },

  onError: (error) => {
    console.error('Chat error:', error);
    // Handle errors (connection, parsing, etc.)
  },

  // ============================================
  // CUSTOM RENDERERS (Advanced)
  // ============================================

  // Override specific message type rendering
  messageRenderers: {
    'custom_type': (content, baseMessage, renderer) => {
      const div = document.createElement('div');
      div.className = 'custom-message';
      div.textContent = content;
      return div;
    }
  },

  // Override specific tool rendering
  toolRenderers: {
    'custom_tool': (toolName, args, result, renderer) => {
      return `<div class="tool">${toolName}: ${result}</div>`;
    }
  },

  // ============================================
  // BEHAVIOR SETTINGS
  // ============================================

  autoSendSuggestedPrompts: true,    // Auto-send when clicking suggested prompt button
  iframeRefreshMs: 500,               // Iframe refresh delay (for LlamaBot's preview)
  scrollThreshold: 50,                // Pixels from bottom to consider "at bottom"
  reconnectDelay: 3000,               // WebSocket reconnection delay (ms)
  railsDebugTimeout: 250,             // Rails debug info timeout (ms)
  cookieExpiryDays: 365,              // Cookie expiration

  // ============================================
  // MARKDOWN SETTINGS
  // ============================================

  markdownOptions: {
    breaks: true,        // Convert \n to <br>
    gfm: true,           // GitHub Flavored Markdown
    sanitize: false,     // Allow HTML (we handle XSS differently)
    smartLists: true,    // Better list parsing
    smartypants: true    // Smart quotes, dashes
  }
});
```

### CSS Class Customization (New Feature)

**Purpose:** Apply custom CSS classes to messages and connection status without writing CSS selectors.

**When to use:**
- **Tailwind projects** - Pass Tailwind utility classes
- **Bootstrap projects** - Pass Bootstrap classes
- **DaisyUI projects** - Pass DaisyUI component classes
- **Custom CSS** - Pass your own class names

**Example: Tailwind**

```javascript
cssClasses: {
  humanMessage: 'bg-blue-50 text-blue-900 p-4 rounded-lg shadow-sm max-w-2xl ml-auto',
  aiMessage: 'bg-gray-50 text-gray-900 p-4 rounded-lg shadow-sm max-w-3xl prose prose-slate',
  errorMessage: 'bg-red-50 text-red-900 p-4 rounded-lg border-l-4 border-red-500',
  connectionStatusConnected: 'w-2 h-2 rounded-full bg-green-500 animate-pulse',
  connectionStatusDisconnected: 'w-2 h-2 rounded-full bg-red-500'
}
```

**Example: DaisyUI**

```javascript
cssClasses: {
  humanMessage: 'chat chat-end',
  aiMessage: 'chat chat-start',
  errorMessage: 'alert alert-error'
}
```

**Alternative:** Instead of `cssClasses`, you can write CSS targeting data-llamabot attributes:

```css
[data-llamabot="human-message"] {
  background: #e0e7ff;
  padding: 1rem;
  border-radius: 0.5rem;
}
```

### Suggested Prompts (New Feature)

**Purpose:** Quick action buttons that fill the input and send messages.

**HTML:**

```html
<button data-llamabot="suggested-prompt" class="btn btn-sm">
  What's the weather?
</button>
```

**Behavior:**
1. User clicks button
2. Input is filled with button text
3. Message is automatically sent (unless `autoSendSuggestedPrompts: false`)

**Configuration:**

```javascript
LlamaBot.create('[data-llamabot="chat-container"]', {
  autoSendSuggestedPrompts: false,  // Disable auto-send, just fill input
  // ... other config
});
```

**Use cases:**
- Quick actions ("Show me today's tasks", "Generate a report")
- Common questions ("What's the weather?", "Tell me a joke")
- Template messages with placeholders

### Custom Callbacks

**onMessageReceived** - Called when any message arrives

```javascript
onMessageReceived: (data) => {
  console.log('Type:', data.type);       // 'human', 'ai', 'tool', 'error'
  console.log('Content:', data.content); // Message content
  console.log('Base:', data.baseMessage); // Full LangGraph message object

  // Example: Track analytics
  if (data.type === 'ai') {
    analytics.track('ai_response_received', {
      length: data.content.length
    });
  }
}
```

**onToolResult** - Called when tool execution completes

```javascript
onToolResult: (result) => {
  console.log('Tool:', result.tool_name);
  console.log('Args:', result.args);
  console.log('Output:', result.output);

  // Example: Log tool usage
  if (result.tool_name === 'web_search') {
    logToolUsage('web_search', result.args.query);
  }
}
```

**onError** - Called on errors (connection, parsing, etc.)

```javascript
onError: (error) => {
  console.error('Error:', error);

  // Example: Send to error tracking service
  Sentry.captureException(error);

  // Example: Show user-friendly message
  showNotification('Oops! Something went wrong. Please try again.');
}
```

### Custom Renderers

**Message Renderers** - Override how specific message types render

```javascript
messageRenderers: {
  'plan': (content, baseMessage, renderer) => {
    // Custom rendering for 'plan' type messages
    const div = document.createElement('div');
    div.className = 'plan-message bg-blue-50 p-4 rounded-lg';
    div.innerHTML = `
      <h3 class="font-bold mb-2">Plan</h3>
      <div class="prose">${renderer.markdownParser.parse(content)}</div>
    `;
    return div;
  }
}
```

**Tool Renderers** - Override how specific tools render

```javascript
toolRenderers: {
  'web_search': (toolName, args, result, renderer) => {
    // Custom rendering for web_search tool
    return `
      <div class="tool-result bg-purple-50 p-3 rounded">
        <div class="font-semibold">🔍 Web Search: ${args.query}</div>
        <div class="mt-2 text-sm">${result}</div>
      </div>
    `;
  }
}
```

## Architecture Benefits

### DRY Principle
- ✅ Single source of truth: `LlamaBot/app/frontend/chat/`
- ✅ Eliminated 246KB of duplicate code from Leonardo
- ✅ No need to copy files to each project

### Zero Maintenance
- ✅ Update once in LlamaBot, deploy to S3
- ✅ All Leonardo projects get updates instantly (or pin to version)
- ✅ No git submodules or npm packages

### Version Control
- ✅ Pin to specific version for stability
- ✅ Use `latest` for auto-updates
- ✅ Easy rollback by changing version number

### Performance
- ✅ S3 CDN caching (1 year for versioned assets)
- ✅ HTTP/2 server push support
- ✅ Minimal overhead (~123KB uncompressed)

### Framework Agnostic
- ✅ Works with any CSS framework (Tailwind, Bootstrap, etc.)
- ✅ `data-llamabot` attribute paradigm separates structure from style
- ✅ No CSS distributed, only JavaScript

## Critical Implementation Details

### mergeConfig Handles Circular References

```javascript
mergeConfig(defaults, userConfig) {
  const result = { ...defaults };

  for (const key in userConfig) {
    const value = userConfig[key];

    // Skip deep merge for DOM/ActionCable/Arrays
    if (
      value instanceof Element ||
      value.constructor?.name === 'Consumer' ||
      value.constructor?.name === 'Subscription' ||
      Array.isArray(value)
    ) {
      result[key] = value;  // Shallow copy only
    }
    // Deep merge plain objects
    else if (typeof value === 'object' && value.constructor === Object) {
      result[key] = this.mergeConfig(defaults[key] || {}, value);
    }
    else {
      result[key] = value;
    }
  }

  return result;
}
```

### ActionCableAdapter Wraps Rails Consumer

```javascript
export class ActionCableAdapter {
  constructor(consumer, channelConfig, messageHandler) {
    this.consumer = consumer;
    this.channelConfig = channelConfig;
  }

  connect() {
    this.subscription = this.consumer.subscriptions.create(
      this.channelConfig,
      {
        connected: () => this.handleConnected(),
        disconnected: () => this.handleDisconnected(),
        received: (data) => this.handleReceived(data)
      }
    );
  }

  send(data) {
    const messageData = JSON.parse(data);
    this.subscription.send(messageData);
  }

  // Provides WebSocket-like interface
  onopen() {}
  onclose() {}
  onmessage() {}
  onerror() {}
}
```

### WebSocketManager Detects Connection Type

```javascript
connect() {
  if (this.config.actionCable) {
    return this.connectActionCable();  // Use ActionCable
  } else {
    return this.connectWebSocket();     // Use native WebSocket
  }
}

connectActionCable() {
  const { consumer, channel, session_id } = this.config.actionCable;
  this.socket = new ActionCableAdapter(consumer, { channel, session_id });
  this.isActionCable = true;
  this.socket.connect();
  return this.socket;
}
```

## Related Documentation

- [LLAMABOT_JAVASCRIPT_REUSABILITY.md](../LLAMABOT_JAVASCRIPT_REUSABILITY.md) - Full architecture guide
- [S3_CDN_DEPLOYMENT.md](../S3_CDN_DEPLOYMENT.md) - Deployment instructions
- [app/frontend/chat/STYLING.md](../app/frontend/chat/STYLING.md) - Styling paradigm
- [app/frontend/chat/EXAMPLE_USAGE.js](../app/frontend/chat/EXAMPLE_USAGE.js) - Usage examples

## Cost Estimate

**S3 Storage**: ~0.1MB = $0.002/month
**Requests**: 1000 downloads/month = $0.004/month
**Total**: < $0.01/month (essentially free)

## Future Enhancements

- [ ] Bundle option for production (single file)
- [ ] TypeScript definitions
- [ ] NPM package mirror
- [ ] CloudFront CDN for faster global delivery
- [ ] Automated CI/CD deployment on git push
