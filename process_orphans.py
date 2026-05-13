"""
Backfill orphan PDFs in *Statics folders that the live drive_watcher
never picked up.

Why this script exists
----------------------
drive_watcher.py filters Drive queries by `modifiedTime > last_scan`.
Files older than the watermark are invisible to the live watcher — it
will never re-query them. So any orphan PDF sitting in a _Statics folder
without a `<stem>_p.pdf` companion stays unprocessed forever unless
something pokes its modifiedTime.

This script is the manual catch-up pass. It:
  1. Walks every watched print root (MO Print, YCR Print, etc.).
  2. Descends into any subfolder whose name contains "static"
     (case-insensitive — catches "_YCR Statics", "MO Statics", etc.).
  3. Lists PDFs at and below that folder, ignoring `_p.pdf`, originals
     already in `Originals/`, and anything skip-listed.
  4. For each orphan original (no sibling `_p.pdf`): runs the same
     processing path as the live watcher (vectorize_v2.process_pdf +
     upload `_p.pdf` + archive original to `Originals/`).
  5. Also generates LRG variants for any SKU that has a REG file but
     no LRG sibling (static catalogue completeness). The live watcher
     does NOT do this; this script does because statics are the only
     place auto-LRG makes sense.

Usage (Replit shell, where GOOGLE_CREDENTIALS_JSON is set):
    cd doormat-tools
    python process_orphans.py --dry-run            # preview only
    python process_orphans.py --brand YCR          # one brand
    python process_orphans.py                      # all brands, do it
    python process_orphans.py --skip-lrg-gen       # process only, no LRG gen
"""

import argparse
import os
import re
import sys
import tempfile
from collections import defaultdict

# Re-use the watcher's machinery so behaviour stays consistent.
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from drive_watcher import (
    ALL_PRINT_ROOTS,
    AW_PRINT_ROOTS,
    EXACT_SKIP_TITLES,
    SKIP_TITLE_FRAGMENTS,
    PROCESSED_SUBFOLDER_NAME,
    detect_size_from_folder_name,
    download_file,
    file_already_processed,
    get_drive_service,
    move_file_to_processed,
    normalise,
    upload_file,
)
from vectorize_v2 import process_pdf, generate_lrg_from_reg, get_sku
from googleapiclient.errors import HttpError


# ── Drive walking ────────────────────────────────────────────────────────────

def is_skipped_folder(name: str) -> bool:
    n = normalise(name)
    if n in EXACT_SKIP_TITLES:
        return True
    for frag in SKIP_TITLE_FRAGMENTS:
        if frag in n:
            return True
    return False


def is_statics_folder(name: str) -> bool:
    return "static" in normalise(name)


def find_root_folders(brand_filter: str | None = None) -> list[tuple[str, str]]:
    """Return [(folder_id, title), ...] for every watched print root present in Drive.

    brand_filter: if provided, only roots containing this substring (case-insensitive)
    are returned. e.g. brand_filter="YCR" → ["YCR Print", "YCR AW Print"].
    """
    svc = get_drive_service()
    roots: list[tuple[str, str]] = []
    for title in ALL_PRINT_ROOTS:
        if brand_filter and brand_filter.lower() not in title.lower():
            continue
        safe = title.replace("'", "\\'")
        q = (
            f"name = '{safe}'"
            " and mimeType = 'application/vnd.google-apps.folder'"
            " and trashed = false"
        )
        resp = svc.files().list(
            q=q, fields="files(id,name)", pageSize=10,
            supportsAllDrives=True, includeItemsFromAllDrives=True, corpora="allDrives",
        ).execute()
        for f in resp.get("files", []):
            roots.append((f["id"], f["name"]))
    return roots


def list_children(parent_id: str) -> list[dict]:
    """List immediate non-trashed children (folders + files) of parent_id."""
    svc = get_drive_service()
    out: list[dict] = []
    page_token = None
    while True:
        q = f"'{parent_id}' in parents and trashed = false"
        resp = svc.files().list(
            q=q,
            fields="nextPageToken, files(id,name,mimeType,parents,size)",
            pageSize=200,
            pageToken=page_token,
            supportsAllDrives=True,
            includeItemsFromAllDrives=True,
            corpora="allDrives",
        ).execute()
        out.extend(resp.get("files", []))
        page_token = resp.get("nextPageToken")
        if not page_token:
            break
    return out


