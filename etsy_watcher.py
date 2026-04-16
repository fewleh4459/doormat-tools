"""Doormat Etsy Fast Watcher — hourly Google Drive monitor.

Searches for PDFs modified in the last 90 minutes within Etsy folders,
processes them (vectorize B&W / boost colour), uploads results as *_p.pdf,
and deletes originals.

Designed to run as an Anthropic Routine (hourly).

Environment variables:
  GOOGLE_CREDENTIALS    — JSON string containing service account credentials
  GOOGLE_CREDENTIALS_PATH — Path to service account JSON file
  GMAIL_USER            — From address for email notifications
  GMAIL_APP_PASSWORD    — 16-char Gmail app password
  NOTIFY_TO             — Recipient email (defaults to GMAIL_USER)

Usage:
    python etsy_watcher.py                     # Run once
    python etsy_watcher.py --test-search       # Test Google Drive search without processing
"""

import os
import sys
import json
import time
import tempfile
import shutil
from datetime import datetime, timedelta, timezone
from pathlib import Path

try:
    from google.oauth2 import service_account
    from google.auth.transport.requests import Request
    from google.api_core.retry import Retry
    import google.auth
    import googleapiclient.discovery
    import googleapiclient.errors
    import googleapiclient.http
except ImportError:
    print("ERROR: Missing Google API dependencies.")
    print("Install with: pip install google-auth google-auth-oauthlib google-auth-httplib2 google-api-python-client")
    sys.exit(1)

from vectorize_v2 import process_pdf, get_target_size, is_color_pdf
from notify import send_summary


# ── Configuration ─────────────────────────────────────────────────────────────

ETSY_FOLDERS = {
    # Coir mats
    "MO": {"path": "MuckOff (MO)/MO MAT JOB BAGS/MO Print/MO Etsy", "force_size": None},
    "CB": {"path": "Coir Blimey (CB)/_CB MAT JOB BAGS/_CB Print/CB Etsy", "force_size": None},
    "EMA": {"path": "Emagineered (EMA)/_EMA MAT JOB BAGS/EMA Print/EMA Etsy", "force_size": None},
    "CMD": {"path": "Customat (CMD)/_CMD MAT JOB BAGS/CUS Print/CMD Etsy", "force_size": None},
    "YCR": {"path": "Your Custom Rug (YCR)/_YCR Mat Job Bag/YCR Print/YCR Etsy", "force_size": None},
    "2DD": {"path": "2 Day Doormats (2DD)/2DD Coir Mat Job Bag/2DD Print/2DD Etsy", "force_size": None},
    "DD": {"path": "Doormats Direct (DD)/DD Coir Mat Job Bag/DD Print/DD Etsy", "force_size": None},
    # All-weather mats
    "CMD-AW": {"path": "Customat (CMD)/_CMD ALL WEATHER MAT JOB BAGS/CUS AW Print/CMD AW Etsy", "force_size": "AW"},
    "EMA-AW": {"path": "Emagineered (EMA)/EMA ALL WEATHER MAT JOB BAG/EMA AW Print/EMA AW Etsy", "force_size": "AW"},
    "2DD-AW": {"path": "2 Day Doormats (2DD)/2DD Weatherproof Mat Job Bag/2DD AW Print/2DD AW Etsy", "force_size": "AW"},
    "YCR-AW": {"path": "Your Custom Rug (YCR)/_YRC Weatherproof Mat Job Bag/YCR AW Print/YCR AW Etsy", "force_size": "AW"},
    "DD-AW": {"path": "Doormats Direct (DD)/DD Weatherproof Mat Job Bag/DD AW Print/DD AW Etsy", "force_size": "AW"},
}

PARENT_FOLDER = "EMAGINEERED/_New File System 2021"
SEARCH_WINDOW_MINUTES = 90
DPI = 300


# ── Google Drive Auth ─────────────────────────────────────────────────────────

def get_drive_service():
    """Get authenticated Google Drive service from credentials."""
    creds = None

    # Try explicit JSON string first
    creds_json = os.environ.get("GOOGLE_CREDENTIALS")
    if creds_json:
        try:
            creds_dict = json.loads(creds_json)
            creds = service_account.Credentials.from_service_account_info(creds_dict)
        except Exception as e:
            print(f"ERROR parsing GOOGLE_CREDENTIALS: {e}")
            return None

    # Try JSON file path
    if not creds:
        creds_path = os.environ.get("GOOGLE_CREDENTIALS_PATH")
        if creds_path and os.path.exists(creds_path):
            try:
                creds = service_account.Credentials.from_service_account_file(creds_path)
            except Exception as e:
                print(f"ERROR loading GOOGLE_CREDENTIALS_PATH: {e}")
                return None

    # Try Application Default Credentials
    if not creds:
        try:
            creds, _ = google.auth.default()
        except Exception as e:
            print(f"ERROR with Application Default Credentials: {e}")
            return None

    if not creds:
        print("ERROR: No Google credentials found. Set GOOGLE_CREDENTIALS or GOOGLE_CREDENTIALS_PATH.")
        return None

    # Build service
    try:
        service = googleapiclient.discovery.build("drive", "v3", credentials=creds, cache_discovery=False)
        return service
    except Exception as e:
        print(f"ERROR building Drive service: {e}")
        return None


