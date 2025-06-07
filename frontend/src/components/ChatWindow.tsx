import React from 'react';
import { Send, User, Loader2, Menu, ChevronDown, Settings, Wrench, ChevronUp } from 'lucide-react';
import { useChatStore } from '@/stores/chatStore';
import type { UIMessage } from '@/types/chat';

interface MessageProps {
  message: UIMessage;
  nextMessage?: UIMessage;
  toolMessages?: UIMessage[];
}

const Message: React.FC<MessageProps> = ({ message, nextMessage, toolMessages = [] }) => {
  const isUser = message.type === 'user';
  const isSystem = message.type === 'system';
  const isTool = message.type === 'tool';
  const isFunction = message.type === 'function';
  const isAI = message.type === 'ai';
  
  // State for tracking expanded tool messages
  const [expandedTools, setExpandedTools] = React.useState<Set<string>>(new Set());

  // Don't render tool/function messages separately - they'll be shown as part of AI messages
  if (isTool || isFunction) {
    return null;
  }

  // Format tool name for display
  const formatToolName = (toolName?: string): string => {
    if (!toolName) return 'Tool';
    return toolName
      .replace(/_/g, ' ')
      .replace(/\b\w/g, l => l.toUpperCase());
  };

  // Format tool content for detailed view
  const formatToolContent = (toolMsg: UIMessage): string => {
    let content = toolMsg.content;
    
    // Clean up common patterns
    if (content.includes('written to')) {
      return content;
    }
    
    // Handle long outputs by truncating
    if (content.length > 500) {
      return content.substring(0, 500) + '...';
    }
    
    return content;
  };

  // Toggle tool expansion
  const toggleToolExpansion = (toolId: string) => {
    const newExpanded = new Set(expandedTools);
    if (newExpanded.has(toolId)) {
      newExpanded.delete(toolId);
    } else {
      newExpanded.add(toolId);
    }
    setExpandedTools(newExpanded);
  };

  // Get appropriate icon for message type
  const getMessageIcon = () => {
    if (isUser) return <User className="w-4 h-4 text-dark-text" />;
    if (isSystem) return <Settings className="w-4 h-4 text-blue-400" />;
    // AI message
    return (
      <img 
        src="https://service-jobs-images.s3.us-east-2.amazonaws.com/7rl98t1weu387r43il97h6ipk1l7" 
        alt="LlamaBot" 
        className="w-8 h-8 rounded-full"
      />
    );
  };

  // Get appropriate styling for different message types
  const getMessageStyles = () => {
    if (isUser) {
      return 'bg-dark-input text-dark-text rounded-br-sm';
    }
    if (isSystem) {
      return 'bg-blue-500/10 text-blue-300 rounded-bl-sm border border-blue-500/20';
    }
    // AI message
    return 'bg-dark-chat text-dark-text rounded-bl-sm border border-dark-border';
  };

  // Get container alignment
  const getContainerAlignment = () => {
    if (isUser) return 'justify-end';
    return 'justify-start';
  };

  return (
    <div className={`flex gap-3 mb-4 ${getContainerAlignment()}`}>
      {!isUser && (
        <div className="w-8 h-8 rounded-full bg-dark-accent flex items-center justify-center flex-shrink-0">
          {getMessageIcon()}
        </div>
      )}
      
      <div className={`max-w-[70%] ${getMessageStyles()}`}>
        <div className="p-3 rounded-lg">
          {/* Message type indicator for system messages */}
          {isSystem && (
            <div className="text-xs font-medium mb-2 flex items-center gap-2 text-blue-400">
              <Settings className="w-3 h-3" />
              System
            </div>
          )}
          
          <div className="whitespace-pre-wrap break-words">
            {message.content}
          </div>
          
          {message.timestamp && (
            <div className="text-xs text-dark-text/40 mt-2">
              {message.timestamp.toLocaleTimeString()}
            </div>
          )}
        </div>

        {/* Tool Messages - shown as subtle badges below AI messages */}
        {isAI && toolMessages.length > 0 && (
          <div className="px-3 pb-2 animate-in fade-in duration-300">
            <div className="mt-2 pt-1 border-t border-dark-border/10">
              {/* Collapsed badges in a flex row */}
              <div className="flex flex-wrap gap-1.5 mb-2">
                {toolMessages.map((toolMsg, index) => {
                  const isSuccess = toolMsg.content.includes('written to') || toolMsg.content.includes('successful');
                  const isError = toolMsg.content.includes('Error') || toolMsg.content.includes('error') || toolMsg.content.includes('failed');
                  const isExpanded = expandedTools.has(toolMsg.id);
                  
                  // Only show badge if not expanded
                  if (isExpanded) return null;
                  
                  return (
                    <button
                      key={toolMsg.id}
                      onClick={() => toggleToolExpansion(toolMsg.id)}
                      className={`inline-flex items-center gap-1 px-2.5 py-1 text-xs rounded-full border transition-all duration-200 hover:scale-105 hover:shadow-sm cursor-pointer ${
                        isError 
                          ? 'bg-red-500/10 text-red-400 border-red-500/20 hover:bg-red-500/15'
                          : isSuccess
                          ? 'bg-green-500/10 text-green-400 border-green-500/20 hover:bg-green-500/15'
                          : 'bg-dark-border/20 text-dark-text/60 border-dark-border/30 hover:bg-dark-border/30'
                      }`}
                      title={`Click to expand ${formatToolName(toolMsg.toolName)} details`}
                      style={{ 
                        animationDelay: `${index * 100}ms`,
                      }}
                    >
                      <Wrench className="w-3 h-3 opacity-80" />
                      <span className="truncate max-w-[100px] font-medium">
                        {formatToolName(toolMsg.toolName)}
                      </span>
                      {isSuccess && <span className="text-green-400 font-bold ml-0.5">✓</span>}
                      {isError && <span className="text-red-400 font-bold ml-0.5">✗</span>}
                      <ChevronDown className="w-3 h-3 ml-1" />
                    </button>
                  );
                })}
              </div>

              {/* Expanded tool details - each gets full width */}
              <div className="space-y-2">
                {toolMessages.map((toolMsg, index) => {
                  const isSuccess = toolMsg.content.includes('written to') || toolMsg.content.includes('successful');
                  const isError = toolMsg.content.includes('Error') || toolMsg.content.includes('error') || toolMsg.content.includes('failed');
                  const isExpanded = expandedTools.has(toolMsg.id);
                  
                  // Only show expanded details
                  if (!isExpanded) return null;
                  
                  return (
                    <div key={`expanded-${toolMsg.id}`} className="animate-in slide-in-from-top-1 duration-200">
                      {/* Expanded header with collapse button */}
                      <div className="flex items-center justify-between mb-2">
                        <div className="flex items-center gap-2">
                          <Wrench className="w-4 h-4 text-dark-accent" />
                          <span className="font-medium text-sm text-dark-text">
                            {formatToolName(toolMsg.toolName)}
                          </span>
                          {isSuccess && <span className="text-green-400 font-bold">✓</span>}
                          {isError && <span className="text-red-400 font-bold">✗</span>}
                        </div>
                        <button
                          onClick={() => toggleToolExpansion(toolMsg.id)}
                          className="flex items-center gap-1 px-2 py-1 text-xs text-dark-text/60 hover:text-dark-text transition-colors"
                          title="Collapse details"
                        >
                          <ChevronUp className="w-3 h-3" />
                          <span>Collapse</span>
                        </button>
                      </div>
                      
                      <div className="p-3 bg-dark-sidebar/30 rounded-md border border-dark-border/20">
                        <div className="text-sm">
                          <div className="mb-2 flex items-center justify-between">
                            <span className="text-xs uppercase tracking-wide text-dark-text/60 font-medium">
                              Output:
                            </span>
                            {toolMsg.toolCallId && (
                              <span className="text-xs text-dark-text/40 font-mono">
                                ID: {toolMsg.toolCallId.slice(-8)}
                              </span>
                            )}
                          </div>
                          <div className={`p-3 rounded text-xs font-mono whitespace-pre-wrap ${
                            isError 
                              ? 'bg-red-500/5 border border-red-500/10 text-red-300'
                              : isSuccess
                              ? 'bg-green-500/5 border border-green-500/10 text-green-300'
                              : 'bg-dark-input border border-dark-border/30 text-dark-text/80'
                          }`}>
                            {formatToolContent(toolMsg)}
                          </div>
                          
                          {toolMsg.timestamp && (
                            <div className="mt-2 text-xs text-dark-text/40">
                              Executed at {toolMsg.timestamp.toLocaleTimeString()}
                            </div>
                          )}
                        </div>
                      </div>
                    </div>
                  );
                })}
              </div>
            </div>
          </div>
        )}
      </div>

      {isUser && (
        <div className="w-8 h-8 rounded-full bg-dark-input flex items-center justify-center flex-shrink-0">
          <User className="w-4 h-4 text-dark-text" />
        </div>
      )}
    </div>
  );
};