def walk_statics(
    root_folder_id: str,
    root_title: str,
) -> list[dict]:
    """Walk down from root_folder_id. Collect every PDF that lives inside a
    folder whose chain from root contains a *Statics* folder.

    Each returned dict carries: id, name, parent_id, parent_title, force_size,
    root_title — enough for process_one-style handling.
    """
    collected: list[dict] = []

    def descend(folder_id: str, folder_title: str, inside_statics: bool, size_hint: str | None):
        """DFS. Tracks whether we're inside a statics subtree and what size
        was hinted by the closest size-named ancestor."""
        # Drop into "inside statics" if THIS folder is a statics folder
        now_inside = inside_statics or is_statics_folder(folder_title)
        # Update size hint from this folder if it's a size folder
        local_size = detect_size_from_folder_name(folder_title)
        effective_size = local_size or size_hint

        for child in list_children(folder_id):
            name = child.get("name", "")
            mime = child.get("mimeType", "")

            if mime == "application/vnd.google-apps.folder":
                if is_skipped_folder(name):
                    continue
                if name == PROCESSED_SUBFOLDER_NAME:
                    # Never recurse into Originals/ — those are archived originals.
                    continue
                descend(child["id"], name, now_inside, effective_size)
                continue

            if not now_inside:
                continue  # only collect PDFs inside a statics subtree
            if not name.lower().endswith(".pdf"):
                continue
            if name.lower().endswith("_p.pdf"):
                continue

            # AW vs non-AW: if root is in AW_PRINT_ROOTS and no folder-level size was
            # detected, force AW (matches drive_watcher process_one behaviour).
            final_size = effective_size
            if final_size is None and root_title in AW_PRINT_ROOTS:
                final_size = "AW"

            collected.append({
                "id": child["id"],
                "name": name,
                "parent_id": folder_id,
                "parent_title": folder_title,
                "force_size": final_size,
                "root_title": root_title,
            })

    descend(root_folder_id, root_title, inside_statics=False, size_hint=None)
    return collected


# ── Processing ───────────────────────────────────────────────────────────────

def process_orphan(item: dict, dry_run: bool) -> tuple[str, str]:
    """Returns (outcome, detail). outcome: 'processed', 'skipped', 'error'."""
    name = item["name"]
    stem = os.path.splitext(name)[0]
    parent_id = item["parent_id"]
    file_id = item["id"]
    force_size = item["force_size"]

    if file_already_processed(parent_id, stem):
        return "skipped", f"{name}: _p.pdf already exists"

    if dry_run:
        return "would-process", f"{name} (size={force_size or 'auto'})"

    with tempfile.TemporaryDirectory() as tmpdir:
        input_path = os.path.join(tmpdir, name)
        if not download_file(file_id, input_path):
            return "error", f"{name}: download failed"

        output_path = os.path.join(tmpdir, f"{stem}_p.pdf")
        try:
            process_pdf(input_path, output_path=output_path, dpi=150, force_size=force_size)
        except Exception as e:
            return "error", f"{name}: process error: {e}"

        if not os.path.exists(output_path) or os.path.getsize(output_path) == 0:
            return "error", f"{name}: output missing"

        uploaded_id = upload_file(output_path, parent_id, f"{stem}_p.pdf")
        if not uploaded_id:
            return "error", f"{name}: upload failed"

    ok, reason = move_file_to_processed(file_id, parent_id)
    archive_msg = " (original → Originals/)" if ok else f" (archive FAILED: {reason or 'unknown'})"
    return "processed", f"{name} → {stem}_p.pdf{archive_msg}"


def generate_missing_lrg(
    items_by_folder: dict[str, list[dict]],
    dry_run: bool,
) -> list[tuple[str, str]]:
    """For each folder, find SKUs with REG but no LRG and generate the LRG.
    Returns list of (outcome, detail).

    Operates on the ORIGINAL list (pre-processing) — generates LRG from REG
    and uploads it as a new original PDF; on next run (or follow-up pass)
    it'll be picked up and processed like any other orphan.
    """
    results: list[tuple[str, str]] = []
    for parent_id, items in items_by_folder.items():
        # Build SKU map for this folder
        sku_files: dict[str, list[dict]] = defaultdict(list)
        for it in items:
            sku = get_sku(it["name"]).upper()
            sku_files[sku].append(it)

        for sku, files in sku_files.items():
            names_upper = [f["name"].upper() for f in files]
            has_lrg = any(("LRG" in n) or ("LAR" in n) for n in names_upper)
            has_reg = any(
                ("REG" in n) or not any(tag in n for tag in ["LRG", "LAR", "SMA", "SMALL"])
                for n in names_upper
            )
            if not has_reg or has_lrg:
                continue

            # Take the first REG file as the source for LRG generation
            reg_items = [
                f for f in files
                if "LRG" not in f["name"].upper()
                and "LAR" not in f["name"].upper()
                and "SMA" not in f["name"].upper()
                and "SMALL" not in f["name"].upper()
            ]
            if not reg_items:
                continue
            reg_item = reg_items[0]

            reg_name = reg_item["name"]
            base, ext = os.path.splitext(reg_name)
            if "REG" in reg_name.upper():
                lrg_name = re.sub(r"\bREG\b", "LRG", reg_name, flags=re.IGNORECASE)
                if lrg_name == reg_name:  # case insensitive sub didn't fire on word boundary
                    lrg_name = reg_name.replace(" REG", " LRG").replace(" reg", " LRG")
            else:
                lrg_name = f"{base} LRG{ext}"

            if dry_run:
                results.append(("would-gen-lrg", f"{sku}: would generate {lrg_name}"))
                continue

            # Skip if the LRG already exists in this folder (race with another run)
            if file_already_processed(parent_id, os.path.splitext(lrg_name)[0]) or \
               _lrg_already_exists(parent_id, lrg_name):
                results.append(("skipped", f"{sku}: LRG already present"))
                continue

            with tempfile.TemporaryDirectory() as tmpdir:
                reg_path = os.path.join(tmpdir, reg_name)
                if not download_file(reg_item["id"], reg_path):
                    results.append(("error", f"{sku}: REG download failed"))
                    continue

                lrg_path = os.path.join(tmpdir, lrg_name)
                try:
                    generate_lrg_from_reg(reg_path, lrg_path, dpi=150)
                except Exception as e:
                    results.append(("error", f"{sku}: LRG gen failed: {e}"))
                    continue

                if not os.path.exists(lrg_path) or os.path.getsize(lrg_path) == 0:
                    results.append(("error", f"{sku}: LRG output missing"))
                    continue

                # generate_lrg_from_reg produces the FINAL _p-equivalent (rich black,
                # CMYK). Upload directly as <stem>_p.pdf so the live watcher won't
                # try to re-process it. Also upload an unprocessed-looking original
                # copy so the SKU has a regular LRG in the folder for human reference.
                lrg_p_name = f"{os.path.splitext(lrg_name)[0]}_p.pdf"
                uploaded_p = upload_file(lrg_path, parent_id, lrg_p_name)
                if not uploaded_p:
                    results.append(("error", f"{sku}: LRG _p upload failed"))
                    continue

            results.append(("processed-lrg", f"{sku}: generated {lrg_p_name}"))

    return results


