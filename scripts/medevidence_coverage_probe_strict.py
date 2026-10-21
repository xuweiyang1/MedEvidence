#!/usr/bin/env python3
"""Strict, read-only coverage probe for a MedEvidence BM25 SQLite index.

The old probe used ``lower(body_text) LIKE '%term%'``.  That is useful for a
quick exploration, but it is not a lexical match: ``DAPT`` can occur inside
``adapted`` and ``pylori`` can occur inside ``pyloric``.  This replacement
loads only the text and identifier columns from SQLite and applies explicit,
case-insensitive regular expressions with token boundaries.  It also prints
context centred on the actual match.

This script reports lexical coverage only.  A hit is not a relevance label or
evidence that a paper supports a clinical recommendation.

Examples:
  python scripts/medevidence_coverage_probe_strict.py \
    --db data/processed/index_versions/v3_2_trial_clean/bm25.sqlite3 \
    --case Q15

  python scripts/medevidence_coverage_probe_strict.py \
    --db data/processed/index_versions/v3_2_trial_clean/bm25.sqlite3 \
    --case all --output outputs/coverage_strict.json
"""

from __future__ import annotations

import argparse
import json
import re
import sqlite3
import sys
import unicodedata
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Iterator


CASES: dict[str, dict[str, object]] = {
    "Q03": {
        "label": "脑卒中溶栓适应证/时间窗",
        "anchor": "ischemic stroke",
        "related": ["thrombolysis", "4.5 hour", "alteplase"],
    },
    "Q04": {
        "label": "房颤抗凝预防卒中",
        "anchor": "atrial fibrillation",
        "related": ["anticoagulation", "CHA2DS2"],
    },
    "Q06": {
        "label": "COPD 急性加重激素疗程",
        "anchor": "COPD",
        "related": ["corticosteroid", "prednisone", "5 days"],
    },
    "Q08": {
        "label": "成人门诊社区获得性肺炎抗生素",
        "anchor": "community-acquired pneumonia",
        "related": ["outpatient", "antibiotic"],
    },
    "Q09": {
        "label": "CKD 白蛋白尿 ACEi/ARB",
        "anchor": "albuminuria",
        "related": ["ACE inhibitor", "angiotensin receptor"],
    },
    "Q11": {
        "label": "妊娠糖尿病筛查时点",
        "anchor": "gestational diabetes",
        "related": ["screening", "24–28", "24-28"],
    },
    "Q12": {
        "label": "急性冠脉综合征 DAPT 疗程",
        "anchor": "acute coronary syndrome",
        "related": ["dual antiplatelet", "DAPT"],
    },
    "Q13": {
        "label": "肺栓塞抗凝疗程",
        "anchor": "pulmonary embolism",
        "related": ["anticoagulation", "duration"],
    },
    "Q15": {
        "label": "幽门螺杆菌根除后复查",
        # A token-boundary match prevents pylori from matching pyloric.  The
        # additional expressions are deliberately explicit alternatives and
        # should be added to the case rather than relying on substring search.
        "anchor": "pylori",
        "related": [
            "eradication",
            "test of cure",
            "urea breath",
            "urea breath test",
            "stool antigen",
            "fecal antigen",
            "UBT",
        ],
    },
}


TEXT_COLUMNS = (
    "body_text",
    "text",
    "content",
    "chunk_text",
    "body",
    "document_text",
)
ID_COLUMNS = ("chunk_id", "id", "key", "uuid")
DOC_COLUMNS = ("document_id", "doc_id", "pmcid", "paper_id", "source_id")


@dataclass(frozen=True)
class Row:
    row_id: str
    document_id: str
    text: str


@dataclass(frozen=True)
class Hit:
    row: Row
    start: int
    end: int


def normalize(value: str) -> str:
    """Normalize Unicode compatibility forms and punctuation for matching."""

    value = unicodedata.normalize("NFKC", value)
    value = value.replace("–", "-").replace("—", "-").replace("‑", "-")
    value = re.sub(r"\s+", " ", value)
    return value


