"""
RiveScript engine wrapper — one RiveScript instance per persona.

Security hardening vs upstream:
- Only 'python' object macro language is allowed (no Perl, JS, shell).
- Macros are registered from a whitelist (macro_bridge.py) — .rive files
  cannot define arbitrary Python code at load time.
- The RiveScript 'python' handler is replaced with our sandboxed
  MacroBridge that routes <call> tags to whitelisted async handlers.

Brain loading order:
  1. _shared/*.rive   — substitutions, arrays, bot vars  (sorted by name)
  2. _common/*.rive   — shared conversation triggers     (sorted by name)
  3. {persona}.rive   — persona-specific triggers

NoAI graceful degradation:
  When `noai=true` for a user (or globally), the engine intercepts
  `{{ai_fallback}}` responses and returns an escalating sequence:
    1st → diplomatic acknowledgment
    2nd → short reminder
    3rd+ → None (silence, avoids spam)

State persistence:
  User vars are saved to disk on every set_uservar() call and restored
  on engine reload. File: {BRAINS_DIR}/.userstate/{persona}.json

Hot-reload: call reload_all() or reload_persona(name) after brain files change.

Note: brains are NOT loaded on import. FastAPI's startup_event calls reload_all()
      so that brains are loaded after the process fully initialises.
"""

import json
import os
import time
from collections import defaultdict
from pathlib import Path
from typing import Optional
from loguru import logger

from rivescript import RiveScript

BRAINS_DIR = Path(os.getenv("RIVEBOT_BRAINS_DIR", Path(__file__).parent.parent / "data" / "brains"))
STATE_DIR = BRAINS_DIR / ".userstate"

from .macro_bridge import MacroBridgeHandler, ALLOWED_MACROS

# Map persona → active RiveScript instance
_engines: dict[str, RiveScript] = {}

# ── Analytics Counters ───────────────────────────────────────────────────────
# In-memory: {persona: {"_ai_fallback": N, "help": N, ...}}
# Persisted alongside user state in .userstate/{persona}_analytics.json
_analytics: dict[str, dict[str, int]] = defaultdict(lambda: defaultdict(int))

# ── Session Timestamps ───────────────────────────────────────────────────────
# Track last activity per user to detect stale sessions
# {persona: {user_id: timestamp}}
_last_seen: dict[str, dict[str, float]] = defaultdict(dict)



# ── Friendly error for unimplemented macros ──────────────────────────────────
_MACRO_ERROR_MESSAGES = {
    "ht": "⚙️ Fonksyon sa a poko disponib. N ap travay sou sa!",
    "en": "⚙️ This feature isn't available yet. We're working on it!",
}


# ═════════════════════════════════════════════════════════════════════════════
#  Engine Loading
# ═════════════════════════════════════════════════════════════════════════════

def _load_directory(rs: RiveScript, directory: Path) -> int:
    """Stream all .rive files from a directory into the engine (sorted)."""
    if not directory.is_dir():
        return 0
    count = 0
    for f in sorted(directory.glob("*.rive")):
        rs.stream(f.read_text())
        count += 1
    return count


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

    # 1. Load shared definitions (substitutions, arrays, bot vars)
    shared = _load_directory(rs, BRAINS_DIR / "_shared")

    # 2. Load common conversation triggers
    common = _load_directory(rs, BRAINS_DIR / "_common")

    # 3. Load persona-specific brain
    rs.stream(brain_file.read_text())
    rs.sort_replies()

    logger.info(
        f"[engine] Loaded brain for '{persona}': "
        f"{shared} shared + {common} common + {brain_file.name}"
    )

    # 4. Restore persisted user state
    _restore_state(rs, persona)

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
        results[persona] = load_persona(persona)
    logger.info(f"[engine] Reload complete: {results}")
    return results


# ═════════════════════════════════════════════════════════════════════════════
#  State Persistence
# ═════════════════════════════════════════════════════════════════════════════

