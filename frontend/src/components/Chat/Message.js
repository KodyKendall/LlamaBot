import React, { useState } from 'react';

// ToolMessage Component
const ToolMessage = ({ toolCall, toolResult }) => {
  const [isExpanded, setIsExpanded] = useState(false);
  
  // Add null checks to prevent errors when args is undefined
  const args = toolCall?.args || {};
  const firstArgument = args[Object.keys(args)[0]] || 'No arguments';
  
  return (
    <div>
      <div 
        className="cursor-pointer select-none p-2 rounded bg-blue-700 border border-white/10 mb-1 hover:bg-blue-600/80 transition-colors"
        onClick={() => setIsExpanded(!isExpanded)}
      >
        <span className="font-bold text-white/90">
          <span 
            className="inline-block transition-transform duration-200 mr-1"
            style={{ transform: isExpanded ? 'rotate(90deg)' : 'rotate(0deg)' }}
          >
            ▶
          </span>
          🔨 {toolCall?.name || 'Unknown Tool'} {firstArgument}
        </span>
      </div>
      <div 
        className={`overflow-hidden transition-all duration-300 bg-black/20 rounded mt-1 ${
          isExpanded ? 'max-h-96 p-2' : 'max-h-0 p-0'
        }`}
      >
        <div className="text-white/70 text-sm">
          <strong>Arguments:</strong><br />
          <pre className="mt-1 text-xs whitespace-pre-wrap">
            {JSON.stringify(args, null, 2)}
          </pre>
          {toolResult && (
            <>
              <br /><strong>Result:</strong><br />
              <pre className="mt-1 text-xs whitespace-pre-wrap">
                {toolResult}
              </pre>
            </>
          )}
        </div>
      </div>
    </div>
);
};

// Main Message Component
const Message = ({ message }) => {
  const getMessageClassName = () => {
    switch (message.type) {
      case 'human':
        return 'bg-gray-700 self-end rounded-br-sm';
      case 'ai':
        return 'bg-gray-600 self-start rounded-bl-sm';
      case 'tool':
        return 'bg-blue-700 self-start rounded-bl-sm';
      case 'error':
        return 'bg-red-900/20 border border-red-500/50 text-red-400';
      default:
        return 'bg-gray-600 self-start rounded-bl-sm';
    }
  };

  const renderMessageContent = () => {
    if (message.type === 'ai') {
      return <div dangerouslySetInnerHTML={{ __html: parseMarkdown(message.content) }} />;
    } else if (message.type === 'tool' && message.baseMessage?.tool_calls && message.baseMessage.tool_calls.length > 0) {
      return (
        <ToolMessage 
          toolCall={message.baseMessage.tool_calls[0]}
          toolResult={message.toolResult}
        />
      );
    } else {
      return message.content;
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

  return (
    <div 
      className={`p-3 rounded-xl max-w-[85%] break-words relative text-sm ${getMessageClassName()}`}
    >
      {renderMessageContent()}
    </div>
  );
};

export default Message;
