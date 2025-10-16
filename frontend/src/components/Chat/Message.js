import React, { useState } from 'react';
import { marked } from 'marked';

// Configure marked for better security and formatting
marked.setOptions({
  breaks: true,
  gfm: true,
  sanitize: false,
  smartLists: true,
  smartypants: true
});

// TodoList Component for write_todos tool
const TodoList = ({ todos }) => {
  const [expandedTodos, setExpandedTodos] = useState({});

  // Sort todos: in_progress → pending → completed
  const statusOrder = { 'in_progress': 0, 'pending': 1, 'completed': 2 };
  const sortedTodos = [...todos].sort((a, b) => statusOrder[a.status] - statusOrder[b.status]);

  const toggleTodo = (index) => {
    setExpandedTodos(prev => ({ ...prev, [index]: !prev[index] }));
  };

  let currentStatus = null;

  return (
    <div className="space-y-2">
      {sortedTodos.map((todo, index) => {
        const showHeader = todo.status !== currentStatus;
        currentStatus = todo.status;

        const statusClass = `todo-status-${todo.status.replace('_', '-')}`;
        const icon = todo.status === 'completed' ? '✅' :
                     todo.status === 'in_progress' ? '🎯' : '🕒';
        const sectionTitle = todo.status === 'in_progress' ? 'In Progress' :
                            todo.status === 'pending' ? 'Pending' : 'Completed';

        return (
          <React.Fragment key={index}>
            {showHeader && (
              <div className="text-xs font-semibold uppercase tracking-wide text-white/60 mt-3 mb-1.5 pb-1.5 border-b border-white/10">
                {sectionTitle}
              </div>
            )}
            <div
              className={`flex items-start p-2 px-2.5 my-1 bg-white/8 rounded-md cursor-pointer border-l-3 transition-colors hover:bg-white/12 ${statusClass}`}
              onClick={() => toggleTodo(index)}
            >
              <span className="mr-2.5 text-base min-w-[18px] mt-0.5 font-bold">{icon}</span>
              <span className={`flex-1 text-sm leading-relaxed ${expandedTodos[index] ? '' : 'line-clamp-2'}`}>
                {todo.content}
              </span>
            </div>
          </React.Fragment>
        );
      })}
    </div>
  );
};

// ToolMessage Component
const ToolMessage = ({ message, toolCall, toolResult }) => {
  const [isExpanded, setIsExpanded] = useState(false);

  const args = toolCall?.args || {};
  const firstArgument = args[Object.keys(args)[0]] || '';
  const result = toolCall?.result || toolResult;
  const toolName = toolCall?.name || 'Unknown Tool';

  // Special handling for write_todos
  if (toolName === 'write_todos') {
    const todos = args.todos || [];
    return (
      <div>
        <div
          className="cursor-pointer select-none p-2 rounded bg-blue-700 border border-white/10 mb-1 hover:bg-blue-600/80 transition-colors"
          onClick={() => setIsExpanded(!isExpanded)}
        >
          <span className="font-bold text-white/90">
            <span
              className="inline-block transition-transform duration-200 mr-1 text-xs"
              style={{ transform: isExpanded ? 'rotate(90deg)' : 'rotate(0deg)' }}
            >
              ▶
            </span>
            🎯 Todo List ({todos.length} tasks)
          </span>
        </div>
        <div
          className={`overflow-hidden transition-all duration-300 bg-black/20 rounded mt-1 ${
            isExpanded ? 'max-h-[500px] p-2 overflow-y-auto' : 'max-h-0 p-0'
          }`}
        >
          <TodoList todos={todos} />
        </div>
      </div>
    );
  }

  // Special handling for edit_file
  if (toolName === 'edit_file') {
    const displayName = result?.includes('success') ? '✅ Edit' :
                       result?.includes('error') ? '❌ Edit' : 'Edit';
    return (
      <div>
        <div
          className="cursor-pointer select-none p-2 rounded bg-blue-700 border border-white/10 mb-1 hover:bg-blue-600/80 transition-colors"
          onClick={() => setIsExpanded(!isExpanded)}
        >
          <span className="font-bold text-white/90">
            <span
              className="inline-block transition-transform duration-200 mr-1 text-xs"
              style={{ transform: isExpanded ? 'rotate(90deg)' : 'rotate(0deg)' }}
            >
              ▶
            </span>
            {displayName} {firstArgument}
          </span>
        </div>
        <div
          className={`overflow-hidden transition-all duration-300 bg-black/20 rounded mt-1 ${
            isExpanded ? 'max-h-96 p-2 overflow-y-auto' : 'max-h-0 p-0'
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
  }

  // Default tool message
  return (
    <div>
      <div
        className="cursor-pointer select-none p-2 rounded bg-blue-700 border border-white/10 mb-1 hover:bg-blue-600/80 transition-colors"
        onClick={() => setIsExpanded(!isExpanded)}
      >
        <span className="font-bold text-white/90">
          <span
            className="inline-block transition-transform duration-200 mr-1 text-xs"
            style={{ transform: isExpanded ? 'rotate(90deg)' : 'rotate(0deg)' }}
          >
            ▶
          </span>
          🔨 {toolName} {firstArgument}
          {result && <span className="text-green-400 ml-2">✓</span>}
        </span>
      </div>
      <div
        className={`overflow-hidden transition-all duration-300 bg-black/20 rounded mt-1 ${
          isExpanded ? 'max-h-96 p-2 overflow-y-auto' : 'max-h-0 p-0'
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
      // Handle array content (for some LLM models)
      if (Array.isArray(text)) {
        text = text[0]?.text || '';
      }

      let html = marked.parse(text);

      // Basic XSS prevention
      html = html.replace(/<script\b[^<]*(?:(?!<\/script>)<[^<]*)*<\/script>/gi, '');
      html = html.replace(/\son\w+="[^"]*"/gi, '');
      html = html.replace(/\son\w+='[^']*'/gi, '');

      return html;
    } catch (error) {
      console.error('Markdown parsing error:', error);
      return text.replace(/\n/g, '<br>');
    }
  };

  return (
    <div
      className={`p-3 rounded-xl max-w-[85%] break-words relative text-sm ${getMessageClassName()} ${message.type === 'ai' && !message.tool_calls ? 'message-ai' : ''}`}
    >
      {renderMessageContent()}
    </div>
  );
};

export default Message;