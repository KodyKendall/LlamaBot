# Styling Guide - data-llamabot Attributes

This guide documents all available `data-llamabot` attributes used for styling the chat application. These attributes provide a framework-agnostic way to style components whether you're using vanilla CSS, Tailwind, Bootstrap, or any other CSS framework.

## Why Data Attributes?

The chat application uses `data-llamabot` attributes instead of classes for component identification. This approach:

- **Works with any CSS framework** - Style using vanilla CSS, Tailwind, Bootstrap, DaisyUI, etc.
- **Zero JavaScript configuration** - No className mapping needed
- **Industry standard** - Same pattern used by Radix UI, Headless UI, Alpine.js
- **Clean separation** - Data attributes = structure, CSS/classes = styling
- **DevTools friendly** - Easy to inspect and debug

## Available Attributes

### Message Types

#### `[data-llamabot="human-message"]`
User messages in the chat interface.

**Default styling**: Right-aligned bubble with user background color

```css
/* Vanilla CSS */
[data-llamabot="human-message"] {
  background-color: #4c51bf;
  color: white;
  padding: 0.75rem 1rem;
  border-radius: 12px;
  align-self: flex-end;
}

/* Tailwind */
<div data-llamabot="human-message" class="bg-blue-600 text-white px-4 py-3 rounded-xl self-end">
```

#### `[data-llamabot="ai-message"]`
AI assistant responses.

**Default styling**: Left-aligned bubble with AI background color

```css
/* Vanilla CSS */
[data-llamabot="ai-message"] {
  background-color: #2d3748;
  color: #e2e8f0;
  padding: 0.75rem 1rem;
  border-radius: 12px;
  align-self: flex-start;
}

/* Tailwind */
<div data-llamabot="ai-message" class="bg-gray-800 text-gray-200 px-4 py-3 rounded-xl self-start">
```

**Markdown Elements**: AI messages support rich markdown rendering. Style nested elements:

```css
/* Headings */
[data-llamabot="ai-message"] h1 { font-size: 1.3em; }
[data-llamabot="ai-message"] h2 { font-size: 1.2em; }
[data-llamabot="ai-message"] h3 { font-size: 1.15em; }

/* Paragraphs */
[data-llamabot="ai-message"] p {
  margin: 0.5em 0;
  line-height: 1.5;
}

/* Lists */
[data-llamabot="ai-message"] ul,
[data-llamabot="ai-message"] ol {
  margin: 0.5em 0;
  padding-left: 1.5em;
}

/* Code blocks */
[data-llamabot="ai-message"] code {
  background-color: rgba(255, 255, 255, 0.1);
  padding: 0.1em 0.3em;
  border-radius: 3px;
  font-family: monospace;
}

[data-llamabot="ai-message"] pre {
  background-color: rgba(255, 255, 255, 0.05);
  padding: 1em;
  border-radius: 6px;
  overflow-x: auto;
}

/* Links */
[data-llamabot="ai-message"] a {
  color: #4ade80;
  text-decoration: none;
}

[data-llamabot="ai-message"] a:hover {
  text-decoration: underline;
}

/* Tables */
[data-llamabot="ai-message"] table {
  border-collapse: collapse;
  width: 100%;
}

[data-llamabot="ai-message"] th,
[data-llamabot="ai-message"] td {
  border: 1px solid #374151;
  padding: 0.5em;
}
```

#### `[data-llamabot="tool-message"]`
Tool execution messages (file edits, searches, etc).

**Default styling**: Compact inline indicator

```css
/* Vanilla CSS */
[data-llamabot="tool-message"] {
  margin: 0.35rem 0;
  font-size: 0.85em;
  align-self: flex-start;
}

/* Tailwind */
<div data-llamabot="tool-message" class="my-1.5 text-sm self-start">
```

#### `[data-llamabot="error-message"]`
Error messages.

**Default styling**: Red-tinted message bubble

```css
/* Vanilla CSS */
[data-llamabot="error-message"] {
  background-color: rgba(239, 68, 68, 0.1);
  border: 1px solid rgba(239, 68, 68, 0.3);
  color: #fca5a5;
  padding: 0.75rem 1rem;
  border-radius: 8px;
}

/* Tailwind */
<div data-llamabot="error-message" class="bg-red-900/10 border border-red-500/30 text-red-300 px-4 py-3 rounded-lg">
```

#### `[data-llamabot="queued-message"]`
Messages waiting to be sent.

**Default styling**: Translucent version of human message

```css
/* Vanilla CSS */
[data-llamabot="queued-message"] {
  opacity: 0.5;
  background-color: #4c51bf;
  align-self: flex-end;
}

/* Tailwind */
<div data-llamabot="queued-message" class="opacity-50 bg-blue-600 self-end">
```

