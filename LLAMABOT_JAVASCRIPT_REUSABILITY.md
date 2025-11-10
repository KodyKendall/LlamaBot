# LlamaBot JavaScript Reusability Guide

## Overview

LlamaBot's chat JavaScript has been refactored to support both native WebSocket connections and ActionCable connections, making it reusable across multiple projects (Leonardo, LlamaPress-Simple, etc.) while maintaining DRY principles.

## Architecture Changes

### 1. ActionCable Support Added

**New File:** `app/frontend/chat/websocket/ActionCableAdapter.js`
- Provides a WebSocket-like interface for ActionCable connections
- Translates ActionCable message format to WebSocket format
- Allows LlamaBot's JavaScript to work seamlessly with Rails ActionCable

**Modified File:** `app/frontend/chat/websocket/WebSocketManager.js`
- Now supports both native WebSocket and ActionCable connections
- Detects connection type via config and routes appropriately
- Backward compatible - existing LlamaBot chat.html continues to work unchanged

**Modified File:** `app/frontend/chat/config.js`
- Added `actionCable` configuration option
- Allows consumers to provide ActionCable consumer, channel, and session_id

### 2. Connection Type Detection

```javascript
// Native WebSocket (LlamaBot's own chat.html)
const chat = LlamaBot.create('#chat-container', {
  websocketUrl: 'ws://localhost:8080/ws'
});

// ActionCable (Leonardo, LlamaPress-Simple, etc.)
const chat = LlamaBot.create('#chat-container', {
  actionCable: {
    consumer: LlamaBotRails.cable,
    channel: 'LlamaBotRails::ChatChannel',
    session_id: 'unique-session-id'
  }
});
```

## Integration Guide for Leonardo (and other Rails projects)

### Step 1: Copy LlamaBot Chat Modules

```bash
cp -r /path/to/LlamaBot/app/frontend/chat /path/to/Leonardo/rails/app/assets/javascripts/llamabot_chat
```

### Step 2: Configure Importmap

Add to `config/importmap.rb`:

```ruby
# Pin LlamaBot chat modules
pin_all_from "app/assets/javascripts/llamabot_chat", under: "llamabot_chat"
```

### Step 3: Create HTML with data-llamabot Attributes

Follow the data-llamabot attribute paradigm (see `LlamaBot/app/frontend/chat/STYLING.md`):

```html
<div data-llamabot="chat-container">
  <div data-llamabot="message-history"></div>
  <input data-llamabot="message-input" />
  <button data-llamabot="send-button">Send</button>
</div>
```

### Step 4: Initialize with ActionCable

```html
<script type="module">
  function waitForCableConnection(callback) {
    const interval = setInterval(() => {
      if (window.LlamaBotRails && LlamaBotRails.cable) {
        clearInterval(interval);
        callback(LlamaBotRails.cable);
      }
    }, 50);
  }

  waitForCableConnection(async (consumer) => {
    const { default: LlamaBot } = await import('llamabot_chat/index.js');
    const sessionId = crypto.randomUUID();

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

    console.log('Chat initialized with LlamaBot modules');
  });
</script>
```

### Step 5: Style with Tailwind (or any CSS framework)

Since Leonardo already uses Tailwind, just apply Tailwind classes directly in the HTML:

```html
<div data-llamabot="human-message" class="flex justify-end mb-4">
  <div class="bg-indigo-100 rounded-lg py-2 px-4 max-w-[80%]">
    <!-- Message content -->
  </div>
</div>
```

## Key Benefits

### 1. DRY Principle Achieved
- ✅ Single source of truth for chat JavaScript (LlamaBot repo)
- ✅ No code duplication across Leonardo projects
- ✅ Bug fixes in LlamaBot benefit all consumers

### 2. Flexibility Maintained
- ✅ Each project can customize styling with own CSS framework
- ✅ Data-llamabot attributes separate structure from styling
- ✅ Callbacks allow project-specific behavior

### 3. Backward Compatibility
- ✅ LlamaBot's own chat.html continues to work unchanged
- ✅ Native WebSocket mode still fully functional
- ✅ No breaking changes to existing code

