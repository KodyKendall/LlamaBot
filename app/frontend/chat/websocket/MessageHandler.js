/**
 * WebSocket message routing and processing
 */

const PAYWALL_UPGRADE_URL = 'https://llamapress.ai/pricing';

export class MessageHandler {
  constructor(appState, streamingState, messageRenderer, iframeManager, scrollManager, tokenIndicator, config) {
    this.appState = appState;
    this.streamingState = streamingState;
    this.messageRenderer = messageRenderer;
    this.iframeManager = iframeManager;
    this.scrollManager = scrollManager;
    this.tokenIndicator = tokenIndicator;
    this.config = config;

    // Track active plan for real-time updates
    this.activePlanId = null;
    this.planStepMapping = new Map(); // Maps step content to step DOM IDs

    // Track current thinking message for inline display
    this.currentThinkingId = null;
    this.currentThinkingBuffer = '';
    // Track if a non-thinking message was added since last thinking
    // Used to determine if we need a new thinking bubble or can append to existing
    this.hasNonThinkingMessageSinceLastThinking = false;
  }

  /**
   * Normalize streaming content from different LLM provider formats
   * Handles OpenAI (string), Anthropic/Claude, and Gemini (array of content blocks) formats
   * @param {string|Array} content - The content from AIMessageChunk
   * @returns {string} - Extracted text content
   */
  normalizeLLMStreamingContent(content) {
    if (!content) return '';

    // OpenAI format: content is a simple string
    if (typeof content === 'string') {
      return content;
    }

    // Anthropic/Claude/Gemini/GPT-5 Codex format: content is array of content blocks
    // Examples:
    //   Anthropic: [{type: "text", text: "Hello"}]
    //   Gemini: [{type: "text", text: "Hello"}, {type: "image_url", image_url: "..."}]
    //   Gemini streaming: [{type: "text_delta", text: "Hello"}]
    //   GPT-5 Codex: [{type: "text", text: "Hello"}, {type: "reasoning", text: "thinking..."}]
    if (Array.isArray(content) && content.length > 0) {
      return content
        .filter(block => {
          if (!block || typeof block !== 'object') return false;

          // Handle text blocks from all providers
          const isTextBlock = block.type === 'text' ||
                             block.type === 'text_delta' ||  // Gemini streaming
                             block.text;
          return isTextBlock;
        })
        .map(block => {
          const text = block.text || block.content || '';
          // Filter out undefined/null values
          return (text !== undefined && text !== null && text !== 'undefined') ? text : '';
        })
        .filter(text => text.length > 0)  // Remove empty strings
        .join('');
    }

    return '';
  }

  /**
   * Extract thinking/reasoning content from LLM response
   * Handles different provider formats:
   * - Claude: {type: "thinking", thinking: "..."}
   * - OpenAI: {type: "reasoning", summary: [...]} or {type: "reasoning_summary", text: "..."}
   * - Gemini: {thought: true, text: "..."}
   * @param {Array|null} thinkingBlocks - The thinking content blocks from the backend
   * @returns {string|null} - Extracted thinking text or null
   */
  extractThinkingContent(thinkingBlocks) {
    if (!thinkingBlocks || !Array.isArray(thinkingBlocks) || thinkingBlocks.length === 0) {
      return null;
    }

    return thinkingBlocks
      .map(block => {
        // Handle different provider formats
        if (block.thinking) return block.thinking;  // Claude format
        if (block.text) return block.text;          // OpenAI/Gemini format
        // OpenAI reasoning format: summary is an array of {type: "summary_text", text: "..."} objects
        if (block.summary && Array.isArray(block.summary)) {
          return block.summary
            .map(s => s.text || s.content || (typeof s === 'string' ? s : ''))
            .filter(t => t)
            .join('\n');
        }
        return '';
      })
      .filter(text => text.length > 0)
      .join('');
  }

  /**
   * Handle incoming WebSocket message
   */
  handleMessage(data) {
    // Update token indicator if token usage data is present
    if (data.token_usage && this.tokenIndicator) {
      this.tokenIndicator.update(data.token_usage);
    }

    if (data.type === 'AIMessageChunk') {
      this.handleAIMessageChunk(data);
    } else if (data.type === 'ai') {
      this.handleAIMessage(data);
    } else if (data.type === 'approval_request') {
      this.handleApprovalRequest(data);
    } else if (data.type === 'question_request') {
      this.handleQuestionRequest(data);
    } else if (data.type === 'uiux_question_request') {
      this.handleUiuxQuestionRequest(data);
    } else if (data.type === 'suggest_mode_switch') {
      this.handleSuggestModeSwitch(data);
    } else if (data.type === 'implement_ticket') {
      this.handleImplementTicket(data);
    } else {
      this.handleGenericMessage(data);
    }
  }

  /**
   * Check if current mode is beginner, engineer, or plan (hides sub-agent content)
   */
  _isSimplifiedMode() {
    const modeSelect = document.querySelector('[data-llamabot="agent-mode-select"]');
    const isBeginnerAgent = modeSelect?.value === 'beginner' || modeSelect?.value === 'engineer';
    const savedMode = document.cookie.split(';').find(c => c.trim().startsWith('executionMode='));
    const isPlanExec = savedMode?.split('=')?.[1]?.trim() === 'plan';
    return isBeginnerAgent || isPlanExec;
  }

