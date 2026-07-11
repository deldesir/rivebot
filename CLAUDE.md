# RiveBot

Deterministic RiveScript router that answers known intents for zero tokens
before anything reaches the LLM. Runs as systemd unit `rivebot` on
**127.0.0.1:8087**, executing from this working tree (`uv run uvicorn
rivebot.main:app`). Edit → `systemctl restart rivebot` (brains are loaded at
startup; `macro_reload` via the gateway also reloads them).

## Map

- `data/brains/<persona>/` + `data/brains/_common/` — `.rive` brain files.
  One engine per persona (see `/list-brains`). `_common/system.rive` holds
  the `sysadmin *` operator commands.
- `rivebot/macro_bridge.py` — the ONLY path from `.rive` to code:
  `<call>name args</call>` → whitelist lookup in `ALLOWED_MACROS` → HTTP to
  the gateway's `/v1/tools/*` with `GATEWAY_INTERNAL_KEY` + the caller's
  user id. No Perl/JS object handlers, no shell.
  - `ADMIN_MACROS` are gated by the `ROLES` matrix (RapidPro groups, cached
    5 min, `ADMIN_PHONE` bypass, fail-closed, audited to `data/audit.db`).
  - This gate is the FIRST layer only — the gateway re-checks per-caller
    toolsets and is the final authority. "allow: all" here does not open
    operator-only toolsets.
  - Organized macros keep `macro_*` names on the left (brains call them) but
    must point at the gateway's `organized_*` registry names on the right.
- `rivebot/main.py` — FastAPI app. Endpoints: `/reply`, `/health`,
  `/stale-sessions`, `/list-brains`, `/flush-auth-cache` (the gateway calls
  this when grants/personas change). `/admin-assign` was deliberately
  removed — user promotion lives gateway-side (`access grant`), never here.

## Conventions

- New macro: implement + register it gateway-side first (schema + toolset),
  then add the `ALLOWED_MACROS` entry, then reference it from `.rive`.
  Categorize any privileged macro in `ADMIN_MACROS`.
- Auth failures must stay fail-closed; keep the audit log writing.
- This repo is PUBLIC: no real phone numbers, fleet domains, business names,
  or AI attribution in commits/PRs/code — including inside `.rive` files.

## Verify after changes

```bash
uv run python -c "import rivebot.main, rivebot.macro_bridge"   # imports
systemctl restart rivebot && systemctl is-active rivebot
curl -s localhost:8087/health          # engines list
curl -s localhost:8087/list-brains     # topics/trigger counts per brain
```
