# RiveBot — RiveScript Brain Service

A lightweight, stateless deterministic intent router sitting in front of the AI Gateway.  
Zero LLM tokens are spent on matched intents. Non-matching messages fall through to LangGraph.

```
WhatsApp → RapidPro → AI Gateway (:8086)
                           │
                    POST /match
                           ↓
              RiveScript Service (:8087)
                    │           │
               matched       no match
                    │           │
              return text    LangGraph
              (zero tokens)    + LLM
```

## Quick Start

```bash
cd /opt/iiab/rivebot
uv sync
uv run uvicorn rivebot.main:app --host 127.0.0.1 --port 8087
```

Test a match:

```bash
curl -s -X POST http://localhost:8087/match \
  -H "Content-Type: application/json" \
  -d '{"message": "status", "persona": "talkprep", "user": "+509"}'
```

## Brain Files

Brain files live in `data/brains/`. One file per persona:

| File | Persona |
|------|---------|
| `global.rive` | Shared arrays + substitutions (loaded by all) |
| `talkprep.rive` | TalkPrep 6-stage workflow intents |
| `konex-support.rive` | Konex support commands |

### RiveScript Primer

```rivescript
! version = 2.0

! sub montre = show     // substitution: "montre" → "show"
! sub mwen  = my

> topic default
  + (@status)           // matches any word in the "status" array
  - <call>talkmaster_status</call>

  + show my talks
  - <call>list_talks</call>

  + *                   // wildcard — falls through to AI
  - {@ ai_fallback}
< topic
```

Macros like `<call>talkmaster_status</call>` are handled by `macro_bridge.py`, which
forwards them as HTTP calls to the gateway's `/v1/tools/<tool>` endpoint.

## Editing Brains via SiYuan

If you prefer to edit brain files in SiYuan rather than directly:

1. Set `SIYUAN_DATA_DIR` to your SiYuan data path (e.g. `~/.config/siyuan/data`).
2. Create a SiYuan notebook called **"Bot Brains"**.
3. Add one document per persona (title must match the `.rive` filename, e.g. `talkprep`).
4. Inside the document, use fenced code blocks tagged `rivescript`:

````markdown
# TalkPrep Brain

```rivescript
> topic default
  + (@status)
  - <call>talkmaster_status</call>
```
````

5. On save, `siyuan_sync.py` (started automatically with the service) extracts the code
   blocks, writes them to `data/brains/<persona>.rive`, and triggers `/reload`.

> **No SiYuan?** Edit `.rive` files directly. The file watcher reloads the engine automatically.

## API

| Method | Path | Body | Returns |
|--------|------|------|---------|
| `POST` | `/match` | `{"message": "...", "persona": "talkprep", "user": "+509"}` | `{"matched": true/false, "response": "..."}` |
| `POST` | `/reload` | — | `{"reloaded": ["talkprep", "konex-support"]}` |
| `GET` | `/health` | — | `{"status": "ok", "engines": [...]}` |

## Deployment (Ansible)

The IIAB role `roles/rivebot` handles installation:

```bash
# In local_vars.yml:
rivebot_install: true
rivebot_port: 8087
rivebot_gateway_url: "http://127.0.0.1:8086"
# Optional — only if SiYuan runs server-side:
rivebot_siyuan_data_dir: "/home/user/.config/siyuan/data"
```

```bash
ansible-playbook -i inventory/hosts iiab-from-console.yml --tags rivebot
```

## Architecture

```
rivebot/
├── rivebot/
│   ├── main.py          # FastAPI: /match, /reload, /health
│   ├── engine.py        # RiveScript engine, one instance per persona
│   ├── macro_bridge.py  # <call>tool</call> → HTTP to gateway /v1/tools/*
│   └── siyuan_sync.py   # Watchdog: .rive + SiYuan .sy sync
└── data/brains/
    ├── global.rive
    ├── talkprep.rive
    └── konex-support.rive
```
