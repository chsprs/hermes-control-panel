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
- [x] Guarded Kanban auto-sync against in-flight request pileup; slow responses no longer stack overlapping board refreshes.
- [x] Added generation guards to network gateway restart action; delayed failure callbacks or postponed 45s refresh timers cannot overwrite newer network card operations.
- [x] Restored instant scroll-to-bottom on log un-dismissal (`toggleLog`); restored logs now consistently scroll to the newest output even if offline or before the next SSE tick.
- [x] Aligned SSE gateway log card caching with the other three log cards, storing `d.gateway_log_card` before dismissal checks so un-dismissal always restores the freshest payload.
- [x] Inspected `input.method` in global fetch toast interceptor; mutation requests passed as `Request` instances now trigger automatic toast notifications.
- [x] Sanitized Kanban comment author inputs, falling back cleanly to `web-panel` on blank/whitespace strings across both SQL insertion and event logs.
- [x] Validated task title in `update_kanban_task` and increased `MODELS_TIMEOUT` to 8.0s; empty/whitespace titles are rejected and upstream model discovery remains resilient under system load.
- [x] Cleared stale poll and countdown interval timers when re-starting WhatsApp pairing (`startWaPair`); subsequent attempts now start with pristine timer state.
- [ ] Continue audit of remaining delayed UI callbacks and shared status/log refresh paths.

## Wave 77 completed

- **Finite backlog audit verification & hardening (Wave 21 candidates closure):**
  1. *Centralized fetch error/status handling:* diverifikasi seluruh 46 pemanggilan AJAX/fetch menggunakan helper terstandarisasi (`fetchJsonChecked()`, `netGet()`, dan `netPost()`); seluruhnya memeriksa status HTTP `response.ok` (atau melempar status error) dan tidak ada pemanggilan mentah `r.json()` tanpa penanganan status error.
  2. *Frontend rendering boundaries & escaping:* slot teks dinamis `gw_summary_text` pada handler SSE `apply()` dimigrasikan dari `set()` (`innerHTML`) ke text node (`textContent`), menutup potensi rendering injection pada badge ringkasan gateway. Rendering boundary modal profil dan komponen lain diverifikasi menggunakan DOM text node atau escaping ketat (`escHtml`/`escGw`).
  3. *In-flight mutation submission guards:* menambahkan flag guard dan disable tombol/input selama pengiriman untuk form profil yang tersisa: `submitRenameProfile` (`profileRenameSubmitting`, label tombol 'Menyimpan…'), `submitProfileSoul` (`profileSoulSubmitting`, label tombol 'Menyimpan…'), `toggleProfileSkill` (`profileSkillSubmitting[skill]`), dan `toggleProfileToolset` (`profileToolSubmitting[toolset]`), disertai feedback notifikasi toast sukses/error yang konsisten.
  4. *SSE interaction guard processes_table:* penimpaan slot `process-table-slot` pada setiap tick SSE kini memeriksa interaksi aktif pengguna (`contains(document.activeElement)`, `:focus-within`, `:active`, hover pada tombol aksi tugas/layanan, atau seleksi teks) agar tabel proses tidak di-replace secara destruktif saat pengguna sedang mengklik atau memilih baris.
  5. *Static checks inline JS & route/API parity:* memperluas cakupan audit sintaks JavaScript ke seluruh blok `<script>` inline dan `NETWORK_SCRIPT` via `node --check`. Menambahkan verifikasi paritas route mutasi backend `do_POST` dengan `MUTATING_PATHS`, serta memperbaiki pencocokan awalan `isMutatingPath` untuk prefix direktori (seperti `/api/kanban/`).
- Test RED→GREEN: 289 unit tests (maju & mundur) lulus 100%. Node check lolos 4/4 scripts.

## Wave 76 completed