# Keys that are user-global (same value across all personas).
# When set on ANY persona, auto-propagated to all loaded engines.
_GLOBAL_KEYS = {"lang", "name", "onboarded", "welcomed"}

# Keys that are persona-specific (vary per persona for the same user)
_PERSONA_KEYS = {"topic", "mood"}

# Combined for backward compat with save/restore
_PERSIST_KEYS = _GLOBAL_KEYS | _PERSONA_KEYS


def _state_path(persona: str) -> Path:
    return STATE_DIR / f"{persona}.json"


def _global_state_path() -> Path:
    return STATE_DIR / "_global.json"


def _save_state(persona: str) -> None:
    """Persist important user variables to disk."""
    rs = _engines.get(persona)
    if rs is None:
        return

    all_vars = rs.get_uservars()
    if not all_vars or not isinstance(all_vars, dict):
        return

    state = {}
    for uid, udata in all_vars.items():
        if not isinstance(udata, dict):
            continue
        filtered = {k: v for k, v in udata.items()
                    if k in _PERSIST_KEYS and v and v != "undefined"}
        if filtered:
            state[uid] = filtered

    STATE_DIR.mkdir(parents=True, exist_ok=True)
    try:
        _state_path(persona).write_text(json.dumps(state, ensure_ascii=False, indent=2))
    except Exception as e:
        logger.warning(f"[engine] Failed to save state for '{persona}': {e}")


def _save_global_state() -> None:
    """Persist global user variables (shared across all personas) to disk."""
    # Merge global vars from all engines into a single file
    merged: dict[str, dict[str, str]] = {}
    for persona, rs in _engines.items():
        all_vars = rs.get_uservars()
        if not all_vars or not isinstance(all_vars, dict):
            continue
        for uid, udata in all_vars.items():
            if not isinstance(udata, dict):
                continue
            global_filtered = {k: v for k, v in udata.items()
                               if k in _GLOBAL_KEYS and v and v != "undefined"}
            if global_filtered:
                if uid not in merged:
                    merged[uid] = {}
                merged[uid].update(global_filtered)

    STATE_DIR.mkdir(parents=True, exist_ok=True)
    try:
        _global_state_path().write_text(
            json.dumps(merged, ensure_ascii=False, indent=2)
        )
    except Exception as e:
        logger.warning(f"[engine] Failed to save global state: {e}")


def _restore_state(rs: RiveScript, persona: str) -> None:
    """Restore persisted user variables into a freshly loaded engine."""
    # 1. Restore persona-specific state
    path = _state_path(persona)
    if path.exists():
        try:
            state = json.loads(path.read_text())
            count = 0
            for uid, udata in state.items():
                for var, value in udata.items():
                    rs.set_uservar(uid, var, value)
                count += 1
            if count:
                logger.info(f"[engine] Restored persona state for '{persona}': {count} users")
        except Exception as e:
            logger.warning(f"[engine] Failed to restore persona state for '{persona}': {e}")

    # 2. Overlay global state (takes precedence for global keys)
    gpath = _global_state_path()
    if gpath.exists():
        try:
            gstate = json.loads(gpath.read_text())
            gcount = 0
            for uid, udata in gstate.items():
                for var, value in udata.items():
                    rs.set_uservar(uid, var, value)
                gcount += 1
            if gcount:
                logger.info(f"[engine] Restored global state for '{persona}': {gcount} users")
        except Exception as e:
            logger.warning(f"[engine] Failed to restore global state for '{persona}': {e}")


# ═════════════════════════════════════════════════════════════════════════════
#  User Variable Helpers
# ═════════════════════════════════════════════════════════════════════════════