interface MessageInputProps {
  onSendMessage: (message: string) => void;
  disabled?: boolean;
  isStreaming?: boolean;
}

const MessageInput: React.FC<MessageInputProps> = ({ 
  onSendMessage, 
  disabled = false,
  isStreaming = false 
}) => {
  const [input, setInput] = React.useState('');

  const handleSubmit = (e: React.FormEvent) => {
    e.preventDefault();
    const trimmedInput = input.trim();
    if (trimmedInput && !disabled) {
      onSendMessage(trimmedInput);
      setInput('');
    }
  };

  const handleKeyDown = (e: React.KeyboardEvent<HTMLTextAreaElement>) => {
    if (e.key === 'Enter' && !e.shiftKey) {
      e.preventDefault();
      handleSubmit(e);
    }
  };

  return (
    <div className="p-4 bg-dark-chat border-t border-dark-border">
      <form onSubmit={handleSubmit} className="flex flex-col gap-3">
        <textarea
          value={input}
          onChange={(e) => setInput(e.target.value)}
          onKeyDown={handleKeyDown}
          placeholder="Type your message..."
          disabled={disabled}
          className="w-full p-3 border border-dark-border rounded-lg bg-dark-input text-dark-text 
                   placeholder-dark-text/50 focus:outline-none focus:border-dark-accent 
                   resize-none min-h-[2.5rem] max-h-48"
          rows={1}
          style={{
            height: 'auto',
            minHeight: '2.5rem',
          }}
        />
        <div className="flex justify-end">
          <button
            type="submit"
            disabled={!input.trim() || disabled}
            className="flex items-center gap-2 px-4 py-2 bg-dark-accent text-white font-medium 
                     rounded-lg hover:bg-dark-accent/80 disabled:opacity-50 disabled:cursor-not-allowed 
                     transition-colors"
          >
            {isStreaming ? (
              <Loader2 className="w-4 h-4 animate-spin" />
            ) : (
              <Send className="w-4 h-4" />
            )}
            Send
          </button>
        </div>
      </form>
    </div>
  );
};

