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
- [ ] Audit remaining non-JSON fetches, SSE reconnect/error paths, and API-derived `innerHTML` boundaries.


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
