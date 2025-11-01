import pytest
from dotenv import load_dotenv
from langsmith import Client
from app.main import get_langgraph_app_and_state_helper

load_dotenv()

client = Client()
DATASET_NAME = "Asahi-Evals-Leonardo"

dataset = client.read_dataset(dataset_name=DATASET_NAME)
if not dataset:
    raise RuntimeError(f"❌ Dataset '{DATASET_NAME}' が見つかりませんでした")

examples = list(client.list_examples(dataset_id=dataset.id))
if not examples:
    raise RuntimeError("❌ Example が存在しません")

print(f"✅ Dataset '{DATASET_NAME}' loaded with {len(examples)} examples")


@pytest.mark.parametrize("example", examples)
def test_edit_file_error(example):
    graph, state = get_langgraph_app_and_state_helper(example.inputs)
    result = None
    try:
        stream = graph.stream(
            state, config={"configurable": {"thread_id": "regression"}}
        )
        for chunk in stream:
            result = chunk
    except Exception as e:
        result = {"error": str(e)}

    assert "Error: String not found in file" not in str(result)
