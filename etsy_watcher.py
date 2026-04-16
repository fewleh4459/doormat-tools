"""
Doormat Etsy Fast Watcher — Routine for Google Cloud Functions.

Monitors Etsy folders on Google Drive for new personalised designs (hourly).
Uses efficient Drive API queries with modifiedTime filtering.

Flow:
1. Search Drive for PDFs modified in last 90 minutes
2. Client-side filter: keep only Etsy folders + current month subfolders
3. Skip files that already have a _p.pdf counterpart
4. For each matched file:
   - Download to local workspace
   - Determine coir vs AW from path
   - Process with vectorize_v2.py
   - Upload output back to Drive as <name>_p.pdf
   - Delete original from Drive
5. Email summary

Environment variables (set in Routine):
  GOOGLE_DRIVE_ROOT_ID    — ID of EMAGINEERED/_New File System 2021/
  GOOGLE_APPLICATION_CREDENTIALS — path to service account JSON (Cloud Function auto-provides)
  GMAIL_USER, GMAIL_APP_PASSWORD, NOTIFY_TO — email config (see notify.py)
"""

import os
import sys
import json
import time
import tempfile
import shutil
from datetime import datetime, timedelta
from pathlib import Path

from google.auth.transport.requests import Request
from google.oauth2.service_account import Credentials
from google.auth.exceptions import GoogleAuthError
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError
from googleapiclient.http import MediaFileUpload, MediaIoBaseDownload

from vectorize_v2 import process_pdf

# Try to import notify for email summaries
try:
    from notify import send_summary
except ImportError:
    send_summary = None


# ── Configuration ──────────────────────────────────────────────────────────────

# Etsy folder patterns (must match path in Google Drive)
COIR_ETSY_PATTERNS = [
    "MO Print/MO Etsy",
    "_CB Print/CB Etsy",
    "EMA Print/EMA Etsy",
    "CUS Print/CMD Etsy",
    "YCR Print/YCR Etsy",
    "2DD Print/2DD Etsy",
    "DD Print/DD Etsy",
]

AW_ETSY_PATTERNS = [
    "CUS AW Print/CMD AW Etsy",
    "EMA AW Print/EMA AW Etsy",
    "2DD AW Print/2DD AW Etsy",
    "YCR AW Print/YCR AW Etsy",
    "DD AW Print/DD AW Etsy",
]

ALL_ETSY_PATTERNS = COIR_ETSY_PATTERNS + AW_ETSY_PATTERNS

# Skip rules
SKIP_FOLDERS = {"OLD", "TEST", "print-tests", "_RichBlack"}


# ── Google Drive API ───────────────────────────────────────────────────────────

def get_drive_service():
    """Authenticate and return Google Drive service."""
    try:
        credentials = Credentials.from_service_account_file(
            os.environ.get("GOOGLE_APPLICATION_CREDENTIALS", ""),
            scopes=["https://www.googleapis.com/auth/drive"],
        )
        return build("drive", "v3", credentials=credentials)
    except (GoogleAuthError, FileNotFoundError, json.JSONDecodeError) as e:
        print(f"[ERROR] Failed to authenticate with Google Drive: {e}")
        return None


def search_recent_pdfs(service, minutes=90):
    """Search for PDFs modified in the last N minutes.

    Returns a list of (file_id, name, parents) tuples.
    """
    timestamp = (datetime.utcnow() - timedelta(minutes=minutes)).isoformat() + "Z"
    query = (
        f"modifiedTime > '{timestamp}' "
        f"AND mimeType = 'application/pdf' "
        f"AND not name contains '_p.pdf'"
    )

    all_files = []
    page_token = None

    try:
        while True:
            results = service.files().list(
                q=query,
                spaces="drive",
                fields="files(id, name, parents, fullFileExtension), nextPageToken",
                pageSize=100,
                pageToken=page_token,
            ).execute()

            all_files.extend(results.get("files", []))
            page_token = results.get("nextPageToken")

            if not page_token:
                break
    except HttpError as e:
        print(f"[ERROR] Failed to search Drive: {e}")
        return []

    return all_files


