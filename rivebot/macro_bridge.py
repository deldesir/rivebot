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
    # Study Tools
    "generate_anki_deck":    "/v1/tools/generate_anki_deck",
    "push_to_siyuan":        "/v1/tools/push_to_siyuan",
    # Konex
    "fetch_dossier":         "/v1/tools/fetch_dossier",
    "start_flow":            "/v1/tools/start_flow",
    # Forms
    "submit_form":           "/v1/tools/submit_form",
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
    """Sync wrapper for call_macro — used by the RiveScript Python macro handler.

    RiveScript calls this from a synchronous context. We need to run the async
    call_macro() without nesting inside the running event loop. We do this via a
    dedicated thread with its own event loop — safe, portable, no deprecation.
    """
    import concurrent.futures
    def _run():
        return asyncio.run(call_macro(name, args, user_id))
    try:
        with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
            future = pool.submit(_run)
            return future.result(timeout=MACRO_TIMEOUT + 2)
    except concurrent.futures.TimeoutError:
        logger.error(f"[macro] Sync wrapper timed out for '{name}'")
        return f"⏱️ Tool `{name}` timed out."
    except Exception as e:
        logger.error(f"[macro] Sync wrapper error for '{name}': {e}")
        return f"⚠️ Error: {e}"


# ── Stage transitions ─────────────────────────────────────────────────────────
# Maps the tool name that *completes* a stage → the topic the user moves into.
# Applied atomically after a successful macro call via MacroBridgeHandler.call().

STAGE_TRANSITIONS: dict[str, str] = {
    # Completing Stage 1 (talk imported or selected)
    "import_talk":        "stage_1",
    "select_active_talk": "stage_1",
    # Completing Stage 2 (revision created)
    "create_revision":    "stage_2",
    # Completing Stage 3 (section development started)
    "develop_section":    "stage_3",
    # Completing Stage 4 (evaluation run)
    "evaluate_talk":      "stage_4",
    # Completing Stage 5 (rehearsal started)
    "rehearsal_cue":      "stage_5",
    # Completing Stage 6 (export)
    "export_talk_summary":"stage_6",
}


class MacroBridgeHandler:
    def load(self, rs, code):
        pass

    def call(self, rs, name, user, args):
        result = call_macro_sync(name, " ".join(args), user)

        # Advance the user's workflow topic if this macro completed a stage.
        # Only advance — never go backwards — so we check current topic index.
        if name in STAGE_TRANSITIONS and not result.startswith("⚠️") and not result.startswith("⏱️"):
            next_topic = STAGE_TRANSITIONS[name]
            current = rs.get_uservar(user, "topic") or "random"
            # Topic order for guard
            _ORDER = ["random", "stage_1", "stage_2", "stage_3", "stage_4", "stage_5", "stage_6"]
            curr_idx = _ORDER.index(current) if current in _ORDER else 0
            next_idx = _ORDER.index(next_topic) if next_topic in _ORDER else 0
            if next_idx > curr_idx:
                rs.set_uservar(user, "topic", next_topic)
                logger.info(f"[stage] {user}: {current} → {next_topic}")

        return result
