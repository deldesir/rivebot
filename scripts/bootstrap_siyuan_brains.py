#!/usr/bin/env python3
"""
Bootstrap SiYuan "Bot Brains" notebook for RiveBot.

Run this on the machine where SiYuan is running:
    python3 bootstrap_siyuan_brains.py

It creates:
  - Notebook: "Bot Brains"
  - Document: "talkprep"    (with full TalkPrep rivescript brain)
  - Document: "konex-support" (with Konex support rivescript brain)

Requires: requests  (pip install requests)
"""

import sys
import json
import uuid
import requests

SIYUAN_URL = "http://100.64.0.11:6806"  # SiYuan on your laptop (Tailscale)
API_TOKEN  = "59973h7dz4jr4moa"          # Settings → About → API token

SESSION = requests.Session()
if API_TOKEN:
    SESSION.headers["Authorization"] = f"Token {API_TOKEN}"
SESSION.headers["Content-Type"] = "application/json"


# ── Brain content ─────────────────────────────────────────────────────────────

TALKPREP_BRAIN = """\
# TalkPrep Brain

Edit the rivescript blocks below to update the TalkPrep intent triggers.
Save the document — the rivebot siyuan_sync watcher will reload automatically.

```rivescript
! version = 2.0

// ── Stage 0: Always available ──────────────────────────────────────
> topic random

  + help
  - 📚 *TalkPrep commands:*\\n\\n• *show my talks* — list imports\\n• *status* — progress summary\\n• *show publications* — browse sources\\n• *import talk [topic]* — start a talk\\n• *develop section [name]* — AI-write a section\\n• *evaluate talk* — score your talk\\n• *rehearsal* — delivery coaching\\n• *export* — final manuscript\\n• *cost* — token usage

  + what can you do
  @ help

  + status
  - <call>talkmaster_status</call>

  + show [my] talks
  - <call>talkmaster_status</call>

  + show [my] talk
  - <call>talkmaster_status</call>

  + show talk my
  - <call>talkmaster_status</call>

  + list [my] talks
  - <call>talkmaster_status</call>

  + my talks
  - <call>talkmaster_status</call>

  + show publications
  - <call>list_publications</call>

  + list publications
  - <call>list_publications</call>

  + (cost|usage|tokens)
  - <call>cost_report</call>

  + *
  - {{ai_fallback}}

< topic

// ── Stage 1: Import ────────────────────────────────────────────────
> topic stage_1 inherits random

  + import talk *
  - <call>import_talk <star></call>

  + start [a] [new] talk [about] *
  - <call>import_talk <star></call>

  + select talk *
  - <call>select_active_talk <star></call>

< topic

// ── Stage 2: Revision ──────────────────────────────────────────────
> topic stage_2 inherits stage_1

  + (create|new) revision *
  - <call>create_revision <star></call>

  + revise *
  - <call>create_revision <star></call>

< topic

// ── Stage 3: Development ───────────────────────────────────────────
> topic stage_3 inherits stage_2

  + develop [section] *
  - <call>develop_section <star></call>

  + write [section] *
  - <call>develop_section <star></call>

< topic

// ── Stage 4: Evaluation ────────────────────────────────────────────
> topic stage_4 inherits stage_3

  + evaluate [my] talk
  - <call>evaluate_talk</call>

  + (scores|results)
  - <call>get_evaluation_scores</call>

< topic

// ── Stage 5: Rehearsal ─────────────────────────────────────────────
> topic stage_5 inherits stage_4

  + (rehearsal|rehearse|practice) [*]
  - <call>rehearsal_cue <star></call>

< topic

// ── Stage 6: Export ────────────────────────────────────────────────
> topic stage_6 inherits stage_5

  + (export|manuscript|final) [*]
  - <call>export_talk_summary <star></call>

< topic
```
"""

KONEX_BRAIN = """\
# Konex Support Brain

Edit rivescript blocks to update Konex support intents.

```rivescript
! version = 2.0

> topic random

  + help
  - 👋 *Konex Support*\\n\\n• *my profile* — view your account\\n• *help* — this menu\\n\\nOr just ask your question!

  + what can you do
  @ help

  + (my|mon) (profile|profil|info|account|kont)
  - <call>fetch_dossier</call>

  + show [my] profile
  - <call>fetch_dossier</call>

  + *
  - {{ai_fallback}}

< topic
```
"""


# ── SiYuan API helpers ────────────────────────────────────────────────────────

def api(path, body=None):
    resp = SESSION.post(f"{SIYUAN_URL}{path}", json=body or {})
    resp.raise_for_status()
    data = resp.json()
    if data.get("code", 0) != 0:
        raise RuntimeError(f"SiYuan API error on {path}: {data.get('msg')}")
    return data.get("data", {})


def create_notebook(name):
    data = api("/api/notebook/createNotebook", {"name": name})
    nb_id = data.get("notebook", {}).get("id")
    print(f"  ✅ Notebook created: '{name}' ({nb_id})")
    return nb_id


def find_notebook(name):
    data = api("/api/notebook/lsNotebooks")
    for nb in data.get("notebooks", []):
        if nb.get("name") == name:
            return nb.get("id")
    return None


def create_doc(notebook_id, title, markdown_content):
    data = api("/api/filetree/createDocWithMd", {
        "notebook": notebook_id,
        "path": f"/{title}",
        "markdown": markdown_content,
    })
    doc_id = data  # returns the document id directly
    print(f"  ✅ Document created: '{title}' ({doc_id})")
    return doc_id


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    print(f"\nBootstrapping SiYuan Bot Brains at {SIYUAN_URL}...\n")

    # Test connectivity
    try:
        api("/api/system/getConf")
    except Exception as e:
        print(f"❌ Cannot reach SiYuan at {SIYUAN_URL}: {e}")
        print("   Make sure SiYuan is running and the URL/token are correct.")
        sys.exit(1)

    # Find or create "Bot Brains" notebook
    nb_id = find_notebook("Bot Brains")
    if nb_id:
        print(f"  ℹ️  Notebook 'Bot Brains' already exists ({nb_id}) — adding documents")
    else:
        nb_id = create_notebook("Bot Brains")

    # Create persona documents
    create_doc(nb_id, "talkprep", TALKPREP_BRAIN)
    create_doc(nb_id, "konex-support", KONEX_BRAIN)

    print("\n✅ Done! Open SiYuan and look for the 'Bot Brains' notebook.")
    print("   Edit the rivescript blocks and save — rivebot will reload automatically")
    print("   (when SIYUAN_DATA_DIR is set in the rivebot environment).\n")


if __name__ == "__main__":
    main()
