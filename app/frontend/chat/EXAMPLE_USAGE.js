/**
 * LlamaBot Client Library - Usage Examples
 *
 * This file demonstrates how to use the LlamaBot client library
 * in different scenarios.
 */

// =================================================================
// EXAMPLE 1: Basic Usage (Minimal Configuration)
// =================================================================

// Auto-initialization happens if you have data-llamabot attributes in your HTML
// The library will automatically create an instance for backward compatibility

// =================================================================
// EXAMPLE 2: Explicit Initialization with Custom Config
// =================================================================

const chatbot = LlamaBot.create('#my-chat-container', {
  agent: {
    name: 'customer_support_agent',
    type: 'default'
  },

  websocketUrl: 'wss://myapp.com/ws',

  // Custom callbacks
  onMessageReceived: (data) => {
    console.log('Message sent:', data.message);
    // Send analytics event
    analytics.track('message_sent', {
      thread_id: data.threadId,
      agent_mode: data.agentMode
    });
  },

  onToolResult: (result) => {
    console.log('Tool completed:', result.name);

    // Custom handling for specific tools
    if (result.name === 'create_book') {
      showToast('Book created successfully!');
      refreshBookList();
    }
  },

  onError: (error) => {
    console.error('Chat error:', error);
    showErrorNotification('Something went wrong. Please try again.');
  }
});

// =================================================================
// EXAMPLE 3: Multiple Chat Instances on Same Page
// =================================================================

// Customer Support Chat
const supportChat = LlamaBot.create('#support-chat', {
  agent: { name: 'support_agent' },
  agentModes: {
    tier1: 'support_tier1_agent',
    tier2: 'support_tier2_agent',
    escalation: 'support_escalation_agent'
  }
});

// Internal Admin Chat
const adminChat = LlamaBot.create('#admin-chat', {
  agent: { name: 'admin_agent' },
  agentModes: {
    database: 'admin_db_agent',
    logs: 'admin_logs_agent',
    monitoring: 'admin_monitoring_agent'
  }
});

// Both instances work independently with no conflicts!

// =================================================================
// EXAMPLE 4: Custom Tool Renderers
// =================================================================

const customBot = LlamaBot.create('#chat', {
  agent: { name: 'book_agent' },

  // Custom renderers for tool outputs
  toolRenderers: {
    'list_books': (output, args) => {
      // Custom rendering for book list
      return `
        <div class="book-grid">
          <h3>Found ${output.length} books</h3>
          ${output.map(book => `
            <div class="book-card">
              <img src="${book.cover_url}" alt="${book.title}">
              <h4>${book.title}</h4>
              <p class="author">${book.author}</p>
              <p class="price">$${book.price}</p>
              <button onclick="addToCart('${book.id}')">Add to Cart</button>
            </div>
          `).join('')}
        </div>
      `;
    },

    'search_books': (output, args) => {
      return `
        <div class="search-results">
          <p>Search results for "${args.query}":</p>
          <ul>
            ${output.map(book => `
              <li><strong>${book.title}</strong> by ${book.author}</li>
            `).join('')}
          </ul>
        </div>
      `;
    }
  },

  // Custom callbacks
  onToolResult: (result) => {
    if (result.name === 'create_order') {
      // Show success message
      showOrderConfirmation(result.output.order_id);

      // Update cart UI
      updateCartCount();

      // Send to analytics
      analytics.track('order_created', {
        order_id: result.output.order_id,
        total: result.output.total
      });
    }
  }
});

// =================================================================
// EXAMPLE 5: Configuration Options Reference
// =================================================================

const fullyConfiguredBot = LlamaBot.create('#chat', {
  // WebSocket configuration
  websocketUrl: 'wss://custom-backend.com/ws', // Default: auto-detect

  // Agent configuration
  agent: {
    name: 'my_custom_agent',
    type: 'default'
  },

  // Agent modes for dropdown
  agentModes: {
    prototype: 'prototype_agent',
    engineer: 'engineer_agent',
    testing: 'testing_agent'
  },

  // UI configuration
  iframeRefreshMs: 500,
  scrollThreshold: 50,
  railsDebugTimeout: 250,

  // Cookie settings
  cookieExpiryDays: 365,

  // Markdown options
  markdownOptions: {
    breaks: true,
    gfm: true,
    sanitize: false,
    smartLists: true,
    smartypants: true
  },

  // WebSocket reconnection
  reconnectDelay: 3000,

  // Custom renderers
  toolRenderers: {
    // tool_name: (output, args) => { return html; }
  },

  messageRenderers: {
    // message_type: (message) => { return html; }
  },

  // Event callbacks
  onMessageReceived: (data) => {
    // Called when user sends a message
  },

  onToolResult: (result) => {
    // Called when a tool execution completes
  },

  onError: (error) => {
    // Called when an error occurs
  }
});

// =================================================================
// EXAMPLE 6: Accessing Instance Properties
// =================================================================

const myBot = LlamaBot.create('#chat', {
  agent: { name: 'my_agent' }
});

