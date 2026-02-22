# Incident Summary: Over-Aggressive Summarization in Rails Agent

**Date**: 2026-02-14  
**Trace ID**: [019c5d80-663c-7f51-ac78-4489f830a94d](ls://runs/019c5d80-cc02-7290-9631-1a51a38134d8)  
**Status**: Under Investigation  
**Severity**: Medium - Causes 6-8 second delays on every turn and loses conversation context prematurely

---

## Problem Statement

The Rails Agent's `SummarizationMiddleware` is triggering on **every single conversation turn**, even though the token count is well below the configured threshold. This causes:

1. **Performance degradation**: 6-8 second delay per turn for summarization
2. **Unnecessary token consumption**: ~21,000 tokens per summarization call
3. **Premature context loss**: Conversation details are being compressed too early
4. **Compounding effect**: Each turn adds another summarization, creating a cascade

---

## Observed Behavior

### Token Counts from Trace

| Turn | Input Tokens | Cached Tokens | Summarization Triggered? | Expected? |
|------|--------------|---------------|-------------------------|-----------|
| 1 | 12,457 | 0 | ✅ Yes | ❌ No |
| 2 | 47,325 | 41,070 | ✅ Yes | ❌ No |
| 3 | 46,379 | 41,037 | ✅ Yes | ❌ No |
| 4+ | ~47k | ~41k | ✅ Yes | ❌ No |

**Configured threshold**: `trigger=("tokens", 80000)`  
**Actual trigger point**: Appears to trigger at ~12k-50k tokens

### Trace Pattern

Every turn shows this sequence:
```
1. User message / Tool result
2. SummarizationMiddleware.before_model (6-8 seconds, 21k tokens)
3. Model generates response
4. Tool executes
5. [Repeat - summarization triggers again]
```

### Summarization Output Format Issue

The generated summaries use this format:
```markdown
## SESSION INTENT
## SUMMARY
## ARTIFACTS
## NEXT STEPS
```

But the custom `SUMMARIZATION_PROMPT` in `nodes.py` specifies a different format:
```markdown
1. Primary Request and Intent:
2. Key Technical Concepts:
3. Files and Code Sections:
4. Errors and fixes:
...
9. Optional Next Step:
```

**This indicates the custom prompt is being ignored.**

---

## Hypotheses

### Hypothesis 1: Image Tokens Pushing Over Threshold (Most Likely)

**Evidence**:
- User mentioned attaching an image earlier in the conversation
- Images can consume 10k-100k+ tokens depending on resolution
- Turn 1 shows 12,457 tokens (likely includes image)
- Image content persists in message history across all subsequent turns
- Cumulative token count across conversation may exceed 80k by turn 3-5

**Token Math**:
```
Image: ~25,000 tokens (medium-sized)
System prompt: ~4,000 tokens (cached)
5 turns of conversation: ~10,000 tokens each = 50,000 tokens
Total: ~79,000 tokens → Triggers at 80,000!
```

**Why this causes every-turn summarization**:
- Once cumulative tokens exceed 80k, the middleware triggers
- After summarization, the context is compressed but the image remains
- Next turn adds new messages, pushing past 80k again
- Summarization triggers again → infinite loop

### Hypothesis 2: Token Counting Accumulates Session-Wide

**Evidence**:
- `trigger=("tokens", 80000)` might count **cumulative** tokens across entire session
- Not just the current context window size
- After hitting threshold once, every subsequent turn exceeds it

**Why this causes the issue**:
- Session accumulates 80k+ tokens over first few turns
- Summarization compresses context but doesn't reset the accumulator
- Every new turn adds tokens to the accumulator → continuous triggering

### Hypothesis 3: Custom Prompt Not Being Applied

**Evidence**:
- Summarization output format doesn't match `SUMMARIZATION_PROMPT`
- Using default LangChain summarization format instead

**Possible causes**:
1. **Wrong parameter name**: The `prompt` parameter might not exist or be named differently in this LangChain version
2. **Cached state**: Old summarization format cached in PostgresSaver checkpoint
3. **Version mismatch**: Parameter API changed between LangChain versions
4. **Prompt override**: Another middleware or system is overriding the custom prompt

**Impact**:
- Even if we fix the threshold, summaries won't preserve technical details properly
- Critical context like code snippets, file names, and error fixes being lost

---

## Configuration Analysis

### Current Configuration (nodes.py)

```python
SummarizationMiddleware(
    model=summarization_model,  # Gemini 3 Flash
    trigger=("tokens", 80000),  # ← TOO LOW
    keep=("messages", 20),
    trim_tokens_to_summarize=None,  # Good - lets Gemini see everything
    prompt=SUMMARIZATION_PROMPT,  # ← NOT BEING USED
)
```

### Context Window Sizes

| Model | Context Window | Recommended Threshold (80%) |
|-------|----------------|---------------------------|
| Gemini 3 Flash | 1,000,000 | 800,000 |
| Gemini 3 Pro | 2,000,000 | 1,600,000 |
| Claude 4.5 Sonnet | 200,000 | 160,000 |
| Claude 4.5 Haiku | 200,000 | 160,000 |
| GPT-5 Codex | 200,000 | 160,000 |
| DeepSeek Chat | 64,000 | 51,200 |

**Current threshold (80k) is only 8% of Gemini's capacity!**

---

## Recommended Fixes

### Fix 1: Increase Threshold (Immediate)

```python
SummarizationMiddleware(
    model=summarization_model,
    trigger=("tokens", 800000),  # ← 10x increase for Gemini
    keep=("messages", 20),
    trim_tokens_to_summarize=None,
    prompt=SUMMARIZATION_PROMPT,
)
```

**Alternative**: Use message count instead
```python
trigger=("messages", 100),  # Trigger after 100 messages instead
```

### Fix 2: Strip Images from Old Messages (Recommended)

Add middleware to remove images from messages older than N turns:

```python
class ImageCleanupMiddleware(AgentMiddleware):
    """Remove images from message history after N turns to reduce token usage."""
    
    def _strip_old_images(self, messages, keep_recent=5):
        """Remove image content from messages older than keep_recent turns."""
        modified_messages = []
        for i, msg in enumerate(messages):
            if i < len(messages) - keep_recent:
                if isinstance(msg.content, list):
                    # Keep only text blocks, remove images
                    msg.content = [
                        block for block in msg.content 
                        if not (isinstance(block, dict) and block.get("type") == "image_url")
                    ]
            modified_messages.append(msg)
        return modified_messages
    
    def wrap_model_call(self, request, handler):
        messages = self._strip_old_images(list(request.messages))
        return handler(request.override(messages=messages))
```

### Fix 3: Debug Custom Prompt Not Being Used

**Step 1**: Check LangChain version
```bash
pip show langchain langchain-core langchain-google-genai
```

**Step 2**: Verify parameter name
```python
from langchain.agents.middleware import SummarizationMiddleware
import inspect
print(inspect.signature(SummarizationMiddleware.__init__))
```

**Step 3**: Try alternative parameter names
```python
# If 'prompt' doesn't work, try:
summarization_prompt=SUMMARIZATION_PROMPT
system_message=SUMMARIZATION_PROMPT
instructions=SUMMARIZATION_PROMPT
```

**Step 4**: Clear checkpointer state and test fresh thread

### Fix 4: Dynamic Thresholds per Model (Advanced)

```python
def get_summarization_threshold(model_name: str) -> int:
    """Get appropriate summarization threshold for the current model."""
    thresholds = {
        "gemini-3-flash": 800_000,
        "gemini-3-pro": 1_600_000,
        "claude-4.5-sonnet": 160_000,
        "claude-4.5-haiku": 160_000,
        "gpt-5-codex": 160_000,
        "deepseek-chat": 51_000,
    }
    return thresholds.get(model_name, 160_000)

# In build_workflow:
model_name = state.get("llm_model", "claude-4.5-haiku")
threshold = get_summarization_threshold(model_name)

SummarizationMiddleware(
    model=summarization_model,
    trigger=("tokens", threshold),
    keep=("messages", 20),
    trim_tokens_to_summarize=None,
    prompt=SUMMARIZATION_PROMPT,
)
```

---

## Testing Plan

### Test 1: Verify Threshold Increase
1. Change `trigger=("tokens", 800000)`
2. Start fresh conversation thread (clear checkpointer)
3. Have 10+ turn conversation with mixed text/code
4. Verify summarization does NOT trigger early
5. Monitor token counts in traces

### Test 2: Test with Image
1. Attach medium-sized image (1024x1024) in turn 1
2. Continue conversation for 20 turns
3. Monitor when summarization triggers
4. Verify image tokens are counted correctly

### Test 3: Verify Custom Prompt
1. Apply threshold fix
2. Trigger summarization (go past threshold)
3. Check summarization output format
4. Verify it matches custom `SUMMARIZATION_PROMPT` structure

### Test 4: Image Cleanup Middleware
1. Implement `ImageCleanupMiddleware`
2. Add to middleware stack
3. Verify images removed after N turns
4. Check token counts drop appropriately

---

## Acceptance Criteria

- [ ] Summarization does NOT trigger before reaching 80% of model's context window
- [ ] Custom `SUMMARIZATION_PROMPT` format is used in summaries
- [ ] Images are cleaned from history after 5-10 turns
- [ ] No performance degradation (6-8 sec delays) on regular turns
- [ ] Summaries preserve technical details (file names, code snippets, errors)
- [ ] Works correctly across all supported models (Gemini, Claude, GPT-5, DeepSeek)

---

## Additional Notes

### Why 80k Was Chosen (Probably)

The original 80k threshold likely came from:
- **GPT-4 context window**: 128k tokens (80k = 62% threshold)
- **Claude 2 context window**: 100k tokens (80k = 80% threshold)

But modern models have **much larger context windows**, so this threshold is now obsolete.

### Performance Impact

Each unnecessary summarization costs:
- **Latency**: 6-8 seconds per turn
- **Tokens**: ~21,000 tokens per summarization
- **User experience**: Noticeable lag on every interaction
- **Cost**: Wasted API calls to Gemini for summarization

Over a 50-turn conversation with current bug:
- **Total delay**: 50 turns × 7 seconds = 350 seconds (5.8 minutes)
- **Wasted tokens**: 50 × 21,000 = 1,050,000 tokens

With fix (summarizing once at turn 40):
- **Total delay**: 1 × 7 seconds = 7 seconds
- **Tokens used**: 1 × 21,000 = 21,000 tokens

**Savings**: 98% reduction in summarization overhead

---

## References

- **Trace**: [019c5d80-663c-7f51-ac78-4489f830a94d](ls://runs/019c5d80-cc02-7290-9631-1a51a38134d8)
- **Session**: [165f830e-839d-486f-aa5f-1a413e51faa2](ls://sessions/165f830e-839d-486f-aa5f-1a413e51faa2)
- **Code**: `app/agents/leonardo/rails_agent/nodes.py` (lines with `SummarizationMiddleware`)
- **Related Files**: 
  - `app/agents/leonardo/rails_agent/middleware.py`
  - `app/agents/leonardo/rails_agent/prompts.py`

---

## Next Steps

1. **Immediate**: Change threshold from 80k → 800k and test
2. **Short-term**: Debug why custom prompt isn't being used
3. **Medium-term**: Implement image cleanup middleware
4. **Long-term**: Consider dynamic thresholds per model