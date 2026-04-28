"""
Macro bridge — safely routes RiveScript <call>tool args</call> to the
AI Gateway's tool endpoints via HTTP.

Only whitelisted tool names can be called. The .rive brain files cannot
call arbitrary code — they can only reference names in ALLOWED_MACROS.

Security model:
  - Whitelist-only: unknown macro names return an error string (not raised).
  - RBAC gate: macros in ADMIN_MACROS are checked against the ROLES matrix.
    The caller's RapidPro groups are fetched (cached 5 min) and matched
    against the per-role allow/deny lists (ADR-011 Tier 3).
  - Audit log: all ADMIN_MACRO executions are appended to audit.db with
    timestamp, user, macro, args, status, and duration_ms.
  - Timeouts: each macro call has a 10-second timeout.
  - No shell: the RiveScript Perl/JS object handler is disabled entirely.
  - No-arg tools use GET; tools with args use POST with {"_args": [...]}
    The gateway /v1/tools/* router maps positional args to the tool's schema.
"""

import asyncio
import os
import time
import datetime
import sqlite3
import httpx
from loguru import logger

GATEWAY_URL = os.getenv("RIVEBOT_GATEWAY_URL", "http://localhost:8086")
MACRO_TIMEOUT = float(os.getenv("RIVEBOT_MACRO_TIMEOUT_S", "10"))
GATEWAY_INTERNAL_KEY = os.getenv("GATEWAY_INTERNAL_KEY", "")
RAPIDPRO_API_URL = os.getenv("RAPIDPRO_API_URL", "http://localhost:8080/api/v2")
RAPIDPRO_API_TOKEN = os.getenv("RAPIDPRO_API_TOKEN", "")


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
    # CRM Operations (ADR-010)
    "start_crm_ops":         "/v1/tools/start_crm_ops",
    "send_crm_help":         "/v1/tools/send_crm_help",
    # CRM Layer 2 Direct Commands (ADR-011 T2)
    "crm_list_groups":       "/v1/tools/crm_list_groups",
    "crm_lookup_contact":    "/v1/tools/crm_lookup_contact",
    "crm_org_info":          "/v1/tools/crm_org_info",
    "crm_create_group":      "/v1/tools/crm_create_group",
    # ── System operations (ADR-011 migration) ────────────────────
    # User-self (T3, NOT in ADMIN_MACROS)
    "macro_reset":           "/v1/tools/macro_reset",
    "macro_debug":           "/v1/tools/macro_debug",
    "macro_noai":            "/v1/tools/macro_noai",
    "macro_enableai":        "/v1/tools/macro_enableai",
    # Admin system ops (T2, in ADMIN_MACROS)
    "macro_noai_global":     "/v1/tools/macro_noai_global",
    "macro_enableai_global": "/v1/tools/macro_enableai_global",
    "macro_noai_status":     "/v1/tools/macro_noai_status",
    "macro_reload":          "/v1/tools/macro_reload",
    "macro_health":          "/v1/tools/macro_health",
    "macro_skills":          "/v1/tools/macro_skills",
    "macro_flow":            "/v1/tools/macro_flow",
    # ── Config operations (ADR-011 migration, T2) ────────────────
    "macro_persona":         "/v1/tools/macro_persona",
    "macro_channel":         "/v1/tools/macro_channel",
    "macro_admin":           "/v1/tools/macro_admin",
    "macro_global":          "/v1/tools/macro_global",
    "macro_label":           "/v1/tools/macro_label",
    # ── Organized persona (ADR-012) ──────────────────────────────
    "macro_organized_menu":           "/v1/tools/macro_organized_menu",
    "macro_get_schedule":             "/v1/tools/macro_get_schedule",
    "macro_get_next_week":            "/v1/tools/macro_get_next_week",
    "macro_get_my_assignments":       "/v1/tools/macro_get_my_assignments",
    "macro_search_persons":           "/v1/tools/macro_search_persons",
    "macro_get_events":               "/v1/tools/macro_get_events",
    "macro_get_sources":              "/v1/tools/macro_get_sources",
    "macro_get_field_group":          "/v1/tools/macro_get_field_group",
    "macro_get_attendance":           "/v1/tools/macro_get_attendance",
    "macro_get_field_report":         "/v1/tools/macro_get_field_report",
    "macro_get_visiting_speakers":    "/v1/tools/macro_get_visiting_speakers",
    "macro_get_speakers_congregations": "/v1/tools/macro_get_speakers_congregations",
    "macro_get_cong_report":          "/v1/tools/macro_get_cong_report",
    "macro_get_branch_report":        "/v1/tools/macro_get_branch_report",
    "macro_get_delegated_reports":    "/v1/tools/macro_get_delegated_reports",
    "macro_get_cong_analysis":        "/v1/tools/macro_get_cong_analysis",
    "macro_get_bible_studies":        "/v1/tools/macro_get_bible_studies",
    "macro_get_notifications":        "/v1/tools/macro_get_notifications",
}

