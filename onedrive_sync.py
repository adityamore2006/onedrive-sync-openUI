"""
OneDrive <-> Open WebUI Knowledge Base Two-Way Sync

Keeps your knowledge base in perfect sync with your OneDrive folder.
- New files in OneDrive -> uploaded to KB
- Deleted files from OneDrive -> removed from KB
- Changed files -> re-uploaded to KB
- Runs a full reconciliation every 5 minutes as a safety net

SETUP:
1. pip install requests watchdog
2. Fill in your API key and folder/KB mappings below
3. Run: python onedrive_sync.py
4. (Optional) Add to Task Scheduler to run on boot

TO ADD MORE FOLDERS: Add entries to FOLDER_KB_MAP
"""

import os
import sys
import json
import time
import hashlib
import logging
import requests
import threading
from pathlib import Path
from watchdog.observers import Observer
from watchdog.events import FileSystemEventHandler

# ──────────────────────────────────────────────────────────────
# CONFIG - Fill these in before running
# ──────────────────────────────────────────────────────────────

# Base URL of your Open WebUI instance
OPEN_WEBUI_URL = "http://localhost:3000"

# Your Open WebUI API key
# Generate one at: Open WebUI -> Settings -> Account -> API Keys
API_KEY = "your-api-key-here"

# Map each local folder to its corresponding Knowledge Base ID.
# The KB ID is the UUID in the URL when you open a knowledge base in Open WebUI.
# Example: http://localhost:3000/workspace/knowledge/c0709e6d-1739-4cab-a955-661eeb46eadf
#                                                     ^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^
#                                                     This is your KB ID
FOLDER_KB_MAP = {
    r"C:\Users\YourUser\OneDrive\Your Folder": "your-kb-id-here",
    # Add more mappings as needed:
    # r"C:\Users\YourUser\OneDrive\Another Folder": "another-kb-id-here",
}

# File types that will be synced (must be supported by Open WebUI)
ALLOWED_EXTENSIONS = {".pdf", ".txt", ".md", ".docx", ".csv", ".pptx"}

# Path where sync state is persisted between runs (auto-created next to this script)
STATE_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "sync_state.json")

# Seconds between full reconciliation passes (default: 300 = 5 minutes)
RECONCILE_INTERVAL = 300

# ──────────────────────────────────────────────────────────────
# LOGGING
# ──────────────────────────────────────────────────────────────

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.FileHandler("sync.log"),
        logging.StreamHandler(sys.stdout)
    ]
)
log = logging.getLogger(__name__)

HEADERS      = {"Authorization": f"Bearer {API_KEY}"}
JSON_HEADERS = {**HEADERS, "Content-Type": "application/json"}

# ──────────────────────────────────────────────────────────────
# STATE MANAGEMENT
# ──────────────────────────────────────────────────────────────

def load_state():
    if os.path.exists(STATE_FILE):
        with open(STATE_FILE, "r") as f:
            return json.load(f)
    return {}

def save_state(state):
    with open(STATE_FILE, "w") as f:
        json.dump(state, f, indent=2)

state_lock = threading.Lock()
sync_state = load_state()

# ──────────────────────────────────────────────────────────────
# UTILITIES
# ──────────────────────────────────────────────────────────────

def file_hash(path):
    """MD5 hash of a file. Retries up to 5 times if the file is locked (common with OneDrive)."""
    for attempt in range(5):
        try:
            h = hashlib.md5()
            with open(path, "rb") as f:
                for chunk in iter(lambda: f.read(8192), b""):
                    h.update(chunk)
            return h.hexdigest()
        except PermissionError:
            log.info(f"File locked by OneDrive, waiting... ({attempt+1}/5)")
            time.sleep(5)
    raise PermissionError(f"Could not access {path} after 5 attempts")

def kb_for_path(filepath):
    """Return the KB ID for a given file path based on FOLDER_KB_MAP."""
    for folder, kb_id in FOLDER_KB_MAP.items():
        if filepath.startswith(folder):
            return kb_id
    return None

# ──────────────────────────────────────────────────────────────
# API CALLS
# ──────────────────────────────────────────────────────────────