- **Kebocoran kredensial di editor konfigurasi gateway.** `GET /api/gateway-config` mengembalikan YAML mentah blok platform ke browser, dan editor memutarnya bolak-balik lewat `<textarea>` (mode YAML) serta mengirimnya kembali saat Simpan. Akibatnya `token`, `bot_token`, `password`, `app_secret`, `client_secret`, `corp_secret`, `channel_secret`, `api_key`, `access_token`, dst. tampil apa adanya di layar — padahal panel bisa jalan tanpa password di LAN. `redact_sensitive_tokens()` yang ada hanya mengenali *bentuk* token (Telegram/Discord/Slack/sk-/AIza/gh*), jadi secret berbasis key seperti `password:` atau `app_secret:` lolos total.
- Fix: masking berbasis **nama key** (`_SECRET_KEY_RE`) yang rekursif atas dict/list, nilai diganti sentinel `__HERMES_PANEL_REDACTED__` sambil **mempertahankan key** agar YAML tetap valid dan user tahu ada secret. Diterapkan di `get_gateway_platform_config` dan `preview_gateway_platform_config` (jalur switch Form→YAML).
- Fix sisi tulis: `save_gateway_platform_config` memulihkan nilai asli dari config on-disk untuk setiap key yang masih bernilai sentinel (`_restore_masked_secrets`). Tanpa ini, buka editor lalu tekan Simpan akan menimpa token asli dengan string mask dan mematikan platform. Sentinel pada key yang tidak punya nilai tersimpan **dibuang**, tidak pernah ditulis sebagai secret palsu.
- Test RED→GREEN: `test_27b_editor_never_sends_platform_secrets_to_the_browser`, `test_27c_saving_a_masked_secret_restores_the_stored_value`, `test_27d_masking_a_secret_that_does_not_exist_yet_is_harmless`. 283 unit + 113 contract pass.

## Wave 75 completed

- `netLoad(true)` (full network-card re-render) no longer runs while `NET.dirty.routes` or `NET.dirty.dns` is set. A full re-render rebuilds the route/DNS inputs from the server and resets the dirty flag, which silently discarded edits the user never saved (e.g. saving DNS while route edits were pending, or the 45s post-restart reload). It now refreshes only the live status tiles and shows a warning callout naming what is still unsaved.
- The dirty guard runs before the destructive `slot.innerHTML = netRenderCard(st)` and returns early; `netSaveRoutes`/`netSaveDns` clear their own dirty flag first, so their own reloads are unaffected.

## Wave 74 completed

- Kanban comment submission (`submitCurrentTaskComment`) now guards against in-flight double submissions (`kanbanCommentSubmitting`), disables input/button with "Mengirim…" label, and re-enables them on completion.
- `openViewTaskModal` and `closeViewTaskModal` now reset comment input text and state, preventing comment draft leaks across task views.
- Task comments preserve line breaks with `white-space: pre-wrap; word-break: break-word` and safely guard `created_at` date formatting.
- `renderAuxPickerItems` hides "Otomatis (Pakai Model Utama)" when configuring agent profile models, preventing config corruption.
- Model pickers (`renderAuxPickerItems`, `renderFallbackPickerItems`, `filterModels`) now trim search queries and display helpful empty state feedback instead of blank containers.
- Cleaned up duplicate `escGw` function declaration.

## Wave 73 completed

- Fixed markdown link URL handling in both Python `render_markdown` and client `renderMarkdown` to disallow `\x00` placeholder characters, preventing inline-code placeholders from breaking out of `href` attributes.

## Wave 72 completed

- Kanban drag and trash drop now record `draggedBoard` on dragstart, aborting cleanly if active board switches before drop.
- `handleKanbanTrashDrop` guards against board switch during blocking `confirm()` modal, verifies card element existence, and avoids cross-board deletion.
- `switchKanbanBoard` serializes overlapping switch requests (`kanbanSwitchBusy` queue), cancels active dragging, and increments `kanbanMutationGen` and `kanbanBoardLoadGen` to invalidate in-flight drag mutations and stale board loads.

## Wave 71 completed

- Per-action debounce in `dashboard_action` and `bot_action`: distinct actions executed in quick succession now run independently instead of colliding under a global 3-second debounce window.

## Wave 70 completed

- Injected `GITHUB_TOKEN` from CLI into Hermes update check environment and compare requests.
- Paused SSE EventSource reconnect retry timers when browser tab is hidden to avoid background socket churn.

## Wave 69 completed

- WhatsApp pairing initialization (`startWaPair`) now cancels any active polling and expiration countdown intervals before starting a new session, avoiding timer leaks and false expiration state.

## Wave 68 completed

- `update_kanban_task` now strictly validates `title`, rejecting empty or whitespace-only updates to preserve database integrity.
- `MODELS_TIMEOUT` raised to 8.0s to accommodate upstream model discovery fan-out during heavy concurrent workloads.

## Wave 67 completed

- Kanban comment submission now strips and coerces blank/whitespace author strings to `web-panel` in both the comments table and task events payload.

## Wave 66 completed

- Global fetch toast interceptor now inspects both `init.method` and `input.method`, ensuring automatic toasts display when `fetch()` receives a `Request` object instead of a plain URL string.

