// ToolMessage Component
const AIWithToolMessage = ({ message, toolCall, toolResult }) => {
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