def set_uservar(persona: str, user_id: str, var: str, value: str) -> bool:
    """Set a RiveScript user variable. Returns True on success."""
    rs = get_engine(persona)
    if rs is None:
        return False
    rs.set_uservar(user_id, var, value)



    # If this is a global variable, propagate to all other loaded engines
    # so it persists across persona switches without manual carry-over.
    if var in _GLOBAL_KEYS:
        for other_persona, other_rs in _engines.items():
            if other_persona != persona:
                other_rs.set_uservar(user_id, var, value)

        _save_global_state()

    logger.info(f"[engine] {persona}:{user_id} — set {var}={value}")
    _save_state(persona)
    return True


def set_uservar_all(var: str, value: str) -> dict[str, bool]:
    """Set a user variable across ALL personas globally."""

    results = {}
    for persona, rs in _engines.items():
        # Get all known users for this engine
        all_vars = rs.get_uservars()
        users = list(all_vars.keys()) if all_vars and isinstance(all_vars, dict) else []
        for uid in users:
            rs.set_uservar(uid, var, value)

        results[persona] = True
        logger.info(f"[engine] {persona}:* — set {var}={value} for {len(users)} users")
        _save_state(persona)

    # Persist global state for global keys (noai, lang, name, etc.)
    if var in _GLOBAL_KEYS:
        _save_global_state()

    return results


def get_noai_status() -> dict:
    """Deprecated — noai state is now managed by the AI Gateway circuit breaker.

    This endpoint is kept for backward compatibility but returns an empty status.
    Query the AI Gateway's /health endpoint for circuit breaker state.
    """
    return {"deprecated": True, "message": "noai replaced by gateway circuit breaker"}


def get_analytics() -> dict:
    """Return trigger hit counts and AI fallback ratio per persona.

    Format:
        {persona: {"_ai_fallback": N, "_total": N, "fallback_ratio": 0.3,
                   "top_triggers": [("help", 42), ("plans", 17), ...]}}
    """
    result = {}
    for persona, counts in _analytics.items():
        total = sum(counts.values())
        fallback = counts.get("_ai_fallback", 0)
        deterministic = {k: v for k, v in counts.items() if not k.startswith("_")}
        top = sorted(deterministic.items(), key=lambda x: x[1], reverse=True)[:20]
        result[persona] = {
            "_ai_fallback": fallback,
            "_macro_error": counts.get("_macro_error", 0),
            "_total": total,
            "fallback_ratio": round(fallback / total, 3) if total else 0,
            "top_triggers": top,
        }
    return result


def get_stale_sessions(max_age_hours: float = 24.0) -> dict:
    """Find users stuck in a non-random topic beyond max_age_hours.

    Returns:
        {persona: [{user_id, topic, last_seen_ago_hours, last_seen_ts}]}

    Used by RapidPro to poll and send follow-up nudges.
    """
    now = time.time()
    cutoff = max_age_hours * 3600
    result: dict[str, list] = {}

    for persona, rs in _engines.items():
        all_vars = rs.get_uservars()
        if not all_vars or not isinstance(all_vars, dict):
            continue
        stale = []
        for uid, udata in all_vars.items():
            if not isinstance(udata, dict):
                continue
            topic = udata.get("topic", "random")
            if topic in ("random", "conversation", "undefined", ""):
                continue
            last = _last_seen.get(persona, {}).get(uid, 0)
            age = now - last
            if age >= cutoff:
                stale.append({
                    "user_id": uid,
                    "topic": topic,
                    "last_seen_ago_hours": round(age / 3600, 1),
                    "last_seen_ts": int(last),
                })
        if stale:
            result[persona] = sorted(stale, key=lambda x: x["last_seen_ago_hours"], reverse=True)

    return result


# ═════════════════════════════════════════════════════════════════════════════
#  Match with NoAI Awareness
# ═════════════════════════════════════════════════════════════════════════════