## Wave 65 completed

- Unified SSE gateway log card caching order with the other log cards. Fresh incoming log card payloads are captured into memory before DOM removal, ensuring instant accurate restore.

## Wave 64 completed

- Restored log boxes now trigger `scrollAllLogsToBottom()` immediately upon un-dismissal, ensuring newest log lines are visible without waiting for a subsequent SSE tick.

## Wave 63 completed

- Network gateway restart action now uses a generation token (`netRestartGen`), preventing delayed failure messages or trailing 45s timer callbacks from overwriting subsequent network actions.

## Wave 62 completed

- Kanban auto-sync now checks an `isKanbanRefreshing` lock, preventing interval ticks from firing new requests while a previous board refresh is still in-flight.

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

- [x] Centralize frontend fetch error/status handling; several fetch chains call `response.json()` without checking `response.ok`.
- [x] Audit frontend rendering boundaries where API-derived strings reach `innerHTML`; use escaping or text nodes where required.
- [x] Verify mutation controls disable during submission and show consistent success/error toast feedback.
- [x] Audit SSE reconnect/error behavior and stale fragment replacement.
- [x] Add lightweight static checks for inline JavaScript syntax and route/API parity.

## Deferred / needs separate decision

- [ ] Runtime Hermes Kanban import from panel system Python: embedded runtime dependencies are not available to `/usr/bin/python3`; fallback path is schema-compatible and guarded.
- [ ] Remove or migrate production orphan board directory `kanban/boards/test-board-val` only with explicit cleanup authorization.

## Wave 78 — mobile & accessibility polish (task t_8946e3d3)

Baseline: HEAD 107407c, 289 unit + 113 contract GREEN, runtime IDENTICAL, panel active :1000.

- [x] 1. `lang="id"` on PAGE — FIXED. `<html>` had no lang attribute (screen readers could not pick pronunciation language); login page already had `lang="id"`. Commit 29ceae7, deployed + restarted hermes-panel.service, runtime IDENTICAL. Test: `test_page_declares_lang_attribute`.
- [x] 2. Auto-scroll tab aktif ke view — SUDAH AMAN, tanpa perubahan kode. Bukti: `switchTab()` (satu-satunya jalur aktivasi tab: klik, restore dari URL/sessionStorage, dan navigasi keyboard) sudah memanggil `tab.scrollIntoView({behavior:'smooth', block:'nearest', inline:'center'})`; handler panah kiri/kanan di NAV_SCRIPT memanggil `next.focus(); next.click()` yang masuk ke jalur yang sama; keenam tombol tab memanggil `switchTab('<nama>', this)`. Test: `test_active_tab_scrolls_into_view`. Tidak ada perubahan server, runtime sudah identik dengan commit (tidak perlu restart).
- [x] 3. Touch target — FIXED. `.tab` 42px→44px (termasuk override 390px yang tadinya justru turun ke 40px); `.btn-end-task`/`.btn-start-task`/`.btn-restart-task` (tombol aksi kartu layanan) sebelumnya tanpa min-height (~20px) → 32px + inline-flex centering; `.patch-notes-pages button` 30px→32px; tombol kartu profil 30px→32px; inline override di bawah 32px (26/28/24px: gw mode toggle, template Terapkan, Hapus Tugas, attachment Preview/Raw, Lihat attachment kanban, Tutup log warp, select template) → 32px. `.btn-action-sm` sudah 32px. Test: `test_touch_targets_meet_minimums` (aturan: `.tab` ≥44, kelas aksi ≥32, tidak ada min-height <32 di PAGE+NAV/NETWORK/SSE script).

## Wave 79 — hapus dependensi eksternal (task t_73060554)

Baseline: HEAD 107407c, 289 unit + 113 contract GREEN, runtime IDENTICAL, panel PID 1273024.