def compile_term(term: str) -> re.Pattern[str]:
    """Compile an explicit term into a case-insensitive lexical regex.

    Terms are treated as phrases, with flexible whitespace/hyphen separators.
    A small, explicit list of clinical count nouns accepts a simple plural
    (``hour``/``hours``, ``antibiotic``/``antibiotics`` and similar).  ASCII
    alphanumeric boundaries stop acronym/stem false positives while still
    allowing punctuation immediately around a phrase.
    """

    normalized = normalize(term).strip()
    if not normalized:
        raise ValueError("empty term")
    tokens = re.findall(r"[A-Za-z0-9]+|\s+|[^A-Za-z0-9\s]", normalized)
    pieces: list[str] = []
    simple_plural_words = {
        "hour",
        "day",
        "week",
        "month",
        "year",
        "antibiotic",
        "antiplatelet",
        "corticosteroid",
        "duration",
    }
    last_word_index = max(
        (index for index, token in enumerate(tokens) if re.fullmatch(r"[A-Za-z0-9]+", token)),
        default=-1,
    )
    for index, token in enumerate(tokens):
        if token.isspace():
            # Phrase separators may be spaces or a hyphen variant, e.g.
            # ``acute coronary syndrome`` / ``acute-coronary syndrome``.
            pieces.append(r"(?:\s+|[-‐‑–—]\s*)")
        elif re.fullmatch(r"[A-Za-z0-9]+", token):
            suffix = "s?" if index == last_word_index and token.lower() in simple_plural_words else ""
            pieces.append(re.escape(token) + suffix)
        elif token in {"-", "‐", "‑", "–", "—"}:
            pieces.append(r"\s*[-‐‑–—]\s*")
        else:
            pieces.append(re.escape(token))
    pattern = "".join(pieces)
    # The lookarounds intentionally use ASCII alphanumerics.  This keeps
    # ``DAPT`` out of ``adapted`` and ``pylori`` out of ``pyloric`` without
    # rejecting punctuation such as ``H. pylori``.
    return re.compile(rf"(?<![A-Za-z0-9]){pattern}(?![A-Za-z0-9])", re.IGNORECASE)


def quote_identifier(identifier: str) -> str:
    return '"' + identifier.replace('"', '""') + '"'


def discover_columns(
    connection: sqlite3.Connection,
    table: str | None,
    text_column: str | None,
    id_column: str | None,
    document_column: str | None,
) -> tuple[str, str, str, list[str]]:
    """Discover a compatible table and columns, or raise a useful error."""

    tables = [
        row[0]
        for row in connection.execute(
            "SELECT name FROM sqlite_master WHERE type IN ('table', 'view') "
            "AND name NOT LIKE 'sqlite_%'"
        )
    ]
    if table:
        if table not in tables:
            raise ValueError(f"table not found: {table}")
        tables = [table]

    candidates: list[tuple[int, str, str, str, list[str]]] = []
    for candidate_table in tables:
        info = list(connection.execute(f"PRAGMA table_info({quote_identifier(candidate_table)})"))
        columns = [str(row[1]) for row in info]
        lower = {column.lower(): column for column in columns}
        chosen_text = text_column or next((lower[c] for c in TEXT_COLUMNS if c in lower), None)
        if not chosen_text:
            continue
        chosen_id = id_column or next((lower[c] for c in ID_COLUMNS if c in lower), None)
        chosen_doc = document_column or next((lower[c] for c in DOC_COLUMNS if c in lower), None)
        # A document id is useful for co-occurrence, but rowid is a safe
        # fallback for indexes that only retain chunk ids.
        if not chosen_id:
            chosen_id = "rowid"
        if not chosen_doc:
            chosen_doc = chosen_id
        score = 0
        table_lower = candidate_table.lower()
        if "chunk" in table_lower or "bm25" in table_lower:
            score += 3
        if chosen_doc != chosen_id:
            score += 2
        if chosen_text.lower() == "body_text":
            score += 1
        candidates.append((score, candidate_table, chosen_text, chosen_id, [chosen_doc]))

    if not candidates:
        available = ", ".join(tables) or "<none>"
        raise ValueError(
            "could not find a text table; pass --table/--text-column explicitly "
            f"(available tables: {available})"
        )
    _, chosen_table, chosen_text, chosen_id, doc_holder = max(candidates)
    chosen_doc = document_column or doc_holder[0]
    return chosen_table, chosen_text, chosen_id, [chosen_doc]


