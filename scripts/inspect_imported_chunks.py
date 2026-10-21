
import json
from pathlib import Path

root = Path(__file__).resolve().parents[1]
base = root / "data/processed/knowledge/v3_2_trial"

target_doc = "IMPORT_E6682B7314C1ADEBD029CB8C"
pages = {16}

print("=== 新增论文信息 ===")
with (base / "documents.jsonl").open(
    encoding="utf-8"
) as f:
    for line in f:
        obj = json.loads(line)
        if obj.get("document_id") == target_doc:
            print("标题:", obj.get("title"))
            print("来源:", obj.get("source"))
            break

print("\n=== PDF Chunk 内容抽查 ===")

counts = {page: 0 for page in pages}

with (base / "retrieval_chunks.jsonl").open(
    encoding="utf-8"
) as f:
    for line in f:
        obj = json.loads(line)

        if obj.get("document_id") != target_doc:
            continue

        cid = obj.get("chunk_id", "")
        for page in sorted(pages):
            if f":s{page:04d}:" not in cid:
                continue
            if counts[page] >= 2:
                continue

            counts[page] += 1
            print(f"\n--- PDF 第 {page} 页 ---")
            print("Chunk ID:", cid)
            print("估算 Token:",
                  obj.get("token_count_estimate"))
            print("内容:")
            print(obj.get("text", "")[:900])

print("\n抽查数量:", counts)
