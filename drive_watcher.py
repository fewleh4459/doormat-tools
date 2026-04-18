"""
Drive-API-based doormat watcher for Replit Reserved VM.

Polls Google Drive every 60 seconds for new PDF design files across all
watched folders, processes them via vectorize_v2.py, uploads the result
and deletes the original. Designed to run 24/7 on a Linux VM (Replit,
Raspberry Pi, DigitalOcean, etc.) with no PC dependency.

Quality is identical to the local watcher.py — same Python, same
vectorize_v2.py, same rich black CMYK output.

Environment variables required:
  GOOGLE_CREDENTIALS_JSON  — service account JSON (full content as string)
  GMAIL_USER               — gmail address for notifications
  GMAIL_APP_PASSWORD       — 16-char gmail app password
  NOTIFY_TO                — recipient for summary emails (defaults to GMAIL_USER)

Optional:
  POLL_INTERVAL_SECONDS    — default 60
  STATE_FILE               — default ./last_scan.json

Usage:
  python drive_watcher.py
"""

import os
import sys
import time
import json
import io
import re
import tempfile
import logging
import traceback
from datetime import datetime, timedelta, timezone
from pathlib import Path

from google.oauth2 import service_account
from google.oauth2.credentials import Credentials as UserCredentials
from googleapiclient.discovery import build
from googleapiclient.http import MediaIoBaseDownload, MediaFileUpload
from googleapiclient.errors import HttpError

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from vectorize_v2 import process_pdf, generate_lrg_from_reg, get_target_size
from notify import send_summary


# ── Configuration ─────────────────────────────────────────────────────────────

POLL_INTERVAL_SECONDS = int(os.environ.get("POLL_INTERVAL_SECONDS", "60"))
STATE_FILE = os.environ.get("STATE_FILE", "last_scan.json")
LOG_FILE = os.environ.get("LOG_FILE", "drive_watcher.log")

# Overlap window: re-query up to 2 minutes before last successful scan,
# so any files mid-write at last poll still get picked up next time.
OVERLAP_SECONDS = 120

# Root folders to watch. The script walks each candidate file's parent chain
# until it hits one of these titles; files not inside one of these trees are
# skipped. No need to hard-code folder IDs — we identify by title.
COIR_PRINT_ROOTS = {
    "MO Print",
    "_CB Print",
    "EMA Print",
    "CUS Print",       # CMD
    "YCR Print",
    "2DD Print",
    "DD Print",
}
AW_PRINT_ROOTS = {
    "CUS AW Print",    # CMD AW
    "EMA AW Print",
    "2DD AW Print",
    "YCR AW Print",
    "DD AW Print",
}
ALL_PRINT_ROOTS = COIR_PRINT_ROOTS | AW_PRINT_ROOTS

# Titles that ALWAYS cause a file to be skipped if present anywhere in the chain.
SKIP_TITLE_FRAGMENTS = [
    "_richblack", "old", "test", "print-tests",
    "backup", "archive", "do not use", "dnu", "deprecated",
]

# Folder titles that are always skipped on exact (case-insensitive) match.
# Used for folders whose name is a common word — fragment match would be too broad.
EXACT_SKIP_TITLES = {"processed"}

# Name of the subfolder where originals are archived after processing.
# Using an exact match so legitimate folders like "Pre-processed" aren't skipped.
PROCESSED_SUBFOLDER_NAME = "Processed"

SIZE_TITLES = {
    "small", "medium", "large", "regular", "reg", "lrg", "sml", "med",
    "sma", "lar", "xl", "xxl", "mini", "tiny", "big",
}

# Folder-name → force_size string passed to vectorize_v2.process_pdf.
# Covers both named folders ("Small") and dimension folders ("60x40", "700x400mm").
SIZE_FOLDER_MAP = {
    # Named sizes
    "small": "SMALL", "sml": "SMALL", "sma": "SMALL",
    "medium": "MED", "med": "MED", "regular": "MED", "reg": "MED",
    "large": "LAR", "lrg": "LAR", "lar": "LAR",
    "exact": "AW", "aw": "AW",
    "all weather": "AW", "all-weather": "AW", "weatherproof": "AW",
}