# ── Admin authorization (ADR-011 Finding 3 + Tier 3 RBAC) ─────────────────────
# Macros that require the caller to be in specific RapidPro groups.
# Any future Layer 2 direct commands MUST be categorized here.

ROLES: dict[str, dict[str, list[str]]] = {
    "Admins": {
        "allow": ["all"],
        "deny": [],
    },
    "Teachers": {
        "allow": ["crm_lookup_contact", "crm_list_groups", "start_crm_ops", "send_crm_help"],
        "deny": [],
    },
    "Staff": {
        "allow": ["crm_lookup_contact", "crm_list_groups", "send_crm_help"],
        "deny": ["start_crm_ops", "crm_create_group"],
    },
}

ADMIN_MACROS: set[str] = {
    "start_crm_ops",
    "send_crm_help",
    # Layer 2 direct commands (ADR-011 T2)
    "crm_list_groups",
    "crm_lookup_contact",
    "crm_org_info",
    "crm_create_group",
    # System admin ops (ADR-011 migration)
    "macro_noai_global", "macro_enableai_global", "macro_noai_status",
    "macro_reload", "macro_health", "macro_skills", "macro_flow",
    # Config admin ops (ADR-011 migration)
    "macro_persona", "macro_channel", "macro_admin",
    "macro_global", "macro_label",
    # Organized admin reports (ADR-012) — sensitive aggregate data
    "macro_get_cong_report", "macro_get_branch_report",
    "macro_get_delegated_reports", "macro_get_cong_analysis",
}

# Cache: {phone: (allowed_macros_set, timestamp)} — 5 min TTL
_access_cache: dict[str, tuple[set[str], float]] = {}
_ACCESS_CACHE_TTL = 300  # seconds

async def _verify_access(user_id: str, macro_name: str) -> tuple[bool, str]:
    """Check if the user has permission to execute the macro.

    Three-tier auth model (ADR-011):
      Tier 1: ADMIN_PHONE superuser bypass (env var, comma-separated).
      Tier 2: RapidPro group membership → ROLES matrix.
      Tier 3: Fail-closed on any error.

    Queries GET /api/v2/contacts.json?urn=whatsapp:{user_id} and evaluates
    the returned groups against the ROLES matrix. Results are cached for 5 min.
    """
    # ── Tier 1: ADMIN_PHONE superuser bypass ─────────────────────
    admin_phones = os.getenv("ADMIN_PHONE", "").replace(" ", "").split(",")
    clean_user = user_id.replace("+", "").split(":")[-1]
    if clean_user in [p.replace("+", "") for p in admin_phones if p]:
        logger.info(f"[auth] Superuser bypass for {user_id}")
        return True, "SUPERUSER"

    # ── Tier 2: RapidPro group check ─────────────────────────────
    now = time.time()
    cached = _access_cache.get(user_id)
    if cached and (now - cached[1]) < _ACCESS_CACHE_TTL:
        allowed = cached[0]
        if "all" in allowed: return True, "CACHE:GROUP:Admins"
        if macro_name in allowed: return True, "CACHE:GROUP:Authorized"
        return False, "CACHE:DENIED:no_group"

    if not RAPIDPRO_API_TOKEN:
        # Bootstrap bypass: allow initial setup if system is unconfigured
        bootstrap_macros = {"macro_persona", "macro_channel"}
        if macro_name in bootstrap_macros:
            try:
                resp = httpx.get(f"{GATEWAY_URL}/v1/system/personas", timeout=1.0,
                                 headers={"X-API-Key": GATEWAY_INTERNAL_KEY})
                if resp.status_code == 200 and len(resp.json()) == 0:
                    logger.warning("[auth] BOOTSTRAP MODE: Permitting setup action.")
                    return True, "BOOTSTRAP"
            except Exception:
                pass
        logger.warning("[auth] RAPIDPRO_API_TOKEN not set — DENYING (fail-closed)")
        return False, "DENIED:no_token"

    try:
        urn = f"whatsapp:{user_id}" if ":" not in user_id else user_id
        async with httpx.AsyncClient(timeout=5.0) as client:
            resp = await client.get(
                f"{RAPIDPRO_API_URL}/contacts.json",
                params={"urn": urn},
                headers={"Authorization": f"Token {RAPIDPRO_API_TOKEN}"},
            )
            resp.raise_for_status()
            data = resp.json()
            results = data.get("results", [])

            allowed_macros = set()
            denied_macros = set()

            if results:
                groups = [g.get("name") for g in results[0].get("groups", [])]
                for g_name in groups:
                    if g_name in ROLES:
                        role = ROLES[g_name]
                        allowed_macros.update(role["allow"])
                        denied_macros.update(role["deny"])

            final_allowed = allowed_macros - denied_macros

            _access_cache[user_id] = (final_allowed, now)
            logger.info(f"[auth] {user_id}: allowed_macros={final_allowed}")

            if "all" in final_allowed:
                return True, "GROUP:Admins"
            if macro_name in final_allowed:
                return True, f"GROUP:{','.join(groups)}"
            return False, "DENIED:no_group"
    except Exception as e:
        # ── Tier 3: Fail-CLOSED on error ─────────────────────────
        logger.warning(f"[auth] Access check failed for {user_id}: {e} — DENIED (fail-closed)")
        return False, f"DENIED:error:{e}"


