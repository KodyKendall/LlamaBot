import React, { useState, useEffect, useRef, useCallback } from 'react';
import MessageWindow from './components/Chat/MessageWindow';

function App() {
  // State management
  const [socket, setSocket] = useState(null);
  const [currentThreadId, setCurrentThreadId] = useState(null);
  
  const [messages, setMessages] = useState([
    { id: `msg-${Date.now()}-${Math.random()}`, type: 'ai', content: "Hi! I'm Leonardo. What are we building today?" }
  ]);

  const [isConnected, setIsConnected] = useState(false);
  const [isConnecting, setIsConnecting] = useState(true);
  const [isThinking, setIsThinking] = useState(false);
  const [isMenuOpen, setIsMenuOpen] = useState(false);
  const [threads, setThreads] = useState([]);
  const [isLoadingThreads, setIsLoadingThreads] = useState(false);
  const [isRefreshing, setIsRefreshing] = useState(false);
  
  const [activeTab, setActiveTab] = useState('liveSiteFrame');
  const [currentMobileView, setCurrentMobileView] = useState('chat');
  const [viewMode, setViewMode] = useState('desktop'); // 'desktop' or 'mobile'

  // Refs
  const currentAiMessageRef = useRef(null);
  const currentAiMessageBufferRef = useRef('');
  const htmlFragmentBufferRef = useRef('');
  const fullMessageBufferRef = useRef('');
  const htmlChunksStartedStreamingRef = useRef(false);
  const htmlChunksEndedStreamingRef = useRef(false);
  const iframeFlushTimerRef = useRef(null);
  const contentFrameRef = useRef(null);
  const socketRef = useRef(null);

  // Constants
  const IFRAME_REFRESH_MS = 500;

  // Helper functions
  const createStreamingOverlay = useCallback(() => {
    // This would be handled by state in React
    console.log('Creating streaming overlay');
  }, []);

  const removeStreamingOverlay = useCallback(() => {
    // This would be handled by state in React
    console.log('Removing streaming overlay');
  }, []);

  const flushToIframe = useCallback(() => {
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
  }, []);

  const switchToMobileView = useCallback((view) => {
    document.body.classList.remove('mobile-chat-view', 'mobile-iframe-view');
    
    if (view === 'chat') {
      document.body.classList.add('mobile-chat-view');
      setCurrentMobileView('chat');
    } else if (view === 'iframe') {
      document.body.classList.add('mobile-iframe-view');
      setCurrentMobileView('iframe');
    }
  }, []);

  const handleResize = useCallback(() => {
    if (window.innerWidth <= 768) {
      if (!document.body.classList.contains('mobile-chat-view') && 
          !document.body.classList.contains('mobile-iframe-view')) {
        switchToMobileView(currentMobileView);
      }
    } else {
      document.body.classList.remove('mobile-chat-view', 'mobile-iframe-view');
    }
  }, [currentMobileView, switchToMobileView]);

  const initializeMobileView = useCallback(() => {
    if (window.innerWidth <= 768) {
      document.body.classList.add('mobile-chat-view');
      setCurrentMobileView('chat');
    }
  }, []);

  // Handle adding messages from WebSocket responses
  const addMessage = useCallback((content, type, baseMessage = null) => {
    if (type === 'end') {
      setIsThinking(false);
      return;
    }

    // Handle tool messages by updating the corresponding AI message with tool results
    if (type === 'ai' && baseMessage?.tool_calls && baseMessage?.tool_calls.length > 0) {
      const toolCallId = baseMessage?.tool_calls[0]?.id;
      setMessages(prev => [...prev, { ...baseMessage, tool_call_id: toolCallId}]);
    } else if (type === 'tool') {
      const toolCallId = baseMessage?.tool_call_id || baseMessage?.tool_call?.id;
      setMessages(prev => {
        const updatedMessages = [...prev];
        const correspondingAIMessage = updatedMessages.find(msg => msg.tool_call_id === toolCallId);
        if (correspondingAIMessage) {
          correspondingAIMessage.tool_results = [content];
        }
        return updatedMessages;
      });
    } else {
      // For non-tool messages, add them normally
      setMessages(prev => [...prev, baseMessage]);
    }
  }, []);

  const handleWebSocketMessage = useCallback((data) => {
    if (data.type === 'AIMessageChunk') {
      if (data.content) {
        // Handle regular text content
        if (!currentAiMessageRef.current) {
          const newMessageId = `msg-${Date.now()}-${Math.random()}`;
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

        // Handle array content (some LLM models return content as an array)
        if (Array.isArray(data.content)) {
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
      } else if (data.content === '' || data.content === null) {
        // Handle tool call chunks
        if (data.base_message && data.base_message.tool_call_chunks && data.base_message.tool_call_chunks.length > 0 && data.base_message.tool_call_chunks[0]) {
          let tool_call_data = data.base_message.tool_call_chunks[0].args || '';
          
          // Check if this is a regular tool call (not HTML generation)
          if (typeof tool_call_data === 'string' && !tool_call_data.includes('<html')) {
            // Handle regular tool calls
            if (data.base_message.tool_calls && data.base_message.tool_calls.length > 0) {
              addMessage('', 'ai', data.base_message);
            }
          } else {
            // Handle HTML generation tool calls
            htmlFragmentBufferRef.current += tool_call_data;
            fullMessageBufferRef.current += tool_call_data;
          }
          
          let htmlTagIndex = htmlFragmentBufferRef.current.indexOf('<html');
          if (htmlTagIndex !== -1 && !htmlChunksStartedStreamingRef.current) {
            htmlChunksStartedStreamingRef.current = true;
            htmlFragmentBufferRef.current = htmlFragmentBufferRef.current.substring(htmlTagIndex);
            
            if (!currentAiMessageRef.current) {
              const newMessageId = `msg-${Date.now()}-${Math.random()}`;
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
        addMessage(data.content, data.type, data);
      } else {
        addMessage(data.content, data.type, data.base_message);
      }
    } else if (data.type === 'end') {
      setIsThinking(false);
      currentAiMessageRef.current = null;
    } else {
      addMessage(data.content, data.type, data.base_message);
    }
  }, [addMessage, createStreamingOverlay, flushToIframe, removeStreamingOverlay]);

  const initWebSocket = useCallback(() => {
    // Don't create a new connection if one already exists and is open
    if (socketRef.current && socketRef.current.readyState === WebSocket.OPEN) {
      console.log('WebSocket already connected');
      return socketRef.current;
    }

    // Backend is running in Docker on port 8000
    let wsUrl = 'ws://localhost:8000/ws';
    if (window.location.protocol === 'https:') {
      wsUrl = 'wss://localhost:8000/ws';
    }

    console.log('Initializing WebSocket connection to:', wsUrl);
    const ws = new WebSocket(wsUrl);
    socketRef.current = ws;

    ws.onopen = () => {
      console.log('WebSocket connected');
      setIsConnected(true);
      setIsConnecting(false);
      setSocket(ws);
    };

    ws.onclose = (event) => {
      console.log('WebSocket disconnected', event.code, event.reason);
      setIsConnected(false);
      setSocket(null);

      // Only attempt to reconnect if it wasn't a normal closure
      if (event.code !== 1000) {
        console.log('Attempting to reconnect in 3 seconds...');
        setTimeout(() => {
          if (socketRef.current?.readyState !== WebSocket.OPEN) {
            setIsConnecting(true);
            initWebSocket();
          }
        }, 3000);
      }
    };

    ws.onerror = (error) => {
      console.error('WebSocket error:', error);
    };

    ws.onmessage = (event) => {
      try {
        const data = JSON.parse(event.data);
        console.log('Received:', data);
        handleWebSocketMessage(data);
      } catch (error) {
        console.error('Error parsing WebSocket message:', error);
        console.log('Raw message:', event.data);
      }
    };

    return ws;
  }, [handleWebSocketMessage]);

  const fetchThreads = useCallback(async () => {
    try {
      // Use relative URL - proxy will forward to backend on port 8000
      const backendUrl = '/threads';

      console.log('Fetching threads from:', backendUrl);
      const response = await fetch(backendUrl, {
        credentials: 'include', // Include cookies for authentication
        headers: {
          'Accept': 'application/json',
        }
      });

      if (!response.ok) {
        throw new Error(`HTTP error! status: ${response.status}`);
      }

      const contentType = response.headers.get("content-type");
      if (!contentType || !contentType.includes("application/json")) {
        throw new TypeError("Response was not JSON");
      }

      const threadsData = await response.json();
      console.log('Threads fetched:', threadsData);
      console.log('Number of threads:', threadsData.length);

      const sortedThreads = threadsData.sort((a, b) =>
        new Date(b.state[4]) - new Date(a.state[4])
      );

      console.log('Setting threads state with:', sortedThreads);
      setThreads(sortedThreads);
    } catch (error) {
      console.error('Error fetching threads:', error);
      console.error('Error details:', error.message, error.stack);
    }
  }, []);

  const loadThread = useCallback(async (threadId) => {
    try {
      setCurrentThreadId(threadId);

      // Use relative URL - proxy will forward to backend on port 8000
      const backendUrl = `/chat-history/${threadId}`;

      const response = await fetch(backendUrl, {
        credentials: 'include', // Include cookies for authentication
        headers: {
          'Accept': 'application/json',
        }
      });

      if (!response.ok) {
        throw new Error(`HTTP error! status: ${response.status}`);
      }

      const threadData = await response.json();

      // threadData is in format: [{ messages: [...] }, ...]
      // Extract and format messages properly
      const messagesArray = threadData[0]?.messages || [];
      const formattedMessages = messagesArray.map((msg, index) => ({
        id: msg.id || `msg-${Date.now()}-${index}`,
        type: msg.type,
        content: msg.content,
        tool_calls: msg.tool_calls,
        tool_results: msg.tool_results,
        tool_call_id: msg.tool_call_id
      }));

      setMessages(formattedMessages);
      setIsMenuOpen(false);
    } catch (error) {
      console.error('Error loading thread:', error);
      alert('Failed to load conversation. Please check if the backend is running.');
    }
  }, []);

  const sendMessage = useCallback((inputMessage) => {
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
      
      // Add message to UI
      const newMessage = { id: `msg-${Date.now()}-${Math.random()}`, type: 'human', content: inputMessage };
      setMessages(prev => [...prev, newMessage]);
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

      const agentMode = localStorage.getItem('agentMode') || 'prototype';
      const agentName = agentMode === 'prototype' ? 'rails_frontend_starter_agent' : 'rails_agent';

      const messageData = {
        message: inputMessage,
        thread_id: currentThreadId,
        agent_name: agentName,
        agent_mode: agentMode
      };

      socket.send(JSON.stringify(messageData));
    }
  }, [socket, currentThreadId, removeStreamingOverlay]);

  // WebSocket connection - only initialize once
  useEffect(() => {
    initWebSocket();
    fetchThreads();
    initializeMobileView();
    window.addEventListener('resize', handleResize);

    return () => {
      window.removeEventListener('resize', handleResize);
      if (socketRef.current) {
        socketRef.current.close();
        socketRef.current = null;
      }
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []); // Empty dependency array - only run once on mount

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
            <div className="h-[60px] px-4 py-3 pl-[60px] bg-gray-800 border-b border-gray-700 flex items-center justify-between">
              <h3 className="text-xl font-semibold">Conversations</h3>
              <button 
                className="w-8 h-8 flex items-center justify-center rounded hover:bg-gray-700 transition-colors text-gray-300 hover:text-white"
                onClick={() => setIsMenuOpen(false)}
              >
                <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" className="w-5 h-5">
                  <path d="M18 6L6 18M6 6l12 12"/>
                </svg>
              </button>
            </div>
            <div className="flex-1 py-4 overflow-y-auto">
              {(() => {
                console.log('Rendering threads, count:', threads.length);
                console.log('Threads array:', threads);
                return threads.length === 0 ? (
                  <div className="px-5 py-3 text-gray-400">No conversations yet</div>
                ) : (
                  threads.map((thread) => {
                    console.log('Rendering thread:', thread);
                    const { title } = generateConversationSummary(thread.state[0]?.messages || []);
                    console.log('Thread title:', title);
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
                );
              })()}
            </div>
          </div>
        </div>

        <MessageWindow 
          messages={messages}
          isConnected={isConnected}
          isConnecting={isConnecting}
          isThinking={isThinking}
          sendMessage={sendMessage}
        />
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
                {/* View mode toggle */}
                <div className="flex gap-0.5 mr-2 border border-gray-600 rounded p-0.5">
                  <button
                    className={`p-1 rounded transition-colors ${viewMode === 'desktop' ? 'bg-gray-700 text-white' : 'hover:bg-gray-700'}`}
                    onClick={() => setViewMode('desktop')}
                    title="Desktop View"
                  >
                    <svg fill="currentColor" viewBox="0 0 24 24" width="16" height="16">
                      <path d="M21 2H3c-1.1 0-2 .9-2 2v12c0 1.1.9 2 2 2h7v2H8v2h8v-2h-2v-2h7c1.1 0 2-.9 2-2V4c0-1.1-.9-2-2-2zm0 14H3V4h18v12z"/>
                    </svg>
                  </button>
                  <button
                    className={`p-1 rounded transition-colors ${viewMode === 'mobile' ? 'bg-gray-700 text-white' : 'hover:bg-gray-700'}`}
                    onClick={() => setViewMode('mobile')}
                    title="Mobile View"
                  >
                    <svg fill="currentColor" viewBox="0 0 24 24" width="16" height="16">
                      <path d="M17 1.01L7 1c-1.1 0-2 .9-2 2v18c0 1.1.9 2 2 2h10c1.1 0 2-.9 2-2V3c0-1.1-.9-1.99-2-1.99zM17 19H7V5h10v14z"/>
                    </svg>
                  </button>
                </div>
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
          <div className={`flex-grow relative overflow-hidden ${viewMode === 'mobile' ? 'flex items-center justify-center bg-gray-900 p-5' : 'bg-white'}`}>
            <iframe
              src="http://localhost:3000"
              title="Live Site Frame"
              id="liveSiteFrame"
              className={`border-0 bg-white ${activeTab === 'liveSiteFrame' ? 'block' : 'hidden'} ${
                viewMode === 'mobile' ? 'w-[375px] h-[667px] rounded-[20px] shadow-2xl border-[5px] border-gray-800' : 'w-full h-full'
              }`}
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