// AGENT = {NAME: 'llamabot', TYPE: 'default'};
// AGENT = {NAME: 'deep_research', TYPE: 'claude_llm_model'}; // uses create_react_agent which I believe is why we have to case into content in AIMessageChunk differently
// AGENT = {NAME: 'rails_agent', TYPE: 'claude_llm_model'};
// AGENT = {NAME: 'rails_agent', TYPE: 'default'};
AGENT = {NAME: 'rails_frontend_starter_agent', TYPE: 'claude_llm_model'};

// WebSocket connection management
let socket = null;
let currentThreadId = null;
let currentAiMessage = null;
let currentAiMessageBuffer = ''; // Buffer to accumulate streaming content for markdown parsing

// HTML streaming variables
let htmlFragmentBuffer = '';
let fullMessageBuffer = '';
let htmlChunksStartedStreaming = false;
let htmlChunksEndedStreaming = false;
let iframeFlushTimer = null;
const IFRAME_REFRESH_MS = 500; // Update iframe every 500ms

// Scroll management variables
let isUserAtBottom = true;
let scrollThreshold = 50; // pixels from bottom to consider "at bottom"

// Configure marked.js for better security and formatting
marked.setOptions({
    breaks: true,
    gfm: true,
    sanitize: false, // We'll handle XSS prevention differently
    smartLists: true,
    smartypants: true
});

// Function to parse markdown to HTML
function parseMarkdown(text) {
    if (!text) return '';

    try {
        // Parse markdown to HTML
        // if (AGENT.TYPE === 'claude_llm_model') {
        if (Array.isArray(text)) {
         text = text[0].text;
        }

        let html = marked.parse(text);

        // Basic XSS prevention - remove script tags and event handlers
        html = html.replace(/<script\b[^<]*(?:(?!<\/script>)<[^<]*)*<\/script>/gi, '');
        html = html.replace(/\son\w+="[^"]*"/gi, '');
        html = html.replace(/\son\w+='[^']*'/gi, '');

        return html;
    } catch (error) {
        console.error('Markdown parsing error:', error);
        // Fallback to plain text with line breaks
        try {
            return text.replace(/\n/g, '<br>');
        } catch (error) {
            console.error('Text.replace parsing error:', error);
            return text;
        }
    }
}

function getRailsUrl(){
    if (window.location.protocol === 'https:') {
        return 'https://rails-' + window.location.host;
    }
    else {
        return "http://localhost:3000";
    }
}

