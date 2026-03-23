# RiveBot & AI Gateway — TODO

## 🔴 Production Readiness

- [ ] **E2E integration test** — Start both services, send a real `/v1/chat/completions` request, verify full RiveBot → Gateway → LLM pipeline with persona routing
- [ ] **Ansible: add `DEFAULT_PERSONA` env var** — Update `ai-gateway.service.j2` template
- [ ] **RapidPro flow audit** — Verify webhook body sends correct `model` field (channel phone or slug) per flow
- [ ] **Configure `RAPIDPRO_BROADCAST_FLOW_UUID`** — Required for `/broadcast` admin command to work

## 🟡 Feature Completion

- [ ] **`#persona default <slug>` command** — Runtime override of DEFAULT_PERSONA without env var restart
- [ ] **Analytics persistence** — Save `_analytics` counters to `.userstate/analytics.json` alongside user state
- [ ] **Test isolation** — Add `.userstate/*.json` cleanup to test setup so trigger + noai tests can run sequentially
- [ ] **Persona create via `#persona`** — Support setting `language` and `allowed_tools` in the create command

## 🟢 Polish & Enhancements

- [ ] **Knowledge files** — Populate `data/knowledge/<slug>.md` with product-specific context for grounded LLM responses
- [ ] **SiYuan sync for new personas** — Auto-detect new `.rive` files as persona brains (currently requires manual naming)
- [ ] **Monitoring dashboard** — Build consumer for `/analytics` and `/stale-sessions` endpoints (Grafana/RapidPro)
- [ ] **Form integrations** — Wire `submit_form` tool to actual helpdesk/CRM APIs instead of logging
- [ ] **Advanced sentiment** — Expand frustration/positive arrays, add "confused" mood, tune AI tone adaptation
- [ ] **Proactive follow-up cron** — RapidPro polling job that calls `/stale-sessions` and nudges inactive users

## ✅ Completed (This Session)

- [x] User onboarding flow (language + name collection)
- [x] Multi-turn forms (support ticket, plan upgrade)
- [x] Trigger analytics + `/analytics` endpoint
- [x] Sentiment detection (frustration/positive → mood context)
- [x] Stale session detection + `/stale-sessions` endpoint
- [x] `/broadcast` admin command
- [x] Persona management: DB source of truth, startup seeder, slug-based routing
- [x] Channel → persona routing (3-tier: ChannelConfig → slug → DEFAULT_PERSONA)
- [x] In-session persona switching ("pale ak vant" → instant switch)
- [x] DB migration for new Persona columns (slug, language)
- [x] `submit_form` tool registered in ToolRegistry
- [x] State carryover on persona switch (onboarded/lang/name/welcomed)
