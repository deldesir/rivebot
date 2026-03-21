"""FastAPI entry point for the RiveScript brain service."""

from fastapi import FastAPI, Request
from pydantic import BaseModel
from loguru import logger
import traceback

from . import engine
from . import siyuan_sync

app = FastAPI(title="RiveScript Brain Service")

class MatchRequest(BaseModel):
    message: str
    persona: str
    user: str = "user"

@app.on_event("startup")
async def startup_event():
    logger.info("Starting rivebot service...")
    engine.reload_all()
    siyuan_sync.start_watchers()

@app.post("/match")
async def match_intent(req: MatchRequest):
    """
    Attempt to match a message against a persona's RiveScript brain.
    Returns the response string if matched, or null to fall through to LangGraph.
    """
    try:
        reply = engine.match(req.message, req.persona, req.user)
        return {
            "matched": reply is not None,
            "response": reply
        }
    except Exception as e:
        logger.error(f"Error handling /match: {traceback.format_exc()}")
        return {"matched": False, "response": None}

@app.post("/reload")
async def reload_brains():
    """Hot-reload all brain files (called by SiYuan sync watcher)."""
    results = engine.reload_all()
    return {"status": "reloaded", "results": results}

@app.get("/health")
async def health_check():
    return {"status": "ok", "engines": list(engine._engines.keys())}
