"""FastAPI entry point for the RiveScript brain service."""

from contextlib import asynccontextmanager
from typing import Optional

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel
from loguru import logger

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


# ── Endpoints ─────────────────────────────────────────────────────────────────

@app.post("/match")
async def match_intent(req: MatchRequest) -> dict:
    """
    Attempt to match a message against a persona's RiveScript brain.

    Returns {"matched": true, "response": "..."} if a trigger fires,
    or {"matched": false, "response": null} to fall through to LangGraph.
    """
    try:
        reply = engine.match(req.message, req.persona, req.user)
        return {"matched": reply is not None, "response": reply}
    except Exception:
        logger.exception(f"Error in /match for persona={req.persona}")
        return {"matched": False, "response": None}


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

    rs.set_uservar(req.user, "topic", req.topic)
    logger.info(f"[topic] {req.persona}:{req.user} → {req.topic}")
    return {"ok": True, "user": req.user, "topic": req.topic}


@app.post("/reload")
async def reload_brains() -> dict:
    """Hot-reload all brain files from disk (called by SiYuan sync watcher)."""
    results = engine.reload_all()
    return {"status": "reloaded", "results": results}


@app.get("/health")
async def health_check() -> dict:
    return {"status": "ok", "engines": list(engine._engines.keys())}