# ── Drive helpers ─────────────────────────────────────────────────────────────

def find_folder_by_path(service, parent_id, path_parts):
    """Find a folder by walking a path, return folder ID or None."""
    current_id = parent_id
    for part in path_parts:
        try:
            q = f"'{current_id}' in parents and name='{part.replace(chr(39), chr(92)+chr(39))}' and mimeType='application/vnd.google-apps.folder' and trashed=false"
            results = service.files().list(q=q, spaces="drive", fields="files(id, name)", pageSize=1).execute()
            files = results.get("files", [])
            if not files:
                return None
            current_id = files[0]["id"]
        except Exception as e:
            print(f"  Error traversing path '{part}': {e}")
            return None
    return current_id


def get_folder_parent_id(service, folder_path):
    """Get the ID of a folder given its path under PARENT_FOLDER."""
    try:
        # Find PARENT_FOLDER
        q = f"name='{PARENT_FOLDER.split('/')[-1]}' and mimeType='application/vnd.google-apps.folder' and trashed=false"
        results = service.files().list(q=q, spaces="drive", fields="files(id)", pageSize=1).execute()
        files = results.get("files", [])
        if not files:
            print(f"  Parent folder '{PARENT_FOLDER}' not found")
            return None

        parent_id = files[0]["id"]

        # Walk the path
        path_parts = folder_path.split("/")
        return find_folder_by_path(service, parent_id, path_parts)
    except Exception as e:
        print(f"  Error finding folder: {e}")
        return None


def get_current_month_name():
    """Get current month name (e.g., 'April 2026')."""
    now = datetime.now(timezone.utc)
    return now.strftime("%B %Y")


def is_current_month_folder(folder_name):
    """Check if folder name is from the current month."""
    current_month = get_current_month_name()
    current_month_abbr = datetime.now(timezone.utc).strftime("%b")
    current_month_num = datetime.now(timezone.utc).strftime("%m")
    year = datetime.now(timezone.utc).strftime("%Y")

    name_upper = folder_name.upper()
    current_month_upper = current_month.upper()
    current_month_abbr_upper = current_month_abbr.upper()

    # Check for past months/years or OLD
    skip_keywords = ["OLD", "TEST", "2024", "2025"]
    if any(kw in name_upper for kw in skip_keywords):
        return False

    # Check for current month matches
    if current_month_upper in name_upper:
        return True
    if current_month_abbr_upper in name_upper:
        return True
    if current_month_num in name_upper and year in name_upper:
        return True

    return False


def search_etsy_pdfs(service):
    """Search for PDFs modified in the last 90 minutes across all Etsy folders.

    Returns: list of dicts with keys: file_id, name, folder_id, folder_name, folder_label, size
    """
    results = []
    cutoff_time = (datetime.now(timezone.utc) - timedelta(minutes=SEARCH_WINDOW_MINUTES)).isoformat()

    for label, folder_config in ETSY_FOLDERS.items():
        folder_path = folder_config["path"]
        print(f"  Searching {label}...")

        # Get folder ID
        folder_id = get_folder_parent_id(service, folder_path)
        if not folder_id:
            print(f"    Folder not found: {folder_path}")
            continue

        try:
            # Search for PDFs in this folder AND its subfolders (modified recently, not _p)
            q = (
                f"'{folder_id}' in parents and "
                f"mimeType='application/pdf' and "
                f"modifiedTime > '{cutoff_time}' and "
                f"not name contains '_p.pdf' and "
                f"trashed=false"
            )

            request = service.files().list(
                q=q,
                spaces="drive",
                fields="files(id, name, parents, modifiedTime, size)",
                pageSize=100,
            )

            while request:
                response = request.execute()
                files = response.get("files", [])

                for file in files:
                    # Check if it's in a current-month subfolder
                    file_id = file.get("id")
                    file_name = file.get("name")

                    # Get parent folder to extract month
                    parent_ids = file.get("parents", [])
                    if parent_ids:
                        parent_id = parent_ids[0]
                        parent_info = service.files().get(fields="name", fileId=parent_id).execute()
                        parent_name = parent_info.get("name", "")

                        # Check if parent is current month subfolder
                        if is_current_month_folder(parent_name):
                            results.append({
                                "file_id": file_id,
                                "name": file_name,
                                "folder_id": folder_id,
                                "folder_label": label,
                                "month_folder": parent_name,
                                "size": int(file.get("size", 0)),
                            })

                # Check for next page
                request = service.files().list_next(request, response)

        except googleapiclient.errors.HttpError as e:
            print(f"    API error searching {label}: {e}")
        except Exception as e:
            print(f"    Error searching {label}: {e}")

    return results


