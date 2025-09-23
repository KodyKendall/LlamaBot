import pytest
from dotenv import load_dotenv
from langsmith import testing as t
from langsmith.schemas import Example
from app.main import get_langgraph_app_and_state_helper

load_dotenv()

@pytest.mark.langsmith(dataset="Asahi-Evals-Leonardo")
def test_edit_file_error(example: Example):
    t.log_inputs(example.inputs)

    # 実行
    graph, state = get_langgraph_app_and_state_helper(example.inputs)
    result = None
    try:
        # 実際のLangGraph実行（例：エージェントがedit_fileを呼ぶ）
        stream = graph.stream(
            state, config={"configurable": {"thread_id": "regression"}}
        )
        for chunk in stream:
            result = chunk
    except Exception as e:
        result = {"error": str(e)}

    # 出力を記録
    t.log_outputs({"result": result})

    # エラーが再現していないかチェック
    assert "Error: String not found in file" not in str(result)
