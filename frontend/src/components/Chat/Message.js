import React, { useState } from 'react';

// ToolMessage Component
const ToolMessage = ({ message, toolCall, toolResult }) => {
  const [isExpanded, setIsExpanded] = useState(false);
  
  // Add null checks to prevent errors when args is undefined
  const args = toolCall?.args || {};
  const firstArgument = args[Object.keys(args)[0]] || 'No arguments';
  
  // Check for tool result in the toolCall itself (from the updated structure)
  const result = toolCall?.result || toolResult;
  
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
          {result && <span className="text-green-400 ml-2">✓</span>}
        </span>
      </div>
      <div id={toolCall?.id}
        className={`overflow-hidden transition-all duration-300 bg-black/20 rounded mt-1 ${
          isExpanded ? 'max-h-96 p-2' : 'max-h-0 p-0'
        }`}
      >
        <div className="text-white/70 text-sm">
          <strong>Arguments:</strong><br />
          <pre className="mt-1 text-xs whitespace-pre-wrap">
            {JSON.stringify(args, null, 2)}
          </pre>
          {result && (
            <>
              <br /><strong>Result:</strong><br />
              <pre className="mt-1 text-xs whitespace-pre-wrap">
                {result}
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
    
    const shouldAiMessageBeTreatedAsTool = message.type === 'ai' && message?.tool_calls && message?.tool_calls.length > 0;
    if (shouldAiMessageBeTreatedAsTool) { // we're doing this for ai messages where the LLM just called a tool. We want to treat it as a tool message for styling purposes.
        return 'bg-blue-700 self-start rounded-bl-sm';
    }

    switch (message.type) {
      case 'human':
        return 'bg-gray-700 self-end rounded-br-sm';
      case 'ai':
        return 'bg-blue-700 self-start rounded-bl-sm';
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
        if (message?.tool_calls && message?.tool_calls.length > 0) {
            return(
                <div>
                    {message.tool_calls.map((toolCall, index) => (
                        <ToolMessage 
                            key={toolCall.id || index}
                            message={message}
                            toolCall={toolCall}
                            toolResult={message.tool_results?.[index] || null}
                        />
                    ))}
                </div>
            );
        }
        else {
            return <div dangerouslySetInnerHTML={{ __html: parseMarkdown(message.content) }} />;
        }
    } else if (message.type === 'tool') {
        // For tool messages, we need to find the corresponding AI message that contains the tool call
        // and update its toolResult. This should be handled at the parent component level.
        // For now, we'll render the tool result directly
        return (
            <div className="text-white/70 text-sm">
                <strong>Tool Result:</strong><br />
                <pre className="mt-1 text-xs whitespace-pre-wrap">
                    {message.content}
                </pre>
            </div>
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