
import json
from pathlib import Path

root = Path(__file__).resolve().parents[1]
base = root / "data/processed/knowledge/v3_1_all"

def get_ids(filename):
    path = base / filename
    ids = set()
    count = 0

    with path.open("r", encoding="utf-8") as f:
        for line in f:
            if not line.strip():
                continue
            obj = json.loads(line)
            count += 1

            doc_id = (
                obj.get("document_id")
                or obj.get("pmcid")
                or obj.get("paper_id")
            )
            if doc_id:
                ids.add(str(doc_id))

    return count, ids

doc_count, doc_ids = get_ids("documents.jsonl")
chunk_count, chunk_ids = get_ids("retrieval_chunks.jsonl")

print("论文元数据记录数:", doc_count)
print("不同论文 ID 数:", len(doc_ids))
print("检索 Chunk 数:", chunk_count)
print("Chunk 覆盖论文数:", len(chunk_ids))
print("元数据论文中未被 Chunk 覆盖的数量:",
      len(doc_ids - chunk_ids))
print("Chunk 中没有对应论文元数据的数量:",
      len(chunk_ids - doc_ids))