// Access configuration
console.log(myBot.config.agent.name); // 'my_agent'
console.log(myBot.instanceId); // 'llamabot-1234567890-abc123'

// Access managers
myBot.scrollManager.scrollToBottom();
myBot.iframeManager.refreshRailsApp();

// Send programmatic message
myBot.sendMessage({ message: 'Hello from code!' });

// =================================================================
// EXAMPLE 7: Default Configuration
// =================================================================

// View the default configuration
console.log(LlamaBot.defaultConfig);

// Returns:
// {
//   websocketUrl: null,
//   agent: { name: 'rails_frontend_starter_agent', type: 'default' },
//   agentModes: { ... },
//   iframeRefreshMs: 500,
//   scrollThreshold: 50,
//   // ... etc
// }

// =================================================================
// EXAMPLE 8: HTML Structure Required
// =================================================================

/*
Your HTML must include elements with data-llamabot attributes:

<div id="my-chat-container">
  <div class="chat-section">
    <div class="chat-header">
      <button data-llamabot="hamburger-menu">Menu</button>
      <div data-llamabot="connection-status"></div>
    </div>

    <div data-llamabot="menu-drawer">
      <!-- Thread list will be populated here -->
    </div>

    <div data-llamabot="message-history">
      <!-- Messages will appear here -->
    </div>

    <div class="input-area">
      <button data-llamabot="scroll-to-bottom">↓</button>
      <div data-llamabot="thinking-area"></div>
      <textarea data-llamabot="message-input"></textarea>
      <button data-llamabot="element-selector-btn">Select</button>
      <select data-llamabot="agent-mode-select">
        <option value="prototype">Design Mode</option>
        <option value="engineer">Engineer Mode</option>
      </select>
      <button data-llamabot="send-button">Send</button>
    </div>
  </div>

  <div class="iframe-section">
    <iframe data-llamabot="live-site-frame"></iframe>
    <iframe data-llamabot="vscode-frame"></iframe>
  </div>
</div>
*/

// =================================================================
// EXAMPLE 9: Styling & Customization
// =================================================================

/*
The library uses existing CSS classes (no forced framework).
You can style it however you want by overriding CSS:

.chat-section { ... }
.message-history { ... }
.message { ... }
.ai-message { ... }
.human-message { ... }
.tool-compact { ... }

The library is CSS framework agnostic - use Tailwind, Bootstrap,
or vanilla CSS - whatever you prefer!
*/

// =================================================================
// EXAMPLE 10: Real-World Application - E-Commerce Chat
// =================================================================

const ecommerceChat = LlamaBot.create('#store-chat', {
  agent: { name: 'store_assistant_agent' },

  toolRenderers: {
    'list_products': (products, args) => {
      return `
        <div class="product-grid">
          ${products.map(p => `
            <div class="product-card" data-product-id="${p.id}">
              <img src="${p.image}" alt="${p.name}">
              <h3>${p.name}</h3>
              <p class="price">$${p.price.toFixed(2)}</p>
              <button class="add-to-cart" onclick="addToCart('${p.id}')">
                Add to Cart
              </button>
            </div>
          `).join('')}
        </div>
      `;
    },

    'check_inventory': (inventory, args) => {
      const inStock = inventory.quantity > 0;
      return `
        <div class="inventory-status ${inStock ? 'in-stock' : 'out-of-stock'}">
          <strong>${inventory.product_name}</strong>
          ${inStock
            ? `✓ In stock (${inventory.quantity} available)`
            : '✗ Out of stock'}
        </div>
      `;
    },

    'create_order': (order, args) => {
      return `
        <div class="order-confirmation">
          <h3>✓ Order Confirmed!</h3>
          <p>Order #${order.order_id}</p>
          <p>Total: $${order.total.toFixed(2)}</p>
          <p>Estimated delivery: ${order.estimated_delivery}</p>
          <a href="/orders/${order.order_id}">View Order Details</a>
        </div>
      `;
    }
  },

  onToolResult: (result) => {
    // Track conversions
    if (result.name === 'create_order') {
      gtag('event', 'purchase', {
        transaction_id: result.output.order_id,
        value: result.output.total,
        currency: 'USD'
      });
    }

    // Update UI
    if (result.name === 'add_to_cart') {
      updateCartBadge();
      showToast('Added to cart!');
    }
  },

  onError: (error) => {
    console.error('Chat error:', error);
    showNotification('Oops! Something went wrong. Please try again.', 'error');
  }
});

// Helper functions for the e-commerce example
function addToCart(productId) {
  // Add to cart logic
  console.log('Adding product to cart:', productId);
}

function updateCartBadge() {
  // Update cart count in header
  const badge = document.querySelector('.cart-badge');
  if (badge) {
    const currentCount = parseInt(badge.textContent) || 0;
    badge.textContent = currentCount + 1;
  }
}

function showToast(message) {
  // Show toast notification
  console.log('Toast:', message);
}

function showNotification(message, type) {
  // Show notification
  console.log(`${type.toUpperCase()}:`, message);
}
