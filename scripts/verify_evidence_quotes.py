#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Re-check quotation existence against the exact source text stored with cards.
Run in PyCharm after generating cards or after changes to verification logic.
"""
from __future__ import annotations

import json
from pathlib import Path
from evidence_review_core import DB_FILE, REPORTS, audit_card, connect_db, export_cards


def main():
    con = connect_db(DB_FILE)
    try:
        rows = con.execute("SELECT key,card_json,context_json,audit_json FROM cards").fetchall()
        failures = 0
        for row in rows:
            context = json.loads(row["context_json"])
            card = json.loads(row["card_json"])
            old = json.loads(row["audit_json"])
            new = audit_card(card, context)
            # Never silently change a previous audit: any change appears in console.
            if old["counts"] != new["counts"]:
                print(f"变化 {row['key']}: {old['counts']} -> {new['counts']}")
            con.execute("UPDATE cards SET audit_json=? WHERE key=?", (json.dumps(new, ensure_ascii=False), row["key"]))
            failures += new["counts"]["QUOTE_NOT_FOUND"]
            print(f"{row['key']}: {new['counts']}")
        con.commit()
        export_cards(con, REPORTS / "evidence_cards.jsonl")
        print(f"已复核 {len(rows)} 卡，完全找不到的引用 {failures} 条。字面核验不等于语义支持。")
    finally:
        con.close()


if __name__ == "__main__":
    main()