# Known doormat dimensions in mm → force_size string. Matched via regex
# below so variants like "60x40", "60 x 40 cm", "600x400mm", "600×400" all work.
_DIM_TO_SIZE = {
    (600, 400): "SMALL",
    (700, 400): "MED",
    (900, 600): "LAR",
    (760, 460): "AW",
}


def detect_size_from_folder_name(title: str) -> str | None:
    """Return a force_size string ('SMALL' / 'MED' / 'LAR' / 'AW') if the
    folder title is a recognised size folder; otherwise None."""
    t = normalise(title)
    # Direct named match
    if t in SIZE_FOLDER_MAP:
        return SIZE_FOLDER_MAP[t]
    # Dimension match: e.g. "60x40", "60 x 40 cm", "600x400mm", "900×600"
    dim_match = re.fullmatch(
        r"\s*(\d{2,3})\s*[x×]\s*(\d{2,3})\s*(cm|mm)?\s*",
        t,
    )
    if dim_match:
        w = int(dim_match.group(1))
        h = int(dim_match.group(2))
        unit = dim_match.group(3) or ""
        # Convert cm to mm when needed
        if w < 100 and h < 100 and unit != "mm":   # looks like cm
            w *= 10
            h *= 10
        return _DIM_TO_SIZE.get((w, h))
    return None

MONTH_NAMES = {
    "january": 1, "february": 2, "march": 3, "april": 4, "may": 5, "june": 6,
    "july": 7, "august": 8, "september": 9, "october": 10, "november": 11, "december": 12,
    "jan": 1, "feb": 2, "mar": 3, "apr": 4, "jun": 6,
    "jul": 7, "aug": 8, "sep": 9, "sept": 9, "oct": 10, "nov": 11, "dec": 12,
}

# Max walk depth — safety limit so we don't loop forever if something weird happens
MAX_WALK_HOPS = 15

# Cap files per cycle so a mass upload doesn't stall the poller; leftovers
# get picked up on the next cycle.
MAX_FILES_PER_CYCLE = 20


# ── Logging ───────────────────────────────────────────────────────────────────

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.FileHandler(LOG_FILE),
        logging.StreamHandler(sys.stdout),
    ],
)
log = logging.getLogger("drive_watcher")


# ── Drive service ─────────────────────────────────────────────────────────────

_SERVICE = None
_METADATA_CACHE: dict = {}  # fileId -> {title, parents, mimeType, shortcut_target}
_PARENT_CLASSIFICATION_CACHE: dict = {}  # parentId -> "skip" | "ok" | title
_PROCESSED_FOLDER_CACHE: dict = {}  # parentId -> "Processed" subfolder id (or "" if absent)


SCOPES = ["https://www.googleapis.com/auth/drive"]


def get_drive_service():
    """Authenticate and return the Drive v3 service.

    Accepts either:
    - Service account JSON (works for read, FAILS for upload due to quota)
    - OAuth user credentials JSON with refresh_token (recommended for upload)

    The OAuth user flavour is produced by running authorize.py once on the
    user's PC. It looks like:
        {
          "type": "oauth_user",
          "refresh_token": "1//...",
          "client_id": "...apps.googleusercontent.com",
          "client_secret": "...",
          "token_uri": "https://oauth2.googleapis.com/token"
        }
    """
    global _SERVICE
    if _SERVICE is not None:
        return _SERVICE

    creds_json = os.environ.get("GOOGLE_CREDENTIALS_JSON")
    if not creds_json:
        raise RuntimeError("GOOGLE_CREDENTIALS_JSON env var not set")

    if creds_json.strip().startswith("{"):
        info = json.loads(creds_json)
    else:
        with open(creds_json, "r") as f:
            info = json.load(f)

    cred_type = info.get("type", "")

    if cred_type == "service_account":
        # Service account — works for read-only but CAN'T upload (no quota).
        # Use OAuth user credentials for full read/write.
        creds = service_account.Credentials.from_service_account_info(info, scopes=SCOPES)
        log.warning(
            "Using service account credentials — uploads will fail due to storage quota. "
            "Run authorize.py to generate OAuth user credentials for full read/write."
        )
    elif cred_type == "oauth_user" or "refresh_token" in info:
        creds = UserCredentials(
            token=None,
            refresh_token=info["refresh_token"],
            token_uri=info.get("token_uri", "https://oauth2.googleapis.com/token"),
            client_id=info["client_id"],
            client_secret=info["client_secret"],
            scopes=SCOPES,
        )
    else:
        raise RuntimeError(
            f"GOOGLE_CREDENTIALS_JSON has unrecognised type={cred_type!r}. "
            "Expected 'service_account' or 'oauth_user'."
        )

    _SERVICE = build("drive", "v3", credentials=creds, cache_discovery=False)
    return _SERVICE


