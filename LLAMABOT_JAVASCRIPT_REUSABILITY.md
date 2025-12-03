# LlamaBot JavaScript Reusability Guide

## Overview

LlamaBot's chat JavaScript modules are distributed via **S3 CDN** and support both native WebSocket and ActionCable connections, making them reusable across multiple projects (Leonardo, LlamaPress-Simple, etc.) while maintaining DRY principles.

**No file copying required** - projects import directly from S3.

## Architecture

### S3 CDN Distribution

```
┌─────────────────────────────────────┐
│  LlamaBot Repository                │
│  app/frontend/chat/ (source)        │
└─────────────────────────────────────┘
              ↓ deploy
┌─────────────────────────────────────┐
│  S3 Bucket: llamapress-cdn          │
│  llamabot-chat-js-v0.2.19/          │
│  llamabot-chat-js-latest/           │
└─────────────────────────────────────┘
              ↓ HTTPS import
┌─────────────────────────────────────┐
│  Leonardo / LlamaPress-Simple       │
│  import from S3 CDN                 │
└─────────────────────────────────────┘
```

### Dual Connection Support

LlamaBot supports two connection types:

1. **Native WebSocket** - LlamaBot's own chat interface
2. **ActionCable** - Rails projects (Leonardo, LlamaPress-Simple)

The `ActionCableAdapter` wraps ActionCable to provide a WebSocket-like interface, allowing all LlamaBot modules to work seamlessly with either connection type.

## Quick Start: Integrating LlamaBot in a Rails Project

### Step 1: Create HTML with data-llamabot Attributes

Follow the data-llamabot attribute paradigm (see `app/frontend/chat/STYLING.md`):

```html
<div data-llamabot="chat-container" class="flex flex-col h-screen">
  <!-- Header with connection status -->
  <div class="flex items-center border-b p-4">
    <h1>Chat</h1>
    <div data-llamabot="connection-status" class="h-3 w-3 rounded-full"></div>
  </div>

  <!-- Message history -->
  <div data-llamabot="message-history" class="flex-grow overflow-y-auto p-4"></div>

  <!-- Thinking indicator -->
  <div data-llamabot="thinking-area" class="hidden"></div>

  <!-- Input area -->
  <div class="border-t p-4 flex">
    <input
      data-llamabot="message-input"
      type="text"
      placeholder="Type your message..."
      class="flex-grow border rounded-l-lg px-4 py-2"
    />
    <button
      data-llamabot="send-button"
      class="bg-indigo-600 text-white px-4 py-2 rounded-r-lg"
      disabled
    >
      Send
    </button>
  </div>
</div>
```

### Step 2: Wait for ActionCable and Import from S3

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
    // Import LlamaBot from S3 CDN
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
      },
      // Optional: Tailwind CSS classes for styling
      cssClasses: {
        humanMessage: 'bg-indigo-100 text-indigo-900 p-3 rounded-lg mb-2',
        aiMessage: 'bg-gray-100 text-gray-900 p-3 rounded-lg mb-2 prose',
        errorMessage: 'bg-red-100 text-red-800 p-3 rounded-lg mb-2',
        connectionStatusConnected: 'h-3 w-3 rounded-full bg-green-400',
        connectionStatusDisconnected: 'h-3 w-3 rounded-full bg-red-400'
      }
    });

    console.log('Chat initialized with LlamaBot from S3');
  });
</script>
```

### Step 3: (Optional) Add Suggested Prompts

```html
<!-- Suggested prompts -->
<div class="p-4 border-t">
  <div class="text-sm text-gray-600 mb-2">Quick actions:</div>
  <div class="flex flex-wrap gap-2">
    <button
      data-llamabot="suggested-prompt"
      class="px-3 py-1 text-sm bg-gray-100 hover:bg-gray-200 rounded-full"
    >
      What's the weather like today?
    </button>
    <button
      data-llamabot="suggested-prompt"
      class="px-3 py-1 text-sm bg-gray-100 hover:bg-gray-200 rounded-full"
    >
      Tell me a fun fact.
    </button>
  </div>