def get_file_path(service, file_id, root_folder_id):
    """Reconstruct the full path of a file from its ID.

    Returns path relative to root_folder_id (e.g. "Brand/Brand Etsy/April 2026/design.pdf")
    """
    path_parts = []
    current_id = file_id

    # Walk up the parent chain until we reach the root
    while current_id != root_folder_id and current_id:
        try:
            file = service.files().get(
                fileId=current_id,
                fields="id, name, parents",
            ).execute()

            path_parts.insert(0, file["name"])

            # Get parent
            parents = file.get("parents", [])
            current_id = parents[0] if parents else None

        except HttpError as e:
            print(f"[WARNING] Could not get path for {file_id}: {e}")
            return None

    return "/".join(path_parts) if path_parts else None


def get_month_from_path(path):
    """Extract month name/number from a path.

    Returns month name (e.g. 'April', 'Apr', '04') if found, None otherwise.
    """
    if not path:
        return None

    # Current month names in various formats
    import calendar
    month_num = datetime.now().month
    month_name_full = calendar.month_name[month_num]  # e.g. 'April'
    month_name_short = calendar.month_abbr[month_num]  # e.g. 'Apr'
    month_num_str = f"{month_num:02d}"
    year = datetime.now().year

    path_upper = path.upper()

    if (month_name_full.upper() in path_upper or
        month_name_short.upper() in path_upper or
        month_num_str in path or
        str(year) in path):
        return month_name_full

    return None


def is_current_month_etsy(path):
    """Check if a file path is in an Etsy folder with current month subfolder.

    Returns (is_etsy, is_coir, is_aw, relative_path_in_etsy)
    """
    if not path:
        return False, False, False, None

    # Check skip rules
    for skip_word in SKIP_FOLDERS:
        if skip_word in path:
            return False, False, False, None

    # Check if it's in an Etsy folder
    is_coir = any(pattern in path for pattern in COIR_ETSY_PATTERNS)
    is_aw = any(pattern in path for pattern in AW_ETSY_PATTERNS)

    if not (is_coir or is_aw):
        return False, False, False, None

    # Check if in current month subfolder
    if not get_month_from_path(path):
        return False, False, False, None

    # This is a valid current-month Etsy file
    return True, is_coir, is_aw, path


def download_file(service, file_id, output_path):
    """Download a file from Drive to local path."""
    try:
        request = service.files().get_media(fileId=file_id)
        with open(output_path, "wb") as f:
            downloader = MediaIoBaseDownload(f, request)
            done = False
            while not done:
                _, done = downloader.next_chunk()
        return True
    except HttpError as e:
        print(f"[ERROR] Failed to download file {file_id}: {e}")
        return False


def upload_file(service, file_path, parent_id, new_name):
    """Upload a file to Drive in the specified parent folder.

    Returns file_id on success, None on failure.
    """
    try:
        file_metadata = {
            "name": new_name,
            "parents": [parent_id],
        }
        media = MediaFileUpload(file_path, mimetype="application/pdf", resumable=True)
        file = (
            service.files()
            .create(body=file_metadata, media_body=media, fields="id")
            .execute()
        )
        return file.get("id")
    except HttpError as e:
        print(f"[ERROR] Failed to upload {new_name}: {e}")
        return None


def delete_file(service, file_id):
    """Delete a file from Drive."""
    try:
        service.files().delete(fileId=file_id).execute()
        return True
    except HttpError as e:
        print(f"[ERROR] Failed to delete file {file_id}: {e}")
        return False


def has_processed_counterpart(service, parent_id, original_name):
    """Check if a _p.pdf version of this file already exists in the same folder."""
    name_no_ext = os.path.splitext(original_name)[0]
    counterpart_name = f"{name_no_ext}_p.pdf"

    try:
        results = service.files().list(
            q=f"'{parent_id}' in parents AND name = '{counterpart_name}'",
            spaces="drive",
            fields="files(id)",
            pageSize=1,
        ).execute()

        return len(results.get("files", [])) > 0
    except HttpError as e:
        print(f"[WARNING] Could not check for counterpart {counterpart_name}: {e}")
        return False


# ── Processing ─────────────────────────────────────────────────────────────────