# ── Audit Logging (ADR-011 Tier 3) ────────────────────────────────────────────

AUDIT_DB_PATH = os.getenv("RIVEBOT_AUDIT_DB", "/opt/iiab/rivebot/data/audit.db")

def _init_audit_db():
    try:
        os.makedirs(os.path.dirname(AUDIT_DB_PATH), exist_ok=True)
        with sqlite3.connect(AUDIT_DB_PATH) as conn:
            conn.execute('''
                CREATE TABLE IF NOT EXISTS audit_log (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    timestamp DATETIME,
                    user_id TEXT,
                    macro TEXT,
                    args TEXT,
                    status TEXT,
                    duration_ms INTEGER,
                    auth_method TEXT
                )
            ''')
            # Migrate existing tables
            try:
                conn.execute("ALTER TABLE audit_log ADD COLUMN auth_method TEXT")
            except sqlite3.OperationalError:
                pass  # Column already exists
    except Exception as e:
        logger.error(f"[audit] Failed to init DB: {e}")

_init_audit_db()

async def _log_audit_async(user_id: str, macro: str, args: str, status: str, duration_ms: int, auth_method: str = ""):
    def _write():
        try:
            with sqlite3.connect(AUDIT_DB_PATH) as conn:
                conn.execute(
                    "INSERT INTO audit_log (timestamp, user_id, macro, args, status, duration_ms, auth_method) VALUES (?, ?, ?, ?, ?, ?, ?)",
                    (datetime.datetime.utcnow().isoformat(), user_id, macro, args, status, duration_ms, auth_method)
                )
        except Exception as e:
            logger.error(f"[audit] Log write failed: {e}")
    
    await asyncio.to_thread(_write)



# Context var names tracked between tool calls
_CONTEXT_VARS = ("active_pub", "active_talk_id", "active_revision")