def api_upload_file(filepath):
    """Upload a file to Open WebUI and return its file ID."""
    filename = os.path.basename(filepath)
    try:
        with open(filepath, "rb") as f:
            resp = requests.post(
                f"{OPEN_WEBUI_URL}/api/v1/files/",
                headers=HEADERS,
                files={"file": (filename, f)},
                timeout=60
            )
        resp.raise_for_status()
        file_id = resp.json()["id"]
        log.info(f"Uploaded: {filename} (id={file_id})")
        return file_id
    except Exception as e:
        log.error(f"Upload failed for {filename}: {e}")
        return None

def api_add_to_kb(kb_id, file_id, filename):
    """Add an uploaded file to a knowledge base. Waits for processing to complete first."""
    # Wait for file to finish processing
    for _ in range(10):
        time.sleep(3)
        try:
            r = requests.get(f"{OPEN_WEBUI_URL}/api/v1/files/{file_id}", headers=HEADERS, timeout=15)
            r.raise_for_status()
            status = r.json().get("meta", {}).get("status", "")
            if status in ("processed", ""):
                break
        except Exception:
            pass

    try:
        resp = requests.post(
            f"{OPEN_WEBUI_URL}/api/v1/knowledge/{kb_id}/file/add",
            headers=JSON_HEADERS,
            json={"file_id": file_id},
            timeout=30
        )
        if resp.status_code == 400 and "Duplicate" in resp.text:
            log.info(f"{filename} already in KB, skipping")
            return True
        resp.raise_for_status()
        log.info(f"Added to KB: {filename}")
        return True
    except Exception as e:
        log.error(f"Failed to add {filename} to KB: {e}")
        return False

def api_remove_from_kb(kb_id, file_id, filename):
    """Remove a file from a knowledge base (does not delete the file record)."""
    try:
        resp = requests.post(
            f"{OPEN_WEBUI_URL}/api/v1/knowledge/{kb_id}/file/remove",
            headers=JSON_HEADERS,
            json={"file_id": file_id},
            timeout=30
        )
        resp.raise_for_status()
        log.info(f"Removed from KB: {filename}")
        return True
    except Exception as e:
        log.error(f"Failed to remove {filename} from KB: {e}")
        return False

def api_delete_file(file_id, filename):
    """Delete a file record from Open WebUI entirely."""
    try:
        resp = requests.delete(
            f"{OPEN_WEBUI_URL}/api/v1/files/{file_id}",
            headers=HEADERS,
            timeout=30
        )
        if resp.status_code == 404:
            log.info(f"File record already cleaned up: {filename}")
            return True
        resp.raise_for_status()
        log.info(f"Deleted file record: {filename}")
        return True
    except Exception as e:
        log.error(f"Failed to delete file record {filename}: {e}")
        return False

# ──────────────────────────────────────────────────────────────
# CORE SYNC OPERATIONS
# ──────────────────────────────────────────────────────────────

def add_file(filepath):
    """Upload a file and add it to the appropriate KB. Handles updates by removing the old version first."""
    kb_id = kb_for_path(filepath)
    if not kb_id:
        return
    ext = Path(filepath).suffix.lower()
    if ext not in ALLOWED_EXTENSIONS:
        return

    filename = os.path.basename(filepath)

    try:
        current_hash = file_hash(filepath)
    except Exception as e:
        log.error(f"Could not hash {filename}: {e}")
        return

    with state_lock:
        existing = sync_state.get(filepath, {})
        if existing.get("hash") == current_hash:
            log.info(f"No changes: {filename}")
            return
        old_file_id = existing.get("file_id")

    # Remove old version if it exists
    if old_file_id:
        api_remove_from_kb(kb_id, old_file_id, filename)
        api_delete_file(old_file_id, filename)

    file_id = api_upload_file(filepath)
    if not file_id:
        return

    success = api_add_to_kb(kb_id, file_id, filename)
    if success:
        with state_lock:
            sync_state[filepath] = {"hash": current_hash, "file_id": file_id}
            save_state(sync_state)