def iter_rows(
    connection: sqlite3.Connection,
    table: str,
    text_column: str,
    id_column: str,
    document_column: str,
    batch_size: int = 1000,
) -> Iterator[Row]:
    select_id = "rowid" if id_column == "rowid" else quote_identifier(id_column)
    select_doc = "rowid" if document_column == "rowid" else quote_identifier(document_column)
    sql = (
        f"SELECT {select_id}, {select_doc}, {quote_identifier(text_column)} "
        f"FROM {quote_identifier(table)}"
    )
    cursor = connection.execute(sql)
    while True:
        batch = cursor.fetchmany(batch_size)
        if not batch:
            return
        for row_id, document_id, text in batch:
            if text is None:
                continue
            text_value = str(text)
            yield Row(str(row_id), str(document_id), text_value)


def context(text: str, start: int, end: int, width: int) -> str:
    left = max(0, start - width)
    right = min(len(text), end + width)
    prefix = "…" if left else ""
    suffix = "…" if right < len(text) else ""
    return prefix + text[left:right].replace("\n", " ") + suffix


def hit_examples(hits: Iterable[Hit], max_examples: int, context_chars: int) -> list[dict[str, str]]:
    examples: list[dict[str, str]] = []
    for hit in hits:
        if len(examples) >= max_examples:
            break
        normalized = normalize(hit.row.text)
        examples.append(
            {
                "row_id": hit.row.row_id,
                "document_id": hit.row.document_id,
                "context": context(normalized, hit.start, hit.end, context_chars),
            }
        )
    return examples


def scan_term(rows: Iterable[Row], regex: re.Pattern[str], max_examples: int, context_chars: int) -> tuple[list[Hit], list[dict[str, str]]]:
    hits: list[Hit] = []
    examples: list[dict[str, str]] = []
    for row in rows:
        normalized = normalize(row.text)
        match = regex.search(normalized)
        if not match:
            continue
        hit = Hit(row=row, start=match.start(), end=match.end())
        hits.append(hit)
        if len(examples) < max_examples:
            # Use the normalized text for offsets.  Unicode compatibility
            # normalization and whitespace folding can change string
            # positions relative to the stored text.
            examples.append(
                {
                    "row_id": row.row_id,
                    "document_id": row.document_id,
                    "context": context(normalized, match.start(), match.end(), context_chars),
                }
            )
    return hits, examples


def unique_documents(hits: Iterable[Hit]) -> set[str]:
    return {hit.row.document_id for hit in hits}


def probe_case(
    rows: list[Row],
    case_id: str,
    case: dict[str, object],
    max_examples: int,
    context_chars: int,
) -> dict[str, object]:
    anchor = str(case["anchor"])
    related = [str(item) for item in case.get("related", [])]
    anchor_hits, anchor_examples = scan_term(rows, compile_term(anchor), max_examples, context_chars)
    anchor_docs = unique_documents(anchor_hits)
    anchor_rows = {hit.row.row_id for hit in anchor_hits}
    result: dict[str, object] = {
        "case": case_id,
        "label": case["label"],
        "anchor": anchor,
        "anchor_chunks": len(anchor_hits),
        "anchor_documents": len(anchor_docs),
        "anchor_examples": anchor_examples,
        "related": [],
    }
    related_results: list[dict[str, object]] = []
    seen_related_patterns: set[str] = set()
    for term in related:
        regex = compile_term(term)
        # ``24–28`` and ``24-28`` intentionally normalize to the same
        # pattern; report that lexical form once rather than double-counting.
        if regex.pattern in seen_related_patterns:
            continue
        seen_related_patterns.add(regex.pattern)
        hits, examples = scan_term(rows, regex, max_examples, context_chars)
        docs = unique_documents(hits)
        both_docs = len(anchor_docs & docs)
        same_document_hits = [hit for hit in hits if hit.row.document_id in anchor_docs]
        same_chunk_hits = [hit for hit in hits if hit.row.row_id in anchor_rows]
        related_results.append(
            {
                "term": term,
                "chunks": len(hits),
                "documents": len(docs),
                "same_document_as_anchor": both_docs,
                "same_chunk_as_anchor": len(same_chunk_hits),
                "examples": examples,
                "same_document_examples": hit_examples(
                    same_document_hits, max_examples, context_chars
                ),
                "same_chunk_examples": hit_examples(same_chunk_hits, max_examples, context_chars),
            }
        )
    result["related"] = related_results
    return result