// Helper function to format agent names for display
const formatAgentName = (agentName: string): string => {
  return agentName
    .split('_')
    .map(word => word.charAt(0).toUpperCase() + word.slice(1))
    .join(' ');
};

export const ChatWindow: React.FC = () => {
  const { 
    currentMessages, 
    currentConversationId,
    isStreaming,
    error,
    isSidebarVisible,
    selectedAgent,
    availableAgents,
    isLoadingAgents,
    sendMessage,
    clearError,
    toggleSidebar,
    setSelectedAgent,
    loadAvailableAgents
  } = useChatStore();

  const messagesEndRef = React.useRef<HTMLDivElement>(null);
  const dropdownRef = React.useRef<HTMLDivElement>(null);
  const [isAgentDropdownOpen, setIsAgentDropdownOpen] = React.useState(false);

  // Group messages with their associated tool calls
  const groupedMessages = React.useMemo(() => {
    const groups: Array<{ message: UIMessage; toolMessages: UIMessage[] }> = [];
    
    for (let i = 0; i < currentMessages.length; i++) {
      const message = currentMessages[i];
      
      if (message.type === 'tool' || message.type === 'function') {
        // Skip tool messages in this loop - they'll be attached to AI messages
        continue;
      }

      const toolMessages: UIMessage[] = [];
      
      // If this is an AI message, look for tool messages that follow it
      if (message.type === 'ai') {
        let j = i + 1;
        while (j < currentMessages.length && 
               (currentMessages[j].type === 'tool' || currentMessages[j].type === 'function')) {
          toolMessages.push(currentMessages[j]);
          j++;
        }
      }
      
      groups.push({ message, toolMessages });
    }
    
    return groups;
  }, [currentMessages]);

  // Auto-scroll to bottom when new messages arrive
  React.useEffect(() => {
    messagesEndRef.current?.scrollIntoView({ behavior: 'smooth' });
  }, [currentMessages]);

  // Load available agents on component mount
  React.useEffect(() => {
    loadAvailableAgents();
  }, [loadAvailableAgents]);

  // Close dropdown when clicking outside
  React.useEffect(() => {
    const handleClickOutside = (event: MouseEvent) => {
      if (dropdownRef.current && !dropdownRef.current.contains(event.target as Node)) {
        setIsAgentDropdownOpen(false);
      }
    };

    document.addEventListener('mousedown', handleClickOutside);
    return () => {
      document.removeEventListener('mousedown', handleClickOutside);
    };
  }, []);

  const handleSendMessage = (message: string) => {
    if (error) clearError();
    sendMessage(message);
  };

  const handleAgentSelect = (agent: string) => {
    setSelectedAgent(agent);
    setIsAgentDropdownOpen(false);
  };

  if (!currentConversationId) {
    return (
      <div className="flex-1 h-full flex flex-col bg-dark-chat">
        {/* Header - always show even when no conversation */}
        <div className="h-15 px-4 py-3 bg-dark-chat border-b border-dark-border flex items-center gap-3">
          {!isSidebarVisible && (
            <button
              onClick={toggleSidebar}
              className="p-2 text-dark-text hover:bg-dark-border/30 rounded transition-colors"
              title="Show sidebar"
            >
              <Menu className="w-5 h-5" />
            </button>
          )}
          
          <img 
            src="https://service-jobs-images.s3.us-east-2.amazonaws.com/7rl98t1weu387r43il97h6ipk1l7" 
            alt="LlamaBot" 
            className="w-8 h-8 rounded-full"
          />
          <h1 className="text-xl font-semibold text-dark-text">LlamaBot</h1>
          
          <div className="ml-auto flex items-center gap-4">
            {/* Agent Selector */}
            <div className="relative" ref={dropdownRef}>
              <button
                onClick={() => setIsAgentDropdownOpen(!isAgentDropdownOpen)}
                className="flex items-center gap-2 px-3 py-2 bg-dark-input border border-dark-border rounded-md text-dark-text hover:border-dark-accent transition-colors"
                disabled={isLoadingAgents}
              >
                <span className="text-sm">
                  {isLoadingAgents ? 'Loading...' : (selectedAgent ? formatAgentName(selectedAgent) : 'Select Agent')}
                </span>
                <ChevronDown className="w-4 h-4" />
              </button>
              
              {isAgentDropdownOpen && !isLoadingAgents && (
                <div className="absolute right-0 top-full mt-1 w-64 bg-dark-input border border-dark-border rounded-md shadow-lg z-50">
                  {availableAgents.length === 0 ? (
                    <div className="px-3 py-2 text-sm text-dark-text/60">
                      No agents available
                    </div>
                  ) : (
                    availableAgents.map((agent) => (
                      <button
                        key={agent}
                        onClick={() => handleAgentSelect(agent)}
                        className={`w-full text-left px-3 py-2 text-sm hover:bg-dark-border/30 transition-colors first:rounded-t-md last:rounded-b-md ${
                          selectedAgent === agent ? 'bg-dark-accent text-white' : 'text-dark-text'
                        }`}
                      >
                        {formatAgentName(agent)}
                      </button>
                    ))
                  )}
                </div>
              )}
            </div>
          </div>
        </div>
        
        <div className="flex-1 flex items-center justify-center text-center text-dark-text/60">
          <div>
            <img 
              src="https://service-jobs-images.s3.us-east-2.amazonaws.com/7rl98t1weu387r43il97h6ipk1l7" 
              alt="LlamaBot" 
              className="w-16 h-16 mx-auto mb-4 rounded-full opacity-30"
            />
            <h3 className="text-xl font-semibold mb-2">Welcome to LlamaBot</h3>
            <p>Select a conversation or start a new one to get started!</p>
          </div>
        </div>
      </div>
    );
  }

  return (
    <div className="flex-1 h-full flex flex-col bg-dark-chat">
      {/* Header */}
      <div className="h-15 px-4 py-3 bg-dark-chat border-b border-dark-border flex items-center gap-3">
        {!isSidebarVisible && (
          <button
            onClick={toggleSidebar}
            className="p-2 text-dark-text hover:bg-dark-border/30 rounded transition-colors"
            title="Show sidebar"
          >
            <Menu className="w-5 h-5" />
          </button>
        )}
        
        <img 
          src="https://service-jobs-images.s3.us-east-2.amazonaws.com/7rl98t1weu387r43il97h6ipk1l7" 
          alt="LlamaBot" 
          className="w-8 h-8 rounded-full"
        />
        <h1 className="text-xl font-semibold text-dark-text">LlamaBot</h1>
        {selectedAgent && (
          <span className="text-sm text-dark-text/60 px-2 py-1 bg-dark-input rounded-md">
            {formatAgentName(selectedAgent)}
          </span>
        )}
        {isStreaming && (
          <div className="text-sm text-dark-accent flex items-center gap-2">
            <Loader2 className="w-4 h-4 animate-spin" />
            Thinking...
          </div>
        )}
        
        <div className="ml-auto flex items-center gap-4">
          {/* Agent Selector */}
          <div className="relative" ref={dropdownRef}>
            <button
              onClick={() => setIsAgentDropdownOpen(!isAgentDropdownOpen)}
              className="flex items-center gap-2 px-3 py-2 bg-dark-input border border-dark-border rounded-md text-dark-text hover:border-dark-accent transition-colors"
              disabled={isLoadingAgents}
            >
              <span className="text-sm">
                {isLoadingAgents ? 'Loading...' : (selectedAgent ? formatAgentName(selectedAgent) : 'Select Agent')}
              </span>
              <ChevronDown className="w-4 h-4" />
            </button>
            
            {isAgentDropdownOpen && !isLoadingAgents && (
              <div className="absolute right-0 top-full mt-1 w-64 bg-dark-input border border-dark-border rounded-md shadow-lg z-50">
                {availableAgents.length === 0 ? (
                  <div className="px-3 py-2 text-sm text-dark-text/60">
                    No agents available
                  </div>
                ) : (
                  availableAgents.map((agent) => (
                    <button
                      key={agent}
                      onClick={() => handleAgentSelect(agent)}
                      className={`w-full text-left px-3 py-2 text-sm hover:bg-dark-border/30 transition-colors first:rounded-t-md last:rounded-b-md ${
                        selectedAgent === agent ? 'bg-dark-accent text-white' : 'text-dark-text'
                      }`}
                    >
                      {formatAgentName(agent)}
                    </button>
                  ))
                )}
              </div>
            )}
          </div>
        </div>
      </div>

      {/* Messages */}
      <div className="flex-1 overflow-y-auto p-4">
        {currentMessages.length === 0 ? (
          <div className="flex items-center justify-center h-full text-dark-text/60">
            <div className="text-center">
              <img 
                src="https://service-jobs-images.s3.us-east-2.amazonaws.com/7rl98t1weu387r43il97h6ipk1l7" 
                alt="LlamaBot" 
                className="w-12 h-12 mx-auto mb-3 rounded-full"
              />
              <p>Start a conversation by sending a message below!</p>
            </div>
          </div>
        ) : (
          groupedMessages.map(({ message, toolMessages }) => (
            <Message 
              key={message.id} 
              message={message} 
              toolMessages={toolMessages}
            />
          ))
        )}
        
        {/* Error display */}
        {error && (
          <div className="bg-red-500/10 border border-red-500/20 text-red-400 p-3 rounded-lg mb-4">
            <p className="font-medium">Error</p>
            <p className="text-sm">{error}</p>
            <button 
              onClick={clearError}
              className="text-xs underline mt-1 hover:no-underline"
            >
              Dismiss
            </button>
          </div>
        )}
        
        <div ref={messagesEndRef} />
      </div>

      {/* Input */}
      <MessageInput 
        onSendMessage={handleSendMessage}
        disabled={isStreaming}
        isStreaming={isStreaming}
      />
    </div>
  );
}; 