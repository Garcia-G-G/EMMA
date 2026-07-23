#!/usr/bin/env python3
"""Audit ~/.emma/memory.db for mis-classified sensitive facts (24.6-E2 / 24.7-A2).

Opens the memory store READ-ONLY, scans every `facts` row, and classifies its
content against the redaction patterns. The invariant from SECURITY.md:

    No Secret-tier value (API key, token, card, CURP/RFC/SSN, IBAN) ever lives in
    `memory.db` as cleartext — it must be a `vault_ref` placeholder.

So any row whose CONTENT matches a high-confidence secret pattern but has
`vault_ref IS NULL` is a [CRITICAL] mis-classification (a real secret sitting in
the personal DB). Personal PII (email/phone) is reported as [INFO].

    python scripts/audit_memory_db.py                 # report only
    python scripts/audit_memory_db.py --fix-classification   # null out leaked
                                                      # content (keeps the row,
                                                      # sets a placeholder) — review!

Writes flagged_rows.csv next to the DB. Never prints a secret value.
"""

from __future__ import annotations

import argparse
import csv
import re
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from core import redaction

_DB = Path.home() / ".emma" / "memory.db"
# Personal-tier (shareable PII, not secrets) — reported, not critical.
_EMAIL = re.compile(r"\b[\w.+-]+@[\w-]+\.[\w.-]+\b")
_PHONE = re.compile(r"\+?\d[\d\s().-]{7,}\d")


def _classify(content: str) -> str:
    """secret | personal | public for one fact's content."""
    if redaction.contains_secret(content):
        return "secret"
    if _EMAIL.search(content) or _PHONE.search(content):
        return "personal"
    return "public"


def main() -> int:
    ap = argparse.ArgumentParser(description="Auditar memory.db por secretos mal clasificados.")
    ap.add_argument("--db", type=Path, default=_DB)
    ap.add_argument("--fix-classification", action="store_true",
                    help="reemplaza el contenido de filas secret-sin-vault por un placeholder")
    args = ap.parse_args()

    if not args.db.exists():
        print(f"No existe {args.db}.")
        return 0

    conn = sqlite3.connect(f"file:{args.db}?mode=ro", uri=True)
    try:
        rows = conn.execute(
            "SELECT id, content, kind, vault_ref FROM facts WHERE superseded_at IS NULL"
        ).fetchall()
    finally:
        conn.close()

    counts = {"secret": 0, "personal": 0, "public": 0}
    flagged: list[tuple] = []  # (id, tier, kind, has_vault, reason)
    for fid, content, kind, vault_ref in rows:
        tier = _classify(content or "")
        counts[tier] += 1
        if tier == "secret" and vault_ref is None:
            flagged.append((fid, "secret", kind, False,
                            "[CRITICAL] secret-shaped content with NO vault_ref"))
        elif tier == "personal":
            flagged.append((fid, "personal", kind, vault_ref is not None,
                            "[INFO] personal PII (email/phone) in cleartext"))

    print(f"Escaneadas {len(rows)} filas activas:")
    print(f"  secret-shaped: {counts['secret']}   personal-PII: {counts['personal']}   "
          f"public: {counts['public']}")
    crit = [f for f in flagged if f[1] == "secret"]
    print(f"\n[CRITICAL] secretos mal clasificados (sin vault_ref): {len(crit)}")
    print(f"[INFO] PII personal en claro: {len([f for f in flagged if f[1] == 'personal'])}")

    out_csv = args.db.parent / "flagged_rows.csv"
    with open(out_csv, "w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        w.writerow(["fact_id", "tier", "kind", "has_vault_ref", "reason"])  # NEVER the content
        w.writerows(flagged)
    print(f"\nDetalle (sin valores) → {out_csv}")

    if args.fix_classification and crit:
        # Replace leaked secret content with a placeholder (keep the row so the user
        # can review). Does NOT delete and does NOT move to Keychain automatically.
        wconn = sqlite3.connect(args.db)
        try:
            for fid, *_ in crit:
                wconn.execute(
                    "UPDATE facts SET content=? WHERE id=?",
                    (f"(stored as secret: needs-review-{fid})", fid))
            wconn.commit()
        finally:
            wconn.close()
        print(f"\n✓ {len(crit)} filas neutralizadas (contenido → placeholder). Revísalas y "
              "muévelas al Keychain con remember_secret si aplica.")
    elif crit:
        print("\nCorre con --fix-classification para neutralizar el contenido filtrado.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