- [x] 1. Font Google render-blocking — FIXED. Ganti 3 `<link>` Google Fonts (preconnect + stylesheet) dengan `@font-face` lokal di `<style>`. Font self-hosted dari `/static/fonts/<name>.woff2` (Geist variable 400-700, Geist Mono variable 400-600, total 52.5 KB). `font-display:swap`. Route static Python stdlib: `Content-Type: font/woff2`, `Cache-Control: public, max-age=31536000, immutable`, tanpa auth (font = aset publik). Commit 7514b83, deployed + restarted hermes-panel.service. Test: `test_page_has_no_external_font_or_icon_dependency`, `test_font_route_serves_woff2_with_immutable_cache`.
- [x] 2. Icon jsdelivr CDN — FIXED. 10 referensi `cdn.jsdelivr.net` diganti: ICON_HERMES_LOGO → inline SVG (lingkaran gradient + inisial "H"); header logo → `{icon_hermes_logo}`; 6 icon katalog layanan (pi-hole, 9router, cloudflared, jellyfin, qbittorrent, docker) → inline SVG data URI monokrom (`_svg_data_uri()` helper, stroke `#94a3b8` cocok palette panel). Commit 7514b83.
- [x] 3. CSP bersih — FIXED. Buang `https://fonts.googleapis.com` dari `style-src`, `https://fonts.gstatic.com` dari `font-src`, `https://cdn.jsdelivr.net` dari `img-src`. CSP akhir: `default-src 'self'; script-src 'self' 'unsafe-inline'; style-src 'self' 'unsafe-inline'; font-src 'self' data:; img-src 'self' data:; connect-src 'self'; frame-ancestors 'none'; base-uri 'self'; form-action 'self'`.
- [x] 4. Hapus onerror fallback — FIXED. `<img onerror="this.src='.../docker.webp'">` di kartu layanan dihapus (tidak relevan dengan SVG data URI).
- [x] 5. Verifikasi — (a) `curl /status | grep -cE "fonts.googleapis|jsdelivr"` = 0; (b) `curl -I /static/fonts/Geist-400.woff2` → 200, `font/woff2`, immutable; GeistMono juga 200; unknown font → 404; path traversal → 404; (c) CSP header bersih dari domain eksternal; (d) 303 unit test (289 baseline + 3 baru + 11 worker lain), hanya 2 pre-existing failure dari worker lain (`test_active_tab_scrolls_into_view`, `test_72_dos_and_input_validation`); (e) 113 contract GREEN; (f) node --check JS syntax OK.
- [x] 4. `:focus-visible` global — SEBAGIAN SUDAH AMAN + 1 celah diperbaiki. Aturan global `:focus-visible{outline:2px solid var(--accent-light);outline-offset:2px}` sudah ada di PAGE (wave sebelumnya); `.search-input` dan `.net-input` yang men-set `outline:none` sudah punya pengganti (`:focus` border-color + box-shadow), halaman login juga (`input:focus{border-color:#60a5fa}`). Satu-satunya celah: textarea `#gw-config-yaml` punya inline `outline:none` — inline style mengalahkan aturan stylesheet sehingga ring fokus hilang tanpa pengganti. FIX: `outline:none` dihapus dari inline style, ring global kembali berlaku. Test: `test_focus_visible_style_present` (aturan global ada + tak ada outline:none tanpa pengganti, inline maupun CSS).

## Wave 80 — audit bug fungsional & optimalisasi resource (task t_0fe2afd8)

Baseline: HEAD 107407c (sebelum commit worker lain e7517a5/b118a7c self-host font),
289 unit + 113 contract GREEN, panel PID 1273024, RSS 39.064 KB, latency /status 0.069–0.272 s,
SSE 332.260 B / 9 event / 12 s (27,7 KB/s), 13 key per event.

### 1. Timer & interval leak — SUDAH AMAN, tanpa perubahan kode
Bukti (semua `setInterval`/`setTimeout` di PAGE/NAV/NETWORK/SSE script):
- `kanbanSyncTimer` (3803) & `NET.timer` (9034): interval tunggal seumur halaman, guarded
  `document.hidden` + tab tidak aktif; tidak di-clear karena memang auto-sync by design.
- `_waPairPollTimer`/`_waPairTimerInterval` (5765–6090): di-clear di `closeWaPairModal`,
  `cancelWaPair`, `startWaPair`, dan saat `rem <= 0`; poll hanya menjadwalkan diri berikutnya
  bila modal masih `show`.
- `NET.warpOp.timer` (8875): di-clear di `netWarpToggle` (sebelum assign baru),
  `netClearWarpOp`, dan di kedua jalur `.then`/`.catch` dari `netPost`.
- `retryTimer`/`hideDisconnectTimer`/`_hiddenTimer`/`_scrollTimer` (8020–8272): di-clear di
  `disconnect()` dan `onVisibilityChange`.
