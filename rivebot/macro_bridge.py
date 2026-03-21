"""
Macro bridge — safely routes RiveScript <call>tool args</call> to the
AI Gateway's tool endpoints via HTTP.

Only whitelisted tool names can be called. The .rive brain files cannot
call arbitrary code — they can only reference names in ALLOWED_MACROS.

Security model:
  - Whitelist-only: unknown macro names return an error string (not raised).
  - Timeouts: each macro call has a 10-second timeout.
  - No shell: the RiveScript Perl/JS object handler is disabled entirely.
  - No-arg tools use GET; tools with args use POST with {"_args": [...]}
    The gateway /v1/tools/* router maps positional args to the tool's schema.
"""

import asyncio
import os
import httpx
from loguru import logger

GATEWAY_URL = os.getenv("RIVEBOT_GATEWAY_URL", "http://localhost:8086")
MACRO_TIMEOUT = float(os.getenv("RIVEBOT_MACRO_TIMEOUT_S", "10"))


# ── Whitelist ────────────────────────────────────────────────────────
# Map macro name → gateway tool endpoint path
# Only names in this dict can be called from .rive files.

ALLOWED_MACROS: dict[str, str] = {
    # TalkPrep — Stage 0
    "get_talkprep_help":     "/v1/tools/get_talkprep_help",
    "talkmaster_status":     "/v1/tools/talkmaster_status",
    "cost_report":           "/v1/tools/cost_report",
    # TalkPrep — Stage 1
    "list_publications":     "/v1/tools/list_publications",
    "list_topics":           "/v1/tools/list_topics",
    "import_talk":           "/v1/tools/import_talk",
    "select_active_talk":    "/v1/tools/select_active_talk",
    # TalkPrep — Stage 2
    "create_revision":       "/v1/tools/create_revision",
    # TalkPrep — Stage 3
    "develop_section":       "/v1/tools/develop_section",
    # TalkPrep — Stage 4
    "evaluate_talk":         "/v1/tools/evaluate_talk",
    "get_evaluation_scores": "/v1/tools/get_evaluation_scores",
    # TalkPrep — Stage 5
    "rehearsal_cue":         "/v1/tools/rehearsal_cue",
    # TalkPrep — Stage 6
    "export_talk_summary":   "/v1/tools/export_talk_summary",
    # Konex
    "fetch_dossier":         "/v1/tools/fetch_dossier",
}


async def call_macro(name: str, args: str, user_id: str) -> str:
    """
    Call a whitelisted macro via HTTP to the gateway tool endpoint.

    No-arg calls use GET /v1/tools/{name}.
    Calls with args use POST /v1/tools/{name} with body {"_args": ["a", "b"]}.
    The gateway maps positional args to the tool's Pydantic field names.

    Args:
        name: Macro name from <call>name args</call>.
        args: Space-separated args string (may be empty).
        user_id: User identifier for context.

    Returns:
        String response from the tool, or an error message.
    """
    if name not in ALLOWED_MACROS:
        logger.warning(f"[macro] Blocked unknown macro: '{name}' from user {user_id}")
        return f"⚠️ Unknown command: `{name}`"

    path = ALLOWED_MACROS[name]
    headers = {"X-User-Id": user_id}
    arg_list = args.strip().split() if args.strip() else []

    try:
        async with httpx.AsyncClient(timeout=MACRO_TIMEOUT) as client:
            if arg_list:
                # POST with positional args in JSON body
                resp = await client.post(
                    f"{GATEWAY_URL}{path}",
                    json={"_args": arg_list},
                    headers=headers,
                )
            else:
                # GET for no-arg tools
                resp = await client.get(f"{GATEWAY_URL}{path}", headers=headers)
            resp.raise_for_status()
            data = resp.json()
            return data.get("result") or data.get("response") or str(data)
    except httpx.TimeoutException:
        logger.error(f"[macro] Timeout calling '{name}'")
        return f"⏱️ Tool `{name}` timed out. Try again."
    except httpx.HTTPStatusError as e:
        logger.error(f"[macro] HTTP error calling '{name}': {e.response.status_code}")
        return f"⚠️ Tool error ({e.response.status_code})."
    except Exception as e:
        logger.error(f"[macro] Unexpected error calling '{name}': {e}")
        return f"⚠️ Could not run `{name}`."


def call_macro_sync(name: str, args: str, user_id: str = "user") -> str:
    """Sync wrapper for call_macro — used by the RiveScript Python macro handler."""
    try:
        loop = asyncio.get_event_loop()
        if loop.is_running():
            # In async context: run in thread pool to avoid nested loop
            import concurrent.futures
            with concurrent.futures.ThreadPoolExecutor() as pool:
                future = pool.submit(asyncio.run, call_macro(name, args, user_id))
                return future.result(timeout=MACRO_TIMEOUT + 1)
        else:
            return loop.run_until_complete(call_macro(name, args, user_id))
    except Exception as e:
        logger.error(f"[macro] Sync wrapper error: {e}")
        return f"⚠️ Error: {e}"


class MacroBridgeHandler:
    def load(self, rs, code):
        pass

    def call(self, rs, name, user, args):
        return call_macro_sync(name, " ".join(args), user)