def get_metadata(file_id: str) -> dict | None:
    """Fetch metadata for a file, cached."""
    if file_id in _METADATA_CACHE:
        return _METADATA_CACHE[file_id]
    try:
        svc = get_drive_service()
        meta = svc.files().get(
            fileId=file_id,
            fields="id,name,mimeType,parents,shortcutDetails",
            supportsAllDrives=True,
        ).execute()
        _METADATA_CACHE[file_id] = meta
        return meta
    except HttpError as e:
        log.warning(f"get_metadata({file_id}) failed: {e}")
        return None


def resolve_shortcut(meta: dict) -> dict:
    """If meta is a shortcut, fetch and return the real target. Else return meta."""
    if meta.get("mimeType") == "application/vnd.google-apps.shortcut":
        target_id = meta.get("shortcutDetails", {}).get("targetId")
        if target_id:
            target = get_metadata(target_id)
            if target:
                return target
    return meta


# ── Title classification ──────────────────────────────────────────────────────

def normalise(title: str) -> str:
    return re.sub(r"\s+", " ", title.strip().lower())


def classify_title(title: str, current_year: int, current_month: int) -> str:
    """Returns one of:
      - 'reject-skiplist'  — title matches skip fragment
      - 'reject-year'      — year-like and not current year
      - 'reject-month'     — month-like or combined and not current
      - 'transparent'      — year/month/size matches current, or unrecognised
    """
    t = normalise(title)

    # Skip-list fragments (anywhere in the title)
    for frag in SKIP_TITLE_FRAGMENTS:
        if frag in t:
            return "reject-skiplist"

    # Exact-match skip titles (for common words like "processed")
    if t in EXACT_SKIP_TITLES:
        return "reject-skiplist"

    # Strip common surrounding punctuation before parsing
    core = re.sub(r"[\[\]()_]", " ", t).strip()
    core = re.sub(r"\s+", " ", core)

    # Size name
    if core in SIZE_TITLES:
        return "transparent"

    # Full 4-digit year only (e.g. "2026")
    if re.fullmatch(r"20\d{2}", core):
        year = int(core)
        return "transparent" if year == current_year else "reject-year"

    # Full month name or abbreviation (e.g. "April", "Apr")
    if core in MONTH_NAMES:
        return "transparent" if MONTH_NAMES[core] == current_month else "reject-month"

    # Pure numeric month "01".."12"
    if re.fullmatch(r"0?[1-9]|1[0-2]", core):
        m = int(core)
        return "transparent" if m == current_month else "reject-month"

    # Combined month + year parsing (any order, any separator).
    # Strategy: find month and year independently, then cross-check.
    found_year = None
    found_month = None

    # 4-digit year
    m4 = re.search(r"(?<!\d)(20\d{2})(?!\d)", core)
    if m4:
        found_year = int(m4.group(1))

    # Month name
    for mname, mnum in MONTH_NAMES.items():
        if re.search(rf"\b{re.escape(mname)}\b", core):
            found_month = mnum
            break

    # Redundant numeric-month prefix — e.g. "04 Apr" or "03 March".
    # The digits at the start match the month number; treat the digits as
    # redundant (not a year) to avoid "04 Apr" being read as year 2004.
    redundant_prefix_len = 0
    if found_month is not None:
        prefix_match = re.match(r"^(0?\d{1,2})\s+", core)
        if prefix_match:
            prefix_num = int(prefix_match.group(1))
            if 1 <= prefix_num <= 12 and prefix_num == found_month:
                redundant_prefix_len = len(prefix_match.group(0))

    # 2-digit year adjacent to a month name (e.g. "APRIL 26" → 2026).
    # Skip any redundant prefix (see above) and only accept 2-digit numbers
    # >12 so they can't collide with month numbers.
    if found_year is None and found_month is not None:
        search_space = core[redundant_prefix_len:] if redundant_prefix_len else core
        for mname in MONTH_NAMES:
            for pattern in [
                rf"\b{re.escape(mname)}\b[\s\-_/]*(\d{{2}})(?!\d)",
                rf"(?<!\d)(\d{{2}})[\s\-_/]*\b{re.escape(mname)}\b",
            ]:
                m = re.search(pattern, search_space)
                if m:
                    yy = int(m.group(1))
                    if yy >= 13:   # disambiguate: 01-12 is a month, >12 is a year
                        found_year = 2000 + yy
                        break
            if found_year is not None:
                break

    # ISO-style YYYY-MM or reverse MM-YYYY
    if found_month is None:
        iso = re.search(r"(20\d{2})[\-_/](0?[1-9]|1[0-2])(?!\d)", core)
        if iso:
            found_month = int(iso.group(2))
        else:
            rev = re.search(r"(?<!\d)(0?[1-9]|1[0-2])[\-_/](20\d{2})", core)
            if rev:
                found_month = int(rev.group(1))

    if found_year is not None or found_month is not None:
        if found_year is not None and found_year != current_year:
            return "reject-year"
        if found_month is not None and found_month != current_month:
            return "reject-month"
        return "transparent"

    # Nothing recognised — transparent (don't reject on unknown names)
    return "transparent"