</div>
```

Clicking a suggested prompt button will:
1. Fill the input with the prompt text
2. Automatically send the message (configurable via `autoSendSuggestedPrompts: false`)

That's it! No file copying, no importmap configuration needed.

## Configuration Options

### Complete Configuration Reference

```javascript
const chat = LlamaBot.create('[data-llamabot="chat-container"]', {
  // CONNECTION TYPE (choose one)

  // Option A: Native WebSocket
  websocketUrl: 'ws://localhost:8080/ws',

  // Option B: ActionCable (Rails projects)
  actionCable: {
    consumer: LlamaBotRails.cable,
    channel: 'LlamaBotRails::ChatChannel',
    session_id: crypto.randomUUID()
  },

  // AGENT CONFIGURATION
  agent: {
    name: 'rails_agent',           // Agent name
    type: 'default'                 // Agent type
  },

  // AGENT MODE MAPPINGS (optional)
  agentModes: {
    prototype: 'rails_frontend_starter_agent',
    engineer: 'rails_agent',
    ai_builder: 'rails_ai_builder_agent',
    testing: 'rails_testing_agent'
  },

  // CSS CLASS CUSTOMIZATION (Tailwind/Bootstrap/DaisyUI)
  cssClasses: {
    humanMessage: 'bg-indigo-100 p-3 rounded-lg',
    aiMessage: 'bg-gray-100 p-3 rounded-lg prose',
    errorMessage: 'bg-red-100 text-red-800 p-3 rounded-lg',
    queuedMessage: 'bg-yellow-50 p-3 rounded-lg',
    connectionStatusConnected: 'bg-green-400',
    connectionStatusDisconnected: 'bg-red-400'
  },

  // CUSTOM CALLBACKS
  onMessageReceived: (data) => {
    console.log('Message received:', data);
  },
  onToolResult: (result) => {
    console.log('Tool result:', result);
  },
  onError: (error) => {
    console.error('Chat error:', error);
  },

  // CUSTOM RENDERERS (advanced)
  toolRenderers: {},
  messageRenderers: {},

  // BEHAVIOR SETTINGS
  autoSendSuggestedPrompts: true,    // Auto-send when clicking suggested prompt
  iframeRefreshMs: 500,               // Iframe refresh delay
  scrollThreshold: 50,                // Pixels from bottom to consider "at bottom"
  reconnectDelay: 3000,               // WebSocket reconnection delay (ms)
  cookieExpiryDays: 365,              // Cookie expiration

  // MARKDOWN SETTINGS
  markdownOptions: {
    breaks: true,
    gfm: true,
    sanitize: false,
    smartLists: true,
    smartypants: true
  }
});
```

## Styling Approaches

### Approach 1: CSS Classes via Config (Recommended for Tailwind)

Pass CSS classes directly in configuration:

```javascript
cssClasses: {
  humanMessage: 'bg-indigo-100 text-indigo-900 p-3 rounded-lg max-w-3xl',
  aiMessage: 'bg-gray-100 text-gray-900 p-3 rounded-lg max-w-4xl prose prose-sm'
}
```

### Approach 2: CSS Targeting data-llamabot Attributes

Create CSS that targets the attributes:

```css
[data-llamabot="human-message"] {
  background-color: #e0e7ff;
  color: #312e81;
  padding: 0.75rem;
  border-radius: 0.5rem;
  max-width: 48rem;
}

