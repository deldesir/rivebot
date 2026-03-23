"""FastAPI entry point for the RiveScript brain service."""

from contextlib import asynccontextmanager
from typing import Optional

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel
from loguru import logger

load_dotenv()  # Load .env if present (no-op in production where systemd sets env)

from . import engine
from . import siyuan_sync


# ── Lifespan (replaces deprecated @app.on_event) ─────────────────────────────

@asynccontextmanager
async def lifespan(app: FastAPI):
    """Load all brain engines and start file watchers on startup."""
    logger.info("Starting rivebot service...")
    engine.reload_all()
    observer = siyuan_sync.start_watchers()
    yield
    # Graceful shutdown
    logger.info("Stopping rivebot service...")
    observer.stop()
    observer.join()


app = FastAPI(title="RiveScript Brain Service", lifespan=lifespan)


# ── Schemas ───────────────────────────────────────────────────────────────────

class MatchRequest(BaseModel):
    message: str
    persona: str
    user: str = "user"


class SetTopicRequest(BaseModel):
    persona: str
    user: str
    topic: str


class SetVarRequest(BaseModel):
    persona: str   # "*" = all personas
    user: str      # "*" = all users
    var: str
    value: str


class SetVarsRequest(BaseModel):
    """Batch set multiple variables in one call."""
    persona: str
    user: str
    vars: dict[str, str]  # {"name": "Blondel", "onboarded": "true", ...}


# ── Endpoints ─────────────────────────────────────────────────────────────────

@app.post("/match")
async def match_intent(req: MatchRequest) -> dict:
    """
    Attempt to match a message against a persona's RiveScript brain.

    Returns:
        matched  (bool):     whether a trigger fired
        response (str|null): the reply text, or null for AI fallback
        context  (dict):     session context for AI continuity:
            - lang:    detected user language (ht/en)
            - topic:   current RiveScript topic
            - history: last 3 input/reply pairs
    """
    try:
        result = engine.match(req.message, req.persona, req.user)
        # Empty string response = noai silence (3rd+ fallback).
        # Return matched=True with empty response so gateway sends nothing.
        if result.get("response") == "":
            result["response"] = None
            result["matched"] = False  # Signal: don't send anything
            result["silent"] = True    # Explicit silence flag
        return result
    except Exception:
        logger.exception(f"Error in /match for persona={req.persona}")
        return {"matched": False, "response": None, "context": {}}


@app.post("/set-var")
async def set_var(req: SetVarRequest) -> dict:
    """
    Set a RiveScript user variable.

    Used by RapidPro and the AI Gateway to toggle noai mode,
    set language preferences, or any other user state.

    Supports wildcards:
        persona="*" → set across all loaded personas
        user="*"    → set for all known users in the persona

    Examples:
        POST /set-var {"persona":"*","user":"*","var":"noai","value":"true"}
        POST /set-var {"persona":"talkprep","user":"+509123","var":"lang","value":"en"}
    """
    if req.persona == "*":
        results = engine.set_uservar_all(req.var, req.value)
        return {"ok": True, "scope": "global", "results": results}
    else:
        ok = engine.set_uservar(req.persona, req.user, req.var, req.value)
        if not ok:
            from fastapi import HTTPException
            raise HTTPException(status_code=404, detail=f"No brain for persona '{req.persona}'")
        return {"ok": True, "persona": req.persona, "user": req.user, "var": req.var, "value": req.value}


@app.post("/set-vars")
async def set_vars(req: SetVarsRequest) -> dict:
    """Batch-set multiple RiveScript user variables in one call.

    Used by the AI Gateway to inject WhatsApp contact name + onboarded
    state in a single round-trip instead of 3 sequential /set-var calls.

    Example:
        POST /set-vars {"persona":"konex-support","user":"+509...",
                        "vars":{"name":"Blondel","onboarded":"true","welcomed":"true"}}
    """
    for var, value in req.vars.items():
        ok = engine.set_uservar(req.persona, req.user, var, value)
        if not ok:
            raise HTTPException(status_code=404, detail=f"No brain for persona '{req.persona}'")
    return {"ok": True, "persona": req.persona, "user": req.user, "vars": req.vars}