# ── Parent chain walk ─────────────────────────────────────────────────────────

def walk_chain(file_meta: dict, current_year: int, current_month: int) -> tuple[str | None, str | None, str | None]:
    """Walk parents until we hit a known Print root or run out.

    Returns (root_title, force_size, reject_reason):
      - (root_title, force_size_or_None, None)   — file is in a watched tree and passes filters
      - (None, None, reason)                     — file should be skipped

    force_size is derived from the first size-folder encountered during the
    walk (e.g. "SMALL" for a "60x40" or "Small" folder). None if no size
    folder was found — the caller can fall back to root-based or filename-based
    size detection.
    """
    meta = resolve_shortcut(file_meta)
    hops = 0
    current_id = None
    parents = meta.get("parents", [])

    # Start from the immediate parent
    if not parents:
        return None, None, "no-parents"
    current_id = parents[0]

    detected_force_size: str | None = None

    while hops < MAX_WALK_HOPS:
        hops += 1

        # Check the parent classification cache first
        cached = _PARENT_CLASSIFICATION_CACHE.get(current_id)
        if cached == "skip":
            return None, None, "reject-skiplist-cached"

        parent_meta = get_metadata(current_id)
        if parent_meta is None:
            return None, None, "parent-fetch-failed"

        title = parent_meta.get("name") or parent_meta.get("title", "")

        # Record the first size-folder hint we see (closest to the file wins)
        if detected_force_size is None:
            size_hint = detect_size_from_folder_name(title)
            if size_hint:
                detected_force_size = size_hint

        # Classify
        classification = classify_title(title, current_year, current_month)
        if classification.startswith("reject"):
            if classification == "reject-skiplist":
                _PARENT_CLASSIFICATION_CACHE[current_id] = "skip"
            return None, None, classification

        # Stop if we've hit a watched root
        if title in ALL_PRINT_ROOTS:
            return title, detected_force_size, None

        # Step up
        grandparents = parent_meta.get("parents", [])
        if not grandparents:
            return None, None, "not-in-tree"
        current_id = grandparents[0]

    return None, None, "too-deep"


# ── State persistence ─────────────────────────────────────────────────────────

def load_last_scan_time() -> datetime:
    """Return the last successful scan timestamp (or 2 minutes ago if never scanned)."""
    try:
        with open(STATE_FILE, "r") as f:
            data = json.load(f)
            return datetime.fromisoformat(data["last_scan"])
    except (FileNotFoundError, KeyError, ValueError):
        # First run — look back 2 minutes so we don't miss anything just dropped in
        return datetime.now(timezone.utc) - timedelta(seconds=OVERLAP_SECONDS)


def save_last_scan_time(ts: datetime) -> None:
    with open(STATE_FILE, "w") as f:
        json.dump({"last_scan": ts.isoformat()}, f)


# ── Drive operations ──────────────────────────────────────────────────────────

