# RiveBot

A standalone [RiveScript](https://www.rivescript.com/) brain engine that
provides **deterministic intent matching** ahead of LLM invocation in the
[IIAB AI Gateway](https://github.com/deldesir/gateway). Known intents get a
scripted reply for zero LLM tokens; everything else falls through to the agent.

```
WhatsApp → RapidPro → AI Gateway → RiveBot (match?)
                                        ↓ yes → deterministic reply (0 LLM tokens)
                                        ↓ no  → Hermes agent + LLM
```

Runs as systemd `rivebot` on **127.0.0.1:8087** (localhost-only); the gateway
is on `:8086`. See [`CLAUDE.md`](CLAUDE.md) for contributor/agent orientation.
This repo is **public** — no real tokens, phone numbers, fleet domains, or
business names in anything committed.

## Features

- **RiveScript brains** per persona — `talkprep`, `konex-support`,
  `konex-sales`, `organized`, `social-code`, `assistant`, `general` — plus a
  shared `_common/` set loaded for all.
- **Haitian Creole substitutions** normalize input before matching
  (`montre → show`, `mwen → my`).
- **Staged workflow topics** — a forward-only topic guard advances users
  through stages as they complete tasks (`/set-topic`).
- **Macro bridge** — `<call>tool args</call>` routes to the gateway's
  `/v1/tools/*` through a name whitelist. No Perl/JS/shell.
- **RBAC gate** — admin macros are checked against RapidPro group membership
  before the bridge forwards them (details below).
- **SiYuan sync** — optionally author brains in SiYuan; changes auto-reload.

## Authorization

The bridge gate is the **first** of two layers. `ADMIN_MACROS` are checked in
`macro_bridge._verify_access()`:

1. **Tier 1** — `ADMIN_PHONE` superuser bypass (env, comma-separated).
2. **Tier 2** — RapidPro group lookup → `ROLES` matrix (`Admins` = all,
   `Teachers`/`Staff` = allow/deny lists). Cached 5 min; flush via
   `POST /flush-auth-cache` (the gateway calls this on grant/persona changes).
3. **Tier 3** — fail-closed on any error. All admin-macro runs are appended to
   `data/audit.db`.

**This gate only protects the bridge.** The gateway's `/v1/tools/*` API
re-checks every call against the caller's per-persona/tier toolsets and is the
**final authority** — `allow: all` here does not open operator-only toolsets.

User promotion does **not** happen here. It lives gateway-side
(`access grant …`, operator-only); the old `sysadmin admin` / `/admin-assign`
paths were retired.

## Configuration

| Variable | Default | Description |
|---|---|---|
| `RIVEBOT_PORT` | `8087` | Listening port |
| `RIVEBOT_BRAINS_DIR` | `data/brains` | Path to `.rive` files |
| `RIVEBOT_GATEWAY_URL` | `http://localhost:8086` | AI Gateway base URL |
| `RIVEBOT_MACRO_TIMEOUT_S` | `10` | Per-macro timeout (seconds) |
| `GATEWAY_INTERNAL_KEY` | `` | Shared key sent to the gateway tools API |
| `ADMIN_PHONE` | `` | Comma-separated superuser URNs (Tier 1 bypass) |
| `RAPIDPRO_API_URL` | `http://localhost:8080/api/v2` | RapidPro API (group lookup) |
| `RAPIDPRO_API_TOKEN` | `` | RapidPro API token; unset ⇒ fail-closed |
| `RIVEBOT_AUDIT_DB` | `data/audit.db` | Admin-macro audit log |
| `SIYUAN_API_URL` / `SIYUAN_API_TOKEN` / `SIYUAN_NOTEBOOK_ID` | `` | Optional SiYuan brain-sync (Mode C) |

Secrets live in `.env` / `local_vars.yml` (gitignored). **Never commit real
tokens** — the values above are placeholders.

## API

| Endpoint | Method | Description |
|---|---|---|
| `/match` | POST | Match a message against a persona's brain |
| `/get-var` `/set-var` `/set-vars` | POST | Read/write per-user RiveScript vars (gateway + social-code sim) |
| `/set-topic` | POST | Advance a user's RiveScript topic (forward-only) |
| `/reload` | POST | Reload all brain files from disk |
| `/flush-auth-cache` | POST | Clear the RBAC access cache (called after admin/grant changes) |
| `/noai-status` | GET | Per-user AI on/off state |
| `/analytics` | GET | Match/topic analytics |
| `/stale-sessions` | GET | Users stuck in a non-random topic (for follow-up polling) |
| `/list-brains` | GET | Loaded engines with topic/trigger counts |
| `/health` | GET | Health check + engine list |

## Brain layout

```
data/brains/
  <persona>/…            per-persona brains (talkprep, konex-sales, …)
  _common/               loaded for ALL personas (system, admin, onboarding,
                         conversation, firewall, forms)
  _shared/               substitutions + config (creole/english subs, person subs)
  .userstate/*.json      per-user state persistence
```

`_common/system.rive` holds the operator `sysadmin *` commands. New macros must
be registered gateway-side (schema + toolset) *before* being whitelisted here.

## Run & test

```bash
uv sync
uv run uvicorn rivebot.main:app --host 127.0.0.1 --port 8087   # (systemd does this)
uv run pytest tests/ -v
curl -s localhost:8087/health
```
