# AI safety limits on ZDR boxes (LP-33)

For HECVAT AIGN-05, AILM-01, AILM-04 and AILM-06. All on when ZDR is on, off otherwise, with no other switch.

| Limit | Code | Configure |
| --- | --- | --- |
| Sensitive-data filter | `agents/leonardo/sensitive_data.py` | Admin: `/users`, or `PUT /api/sensitive-data-rules` |
| Read-only tools by default | `permissions.py` (`READ_ONLY_TOOLS`, `tool_allowed`) | Admin: `/users`, or `PUT /api/role-write-tools` |
| Tool calls per input | `agents/leonardo/tool_limits.py` | `TOOL_CALL_PER_TURN_LIMIT`, default 40 |
| Eval before approving a model | `agents/leonardo/model_evals.json` | Leonardo `docs/SHIPPING.md` |

## Why the checks are on the LangChain base classes

Agents here are built several ways: `create_agent`, raw graph nodes, sub-agents, clients' own agents. A check in shared middleware would miss some of them. Every chat model and tool goes through `BaseChatModel._convert_input` and `BaseTool.run`/`arun`, so `safety_hooks.py` wraps those. They are LangChain internals: the tests fail if an upgrade moves them.

Middleware only hides tools from the model. The refusal is at the tool.

## Behavior worth knowing

- Only the copy sent to the model changes. Chat history on the box keeps the original.
- `block` fires only on text newer than the last model reply. Otherwise a blocked message in history would stop the thread forever; later turns redact it.
- Logs and the ZDR health report carry rule names and counts, never matched text. Counts reset on restart.
- The default student ID format is the University of Utah's uNID (`u` plus 7 digits). Set another per box.
- Scheduled jobs run as the `scheduled` role: read-only until granted.

## Known gaps

- Images are not scanned. PDFs and other attached files are withheld; their text can go through `read_uploaded_file`, whose result is filtered.
- Admin regexes are not protected against catastrophic backtracking.
- Only LlamaBot's model calls are covered, not any model call made from the Rails app itself.
- Matching is pattern-based, so it can miss other formats and can redact look-alikes such as a test SSN in code.
