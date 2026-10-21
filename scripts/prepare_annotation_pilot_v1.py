
import csv
import shutil
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
REPORTS = ROOT / "reports"

# 1. 冻结原始实验文件
frozen_dir = REPORTS / "pilot_v1_frozen"
frozen_dir.mkdir(parents=True, exist_ok=True)

files = [
    "bm25_v1_1_review_candidates.csv",
    "dense_v1_review_candidates.csv",
    "retrieval_union_blinded_v1.csv",
    "retrieval_union_provenance_v1.csv",
]

for name in files:
    src = REPORTS / name
    dst = frozen_dir / name
    if not src.exists():
        raise FileNotFoundError(src)
    if dst.exists():
        print(f"已存在，跳过备份: {name}")
    else:
        shutil.copy2(src, dst)

# 2. 读取联合候选
source = REPORTS / "retrieval_union_blinded_v1.csv"

with source.open("r", encoding="utf-8-sig", newline="") as f:
    reader = csv.DictReader(f)
    original_fields = reader.fieldnames or []
    rows = list(reader)

# 3. 添加医学标注字段
extra_fields = [
    "evidence_directness",
    "study_design",
    "population_match",
    "outcome_match",
    "supports_answer_from_paper",
    "reviewer_id",
]

fields = original_fields + [
    name for name in extra_fields
    if name not in original_fields
]

for row in rows:
    for name in extra_fields:
        row.setdefault(name, "")

# 4. 保存完整标注表
output = REPORTS / "retrieval_union_annotation_v1.csv"

if output.exists():
    raise FileExistsError(
        f"{output} 已存在，为保护人工标注数据，拒绝覆盖。"
    )

with output.open("w", encoding="utf-8-sig", newline="") as f:
    writer = csv.DictWriter(f, fieldnames=fields)
    writer.writeheader()
    writer.writerows(rows)

# 5. 每个问题选择 3 条试标注候选
pilot = []
counts = {}

for row in rows:
    qid = row["question_id"]
    if counts.get(qid, 0) < 3:
        pilot.append(row)
        counts[qid] = counts.get(qid, 0) + 1

pilot_path = REPORTS / "pilot_annotation_24.csv"

if pilot_path.exists():
    raise FileExistsError(
        f"{pilot_path} 已存在，为保护人工标注数据，拒绝覆盖。"
    )

with pilot_path.open("w", encoding="utf-8-sig", newline="") as f:
    writer = csv.DictWriter(f, fieldnames=fields)
    writer.writeheader()
    writer.writerows(pilot)

print(f"完整标注候选数: {len(rows)}")
print(f"试标注候选数: {len(pilot)}")
print(f"完整标注表: {output}")
print(f"试标注表: {pilot_path}")
