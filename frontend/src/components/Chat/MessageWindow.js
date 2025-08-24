import React from 'react';
import MessageHistory from './MessageHistory';
import MessageInput from './MessageInput';

const MessageWindow = ({ 
  messages, 
  isConnected, 
  isConnecting, 
  isThinking, 
  sendMessage 
}) => {
  return (
    <div className="flex flex-col h-full">
      <MessageHistory messages={messages} />
      <MessageInput sendMessage={sendMessage} />
      
      {/* Connection and thinking status indicators */}
      <div className="absolute bottom-[72px] right-4 flex items-center gap-3">
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
      </div>
    </div>
  );
};

export default MessageWindow;