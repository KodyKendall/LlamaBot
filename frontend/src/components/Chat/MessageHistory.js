import React, { useState, useRef, useEffect, useCallback } from 'react';
import Message from './Message';

const MessageHistory = ({ messages }) => {
    const [isUserAtBottom, setIsUserAtBottom] = useState(true);
    const [showScrollButton, setShowScrollButton] = useState(false);
    const messageHistoryRef = useRef(null);
    const scrollThreshold = 50;

    const checkIfUserAtBottom = useCallback(() => {
        if (!messageHistoryRef.current) return;
        
        const { scrollTop, scrollHeight, clientHeight } = messageHistoryRef.current;
        const isAtBottom = scrollTop + clientHeight >= scrollHeight - scrollThreshold;
        setIsUserAtBottom(isAtBottom);
        setShowScrollButton(!isAtBottom);
    }, [scrollThreshold]);

    const scrollToBottom = useCallback((force = false) => {
        if (!messageHistoryRef.current) return;
        
        if (force || isUserAtBottom) {
            messageHistoryRef.current.scrollTop = messageHistoryRef.current.scrollHeight;
            setIsUserAtBottom(true);
            setShowScrollButton(false);
        }
    }, [isUserAtBottom]);

    // Scroll to bottom when messages change
    useEffect(() => {
        scrollToBottom();
    }, [messages, scrollToBottom]);

    return (
        <div 
            ref={messageHistoryRef}
            className="flex-grow p-4 overflow-y-auto bg-gray-800 flex flex-col gap-4 relative message-history"
            onScroll={checkIfUserAtBottom}
        >
            {messages.map((message) => (
                <Message 
                    key={message.id}
                    message={message}
                />
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
    );
};

export default MessageHistory;