def match(message: str, persona: str, user_id: str = "user") -> dict:
    """
    Try to match a message against a persona's brain.

    Returns a dict with:
        matched  (bool):  whether a trigger fired
        response (str|None): the reply text, or None for AI fallback
        context  (dict):  session context for AI continuity
    """
    fallback = {"matched": False, "response": None, "context": {}}

    if persona not in _engines:
        if not load_persona(persona):
            return fallback

    rs = _engines[persona]
    try:
        reply = rs.reply(user_id, message)
    except Exception as e:
        logger.error(f"[engine] RiveScript error for '{persona}': {e}")
        return fallback

    # Build session context regardless of match result
    context = _build_context(rs, user_id)

    # ── Track activity timestamp ─────────────────────────────────────────────
    _last_seen[persona][user_id] = time.time()

    # Intercept macro errors → friendly message
    # Two patterns: "[ERR: Object Not Found]" (unregistered) or "⚠️ Tool error" (HTTP fail)
    if reply and (("[ERR:" in reply and "No Reply Matched" not in reply) or "⚠️ Tool error" in reply):
        lang = context.get("lang", "ht")
        friendly = _MACRO_ERROR_MESSAGES.get(lang, _MACRO_ERROR_MESSAGES["ht"])
        _analytics[persona]["_macro_error"] += 1
        return {"matched": True, "response": friendly, "context": context}

    # Sentinel: silence — trigger matched but no reply needed
    if reply and reply.strip() == "{{silent}}":
        logger.debug(f"[engine] {persona}:{user_id} → silent (no reply)")
        return {"matched": True, "response": "", "context": context}

    # Sentinel: noreply — trigger matched, suppress all output (ADR-010)
    # Used for CRM ops flow handoff: the RapidPro flow sends its own menu.
    if reply and reply.strip() == "{{noreply}}":
        logger.debug(f"[engine] {persona}:{user_id} → noreply (flow handoff)")
        return {"matched": True, "response": "{{noreply}}", "context": context}

    # Sentinel: persona switch request
    if reply and reply.strip() == "{{persona_switch}}":
        target = rs.get_uservar(user_id, "switch_persona")
        if target and target != "undefined":
            context["switch_persona"] = target
            _analytics[persona]["_persona_switch"] += 1
            logger.info(f"[engine] {persona}:{user_id} → persona switch to '{target}'")
            return {"matched": True, "response": None, "context": context}

    # Sentinel: the brain explicitly delegated to AI
    if not reply or reply.strip() == "{{ai_fallback}}" or reply.startswith("ERR:"):
        return {"matched": False, "response": None, "context": context}

    # Matched a deterministic trigger — count it by trigger pattern (F-39)
    matched_trigger = rs.last_match(user_id) or message.lower().strip()
    _analytics[persona][matched_trigger] += 1
    return {"matched": True, "response": reply, "context": context}



def _build_context(rs: RiveScript, user_id: str) -> dict:
    """Extract session context from RiveScript user data for AI continuity."""
    context = {}

    # User language preference
    lang = rs.get_uservar(user_id, "lang")
    if lang and lang != "undefined":
        context["lang"] = lang

    # Current topic
    topic = rs.get_uservar(user_id, "topic")
    if topic and topic != "undefined":
        context["topic"] = topic
    else:
        context["topic"] = "random"



    # Mood (set by sentiment triggers in conversation.rive)
    mood = rs.get_uservar(user_id, "mood")
    if mood and mood != "undefined":
        context["mood"] = mood

    # Name and onboarding status
    name = rs.get_uservar(user_id, "name")
    if name and name != "undefined":
        context["name"] = name
    onboarded = rs.get_uservar(user_id, "onboarded")
    context["onboarded"] = onboarded == "true"

    # NOTE: RiveScript's __history__ stores post-substitution, punctuation-stripped
    # input (e.g. "what's" → "what is", "ki" → "what"). We intentionally do NOT
    # include it here — the AI Gateway's session file (maintained by openai.py via
    # _persist_rivebot_turn) stores the original unmodified user text and is
    # the correct source for Hermes conversation history.
    # See ADR-011 / bug analysis: substituted history corrupts Hermes context.

    return context