## Message Flow

### Native WebSocket (LlamaBot)
```
User Input → WebSocketManager → Native WebSocket → LlamaBot Backend
                                                        ↓
User Sees Message ← MessageRenderer ← MessageHandler ← WebSocket Response
```

### ActionCable (Leonardo)
```
User Input → WebSocketManager → ActionCableAdapter → ActionCable Subscription
                                                              ↓
                                                   ChatChannel (llama_bot_rails gem)
                                                              ↓
                                                     LlamaBot Backend WebSocket
                                                              ↓
User Sees Message ← MessageRenderer ← MessageHandler ← ActionCableAdapter ← ActionCable Broadcast
```

## Components That Work Across Both

All core LlamaBot modules work identically regardless of connection type:

- ✅ `MessageRenderer` - Renders messages, markdown, tool results
- ✅ `MessageHandler` - Routes messages by type
- ✅ `AppState` - Manages conversation state
- ✅ `ThreadManager` - Manages conversation threads
- ✅ `ScrollManager` - Auto-scroll behavior
- ✅ `MenuManager` - Hamburger menu and drawer

## Optional Components

These components are optional and only initialize if elements exist:

- `IframeManager` - Only used if iframe elements present (LlamaBot has them, Leonardo doesn't)
- `ElementSelector` - Only if element selector button exists
- `MobileViewManager` - Only if mobile view toggle exists

## Future: S3 CDN Distribution

Currently, projects copy the `chat/` folder locally. Future enhancement:

1. Upload LlamaBot's `chat/` folder to S3 with version tags
2. Projects load from CDN:
   ```html
   <script type="module" src="https://llamapress-cdn.s3.amazonaws.com/v0.2.19/chat/index.js"></script>
   ```
3. Benefits:
   - No need to copy files
   - Version pinning for stability
   - Automatic updates with `latest` tag
   - Universal access from any project

## Testing

### Test LlamaBot Native WebSocket
1. Start LlamaBot: `docker-compose up`
2. Visit http://localhost:8080
3. Verify chat works normally

### Test Leonardo ActionCable
1. Start Leonardo: `cd Leonardo && docker-compose up`
2. Visit http://localhost:3000/chat
3. Verify:
   - Connection status turns green
   - Messages send and receive
   - Tool results render correctly
   - Markdown parsing works
   - Thread management functions

## Troubleshooting

### ImportMap not loading modules
- Ensure `pin_all_from` is in `config/importmap.rb`
- Restart Rails server after changing importmap
- Check browser console for 404 errors

### ActionCable connection fails
- Verify `llama_bot_rails` gem is loaded
- Check that `LlamaBotRails.cable` is available
- Ensure LlamaBot container is running and accessible

### Messages not rendering
- Check browser console for JavaScript errors
- Verify data-llamabot attributes are present in HTML
- Confirm ActionCableAdapter is translating messages correctly

## Files Changed

### LlamaBot Repository
- ✅ `app/frontend/chat/websocket/ActionCableAdapter.js` - NEW
- ✅ `app/frontend/chat/websocket/WebSocketManager.js` - MODIFIED
- ✅ `app/frontend/chat/config.js` - MODIFIED

### Leonardo Repository
- ✅ `rails/app/views/public/chat.html.erb` - REWRITTEN
- ✅ `rails/app/assets/javascripts/llamabot_chat/` - COPIED
- ✅ `rails/config/importmap.rb` - MODIFIED

## Next Steps

1. ✅ Test in Leonardo development environment
2. ✅ Fix any runtime errors
3. ✅ Verify all chat features work (messages, tools, threads)
4. ⏳ Deploy to Leonardo production
5. ⏳ Apply same pattern to other Leonardo instances
6. ⏳ Set up S3 CDN distribution (optional, future enhancement)

## Credits

Built by Claude Code with user guidance on 2025-11-09.
Implements the data-llamabot paradigm documented in `app/frontend/chat/STYLING.md`.
