import React, { useState, useEffect, useRef } from 'react';

function App() {
  // State management
  const [socket, setSocket] = useState(null);
  const [currentThreadId, setCurrentThreadId] = useState(null);
  const [messages, setMessages] = useState([
    { id: `msg-${Date.now()}-${Math.random()}`, type: 'ai', content: "Hi! I'm Leonardo. What are we building today?" }
  ]);
  const [inputMessage, setInputMessage] = useState('');
  const [isConnected, setIsConnected] = useState(false);
  const [isConnecting, setIsConnecting] = useState(true);
  const [isThinking, setIsThinking] = useState(false);
  const [isMenuOpen, setIsMenuOpen] = useState(false);
  const [threads, setThreads] = useState([]);
  const [activeTab, setActiveTab] = useState('liveSiteFrame');
  const [currentMobileView, setCurrentMobileView] = useState('chat');
  const [isUserAtBottom, setIsUserAtBottom] = useState(true);
  const [showScrollButton, setShowScrollButton] = useState(false);

  // Refs
  const messageHistoryRef = useRef(null);
  const currentAiMessageRef = useRef(null);
  const currentAiMessageBufferRef = useRef('');
  const htmlFragmentBufferRef = useRef('');
  const fullMessageBufferRef = useRef('');
  const htmlChunksStartedStreamingRef = useRef(false);
  const htmlChunksEndedStreamingRef = useRef(false);
  const iframeFlushTimerRef = useRef(null);
  const contentFrameRef = useRef(null);
  const socketRef = useRef(null);
  const messageIdCounterRef = useRef(0);

  // Constants
  const AGENT = { NAME: 'rails_agent', TYPE: 'default' };
  const IFRAME_REFRESH_MS = 500;
  const scrollThreshold = 50;

  // Generate unique message ID
  const generateMessageId = () => {
    messageIdCounterRef.current += 1;
    return `msg-${Date.now()}-${messageIdCounterRef.current}-${Math.random().toString(36).substr(2, 9)}`;
  };

  // WebSocket connection
  useEffect(() => {
    // Add a small delay to ensure the backend is ready
    const timer = setTimeout(() => {
      const ws = initWebSocket();
      fetchThreads();
      initializeMobileView();
    }, 1000); // 1 second delay
    
    window.addEventListener('resize', handleResize);
    return () => {
      clearTimeout(timer);
      window.removeEventListener('resize', handleResize);
      if (socketRef.current) socketRef.current.close();
    };
  }, []); // Empty dependency array - only run once

  const initWebSocket = () => {
    // Close existing connection if any
    if (socketRef.current && socketRef.current.readyState === WebSocket.OPEN) {
      socketRef.current.close();
    }

    // Backend is running in Docker on port 8000
    let wsUrl = 'ws://localhost:8000/ws';
    if (window.location.protocol === 'https:') {
      wsUrl = 'wss://localhost:8000/ws';
    }

    const ws = new WebSocket(wsUrl);
    socketRef.current = ws;

    // Add connection timeout
    const connectionTimeout = setTimeout(() => {
      if (ws.readyState === WebSocket.CONNECTING) {
        console.log('WebSocket connection timeout, closing...');
        ws.close();
      }
    }, 10000); // 10 second timeout

    ws.onopen = () => {
      console.log('WebSocket connected');
      clearTimeout(connectionTimeout);
      setIsConnected(true);
      setIsConnecting(false);
      setSocket(ws);
    };

    ws.onclose = (event) => {
      console.log('WebSocket disconnected', event.code, event.reason);
      clearTimeout(connectionTimeout);
      setIsConnected(false);
      setIsConnecting(false);
      setSocket(null);
      
      // Only show error message if it's not a normal closure and we were previously connected
      if (event.code !== 1000 && isConnected) {
        addMessage('Connection lost. Attempting to reconnect...', 'error');
      }
      
      // Attempt to reconnect after 3 seconds
      setTimeout(() => {
        if (socketRef.current === ws) { // Only reconnect if this is still the current socket
          console.log('Attempting to reconnect WebSocket...');
          setIsConnecting(true);
          initWebSocket();
        }
      }, 3000);
    };

    ws.onerror = (error) => {
      console.error('WebSocket error:', error);
      clearTimeout(connectionTimeout);
      // Don't show error message immediately - let the onclose handler manage reconnection
      // Only show error if we've been trying to connect for a while
      if (socketRef.current === ws && !isConnected) {
        // This is likely an initial connection failure, don't show error yet
        console.log('Initial WebSocket connection attempt failed, will retry...');
      }
    };

    ws.onmessage = (event) => {
      const data = JSON.parse(event.data);
      console.log('Received:', data);
      handleWebSocketMessage(data);
    };

    return ws;
  };

  const handleWebSocketMessage = (data) => {
    if (data.type === 'AIMessageChunk') {
      if (data.content) {
        // Handle regular text content
        if (!currentAiMessageRef.current) {
          const newMessageId = generateMessageId();
          const newMessage = { 
            id: newMessageId, 
            type: 'ai', 
            content: '',
            isStreaming: true 
          };
          setMessages(prev => [...prev, newMessage]);
          currentAiMessageRef.current = newMessageId;
          currentAiMessageBufferRef.current = '';
        }

        if (AGENT.TYPE === 'deep_agent') {
          if (data.content && data.content.length > 0) {
            currentAiMessageBufferRef.current += data.content[0].text;
          }
        } else {
          currentAiMessageBufferRef.current += data.content;
        }

        setMessages(prev => prev.map(msg => 
          msg.id === currentAiMessageRef.current 
            ? { ...msg, content: currentAiMessageBufferRef.current }
            : msg
        ));
        
        checkIfUserAtBottom();
        scrollToBottom();
      } else if (data.content === '' || data.content === null) {
        // Handle tool call chunks
        if (data.base_message && data.base_message.tool_call_chunks && data.base_message.tool_call_chunks[0]) {
          let tool_call_data = data.base_message.tool_call_chunks[0].args;
          
          htmlFragmentBufferRef.current += tool_call_data;
          fullMessageBufferRef.current += tool_call_data;
          
          let htmlTagIndex = htmlFragmentBufferRef.current.indexOf('<html');
          if (htmlTagIndex !== -1 && !htmlChunksStartedStreamingRef.current) {
            htmlChunksStartedStreamingRef.current = true;
            htmlFragmentBufferRef.current = htmlFragmentBufferRef.current.substring(htmlTagIndex);
            
            if (!currentAiMessageRef.current) {
              const newMessageId = generateMessageId();
              const newMessage = { 
                id: newMessageId, 
                type: 'ai', 
                content: '🎨 Generating your page...',
                isStreaming: true 
              };
              setMessages(prev => [...prev, newMessage]);
              currentAiMessageRef.current = newMessageId;
            }
            
            createStreamingOverlay();
          }
          
          let endingHtmlTagIndex = fullMessageBufferRef.current.indexOf('</html>');
          if (endingHtmlTagIndex !== -1 && !htmlChunksEndedStreamingRef.current) {
            htmlChunksEndedStreamingRef.current = true;
            
            setMessages(prev => prev.map(msg => 
              msg.id === currentAiMessageRef.current 
                ? { ...msg, content: '✨ Page generated successfully!' }
                : msg
            ));
            
            if (iframeFlushTimerRef.current) {
              clearTimeout(iframeFlushTimerRef.current);
              iframeFlushTimerRef.current = null;
            }
            
            flushToIframe();
            removeStreamingOverlay();
            
            // Reset buffers
            htmlFragmentBufferRef.current = '';
            fullMessageBufferRef.current = '';
            htmlChunksStartedStreamingRef.current = false;
            htmlChunksEndedStreamingRef.current = false;
          }
          
          if (htmlChunksStartedStreamingRef.current && !htmlChunksEndedStreamingRef.current) {
            if (!iframeFlushTimerRef.current) {
              iframeFlushTimerRef.current = setTimeout(() => {
                flushToIframe();
                iframeFlushTimerRef.current = null;
              }, IFRAME_REFRESH_MS);
            }
            htmlFragmentBufferRef.current = '';
          }
        }
      }
    } else if (data.type === 'ai') {
      if (data.tool_calls && data.tool_calls.length > 0) {
        addMessage(data.content, data.type, data.base_message);
      }
    } else if (data.type === 'end') {
      setIsThinking(false);
      currentAiMessageRef.current = null;
    } else {
      addMessage(data.content, data.type, data.base_message);
    }
  };

  const addMessage = (content, type, baseMessage = null) => {
    if (type === 'end') {
      setIsThinking(false);
      return;
    }

    const newMessage = {
      id: generateMessageId(),
      type: type,
      content: content,
      baseMessage: baseMessage
    };

    if (type === 'tool' && baseMessage?.tool_call_id) {
      setMessages(prev => prev.map(msg => 
        msg.baseMessage?.tool_calls?.[0]?.id === baseMessage.tool_call_id
          ? { ...msg, toolResult: content }
          : msg
      ));
    } else {
      setMessages(prev => [...prev, newMessage]);
    }

    if (type === 'human') {
      scrollToBottom(true);
    } else {
      checkIfUserAtBottom();
      scrollToBottom();
    }
  };

  const sendMessage = () => {
    if (inputMessage.trim() && socket && socket.readyState === WebSocket.OPEN) {
      currentAiMessageRef.current = null;
      currentAiMessageBufferRef.current = '';
      
      // Reset HTML streaming state
      htmlFragmentBufferRef.current = '';
      fullMessageBufferRef.current = '';
      htmlChunksStartedStreamingRef.current = false;
      htmlChunksEndedStreamingRef.current = false;
      if (iframeFlushTimerRef.current) {
        clearTimeout(iframeFlushTimerRef.current);
        iframeFlushTimerRef.current = null;
      }
      
      removeStreamingOverlay();
      
      addMessage(inputMessage, 'human', null);
      setIsThinking(true);
      
      if (!currentThreadId) {
        const now = new Date();
        setCurrentThreadId(
          now.getFullYear() + '-' + 
          String(now.getMonth() + 1).padStart(2, '0') + '-' + 
          String(now.getDate()).padStart(2, '0') + '_' + 
          String(now.getHours()).padStart(2, '0') + '-' + 
          String(now.getMinutes()).padStart(2, '0') + '-' + 
          String(now.getSeconds()).padStart(2, '0')
        );
      }

      const messageData = {
        message: inputMessage,
        thread_id: currentThreadId,
        agent_name: AGENT.NAME
      };
      
      socket.send(JSON.stringify(messageData));
      setInputMessage('');
    }
  };

  const fetchThreads = async () => {
    try {
      // Backend is running in Docker on port 8000
      const backendUrl = 'http://localhost:8000/threads';
        
      const response = await fetch(backendUrl);
      
      if (!response.ok) {
        throw new Error(`HTTP error! status: ${response.status}`);
      }
      
      const contentType = response.headers.get("content-type");
      if (!contentType || !contentType.includes("application/json")) {
        throw new TypeError("Response was not JSON");
      }
      
      const threadsData = await response.json();
      const sortedThreads = threadsData.sort((a, b) => 
        new Date(b.state[4]) - new Date(a.state[4])
      );
      setThreads(sortedThreads);
    } catch (error) {
      console.error('Error fetching threads:', error);
      // Don't show error in UI for now, just log it
    }
  };

  const loadThread = async (threadId) => {
    try {
      setCurrentThreadId(threadId);
      
      const backendUrl = `http://localhost:8000/chat-history/${threadId}`;
        
      const response = await fetch(backendUrl);
      
      if (!response.ok) {
        throw new Error(`HTTP error! status: ${response.status}`);
      }
      
      const threadData = await response.json();
      
      const threadMessages = threadData[0]?.messages || [];
      const formattedMessages = threadMessages.map(msg => ({
        id: generateMessageId(),
        type: msg.type,
        content: msg.content,
        baseMessage: msg
      }));
      
      setMessages(formattedMessages.length > 0 ? formattedMessages : [
        { id: generateMessageId(), type: 'ai', content: "Hi! I'm Leonardo. What are we building today?" }
      ]);
      
      setIsMenuOpen(false);
      scrollToBottom(true);
    } catch (error) {
      console.error('Error loading thread:', error);
      alert('Failed to load conversation. Please check if the backend is running.');
    }
  };

  const checkIfUserAtBottom = () => {
    if (!messageHistoryRef.current) return;
    
    const { scrollTop, scrollHeight, clientHeight } = messageHistoryRef.current;
    const isAtBottom = scrollTop + clientHeight >= scrollHeight - scrollThreshold;
    setIsUserAtBottom(isAtBottom);
    setShowScrollButton(!isAtBottom);
  };

  const scrollToBottom = (force = false) => {
    if (!messageHistoryRef.current) return;
    
    if (force || isUserAtBottom) {
      messageHistoryRef.current.scrollTop = messageHistoryRef.current.scrollHeight;
      setIsUserAtBottom(true);
      setShowScrollButton(false);
    }
  };

  const flushToIframe = () => {
    try {
      const iframe = contentFrameRef.current;
      if (!iframe) return;
      
      const cleanedHTML = fullMessageBufferRef.current
        .replace(/\\n/g, '\n')
        .replace(/\\"/g, '"')
        .replace(/\\t/g, '\t')
        .replace(/\\r/g, '\r');
      
      const iframeDoc = iframe.contentDocument || iframe.contentWindow.document;
      if (iframeDoc) {
        iframeDoc.open();
        iframeDoc.write(cleanedHTML);
        iframeDoc.close();
        
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
      console.log('Error updating iframe:', e);
    }
  };

  const createStreamingOverlay = () => {
    // This would be handled by state in React
    console.log('Creating streaming overlay');
  };

  const removeStreamingOverlay = () => {
    // This would be handled by state in React
    console.log('Removing streaming overlay');
  };

  const initializeMobileView = () => {
    if (window.innerWidth <= 768) {
      document.body.classList.add('mobile-chat-view');
      setCurrentMobileView('chat');
    }
  };

  const handleResize = () => {
    if (window.innerWidth <= 768) {
      if (!document.body.classList.contains('mobile-chat-view') && 
          !document.body.classList.contains('mobile-iframe-view')) {
        switchToMobileView(currentMobileView);
      }
    } else {
      document.body.classList.remove('mobile-chat-view', 'mobile-iframe-view');
    }
  };

  const switchToMobileView = (view) => {
    document.body.classList.remove('mobile-chat-view', 'mobile-iframe-view');
    
    if (view === 'chat') {
      document.body.classList.add('mobile-chat-view');
      setCurrentMobileView('chat');
      setTimeout(() => {
        checkIfUserAtBottom();
        if (!isUserAtBottom) {
          scrollToBottom(true);
        }
      }, 300);
    } else if (view === 'iframe') {
      document.body.classList.add('mobile-iframe-view');
      setCurrentMobileView('iframe');
    }
  };

  const parseMarkdown = (text) => {
    if (!text) return '';
    
    try {
      // Using marked.js from CDN
      if (window.marked) {
        let html = window.marked.parse(text);
        // Basic XSS prevention
        html = html.replace(/<script\b[^<]*(?:(?!<\/script>)<[^<]*)*<\/script>/gi, '');
        html = html.replace(/\son\w+="[^"]*"/gi, '');
        html = html.replace(/\son\w+='[^']*'/gi, '');
        return html;
      }
      return text.replace(/\n/g, '<br>');
    } catch (error) {
      console.error('Markdown parsing error:', error);
      return text.replace(/\n/g, '<br>');
    }
  };

  const refreshIframes = () => {
    const liveSiteFrame = document.getElementById('liveSiteFrame');
    const contentFrame = document.getElementById('contentFrame');
    const gitFrame = document.getElementById('gitFrame');
    
    [liveSiteFrame, contentFrame, gitFrame].forEach(iframe => {
      if (iframe && iframe.src) {
        // Store the current src and reassign to refresh the iframe
        const currentSrc = iframe.src;
        iframe.src = '';
        iframe.src = currentSrc;
      }
    });
  };

  const generateConversationSummary = (messages) => {
    if (!messages || messages.length === 0) {
      return { title: 'New Conversation' };
    }
    
    const firstUserMessage = messages.find(msg => msg.type === 'human');
    let title = 'New Conversation';
    
    if (firstUserMessage && firstUserMessage.content) {
      title = firstUserMessage.content.substring(0, 50);
      if (firstUserMessage.content.length > 50) {
        title += '...';
      }
    }
    
    return { title };
  };

  // Handle text area auto-resize
  const handleInputChange = (e) => {
    setInputMessage(e.target.value);
    const textarea = e.target;
    textarea.style.height = 'auto';
    textarea.style.height = textarea.scrollHeight + 'px';
  };

  return (
    <div className="flex h-screen bg-gray-900 text-gray-100 overflow-hidden">
      {/* Chat Section */}
      <div className="w-1/3 h-full flex flex-col border-r border-gray-700 relative chat-section">
        {/* Chat Header */}
        <div className="h-[60px] px-4 py-3 bg-gray-800 border-b border-gray-700 flex items-center gap-3">
          <button 
            className="w-8 h-8 flex flex-col justify-center items-center gap-1 p-1.5 rounded hover:bg-gray-700 transition-colors hamburger-menu"
            onClick={() => setIsMenuOpen(!isMenuOpen)}
          >
            <span className={`w-5 h-0.5 bg-gray-100 rounded transition-all ${isMenuOpen ? 'rotate-45 translate-y-1.5' : ''}`}></span>
            <span className={`w-5 h-0.5 bg-gray-100 rounded transition-all ${isMenuOpen ? 'opacity-0' : ''}`}></span>
            <span className={`w-5 h-0.5 bg-gray-100 rounded transition-all ${isMenuOpen ? '-rotate-45 -translate-y-1.5' : ''}`}></span>
          </button>
          <img 
            src="https://service-jobs-images.s3.us-east-2.amazonaws.com/7rl98t1weu387r43il97h6ipk1l7" 
            alt="LlamaBot Logo" 
            className="w-8 h-8 rounded-lg"
          />
          <h1 className="text-xl font-semibold">Leonardo</h1>
          <button 
            className="ml-auto mobile-view-toggle hidden"
            onClick={() => switchToMobileView('iframe')}
          >
            <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" className="w-5 h-5">
              <rect x="3" y="3" width="18" height="18" rx="2" ry="2"/>
              <line x1="9" y1="3" x2="9" y2="21"/>
            </svg>
          </button>
        </div>

        {/* Menu Drawer */}
        <div className={`absolute top-0 left-0 w-[280px] h-full bg-gray-800 border-r border-gray-700 shadow-lg z-[999] transform transition-transform menu-drawer ${isMenuOpen ? 'translate-x-0' : '-translate-x-full'}`}>
          <div className="h-full flex flex-col">
            <div className="h-[60px] px-4 py-3 pl-[60px] bg-gray-800 border-b border-gray-700 flex items-center">
              <h3 className="text-xl font-semibold">Conversations</h3>
            </div>
            <div className="flex-1 py-4 overflow-y-auto">
              {threads.length === 0 ? (
                <div className="px-5 py-3 text-gray-400">No conversations yet</div>
              ) : (
                threads.map((thread) => {
                  const { title } = generateConversationSummary(thread.state[0]?.messages || []);
                  return (
                    <div 
                      key={thread.thread_id}
                      className="px-5 py-3 cursor-pointer hover:bg-gray-700 transition-colors border-b border-gray-700"
                      onClick={() => loadThread(thread.thread_id)}
                    >
                      {title}
                    </div>
                  );
                })
              )}
            </div>
          </div>
        </div>

        {/* Message History */}
        <div 
          ref={messageHistoryRef}
          className="flex-grow p-4 overflow-y-auto bg-gray-800 flex flex-col gap-4 relative message-history"
          onScroll={checkIfUserAtBottom}
        >
          {messages.map((message) => (
            <div 
              key={message.id}
              className={`p-3 rounded-xl max-w-[85%] break-words relative text-sm ${
                message.type === 'human' ? 'bg-gray-700 self-end rounded-br-sm' :
                message.type === 'ai' ? 'bg-gray-600 self-start rounded-bl-sm' :
                message.type === 'tool' ? 'bg-blue-700 self-start rounded-bl-sm' :
                message.type === 'error' ? 'bg-red-900/20 border border-red-500/50 text-red-400' : ''
              }`}
            >
              {message.type === 'ai' ? (
                <div dangerouslySetInnerHTML={{ __html: parseMarkdown(message.content) }} />
              ) : message.baseMessage?.tool_calls ? (
                <div>
                  <div className="font-bold mb-1">
                    🔨 {message.baseMessage.tool_calls[0].name}
                  </div>
                  <pre className="text-xs whitespace-pre-wrap">
                    {JSON.stringify(message.baseMessage.tool_calls[0].args, null, 2)}
                  </pre>
                  {message.toolResult && (
                    <div className="mt-2 pt-2 border-t border-gray-600">
                      <strong>Result:</strong>
                      <pre className="text-xs whitespace-pre-wrap mt-1">{message.toolResult}</pre>
                    </div>
                  )}
                </div>
              ) : (
                message.content
              )}
            </div>
          ))}
          
          {/* Scroll to bottom button */}
          {showScrollButton && (
            <button 
              className="fixed bottom-[120px] right-[calc(66.67%+20px)] w-7 h-7 bg-white/10 border border-white/20 rounded-full flex items-center justify-center shadow-lg transition-all hover:bg-white/20 opacity-80 hover:opacity-100"
              onClick={() => scrollToBottom(true)}
            >
              <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" className="w-3.5 h-3.5">
                <path d="M6 9l6 6 6-6"/>
              </svg>
            </button>
          )}
        </div>

        {/* Input Area */}
        <div className="p-4 bg-gray-800 border-t border-gray-700 flex flex-col gap-3">
          <textarea 
            value={inputMessage}
            onChange={handleInputChange}
            onKeyDown={(e) => {
              if (e.key === 'Enter' && !e.shiftKey) {
                e.preventDefault();
                sendMessage();
              }
            }}
            placeholder="Type your message..."
            className="w-full p-3 border border-gray-700 rounded-lg bg-gray-700 text-gray-100 text-sm resize-none min-h-[2.5rem] max-h-48 overflow-y-auto focus:outline-none focus:border-green-500 transition-colors"
            style={{ height: 'auto' }}
          />
          <div className="flex justify-end items-center gap-2">
            <div className={`text-xs flex items-center gap-1 ${
              isConnected ? 'text-green-500' : 
              isConnecting ? 'text-yellow-500' : 'text-red-400'
            }`}>
              <span className={`w-1.5 h-1.5 rounded-full ${
                isConnected ? 'bg-green-500' : 
                isConnecting ? 'bg-yellow-500' : 'bg-red-400'
              }`}></span>
              <span>{
                isConnected ? 'Connected' : 
                isConnecting ? 'Connecting...' : 'Disconnected'
              }</span>
            </div>
            {isThinking && (
              <div className="flex gap-1">
                <span className="w-1 h-1 bg-green-500 rounded-full animate-bounce"></span>
                <span className="w-1 h-1 bg-green-500 rounded-full animate-bounce" style={{ animationDelay: '0.2s' }}></span>
                <span className="w-1 h-1 bg-green-500 rounded-full animate-bounce" style={{ animationDelay: '0.4s' }}></span>
              </div>
            )}
            <button 
              onClick={sendMessage}
              disabled={!isConnected || isConnecting}
              className="px-4 py-2 bg-green-600 text-white rounded-lg hover:bg-green-700 disabled:opacity-50 disabled:cursor-not-allowed transition-colors font-medium"
            >
              Send
            </button>
          </div>
        </div>
      </div>

      {/* Browser Section */}
      <div className="w-2/3 h-full p-6 bg-gray-900 flex flex-col iframe-section">
        <div className="w-full h-full bg-gray-800 rounded-xl overflow-hidden shadow-2xl border border-gray-700 flex flex-col">
          {/* Browser Title Bar */}
          <div className="h-10 bg-gray-700 flex items-center px-4 border-b border-gray-700">
            <button className="mobile-view-toggle mobile-back-btn hidden mr-3" onClick={() => switchToMobileView('chat')}>
              <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" className="w-5 h-5">
                <path d="M19 12H5"/>
                <path d="M12 19l-7-7 7-7"/>
              </svg>
            </button>
            <div className="flex gap-2">
              <div className="w-3 h-3 rounded-full bg-red-500 hover:opacity-80 cursor-pointer"></div>
              <div className="w-3 h-3 rounded-full bg-yellow-500 hover:opacity-80 cursor-pointer"></div>
              <div className="w-3 h-3 rounded-full bg-green-500 hover:opacity-80 cursor-pointer"></div>
            </div>
            <div className="flex items-end ml-2.5">
              {['liveSiteFrame', 'contentFrame', 'gitFrame'].map((tabId, index) => (
                <div 
                  key={tabId}
                  className={`px-4 py-2 cursor-pointer bg-gray-800 border border-gray-700 border-b-0 rounded-t-md mr-0.5 text-gray-100 transition-opacity relative -bottom-px ${
                    activeTab === tabId ? 'opacity-100 border-b border-gray-800' : 'opacity-70'
                  }`}
                  onClick={() => setActiveTab(tabId)}
                >
                  {index === 0 ? 'Your Rails App' : index === 1 ? 'Agent TODO List' : 'Git Status'}
                </div>
              ))}
            </div>
          </div>

          {/* Browser Address Bar */}
          <div className="p-1.5 bg-gray-700 border-b border-gray-700">
            <div className="h-7 bg-gray-900 border border-gray-900 rounded-md px-3 flex items-center justify-between text-gray-400 text-sm">
              <span>localhost:3000</span>
              <div className="flex gap-1 ml-2">
                <button 
                  className="p-1 rounded hover:bg-gray-700 transition-colors"
                  onClick={() => {
                    const iframe = document.getElementById('liveSiteFrame');
                    if (iframe && iframe.contentWindow) {
                      try {
                        iframe.contentWindow.history.back();
                      } catch (error) {
                        console.log('Cannot access iframe history');
                      }
                    }
                  }}
                >
                  <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2">
                    <path d="M19 12H5"/>
                    <path d="M12 19l-7-7 7-7"/>
                  </svg>
                </button>
                <button 
                  className="p-1 rounded hover:bg-gray-700 transition-colors"
                  onClick={refreshIframes}
                >
                  <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2">
                    <path d="M3 12a9 9 0 0 1 9-9 9.75 9.75 0 0 1 6.74 2.74L21 8"/>
                    <path d="M21 3v5h-5"/>
                    <path d="M21 12a9 9 0 0 1-9 9 9.75 9.75 0 0 1-6.74-2.74L3 16"/>
                    <path d="M3 21v-5h5"/>
                  </svg>
                </button>
              </div>
            </div>
          </div>

          {/* Browser Content */}
          <div className="flex-grow relative bg-white overflow-hidden">
            <iframe 
              src="http://localhost:3000"
              title="Live Site Frame" 
              id="liveSiteFrame" 
              className={`w-full h-full border-0 bg-white ${activeTab === 'liveSiteFrame' ? 'block' : 'hidden'}`}
            />
            <iframe 
              ref={contentFrameRef}
              src="http://localhost:8000/page"
              title="Content Frame" 
              id="contentFrame" 
              className={`w-full h-full border-0 bg-white ${activeTab === 'contentFrame' ? 'block' : 'hidden'}`}
            />
            <iframe 
              src="http://localhost:8000/agent_page/rails_agent"
              title="Git Status Frame" 
              id="gitFrame" 
              className={`w-full h-full border-0 bg-white ${activeTab === 'gitFrame' ? 'block' : 'hidden'}`}
            />
          </div>
        </div>
      </div>
    </div>
  );
}

export default App;