- Countdown restart (7953): `clearInterval(t)` sendiri saat `n <= 0`.
- Toast (3250/9187) & pbar (2464/2563/8069): one-shot, self-clearing, punya generation guard.
- Esc/backdrop tidak punya jalur tutup yang mem-bypass closer (CLOSERS map 9222 lengkap).
Tidak ada timer yang bertahan setelah modal ditutup. Nol perubahan kode.

### 2. Cache TTL & invalidasi — 1 temuan, DIPERBAIKI
- **TEMUAN**: `_tail_log_cache` (key `(path, n)`) tidak pernah di-evict, dan `?n=` di
  `/api/gateway-log` + `/api/whatsapp-log` menerima 1..1000 dari client. Satu client bisa
  mencetak 1000 entri per path, tiap entri sampai ~40 KB teks log → retensi ~40 MB di proses
  panel. Dampak nyata di STB 4 GB: RSS panel naik monoton, plus 1000× file read terpisah.
- **FIX**: `_TAIL_LOG_CACHE_MAX = 16` (evict FIFO) + `TAIL_N_ALLOWED = (60,100,200,500,1000)`
  dan `_tail_n_from_query()` yang snap `?n=` ke nilai terdekat. Cache lain sudah bounded /
  ter-invalidate: `_redact_cache` (32), `_status_probe_cache` + `_invalidate_status_cache`
  (dipanggil 20+ jalur mutasi), `_tail_log_cache`/`_casaos_compose_cache` (mtime+size),
  `_models_cache` (TTL 300 s + `reload_panel_config`), `_9router_host_cache` (300 s).
- Test: `test_61_tail_log_cache_is_bounded`, `test_61b_log_endpoints_clamp_n`.

### 3. Blocking I/O di request handler — 1 temuan, DIPERBAIKI
- **TEMUAN**: `Handler` tidak punya `timeout`, jadi socket koneksi tanpa timeout. Client yang
  mengirim `Content-Length` lalu stall menahan thread request selamanya —
  `ThreadingHTTPServer` spawn satu thread per koneksi. Dampak nyata: HP yang hang / tab yang
  ditutup paksa menumpuk thread + buffer di panel.
- **FIX**: `Handler.timeout = 60` (`StreamRequestHandler.setup()` menerapkannya ke socket).
  SSE menulis heartbeat tiap 15 s, jauh di dalam window ini.
- Sisanya sudah aman: semua 55 `subprocess.run` punya `timeout` (1.5–300 s), semua
  `urlopen` punya `timeout`, semua `sqlite3.connect` punya `timeout`, `Popen` di-`wait`
  dengan timeout + `killpg`, `TimeoutThreadingHTTPServer` (`daemon_threads`, `timeout=15`),
  `MAX_BODY_SIZE` 5 MB.
- Test: `test_62_handler_socket_has_timeout`.

### 4. Error handling silent — DIPERBAIKI
- **TEMUAN**: 8 blok `except Exception: pass` di `get_process_list()` (baris 14524–14716)
  menelan error saat membangun baris tabel proses. Dampak nyata: sebuah layanan hilang dari
  tabel tanpa jejak — user tidak bisa membedakan "mati" dari "panel gagal membaca".
- **FIX**: `_log_swallow(context, exc)` — rate-limited 1×/menit per context (mencegah spam
  stderr karena fragment dibangun tiap detik), dict bounded 64 entri. Delapan baris proses
  sekarang memanggilnya dengan context spesifik per layanan.
- Test: `test_63_process_row_failure_is_logged`, `test_63b_log_swallow_is_rate_limited`.

### 5. Race pada mutasi config.yaml — SUDAH AMAN, tanpa perubahan kode
Bukti: `_write_config_atomic()` sendiri memegang `_config_write_lock` (RLock, reentrant), dan
kesepuluh call site berada di dalam `with _config_write_lock:` yang juga membungkus
read-modify-write (`_read_config_for_write()` atau `open(CONFIG_PATH)` di dalam lock).
Tidak ada penulis yang membaca di luar lock lalu menulis di dalam lock, jadi tidak ada
lost-update. Jalur terkait: `save_gateway_platform_config`, `toggle_gateway_platform_config`,
`remove_gateway_platform_config`, `set_current_model`, `set_reasoning_effort`,
`set_aux_task_model`, `reset_all_aux_tasks`, `save_kanban_config`, dan jalur profil.

### 5b. Bug laten di `set_current_model()` — DIPERBAIKI (ditemukan saat audit area 5)
- **TEMUAN**: `re.subn(r"(\bdefault:\s*)\S+", ..., count=1)` mengganti `default:` PERTAMA di
  seluruh file. Pada config.yaml nyata `model:` ada di baris 1 sehingga aman, tapi begitu ada
  blok lain ber-`default:` di atasnya (mis. `agent.default:`), panel menimpa key yang salah
  dan model tidak berubah — tanpa error.
