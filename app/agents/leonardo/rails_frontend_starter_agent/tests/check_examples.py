import os
from langsmith import Client
from dotenv import load_dotenv

# .env から LANGSMITH_API_KEY を読み込む
load_dotenv()

client = Client()

# チェックしたいデータセット名
DATASET_NAME = "Asahi-Evals-Leonardo"

# データセットを取得
dataset = client.read_dataset(dataset_name=DATASET_NAME)

if not dataset:
    print(f"❌ Dataset '{DATASET_NAME}' が見つかりませんでした")
    exit(1)

print(f"✅ Dataset '{DATASET_NAME}' が見つかりました (id={dataset.id})")

# その中のExample一覧を取得
examples = list(client.list_examples(dataset_id=dataset.id))

if not examples:
    print("⚠️ データセット内にExampleがありません")
else:
    print(f"✅ {len(examples)}件のExampleが存在します")
    print("--- 最初の1件の内容 ---")
    first = examples[0]
    print("ID:", first.id)
    print("Inputs:", first.inputs)
    print("Outputs:", first.outputs)