[data-llamabot="ai-message"] {
  background-color: #f3f4f6;
  color: #1f2937;
  padding: 0.75rem;
  border-radius: 0.5rem;
}
```

### Approach 3: DaisyUI Components

Use DaisyUI classes via config:

```javascript
cssClasses: {
  humanMessage: 'chat chat-end',
  aiMessage: 'chat chat-start'
}
```

## Version Management

### Pinning to Specific Version (Recommended for Production)

```javascript
await import('https://llamapress-cdn.s3.amazonaws.com/llamabot-chat-js-v0.2.19/index.js');
```

**Benefits:**
- Stable, immutable
- Won't break with updates
- Cache-friendly (1-year cache)

### Using Latest Version (Development)

```javascript
await import('https://llamapress-cdn.s3.amazonaws.com/llamabot-chat-js-latest/index.js');
```

**Benefits:**
- Auto-updates with deployments
- Always newest features
- Shorter cache (1-hour)

### Upgrading Versions

1. Check release notes in LlamaBot repo
2. Test with `llamabot-chat-js-latest` first
3. Update version in production:
   ```javascript
   // Change v0.2.19 → v0.2.20
   await import('https://llamapress-cdn.s3.amazonaws.com/llamabot-chat-js-v0.2.20/index.js');
   ```

## Key Benefits

### 1. Zero Maintenance
- ✅ No file copying to each project
- ✅ Update once in LlamaBot, deploy to S3
- ✅ All projects get updates instantly (or pin to stable version)

### 2. DRY Principle
- ✅ Single source of truth: LlamaBot/app/frontend/chat/
- ✅ No code duplication across projects
- ✅ Bug fixes benefit all consumers

### 3. Version Control
- ✅ Pin to specific version for stability
- ✅ Use `latest` for auto-updates
- ✅ Easy rollback by changing version number

### 4. Performance
- ✅ S3 CDN caching
- ✅ HTTP/2 support
- ✅ Minimal overhead (~123KB uncompressed)

### 5. Framework Agnostic
- ✅ Works with Tailwind, Bootstrap, DaisyUI, vanilla CSS
- ✅ data-llamabot attributes separate structure from styling
- ✅ No CSS distributed, only JavaScript

## Message Flow

### ActionCable Flow (Rails Projects)

```
User Input
   ↓
WebSocketManager (detects ActionCable config)
   ↓
ActionCableAdapter (wraps consumer)
   ↓
ActionCable Subscription
   ↓
ChatChannel (llama_bot_rails gem)
   ↓
LlamaBot Backend WebSocket
   ↓
Response flows back through same path
   ↓
MessageHandler (routes by type)
   ↓
MessageRenderer (renders to DOM)
   ↓
User sees message
```

### Native WebSocket Flow (LlamaBot's Own Chat)

```
User Input
   ↓
WebSocketManager (detects websocketUrl)
   ↓
Native WebSocket connection
   ↓
LlamaBot Backend WebSocket
   ↓
Response flows back
   ↓
MessageHandler → MessageRenderer
   ↓
