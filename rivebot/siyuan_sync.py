"""
SiYuan sync — watches for brain file changes and triggers engine reload.

Three modes (auto-detected by env vars):

MODE A — Direct .rive file watch (always on):
  Watches data/brains/*.rive with watchdog.
  Any .rive change → triggers engine.reload_all() directly.
  Use this when developers edit .rive files directly.

MODE B — SiYuan filesystem watch (if SIYUAN_DATA_DIR is set):
  Watches SiYuan's on-disk data directory for *.sy changes.
  On change: parse .sy JSON → extract fenced ```rivescript blocks →
  write to data/brains/<persona>.rive → triggers Mode A reload.
  Use this when SiYuan runs on the same machine as rivebot.

MODE C — SiYuan HTTP poll (if SIYUAN_API_URL + SIYUAN_NOTEBOOK_ID are set):
  Polls SiYuan's HTTP API every SIYUAN_POLL_INTERVAL_S seconds.
  On content change: fetches updated Markdown export → extracts rivescript
  blocks → writes .rive files → triggers reload.
  Use this when SiYuan runs on a different machine (e.g. laptop) and
  is accessible over a network (Tailscale, local LAN, etc.).

  Required env vars:
    SIYUAN_API_URL       e.g. http://100.64.0.11:56260
    SIYUAN_API_TOKEN     e.g. 59973h7dz4jr4moa
    SIYUAN_NOTEBOOK_ID   e.g. 20260321012908-iej1pzy

SiYuan note naming convention:
  Notebook: "Bot Brains"  (any notebook)
  Document title must match persona name exactly:
    "talkprep", "konex-support", "global"

  The document contains code fences like:
    ```rivescript
    > topic default
      + (@help)
      - <call>get_talkprep_help</call>
    ```
  Only the content inside rivescript fences is extracted.
"""

import asyncio
import json
import os
import re
import threading
import time
from pathlib import Path
from typing import Optional

import httpx
from loguru import logger
from watchdog.events import FileSystemEventHandler
from watchdog.observers import Observer

BRAINS_DIR = Path(os.getenv("RIVEBOT_BRAINS_DIR", Path(__file__).parent.parent / "data" / "brains"))
RIVEBOT_PORT = int(os.getenv("RIVEBOT_PORT", "8087"))
RELOAD_URL = f"http://localhost:{RIVEBOT_PORT}/reload"
DEBOUNCE_S = 1.5   # wait before reloading to batch rapid saves

# Mode B
SIYUAN_DATA_DIR = os.getenv("SIYUAN_DATA_DIR", "")

# Mode C
SIYUAN_API_URL      = os.getenv("SIYUAN_API_URL", "")
SIYUAN_API_TOKEN    = os.getenv("SIYUAN_API_TOKEN", "")
SIYUAN_NOTEBOOK_ID  = os.getenv("SIYUAN_NOTEBOOK_ID", "")
SIYUAN_POLL_INTERVAL_S = float(os.getenv("SIYUAN_POLL_INTERVAL_S", "30"))

# Rivescript fenced block extractor (used by Mode B .sy parser and Mode C MD parser)
_RS_FENCE = re.compile(r"```rivescript\s*\n(.*?)```", re.DOTALL)


# ─── SiYuan .sy parser (Mode B) ──────────────────────────────────────────────

def _extract_rivescript_from_sy(sy_path: Path) -> Optional[tuple[str, str]]:
    """
    Parse a SiYuan .sy file and extract rivescript content.

    Returns:
        (persona_name, rivescript_content) or None if not a brain doc.
    """
    try:
        data = json.loads(sy_path.read_text(encoding="utf-8"))
    except Exception:
        return None

    # SiYuan .sy structure: {"Properties": {"title": "..."}, "Children": [...blocks...]}
    title = data.get("Properties", {}).get("title", "")
    if not title:
        return None

    # Collect all code block content from Children (recursive)
    rs_chunks: list[str] = []
    _collect_rivescript_blocks(data.get("Children", []), rs_chunks)

    if not rs_chunks:
        return None

    return title.lower().replace(" ", "-"), "\n\n".join(rs_chunks)


def _collect_rivescript_blocks(blocks: list, out: list[str]) -> None:
    """Recursively walk SiYuan block tree and collect rivescript code blocks."""
    for block in blocks:
        if not isinstance(block, dict):
            continue
        if block.get("Type") == "NodeCodeBlock":
            lang = ""
            content = ""
            for child in block.get("Children", []):
                if child.get("Type") == "NodeCodeBlockFenceInfoMarker":
                    lang = child.get("Data", "")
                elif child.get("Type") == "NodeCodeBlockCode":
                    content = child.get("Data", "")
            if lang.strip().lower() == "rivescript" and content:
                out.append(content)
        # Recurse into children
        _collect_rivescript_blocks(block.get("Children", []), out)


# ─── SiYuan HTTP poll (Mode C) ───────────────────────────────────────────────

def _siyuan_api(path: str, body: Optional[dict] = None) -> dict:
    """Call SiYuan HTTP API synchronously."""
    headers = {"Content-Type": "application/json"}
    if SIYUAN_API_TOKEN:
        headers["Authorization"] = f"Token {SIYUAN_API_TOKEN}"
    r = httpx.post(
        f"{SIYUAN_API_URL}{path}",
        json=body or {},
        headers=headers,
        timeout=15,
    )
    data = r.json()
    if data.get("code", 0) != 0:
        raise RuntimeError(f"SiYuan API {path}: {data.get('msg')}")
    return data.get("data", {})