### Tool Components

#### `[data-llamabot="tool-compact"]`
Compact tool execution indicator with icon, name, and target.

**Default styling**: Inline badge with icon

```css
/* Vanilla CSS */
[data-llamabot="tool-compact"] {
  display: inline-flex;
  align-items: center;
  gap: 6px;
  padding: 4px 8px;
  border-radius: 6px;
  background: rgba(139, 92, 246, 0.08);
  border: 1px solid rgba(139, 92, 246, 0.15);
  font-size: 0.8em;
}

/* Tailwind */
<div data-llamabot="tool-compact" class="inline-flex items-center gap-1.5 px-2 py-1 rounded-md bg-purple-500/10 border border-purple-500/20 text-xs">
```

**Status States**: Tools can have success/error status via `data-status` attribute:

```css
/* Success state */
[data-llamabot="tool-compact"][data-status="success"] {
  background: rgba(52, 211, 153, 0.08);
  border-color: rgba(52, 211, 153, 0.2);
}

/* Error state */
[data-llamabot="tool-compact"][data-status="error"] {
  background: rgba(239, 68, 68, 0.08);
  border-color: rgba(239, 68, 68, 0.2);
}

/* Tailwind */
<div data-llamabot="tool-compact" data-status="success" class="bg-green-500/10 border-green-500/20">
<div data-llamabot="tool-compact" data-status="error" class="bg-red-500/10 border-red-500/20">
```

#### `[data-llamabot="tool-compact-name"]`
The tool name within a compact tool indicator.

```css
/* Vanilla CSS */
[data-llamabot="tool-compact-name"] {
  font-weight: 500;
  color: rgba(139, 92, 246, 0.9);
}

/* Tailwind */
<span data-llamabot="tool-compact-name" class="font-medium text-purple-400">
```

#### `[data-llamabot="tool-compact-target"]`
The target file/command within a compact tool indicator.

```css
/* Vanilla CSS */
[data-llamabot="tool-compact-target"] {
  color: rgba(255, 255, 255, 0.6);
  font-family: monospace;
  font-size: 0.95em;
}

/* Tailwind */
<span data-llamabot="tool-compact-target" class="text-gray-400 font-mono text-xs">
```

#### `[data-llamabot="tool-icon"]`
SVG icons for tool indicators.

**Default styling**: Small icon with purple tint

```css
/* Vanilla CSS */
[data-llamabot="tool-icon"] {
  width: 12px;
  height: 12px;
  color: rgba(139, 92, 246, 0.8);
}

/* Tailwind */
<svg data-llamabot="tool-icon" class="w-3 h-3 text-purple-400">
```

**Spinning State**: For pending operations:

```css
/* Vanilla CSS */
[data-llamabot="tool-icon"][data-state="spinning"] {
  animation: spin 1s linear infinite;
}

@keyframes spin {
  from { transform: rotate(0deg); }
  to { transform: rotate(360deg); }
}

/* Tailwind */
<svg data-llamabot="tool-icon" data-state="spinning" class="w-3 h-3 animate-spin">
```

### UI Components

#### `[data-llamabot="message-input"]`
The main textarea for user input.

```css
/* Vanilla CSS */
[data-llamabot="message-input"] {
  width: 100%;
  padding: 0.75rem;
  border-radius: 8px;
  border: 1px solid #374151;
  background-color: #1f2937;
  color: white;
  resize: vertical;
}

/* Tailwind */
<textarea data-llamabot="message-input" class="w-full px-3 py-2 rounded-lg border border-gray-700 bg-gray-800 text-white resize-y">
```

#### `[data-llamabot="send-button"]`
The send message button.

```css
/* Vanilla CSS */
[data-llamabot="send-button"] {
  padding: 0.75rem 1.5rem;
  background: linear-gradient(135deg, #667eea 0%, #764ba2 100%);
  color: white;
  border: none;
  border-radius: 8px;
  cursor: pointer;
  font-weight: 600;
}

/* Tailwind */
<button data-llamabot="send-button" class="px-6 py-3 bg-gradient-to-br from-purple-500 to-purple-700 text-white rounded-lg font-semibold">
```

#### `[data-llamabot="hamburger-menu"]`
The hamburger menu button.

```css
/* Vanilla CSS */
[data-llamabot="hamburger-menu"] {
  width: 40px;
  height: 40px;
  background: rgba(255, 255, 255, 0.05);
  border-radius: 8px;
  cursor: pointer;
  display: flex;
  align-items: center;
  justify-content: center;
}

/* Tailwind */
<button data-llamabot="hamburger-menu" class="w-10 h-10 bg-white/5 rounded-lg cursor-pointer flex items-center justify-center">
```