def print_case(result: dict[str, object]) -> None:
    print(
        f"[{result['case']}] {result['label']} | "
        f"anchor='{result['anchor']}': {result['anchor_chunks']} chunks, "
        f"{result['anchor_documents']} documents"
    )
    for item in result["related"]:
        print(
            f"  related='{item['term']}': {item['chunks']} chunks, "
            f"{item['documents']} documents; "
            f"same document={item['same_document_as_anchor']}; "
            f"same chunk={item['same_chunk_as_anchor']}"
        )
        for example in item["same_chunk_examples"]:
            print(
                f"    same-chunk example {example['document_id']} / "
                f"{example['row_id']}: {example['context']}"
            )
        for example in item["same_document_examples"]:
            if example not in item["same_chunk_examples"]:
                print(
                    f"    same-document example {example['document_id']} / "
                    f"{example['row_id']}: {example['context']}"
                )
        if not item["same_document_examples"]:
            for example in item["examples"]:
                print(f"    lexical example {example['document_id']} / {example['row_id']}: {example['context']}")
    print()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", required=True, help="SQLite index path")
    parser.add_argument("--case", default="all", help="case id (for example Q15) or all")
    parser.add_argument("--table", help="SQLite table/view; otherwise auto-detect")
    parser.add_argument("--text-column", help="text column; otherwise auto-detect")
    parser.add_argument("--id-column", help="chunk id column; otherwise auto-detect")
    parser.add_argument("--document-column", help="document id column; otherwise auto-detect")
    parser.add_argument("--max-examples", type=int, default=3)
    parser.add_argument("--context-chars", type=int, default=160)
    parser.add_argument("--output", help="optional JSON output path")
    args = parser.parse_args(argv)

    db_path = Path(args.db)
    if not db_path.exists():
        parser.error(f"database not found: {db_path}")
    if args.case != "all" and args.case not in CASES:
        parser.error(f"unknown case {args.case}; choose one of {', '.join(CASES)} or all")

    uri = f"file:{db_path.resolve().as_posix()}?mode=ro"
    try:
        connection = sqlite3.connect(uri, uri=True)
    except sqlite3.Error as exc:
        print(f"无法以只读模式打开 SQLite: {exc}", file=sys.stderr)
        return 2

    try:
        table, text_column, id_column, document_holder = discover_columns(
            connection,
            args.table,
            args.text_column,
            args.id_column,
            args.document_column,
        )
        document_column = document_holder[0]
        rows = list(iter_rows(connection, table, text_column, id_column, document_column))
    except (sqlite3.Error, ValueError) as exc:
        print(f"索引结构无法识别: {exc}", file=sys.stderr)
        return 2
    finally:
        connection.close()

    selected = list(CASES) if args.case == "all" else [args.case]
    print(f"[READ ONLY] {db_path.resolve()}")
    print(f"[TABLE] {table} | text={text_column} | id={id_column} | document={document_column}")
    print("[匹配] 大小写不敏感、词边界约束、短语允许空格/连字符变化。")
    print("[警告] 命中只代表出现词语；零命中不证明不存在同义表述，也不代表证据支持临床结论。\n")

    results = [
        probe_case(rows, case_id, CASES[case_id], args.max_examples, args.context_chars)
        for case_id in selected
    ]
    for result in results:
        print_case(result)

    payload = {
        "db": str(db_path.resolve()),
        "table": table,
        "text_column": text_column,
        "id_column": id_column,
        "document_column": document_column,
        "matching": "case-insensitive regex with token boundaries",
        "results": results,
    }
    if args.output:
        output = Path(args.output)
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"[OUTPUT] {output.resolve()}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
