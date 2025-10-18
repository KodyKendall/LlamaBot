# Chat Application - Modular Architecture

This directory contains the refactored, modular chat application. The original monolithic `chat.js` file (1,158 lines) has been broken down into focused, maintainable modules.

## Directory Structure

```
chat/
├── index.js                    # Main entry point - initializes and coordinates all modules
├── config.js                   # Configuration constants and helper functions
├── README.md                   # This file
│
├── state/                      # State management
│   ├── AppState.js            # Application state (WebSocket, thread, messages)
│   └── StreamingState.js      # HTML streaming state management
│
├── websocket/                  # WebSocket communication
│   ├── WebSocketManager.js    # Connection, reconnection, send/receive
│   └── MessageHandler.js      # Message routing and processing logic
│
├── messages/                   # Message rendering
│   ├── MessageRenderer.js     # Main message display logic
│   ├── MarkdownParser.js      # Markdown to HTML parsing
│   └── ToolMessageRenderer.js # Tool call UI (collapsible, todos)
│
├── ui/                         # UI components
│   ├── ScrollManager.js       # Auto-scroll behavior
│   ├── IframeManager.js       # Iframe refresh, streaming overlay
│   ├── MenuManager.js         # Hamburger menu and drawer
│   └── MobileViewManager.js   # Mobile/desktop responsive behavior
│
├── threads/                    # Thread/conversation management
│   └── ThreadManager.js       # Fetch, load, switch threads
│
└── utils/                      # Utilities
    ├── cookies.js             # Cookie helper functions
    └── domHelpers.js          # DOM utility functions
```

## Key Modules

### Entry Point

**[index.js](index.js)** - Main application class that:
- Initializes all components
- Coordinates dependency injection
- Sets up event listeners
- Exposes global interfaces for onclick handlers

### State Management

**[state/AppState.js](state/AppState.js)**
- Manages WebSocket connection reference
- Tracks current thread ID
- Handles AI message buffering
- Manages agent configuration

**[state/StreamingState.js](state/StreamingState.js)**
- Handles HTML streaming buffers
- Tracks streaming lifecycle (start/end)
- Manages iframe flush timing

### WebSocket Communication

**[websocket/WebSocketManager.js](websocket/WebSocketManager.js)**
- Creates and manages WebSocket connection
- Auto-reconnection on disconnect
- Updates connection status UI
- Emits custom events for connection state

**[websocket/MessageHandler.js](websocket/MessageHandler.js)**
- Routes incoming messages by type
- Handles AI message chunks (text and tool calls)
- Coordinates HTML streaming with IframeManager
- Updates message display via MessageRenderer

### Message Rendering

**[messages/MessageRenderer.js](messages/MessageRenderer.js)**
- Renders messages (human, AI, tool, error)
- Manages message history DOM
- Delegates to MarkdownParser and ToolMessageRenderer

**[messages/MarkdownParser.js](messages/MarkdownParser.js)**
- Parses markdown to HTML using marked.js
- Sanitizes HTML to prevent XSS
- Handles different LLM formats (Claude vs OpenAI)

**[messages/ToolMessageRenderer.js](messages/ToolMessageRenderer.js)**
- Creates collapsible tool call UI
- Special rendering for todo lists and file edits
- Updates tool results dynamically

### UI Management

**[ui/ScrollManager.js](ui/ScrollManager.js)**
- Tracks user scroll position
- Auto-scrolls when user is at bottom
- Shows/hides scroll-to-bottom button

**[ui/IframeManager.js](ui/IframeManager.js)**
- Flushes HTML to iframe during streaming
- Creates/removes streaming overlay with animation
- Manages iframe refresh logic
- Handles navigation buttons and tabs

**[ui/MenuManager.js](ui/MenuManager.js)**
- Manages hamburger menu and drawer
- Handles menu open/close/toggle
- Click-outside-to-close behavior

**[ui/MobileViewManager.js](ui/MobileViewManager.js)**
- Switches between chat and iframe views on mobile
- Handles responsive behavior on resize
- Auto-resizes message input textarea

### Thread Management

**[threads/ThreadManager.js](threads/ThreadManager.js)**
- Fetches threads from server
- Populates menu with conversation list
- Loads thread messages
- Generates conversation titles

### Utilities

**[utils/cookies.js](utils/cookies.js)**
- `setCookie(name, value, days)` - Set cookie with expiration
- `getCookie(name)` - Get cookie value

**[utils/domHelpers.js](utils/domHelpers.js)**
- `escapeHtml(text)` - Prevent XSS
- `generateUniqueId(prefix)` - Generate unique element IDs
- `cleanEscapedHtml(html)` - Clean escaped characters
- `createElement(tag, attributes, children)` - Create DOM elements

## Benefits of This Architecture

### 1. **Separation of Concerns**
Each module has a single, clear responsibility. Want to change scroll behavior? Look in `ScrollManager.js`.

### 2. **Testability**
Modules can be tested in isolation. Mock WebSocketManager when testing MessageRenderer.

### 3. **Maintainability**
- Easy to find specific functionality
- Clear dependencies between modules
- Reduced cognitive load when reading code

### 4. **Reusability**
Modules like MessageRenderer or MarkdownParser could be used in other projects.

### 5. **Team Development**
Multiple developers can work on different modules without conflicts.

### 6. **Type Safety Ready**
This structure makes it easy to add TypeScript type definitions later.

### 7. **Event-Driven**
Custom events allow loose coupling between modules:
- `websocketConnected` / `websocketDisconnected`
- `streamEnded`
- `iframeRefreshRequested`
- `threadChanged`

## Migration Notes

The original [../chat.js](../chat.js) file has been preserved but is no longer in use. The HTML file now references `chat/index.js` as an ES6 module.

### Key Changes:

1. **ES6 Modules**: Uses `import`/`export` instead of global scope
2. **Classes**: State and behavior encapsulated in classes
3. **Dependency Injection**: Components receive dependencies via constructor
4. **Event System**: Custom events for cross-module communication
5. **Global Functions**: Still exposed for onclick handlers (e.g., `window.toggleToolCollapsible`)

## Development Workflow

### Adding New Features

1. Identify which module(s) need changes
2. Update the relevant module(s)
3. If adding new state, update `AppState` or `StreamingState`
4. If adding new UI, create a new manager in `ui/`
5. Wire up dependencies in `index.js`

### Example: Adding a New Message Type

1. Add rendering logic to `MessageRenderer.js`
2. Add handling logic to `MessageHandler.js`
3. Test independently before integration

## Future Improvements

- Add TypeScript for type safety
- Add unit tests for each module
- Extract more configuration to `config.js`
- Add JSDoc comments for better IDE support
- Consider using a state management library (Redux, Zustand)
- Add build step for minification/bundling
