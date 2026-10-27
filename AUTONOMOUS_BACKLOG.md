# Autonomous Backlog

Scope: Hermes Control Panel (`dashboard-toggle-server.py`, stdlib HTTP server, inline frontend).
Updated: 2026-10-27

## Wave 21 completed

- [x] Added `fetchJsonChecked()` and applied it to `/api/models`; HTTP failures now enter the existing error path instead of being parsed as success.
- [x] Added regression coverage for frontend fetch helper presence and model-loader integration.
- [x] Expanded checked fetch usage to profile loading/creation, SOUL loading, Kanban board/task loading and task creation without changing response contracts.
- [x] Harden Kanban bulk status/delete actions: validate every response and report partial failures instead of false all-success.
- [x] Expanded checked fetch usage to profile mutations, Kanban single-task/board/config mutations, and Kanban drag actions without changing response contracts.
- [x] Expanded checked fetch usage to gateway config/preview/load/save/toggle/remove and WhatsApp pairing status/start/cancel/apply flows.
- [x] Finished checked fetch migration for fallback/aux models, profile skills/toolsets reads, Kanban profile filter loading, and network GET/POST helpers.
- [x] Fixed delayed SSE log autoscroll closure race; each log box now receives its own delayed scroll callback and hidden-tab update debounce remains covered.
- [x] Sanitized WhatsApp QR SVG before DOM insertion with a strict SVG/attribute allowlist and DOM node construction; preserved safe account/error rendering.
- [x] Escaped profile-skill and attachment error messages before HTML insertion; coerced network counters/ports to safe numeric values and escaped network diagnostics.
- [x] Hardened SSE EventSource generation guards against stale callbacks/reconnects; pairing cancellation now checks HTTP status and shows cancellation failures.
- [x] Guarded Kanban attachment viewer against stale async responses and invalidated pending loads when viewer closes.
- [x] Added visible `.catch()` handling for network gateway restart and DNS resolve transport failures; no unhandled rejection or stuck status remains.
- [x] Added rejection handling for gateway catalog/toggle/delete, WhatsApp cancel, profile clone, Kanban assignee filter, and task assignee loaders; failed controls now show explicit feedback.
- [x] Escaped fenced Markdown language labels in server and browser renderers, closing raw code-block header HTML injection.
- [x] Added generation guards to profile skills/tools, skill reader, and Kanban task detail loaders; stale responses can no longer overwrite newer modal state.
- [x] Added generation guards to gateway config and WhatsApp pairing modal/poll requests; stale platform/status responses cannot overwrite current modal state.
- [x] Added generation guards to profile SOUL and Kanban config modal loaders; stale profile/board responses cannot overwrite current fields or errors.
- [x] Added generation guards to auxiliary, fallback, and profile model picker catalog callbacks; stale model lists cannot overwrite current picker state.
- [x] Added board-generation and selected-board checks to Kanban task refresh; stale board responses cannot overwrite current board.
- [x] Added generation guards to profile-clone and Kanban assignee modal loaders; stale profile lists cannot overwrite reopened modals.
- [x] Added generation guards to Kanban board-switch mutations; stale switch responses cannot refresh or toast over newer board selections.
- [x] Added generation guards to gateway YAML preview mode changes; stale previews cannot mutate reopened or switched gateway editors.
- [x] Added generation guards to shared model-catalog cache writes; stale `/api/models` responses cannot replace newer picker data.
- [x] Added generation guards to auxiliary/fallback model-save callbacks; stale save responses cannot overwrite newer model state or progress UI.
- [x] Added generation guards to gateway save/toggle/delete mutations; stale platform responses cannot overwrite the gateway slot or current error state.
- [x] Added profile-context and load-generation guards to skill/toolset mutations; stale profile responses cannot mutate a reopened profile modal.
- [x] Added task-context and mutation-generation guards to Kanban status/reclaim/delete actions; stale task responses cannot close or refresh a different task view.
- [x] Added task-context and mutation-generation guards to Kanban comment submission; stale comment responses cannot reopen or refresh a different task view.
- [x] Added board-context and mutation-generation guards to Kanban drag status/delete actions; stale drag responses cannot refresh or toast over newer board actions.
- [x] Added board/selection snapshots and mutation-generation guards to Kanban bulk status/delete actions; stale batches cannot clear newer selections or refresh another board.
- [x] Added board-context and mutation-generation guards to Kanban task creation; stale create responses cannot refresh or toast over another board.
- [x] Added board-context and mutation-generation guards to Kanban board creation; stale responses cannot force navigation after board context changes.
- [x] Added profile-target and mutation-generation guards to profile create/activate/delete/rename/SOUL/model mutations; stale responses cannot navigate or alert over newer profile actions.
- [x] Added pairing-generation guard to WhatsApp apply; stale apply responses cannot reload or reset a newer pairing modal.
- [x] Added bootstrap-generation guard to Kanban assignee filter loading; stale initialization cannot duplicate or append outdated profiles.
- [x] Added load-generation guards to Network Card refresh; stale status responses cannot overwrite newer card state or clear its loading indicator.
- [x] Added generation guards to Network connection test and DNS resolution; stale test/resolve responses cannot overwrite newer test cards or chips.
- [x] Audited remaining non-JSON fetches, SSE reconnect/error paths, and API-derived `innerHTML` boundaries; verified strict escaping and generation guards across all rendering sinks.
- [x] Added generation guards to Network Card route/DNS saves; stale responses and delayed refresh timers cannot overwrite newer save state.
- [x] Added generation guard to Kanban toast fade/hide callbacks; an old toast timer cannot hide a newer message.
- [x] Added copy-generation token to Markdown code block copy buttons; rapid repeated clicks cannot prematurely reset button label.
- [x] Cleaned pending hidden timers and queued SSE updates on disconnect; background tab sleep cannot flush stale DOM updates after disconnect.
- [ ] Continue audit of remaining delayed UI callbacks and shared status/log refresh paths.