  /**
   * Handle AI message chunks (streaming)
   */
  handleAIMessageChunk(data) {
    // Skip tool result messages that come through the messages stream
    // (ToolMessage content like "Updated todo list to [...]" should not render as AI text)
    if (data.base_message?.type === 'tool') {
      return;
    }

    // In beginner/plan mode, hide sub-agent TEXT and tool content (depth > 0)
    // but still allow thinking/reasoning to flow so activity indicators work
    const isSubagent = (data.agent_depth || 0) > 0;
    const isSimplified = this._isSimplifiedMode();
    if (isSimplified && isSubagent) {
      // Only process thinking content from sub-agents (for activity indicators)
      if (data.thinking) {
        const thinkingText = this.extractThinkingContent(data.thinking);
        if (thinkingText) {
          this.handleThinkingContent(thinkingText);
        }
      }
      return; // Skip text content and tool calls from sub-agents
    }

    // Handle thinking/reasoning content if present - render inline in message history
    if (data.thinking) {
      const thinkingText = this.extractThinkingContent(data.thinking);
      if (thinkingText) {
        this.handleThinkingContent(thinkingText);
      }
    }

    // Handle regular text content
    // Check if content has actual text (not just empty array or empty string)
    const hasActualContent = data.content && (
      (typeof data.content === 'string' && data.content.length > 0) ||
      (Array.isArray(data.content) && data.content.length > 0)
    );

    if (hasActualContent) {
      // Finalize current thinking block before starting text content
      this.finalizeCurrentThinking();
      // Regular text content streaming
      this.handleTextContent(data);
    } else if (data.base_message?.tool_call_chunks?.length > 0) {
      // Tool call arguments streaming - only finalize if there are actual tool calls
      this.finalizeCurrentThinking();
      this.handleToolCallChunk(data);
    }
  }

  /**
   * Finalize and collapse the current thinking block
   * Called when transitioning from thinking to text/tool content
   */
  finalizeCurrentThinking() {
    if (this.currentThinkingId) {
      this.messageRenderer.collapseThinkingMessage(this.currentThinkingId);
      // Mark that a non-thinking message occurred - next thinking will need new bubble
      this.hasNonThinkingMessageSinceLastThinking = true;
      // Note: We do NOT reset currentThinkingId here anymore
      // We only create a new bubble if hasNonThinkingMessageSinceLastThinking is true
    }
  }

  /**
   * Handle thinking content - render as inline message in history
   * @param {string} thinkingText - The thinking text to append
   */
  handleThinkingContent(thinkingText) {
    // Create new thinking bubble only if:
    // 1. We don't have one yet, OR
    // 2. A non-thinking message was added since the last thinking content
    if (!this.currentThinkingId || this.hasNonThinkingMessageSinceLastThinking) {
      this.currentThinkingId = `thinking-${Date.now()}`;
      this.currentThinkingBuffer = '';
      this.hasNonThinkingMessageSinceLastThinking = false;

      // Force the next streamed text chunk to start a new bubble instead of
      // appending to the bubble that came before this thinking block.
      // DeepSeek interleaves reasoning_content with content, so without this
      // post-thinking text would silently concatenate into the prior bubble.
      this.appState.setCurrentAiMessage(null);
      this.appState.currentAiMessageBuffer = '';
    }

    // Append to buffer
    this.currentThinkingBuffer += thinkingText;

    // Render/update the inline thinking message
    this.messageRenderer.renderThinkingMessage(this.currentThinkingBuffer, this.currentThinkingId);
  }

  /**
   * Handle text content streaming
   */
  handleTextContent(data) {
    let currentMessage = this.appState.getCurrentAiMessage();

    // Extract text content using universal parser (handles both OpenAI and Anthropic formats)
    const textContent = this.normalizeLLMStreamingContent(data.content);

    // Only create/update if we have actual content
    if (textContent) {
      // Create content message on first content chunk (or after tool calls)
      if (!currentMessage) {
        const messageElement = this.messageRenderer.addMessage('', 'ai', data);
        messageElement.classList.add('content-message'); // Add class to identify content messages
        this.appState.setCurrentAiMessage(messageElement);
        currentMessage = messageElement;
      }

      this.appState.appendToMessageBuffer(textContent);

      // Update message with parsed markdown
      const parser = this.messageRenderer.markdownParser;
      let fullMessage = this.appState.getMessageBuffer();
      currentMessage.innerHTML = parser.parse(fullMessage);

      // Store raw content for copy functionality
      currentMessage.setAttribute('data-raw-content', fullMessage);
    }

    // Auto-scroll if user was already at bottom (scroll listener tracks user intent)
    this.scrollManager.scrollToBottom();
  }

  /**
   * Handle tool call chunks (HTML generation)
   * This is for the STREAMING PREVIEW feature (contentFrame)
   */
  handleToolCallChunk(data) {
    if (!data.base_message?.tool_call_chunks?.[0]) {
      return;
    }

    const toolCallData = data.base_message.tool_call_chunks[0].args;
    this.streamingState.appendData(toolCallData);

    // Check for HTML start
    if (this.streamingState.checkForHtmlStart()) {
      this.handleHtmlStreamStart(data);
    }

    // Check for HTML end
    if (this.streamingState.checkForHtmlEnd()) {
      this.handleHtmlStreamEnd();
    }

    // Schedule iframe update if streaming
    if (this.streamingState.isStreaming()) {
      this.streamingState.scheduleIframeFlush(() => {
        this.iframeManager.flushToStreamingPreview(this.streamingState.getCleanedFullMessage());
      });
      this.streamingState.clearFragmentBuffer();
    }
  }

