"""
SiYuan sync — watches for brain file changes and triggers engine reload.

Two modes (auto-detected):

MODE A — Direct .rive file watch (always on):
  Watches data/brains/*.rive with watchdog.
  Any .rive change → POST /reload to this service.
  Use this when SiYuan mounts the brains dir as a filesystem path,
  or when developers edit .rive files directly.

MODE B — SiYuan .sy file watch (if SIYUAN_DATA_DIR is set):
  Watches SiYuan's data directory for *.sy changes.
  On change: parse .sy JSON → extract fenced ```rivescript blocks →
  write to data/brains/<persona>.rive → triggers reload (via Mode A).

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

import json
import os
import re
import time
from pathlib import Path
from typing import Optional

import httpx
from loguru import logger
from watchdog.events import FileSystemEventHandler
from watchdog.observers import Observer

BRAINS_DIR = Path(os.getenv("RIVEBOT_BRAINS_DIR", Path(__file__).parent.parent / "data" / "brains"))
SIYUAN_DATA_DIR = os.getenv("SIYUAN_DATA_DIR", "")   # e.g. /home/user/.config/siyuan/data
RIVEBOT_PORT = int(os.getenv("RIVEBOT_PORT", "8087"))
RELOAD_URL = f"http://localhost:{RIVEBOT_PORT}/reload"
DEBOUNCE_S = 1.5   # wait before reloading to batch rapid saves

# Rivescript fenced block extractor
_RS_FENCE = re.compile(r"```rivescript\s*\n(.*?)```", re.DOTALL)


# ─── SiYuan .sy parser ────────────────────────────────────────────────────────

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
            # CodeBlock: {"Type": "NodeCodeBlock", "Data": "rivescript", "Children": [{"Data": content}]}
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


# ─── Watchdog handlers ───────────────────────────────────────────────────────

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
        logger.info(f"[sync] .rive changed: {path} → triggering reload")
        try:
            httpx.post(RELOAD_URL, timeout=5)
        except Exception as e:
            logger.warning(f"[sync] Reload call failed: {e}")


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
        logger.info(f"[sync] SiYuan → wrote {out_path} ({len(content)} chars)")
        # RiveFileHandler will pick up the .rive change and reload


# ─── Public API ─────────────────────────────────────────────────────────────

def start_watchers() -> Observer:
    """Start all filesystem watchers. Returns the Observer (call .stop() to halt)."""
    observer = Observer()

    # Always watch .rive files
    BRAINS_DIR.mkdir(parents=True, exist_ok=True)
    observer.schedule(RiveFileHandler(), str(BRAINS_DIR), recursive=False)
    logger.info(f"[sync] Watching .rive files in {BRAINS_DIR}")

    # Optionally watch SiYuan data dir
    if SIYUAN_DATA_DIR and Path(SIYUAN_DATA_DIR).is_dir():
        observer.schedule(SiYuanFileHandler(), SIYUAN_DATA_DIR, recursive=True)
        logger.info(f"[sync] Watching SiYuan data at {SIYUAN_DATA_DIR}")
    else:
        logger.info("[sync] SIYUAN_DATA_DIR not set — SiYuan sync disabled")
        logger.info("[sync] Tip: set SIYUAN_DATA_DIR=/path/to/siyuan/data in .env")

    observer.start()
    return observer