def process_single_file(service, file_id, filename, parent_id, is_aw):
    """Download, process, upload, and delete a single file.

    Returns (success: bool, error_msg: str or None)
    """
    # Check for existing _p.pdf
    if has_processed_counterpart(service, parent_id, filename):
        return False, f"{filename} — _p.pdf already exists"

    # Create temp workspace
    workspace = tempfile.mkdtemp(prefix="doormat_etsy_")

    try:
        # Download
        input_path = os.path.join(workspace, filename)
        if not download_file(service, file_id, input_path):
            return False, f"{filename} — download failed"

        # Process
        output_path = os.path.join(workspace, f"{os.path.splitext(filename)[0]}_p.pdf")
        try:
            force_size = "AW" if is_aw else None
            process_pdf(input_path, output_path=output_path, dpi=300, force_size=force_size)
        except Exception as e:
            return False, f"{filename} — processing error: {e}"

        if not os.path.exists(output_path):
            return False, f"{filename} — no output generated"

        # Upload
        output_name = os.path.basename(output_path)
        if not upload_file(service, output_path, parent_id, output_name):
            return False, f"{filename} — upload failed"

        # Delete original
        if not delete_file(service, file_id):
            return False, f"{filename} — deletion failed"

        return True, None

    except Exception as e:
        return False, f"{filename} — unexpected error: {e}"

    finally:
        # Clean up workspace
        shutil.rmtree(workspace, ignore_errors=True)


# ── Main routine ───────────────────────────────────────────────────────────────

def run():
    """Main Etsy watcher routine."""
    service = get_drive_service()
    if not service:
        print("[FATAL] Could not authenticate with Google Drive")
        return

    root_id = os.environ.get("GOOGLE_DRIVE_ROOT_ID")
    if not root_id:
        print("[FATAL] GOOGLE_DRIVE_ROOT_ID not set")
        return

    print("=" * 70)
    print("Doormat Etsy Fast Watcher")
    print(f"Time: {datetime.now().isoformat()}")
    print("=" * 70)

    # Search for recent PDFs
    all_pdfs = search_recent_pdfs(service, minutes=90)
    print(f"\nFound {len(all_pdfs)} PDFs modified in last 90 minutes")

    if not all_pdfs:
        print("\n[SUMMARY] No new Etsy files since last check.")
        return

    # Filter to current-month Etsy files
    etsy_files = []
    for pdf in all_pdfs:
        file_id = pdf["id"]
        filename = pdf["name"]
        parent_id = pdf.get("parents", [None])[0]

        # Get full path
        path = get_file_path(service, file_id, root_id)

        # Check if it's a current-month Etsy file
        is_valid, is_coir, is_aw, _ = is_current_month_etsy(path)

        if is_valid:
            etsy_files.append({
                "id": file_id,
                "name": filename,
                "parent_id": parent_id,
                "path": path,
                "is_aw": is_aw,
            })

    print(f"Filtered to {len(etsy_files)} Etsy files")

    if not etsy_files:
        print("\n[SUMMARY] No new Etsy files since last check.")
        return

    # Process files
    processed_count = 0
    error_count = 0
    errors = []
    folders_with_activity = set()

    for file_info in etsy_files:
        print(f"\nProcessing: {file_info['name']}")
        print(f"  Path: {file_info['path']}")

        success, error_msg = process_single_file(
            service,
            file_info["id"],
            file_info["name"],
            file_info["parent_id"],
            file_info["is_aw"],
        )

        if success:
            processed_count += 1
            # Extract folder path (up to Etsy folder)
            path_parts = file_info["path"].split("/")
            if len(path_parts) >= 3:
                folder = "/".join(path_parts[:-1])
                folders_with_activity.add(folder)
            print(f"  ✓ Processed")
        else:
            error_count += 1
            errors.append(error_msg)
            print(f"  ✗ {error_msg}")

    # Summary
    print("\n" + "=" * 70)
    print(f"[SUMMARY] Processed {processed_count} files across {len(folders_with_activity)} Etsy folders.")
    if errors:
        print(f"Errors: {error_count}")
    print("=" * 70)

    # Send email if notify is available
    if send_summary:
        try:
            send_summary(
                subject="Doormat Etsy Watcher",
                processed=processed_count,
                errors=errors,
                folders=sorted(folders_with_activity),
            )
        except Exception as e:
            print(f"[WARNING] Failed to send email: {e}")


if __name__ == "__main__":
    run()