  /**
   * Handle start of HTML streaming
   * This is for the STREAMING PREVIEW feature (contentFrame)
   */
  handleHtmlStreamStart(data) {
    // Show loading state
    if (!this.appState.getCurrentAiMessage()) {
      const messageElement = this.messageRenderer.addMessage('', 'ai', data);
      this.appState.setCurrentAiMessage(messageElement);
    }

    const currentMessage = this.appState.getCurrentAiMessage();
    currentMessage.innerHTML = '🎨 Generating your page...';

    // Create overlay animation for streaming preview
    this.iframeManager.createStreamingOverlay();
  }

  /**
   * Handle end of HTML streaming
   * This is for the STREAMING PREVIEW feature (contentFrame)
   */
  handleHtmlStreamEnd() {
    // Update AI message
    const currentMessage = this.appState.getCurrentAiMessage();
    if (currentMessage) {
      currentMessage.innerHTML = '✨ Page generated successfully!';
    }

    // Clear pending flush
    this.streamingState.clearIframeFlush();

    // Final flush to streaming preview iframe
    this.iframeManager.flushToStreamingPreview(this.streamingState.getCleanedFullMessage());

    // Remove overlay
    this.iframeManager.removeStreamingOverlay();

    // Reset streaming state
    this.streamingState.reset();
  }

  /**
   * Handle complete AI message
   */
  handleAIMessage(data) {
    // Extract agent depth for sub-agent badge display
    const agentDepth = data.agent_depth || 0;
    const isSubagent = data.is_subagent || false;

    // In beginner/plan mode, hide sub-agent messages (depth > 0)
    if (this._isSimplifiedMode() && agentDepth > 0) {
      return;
    }

    // Update depth tracking in app state
    if (agentDepth !== undefined) {
      this.appState.setAgentDepth(agentDepth);
    }

    // Only process tool calls if present
    if (data.base_message?.tool_calls?.length > 0) {
      // Finalize any current thinking block before tool calls
      this.finalizeCurrentThinking();

      // "Close" the current content message by resetting the buffer and clearing current message
      // This ensures that when streaming resumes, a NEW message bubble is created
      this.appState.setCurrentAiMessage(null);
      this.appState.currentAiMessageBuffer = ''; // Reset buffer for next content chunk

      // Check if there was streamed content (Claude/Gemini) or not (OpenAI)
      const textContent = this.normalizeLLMStreamingContent(data.content);
      const hasContent = textContent && textContent.trim() !== '';

      if (!hasContent) {
        // OpenAI style: Content is empty, create the tool call message with the tool calls
        // This will render as a tool call message (not a content message)
        // Add agent depth info to base_message for tool rendering
        const baseMessageWithDepth = {
          ...data.base_message,
          agent_depth: agentDepth,
          is_subagent: isSubagent
        };
        this.messageRenderer.addMessage('', 'ai', baseMessageWithDepth);
      } else {
        // Claude/Gemini style: Content was already streamed
        // Just create the tool call placeholders for each tool call
        for (const toolCall of data.base_message.tool_calls) {
          const toolCallMessage = {
            tool_calls: [toolCall],
            agent_depth: agentDepth,
            is_subagent: isSubagent
          };
          // Empty content since this is just the tool call placeholder
          this.messageRenderer.addMessage('', 'ai', toolCallMessage);
        }
      }

      // Track plan if this is a write_todos tool call
      this.trackPlanFromToolCall(data.base_message.tool_calls);
    } else {
      // No tool calls - the message was already streamed via AIMessageChunk
      // Remove empty content message if it has no content
      const currentAiMessage = this.appState.getCurrentAiMessage();
      if (currentAiMessage && currentAiMessage.innerHTML.trim() === '') {
        currentAiMessage.remove();
      }
    }
  }

  /**
   * Track plan creation and build step mapping for real-time updates
   */
  trackPlanFromToolCall(toolCalls) {
    const writeTodosTool = toolCalls.find(tc => tc.name === 'write_todos');
    if (!writeTodosTool) return;

    try {
      const todos = JSON.parse(writeTodosTool.args)?.todos;
      if (!todos || !Array.isArray(todos)) return;

      // Find the plan element that was just created
      setTimeout(() => {
        const planElements = document.querySelectorAll('[data-plan-id]');
        const latestPlan = planElements[planElements.length - 1];

        if (latestPlan) {
          this.activePlanId = latestPlan.getAttribute('data-plan-id');

          // Build mapping of todo content to step IDs
          this.planStepMapping.clear();
          const stepElements = latestPlan.querySelectorAll('[data-step-id]');
          stepElements.forEach((stepEl, index) => {
            if (todos[index]) {
              this.planStepMapping.set(todos[index].content, stepEl.getAttribute('data-step-id'));
            }
          });
        }
      }, 100);
    } catch (error) {
      console.warn('Failed to track plan:', error);
    }
  }

  /**
   * Update plan step status in real-time
   * Called when receiving updated todo list from streaming
   */
  updatePlanSteps(newTodos) {
    if (!this.activePlanId || !this.messageRenderer.toolRenderer?.planRenderer) {
      return;
    }

    const planRenderer = this.messageRenderer.toolRenderer.planRenderer;

    // Use the new updatePlanMessage method which updates the entire plan state
    planRenderer.updatePlanMessage(this.activePlanId, newTodos);
  }