- **FIX**: rewrite dibatasi ke blok top-level `model:` (cari baris `model:`, batas sampai baris
  non-indented berikutnya), substitusi hanya di dalam rentang itu.
- Test: `test_64_set_current_model_scoped_to_model_block` (memakai decoy `agent.default`).

### 6. Optimalisasi payload SSE — DIPERBAIKI, terukur
- **TEMUAN**: change-detection lama all-or-nothing per payload. `current_state` memuat
  `cells.ts`/`uptime` yang berubah tiap detik → `should_push` hampir selalu True → 13 key
  (~37 KB) dikirim tiap tick. Pengukuran live 12 s: `gateway_log_card` 18,3 KB identik di
  9/9 event, `clean_junk_card` 2,0 KB identik, `gateway_list_block` 1,7 KB identik,
  `hermes_log_card` 0,6 KB identik — semuanya tetap dikirim, di-`JSON.parse`, dan di-diff
  terhadap `innerHTML` di client.
- **FIX**: `_sse_payload(frag, delta=True)` mengirim hanya key yang serialisasinya berubah;
  push loop skip bila delta kosong. Payload penuh tetap dipakai untuk connect awal dan
  `/api/status` (kontrak tidak berubah). Client `apply(d)` sudah guard per-key (`if(d.x)`),
  dan `set()` sudah skip bila `innerHTML` identik.
- **HASIL UKUR (12 s, 9 event)**: 332.260 B → 94.139 B (−72%). `gateway_log_card` 9/9 → 2/9
  event, `processes_table` 9/9 → 3/9, `profiles_block` 9/9 → 2/9. Event steady-state kini
  2 key (~1,7 KB) instead of 13 key (~37 KB).
- Test: `test_60_sse_delta_omits_unchanged_keys`, `test_60b_sse_push_loop_uses_delta`.

### Verifikasi
- `python3 -m unittest tests.test_install tests.test_panel` → 304 test OK (289 baseline + 8 baru
  Wave 80 + 7 worker lain).
- `python3 /root/tests/test_panel_contract.py` → 113 pass, 0 fail.
- `node --check` 3 blok script inline `/status` + `NETWORK_SCRIPT` → 0 error.
- Deploy: `install -m 0755` → `cmp -s` IDENTICAL → `systemctl restart hermes-panel.service` →
  active, listen 0.0.0.0:1000. PID 1434930.
- RSS: 51.868 KB stabil (flat 60 s idle, tidak ada leak), +0,7 MB dengan satu SSE client,
  thread 2 saat idle / 3 dengan client. Kenaikan vs baseline 39 MB berasal dari commit
  self-host font e7517a5 (woff2 + SVG inline di halaman), bukan Wave 80 — state tambahan
  Wave 78 hanya ~40 KB (`_sse_last_keys`).
- Latency `/status`: 0.036–0.070 s (baseline 0.069–0.272 s); `/api/status` 0.043 s.
- Catatan: threshold test gzip 85 KB → 90 KB. Baseline bergerak karena font self-hosted
  (commit e7517a5); guard tetap menangkap bloat 2×. Bukan regresi Wave 80 — gagal juga pada
  HEAD bersih (85.591 B).

### Commit
- `80408a0` fix(panel): Wave 78 — SSE per-key delta, bounded tail cache, handler timeout,
  swallow logging, scoped model rewrite.
- [x] 5. Empty/loading state slot SSE — 3 slot diperbaiki, 3 sudah aman. Audit: `aux-tasks-slot` (`render_aux_tasks_block` return "" kalau kosong), `process-table-slot` (tbody kosong tanpa baris placeholder), `clean-log-slot` (`render_clean_junk_card` return "" sebelum cleanup pertama) → ketiganya sekarang render placeholder informatif (`update-hint` / baris "Tidak ada layanan terdeteksi." colspan=5). Sudah aman tanpa perubahan: `gateway-list-slot` ("Belum ada gateway yang dikonfigurasi..."), `backup-models-slot` ("Belum ada model cadangan..."), `profiles-slot` (banner "Profil Saat Ini" selalu render). Render awal server sudah mengisi semua slot, jadi tidak ada gap loading. Test: `test_sse_slots_have_loading_or_empty_placeholder`.
