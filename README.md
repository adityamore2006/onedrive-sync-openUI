# OneDrive <-> Open WebUI Knowledge Base Sync

Two-way sync between a local OneDrive folder and an [Open WebUI](https://github.com/open-webui/open-webui) knowledge base. Uses [watchdog](https://github.com/gorakhargosh/watchdog) for real-time file detection and periodic reconciliation as a safety net.

## What It Does

- **New files** added to your OneDrive folder are automatically uploaded to your Open WebUI knowledge base
- **Deleted files** are removed from the knowledge base
- **Modified files** are re-uploaded (old version removed, new version added)
- **Reconciliation** runs every 5 minutes to catch anything the file watcher missed

## Requirements

- Python 3.8+
- A running [Open WebUI](https://github.com/open-webui/open-webui) instance
- `pip install requests watchdog`

## Setup

### 1. Get your Open WebUI API key

Open your Open WebUI instance, go to **Settings > Account > API Keys**, and generate a new key.

### 2. Get your Knowledge Base ID

Open the knowledge base you want to sync in Open WebUI. The KB ID is the UUID in the URL:

```
http://localhost:3000/workspace/knowledge/c0709e6d-1739-4cab-a955-661eeb46eadf
                                          └──────────── This is your KB ID ────────────┘
```

### 3. Configure the script

Open `onedrive_sync.py` and fill in the config section at the top:

| Variable | What to put here |
|---|---|
| `OPEN_WEBUI_URL` | Base URL of your Open WebUI instance (e.g. `http://localhost:3000`) |
| `API_KEY` | The API key you generated in step 1 |
| `FOLDER_KB_MAP` | A dictionary mapping local folder paths to KB IDs. Each entry is one folder-to-knowledge-base pairing. |
| `ALLOWED_EXTENSIONS` | Set of file extensions to sync (default: `.pdf`, `.txt`, `.md`, `.docx`, `.csv`, `.pptx`) |
| `RECONCILE_INTERVAL` | Seconds between full reconciliation passes (default: `300`) |

Example `FOLDER_KB_MAP`:

```python
FOLDER_KB_MAP = {
    r"C:\Users\YourUser\OneDrive\Course Notes": "c0709e6d-1739-4cab-a955-661eeb46eadf",
    r"C:\Users\YourUser\OneDrive\Research":     "a1b2c3d4-5678-90ab-cdef-1234567890ab",
}
```

### 4. Run it

```bash
python onedrive_sync.py
```

The script will do an initial full sync, then watch for changes in real time.

### 5. (Optional) Run on boot

On Windows, add a shortcut to the script in your Task Scheduler or Startup folder so it runs automatically.

## Generated Files

The script creates two files in its working directory:

| File | Purpose |
|---|---|
| `sync_state.json` | Tracks which files have been synced and their hashes. Do not delete while the script is running. If you delete it while stopped, the next run will do a full re-sync. |
| `sync.log` | Log output for debugging. |

## How It Works

The script uses two mechanisms in parallel:

1. **Watchdog observer** monitors the mapped folders for file create/modify/delete/move events and syncs immediately (with a 2-second delay to let OneDrive finish writing).

2. **Reconciliation loop** runs a full comparison between disk and tracked state every N seconds to catch edge cases the watcher might miss (e.g., files changed while the script was not running).

File identity is tracked by MD5 hash. If a file's hash changes, the old version is removed from the KB and the new version is uploaded.

## Limitations

- This syncs in one direction conceptually: OneDrive is the source of truth, Open WebUI KB is the target. Changes made directly in the KB (like deleting a file through the UI) won't be reflected back to OneDrive.
- Large files may take time to upload and process. The script waits up to 30 seconds for Open WebUI to finish processing before adding a file to the KB.
- OneDrive sometimes locks files during sync. The script retries up to 5 times with a 5-second delay if it hits a `PermissionError`.

## License

MIT