# ── Processing ────────────────────────────────────────────────────────────────

def download_file(service, file_id, filename):
    """Download a file from Drive to a temp path. Return file path or None."""
    try:
        request = service.files().get_media(fileId=file_id)
        with tempfile.NamedTemporaryFile(prefix="etsy_", suffix=".pdf", delete=False) as f:
            downloader = googleapiclient.http.MediaIoBaseDownload(f, request)
            done = False
            while not done:
                status, done = downloader.next_chunk()
            return f.name
    except Exception as e:
        print(f"    ERROR downloading '{filename}': {e}")
        return None


def upload_file(service, local_path, filename, folder_id):
    """Upload a file to Drive folder. Return file ID or None."""
    try:
        file_metadata = {
            "name": filename,
            "parents": [folder_id],
        }
        media = googleapiclient.http.MediaFileUpload(local_path, mimetype="application/pdf")
        file = service.files().create(body=file_metadata, media_body=media, fields="id").execute()
        return file.get("id")
    except Exception as e:
        print(f"    ERROR uploading '{filename}': {e}")
        return None


def delete_file(service, file_id):
    """Move a file to trash."""
    try:
        service.files().delete(fileId=file_id).execute()
        return True
    except Exception as e:
        print(f"    ERROR deleting file {file_id}: {e}")
        return False


def process_file(service, file_info, folder_config):
    """Download, process, upload, and delete. Return (success, error_msg)."""
    file_id = file_info["file_id"]
    file_name = file_info["name"]
    folder_id = file_info["folder_id"]
    folder_label = file_info["folder_label"]
    force_size = folder_config["force_size"]

    print(f"  Processing: {file_name}")

    # Download
    local_path = download_file(service, file_id, file_name)
    if not local_path:
        return False, f"{file_name}: download failed"

    try:
        # Process
        output_base = os.path.splitext(file_name)[0]
        output_name = f"{output_base}_p.pdf"
        output_path = os.path.join(tempfile.gettempdir(), output_name)

        try:
            process_pdf(local_path, output_path=output_path, dpi=DPI, force_size=force_size)
        except Exception as e:
            return False, f"{file_name}: processing failed — {e}"

        if not os.path.exists(output_path) or os.path.getsize(output_path) == 0:
            return False, f"{file_name}: output file is empty"

        # Upload
        file_id_result = upload_file(service, output_path, output_name, folder_id)
        if not file_id_result:
            return False, f"{file_name}: upload failed"

        # Delete original
        if not delete_file(service, file_id):
            return False, f"{file_name}: deletion of original failed"

        print(f"    ✓ {output_name}")
        return True, None

    finally:
        # Clean up temp files
        if os.path.exists(local_path):
            try:
                os.remove(local_path)
            except:
                pass
        output_path = os.path.join(tempfile.gettempdir(), f"{os.path.splitext(file_name)[0]}_p.pdf")
        if os.path.exists(output_path):
            try:
                os.remove(output_path)
            except:
                pass


# ── Main ──────────────────────────────────────────────────────────────────────

def run_once():
    """Run the watcher once."""
    print(f"[{datetime.now().isoformat()}] Doormat Etsy Watcher started")

    # Get Drive service
    service = get_drive_service()
    if not service:
        print("FATAL: Could not authenticate with Google Drive")
        return False

    # Search for files
    print("Searching for recent Etsy PDFs...")
    files = search_etsy_pdfs(service)

    if not files:
        print("No new Etsy files since last check.")
        send_summary("Doormat Etsy Watcher", processed=0, errors=[])
        return True

    print(f"Found {len(files)} file(s) to process")

    # Process files
    processed = 0
    errors = []
    active_folders = set()

    for file_info in files:
        folder_label = file_info["folder_label"]
        month_folder = file_info["month_folder"]
        active_folders.add(f"{folder_label} Etsy/{month_folder}")

        folder_config = ETSY_FOLDERS[folder_label]
        success, error_msg = process_file(service, file_info, folder_config)

        if success:
            processed += 1
        else:
            errors.append(error_msg)

    # Report
    print()
    if errors:
        print(f"Processed {processed} files. Errors: {len(errors)}")
        for e in errors:
            print(f"  - {e}")
    else:
        print(f"Processed {processed} files. All OK.")

    # Send summary email
    send_summary(
        subject="Doormat Etsy Watcher",
        processed=processed,
        errors=errors,
        folders=sorted(active_folders),
    )

    return len(errors) == 0


if __name__ == "__main__":
    if "--test-search" in sys.argv:
        service = get_drive_service()
        if not service:
            print("Could not authenticate")
            sys.exit(1)
        print("Testing folder search...")
        files = search_etsy_pdfs(service)
        print(f"Found {len(files)} file(s)")
        for f in files:
            print(f"  {f['folder_label']}: {f['name']} ({f['month_folder']})")
        sys.exit(0)

    success = run_once()
    sys.exit(0 if success else 1)