// Initialize WebSocket connection
function initWebSocket() {
    let wsUrl = `ws://${window.location.host}/ws`;
    if (window.location.protocol === 'https:') {
        wsUrl = `wss://${window.location.host}/ws`;
        //TODO: Swap this out with the getRailsUrl() helper function.
        document.getElementById('liveSiteFrame').src = 'https://rails-' + window.location.host;
    }

    socket = new WebSocket(wsUrl);

    socket.onopen = function() {
        console.log('WebSocket connected');
        updateConnectionStatus(true);
        document.getElementById('sendButton').disabled = false;
    };

    socket.onclose = function() {
        console.log('WebSocket disconnected');
        updateConnectionStatus(false);
        document.getElementById('sendButton').disabled = true;

        // Attempt to reconnect after 3 seconds
        setTimeout(initWebSocket, 3000);
    };

    socket.onerror = function(error) {
        console.error('WebSocket error:', error);
        addMessage('Connection error. Please refresh the page.', 'error');
    };

    socket.onmessage = function(event) { //when the websocket sends a message to our front-end, this function is called
        const data = JSON.parse(event.data);
        console.log('Received:', data.type);
        console.log('Data:', data);

        // Handle streaming AI messages
        if (data.type === 'AIMessageChunk') {
            if (data.content) { //this is the regular ai text content streaming back from LLM
                // Handle regular text content
                if (!currentAiMessage) {
                    currentAiMessage = addMessage('', 'ai', data);
                    currentAiMessageBuffer = ''; // Reset buffer for new AI message
                }
                // Check if we still have the typing indicator and remove it
                if (currentAiMessage.querySelector('.typing-indicator')) {
                    currentAiMessage.innerHTML = '';
                }
                if (AGENT.TYPE === 'claude_llm_model') { //follows claude's data content structure for AIMessageChunks.
                    if (data.content && data.content.length > 0) { // it's strange how claude's llm model requires this changed structure/format.
                        currentAiMessageBuffer += data.content[0].text;
                        currentAiMessage.innerHTML = parseMarkdown(currentAiMessageBuffer);
                    }
                } else {
                    currentAiMessageBuffer += data.content;
                    currentAiMessage.innerHTML = parseMarkdown(currentAiMessageBuffer);
                }
                checkIfUserAtBottom();
                scrollToBottom();
            } else if (data.content === '' || data.content === null) { //this is the tool call arguments streaming back from LLM.
                // Handle tool call chunks
                if (data.base_message && data.base_message.tool_call_chunks && data.base_message.tool_call_chunks[0]) {
                    let tool_call_data = data.base_message.tool_call_chunks[0].args;

                    // Buffer the tool call data
                    htmlFragmentBuffer += tool_call_data;
                    fullMessageBuffer += tool_call_data;

                    // Check for HTML start tag
                    let htmlTagIndex = htmlFragmentBuffer.indexOf('<html');
                    if (htmlTagIndex !== -1 && !htmlChunksStartedStreaming) {
                        htmlChunksStartedStreaming = true;
                        htmlFragmentBuffer = htmlFragmentBuffer.substring(htmlTagIndex);

                        // Show loading state in AI message
                        if (!currentAiMessage) {
                            currentAiMessage = addMessage('', 'ai', data);
                        }
                        currentAiMessage.innerHTML = '🎨 Generating your page...';

                        // Create the overlay with animation
                        createStreamingOverlay();
                    }

                    // Check for HTML end tag
                    let endingHtmlTagIndex = fullMessageBuffer.indexOf('</html>');
                    if (endingHtmlTagIndex !== -1 && !htmlChunksEndedStreaming) {
                        htmlChunksEndedStreaming = true;

                        // Update AI message to show completion
                        if (currentAiMessage) {
                            currentAiMessage.innerHTML = '✨ Page generated successfully!';
                        }

                        // Clear any pending flush timer
                        if (iframeFlushTimer) {
                            clearTimeout(iframeFlushTimer);
                            iframeFlushTimer = null;
                        }

                        // Do final flush
                        flushToIframe();

                        // Remove the overlay
                        removeStreamingOverlay();

                        // Reset buffers for next generation
                        htmlFragmentBuffer = '';
                        fullMessageBuffer = '';
                        htmlChunksStartedStreaming = false;
                        htmlChunksEndedStreaming = false;
                    }

                    // If HTML streaming is active, schedule iframe update
                    if (htmlChunksStartedStreaming && !htmlChunksEndedStreaming) {
                        // Clean the fragment
                        let cleanedFragment = htmlFragmentBuffer
                            .replace(/\\n/g, '\n')
                            .replace(/\\"/g, '"')
                            .replace(/\\t/g, '\t');

                        // Schedule iframe update if not already scheduled
                        if (!iframeFlushTimer) {
                            iframeFlushTimer = setTimeout(() => {
                                flushToIframe();
                                iframeFlushTimer = null;
                            }, IFRAME_REFRESH_MS);
                        }

                        // Clear the fragment buffer for next chunk
                        htmlFragmentBuffer = '';
                    }
                }
            }
        }
        else if (data.type === 'ai') {
            //TODO: This is where there's an issue with claude vs. openai.
            // Tool calls come back as undefined for claude models.
            if (data.base_message.tool_calls && data.base_message.tool_calls.length > 0) { //since we streamed the message with AIMessageChunk, we don't need to add a message unless there's a tool_call payload.
                addMessage(data.content, data.type, data.base_message);
            }

            // Old code:
            // if (data.tool_calls && data.tool_calls.length > 0) { //since we streamed the message with AIMessageChunk, we don't need to add a message unless there's a tool_call payload.
            //     addMessage(data.content, data.type, data.base_message);
            // }
        }
        else {
            addMessage(data.content, data.type, data.base_message);
        }
    };
}

function updateConnectionStatus(connected) {
    const status = document.getElementById('connectionStatus');
    if (connected) {
        status.className = 'connection-status connected';
        status.innerHTML = '<span class="status-dot"></span><span>Connected</span>';
    } else {
        status.className = 'connection-status disconnected';
        status.innerHTML = '<span class="status-dot"></span><span>Disconnected</span>';
    }
}

function sendMessage(debugInfo=null) {
    const input = document.getElementById('messageInput');
    const message = input.value.trim();
    let agentMode = document.getElementById('agentModeSelect').value;

    if (message && socket && socket.readyState === WebSocket.OPEN) {
        // Reset any previous AI message reference
        currentAiMessage = null;
        currentAiMessageBuffer = ''; // Reset markdown buffer for new message

        // Reset HTML streaming state
        htmlFragmentBuffer = '';
        fullMessageBuffer = '';
        htmlChunksStartedStreaming = false;
        htmlChunksEndedStreaming = false;
        if (iframeFlushTimer) {
            clearTimeout(iframeFlushTimer);
            iframeFlushTimer = null;
        }

        // Remove any existing overlay
        removeStreamingOverlay();

        // Add user message to chat
        addMessage(message, 'human', null);
        document.getElementById('thinkingIndicator').classList.remove('hidden'); // show thinking indicator

        // Clear input
        input.value = '';
        input.style.height = 'auto';

        // Generate thread ID if needed
        if (!currentThreadId) {
            console.log("Found Thread ID.")
            currentThreadId = crypto.randomUUID();
        }

        user_selected_agent_name = document.getElementById('agentModeSelect').value;
        // For reference:
        // <option value="engineer">Engineer Mode</option>
        // <option value="prototype">Prototype Mode</option>
        if (user_selected_agent_name == 'prototype') {
            AGENT.NAME = 'rails_frontend_starter_agent';
        } else if (user_selected_agent_name == 'engineer') {
            AGENT.NAME = 'rails_agent';
        }

        // Send message via WebSocket
        const messageData = {
            message: message,
            thread_id: currentThreadId,
            origin: window.location.host,
            debug_info: debugInfo,
            agent_name: AGENT.NAME, // agent name is used to route to the langgraph.json entry for the agent.
            agent_mode: agentMode // this is used to route to a subagent with a single langgraph agent (each agent has it's own langgraph.json entry.)
        };

        socket.send(JSON.stringify(messageData));
    }
}

/**
 * This adds a message to the conversation window.
 *
 * @param {string} content - The content of the message.
 * @param {string} type - The type of message. Either 'user', 'ai', 'tool', or 'error'.
 * @param {object} baseMessage - The base langgraph message object.
 * @returns {object} The message div.
 */
function addMessage(content, type, baseMessage = null) {
    const messageHistory = document.getElementById('message-history');
    const messageDiv = document.createElement('div');
    // Set appropriate class based on message type
    if (type === 'human') {
        messageDiv.className = 'message user-message';
        messageDiv.textContent = content;
    }
    else if (type === 'ai') {
        messageDiv.className = 'message ai-message';
        messageDiv.innerHTML = parseMarkdown(content);

        // We'll add a tool-message div that we later go back and add the output of what the tool responded with
        if (content == '' || content == null) { // this follows OpenAI's data content structure for AI message that has a tool call.
            if (baseMessage && baseMessage.tool_calls && baseMessage.tool_calls.length > 0) {
                messageDiv.className = 'message tool-message';

                const toolCall = baseMessage.tool_calls[0];
                let firstArgument = toolCall.args[Object.keys(toolCall.args)[0]];
                // debugger; //TODO: I want to make it so that it formats it with the first argument without the label.
                if (firstArgument == undefined) {
                    firstArgument = '';
                }
                messageDiv.innerHTML = createCollapsibleToolMessage(toolCall.name, firstArgument ,JSON.stringify(toolCall.args), '');
                messageDiv.id = baseMessage.tool_calls[0].id;
            }
        }
    }
    else if (type === 'tool') {
        const messageDiv = document.getElementById(baseMessage.tool_call_id);
        if (messageDiv) {
            messageDiv.className = 'message tool-message';
            // Update the collapsible content with the tool response
            updateCollapsibleToolMessage(messageDiv, content, baseMessage);
        }
        else { //TODO: Issue with Claude's LLM here, because there is no message div created by this point for Claude's model.
        // There was no message div!! This means we are likely using Claude's llm model. Let's create a new message div.
        }

    }
    else if (type === 'error') {
        messageDiv.className = 'message error-message';
        messageDiv.textContent = content;
    }
    else if (type === 'end') {
        console.log('end of stream');
        refreshMainIFrame(); //after langgraph, just refresh for now. In the future, we can do it more intelligently.
        // STOP thinking indicator.
        document.getElementById('thinkingIndicator').classList.add('hidden'); // stop thinking indicator

        // Play task completed sound
        const taskCompletedSound = document.getElementById('taskCompletedSound');
        if (taskCompletedSound) {
            taskCompletedSound.play().catch(error => {
                console.log('Could not play sound:', error);
            });
        }

        return; // Don't append anything for end messages
    }
    // Insert message before the scroll button if it exists
    const scrollButton = document.getElementById('scrollToBottomBtn');
    if (scrollButton) {
        messageHistory.insertBefore(messageDiv, scrollButton);
    } else {
        messageHistory.appendChild(messageDiv);
    }

    // For user messages, always scroll to bottom (force = true)
    // For other messages, only scroll if user is at bottom
    const forceScroll = (type === 'human');
    if (forceScroll) {
        scrollToBottom(true);
    } else {
        checkIfUserAtBottom();
        scrollToBottom();
    }

    return messageDiv;
}

function createCollapsibleToolMessage(toolName, firstArgument, toolArgs, toolResult) {
    const uniqueId = 'tool_' + Date.now() + '_' + Math.random().toString(36).substr(2, 9);
    // <span class="tool-summary">${toolName == 'write_todos' ? '📝' : toolName == 'ls' ? '📁' : toolName == 'read_file' ? '📄' : toolName == 'edit_file' || toolName == 'write_file' ? '✏️': toolName == 'search_file' || toolName == 'internet_search' ? '🔍' : toolName == 'bash_command' ? '⚡' : '🔨'} ${toolName} ${firstArgument}</span>
    console.log('toolName!!!', toolName);
    if (toolName == 'write_todos') {
        let todos = JSON.parse(toolArgs)['todos'];

        // Sort todos: in_progress → pending → completed
        const statusOrder = { 'in_progress': 0, 'pending': 1, 'completed': 2 };
        todos.sort((a, b) => statusOrder[a.status] - statusOrder[b.status]);

        let todoListHTML = '';

        // Current status for grouping
        let currentStatus = null;

        todos.forEach((todo, index) => {
            // Add section header if status changed
            if (todo.status !== currentStatus) {
                currentStatus = todo.status;
                const sectionTitle = todo.status === 'in_progress' ? 'In Progress' :
                                    todo.status === 'pending' ? 'Pending' : 'Completed';
                todoListHTML += `<div class="todo-section-header">${sectionTitle}</div>`;
            }

            const todoId = `todo-${uniqueId}-${index}`;
            const statusClass = `todo-status-${todo.status.replace('_', '-')}`; // ADD THIS LINE
            const icon = todo.status === 'completed' ? '✅' :
                         todo.status === 'in_progress' ? '🎯' : '🕒';

            todoListHTML += `
                <div class="todo-item ${statusClass}" onclick="toggleTodo('${todoId}')">
                    <span class="todo-icon">${icon}</span>
                    <span class="todo-text" id="${todoId}">${todo.content}</span>
                </div>
            `;
        });

        return `
            <div class="tool-collapsible" onclick="toggleToolCollapsible('${uniqueId}')">
                <span class="tool-summary">🎯 Todo List (${todos.length} tasks)</span>
            </div>
            <div class="tool-content" id="${uniqueId}">
                <div class="todo-container">
                    ${todoListHTML}
                </div>
            </div>
        `;
    }
    else if (toolName == 'edit_file') {
        return `
            <div class="tool-collapsible" onclick="toggleToolCollapsible('${uniqueId}')">
                <span class="tool-summary">Edit ${firstArgument}</span>
            </div>
            <div class="tool-content" id="${uniqueId}">
                <div class="tool-details">
                    <strong>Arguments:</strong><br>
                    <pre style="margin: 4px 0; font-size: 0.85em; white-space: pre-wrap;">${toolArgs}</pre>
                    ${toolResult ? `<strong>Result:</strong><br><pre style="margin: 4px 0; font-size: 0.85em; white-space: pre-wrap;">${toolResult}</pre>` : ''}
                </div>
            </div>
        `;
    }
    else {
        return `
            <div class="tool-collapsible" onclick="toggleToolCollapsible('${uniqueId}')">
                <span class="tool-summary">🔨${toolName} ${firstArgument}</span>
            </div>
            <div class="tool-content" id="${uniqueId}">
                <div class="tool-details">
                    <strong>Arguments:</strong><br>
                    <pre style="margin: 4px 0; font-size: 0.85em; white-space: pre-wrap;">${toolArgs}</pre>
                    ${toolResult ? `<strong>Result:</strong><br><pre style="margin: 4px 0; font-size: 0.85em; white-space: pre-wrap;">${toolResult}</pre>` : ''}
                </div>
            </div>
        `;
    }
}

function updateCollapsibleToolMessage(messageDiv, toolResult, baseMessage) {
    const toolContent = messageDiv.querySelector('.tool-content .tool-details');

    if (baseMessage.name == 'edit_file') {
        const tool_header_label = messageDiv.querySelector('.tool-collapsible')
        console.log('edit_file');
        if (baseMessage.artifact?.status == 'success') {
            tool_header_label.innerHTML = tool_header_label.innerHTML.replace('Edit', '✅ Edit');
            refreshMainIFrame(); // forces a refresh of the iframe to show the changes AND to get the new debug info. (rendered HTML)
        }
        else if(baseMessage.artifact?.status == 'error') {
            tool_header_label.innerHTML = tool_header_label.innerHTML.replace('Edit', '❌ Edit');
            refreshMainIFrame(); // forces a refresh of the iframe to show the changes AND to get the new debug info. (rendered HTML)
        }
    }
    if (toolContent) {
        // Check if result already exists to avoid duplication
        if (!toolContent.innerHTML.includes('<strong>Result:</strong>')) {
            toolContent.innerHTML += `<br><strong>Result:</strong><br><pre style="margin: 4px 0; font-size: 0.85em; white-space: pre-wrap;">${toolResult}</pre>`;
        }
    }
}

function toggleToolCollapsible(contentId) {
    const collapsible = document.querySelector(`[onclick="toggleToolCollapsible('${contentId}')"]`);
    const content = document.getElementById(contentId);

    if (collapsible && content) {
        collapsible.classList.toggle('expanded');
        content.classList.toggle('expanded');
    }
}

function checkIfUserAtBottom() {
    const messageHistory = document.getElementById('message-history');
    const scrollTop = messageHistory.scrollTop;
    const scrollHeight = messageHistory.scrollHeight;
    const clientHeight = messageHistory.clientHeight;

    // Check if user is within threshold pixels of the bottom
    isUserAtBottom = (scrollTop + clientHeight >= scrollHeight - scrollThreshold);

    // Show/hide scroll-to-bottom button
    updateScrollToBottomButton();

    return isUserAtBottom;
}

function scrollToBottom(force = false) {
    const messageHistory = document.getElementById('message-history');

    // Only scroll if user is at bottom or force is true
    if (force || isUserAtBottom) {
        messageHistory.scrollTop = messageHistory.scrollHeight;
        isUserAtBottom = true;
        // Update button visibility after scrolling
        updateScrollToBottomButton();
    }
}

function updateScrollToBottomButton() {
    const scrollButton = document.getElementById('scrollToBottomBtn');
    if (scrollButton) {
        if (!isUserAtBottom) {
            scrollButton.classList.add('visible');
        } else {
            scrollButton.classList.remove('visible');
        }
    }
}

function scrollToBottomManually() {
    const messageHistory = document.getElementById('message-history');
    messageHistory.scrollTop = messageHistory.scrollHeight;
    isUserAtBottom = true;
    updateScrollToBottomButton();
}

function refreshIframe() {
    const iframe = document.getElementById('contentFrame');
    // Small delay to ensure any server-side changes are complete
    setTimeout(() => {
        iframe.src = iframe.src;
    }, 100);
}

// Function to update iframe with buffered HTML content
function flushToIframe() {
    try {
        const iframe = document.getElementById('contentFrame');
        const cleanedHTML = fullMessageBuffer
            .replace(/\\n/g, '\n')
            .replace(/\\"/g, '"')
            .replace(/\\t/g, '\t')
            .replace(/\\r/g, '\r');

        // Write to iframe
        const iframeDoc = iframe.contentDocument || iframe.contentWindow.document;
        if (iframeDoc) {
            iframeDoc.open();
            iframeDoc.write(cleanedHTML);
            iframeDoc.close();

            // Auto-scroll iframe to bottom
            setTimeout(() => {
                if (iframeDoc.documentElement) {
                    iframeDoc.documentElement.scrollTop = iframeDoc.documentElement.scrollHeight;
                }
                if (iframeDoc.body) {
                    iframeDoc.body.scrollTop = iframeDoc.body.scrollHeight;
                }
            }, 100);
        }
    } catch (e) {
        console.log('Error updating iframe (normal during streaming):', e);
    }
}

// Function to create overlay with Lottie animation
function createStreamingOverlay() {
    // Check if overlay already exists
    if (document.getElementById('streamingOverlay')) {
        return;
    }

    // Get the browser content div that contains the iframe
    const browserContent = document.querySelector('.browser-content');
    if (!browserContent) return;

    // Create overlay div
    const overlay = document.createElement('div');
    overlay.id = 'streamingOverlay';
    overlay.style.position = 'absolute';
    overlay.style.top = '0';
    overlay.style.left = '0';
    overlay.style.width = '100%';
    overlay.style.height = '100%';
    overlay.style.background = 'rgba(0, 0, 0, 0.4)';
    overlay.style.display = 'flex';
    overlay.style.flexDirection = 'column';
    overlay.style.alignItems = 'center';
    overlay.style.zIndex = '10';
    overlay.style.borderRadius = '8px';

    // Create text for overlay
    const overlayText = document.createElement('div');
    overlayText.textContent = 'Your Page is Being Built!';
    overlayText.style.color = 'white';
    overlayText.style.fontSize = '2.5rem';
    overlayText.style.fontWeight = 'bold';
    overlayText.style.fontFamily = 'Arial, sans-serif';
    overlayText.style.textShadow = '2px 2px 4px rgba(0,0,0,0.5)';

    // Create text container for the top
    const textContainer = document.createElement('div');
    textContainer.style.padding = '30px';
    textContainer.style.width = '100%';
    textContainer.style.textAlign = 'center';

    // Create Lottie container
    const lottieContainer = document.createElement('div');
    lottieContainer.id = 'lottieAnimation';
    lottieContainer.style.width = '300px';
    lottieContainer.style.height = '300px';
    lottieContainer.style.position = 'absolute';
    lottieContainer.style.top = '50%';
    lottieContainer.style.left = '50%';
    lottieContainer.style.transform = 'translate(-50%, -50%)';

    // Add script for Lottie if not already present
    if (!document.querySelector('script[src*="lottie-player"]')) {
        const lottieScript = document.createElement('script');
        lottieScript.src = "https://unpkg.com/@dotlottie/player-component@latest/dist/dotlottie-player.js";
        document.head.appendChild(lottieScript);
    }

    // Create Lottie player element
    const lottiePlayer = document.createElement('dotlottie-player');
    lottiePlayer.src = "https://llamapress-ai-image-uploads.s3.us-west-2.amazonaws.com/hffa8kqjfn9yzfx28pogpvqhn7cd"; // Builder Image
    lottiePlayer.background = "transparent";
    lottiePlayer.speed = "1";
    lottiePlayer.style.width = "300px";
    lottiePlayer.style.height = "300px";
    lottiePlayer.setAttribute("autoplay", "");
    lottiePlayer.setAttribute("loop", "");

    // Append all elements
    textContainer.appendChild(overlayText);
    lottieContainer.appendChild(lottiePlayer);
    overlay.appendChild(textContainer);
    overlay.appendChild(lottieContainer);
    browserContent.appendChild(overlay);
}

// Function to remove overlay
function removeStreamingOverlay() {
    const overlay = document.getElementById('streamingOverlay');
    if (overlay) {
        overlay.remove();
    }
}

async function fetchThreads() {
    try {
        // Show loading state
        showThreadsLoading();

        const response = await fetch('/threads');
        const threads = await response.json();
        console.log(threads);

        // threads[0].state[4] is a date.
        // SORT based on this.
        threads.sort((a, b) => new Date(b.state[4]) - new Date(a.state[4])); // Sort by date, newest first.

        // Update menu items with thread titles
        populateMenuWithThreads(threads);
    } catch (error) {
        console.error('Error fetching threads:', error);
        showThreadsError();
    }
}

function showThreadsLoading() {
    const menuItems = document.querySelector('.menu-items');
    menuItems.innerHTML = `
        <div class="menu-item" style="opacity: 0.6; cursor: default;">
            <div class="typing-indicator" style="justify-content: center;">
                <span></span><span></span><span></span>
            </div>
            Loading conversations...
        </div>
    `;
}

function showThreadsError() {
    const menuItems = document.querySelector('.menu-items');
    menuItems.innerHTML = `
        <div class="menu-item" style="opacity: 0.6; cursor: default; color: #ff6b6b;">
            Failed to load conversations
        </div>
        <div class="menu-item" onclick="fetchThreads()" style="color: var(--accent-color); cursor: pointer;">
            🔄 Retry
        </div>
    `;
}

function populateMenuWithThreads(threads) {
    const menuItems = document.querySelector('.menu-items');

    // Clear existing menu items
    menuItems.innerHTML = '';

    if (!threads || threads.length === 0) {
        // Show empty state
        const emptyItem = document.createElement('div');
        emptyItem.className = 'menu-item';
        emptyItem.style.opacity = '0.6';
        emptyItem.textContent = 'No conversations yet';
        menuItems.appendChild(emptyItem);
        return;
    }

    // Generate conversation titles and add menu items
    threads.forEach((thread, index) => {
        const messages = thread.state[0]?.messages || [];
        const { title } = generateConversationSummary(messages);

        const menuItem = document.createElement('div');
        menuItem.className = 'menu-item';
        menuItem.textContent = title;

        // holding off on displaying a date for now. We don't display a date. (ChatGPT doesn't, so why should we?)
        // date = thread.state[4];
        // formattedDate = new Date(date).toLocaleDateString('en-US', { year: 'numeric', month: 'long', day: 'numeric' });
        // menuItem.textContent += ` - ${formattedDate}`;

        menuItem.onclick = () => handleThreadClick(thread.thread_id, title);

        menuItems.appendChild(menuItem);
    });
}

function generateConversationSummary(messages) {
    if (!messages || messages.length === 0) {
        return { title: 'New Conversation' };
    }

    // Find first user message for title
    const firstUserMessage = messages.find(msg => msg.type === 'human');
    let title = 'New Conversation';

    if (firstUserMessage && firstUserMessage.content) {
        // Use first 50 characters of first user message as title
        title = firstUserMessage.content.substring(0, 50);
        if (firstUserMessage.content.length > 50) {
            title += '...';
        }
    }

    return { title };
}

async function handleThreadClick(threadId, title) {
    console.log(`Loading thread: ${threadId} - ${title}`);

    try {
        // Set current thread ID
        currentThreadId = threadId;

        // Fetch thread messages
        const response = await fetch(`/chat-history/${threadId}`);
        const threadData = await response.json();

        // Clear current chat and load thread messages
        loadThreadMessages(threadData);

        // Close the menu
        closeMenu();

    } catch (error) {
        console.error('Error loading thread:', error);
        alert('Failed to load conversation. Please try again.');
    }
}

function loadThreadMessages(threadData) {
    const messageHistory = document.getElementById('message-history');

    // Clear current messages but preserve the scroll button
    const scrollButton = document.getElementById('scrollToBottomBtn');
    messageHistory.innerHTML = '';

    // Re-add the scroll button
    if (scrollButton) {
        messageHistory.appendChild(scrollButton);
    }

    // Get messages from thread state
    const messages = threadData[0]?.messages || [];

    if (messages.length === 0) {
        // Show default message if no messages
        const defaultMessage = document.createElement('div');
        defaultMessage.className = 'message ai-message';
        defaultMessage.textContent = "Hi! I'm Leonardo. What are we building today?";
        messageHistory.insertBefore(defaultMessage, scrollButton);
        return;
    }

    // Process and display messages
    messages.forEach(message => {
        addMessage(message.content, message.type, message);
    });

    // Scroll to bottom when loading a conversation (force = true)
    scrollToBottom(true);
}

// Helper function to escape HTML to prevent XSS
function escapeHtml(text) {
    const div = document.createElement('div');
    div.textContent = text;
    return div.innerHTML;
}

// Cookie helper functions
function setCookie(name, value, days) {
    let expires = "";
    if (days) {
        let date = new Date();
        date.setTime(date.getTime() + (days * 24 * 60 * 60 * 1000));
        expires = "; expires=" + date.toUTCString();
    }
    document.cookie = name + "=" + (value || "") + expires + "; path=/";
}

function getCookie(name) {
    let nameEQ = name + "=";
    let ca = document.cookie.split(';');
    for (let i = 0; i < ca.length; i++) {
        let c = ca[i];
        while (c.charAt(0) == ' ') c = c.substring(1, c.length);
        if (c.indexOf(nameEQ) == 0) return c.substring(nameEQ.length, c.length);
    }
    return null;
}

// Event listeners
document.getElementById('sendButton').addEventListener('click', sendMessageWithDebugInfo);

// Scroll-to-bottom button event listener
document.getElementById('scrollToBottomBtn').addEventListener('click', scrollToBottomManually);

// Save agent mode to cookie on change
document.getElementById('agentModeSelect').addEventListener('change', function() {
    setCookie('agentMode', this.value, 365); // Remember for a year
});

document.getElementById('messageInput').addEventListener('keydown', function(e) {
    if (e.key === 'Enter' && !e.shiftKey) {
        e.preventDefault();
        sendMessageWithDebugInfo();
    }
});

function sendMessageWithDebugInfo() {
    // sendMessage();
    const debugInfo = getRailsDebugInfo((debugInfoJson) => {
        sendMessage(debugInfoJson);
    });
}

// Refresh button functionality
document.getElementById('refreshButton').addEventListener('click', function() {
    // Add a small loading animation by rotating the icon
    const button = this;
    const svg = button.querySelector('svg');
    svg.style.animation = 'spin 0.5s linear';

    // Remove animation after it completes
    setTimeout(() => {
        svg.style.animation = '';
    }, 500);

    refreshMainIFrame();


});

function refreshMainIFrame(){
    // Get all iframes and refresh them
    const iframes = document.querySelectorAll('iframe');
    iframes.forEach(iframe => {

        let isRailsIFrame = iframe.src.includes(':3000') || iframe.src.includes('https://rails-');
        // debugger; // see where the user is at right now.

        if (isRailsIFrame) {
            getRailsDebugInfo((debugInfoJson) => {
                console.log('debugInfoJson', debugInfoJson);
                if (iframe.src) {
                    let baseUrl = 'localhost:3000'; //todo. fix this.
                    let additionalRequestPath = debugInfoJson.request_path;
                    if (!additionalRequestPath) {
                        console.warn('Warning: debugInfoJson.request_path is undefined or null! Theres probably a rails error.', debugInfoJson);
                        additionalRequestPath = '/'; // fallback path, choose appropriate default for your app
                    }
                    iframe.src = getRailsUrl() + additionalRequestPath;
                }
            });
        }
        else {
            if (iframe.src) {
                iframe.src = iframe.src;
            }
        }
    });
}

// Back button functionality
document.getElementById('backButton').addEventListener('click', function() {
    const liveSiteFrame = document.getElementById('liveSiteFrame');
    if (liveSiteFrame && liveSiteFrame.contentWindow) {
        try {
            // Try to navigate back in the iframe's history
            liveSiteFrame.contentWindow.history.back();
        } catch (error) {
            // If there's a cross-origin error, we can't access the iframe's history
            // This is expected when the iframe is from a different domain
            console.log('Cannot access iframe history due to cross-origin restrictions');

            // Alternative: We could maintain our own history stack if needed
            // For now, just provide visual feedback that the button was clicked
            const button = this;
            button.style.transform = 'scale(0.9)';
            setTimeout(() => {
                button.style.transform = '';
            }, 150);
        }
    }
});

// Tab switching logic
const tabs = document.querySelectorAll('.tab');
const iframes = document.querySelectorAll('.content-iframe');

tabs.forEach(tab => {
    tab.addEventListener('click', () => {
        // Deactivate all tabs and iframes
        tabs.forEach(t => t.classList.remove('active'));
        iframes.forEach(i => i.classList.remove('active'));

        // Activate clicked tab and corresponding iframe
        tab.classList.add('active');
        const targetIframeId = tab.dataset.target;
        document.getElementById(targetIframeId).classList.add('active');
    });
});

// View mode toggle logic
const desktopModeBtn = document.getElementById('desktopModeBtn');
const mobileModeBtn = document.getElementById('mobileModeBtn');
const browserContent = document.querySelector('.browser-content');

if (desktopModeBtn && mobileModeBtn && browserContent) {
    desktopModeBtn.addEventListener('click', () => {
        browserContent.classList.remove('mobile-view');
        desktopModeBtn.classList.add('active');
        mobileModeBtn.classList.remove('active');
    });

    mobileModeBtn.addEventListener('click', () => {
        browserContent.classList.add('mobile-view');
        mobileModeBtn.classList.add('active');
        desktopModeBtn.classList.remove('active');
    });
}

// Auto-resize textarea
document.getElementById('messageInput').addEventListener('input', function() {
    this.style.height = 'auto';
    this.style.height = this.scrollHeight + 'px';
});

// Hamburger menu functionality
function toggleMenu() {
    const hamburger = document.getElementById('hamburgerMenu');
    const drawer = document.getElementById('menuDrawer');

    hamburger.classList.toggle('active');
    drawer.classList.toggle('open');
}

function closeMenu() {
    const hamburger = document.getElementById('hamburgerMenu');
    const drawer = document.getElementById('menuDrawer');

    hamburger.classList.remove('active');
    drawer.classList.remove('open');
}

// Initialize hamburger menu event listeners
document.addEventListener('DOMContentLoaded', function() {
    const hamburgerMenu = document.getElementById('hamburgerMenu');
    const closeDrawer = document.getElementById('closeDrawer');

    if (hamburgerMenu) {
        hamburgerMenu.addEventListener('click', toggleMenu);
    }

    if (closeDrawer) {
        closeDrawer.addEventListener('click', closeMenu);
    }
});

// Close menu when clicking outside
document.addEventListener('click', function(event) {
    const hamburger = document.getElementById('hamburgerMenu');
    const drawer = document.getElementById('menuDrawer');

    if (!hamburger.contains(event.target) && !drawer.contains(event.target)) {
        closeMenu();
    }
});

// Add scroll event listener to track user scroll position
document.getElementById('message-history').addEventListener('scroll', function() {
    checkIfUserAtBottom();
});

// Mobile view management
let currentMobileView = 'chat'; // Default to chat view on mobile

function switchToMobileView(view) {
    const body = document.body;

    // Remove existing mobile view classes
    body.classList.remove('mobile-chat-view', 'mobile-iframe-view');

    // Add appropriate class based on view
    if (view === 'chat') {
        body.classList.add('mobile-chat-view');
        currentMobileView = 'chat';

        // Scroll to bottom of chat when switching to chat view
        setTimeout(() => {
            checkIfUserAtBottom();
            if (!isUserAtBottom) {
                scrollToBottom(true);
            }
        }, 300); // Wait for transition to complete
    } else if (view === 'iframe') {
        body.classList.add('mobile-iframe-view');
        currentMobileView = 'iframe';
    }
}

function initializeMobileView() {
    // Check if we're on mobile and set initial view
    if (window.innerWidth <= 768) {
        document.body.classList.add('mobile-chat-view');
        currentMobileView = 'chat';
    }
}

// Handle window resize
function handleResize() {
    if (window.innerWidth <= 768) {
        // Mobile view - ensure we have a mobile view class
        if (!document.body.classList.contains('mobile-chat-view') &&
            !document.body.classList.contains('mobile-iframe-view')) {
            switchToMobileView(currentMobileView);
        }
    } else {
        // Desktop view - remove mobile classes
        document.body.classList.remove('mobile-chat-view', 'mobile-iframe-view');
    }
}

// Listen for window resize
window.addEventListener('resize', handleResize);

// Initialize mobile view on page load
initializeMobileView();

// Load settings from cookies
function loadSettingsFromCookies() {
    // Load agent mode
    const savedMode = getCookie('agentMode');
    if (savedMode) {
        const selectElement = document.getElementById('agentModeSelect');
        if (selectElement) {
            // Check if the saved mode is a valid option
            if (Array.from(selectElement.options).some(option => option.value === savedMode)) {
                selectElement.value = savedMode;
            }
        }
    }
}

function getRailsDebugInfo(callback, timeout = 250) { // 1/4 second timout is plenty of time since it's inter-process communication. Timeout after 1/4 second = Ruby on Rails server error, and application.js likelly didn't get load.
    const iframe = document.getElementById('liveSiteFrame');
    if (!iframe || !iframe.contentWindow) {
        callback(new Error("Iframe not available"));
        return;
    }

    //NOTE: eventually, we will want to make this a secure hash of the user's session id, so we can verify and authenticate the response.
    const messageId = Math.random().toString(36).substr(2, 9);

    function handleMessage(event) {
        if (event.data && event.data.source === "llamapress") {
            window.removeEventListener("message", handleMessage);
            clearTimeout(timer);
            callback(event.data);

            //TODO: This is an additional security measure we will add in the future.
            // let allowedUrl = `${window.location.host}`;
            // if (window.location.protocol === 'https:') {
            //     allowedUrl = `rails.${window.location.host}`;
            // }
        }
    }

    // Listen for messages coming back from the rails iframe
    window.addEventListener("message", handleMessage);

    // send message
    iframe.contentWindow.postMessage({ source: 'leonardo', type: "get_debug_info", id: messageId }, "*");

    // fallback timeout
    const timer = setTimeout(() => {
        window.removeEventListener("message", handleMessage);
        callback(new Error("No response from Rails iframe"));
    }, timeout);
}

// Initialize WebSocket on page load
initWebSocket();

fetchThreads();

// Load settings after other initializations
loadSettingsFromCookies();

function toggleTodo(todoId) {
    event.stopPropagation(); // Prevent triggering parent tool collapse
    const todoText = document.getElementById(todoId);
    todoText.classList.toggle('expanded');
}
