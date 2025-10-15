import React, { useState, useEffect } from 'react';

const MessageInput = ({ sendMessage }) => {
  const [inputMessage, setInputMessage] = useState('');
  const [agentMode, setAgentMode] = useState('prototype');

  // Load agent mode from localStorage on mount
  useEffect(() => {
    const savedMode = localStorage.getItem('agentMode');
    if (savedMode) {
      setAgentMode(savedMode);
    }
  }, []);

  // Save agent mode to localStorage when it changes
  const handleAgentModeChange = (e) => {
    const newMode = e.target.value;
    setAgentMode(newMode);
    localStorage.setItem('agentMode', newMode);
  };

  const handleInputChange = (e) => {
    setInputMessage(e.target.value);
    const textarea = e.target;
    textarea.style.height = 'auto';
    textarea.style.height = textarea.scrollHeight + 'px';
  };

  const handleSendMessage = () => {
    if (inputMessage.trim()) {
      sendMessage(inputMessage);
      setInputMessage('');
      // Reset textarea height
      const textarea = document.querySelector('textarea');
      if (textarea) {
        textarea.style.height = 'auto';
      }
    }
  };

  return (
    <div className="p-4 bg-gray-800 border-t border-gray-700 flex flex-col gap-3">
      <textarea
        value={inputMessage}
        onChange={handleInputChange}
        onKeyDown={(e) => {
          if (e.key === 'Enter' && !e.shiftKey) {
            e.preventDefault();
            handleSendMessage();
          }
        }}
        placeholder="Type your message..."
        className="w-full p-3 border border-gray-700 rounded-lg bg-gray-700 text-gray-100 text-sm resize-none min-h-[2.5rem] max-h-48 overflow-y-auto focus:outline-none focus:border-green-500 transition-colors whitespace-pre-wrap break-words"
        style={{ height: 'auto' }}
      />
      <div className="flex justify-between items-center gap-2">
        <div className="flex items-center gap-2">
          {/* Agent mode selector */}
          <div className="relative inline-block">
            <select
              value={agentMode}
              onChange={handleAgentModeChange}
              className="appearance-none bg-gray-700 border border-gray-600 rounded-lg text-gray-100 px-3 py-2 pr-8 text-sm cursor-pointer hover:border-green-500 focus:outline-none focus:border-green-500 transition-colors"
            >
              <option value="prototype">Design Mode</option>
              <option value="engineer">Engineer Mode</option>
            </select>
            <div className="absolute right-3 top-1/2 -translate-y-1/2 pointer-events-none text-gray-400">
              ▾
            </div>
          </div>
        </div>
        <button
          onClick={handleSendMessage}
          className="px-4 py-2 bg-green-600 text-white rounded-lg hover:bg-green-700 disabled:opacity-50 disabled:cursor-not-allowed transition-colors font-medium"
        >
          Send
        </button>
      </div>
    </div>
  );
};

export default MessageInput;