def remove_file(filepath):
    """Remove a file from its KB and clean up the file record."""
    kb_id = kb_for_path(filepath)
    if not kb_id:
        return

    filename = os.path.basename(filepath)

    with state_lock:
        existing = sync_state.get(filepath, {})
        if not existing.get("file_id"):
            return
        file_id = existing["file_id"]

    api_remove_from_kb(kb_id, file_id, filename)
    api_delete_file(file_id, filename)

    with state_lock:
        sync_state.pop(filepath, None)
        save_state(sync_state)

# ──────────────────────────────────────────────────────────────
# RECONCILIATION
# ──────────────────────────────────────────────────────────────

def reconcile():
    """Full sync pass: compares disk state to tracked state and resolves differences."""
    log.info("Running reconciliation...")

    for folder, kb_id in FOLDER_KB_MAP.items():
        disk_files = set()
        for root, _, files in os.walk(folder):
            for fname in files:
                if Path(fname).suffix.lower() in ALLOWED_EXTENSIONS:
                    disk_files.add(os.path.join(root, fname))

        with state_lock:
            tracked_files = set(fp for fp in sync_state if kb_for_path(fp) == kb_id)

        # Deleted from OneDrive -> remove from KB
        for filepath in tracked_files - disk_files:
            log.info(f"Reconcile remove: {os.path.basename(filepath)}")
            remove_file(filepath)

        # New in OneDrive -> upload to KB
        for filepath in disk_files - tracked_files:
            log.info(f"Reconcile add: {os.path.basename(filepath)}")
            add_file(filepath)

        # Changed files -> re-upload
        for filepath in disk_files & tracked_files:
            try:
                current_hash = file_hash(filepath)
                with state_lock:
                    stored_hash = sync_state.get(filepath, {}).get("hash")
                if current_hash != stored_hash:
                    log.info(f"Reconcile update: {os.path.basename(filepath)}")
                    add_file(filepath)
            except Exception as e:
                log.error(f"Error checking {filepath}: {e}")

    log.info("Reconciliation complete")

def reconcile_loop():
    """Background thread that runs reconciliation on a fixed interval."""
    while True:
        time.sleep(RECONCILE_INTERVAL)
        try:
            reconcile()
        except Exception as e:
            log.error(f"Reconciliation error: {e}")

# ──────────────────────────────────────────────────────────────
# WATCHDOG (real-time file change detection)
# ──────────────────────────────────────────────────────────────

class SyncHandler(FileSystemEventHandler):

    def on_created(self, event):
        if not event.is_directory:
            time.sleep(2)  # Brief delay to let OneDrive finish writing
            try:
                add_file(event.src_path)
            except Exception as e:
                log.error(f"Error on create: {e}")

    def on_modified(self, event):
        if not event.is_directory:
            time.sleep(2)
            try:
                add_file(event.src_path)
            except Exception as e:
                log.error(f"Error on modify: {e}")

    def on_deleted(self, event):
        if not event.is_directory:
            try:
                remove_file(event.src_path)
            except Exception as e:
                log.error(f"Error on delete: {e}")

    def on_moved(self, event):
        if not event.is_directory:
            try:
                remove_file(event.src_path)
                add_file(event.dest_path)
            except Exception as e:
                log.error(f"Error on move: {e}")

# ──────────────────────────────────────────────────────────────
# MAIN
# ──────────────────────────────────────────────────────────────

if __name__ == "__main__":
    log.info("Starting two-way OneDrive <-> Knowledge Base sync")
    for folder, kb_id in FOLDER_KB_MAP.items():
        log.info(f"  {folder} -> KB {kb_id}")

    # Run a clean reconciliation on startup
    reconcile()

    # Background reconciliation every 5 minutes
    t = threading.Thread(target=reconcile_loop, daemon=True)
    t.start()

    # Watchdog for instant file change detection
    handler = SyncHandler()
    observer = Observer()
    for folder in FOLDER_KB_MAP:
        observer.schedule(handler, folder, recursive=True)

    observer.start()
    log.info("Watching for changes... (Ctrl+C to stop)")

    try:
        while True:
            time.sleep(5)
    except KeyboardInterrupt:
        observer.stop()
    observer.join()