def _fetch_and_write_brains() -> list[str]:
    """
    Fetch all documents in the Bot Brains notebook, extract rivescript blocks,
    write updated .rive files. Returns list of persona names written.
    """
    # List docs in the notebook
    docs = _siyuan_api("/api/filetree/listDocsByPath", {
        "notebook": SIYUAN_NOTEBOOK_ID,
        "path": "/",
    })
    files = docs.get("files", [])

    written = []
    for f in files:
        doc_id = f.get("id")
        name = f.get("name", "").replace(".sy", "")
        if not doc_id or not name:
            continue

        # Export document as Markdown
        md_data = _siyuan_api("/api/export/exportMdContent", {"id": doc_id})
        md_content = md_data.get("content", "") or md_data.get("markdown", "")

        # Extract rivescript fences
        chunks = _RS_FENCE.findall(md_content)
        if not chunks:
            continue

        persona = name.lower().replace(" ", "-")
        out_path = BRAINS_DIR / f"{persona}.rive"
        new_content = "\n\n".join(chunks)

        # Only write if content changed (avoid spurious reloads)
        existing = out_path.read_text(encoding="utf-8") if out_path.exists() else ""
        if new_content.strip() != existing.strip():
            out_path.write_text(new_content, encoding="utf-8")
            logger.info(f"[sync-C] SiYuan API → wrote {out_path} ({len(new_content)} chars)")
            written.append(persona)

    return written


def _poll_siyuan_loop():
    """Background thread: poll SiYuan API every SIYUAN_POLL_INTERVAL_S seconds."""
    logger.info(f"[sync-C] Polling SiYuan at {SIYUAN_API_URL} every {SIYUAN_POLL_INTERVAL_S}s")
    while True:
        try:
            written = _fetch_and_write_brains()
            if written:
                # Trigger reload via local HTTP (Mode A watcher will also catch this)
                httpx.post(RELOAD_URL, timeout=5)
                logger.info(f"[sync-C] Reloaded after changes in: {written}")
        except Exception as e:
            logger.warning(f"[sync-C] Poll error (non-fatal): {e}")
        time.sleep(SIYUAN_POLL_INTERVAL_S)


# ─── Watchdog handlers (Mode A / Mode B) ────────────────────────────────────

class RiveFileHandler(FileSystemEventHandler):
    """Watches .rive files directly — triggers reload on any change."""

    def __init__(self):
        self._last_reload = 0.0

    def on_modified(self, event):
        if event.is_directory or not event.src_path.endswith(".rive"):
            return
        self._debounced_reload(event.src_path)

    def on_created(self, event):
        self.on_modified(event)

    def _debounced_reload(self, path: str):
        now = time.time()
        if now - self._last_reload < DEBOUNCE_S:
            return
        self._last_reload = now
        logger.info(f"[sync-A] .rive changed: {path} → triggering reload")
        try:
            httpx.post(RELOAD_URL, timeout=5)
        except Exception as e:
            logger.warning(f"[sync-A] Reload call failed: {e}")


class SiYuanFileHandler(FileSystemEventHandler):
    """Watches SiYuan .sy files — extracts rivescript blocks and writes .rive files."""

    def __init__(self):
        self._last_write: dict[str, float] = {}

    def on_modified(self, event):
        if event.is_directory or not event.src_path.endswith(".sy"):
            return
        self._process(Path(event.src_path))

    def on_created(self, event):
        self.on_modified(event)

    def _process(self, sy_path: Path):
        now = time.time()
        key = str(sy_path)
        if now - self._last_write.get(key, 0) < DEBOUNCE_S:
            return
        self._last_write[key] = now

        result = _extract_rivescript_from_sy(sy_path)
        if result is None:
            return

        persona, content = result
        out_path = BRAINS_DIR / f"{persona}.rive"
        out_path.write_text(content, encoding="utf-8")
        logger.info(f"[sync-B] SiYuan .sy → wrote {out_path} ({len(content)} chars)")
        # RiveFileHandler will pick up the .rive change and trigger reload


# ─── Public API ─────────────────────────────────────────────────────────────

def start_watchers() -> Observer:
    """Start all filesystem watchers and HTTP poller. Returns the Observer."""
    observer = Observer()

    # Mode A: always watch .rive files
    BRAINS_DIR.mkdir(parents=True, exist_ok=True)
    observer.schedule(RiveFileHandler(), str(BRAINS_DIR), recursive=True)
    logger.info(f"[sync-A] Watching .rive files in {BRAINS_DIR}")

    # Mode B: optionally watch SiYuan data dir (local filesystem)
    if SIYUAN_DATA_DIR and Path(SIYUAN_DATA_DIR).is_dir():
        observer.schedule(SiYuanFileHandler(), SIYUAN_DATA_DIR, recursive=True)
        logger.info(f"[sync-B] Watching SiYuan data at {SIYUAN_DATA_DIR}")

    # Mode C: optionally poll SiYuan HTTP API (remote SiYuan)
    if SIYUAN_API_URL and SIYUAN_NOTEBOOK_ID:
        # Initial fetch on startup
        try:
            written = _fetch_and_write_brains()
            if written:
                logger.info(f"[sync-C] Initial fetch loaded: {written}")
            else:
                logger.info("[sync-C] Initial fetch: no changes")
        except Exception as e:
            logger.warning(f"[sync-C] Initial fetch failed (non-fatal): {e}")

        # Background poller thread
        t = threading.Thread(target=_poll_siyuan_loop, daemon=True)
        t.start()
        logger.info(f"[sync-C] HTTP poller started (notebook: {SIYUAN_NOTEBOOK_ID})")
    elif not SIYUAN_DATA_DIR:
        logger.info("[sync] No SiYuan sync configured.")
        logger.info("[sync] Tip: set SIYUAN_API_URL + SIYUAN_API_TOKEN + SIYUAN_NOTEBOOK_ID")
        logger.info("[sync]   or SIYUAN_DATA_DIR=/path/to/siyuan/data")

    observer.start()
    return observer