  /**
   * Handle approval request (HITL - agent wants to execute a destructive tool)
   */
  handleApprovalRequest(data) {
    this.finalizeCurrentThinking();

    const actionRequests = data.action_requests || [];
    const threadId = data.thread_id;
    const agentName = data.agent_name;

    for (const action of actionRequests) {
      const approvalId = `approval-${Date.now()}-${Math.random().toString(36).substr(2, 6)}`;
      const toolName = action.name;
      const argsStr = JSON.stringify(action.args, null, 2);
      // Truncate args display for readability
      const argsDisplay = argsStr.length > 300 ? argsStr.substring(0, 300) + '...' : argsStr;

      const html = `
        <div class="approval-card" data-approval-id="${approvalId}">
          <div class="approval-header">
            <span class="approval-icon">⚠️</span>
            <span>Leonardo wants to: <strong>${this._escapeHtml(toolName)}</strong></span>
          </div>
          <div class="approval-args"><pre>${this._escapeHtml(argsDisplay)}</pre></div>
          <div class="approval-actions">
            <button class="approval-btn approve-btn" data-action="approve">Approve</button>
            <button class="approval-btn reject-btn" data-action="reject">Reject</button>
          </div>
        </div>
      `;

      this.messageRenderer.addMessage(html, 'approval_request', null);

      // Attach event listeners to the buttons
      setTimeout(() => {
        const card = document.querySelector(`[data-approval-id="${approvalId}"]`);
        if (!card) return;
        card.querySelectorAll('.approval-btn').forEach(btn => {
          btn.addEventListener('click', () => {
            const decision = btn.dataset.action;
            // Disable buttons
            card.querySelectorAll('.approval-btn').forEach(b => b.disabled = true);
            card.classList.add(decision === 'approve' ? 'approved' : 'rejected');
            btn.classList.add('selected');

            if (decision === 'reject') {
              // Cancel the run and tell Leonardo
              window.dispatchEvent(new CustomEvent('approvalRejected', {
                detail: { thread_id: threadId, agent_name: agentName, toolName }
              }));
            } else {
              // Approve — resume the graph
              window.dispatchEvent(new CustomEvent('approvalDecision', {
                detail: {
                  decisions: [{ type: 'approve' }],
                  thread_id: threadId,
                  agent_name: agentName,
                }
              }));
            }
          });
        });
      }, 0);
    }
  }

  _escapeHtml(str) {
    const div = document.createElement('div');
    div.textContent = str;
    return div.innerHTML;
  }

  /**
   * Handle question request (plan mode — agent asks the user a question via interrupt)
   */
  handleQuestionRequest(data) {
    this.finalizeCurrentThinking();

    const { question, options, context, thread_id, agent_name } = data;
    const questionId = `question-${Date.now()}-${Math.random().toString(36).substr(2, 6)}`;

    const html = this._buildQuestionCardHtml(questionId, question, options || [], context || '', thread_id, agent_name);
    this.messageRenderer.addMessage(html, 'question_request', null);

    // Attach interactive event listeners after DOM render
    setTimeout(() => this._attachQuestionListeners(questionId, thread_id, agent_name), 0);
  }

  _buildQuestionCardHtml(questionId, question, options, context, threadId, agentName) {
    const optionButtons = options.map(opt =>
      `<button class="plan-option-btn" data-option="${this._escapeHtml(opt)}">${this._escapeHtml(opt)}</button>`
    ).join('');

    const skipBtn = `<button class="plan-skip-btn">Skip</button>`;

    return `
      <div class="plan-question-card" data-question-id="${questionId}"
           data-thread-id="${threadId}" data-agent-name="${agentName}">
        <div class="plan-question-text">${this._escapeHtml(question)}</div>
        ${context ? `<div class="plan-question-context">${this._escapeHtml(context)}</div>` : ''}
        ${options.length > 0 ? `
          <div class="plan-question-options">
            ${optionButtons}
            ${skipBtn}
          </div>
        ` : ''}
        <button class="plan-continue-btn" style="display: none;">Continue</button>
        <div class="plan-question-input-row">
          <textarea class="plan-question-input" rows="2" placeholder="Add to your answer..."></textarea>
          <button class="plan-send-btn"><i class="fa-solid fa-arrow-up"></i></button>
        </div>
      </div>
    `;
  }

  _attachQuestionListeners(questionId, threadId, agentName) {
    const card = document.querySelector(`[data-question-id="${questionId}"]`);
    if (!card) return;
    let selectedOptions = [];

    const updateContinueBtn = () => {
      const continueBtn = card.querySelector('.plan-continue-btn');
      const input = card.querySelector('.plan-question-input');
      const hasSelection = selectedOptions.length > 0;
      const hasText = input?.value?.trim()?.length > 0;
      continueBtn.style.display = (hasSelection || hasText) ? 'block' : 'none';
    };

    // Option toggle (multi-select)
    card.querySelectorAll('.plan-option-btn').forEach(btn => {
      btn.addEventListener('click', () => {
        btn.classList.toggle('selected');
        const opt = btn.dataset.option;
        if (selectedOptions.includes(opt)) {
          selectedOptions = selectedOptions.filter(o => o !== opt);
        } else {
          selectedOptions.push(opt);
        }
        updateContinueBtn();
      });
    });

    // Skip button
    card.querySelector('.plan-skip-btn')?.addEventListener('click', () => {
      this._submitQuestionAnswer(card, 'skip', threadId, agentName);
    });

    // Continue button
    card.querySelector('.plan-continue-btn')?.addEventListener('click', () => {
      const freeText = card.querySelector('.plan-question-input')?.value?.trim();
      const parts = [...selectedOptions];
      if (freeText) parts.push(freeText);
      this._submitQuestionAnswer(card, parts.join(', '), threadId, agentName);
    });

    // Input handling
    const input = card.querySelector('.plan-question-input');
    const sendBtn = card.querySelector('.plan-send-btn');
    input?.addEventListener('input', updateContinueBtn);
    sendBtn?.addEventListener('click', () => {
      const freeText = input?.value?.trim();
      const parts = [...selectedOptions];
      if (freeText) parts.push(freeText);
      if (parts.length > 0) {
        this._submitQuestionAnswer(card, parts.join(', '), threadId, agentName);
      }
    });

    // Scroll question into view
    card.scrollIntoView({ behavior: 'smooth', block: 'end' });
    setTimeout(() => card.scrollIntoView({ behavior: 'smooth', block: 'end' }), 150);
  }

