import axios from 'axios';
import type { 
  Conversation, 
  ChatMessageRequest, 
  StreamResponse,
  ConversationSummary,
  UIMessage,
  Message 
} from '@/types/chat';

const API_BASE_URL = import.meta.env.VITE_API_URL || 'http://localhost:8000';

// Create axios instance
const api = axios.create({
  baseURL: API_BASE_URL,
  timeout: 30000,
});

class ApiService {
  // Fetch all conversations/threads
  async getConversations(): Promise<Conversation[]> {
    const response = await api.get<Conversation[]>('/threads');
    return response.data;
  }

  // Fetch specific conversation history
  async getConversationHistory(threadId: string): Promise<Conversation> {
    const response = await api.get<Conversation>(`/chat-history/${threadId}`);
    return response.data;
  }

  // Send message with streaming response
  async sendMessage(
    request: ChatMessageRequest,
    onUpdate: (response: StreamResponse) => void,
    onError: (error: Error) => void
  ): Promise<void> {
    try {
      const response = await fetch(`${API_BASE_URL}/chat-message`, {
        method: 'POST',
        headers: {
          'Content-Type': 'application/json',
        },
        body: JSON.stringify(request),
      });

      if (!response.ok) {
        throw new Error(`HTTP error! status: ${response.status}`);
      }

      const reader = response.body?.getReader();
      if (!reader) {
        throw new Error('No response body reader available');
      }

      const decoder = new TextDecoder();
      let buffer = '';

      while (true) {
        const { done, value } = await reader.read();
        
        if (done) break;

        buffer += decoder.decode(value, { stream: true });
        const lines = buffer.split('\n');
        buffer = lines.pop() || '';

        for (const line of lines) {
          if (line.trim() === '') continue;

          try {
            const data: StreamResponse = JSON.parse(line);
            onUpdate(data);
          } catch (parseError) {
            console.error('Error parsing stream data:', parseError, line);
          }
        }
      }
    } catch (error) {
      onError(error instanceof Error ? error : new Error('Unknown error occurred'));
    }
  }

  // Utility function to convert API messages to UI messages
  convertToUIMessages(messages: Message[]): UIMessage[] {
    return messages.map((msg) => {
      let type: UIMessage['type'];
      let toolName: string | undefined;
      let toolCallId: string | undefined;

      switch (msg.type) {
        case 'human':
          type = 'user';
          break;
        case 'ai':
          type = 'ai';
          break;
        case 'system':
          type = 'system';
          break;
        case 'tool':
          type = 'tool';
          // Extract tool name from the name field or content
          toolName = msg.name || this.extractToolNameFromContent(msg.content);
          toolCallId = (msg as any).tool_call_id;
          break;
        case 'function':
          type = 'function';
          toolName = msg.name || this.extractToolNameFromContent(msg.content);
          toolCallId = (msg as any).function_call_id;
          break;
        default:
          type = 'ai'; // fallback
      }

      return {
        id: msg.id,
        type,
        content: msg.content,
        timestamp: new Date(), // You might want to extract timestamp from the message if available
        toolName,
        toolCallId,
      };
    });
  }

  // Helper function to extract tool name from content
  private extractToolNameFromContent(content: string): string | undefined {
    // Try to extract tool name from common patterns
    if (content.includes('written to')) {
      if (content.includes('HTML')) return 'write_html';
      if (content.includes('CSS')) return 'write_css';
      if (content.includes('JavaScript')) return 'write_javascript';
    }
    if (content.includes('screenshot') || content.includes('clone')) {
      return 'get_screenshot_and_html_content_using_playwright';
    }
    return undefined;
  }

  // Generate conversation summary from messages
  generateConversationSummary(conversation: Conversation): ConversationSummary {

    //conversation -> {thread_id: 'thread_1749166123102_bs0r18', state: Array(8)}state: (8) [{…}, Array(0), {…}, {…}, '2025-06-06T22:56:21.161474+00:00', {…}, Array(0), Array(0)]0: messages: (6) [{…}, {…}, {…}, {…}, {…}, {…}]0: {content: "What's my name?", additional_kwargs: {…}, response_metadata: {…}, type: 'human', name: null, …}1: {content: 'I’m not sure what your name is—could you please tell me?', additional_kwargs: {…}, response_metadata: {…}, type: 'ai', name: null, …}2: {content: 'Can you hear me?', additional_kwargs: {…}, response_metadata: {…}, type: 'human', name: null, …}3: {content: 'I can “hear” you in the sense that I see and process your messages here. How can I help?', additional_kwargs: {…}, response_metadata: {…}, type: 'ai', name: null, …}4: {content: 'Give me an epic and long poem in shakespeare fashion', additional_kwargs: {…}, response_metadata: {…}, type: 'human', name: null, …}5: {content: 'Hearken, O gentle souls, and lend thine ear  \nTo a…flame,  \nThat noble hearts enshrine undying name.', additional_kwargs: {…}, response_metadata: {…}, type: 'ai', name: null, …}length: 6[[Prototype]]: Array(0)[[Prototype]]: Object1: []2: {configurable: {…}}3: {step: 7, source: 'loop', writes: {…}, parents: {…}, thread_id: 'thread_1749166123102_bs0r18'}parents: {}source: "loop"step: 7thread_id: "thread_1749166123102_bs0r18"writes: {software_developer_assistant: {…}}[[Prototype]]: Object4: "2025-06-06T22:56:21.161474+00:00"5: {configurable: {…}}6: []7: []length: 8[[Prototype]]: Array(0)thread_id: "thread_1749166123102_bs0r18"[[Prototype]]: Object
    //conversation.state -> this is a langgraph state object from graph.get_state(). 
    const conversation_state:any = conversation.state; //this is a StateSnapshot object: https://github.com/langchain-ai/langgraph/blob/main/libs/langgraph/langgraph/types.py#L211
    const timestamp = conversation_state.created_at; //lame way to do this, but it's the only way I can think of to get the timestamp
    const messages = conversation_state.values.messages || [];
    const firstUserMessage = messages.find((msg:any) => msg.type === 'human');
    const lastMessage = messages[messages.length - 1];
    
    let title = 'New Conversation';
    if (firstUserMessage?.content) {
      title = firstUserMessage.content.substring(0, 50);
      if (firstUserMessage.content.length > 50) {
        title += '...';
      }
    }

    let preview = 'No messages yet...';
    if (lastMessage?.content) {
      preview = lastMessage.content.substring(0, 100);
      if (lastMessage.content.length > 100) {
        preview += '...';
      }
    }

    return {
      id: conversation.thread_id,
      title,
      preview,
      lastMessage: lastMessage?.content,
      timestamp: new Date(timestamp),
      messageCount: messages.length,
    };
  }

  // Generate unique thread ID
  generateThreadId(): string {
    const timestamp = Date.now();
    const randomComponent = Math.random().toString(36).substring(2, 8);
    return `thread_${timestamp}_${randomComponent}`;
  }

  // Fetch available agents
  async getAvailableAgents(): Promise<string[]> {
    const response = await api.get<{ agents: string[] }>('/available-agents');
    return response.data.agents;
  }
}

export const apiService = new ApiService();
export default apiService; 