User sees message
```

## Components

### Core Modules (Always Used)

- **index.js** - Entry point, factory API, auto-initialization
- **config.js** - Configuration defaults
- **websocket/WebSocketManager.js** - Connection management (dual mode)
- **websocket/ActionCableAdapter.js** - ActionCable wrapper
- **websocket/MessageHandler.js** - Message routing
- **messages/MessageRenderer.js** - Message rendering
- **messages/MarkdownParser.js** - Markdown parsing
- **messages/ToolMessageRenderer.js** - Tool message rendering
- **state/AppState.js** - State management

### Optional Modules (Initialize if Elements Present)

- **ui/IframeManager.js** - Iframe preview (LlamaBot only)
- **ui/ScrollManager.js** - Auto-scroll and unread badge
- **ui/MenuManager.js** - Hamburger menu and drawer
- **ui/ElementSelector.js** - Element selection tool
- **ui/MobileViewManager.js** - Mobile/desktop toggle
- **threads/ThreadManager.js** - Conversation threads

## Required HTML Attributes

Minimum required for basic chat:

- `[data-llamabot="chat-container"]` - Root container
- `[data-llamabot="message-history"]` - Message display area
- `[data-llamabot="message-input"]` - Text input
- `[data-llamabot="send-button"]` - Send button

Optional but recommended:

- `[data-llamabot="connection-status"]` - Connection indicator
- `[data-llamabot="thinking-area"]` - "AI is thinking..." indicator
- `[data-llamabot="suggested-prompt"]` - Quick action buttons

See `app/frontend/chat/STYLING.md` for complete attribute reference.

## Auto-Initialization Logic

LlamaBot distinguishes between two loading scenarios:

### Scenario 1: Script Tag (LlamaBot's own chat.html)

```html
<script type="module" src="/frontend/chat/index.js"></script>
```

- Page loads, script runs during page load
- `document.readyState` is `'loading'` or `'interactive'`
- **Result:** Auto-initializes with default config

### Scenario 2: Dynamic Import (Leonardo, Rails projects)

```javascript
await import('https://llamapress-cdn.s3.amazonaws.com/...');
```

- Page fully loaded, then import executes
- `document.readyState` is `'complete'`
- **Result:** Does NOT auto-initialize, waits for manual `LlamaBot.create()`

This prevents duplicate initialization when projects manually configure LlamaBot.

## Advanced: Custom Message Renderers

Override how specific message types render:

```javascript
const chat = LlamaBot.create('[data-llamabot="chat-container"]', {
  messageRenderers: {
    'custom_type': (content, baseMessage, renderer) => {
      const div = document.createElement('div');
      div.className = 'custom-message';
      div.textContent = content;
      return div;
    }
  }
});
```

## Advanced: Custom Tool Renderers

Override how specific tools render:

```javascript
const chat = LlamaBot.create('[data-llamabot="chat-container"]', {
  toolRenderers: {
    'custom_tool': (toolName, args, result, renderer) => {
      return `
        <div class="tool-result">
          <strong>${toolName}</strong>: ${result}
        </div>
      `;
    }
  }
});
```

## Troubleshooting

### Connection Status Not Updating

**Problem:** Connection indicator stays red/yellow

**Solution:** Ensure you configured `cssClasses.connectionStatusConnected`:
```javascript
cssClasses: {
  connectionStatusConnected: 'bg-green-400',
  connectionStatusDisconnected: 'bg-red-400'
}
```

### Messages Have No Styling

**Problem:** Messages appear unstyled

**Solution:** Either:
- Add `cssClasses` config with Tailwind classes, OR
- Write CSS targeting `[data-llamabot="human-message"]` attributes

### Suggested Prompts Not Working

**Problem:** Clicking prompts does nothing

**Solution:** Ensure buttons have `data-llamabot="suggested-prompt"` attribute:
```html
<button data-llamabot="suggested-prompt">Prompt text</button>
```

### CORS Errors

**Problem:** `blocked by CORS policy` in console

**Solution:** This shouldn't happen with S3 CDN (CORS configured). If it does:
```bash
cd /path/to/LlamaBot
./scripts/setup-s3-bucket.sh
```

### Module Not Found

**Problem:** 404 on `https://llamapress-cdn.s3.amazonaws.com/...`

**Solution:** Verify the version exists:
```bash
aws s3 ls s3://llamapress-cdn/llamabot-chat-js-v0.2.19/
```

### ActionCable Connection Fails

**Problem:** WebSocket errors in console

**Checklist:**
- ✅ Is `llama_bot_rails` gem loaded?
- ✅ Is `window.LlamaBotRails.cable` available?
- ✅ Is LlamaBot backend container running?
- ✅ Is ActionCable channel subscribed?

## Deployment (For LlamaBot Maintainers)

See [S3_CDN_DEPLOYMENT.md](S3_CDN_DEPLOYMENT.md) for:
- Deploying updates to S3
- Version bumping
- AWS credentials setup
- CORS configuration

## Related Documentation

- **[.claude/skills/llamabot-s3-cdn.md]** - Complete architecture guide (for AI agents)
- **[S3_CDN_DEPLOYMENT.md]** - Deployment procedures
- **[app/frontend/chat/STYLING.md]** - Complete styling guide and attribute reference
- **[app/frontend/chat/EXAMPLE_USAGE.js]** - Working code examples
- **[app/frontend/chat/README.md]** - Module architecture

## Real-World Example: Leonardo

Leonardo is a Rails project that uses LlamaBot via S3 CDN. See:
- `/Users/kodykendall/SoftEngineering/LLMPress/Leonardo/rails/app/views/public/chat.html.erb`

Key features:
- Imports from S3: `llamabot-chat-js-v0.2.19`
- ActionCable integration with `waitForCableConnection`
- Tailwind styling via `cssClasses` config
- Suggested prompts for quick actions
- Connection status indicator

## Credits

Built with Claude Code, deployed November 2025.
Implements the data-llamabot paradigm for framework-agnostic reusability.