  _submitQuestionAnswer(card, answer, threadId, agentName) {
    // Disable the card
    card.classList.add('answered');
    card.querySelectorAll('button, textarea').forEach(el => el.disabled = true);

    // Show user answer as a right-aligned message
    if (answer && answer !== 'skip') {
      const answerHtml = `<div class="plan-user-answer">${this._escapeHtml(answer)}</div>`;
      this.messageRenderer.addMessage(answerHtml, 'human', null);
    }

    // Send question_response via WebSocket to resume the agent
    if (window.chatApp?.webSocketManager) {
      window.chatApp.webSocketManager.send({
        type: 'question_response',
        answer: answer,
        thread_id: threadId,
        agent_name: agentName,
      });
    }

    // Show thinking indicator since agent will resume — restart the llama
    // loading verbs that renderInterruptMessage() stopped when the card appeared.
    window.chatApp?.setAgentRunning(true);
    window.chatApp?.showThinkingIndicator();
  }

  /**
   * Handle UI/UX question request (plan mode — agent asks the user to pick between
   * visual options via interrupt). Renders an inline carousel in the transcript showing
   * ONE option preview at a time (click through with ‹ ›). "Choose this" picks the
   * current option; "See all" opens the full modal to compare and add a message. The
   * answer resumes the agent over the existing question_response channel.
   */
  handleUiuxQuestionRequest(data) {
    this.finalizeCurrentThinking();

    const { question, options, context, thread_id, agent_name } = data;
    const opts = Array.isArray(options) ? options : [];
    const id = `uiux-${Date.now()}-${Math.random().toString(36).substr(2, 6)}`;

    const frames = opts.map((opt, i) =>
      `<iframe class="uiux-car-frame${i === 0 ? ' active' : ''}" data-i="${i}" sandbox="" title="Preview ${i + 1}"></iframe>`
    ).join('');

    const card = `
      <div class="uiux-carousel" data-uiux-id="${id}">
        <div class="uiux-car-question">${this._escapeHtml(question)}</div>
        <div class="uiux-car-stage">
          <button class="uiux-car-nav uiux-car-prev" title="Previous">&lsaquo;</button>
          <div class="uiux-car-frames">${frames}</div>
          <button class="uiux-car-nav uiux-car-next" title="Next">&rsaquo;</button>
        </div>
        <div class="uiux-car-foot">
          <span class="uiux-car-meta"><strong class="uiux-car-label"></strong> <span class="uiux-car-count"></span></span>
          <div class="uiux-car-actions">
            <button class="uiux-car-seeall">Expand</button>
            <button class="uiux-car-choose">Choose this</button>
          </div>
        </div>
      </div>`;
    this.messageRenderer.addMessage(card, 'uiux_question_request', null);

    setTimeout(() => this._attachUiuxCarousel({
      id, question, context: context || '', options: opts, threadId: thread_id, agentName: agent_name,
    }), 0);
  }

  /**
   * Wire the inline carousel: click-through previews + "Choose this" / "See all".
   */
  _attachUiuxCarousel({ id, question, context, options, threadId, agentName }) {
    const card = document.querySelector(`.uiux-carousel[data-uiux-id="${id}"]`);
    if (!card) return;

    const n = options.length;
    const labelFor = (i) => (options[i] && options[i].label != null) ? String(options[i].label) : String(i);
    const idFor = (i) => (options[i] && options[i].id != null) ? String(options[i].id) : String(i);

    card.querySelectorAll('.uiux-car-frame').forEach(f => {
      const opt = options[+f.dataset.i];
      f.srcdoc = this._buildUiuxPreviewDoc(opt && opt.html ? opt.html : '');
    });

    let idx = 0;
    const labelEl = card.querySelector('.uiux-car-label');
    const countEl = card.querySelector('.uiux-car-count');
    const setActive = (i) => {
      if (n === 0) return;
      idx = ((i % n) + n) % n;
      card.querySelectorAll('.uiux-car-frame').forEach(f => f.classList.toggle('active', +f.dataset.i === idx));
      if (labelEl) labelEl.textContent = labelFor(idx);
      if (countEl) countEl.textContent = `(${idx + 1} of ${n})`;
    };
    setActive(0);

    card.querySelector('.uiux-car-prev')?.addEventListener('click', () => setActive(idx - 1));
    card.querySelector('.uiux-car-next')?.addEventListener('click', () => setActive(idx + 1));

    card.querySelector('.uiux-car-choose')?.addEventListener('click', () => {
      const answer = `Selected option "${idFor(idx)}: ${labelFor(idx)}".`;
      this._answerUiuxQuestion(answer, threadId, agentName, card, labelFor(idx));
    });

    card.querySelector('.uiux-car-seeall')?.addEventListener('click', () =>
      this._openUiuxModal({ id, question, context, options, threadId, agentName, chipEl: card, initialIndex: idx }));
  }