## Wave 61 completed

- SSE disconnect routine now clears `_hiddenTimer` and discards `_pendingUpdate`, preventing stale background updates from rendering after the connection closes.

## Wave 60 completed

- Markdown code copy feedback now tracks per-button generation tokens, ignoring stale text-restore callbacks from older copy clicks.

## Wave 59 completed

- Kanban toast callbacks now carry a generation token. Replaced messages cancel old timers, preventing stale fade/hide callbacks from hiding current feedback.

## Wave 58 completed

- Network Card route and DNS save actions now use separate save-generation counters. Older success/error responses and delayed refresh callbacks are ignored after a newer save starts.

## Completed

- [x] Harden HTTP `HEAD`, body length parsing, and mutation method enforcement.
- [x] Coalesce cold cache misses and reject stale refresh writes.
- [x] Validate diagnostic subprocess exit codes and probe success counts.
- [x] Stream attachment downloads in bounded chunks.
- [x] Reap timed-out subprocesses and kill updater process groups.
- [x] Report lifecycle action launch/exit failures.
- [x] Harden atomic writers with unique temp files, cleanup, flush, and fsync.
- [x] Align Kanban fallback schema with runtime tables/indexes and `board.json` markers.
- [x] Reject nonexistent Kanban task mutations and invalid Kanban config keys/types.
- [x] Surface Kanban DB read errors instead of rendering false empty boards.
- [x] Verify security headers, cookie flags, login lockout, and CSRF checks.

## Wave 21 candidates

- [ ] Centralize frontend fetch error/status handling; several fetch chains call `response.json()` without checking `response.ok`.
- [ ] Audit frontend rendering boundaries where API-derived strings reach `innerHTML`; use escaping or text nodes where required.
- [ ] Verify mutation controls disable during submission and show consistent success/error toast feedback.
- [ ] Audit SSE reconnect/error behavior and stale fragment replacement.
- [ ] Add lightweight static checks for inline JavaScript syntax and route/API parity.

## Deferred / needs separate decision

- [ ] Runtime Hermes Kanban import from panel system Python: embedded runtime dependencies are not available to `/usr/bin/python3`; fallback path is schema-compatible and guarded.
- [ ] Remove or migrate production orphan board directory `kanban/boards/test-board-val` only with explicit cleanup authorization.