@app.post("/set-topic")
async def set_topic(req: SetTopicRequest) -> dict:
    """
    Set the active RiveScript topic for a user within a persona's brain.

    This advances the user through the TalkPrep staged workflow:
      random → stage_1 → stage_2 → stage_3 → stage_4 → stage_5 → stage_6

    Called by the AI Gateway when a workflow stage is completed, so that
    subsequent RiveScript matches use the appropriate stage-locked triggers.

    Example:
        POST /set-topic {"persona":"talkprep","user":"+509","topic":"stage_3"}
    """
    rs = engine.get_engine(req.persona)
    if rs is None:
        raise HTTPException(status_code=404, detail=f"No brain for persona '{req.persona}'")

    # Validate topic is actually defined in the brain
    known_topics = list(getattr(rs, "_topics", {}).keys())
    if req.topic not in known_topics:
        raise HTTPException(
            status_code=400,
            detail=f"Topic '{req.topic}' not found in brain '{req.persona}'. "
                   f"Available: {known_topics}",
        )

    # Forward-only guard: never regress to an earlier stage
    _ORDER = ["random", "stage_1", "stage_2", "stage_3", "stage_4", "stage_5", "stage_6"]
    current = rs.get_uservar(req.user, "topic") or "random"
    curr_idx = _ORDER.index(current) if current in _ORDER else 0
    next_idx = _ORDER.index(req.topic) if req.topic in _ORDER else 0
    if next_idx <= curr_idx:
        logger.info(f"[topic] {req.persona}:{req.user} — already at {current}, not regressing to {req.topic}")
        return {"ok": False, "user": req.user, "topic": current, "reason": f"Already at {current}"}

    rs.set_uservar(req.user, "topic", req.topic)
    logger.info(f"[topic] {req.persona}:{req.user} → {req.topic}")
    return {"ok": True, "user": req.user, "topic": req.topic}


@app.post("/reload")
async def reload_brains() -> dict:
    """Hot-reload all brain files from disk (called by SiYuan sync watcher)."""
    results = engine.reload_all()
    return {"status": "reloaded", "results": results}


@app.get("/noai-status")
async def noai_status() -> dict:
    """Return current noai state: global flag + per-user list."""
    return engine.get_noai_status()


@app.get("/analytics")
async def analytics() -> dict:
    """Return trigger hit counts and AI fallback ratio per persona.

    Useful for identifying which user messages fall through to AI
    and should become new deterministic RiveScript triggers.
    """
    return engine.get_analytics()


@app.get("/stale-sessions")
async def stale_sessions(max_age_hours: float = 24.0) -> dict:
    """Return users stuck in a non-random topic beyond max_age_hours.

    Designed to be polled by RapidPro to trigger follow-up messages.
    Example: GET /stale-sessions?max_age_hours=12
    """
    return engine.get_stale_sessions(max_age_hours)


@app.get("/health")
async def health_check() -> dict:
    return {"status": "ok", "engines": list(engine._engines.keys())}


@app.get("/list-brains")
async def list_brains() -> dict:
    """List all loaded brain engines with their topic counts.

    Useful for admin/debug to verify which personas are active
    and how many triggers each brain contains.
    """
    brains = {}
    for name, rs in engine._engines.items():
        topics = list(getattr(rs, "_topics", {}).keys())
        trigger_count = sum(
            len(getattr(rs, "_topics", {}).get(t, {}))
            for t in topics
        ) if hasattr(rs, "_topics") else 0
        brains[name] = {
            "topics": topics,
            "topic_count": len(topics),
            "trigger_count": trigger_count,
        }
    return {"brains": brains, "count": len(brains)}