#### `[data-llamabot="menu-drawer"]`
The slide-out navigation drawer.

```css
/* Vanilla CSS */
[data-llamabot="menu-drawer"] {
  position: fixed;
  top: 0;
  left: -300px;
  width: 300px;
  height: 100vh;
  background-color: #1f2937;
  transition: left 0.3s ease;
  z-index: 1000;
}

[data-llamabot="menu-drawer"].open {
  left: 0;
}

/* Tailwind */
<div data-llamabot="menu-drawer" class="fixed top-0 -left-[300px] w-[300px] h-screen bg-gray-800 transition-all duration-300 z-[1000] data-[open]:left-0">
```

#### `[data-llamabot="show-preview-btn"]`
Mobile button to switch to preview/iframe view.

```css
/* Vanilla CSS */
[data-llamabot="show-preview-btn"] {
  display: none;
  padding: 0.5rem 1rem;
  background-color: #4c51bf;
  color: white;
  border-radius: 6px;
}

@media (max-width: 768px) {
  [data-llamabot="show-preview-btn"] {
    display: block;
  }
}

/* Tailwind */
<button data-llamabot="show-preview-btn" class="hidden md:block px-4 py-2 bg-blue-600 text-white rounded-md">
```

#### `[data-llamabot="show-chat-btn"]`
Mobile button to switch back to chat view.

```css
/* Similar to show-preview-btn but appears in opposite context */
[data-llamabot="show-chat-btn"] {
  display: none;
}

@media (max-width: 768px) {
  body.mobile-iframe-view [data-llamabot="show-chat-btn"] {
    display: block;
  }
}
```

## Framework Examples

### Vanilla CSS

The default [style.css](../../style.css) provides a complete vanilla CSS implementation. Simply include it:

```html
<link rel="stylesheet" href="style.css">
```

### Tailwind CSS

Replace the default styles with Tailwind classes:

```html
<!-- Message bubbles -->
<div data-llamabot="human-message" class="bg-blue-600 text-white px-4 py-3 rounded-xl self-end max-w-[80%] shadow-md">
  Your message
</div>

<div data-llamabot="ai-message" class="bg-gray-800 text-gray-100 px-4 py-3 rounded-xl self-start max-w-[80%] shadow-md">
  AI response
</div>

<!-- Tool compact indicator -->
<div data-llamabot="tool-compact" class="inline-flex items-center gap-2 px-2 py-1 rounded-md bg-purple-500/10 border border-purple-500/20 text-xs">
  <svg data-llamabot="tool-icon" class="w-3 h-3 text-purple-400">...</svg>
  <span data-llamabot="tool-compact-name" class="font-medium text-purple-400">Edit</span>
  <span data-llamabot="tool-compact-target" class="text-gray-400 font-mono">file.js</span>
</div>

<!-- Success state -->
<div data-llamabot="tool-compact" data-status="success" class="inline-flex items-center gap-2 px-2 py-1 rounded-md bg-green-500/10 border border-green-500/20 text-xs">
  ...
</div>
```

### DaisyUI (Tailwind Components)

Use DaisyUI component classes with data attributes:

```html
<!-- Messages as DaisyUI chat bubbles -->
<div data-llamabot="human-message" class="chat chat-end">
  <div class="chat-bubble chat-bubble-primary">
    Your message
  </div>
</div>

<div data-llamabot="ai-message" class="chat chat-start">
  <div class="chat-bubble chat-bubble-secondary">
    AI response
  </div>
</div>

<!-- Tool as DaisyUI badge -->
<div data-llamabot="tool-compact" class="badge badge-ghost gap-2">
  <svg data-llamabot="tool-icon" class="w-3 h-3">...</svg>
  <span data-llamabot="tool-compact-name">Edit</span>
  <span data-llamabot="tool-compact-target" class="font-mono text-xs">file.js</span>
</div>

<!-- Success badge -->
<div data-llamabot="tool-compact" data-status="success" class="badge badge-success gap-2">
  ...
</div>
```

### Bootstrap

Use Bootstrap classes with data attributes:

