import React, { useState } from 'react';

const MessageInput = ({ sendMessage }) => {
  const [inputMessage, setInputMessage] = useState('');

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
        className="w-full p-3 border border-gray-700 rounded-lg bg-gray-700 text-gray-100 text-sm resize-none min-h-[2.5rem] max-h-48 overflow-y-auto focus:outline-none focus:border-green-500 transition-colors"
        style={{ height: 'auto' }}
      />
      <div className="flex justify-end items-center gap-2">
        <div className="flex gap-1">
          {/* Connection status will be moved to App.js */}
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
