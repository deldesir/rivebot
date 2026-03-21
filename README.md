# RiveBot

A standalone [RiveScript](https://www.rivescript.com/) brain engine that provides **deterministic intent matching** ahead of LLM invocation in the [IIAB AI Gateway](https://github.com/deldesir/gateway).

```
WhatsApp → RapidPro → AI Gateway → RiveBot (match?)
                                        ↓ yes → deterministic reply (0 LLM tokens)
                                        ↓ no  → LangGraph + LLM
```

## Features

- **RiveScript brains** per persona (`talkprep`, `konex-support`, …)
- **Haitian Creole substitutions** normalize input before matching (`montre → show`, `mwen → my`)
- **Staged workflow topics** — users advance through stages as they complete tasks
- **SiYuan sync** — edit brain docs in SiYuan, changes auto-reload (Mode A/B/C)
- **Macro bridge** — `<call>tool_name args</call>` routes to AI Gateway `/v1/tools/*`
- **`/set-topic`** endpoint — AI Gateway advances users between stages after tool calls

## Quick Start

```bash
git clone https://github.com/deldesir/rivebot.git
cd rivebot
uv sync
uv run uvicorn rivebot.main:app --host 127.0.0.1 --port 8087
```

Test:
```bash
curl -s http://127.0.0.1:8087/health
curl -s http://127.0.0.1:8087/match \
  -H "Content-Type: application/json" \
  -d '{"message": "show my talks", "persona": "talkprep", "user": "+509"}'
```

## Configuration

| Variable | Default | Description |
|---|---|---|
| `RIVEBOT_PORT` | `8087` | Listening port |
| `RIVEBOT_BRAINS_DIR` | `data/brains` | Path to `.rive` files |
| `RIVEBOT_GATEWAY_URL` | `http://127.0.0.1:8086` | AI Gateway base URL |
| `RIVEBOT_MACRO_TIMEOUT_S` | `10` | Per-macro timeout |
| `SIYUAN_DATA_DIR` | `` | SiYuan local data dir (Mode B) |
| `SIYUAN_API_URL` | `` | SiYuan HTTP API URL (Mode C) |
| `SIYUAN_API_TOKEN` | `` | SiYuan API token (Mode C) |
| `SIYUAN_NOTEBOOK_ID` | `` | "Bot Brains" notebook ID (Mode C) |
| `SIYUAN_POLL_INTERVAL_S` | `30` | Poll interval in seconds (Mode C) |

## SiYuan Brain Editing (Mode C — remote)

```bash
# .env
SIYUAN_API_URL=http://100.64.0.11:56260
SIYUAN_API_TOKEN=59973h7dz4jr4moa
SIYUAN_NOTEBOOK_ID=20260321012908-iej1pzy
```

On startup, RiveBot fetches all documents from the **Bot Brains** notebook, extracts
`rivescript` fenced code blocks, writes them to `data/brains/*.rive`, and reloads.

Bootstrap the SiYuan notebook (run once):
```bash
python3 scripts/bootstrap_siyuan_brains.py
```

## IIAB Deployment

```yaml
# local_vars.yml
rivebot_install: True
rivebot_enabled: True
rivebot_siyuan_api_url: "http://100.64.0.11:56260"
rivebot_siyuan_api_token: "59973h7dz4jr4moa"
rivebot_siyuan_notebook_id: "20260321012908-iej1pzy"
```

## API

| Endpoint | Method | Description |
|---|---|---|
| `/match` | POST | Match a message against a persona's brain |
| `/set-topic` | POST | Advance a user's RiveScript topic |
| `/reload` | POST | Reload all brain files from disk |
| `/health` | GET | Health check |

## Testing

```bash
uv run pytest tests/ -v
```