  /**
   * Wrap a raw snippet in a self-contained document for the sandboxed preview iframe.
   * Loads the SAME Tailwind (v2) stylesheet the target app itself uses, so previews look
   * like the real page (plain — no DaisyUI component theming). Pure CSS, no scripts, so
   * the iframe can run fully locked down (sandbox="").
   */
  _buildUiuxPreviewDoc(snippet) {
    return `<!doctype html>
<html>
  <head>
    <meta charset="utf-8">
    <meta name="viewport" content="width=device-width, initial-scale=1">
    <link href="https://cdn.jsdelivr.net/npm/tailwindcss@2.2.19/dist/tailwind.min.css" rel="stylesheet">
  </head>
  <body class="p-4 bg-white text-gray-900">${snippet || ''}</body>
</html>`;
  }

  /**
   * Open the full-screen UI/UX option modal: tab bar + large sandboxed preview per
   * option, a free-text area, and a submit button that resumes the agent.
   */
  _openUiuxModal({ id, question, context, options, threadId, agentName, chipEl, initialIndex = 0 }) {
    // Only one modal at a time.
    document.querySelector('.uiux-modal')?.remove();

    const labelFor = (i) => (options[i] && options[i].label != null) ? String(options[i].label) : String(i);
    const idFor = (i) => (options[i] && options[i].id != null) ? String(options[i].id) : String(i);

    const tabs = options.map((opt, i) =>
      `<button class="uiux-tab${i === 0 ? ' active' : ''}" data-i="${i}">${this._escapeHtml(labelFor(i))}</button>`
    ).join('');
    const frames = options.map((opt, i) =>
      `<iframe class="uiux-modal-frame" data-i="${i}" sandbox="" title="Preview: ${this._escapeHtml(labelFor(i))}"></iframe>`
    ).join('');

    const overlay = document.createElement('div');
    overlay.className = 'uiux-modal';
    overlay.setAttribute('data-uiux-modal-for', id);
    overlay.innerHTML = `
      <div class="uiux-modal-content" role="dialog" aria-modal="true">
        <div class="uiux-modal-header">
          <div class="uiux-modal-titles">
            <h3 class="uiux-modal-title">${this._escapeHtml(question)}</h3>
            ${context ? `<div class="uiux-modal-context">${this._escapeHtml(context)}</div>` : ''}
          </div>
          <button class="uiux-modal-close" title="Close (Esc)">&times;</button>
        </div>
        <div class="uiux-modal-tabs">${tabs}</div>
        <div class="uiux-modal-stage">
          <button class="uiux-nav uiux-nav-prev" title="Previous (←)">&lsaquo;</button>
          <div class="uiux-modal-frames">${frames}</div>
          <button class="uiux-nav uiux-nav-next" title="Next (→)">&rsaquo;</button>
        </div>
        <div class="uiux-modal-footer">
          <div class="uiux-modal-selectrow">
            <button class="uiux-use-option" type="button">Use this option</button>
            <span class="uiux-modal-selected">No option selected — optional</span>
          </div>
          <textarea class="uiux-modal-textarea" rows="3" placeholder="Write a message or instructions for Leo…"></textarea>
          <div class="uiux-modal-actions">
            <span class="uiux-modal-hint">Pick an option, write a message, or both · ⌘/Ctrl+Enter to send</span>
            <button class="uiux-modal-send" disabled>Send response</button>
          </div>
        </div>
      </div>`;
    document.body.appendChild(overlay);

    // Populate iframe previews via srcdoc (set as a property — no attribute escaping;
    // sandbox="" fully isolates the snippet: no scripts, no same-origin access).
    overlay.querySelectorAll('.uiux-modal-frame').forEach(f => {
      const opt = options[+f.dataset.i];
      f.srcdoc = this._buildUiuxPreviewDoc(opt && opt.html ? opt.html : '');
    });

    let activeIndex = 0;
    let selectedIndex = null; // no option selected by default — selecting is optional
    const n = options.length;
    const selectedEl = overlay.querySelector('.uiux-modal-selected');
    const useBtn = overlay.querySelector('.uiux-use-option');
    const sendBtn = overlay.querySelector('.uiux-modal-send');
    const textarea = overlay.querySelector('.uiux-modal-textarea');

    const refreshSelectionUI = () => {
      const hasText = (textarea?.value?.trim()?.length || 0) > 0;
      const usingActive = selectedIndex === activeIndex;
      if (useBtn) {
        useBtn.textContent = usingActive ? '✓ Using this option' : 'Use this option';
        useBtn.classList.toggle('active', usingActive);
      }
      if (selectedEl) {
        selectedEl.textContent = selectedIndex === null
          ? 'No option selected — optional'
          : `Selected: ${labelFor(selectedIndex)}`;
      }
      // Can send if an option is selected OR a message was typed.
      if (sendBtn) sendBtn.disabled = (selectedIndex === null && !hasText);
    };

    const setActive = (i) => {
      if (n === 0) return;
      activeIndex = ((i % n) + n) % n; // wrap around
      overlay.querySelectorAll('.uiux-tab').forEach(t => t.classList.toggle('active', +t.dataset.i === activeIndex));
      overlay.querySelectorAll('.uiux-modal-frame').forEach(f => f.classList.toggle('active', +f.dataset.i === activeIndex));
      refreshSelectionUI();
    };
    setActive(initialIndex);

    overlay.querySelectorAll('.uiux-tab').forEach(t =>
      t.addEventListener('click', () => setActive(+t.dataset.i)));
    overlay.querySelector('.uiux-nav-prev')?.addEventListener('click', () => setActive(activeIndex - 1));
    overlay.querySelector('.uiux-nav-next')?.addEventListener('click', () => setActive(activeIndex + 1));

    // Explicitly select/deselect the option currently being viewed.
    useBtn?.addEventListener('click', () => {
      selectedIndex = (selectedIndex === activeIndex) ? null : activeIndex;
      refreshSelectionUI();
    });
    textarea?.addEventListener('input', refreshSelectionUI);

    const close = () => {
      document.removeEventListener('keydown', onKey);
      overlay.remove();
    };
    const submit = () => {
      const text = textarea?.value?.trim() || '';
      const hasSel = selectedIndex !== null;
      if (!hasSel && !text) return; // nothing to send
      let answer, display;
      if (hasSel && text) {
        answer = `Selected option "${idFor(selectedIndex)}: ${labelFor(selectedIndex)}". Message: ${text}`;
        display = `${labelFor(selectedIndex)} — ${text}`;
      } else if (hasSel) {
        answer = `Selected option "${idFor(selectedIndex)}: ${labelFor(selectedIndex)}".`;
        display = labelFor(selectedIndex);
      } else {
        answer = text;
        display = text;
      }
      close();
      this._answerUiuxQuestion(answer, threadId, agentName, chipEl, display);
    };

    // Keyboard: arrows switch tabs (unless typing), Esc closes, Cmd/Ctrl+Enter submits.
    const onKey = (e) => {
      const inText = e.target?.classList?.contains('uiux-modal-textarea');
      if (e.key === 'Escape') { e.preventDefault(); close(); }
      else if ((e.metaKey || e.ctrlKey) && e.key === 'Enter') { e.preventDefault(); submit(); }
      else if (!inText && e.key === 'ArrowRight') { e.preventDefault(); setActive(activeIndex + 1); }
      else if (!inText && e.key === 'ArrowLeft') { e.preventDefault(); setActive(activeIndex - 1); }
    };
    document.addEventListener('keydown', onKey);

    overlay.querySelector('.uiux-modal-close')?.addEventListener('click', close);
    overlay.querySelector('.uiux-modal-send')?.addEventListener('click', submit);
    overlay.addEventListener('click', (e) => { if (e.target === overlay) close(); });
  }