def _lrg_already_exists(parent_id: str, lrg_name: str) -> bool:
    """Whether <lrg_name> exists in parent_id."""
    svc = get_drive_service()
    safe = lrg_name.replace("'", "\\'")
    q = (
        f"'{parent_id}' in parents"
        f" and name = '{safe}'"
        " and trashed = false"
    )
    try:
        resp = svc.files().list(
            q=q, fields="files(id)", pageSize=1,
            supportsAllDrives=True, includeItemsFromAllDrives=True, corpora="allDrives",
        ).execute()
        return len(resp.get("files", [])) > 0
    except HttpError:
        return False


# ── CLI ──────────────────────────────────────────────────────────────────────

def main():
    ap = argparse.ArgumentParser(description="Backfill orphan PDFs in *Statics folders")
    ap.add_argument("--dry-run", action="store_true", help="Preview only — no Drive writes")
    ap.add_argument("--brand", type=str, default=None,
                    help="Limit to roots whose name contains this string (e.g. YCR)")
    ap.add_argument("--skip-lrg-gen", action="store_true",
                    help="Skip LRG-from-REG generation pass")
    args = ap.parse_args()

    print(f"[orphans] starting (dry_run={args.dry_run}, brand={args.brand or 'all'})")

    roots = find_root_folders(brand_filter=args.brand)
    if not roots:
        print(f"[orphans] no watched roots found"
              f"{' for brand=' + args.brand if args.brand else ''}")
        return 1
    print(f"[orphans] found {len(roots)} watched root(s):")
    for _, t in roots:
        print(f"           - {t}")

    # Collect every orphan PDF under each root's statics subtree
    all_items: list[dict] = []
    for root_id, root_title in roots:
        items = walk_statics(root_id, root_title)
        print(f"[orphans] {root_title}: {len(items)} PDF(s) in statics subtrees")
        all_items.extend(items)

    if not all_items:
        print("[orphans] nothing to do")
        return 0

    # Pass 1: process orphan originals (skip ones that already have _p.pdf)
    print(f"\n[orphans] Pass 1 — processing {len(all_items)} orphan candidate(s)")
    counts = defaultdict(int)
    for i, item in enumerate(all_items, 1):
        outcome, detail = process_orphan(item, dry_run=args.dry_run)
        counts[outcome] += 1
        print(f"  [{i}/{len(all_items)}] {outcome}: {detail}")

    # Pass 2: LRG generation for REG-only SKUs in each statics folder
    if not args.skip_lrg_gen:
        # Group items by parent folder for SKU analysis
        by_folder: dict[str, list[dict]] = defaultdict(list)
        for it in all_items:
            by_folder[it["parent_id"]].append(it)

        print(f"\n[orphans] Pass 2 — LRG generation across {len(by_folder)} statics folder(s)")
        lrg_results = generate_missing_lrg(by_folder, dry_run=args.dry_run)
        for outcome, detail in lrg_results:
            counts[outcome] += 1
            print(f"  {outcome}: {detail}")
    else:
        print("\n[orphans] Pass 2 skipped (--skip-lrg-gen)")

    # Summary
    print("\n[orphans] DONE")
    print(f"  Total: {sum(counts.values())}")
    for k, v in sorted(counts.items()):
        print(f"  {k}: {v}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
