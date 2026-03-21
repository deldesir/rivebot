"""
RiveScript engine wrapper — one RiveScript instance per persona.

Security hardening vs upstream:
- Only 'python' object macro language is allowed (no Perl, JS, shell).
- Macros are registered from a whitelist (macro_bridge.py) — .rive files
  cannot define arbitrary Python code at load time.
- The RiveScript 'python' handler is replaced with our sandboxed
  MacroBridge that routes <call> tags to whitelisted async handlers.

Hot-reload: call reload_all() or reload_persona(name) after brain files change.

Note: brains are NOT loaded on import. FastAPI's startup_event calls reload_all()
      so that brains are loaded after the process fully initialises.
"""

import os
from pathlib import Path
from typing import Optional
from loguru import logger

from rivescript import RiveScript

BRAINS_DIR = Path(os.getenv("RIVEBOT_BRAINS_DIR", Path(__file__).parent.parent / "data" / "brains"))
GLOBAL_RIVE = BRAINS_DIR / "global.rive"

from .macro_bridge import MacroBridgeHandler, ALLOWED_MACROS

# Map persona → active RiveScript instance
_engines: dict[str, RiveScript] = {}


def _build_engine(persona: str) -> Optional[RiveScript]:
    """Load and compile a RiveScript engine for a persona."""
    brain_file = BRAINS_DIR / f"{persona}.rive"
    if not brain_file.exists():
        logger.warning(f"[engine] No brain file for persona '{persona}'")
        return None

    rs = RiveScript(utf8=True)
    rs.set_handler("python", MacroBridgeHandler())
    
    # Pre-register all tools so they don't return 'Object Not Found'
    for tool in ALLOWED_MACROS:
        rs._objlangs[tool] = "python"

    # Load global shared brain
    if GLOBAL_RIVE.exists():
        rs.stream(GLOBAL_RIVE.read_text())

    # Load persona brain
    rs.stream(brain_file.read_text())
    rs.sort_replies()

    logger.info(f"[engine] Loaded brain for '{persona}': {brain_file}")
    return rs


def load_persona(persona: str) -> bool:
    """Load or reload a single persona engine. Returns True on success."""
    eng = _build_engine(persona)
    if eng is None:
        return False
    _engines[persona] = eng
    return True


def get_engine(persona: str) -> Optional[RiveScript]:
    """Return the active RiveScript instance for a persona, loading if needed."""
    if persona not in _engines:
        load_persona(persona)
    return _engines.get(persona)


def reload_all() -> dict[str, bool]:
    """Reload all known persona engines. Returns status per persona."""
    results = {}
    for f in BRAINS_DIR.glob("*.rive"):
        persona = f.stem
        if persona == "global":
            continue
        results[persona] = load_persona(persona)
    logger.info(f"[engine] Reload complete: {results}")
    return results


def match(message: str, persona: str, user_id: str = "user") -> Optional[str]:
    """
    Try to match a message against a persona's brain.

    Returns:
        Response string if matched, None to fall through to LangGraph.
        The special sentinel '{{ai_fallback}}' in replies is also treated as None.
    """
    if persona not in _engines:
        if not load_persona(persona):
            return None  # no brain for this persona → always fall through

    rs = _engines[persona]
    try:
        reply = rs.reply(user_id, message)
    except Exception as e:
        logger.error(f"[engine] RiveScript error for '{persona}': {e}")
        return None

    # Sentinel: the brain explicitly delegated to AI
    if not reply or reply.strip() == "{{ai_fallback}}" or reply.startswith("ERR:"):
        return None

    return reply