  /**
   * Resume the agent with the user's UI/UX choice + optional message, mark the chip
   * answered, and echo the choice into the transcript. Reuses the question_response
   * channel that handle_question_response already consumes on the backend.
   */
  _answerUiuxQuestion(answer, threadId, agentName, chipEl, displayChoice) {
    // Close the modal if it's open (answer may come from inline "Choose this").
    document.querySelector('.uiux-modal')?.remove();

    if (chipEl) {
      chipEl.classList.add('answered');
      chipEl.querySelectorAll('button').forEach(b => b.disabled = true);
    }

    if (displayChoice) {
      const answerHtml = `<div class="plan-user-answer">${this._escapeHtml(displayChoice)}</div>`;
      this.messageRenderer.addMessage(answerHtml, 'human', null);
    }

    if (window.chatApp?.webSocketManager) {
      window.chatApp.webSocketManager.send({
        type: 'question_response',
        answer: answer,
        thread_id: threadId,
        agent_name: agentName,
      });
    }

    // Restart the llama thinking verbs that renderInterruptMessage() stopped.
    window.chatApp?.setAgentRunning(true);
    window.chatApp?.showThinkingIndicator();
  }

  /**
   * Handle suggest_mode_switch (beginner agent suggests switching to plan mode)
   */
  handleSuggestModeSwitch(data) {
    this.finalizeCurrentThinking();

    const { reason, target_mode, thread_id, agent_name, original_message } = data;
    const switchId = `switch-${Date.now()}`;

    const html = `
      <div class="plan-question-card" data-switch-id="${switchId}">
        <div class="plan-question-text">${this._escapeHtml(reason)}</div>
        <div class="plan-question-options">
          <button class="plan-option-btn plan-switch-btn" data-action="switch">
            <i class="fa-solid fa-clipboard-list"></i> Switch to Plan mode
          </button>
          <button class="plan-skip-btn" data-action="skip">No thanks</button>
        </div>
      </div>
    `;

    this.messageRenderer.addMessage(html, 'suggest_mode_switch', null);

    setTimeout(() => {
      const card = document.querySelector(`[data-switch-id="${switchId}"]`);
      if (!card) return;

      card.querySelector('[data-action="switch"]')?.addEventListener('click', () => {
        card.classList.add('answered');
        card.querySelectorAll('button').forEach(b => b.disabled = true);

        // 1. Resume beginner agent thread (closes out the interrupted state)
        if (window.chatApp?.webSocketManager) {
          window.chatApp.webSocketManager.send({
            type: 'question_response',
            answer: 'yes, switch to plan mode',
            thread_id,
            agent_name,
          });
        }

        // 2. Switch execution mode to plan
        if (window.chatApp) {
          window.chatApp.setExecutionMode('plan');
        }

        // 3. Create new thread for the plan agent
        window.dispatchEvent(new CustomEvent('createNewThread'));

        // 4. Auto-send original message to plan agent (300ms delay for thread setup)
        if (original_message) {
          setTimeout(() => {
            const input = window.chatApp?.elements?.messageInput;
            if (input) {
              input.value = original_message;
              window.chatApp.sendMessageWithDebugInfo();
            }
          }, 300);
        }
      });

      card.querySelector('[data-action="skip"]')?.addEventListener('click', () => {
        card.classList.add('answered');
        card.querySelectorAll('button').forEach(b => b.disabled = true);
        // Resume the agent with "no"
        if (window.chatApp?.webSocketManager) {
          window.chatApp.webSocketManager.send({
            type: 'question_response',
            answer: 'no, continue in beginner mode',
            thread_id,
            agent_name,
          });
        }
      });
    }, 0);
  }

