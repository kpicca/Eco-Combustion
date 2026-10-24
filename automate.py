#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
This script automates the *entire* daily WEMOB workflow:

High-level steps per ZIP file in Google Drive LogsWemob:
--------------------------------------------------------
1) Download ZIP to a temporary directory.
2) Clean tests/ and extract ZIP contents into tests/.
3) Verify that exactly 24 .log files are present.
4) Run detec.py on tests/ to generate output files.
5) Create a review folder in Google Drive named like 'nov 5'.
6) Upload summary.txt, tag_counts.csv, unique_tags.txt.
7) Create and upload a manifest JSON for auditability.
8) Wipe tests/ again before next run.

Uses OAuth desktop flow stored in data/state/token.json.
"""

import argparse
import io
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from datetime import datetime, timezone, timedelta
from pathlib import Path
from zipfile import ZipFile

# Google API imports
from googleapiclient.discovery import build
from googleapiclient.http import MediaIoBaseDownload, MediaFileUpload
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from google.auth.transport.requests import Request

# ---------- CONFIG DEFAULTS ----------

# Google Drive folder IDs (fixed for your folder structure)
LOGSWEMOB_ID_DEFAULT = "1iPJ_a0QrcrLyrxfX6UEl9QLoMdKmnSp8"
REVIEWS_ID_DEFAULT   = "1rbMN-lxuY7DnytpC7p3vtjtpNYpbmGN7"

# The repo root is the folder containing this script
REPO_ROOT    = Path(__file__).resolve().parent

# Key working paths
TESTS_DIR    = REPO_ROOT / "tests"
PROCESSED_DIR= REPO_ROOT / "data" / "processed"
STATE_DIR    = REPO_ROOT / "data" / "state"
STATE_DIR.mkdir(parents=True, exist_ok=True)

LOG_FILE     = STATE_DIR / "last_run.log"   # Log output persists between runs

EXPECTED_LOG_COUNT = 24   # Daily ZIP bundles must contain exactly 24 logs

# Output files produced by detec.py
OUTPUT_FILES = [
    PROCESSED_DIR / "summary.txt",
    PROCESSED_DIR / "tag_counts.csv",
    PROCESSED_DIR / "unique_tags.txt",
    PROCESSED_DIR / "plain_tag_counts.csv",
]

OPTIONAL_OUTPUT_FILES = [
    PROCESSED_DIR / f
    for f in [
        "wemob_metrics_unknown_date.csv",
        *[p.name for p in PROCESSED_DIR.glob("wemob_metrics_*.csv")],
        "gross_weights.csv",
    ]
]

# Month labels for naming review subfolders
MONTHS = ["jan","feb","mar","apr","may","jun","jul","aug","sept","oct","nov","dec"]

# Regex to extract YYYY-MM-DD from ZIP filenames
ZIP_NAME_DATE_RE = re.compile(r".*?(\d{4})[-_](\d{2})[-_](\d{2}).*", re.IGNORECASE)
# ------------------------------------


def log(msg: str):
    """Writes messages both to console and to last_run.log with timestamps."""
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    line = f"[{ts}] {msg}"
    print(line)
    with LOG_FILE.open("a", encoding="utf-8") as f:
        f.write(line + "\n")


SCOPES = ["https://www.googleapis.com/auth/drive"]


def drive_service_from_env():
    """
    Returns an authenticated Google Drive service.

    Behavior:
    - First checks if an OAuth token exists in data/state/token.json.
    - If expired, attempts refresh.
    - If no valid token, triggers full browser login (InstalledAppFlow).
    - Saves or updates token.json automatically.
    """
    token_path = STATE_DIR / "token.json"
    creds = None

    if token_path.exists():
        creds = Credentials.from_authorized_user_file(token_path, SCOPES)

    # Refresh or re-login if token invalid or missing
    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
        else:
            client_secret = REPO_ROOT / "creds" / "client_secret.json"
            if not client_secret.exists():
                raise RuntimeError(f"Missing OAuth client file: {client_secret}")

            # Browser-based login
            flow = InstalledAppFlow.from_client_secrets_file(
                str(client_secret),
                SCOPES
            )
            creds = flow.run_local_server(port=0)

        # Save token for next run
        token_path.write_text(creds.to_json(), encoding="utf-8")

    return build("drive", "v3", credentials=creds, cache_discovery=False)



def drive_search(service, q, page_size=100):
    """
    Generic Drive search iterator.
    - Accepts a Drive 'q' query string.
    - Yields matching file objects.
    - Handles pagination automatically.
    """
    page_token = None
    while True:
        resp = service.files().list(
            q=q,
            corpora="allDrives",
            fields="nextPageToken, files(id,name,mimeType,modifiedTime,parents,driveId)",
            pageToken=page_token,
            pageSize=page_size,
            supportsAllDrives=True,
            includeItemsFromAllDrives=True,
        ).execute()

        for f in resp.get("files", []):
            yield f

        page_token = resp.get("nextPageToken")
        if not page_token:
            break


def list_zip_files_in_folder(service, folder_id):
    """Returns all ZIP files inside a Google Drive folder."""
    q = f"'{folder_id}' in parents and trashed=false"
    return [
        f for f in drive_search(service, q)
        if f["name"].lower().endswith(".zip")
    ]


def ensure_folder(service, parent_id, name):
    """
    Creates a folder inside another folder.
    If it already exists, returns that ID instead of creating a new one.
    """
    q = (
        f"'{parent_id}' in parents and trashed=false and "
        f"name='{name}' and mimeType='application/vnd.google-apps.folder'"
    )
    existing = list(drive_search(service, q))
    if existing:
        return existing[0]["id"]

    meta = {
        "name": name,
        "mimeType": "application/vnd.google-apps.folder",
        "parents": [parent_id]
    }
    created = service.files().create(body=meta, fields="id").execute()
    return created["id"]


def folder_exists(service, parent_id, name):
    """Checks if a Drive folder exists by name under parent."""
    q = (
        f"'{parent_id}' in parents and trashed=false and "
        f"name='{name}' and mimeType='application/vnd.google-apps.folder'"
    )
    return any(drive_search(service, q))


def download_file(service, file_id, dest_path: Path):
    """Downloads a Drive file by file_id to a local path."""
    dest_path.parent.mkdir(parents=True, exist_ok=True)
    request = service.files().get_media(fileId=file_id)
    fh = io.FileIO(dest_path, "wb")
    downloader = MediaIoBaseDownload(fh, request)

    done = False
    while not done:
        status, done = downloader.next_chunk()
    fh.close()


def upload_simple(service, local_path: Path, parent_id: str, mime=None):
    """Uploads a file to Google Drive under a specific parent folder."""
    body = {"name": local_path.name, "parents": [parent_id]}
    media = MediaFileUpload(local_path.as_posix(), mimetype=mime, resumable=True)
    service.files().create(
        body=body,
        media_body=media,
        fields="id"
    ).execute()


def wipe_tests_dir(tests_dir: Path):
    """
    Cleans tests/ directory before each ZIP extraction.
    Defensive check ensures we NEVER accidentally wipe a wrong folder.
    """
    tests_dir.mkdir(parents=True, exist_ok=True)

    if tests_dir.name != "tests":
        raise RuntimeError("Refusing to wipe non-tests dir.")

    for child in tests_dir.iterdir():
        try:
            if child.is_file():
                child.unlink(missing_ok=True)
            elif child.is_dir():
                shutil.rmtree(child, ignore_errors=True)
        except Exception as e:
            log(f"  WARN: could not remove {child}: {e}")


def parse_date_from_filename(name: str):
    """
    Extracts a date from a filename using ZIP_NAME_DATE_RE.
    If filename doesn't contain a date, returns None.
    """
    m = ZIP_NAME_DATE_RE.match(name)
    if not m:
        return None
    y, mo, d = map(int, m.groups())
    try:
        return datetime(y, mo, d, tzinfo=timezone.utc)
    except Exception:
        return None


def month_day_label(dt: datetime):
    """Returns labels like 'nov 5' for use in review folder names."""
    return f"{MONTHS[dt.month - 1]} {dt.day}"


def derive_date_for_zip(zip_file: dict):
    """
    Determines the correct date for a ZIP using:
    1) Filename
    2) ModifiedTime
    3) Current date (fallback)

    Also returns a 'friendly' standardized ZIP filename.
    """
    name = zip_file["name"]

    # Attempt 1: Filename pattern
    dt = parse_date_from_filename(name)
    if dt:
        return dt, f"WEMOB_{dt.year:04d}_{dt.month:02d}_{dt.day:02d}.zip"

    # Attempt 2: modifiedTime metadata
    mt = zip_file.get("modifiedTime")
    if mt:
        try:
            dt = datetime.fromisoformat(mt.replace("Z", "+00:00"))
            friendly = f"WEMOB_{dt.year:04d}_{dt.month:02d}_{dt.day:02d}.zip"
            return dt, friendly
        except Exception:
            pass

    # Fallback if all else fails
    now = datetime.now(timezone.utc)
    return now, f"WEMOB_{now.year:04d}_{now.month:02d}_{now.day:02d}.zip"


def should_consider(zip_dt: datetime, since_dt):
    """Determines if a ZIP is recent enough based on --since."""
    return True if since_dt is None else (zip_dt >= since_dt)


def count_logs_recursive(path: Path):
    """Counts all .log files recursively under a directory."""
    return sum(1 for _ in path.rglob("*.log"))


def run_detec(repo_root: Path):
    """
    Runs detec.py as a child process.

    Uses:
        python detec.py tests -r
    Ensures recursive scanning of logs.
    """
    cmd = [sys.executable, "detec.py", "tests", "-r"]
    proc = subprocess.run(cmd, cwd=repo_root, capture_output=True, text=True)

    if proc.returncode != 0:
        # Surface stdout/stderr as a single RuntimeError
        raise RuntimeError(
            f"detec.py failed:\nSTDOUT:\n{proc.stdout}\nSTDERR:\n{proc.stderr}"
        )

    return proc.stdout, proc.stderr


def main():
    # Command-line arguments that control filtering, force mode, etc.
    ap = argparse.ArgumentParser(description="Wemob Drive automation")
    ap.add_argument("--logs-id", default=LOGSWEMOB_ID_DEFAULT)
    ap.add_argument("--reviews-id", default=REVIEWS_ID_DEFAULT)
    ap.add_argument("--since", default=None)     # YYYY-MM-DD or Nd
    ap.add_argument("--only", default=None)      # exact names only
    ap.add_argument("--match", default=None)     # substring/regex match
    ap.add_argument("--limit", type=int, default=None)
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--force", action="store_true")
    args = ap.parse_args()

    # Reset last_run.log
    LOG_FILE.write_text("", encoding="utf-8")
    log("Starting wemob pipeline")

    # Build authenticated Drive service
    service = drive_service_from_env()

    # Handle --since argument
    since_dt = None
    if args.since:
        if re.fullmatch(r"\d{4}-\d{2}-\d{2}", args.since):
            # Direct date
            since_dt = datetime.fromisoformat(args.since).replace(tzinfo=timezone.utc)
        elif re.fullmatch(r"\d+d", args.since):
            # N days ago
            days = int(args.since[:-1])
            since_dt = datetime.now(timezone.utc) - timedelta(days=days)
        else:
            log(f"Invalid --since format: {args.since}")
            sys.exit(2)

    # List all ZIPs in LogsWemob
    zips = list_zip_files_in_folder(service, args.logs_id)
    log(f"Found {len(zips)} .zip file(s) in LogsWemob")

    # Apply filters: --only, --match, --since
    only_set = set(s.strip() for s in args.only.split(",")) if args.only else set()
    selected = []

    for f in zips:
        name = f["name"]

        if only_set and name not in only_set:
            continue

        if args.match:
            try:
                if not re.search(args.match, name):
                    continue
            except re.error:
                # If regex fails, treat as plain substring match
                if args.match not in name:
                    continue

        dt, friendly_name = derive_date_for_zip(f)
        if not should_consider(dt, since_dt):
            continue

        selected.append((f, dt, friendly_name))

    # Sort from oldest to newest
    selected.sort(key=lambda t: t[1])

    if args.limit:
        selected = selected[:args.limit]

    log(f"Selected {len(selected)} zip(s) after filtering")

    # Main ZIP processing loop
    for idx, (zf, dt, friendly_name) in enumerate(selected, start=1):
        mon_day = month_day_label(dt)   # e.g., 'nov 5'
        name = zf["name"]; file_id = zf["id"]

        log(f"[{idx}/{len(selected)}] Considering '{name}' ⇒ reviews/{mon_day}")

        # Skip if review folder already exists (unless forced)
        if not args.force and folder_exists(service, args.reviews_id, mon_day):
            log(f"  SKIP: reviews/{mon_day} already exists")
            continue

        # Dry-run: simulation only
        if args.dry_run:
            log("  DRY-RUN only: would download/validate/run/upload")
            continue

        # Download ZIP into a temp directory
        with tempfile.TemporaryDirectory() as td:
            temp_zip = Path(td) / name
            log("  Downloading zip…")
            download_file(service, file_id, temp_zip)

            # Clean out tests/
            log("  Wiping tests/ …")
            wipe_tests_dir(TESTS_DIR)

            # Extract ZIP into tests/
            log("  Extracting to tests/ …")
            with ZipFile(temp_zip, "r") as z:
                z.extractall(TESTS_DIR)

        # Validate log count
        log_count = count_logs_recursive(TESTS_DIR)
        if log_count != EXPECTED_LOG_COUNT:
            log(f"  ERROR: expected {EXPECTED_LOG_COUNT}, found {log_count}. Skipping.")
            wipe_tests_dir(TESTS_DIR)
            continue

        # Run detec.py to generate processed outputs
        log("  Running detec.py …")
        try:
            out, err = run_detec(REPO_ROOT)
        except RuntimeError as e:
            log(str(e))
            wipe_tests_dir(TESTS_DIR)
            continue

        # Ensure core outputs exist
        missing = [p for p in OUTPUT_FILES if not p.exists()]
        if missing:
            log(f"  ERROR: Missing CORE outputs: {missing}")
            wipe_tests_dir(TESTS_DIR)
            continue

        # Add optional metrics files if present
        files_to_upload = OUTPUT_FILES.copy()
        for p in OPTIONAL_OUTPUT_FILES:
            if p.exists():
                files_to_upload.append(p)


        # Ensure the appropriate reviews folder exists
        log("  Ensuring reviews subfolder exists…")
        reviews_sub_id = ensure_folder(service, args.reviews_id, mon_day)

        # Upload processed outputs to Drive
        log("  Uploading outputs…")
        mimes = {
            "summary.txt": "text/plain",
            "tag_counts.csv": "text/csv",
            "unique_tags.txt": "text/plain",
        }
        for p in files_to_upload:
            mime = "text/csv" if p.suffix == ".csv" else "text/plain"
            upload_simple(service, p, reviews_sub_id, mime=mime)


        # Write and upload manifest.json (audit metadata)
        manifest = {
            "zip_name_actual": name,
            "zip_name_friendly": friendly_name,
            "zip_id": file_id,
            "processed_at": datetime.now().isoformat(timespec="seconds"),
            "review_folder": mon_day,
            "outputs": [p.name for p in OUTPUT_FILES],
            "detec_cmd": "python detec.py tests -r",
            "logs_found": log_count,
        }
        man_local = STATE_DIR / f"manifest_{mon_day.replace(' ','_')}.json"
        man_local.write_text(json.dumps(manifest, indent=2), encoding="utf-8")
        upload_simple(service, man_local, reviews_sub_id, mime="application/json")

        log(f"  DONE: Uploaded outputs to reviews/{mon_day}")

        # Clean tests/ before next ZIP
        log("  Final cleanup: wiping tests/")
        wipe_tests_dir(TESTS_DIR)

    log("All done.")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as ex:
        print(f"[FATAL] {ex}", file=sys.stderr)
        sys.exit(1)
