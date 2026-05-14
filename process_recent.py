"""
One-shot backfill: process PDFs modified in the last N hours that haven't
been processed yet.

Use case: production watcher was off for a few hours (deploy issue, disabled
secret, etc.). Order PDFs uploaded during that window never got their _p.pdf
companion, and the watcher's modifiedTime watermark moves forward so once
it's back, it won't re-query them.

This script does what the watcher would have done if it had been running:
queries Drive for "modifiedTime > now - N hours", walks the chain to
determine root/size, skips files already processed (i.e. that have a
_p.pdf companion in the same folder), and processes the rest with the
SAME code path as the live watcher (vectorize_v2.process_pdf + upload
_p.pdf + archive original to Originals/).

Unlike process_orphans.py:
  - This walks the entire Drive query for recent files (not just statics).
  - It uses the live watcher's walk_chain — so files in current-month
    monthly folders, marketplace folders, Reprints, Bulks, etc. are all
    eligible (same rules as the live watcher).
  - It does NOT generate LRGs (statics-only behaviour).

Usage:
    python process_recent.py --hours 4              # last 4 hours, do it
    python process_recent.py --hours 4 --dry-run    # preview only
    python process_recent.py --hours 12             # wider window
"""

import argparse
import os
import sys
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from drive_watcher import (
    find_candidate_files,
    walk_chain,
    process_one,
    OUTCOME_PROCESSED,
    OUTCOME_SKIPPED,
    OUTCOME_ERROR,
)


def main() -> int:
    ap = argparse.ArgumentParser(description="Backfill recent unprocessed PDFs")
    ap.add_argument("--hours", type=float, default=4.0,
                    help="Look back this many hours (default: 4)")
    ap.add_argument("--dry-run", action="store_true",
                    help="Preview only — no Drive writes")
    args = ap.parse_args()

    now = datetime.now(timezone.utc)
    since = now - timedelta(hours=args.hours)
    print(f"[recent] starting (since={since.isoformat()}, dry_run={args.dry_run})")

    candidates = find_candidate_files(since)
    print(f"[recent] found {len(candidates)} candidate(s) modified in last {args.hours}h")

    # Filter to those that pass the watcher's chain rules
    kept = []
    skipped_by_chain = 0
    for f in candidates:
        name = f.get("name", "")
        if name.lower().endswith("_p.pdf"):
            continue
        root_title, force_size, reason = walk_chain(f, now.year, now.month)
        if root_title is None:
            skipped_by_chain += 1
            continue
        kept.append((f, root_title, force_size))

    print(f"[recent] {skipped_by_chain} rejected by chain (wrong root / skip folder / not in tree)")
    print(f"[recent] {len(kept)} candidate(s) eligible for processing")

    if not kept:
        print("[recent] nothing to do")
        return 0

    if args.dry_run:
        for i, (f, root_title, force_size) in enumerate(kept, 1):
            size_str = force_size or "auto"
            print(f"  [{i}/{len(kept)}] would-process: {f['name']} (root={root_title}, size={size_str})")
        print(f"\n[recent] DRY RUN — would process {len(kept)} file(s)")
        return 0

    # Process each via the SAME code path as the live watcher
    counts = {"processed": 0, "skipped": 0, "error": 0}
    for i, (f, root_title, force_size) in enumerate(kept, 1):
        outcome, detail = process_one(f, root_title, force_size=force_size)
        if outcome == OUTCOME_PROCESSED:
            counts["processed"] += 1
            tag = "processed"
        elif outcome == OUTCOME_SKIPPED:
            counts["skipped"] += 1
            tag = "skipped"
        else:
            counts["error"] += 1
            tag = "error"
        print(f"  [{i}/{len(kept)}] {tag}: {detail}")

    print(f"\n[recent] DONE")
    print(f"  processed: {counts['processed']}")
    print(f"  skipped:   {counts['skipped']}")
    print(f"  errors:    {counts['error']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