async def call_macro(name: str, args: str, user_id: str,
                     context: dict | None = None) -> str:
    """
    Call a whitelisted macro via HTTP to the gateway tool endpoint.

    Sends active context vars (active_pub, active_talk_id, active_revision)
    as HTTP headers so tools can auto-infer missing arguments.

    Args:
        name: Macro name from <call>name args</call>.
        args: Space-separated args string (may be empty).
        user_id: User identifier for context.
        context: Dict of active context vars from RiveBot session.

    Returns:
        String response from the tool, or an error message.
    """
    start_time = time.time()

    if name not in ALLOWED_MACROS:
        logger.warning(f"[macro] Blocked unknown macro: '{name}' from user {user_id}")
        return f"⚠️ Unknown command: `{name}`"

    auth_method = ""
    # ADR-011 Finding 3 + Tier 3 RBAC: Admin authorization gate
    if name in ADMIN_MACROS:
        authorized, auth_method = await _verify_access(user_id, name)
        if not authorized:
            logger.warning(f"[auth] Blocked {user_id} from macro '{name}' (RBAC denied)")
            duration_ms = int((time.time() - start_time) * 1000)
            await _log_audit_async(user_id, name, args, "DENIED", duration_ms, auth_method)
            return "🚫 Access denied."

    path = ALLOWED_MACROS[name]
    headers = {"X-User-Id": user_id, "X-API-Key": GATEWAY_INTERNAL_KEY}

    # Forward active context vars as headers
    ctx = context or {}
    for var in _CONTEXT_VARS:
        val = ctx.get(var)
        if val and val != "undefined":
            # active_pub → X-Active-Pub, active_talk_id → X-Active-Talk-Id
            suffix = var.removeprefix("active_").replace("_", "-").title()
            headers[f"X-Active-{suffix}"] = str(val)

    arg_list = args.strip().split() if args.strip() else []

    status = "SUCCESS"
    try:
        async with httpx.AsyncClient(timeout=MACRO_TIMEOUT) as client:
            if arg_list:
                resp = await client.post(
                    f"{GATEWAY_URL}{path}",
                    json={"_args": arg_list},
                    headers=headers,
                )
            else:
                resp = await client.get(f"{GATEWAY_URL}{path}", headers=headers)
            resp.raise_for_status()
            data = resp.json()
            return data.get("result") or data.get("response") or str(data)
    except httpx.TimeoutException:
        status = "TIMEOUT"
        logger.error(f"[macro] Timeout calling '{name}'")
        return f"⏱️ Tool `{name}` timed out. Try again."
    except httpx.HTTPStatusError as e:
        status = f"HTTP_{e.response.status_code}"
        logger.error(f"[macro] HTTP error calling '{name}': {e.response.status_code}")
        return f"⚠️ Tool error ({e.response.status_code})."
    except Exception as e:
        status = "ERROR"
        logger.error(f"[macro] Unexpected error calling '{name}': {e}")
        return f"⚠️ Could not run `{name}`."
    finally:
        if name in ADMIN_MACROS:
            duration_ms = int((time.time() - start_time) * 1000)
            # Use await (not create_task) — create_task() tasks are cancelled
            # when asyncio.run() closes the event loop in call_macro_sync().
            await _log_audit_async(user_id, name, args, status, duration_ms, auth_method)


def call_macro_sync(name: str, args: str, user_id: str = "user",
                    context: dict | None = None) -> str:
    """Sync wrapper for call_macro — used by the RiveScript Python macro handler.

    RiveScript calls this from a synchronous context. We need to run the async
    call_macro() without nesting inside the running event loop. We do this via a
    dedicated thread with its own event loop — safe, portable, no deprecation.
    """
    import concurrent.futures
    def _run():
        return asyncio.run(call_macro(name, args, user_id, context))
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


# Context update markers that tools embed in their response text.
# Format: {{set:<var>:<value>}} — parsed and stripped before returning to user.
import re as _re
_CTX_PATTERN = _re.compile(r"\{\{set:(\w+):([^}]*)\}\}")


def _parse_context_updates(result: str, rs, user: str) -> str:
    """Extract {{set:var:value}} markers from tool response, apply to RiveBot vars."""
    def _apply(m):
        var, val = m.group(1), m.group(2)
        if var in _CONTEXT_VARS:
            rs.set_uservar(user, var, val)
            logger.info(f"[ctx] {user}: {var}={val}")
        return ""  # strip marker from user-visible text
    return _CTX_PATTERN.sub(_apply, result).strip()


class MacroBridgeHandler:
    def load(self, rs, code):
        pass

    def call(self, rs, name, user, args):
        # Gather active context vars to send with the tool call
        context = {}
        for var in _CONTEXT_VARS:
            val = rs.get_uservar(user, var)
            if val and val != "undefined":
                context[var] = val

        result = call_macro_sync(name, " ".join(args), user, context)

        # Parse and apply context update markers from tool response
        result = _parse_context_updates(result, rs, user)

        # Advance the user's workflow topic if this macro completed a stage.
        if name in STAGE_TRANSITIONS and not result.startswith("⚠️") and not result.startswith("⏱️"):
            next_topic = STAGE_TRANSITIONS[name]
            current = rs.get_uservar(user, "topic") or "random"
            _ORDER = ["random", "stage_1", "stage_2", "stage_3", "stage_4", "stage_5", "stage_6"]
            curr_idx = _ORDER.index(current) if current in _ORDER else 0
            next_idx = _ORDER.index(next_topic) if next_topic in _ORDER else 0
            if next_idx > curr_idx:
                rs.set_uservar(user, "topic", next_topic)
                logger.info(f"[stage] {user}: {current} → {next_topic}")

        return result