def find_candidate_files(since: datetime) -> list[dict]:
    """Query Drive for PDFs modified since `since`, excluding already-processed _p.pdf."""
    svc = get_drive_service()
    query = (
        "mimeType = 'application/pdf'"
        f" and modifiedTime > '{since.strftime('%Y-%m-%dT%H:%M:%SZ')}'"
        " and trashed = false"
        " and not name contains '_p.pdf'"
    )

    all_files = []
    page_token = None
    try:
        while True:
            resp = svc.files().list(
                q=query,
                fields="nextPageToken, files(id,name,mimeType,parents,modifiedTime,shortcutDetails,size)",
                pageSize=100,
                pageToken=page_token,
                supportsAllDrives=True,
                includeItemsFromAllDrives=True,
                corpora="allDrives",
            ).execute()
            all_files.extend(resp.get("files", []))
            page_token = resp.get("nextPageToken")
            if not page_token:
                break
    except HttpError as e:
        log.error(f"Drive query failed: {e}")
        return []

    return all_files


def download_file(file_id: str, dest_path: str) -> bool:
    """Download a file by ID to a local path."""
    svc = get_drive_service()
    try:
        request = svc.files().get_media(fileId=file_id, supportsAllDrives=True)
        with open(dest_path, "wb") as f:
            downloader = MediaIoBaseDownload(f, request)
            done = False
            while not done:
                _status, done = downloader.next_chunk()
        return True
    except HttpError as e:
        log.error(f"Download {file_id} failed: {e}")
        return False


def upload_file(local_path: str, parent_id: str, name: str) -> str | None:
    """Upload a local file to Drive under the given parent. Returns new file ID."""
    svc = get_drive_service()
    try:
        media = MediaFileUpload(local_path, mimetype="application/pdf", resumable=False)
        metadata = {"name": name, "parents": [parent_id]}
        created = svc.files().create(
            body=metadata,
            media_body=media,
            fields="id,name",
            supportsAllDrives=True,
        ).execute()
        return created.get("id")
    except HttpError as e:
        log.error(f"Upload failed for {name}: {e}")
        return None


def trash_file(file_id: str) -> bool:
    """Move a file to trash."""
    svc = get_drive_service()
    try:
        svc.files().update(
            fileId=file_id,
            body={"trashed": True},
            supportsAllDrives=True,
        ).execute()
        return True
    except HttpError as e:
        log.error(f"Trash {file_id} failed: {e}")
        return False


def _escape_drive_query(s: str) -> str:
    """Escape single quotes for use inside a Drive API v3 query string."""
    return s.replace("\\", "\\\\").replace("'", "\\'")


def file_already_processed(parent_id: str, stem: str) -> bool:
    """Check whether <stem>_p.pdf already exists in the same parent folder."""
    svc = get_drive_service()
    safe_stem = _escape_drive_query(stem)
    try:
        q = (
            f"'{parent_id}' in parents"
            f" and name = '{safe_stem}_p.pdf'"
            " and trashed = false"
        )
        resp = svc.files().list(
            q=q, fields="files(id)", pageSize=1,
            supportsAllDrives=True, includeItemsFromAllDrives=True, corpora="allDrives",
        ).execute()
        return len(resp.get("files", [])) > 0
    except HttpError as e:
        log.warning(f"processed-check failed: {e}")
        return False


def find_processed_subfolder(parent_id: str) -> str | None:
    """Return the ID of the 'Processed' subfolder inside parent_id, or None if absent."""
    cached = _PROCESSED_FOLDER_CACHE.get(parent_id)
    if cached is not None:
        return cached or None   # "" → not found

    svc = get_drive_service()
    try:
        q = (
            f"'{parent_id}' in parents"
            f" and name = '{PROCESSED_SUBFOLDER_NAME}'"
            f" and mimeType = 'application/vnd.google-apps.folder'"
            f" and trashed = false"
        )
        resp = svc.files().list(
            q=q, fields="files(id,name)", pageSize=1,
            supportsAllDrives=True, includeItemsFromAllDrives=True, corpora="allDrives",
        ).execute()
        files = resp.get("files", [])
        if files:
            folder_id = files[0]["id"]
            _PROCESSED_FOLDER_CACHE[parent_id] = folder_id
            return folder_id
    except HttpError as e:
        log.warning(f"find_processed_subfolder failed: {e}")

    _PROCESSED_FOLDER_CACHE[parent_id] = ""
    return None


