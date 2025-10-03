import pytest
from dotenv import load_dotenv
from langsmith import Client, testing as t
from app.agents.rails_agent.nodes import build_workflow
from langgraph.checkpoint.memory import MemorySaver
from langchain_core.messages import HumanMessage

load_dotenv()

client = Client()
# DATASET_NAME = "Asahi-Evals-Leonardo"
DATASET_NAME = "Hello-World-Leonardo-Basic-Evals"

dataset = client.read_dataset(dataset_name=DATASET_NAME)
if not dataset:
    raise RuntimeError(f"❌ Dataset '{DATASET_NAME}' Not found")

examples = list(client.list_examples(dataset_id=dataset.id))
if not examples:
    raise RuntimeError("❌ Example dosen't exsist")

print(f"✅ Dataset '{DATASET_NAME}' loaded with {len(examples)} examples")

# @pytest.mark.langsmith
# @pytest.mark.parametrize("example", examples)
# def test_edit_file_error(example):
#     print(example.inputs)
#     pass

@pytest.mark.langsmith
@pytest.mark.parametrize("example", examples)
def test_llm_produces_output(example):
    """Simplest eval: does the agent return a non-empty string?"""
    workflow = build_workflow(checkpointer=MemorySaver())
    inputs = example.inputs

    # Call the workflow with the dataset input
    result = workflow.invoke(
        {"messages": [inputs["messages"][-1]]},
        config={"configurable": {"thread_id": str(example.id)}, "recursion_limit": 100},
    )

    # Assert that we got some kind of output
    assert result is not None, "❌ Model returned None"
    assert isinstance(result, dict), "❌ Unexpected return type"
    assert "messages" in result, "❌ No messages key in result"
    assert any(m.content.strip() for m in result["messages"]), "❌ Empty response"

# @pytest.mark.langsmith
# @pytest.mark.parametrize("example", examples)
# def test_edit_file_error(example):
#     t.log_inputs(example.inputs)

#     example.inputs.setdefault("agent_name", "rails_agent")

#     checkpointer = MemorySaver()
#     graph = build_workflow(checkpointer)
#     if "input" in example.inputs:
#         message = example.inputs["input"]
#     elif "messages" in example.inputs:
#         last_message = example.inputs["messages"][-1]
#         if hasattr(last_message, 'content'):
#             message = last_message.content
#         else:
#             message = str(last_message)
#     else:
#         message = ""
#     state = {"messages": [HumanMessage(content=message)]}


#     tool_calls = []         # [{'name': 'edit_file', 'args': {...}}, ...]
#     tool_messages = []      # ['Successfully replaced ...', 'Error: ...', ...]
#     errors = []
#     called_edit = False

#     try:
#         stream = graph.stream(
#             state,
#             config={"configurable": {"thread_id": example.inputs.get("thread_id", "regression")}, "recursion_limit": 100},
#             stream_mode=["updates", "messages"],  
#         )

#         for chunk in stream:
#             state_update = None
#             if isinstance(chunk, tuple):
#                 if len(chunk) == 2:
#                     # ('updates' | 'messages', payload)
#                     kind, payload = chunk
#                     if kind == "updates":
#                         state_update = payload
#                 elif len(chunk) == 3:
#                     # (subgraph_name, 'updates' | 'messages', payload)
#                     _, kind, payload = chunk
#                     if kind == "updates":
#                         state_update = payload

#             if not state_update:
#                 continue

#             # updates  {agent_key: {'messages': [...], ...} 
#             for _, agent_data in state_update.items():
#                 if not (isinstance(agent_data, dict) and "messages" in agent_data):
#                     continue
#                 messages = agent_data["messages"] or []
#                 if not messages:
#                     continue

#                 last_msg = messages[-1]
#                 addkw = getattr(last_msg, "additional_kwargs", None)
#                 if addkw and isinstance(addkw, dict):
#                     tcalls = addkw.get("tool_calls") or []
#                     for tc in tcalls:
#                         name = (tc.get("name")
#                                 or (tc.get("function") or {}).get("name"))
#                         args = (tc.get("args")
#                                 or (tc.get("function") or {}).get("arguments"))
#                         # --- Path Fixing ---
#                         if isinstance(args, dict) and "file_path" in args:
#                             fpath = args["file_path"]
#                             if fpath.startswith("app/views/"):
#                                 args["file_path"] = fpath.replace("app/views/", "views/", 1)
#                         # ---
#                         tool_calls.append({"name": name, "args": args})
#                         if name == "edit_file":
#                             called_edit = True

#                 for m in messages:
#                     if getattr(m, "type", None) == "tool":
#                         content = getattr(m, "content", "")
#                         tool_messages.append(content)
#                         print("TOOL MESSAGE:", content)

#                         if isinstance(content, str):
#                             if (
#                                 "Error:" in content
#                                 or "String not found" in content
#                                 or "appears " in content
#                             ):
#                                 errors.append(content)

#     except Exception as e:
#         errors.append(f"EXCEPTION: {e}")
#         t.log_outputs({"tool_calls": tool_calls, "tool_messages": tool_messages, "errors": errors})
#         raise
#     finally:
#         t.log_outputs({"tool_calls": tool_calls, "tool_messages": tool_messages, "errors": errors})

#     # 検証:
#     # 1) 今回の目的は「実プロンプト/LLM の挙動を再現し、edit_file が呼ばれた場合に
#     #    よくある失敗（String not found / 複数ヒット警告）が出ていないこと」を確認する
#     if called_edit:
#         bad_paths = [
#             tc for tc in tool_calls
#             if tc["name"] == "edit_file"
#             and isinstance(tc.get("args"), dict)
#             and str(tc["args"].get("file_path", "")).startswith("/")
#         ]
#         assert not bad_paths, f"edit_file.file_path must be project-relative, got: {bad_paths}"

#         not_found = any("String not found in file" in msg for msg in tool_messages)
#         multi_hits = any("appears " in msg and "times" in msg for msg in tool_messages)
#         generic_error = any(msg.startswith("Error:") for msg in tool_messages)
#         assert not not_found, f"edit_file failed with 'String not found'. steps={tool_calls}, tool_messages={tool_messages}"
#         assert not multi_hits, f"edit_file failed due to multiple occurrences and replace_all=False. steps={tool_calls}, tool_messages={tool_messages}"
#         assert not generic_error, f"edit_file returned an error. steps={tool_calls}, tool_messages={tool_messages}"
#     else:
#         assert not errors, f"Unexpected errors without edit_file call: {errors}"