  /**
   * Handle implement_ticket (ticket agent offers to switch to engineer mode)
   */
  handleImplementTicket(data) {
    this.finalizeCurrentThinking();
    const { ticket_id, ticket_title, ticket_content, thread_id, agent_name } = data;

    // Helper: perform the actual switch to engineer mode and start building
    const doImplement = (card) => {
      if (card) {
        card.classList.add('answered');
        card.querySelectorAll('button').forEach(b => b.disabled = true);
      }

      // 1. Resume ticket agent with "yes" (it will update ticket status)
      if (window.chatApp?.webSocketManager) {
        window.chatApp.webSocketManager.send({
          type: 'question_response',
          answer: 'yes',
          thread_id,
          agent_name,
        });
      }

      // 2. Switch agent mode to engineer
      const agentSelect = window.chatApp?.elements?.agentModeSelect;
      if (agentSelect) {
        agentSelect.value = 'engineer';
        agentSelect.dispatchEvent(new Event('change'));
      }

      // 3. Create new thread
      window.dispatchEvent(new CustomEvent('createNewThread'));

      // 4. Auto-send ticket content to engineer agent (300ms delay for thread setup)
      setTimeout(() => {
        const input = window.chatApp?.elements?.messageInput;
        if (input) {
          input.value = `## Implement Ticket #${ticket_id}: ${ticket_title}\n\n${ticket_content}`;
          window.chatApp.sendMessageWithDebugInfo();
        }
      }, 300);
    };

    // If proactive build is enabled, skip the confirmation and auto-implement
    if (window.LLAMABOT_PROACTIVE_BUILD) {
      this.messageRenderer.addMessage(
        '<div class="plan-question-card answered"><div class="plan-question-text">Automatically switching to Engineer mode to implement this ticket...</div></div>',
        'implement_ticket', null
      );
      doImplement(null);
      return;
    }

    const switchId = `implement-${Date.now()}`;
    const html = `
      <div class="plan-question-card" data-switch-id="${switchId}">
        <div class="plan-question-text">Do you want me to switch to Engineer mode and implement this ticket?</div>
        <div class="plan-question-options">
          <button class="plan-option-btn plan-switch-btn" data-action="implement">
            <i class="fa-solid fa-code"></i> Yes, implement this
          </button>
          <button class="plan-skip-btn" data-action="skip">No thanks</button>
        </div>
      </div>
    `;
    this.messageRenderer.addMessage(html, 'implement_ticket', null);

    setTimeout(() => {
      const card = document.querySelector(`[data-switch-id="${switchId}"]`);
      if (!card) return;

      card.querySelector('[data-action="implement"]')?.addEventListener('click', () => doImplement(card));

      card.querySelector('[data-action="skip"]')?.addEventListener('click', () => {
        card.classList.add('answered');
        card.querySelectorAll('button').forEach(b => b.disabled = true);
        if (window.chatApp?.webSocketManager) {
          window.chatApp.webSocketManager.send({
            type: 'question_response',
            answer: 'no',
            thread_id,
            agent_name,
          });
        }
      });
    }, 0);
  }

  /**
   * Handle generic messages (tool, error, end, etc.)
   */
  handleGenericMessage(data) {
    if (data.type === 'end' || data.type === 'system_message' || data.type === 'error' || data.type === 'paywall_hit') {
      this.messageRenderer.handleEndMessage();
      // Remove beginner mode overlay when agent finishes
      this.iframeManager.removeStreamingOverlay();

      // Clear plan tracking when conversation ends
      this.activePlanId = null;
      this.planStepMapping.clear();
      // Finalize any remaining thinking message and reset all tracking
      this.finalizeCurrentThinking();
      this.currentThinkingId = null;
      this.currentThinkingBuffer = '';
      this.hasNonThinkingMessageSinceLastThinking = false;

      // Dispatch event to notify ChatApp to stop duration timer
      // Include elapsed time for display on completion badges
      const elapsedTime = this.appState.getFormattedElapsedTime();
      window.dispatchEvent(new CustomEvent('agentTaskCompleted', {
        detail: { elapsedTime }
      }));

      if (data.type === 'paywall_hit') {
        this.messageRenderer.renderPaywallMessage(PAYWALL_UPGRADE_URL);
      } else if ((data.type === 'system_message' || data.type === 'error') && data.content) {
        this.messageRenderer.addMessage(data.content, data.type, data.base_message);
      }
    } else {
      // In beginner/plan mode, hide sub-agent generic messages (tool results, etc.)
      if (this._isSimplifiedMode() && (data.agent_depth || 0) > 0) {
        return;
      }

      // Finalize thinking before tool messages so they appear interspersed
      if (data.type === 'tool') {
        this.finalizeCurrentThinking();
      }
      this.messageRenderer.addMessage(data.content, data.type, data.base_message);

      // Check if this is an updated todo list and update plan steps in real-time
      if (data.base_message?.name === 'write_todos' && data.base_message?.args) {
        try {
          const argsObj = typeof data.base_message.args === 'string'
            ? JSON.parse(data.base_message.args)
            : data.base_message.args;

          if (argsObj?.todos) {
            this.updatePlanSteps(argsObj.todos);
          }
        } catch (error) {
          console.warn('Failed to parse updated todos:', error);
        }
      }
    }
  }
}