def get_or_create_processed_subfolder(parent_id: str) -> str | None:
    """Find or create the 'Processed' subfolder inside parent_id. Returns its ID."""
    existing = find_processed_subfolder(parent_id)
    if existing:
        return existing

    svc = get_drive_service()
    try:
        body = {
            "name": PROCESSED_SUBFOLDER_NAME,
            "mimeType": "application/vnd.google-apps.folder",
            "parents": [parent_id],
        }
        created = svc.files().create(
            body=body, fields="id,name", supportsAllDrives=True,
        ).execute()
        folder_id = created["id"]
        _PROCESSED_FOLDER_CACHE[parent_id] = folder_id
        log.info(f"Created '{PROCESSED_SUBFOLDER_NAME}' subfolder (id={folder_id})")
        return folder_id
    except HttpError as e:
        log.error(f"Could not create Processed subfolder: {e}")
        return None


def move_file_to_processed(file_id: str, current_parent_id: str) -> bool:
    """Move file from current_parent_id into current_parent_id's Processed subfolder.
    Creates the Processed subfolder if it doesn't exist. Returns True on success."""
    processed_id = get_or_create_processed_subfolder(current_parent_id)
    if not processed_id:
        return False

    svc = get_drive_service()
    try:
        svc.files().update(
            fileId=file_id,
            addParents=processed_id,
            removeParents=current_parent_id,
            fields="id,parents",
            supportsAllDrives=True,
        ).execute()
        return True
    except HttpError as e:
        log.error(f"Move to Processed failed for {file_id}: {e}")
        return False


# ── Core processing ───────────────────────────────────────────────────────────

def process_one(file_meta: dict, root_title: str, force_size: str | None = None) -> tuple[bool, str]:
    """Download, process with vectorize_v2.py, upload the _p.pdf back to the
    SAME folder as the original, then archive the original into a 'Processed'
    subfolder.

    force_size: override size (SMALL/MED/LAR/AW) — typically derived from the
    folder name during chain walk. Falls back to AW if in an AW root, else
    vectorize_v2's filename-based legacy logic.

    Returns (success, description_for_summary).
    """
    file_id = file_meta["id"]
    name = file_meta["name"]
    stem, _ext = os.path.splitext(name)

    original_meta = resolve_shortcut(file_meta)
    parents = original_meta.get("parents", [])
    if not parents:
        return False, f"{name}: no parent"
    parent_id = parents[0]

    # Skip if <stem>_p.pdf already exists in the same folder
    if file_already_processed(parent_id, stem):
        log.info(f"  {name}: _p.pdf already exists, skipping")
        return False, f"{name}: already processed"

    # Decide size: explicit folder-derived > AW from root > filename legacy (None)
    effective_force_size = force_size
    if effective_force_size is None and root_title in AW_PRINT_ROOTS:
        effective_force_size = "AW"

    with tempfile.TemporaryDirectory() as tmpdir:
        input_path = os.path.join(tmpdir, name)
        if not download_file(file_id, input_path):
            return False, f"{name}: download failed"

        output_path = os.path.join(tmpdir, f"{stem}_p.pdf")
        try:
            process_pdf(input_path, output_path=output_path, dpi=150, force_size=effective_force_size)
        except Exception as e:
            log.error(f"  {name}: processing error: {e}")
            return False, f"{name}: process error: {e}"

        if not os.path.exists(output_path) or os.path.getsize(output_path) == 0:
            return False, f"{name}: output missing"

        size_mb = os.path.getsize(input_path) / 1024 / 1024

        # Upload _p.pdf alongside the original (same parent folder)
        uploaded_id = upload_file(output_path, parent_id, f"{stem}_p.pdf")
        if not uploaded_id:
            return False, f"{name}: upload failed"

    # Archive the original into the Processed subfolder (non-destructive)
    archive_enabled = os.environ.get("ARCHIVE_ORIGINALS", "true").lower() != "false"
    archive_msg = ""
    if archive_enabled:
        if move_file_to_processed(file_id, parent_id):
            archive_msg = " (original → Processed/)"
        else:
            archive_msg = " (original kept, archive move failed)"
    else:
        archive_msg = " (original kept, ARCHIVE_ORIGINALS=false)"

    log.info(f"  ✓ {name} ({size_mb:.1f} MB, size={effective_force_size or 'auto'}) → {stem}_p.pdf{archive_msg}")
    return True, f"{name} → {stem}_p.pdf{archive_msg}"