```html
<!-- Messages -->
<div data-llamabot="human-message" class="alert alert-primary text-end ms-auto" style="max-width: 80%;">
  Your message
</div>

<div data-llamabot="ai-message" class="alert alert-secondary" style="max-width: 80%;">
  AI response
</div>

<!-- Tool as Bootstrap badge -->
<span data-llamabot="tool-compact" class="badge bg-primary d-inline-flex align-items-center gap-2">
  <svg data-llamabot="tool-icon" width="12" height="12">...</svg>
  <span data-llamabot="tool-compact-name">Edit</span>
  <span data-llamabot="tool-compact-target" class="font-monospace small">file.js</span>
</span>

<!-- Success badge -->
<span data-llamabot="tool-compact" data-status="success" class="badge bg-success d-inline-flex align-items-center gap-2">
  ...
</span>
```

## Custom Styling Tips

### 1. Override Default Styles

Create your own CSS file that loads after the default:

```html
<link rel="stylesheet" href="style.css">
<link rel="stylesheet" href="my-custom-styles.css">
```

```css
/* my-custom-styles.css */
[data-llamabot="human-message"] {
  background: linear-gradient(135deg, #667eea 0%, #764ba2 100%);
  border-radius: 20px;
  box-shadow: 0 4px 6px rgba(0, 0, 0, 0.1);
}

[data-llamabot="ai-message"] {
  background: #ffffff;
  color: #1a202c;
  border: 1px solid #e2e8f0;
}
```

### 2. Use CSS Custom Properties

Leverage CSS variables for theme customization:

```css
:root {
  --message-user-bg: #4c51bf;
  --message-ai-bg: #2d3748;
  --tool-icon-color: #8b5cf6;
}

[data-llamabot="human-message"] {
  background-color: var(--message-user-bg);
}

[data-llamabot="ai-message"] {
  background-color: var(--message-ai-bg);
}

[data-llamabot="tool-icon"] {
  color: var(--tool-icon-color);
}
```

### 3. Dark/Light Mode

Toggle themes using data attributes on body:

```css
/* Dark mode (default) */
body[data-theme="dark"] [data-llamabot="ai-message"] {
  background-color: #2d3748;
  color: #e2e8f0;
}

/* Light mode */
body[data-theme="light"] [data-llamabot="ai-message"] {
  background-color: #f7fafc;
  color: #1a202c;
  border: 1px solid #e2e8f0;
}
```

### 4. Responsive Design

Adjust styles for mobile:

```css
/* Desktop */
[data-llamabot="human-message"],
[data-llamabot="ai-message"] {
  max-width: 70%;
}

/* Mobile */
@media (max-width: 768px) {
  [data-llamabot="human-message"],
  [data-llamabot="ai-message"] {
    max-width: 85%;
    font-size: 0.95rem;
  }
}
```

## Complete Attribute Reference

| Attribute | Purpose | Common Parent |
|-----------|---------|---------------|
| `data-llamabot="human-message"` | User message bubble | Message container |
| `data-llamabot="ai-message"` | AI message bubble | Message container |
| `data-llamabot="tool-message"` | Tool execution message | Message container |
| `data-llamabot="error-message"` | Error message bubble | Message container |
| `data-llamabot="queued-message"` | Pending message | Message container |
| `data-llamabot="tool-compact"` | Compact tool indicator | Tool message |
| `data-llamabot="tool-compact-name"` | Tool name label | Tool compact |
| `data-llamabot="tool-compact-target"` | Tool target label | Tool compact |
| `data-llamabot="tool-icon"` | SVG icon | Tool compact |
| `data-llamabot="message-input"` | User input textarea | Input area |
| `data-llamabot="send-button"` | Send button | Input area |
| `data-llamabot="hamburger-menu"` | Menu toggle button | Header |
| `data-llamabot="menu-drawer"` | Navigation drawer | Body |
| `data-llamabot="show-preview-btn"` | Switch to preview (mobile) | Header |
| `data-llamabot="show-chat-btn"` | Switch to chat (mobile) | Header |

## Additional State Attributes

Beyond `data-llamabot`, components may have additional state attributes:

- `data-status="success"` - Successful operation (tools)
- `data-status="error"` - Failed operation (tools)
- `data-state="spinning"` - Animated spinner state (icons)
- `data-open="true"` - Open state (drawers, menus)

## Best Practices

1. **Don't remove data-llamabot attributes** - They're required for JavaScript functionality
2. **Add your own classes freely** - Combine data attributes with framework classes
3. **Use attribute selectors** - Style with `[data-llamabot="..."]` in your CSS
4. **Respect state attributes** - Tools and icons use `data-status` and `data-state` for dynamic styling
5. **Test responsive behavior** - Ensure your custom styles work on mobile
6. **Consider accessibility** - Maintain sufficient contrast ratios and focus states

## Questions?

For more details on the application architecture, see [README.md](README.md).

For implementation details, see the source code in [messages/](messages/), [ui/](ui/), and [utils/](utils/).