def cycle() -> dict:
    """One polling cycle. Returns stats dict."""
    now = datetime.now(timezone.utc)
    last_scan = load_last_scan_time()
    since = min(last_scan, now - timedelta(seconds=OVERLAP_SECONDS))

    current_year = now.year
    current_month = now.month

    stats = {
        "found": 0,
        "filtered_in": 0,
        "processed": 0,
        "errors": 0,
        "error_details": [],
        "processed_details": [],
    }

    candidates = find_candidate_files(since)
    stats["found"] = len(candidates)

    # Clear metadata cache at start of each cycle to pick up any folder changes
    _METADATA_CACHE.clear()

    kept = []
    for f in candidates:
        # Re-check name filter client-side (Drive's "not name contains" isn't 100% reliable)
        if f["name"].endswith("_p.pdf"):
            continue

        root_title, force_size, reason = walk_chain(f, current_year, current_month)
        if root_title is None:
            log.debug(f"  skip {f['name']}: {reason}")
            continue
        kept.append((f, root_title, force_size))

    stats["filtered_in"] = len(kept)

    # Cap per cycle to keep things responsive
    if len(kept) > MAX_FILES_PER_CYCLE:
        log.info(f"  cycle cap: processing {MAX_FILES_PER_CYCLE} of {len(kept)} candidates; rest next cycle")
        kept = kept[:MAX_FILES_PER_CYCLE]

    for f, root_title, force_size in kept:
        ok, desc = process_one(f, root_title, force_size=force_size)
        if ok:
            stats["processed"] += 1
            stats["processed_details"].append(desc)
        else:
            stats["errors"] += 1
            stats["error_details"].append(desc)

    # Only advance last_scan if we fully processed everything we wanted to
    if stats["errors"] == 0:
        save_last_scan_time(now)

    return stats


# ── Main loop ─────────────────────────────────────────────────────────────────

def main():
    log.info("=" * 70)
    log.info("Doormat Drive Watcher starting")
    log.info(f"  Poll interval: {POLL_INTERVAL_SECONDS}s")
    log.info(f"  State file:    {STATE_FILE}")
    log.info(f"  Watched roots: {len(ALL_PRINT_ROOTS)} folders")
    log.info("=" * 70)

    # Verify credentials up front (fail fast)
    try:
        svc = get_drive_service()
        svc.about().get(fields="user").execute()
    except Exception as e:
        log.error(f"Drive auth failed: {e}")
        send_summary(
            subject="Doormat Drive Watcher — AUTH FAILED",
            processed=0,
            errors=[f"Auth error: {e}"],
            folders=[],
            body_extra="Check GOOGLE_CREDENTIALS_JSON env var on the VM.",
            silent_if_empty=False,
        )
        sys.exit(1)

    log.info("Drive auth OK; entering poll loop")

    while True:
        try:
            stats = cycle()
            if stats["processed"] > 0 or stats["errors"] > 0:
                log.info(
                    f"cycle: found={stats['found']} kept={stats['filtered_in']}"
                    f" processed={stats['processed']} errors={stats['errors']}"
                )
                # Email summary — only if something actually happened
                body_parts = []
                if stats["processed_details"]:
                    body_parts.append("Processed:\n" + "\n".join(f"  - {d}" for d in stats["processed_details"]))
                if stats["error_details"]:
                    body_parts.append("Errors:\n" + "\n".join(f"  - {d}" for d in stats["error_details"]))
                send_summary(
                    subject="Doormat Drive Watcher",
                    processed=stats["processed"],
                    errors=stats["error_details"],
                    folders=[],
                    body_extra="\n\n".join(body_parts),
                    silent_if_empty=True,
                )
        except KeyboardInterrupt:
            log.info("Interrupted, exiting")
            break
        except Exception as e:
            log.error(f"Cycle failed: {e}\n{traceback.format_exc()}")

        time.sleep(POLL_INTERVAL_SECONDS)


if __name__ == "__main__":
    main()
