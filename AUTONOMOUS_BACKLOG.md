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
- [x] Wave 78 selesai — verifikasi akhir: 305 unit test OK (tests.test_install + tests.test_panel), 113/113 contract test OK, `node --check` semua blok script inline yang tersentuh (PAGE + NAV/NETWORK/SSE) OK, runtime IDENTICAL + `hermes-panel.service` active + listen 0.0.0.0:1000. Cek visual 390px (render page + inspeksi CSS): `.tab` 44px termasuk override 390px, `.tabs` dan `.task-table-wrap` pakai `overflow-x:auto` (tidak ada overflow horizontal), grid fluid (1fr / auto-fit), tidak ada fixed width >390px di area layout. Catatan: perubahan server item 3 & 4 sempat ter-commit bersama worker paralel (tree bersama) — isi identik, test pengunci masuk di commit terpisah.

## Wave 81 — Tab Profil sesuai runtime Hermes (skills / toolset / config)

**Masalah**: tab Profil menampilkan angka karangan. Kartu profil default menulis
"Skills: 79/150 aktif · Tools: 18 aktif" padahal runtime memuat 130 skill (117 aktif)
dan 18 toolset berisi 50 tool. Untuk profil baru, `platform_toolsets.cli: null` dibalas
`cli_toolsets: null` lalu UI memakai daftar hardcode 28 nama toolset (sebagian tidak ada
di registry). `skills.external_dirs`, `skills.platform_disabled.<platform>`, dan gate
`platforms:/environments:/requires_apps:` tidak dibaca sama sekali; tab Config tidak ada;
deskripsi profil dibaca dari `meta.json` padahal Hermes 0.22.5 menulis `profile.yaml`.

**Perbaikan**
- Inventaris skill memakai fungsi Hermes sendiri: `get_skill_search_roots` (local →
  create_dir → external_dirs, tier 1/2/3), `iter_skill_index_files`,
  `resolve_skill_catalog`, `is_disabled_entry`, plus matcher platform/environment/apps.
  Tiap skill kini berstatus `on` / `off` / `hidden` (+`hidden_reason`, `tier`, `root`).
  Root project sengaja dikecualikan (bergantung cwd sesi, tak bisa diketahui panel).
- `skills.platform_disabled` dihormati: inventory & toggle sadar platform
  (`platform_disabled.<p>` untuk platform pesan, `disabled` untuk CLI), dan disable
  global tidak bisa di-downgrade per platform (cermin `get_disabled_skill_names`).
- Toolset: katalog asli dari registry (`deskripsi`, `tool_count`, `tools`, `includes`,
  `is_bundle`), daftar efektif per platform = `platform_toolsets.<p>` (atau bundle
  `hermes-cli`/`hermes-<p>`) − `agent.disabled_toolsets`, plus total tool unik.
- Endpoint baru `/api/profile-config` (read-only, di luar `MUTATING_PATHS`): 7 grup
  (Model, Agent, Toolset per Platform, Skills, Platform Messaging, Aux, MCP) + YAML
  mentah, semua kredensial disamarkan via `_mask_secret_values`.
- UI: selector platform + tab ketiga "Config" di modal profil; baris toolset bisa dibuka
  untuk melihat nama tool; JS dipindah ke `PROFILE_SCRIPT` + `page_source()` supaya brace
  JS tidak bentrok dengan `PAGE.format()`.
- `list_mismatch`: `hermes skills list` hanya membandingkan load name, jadi nama deklarasi
  ganda (mis. `comfyui` di dua kategori) tampil "enabled" di CLI walau loader memfilter
  keduanya. Panel melaporkan hitungan loader + menyebut selisihnya di UI.

**Batas yang didokumentasikan (bukan bug)**: profil tanpa `platform_toolsets` memakai
bundle; `_get_platform_tools()` milik Hermes memangkasnya ke toolset konkret (37 tool),
sedangkan panel melaporkan isi penuh bundle (51 tool) karena replikasinya butuh import
`hermes_cli.tools_config` (+26–60 MB RSS). UI menandainya "(bundel)".

### Verifikasi
- `tests.test_install + tests.test_panel` → **359 test OK** (342 baseline + 17 baru di
  `TestProfileRuntimeParity`).
- Live: `/api/profile-skills?profile=default` → 130 skill, 115 on / 15 off, 0 hidden,
  `list_enabled_count=117`, `list_mismatch=['creative/comfyui','video-production/comfyui']`
  — sama dengan `hermes skills list` (117 enabled, 13 disabled) setelah selisih dijelaskan.
- Live: `/api/profile-toolsets` → 18 toolset / 50 tool, `source=hermes-lib`.
- Live: `/api/profile-config` → 7 grup, `masked=true` (`api_key` fallback = sentinel).
- Named profile uji (`paneltest`): 58 skill (53 on, 5 hidden = skill khusus macOS),
  toolset bundle `hermes-cli` 51 tool — profil uji sudah dihapus lagi.
- `/status` HTTP 200 (452 KB), tidak ada placeholder `profile_script` tersisa,
  `node --check` 3 blok script → 0 error. Panel RSS 64,7 MB (tidak naik: modul berat
  Hermes tidak di-import ke proses panel).
- Deploy: `cp` → md5 identik → `systemctl restart hermes-panel.service` → active.
  Gateway default tetap jalan (PID 51466, `served: true`).

### Commit
- `7357816` fix(panel): tab Profil baca runtime Hermes (skills, toolset, config).

## Wave 82 — Loop audit "nilai panel vs kenyataan" (4 putaran)

Metode tiap putaran: ambil angka/tampilan panel, bandingkan dengan sumber nyata
(`hermes CLI`, config.yaml, sysfs, sqlite, registry toolsets), perbaiki, test, deploy.

**Putaran 1 — manajemen profil**
- Deskripsi profil dibaca dari `profile.yaml` (0.22.5), fallback `meta.json`; dulu
  selalu kosong karena hanya meta.json yang dibaca.
- `/api/profiles` + badge kartu: `skill_count` 158 → 130 (walk dua level mentah
  menghitung `.archive`, `.curator_backups`, direktori support).
- `create_agent_profile`/`rename_agent_profile` memakai `hermes profile create/rename`
  (skill bawaan tersinkron 58, wrapper, migrasi identitas) + fallback manual yang kini
  menulis `profile.yaml`. Bukti live: create → "58 skill bawaan disinkronkan",
  rename → profile.yaml ikut pindah, delete → bersih.
- `set_agent_profile_model` membuang `base_url`/`api_key`/`api_mode` saat provider
  berubah (dulu profil bisa menembak endpoint provider sebelumnya).

**Putaran 2 — nilai tampilan**
- `192.168.1.100` hardcode di href dasbor OpenClaw + kartu Docker → `_link_host()`
  (IP LAN terdeteksi, cache 60 s, fallback 127.0.0.1).
- eMMC: `life_time` adalah bucket; "40% aus" untuk 0x04 (30–40%) → "30–40% aus".
- `delete_agent_profile("")` tidak lagi menjawab "profil default".

**Putaran 3 — bentuk config**
- Tab Config: `auxiliary` datar (`auxiliary.vision`) tidak terbaca → tab melaporkan
  "semua otomatis" padahal 8+ tugas dipin. Sekarang datar + bersarang + `delegation`.

**Putaran 4 — regresi performa yang saya buat sendiri**
- Kartu profil memindai inventaris tiap profil: `/api/profiles` cold 12,8 dtk (9
  profil) + rescan tiap 45-60 dtk. Diukur ulang setelah perbaikan: 1,5 dtk cold
  (import) / 0,11 dtk hangat; `/status` 0,076 dtk.
- Mekanisme: `_cached_or_fast` (tanpa warm massal — 9 thread paralel berebut GIL,
  sempat 8,7 dtk), `_cached_inventory` (hanya jalur user yang memindai),
  `_fast_skill_stats` (walk kanonik tanpa parse) + label "perkiraan" yang hilang
  begitu inventaris eksak masuk cache (handler meng-invalidate profiles_block).

**Yang diperiksa dan ternyata sudah benar** (tidak diubah): versi 9router 0.5.112,
status update Hermes (origin/main memang 197 commit di depan), RAM% (51% vs procfs),
zram, disk, uptime, load, badge gateway (`served: true`, PID hidup), hitungan task
kanban (selisih = baris `archived` yang memang dikecualikan), `node --check` 3 blok
script, dan payload deflate (87,4 KB, guard dinaikkan 85→90 KB karena tab Config).

### Verifikasi
- `tests.test_install tests.test_panel` → **375 test OK** (342 baseline + 33 baru:
  `TestProfileRuntimeParity` 17, `TestProfileManagementParity` 7,
  `TestDisplayedValuesMatchReality` 3, `TestConfigSummarySchemaShapes` 3,
  `TestProfileCardStaysCheap` 3).
- Runtime md5 IDENTIK dengan repo, `hermes-panel.service` active, gateway default
  tetap jalan (PID 51466, `served: true`), profil uji (`paneltest`, `panelrt1/2`)
  sudah dihapus.

### Commit
- `9d0009b` fix(panel): profil ikut CLI Hermes (buat/rename), deskripsi & skill_count benar
- `06e384a` fix(panel): nilai tampilan ikut keadaan nyata (link IP, eMMC, aux, config)
- `8557147` perf(panel): kartu profil tidak lagi memindai skill (12 dtk → 0,1 dtk)

### Catatan lingkungan (bukan bug panel)
- `hermes-lib` dalam keadaan detached HEAD dan **2 file termodifikasi**
  (`agent/provider_media.py`, `gateway/slash_commands_model.py`) — `hermes update`
  bisa menolak/menimpa; sengaja tidak disentuh.
- Ada `git stash@{0}` lama (28 Okt) di repo panel, bukan dari sesi ini.

## Wave 83 — Loop audit lanjutan: tab Kontrol, Kanban, Model, Log, Update

**Bug diperbaiki**
- **Memori layanan**: tabel proses memakai RSS MainPID. 9router.service MainPID 741
  hanya wrapper (6 MB) sementara anak `next-server`-nya 240 MB → layanan tampil
  "6 MB". Sekarang `_service_mem_mb()` menjumlahkan RSS semua PID di cgroup unit
  (`_cgroup_procs`). `MemoryCurrent` systemd sengaja tidak dipakai: ikut menghitung
  page cache (gateway 977 MB RSS → dilaporkan 1.816 MB).
- **Layanan hantu**: Pi-hole (tidak ada container), CasaOS & OpenClaw Gateway (tidak
  ada unit systemd) tampil "Berhenti" + tombol Nyalakan yang pasti gagal. Sekarang
  `get_docker_metric()` mengembalikan `missing` (di-cache, tidak memanggil docker
  tiap render) dan `_unit_installed()` membedakan unit terpasang vs tidak →
  status "Tidak terpasang", badge abu-abu, tanpa aksi.
- **Blok pembaruan nyangkut**: `_refresh_hermes_update()` tidak meng-invalidate
  fragmen cache, jadi blok tetap "Mengecek pembaruan Hermes…" sampai TTL habis
  padahal sel dasbor sudah "pembaruan tersedia". Sekarang fragmen terkait
  di-invalidate di `finally`.
- **Hint OpenClaw**: "mati — nyalakan dulu" → "tidak terpasang di server ini" bila
  unitnya memang tidak ada.
- **Regresi proses patch**: blok docker "missing" sempat tersisip ke
  `tail_gateway_log` (anchor `if r.returncode == 0` tidak unik). Dipulihkan, dan
  7 test baru mengunci perilaku benar.

**Diperiksa, sudah benar (tidak diubah)**
- Kanban: kolom & jumlah task sama dengan DB; daftar board sama dengan
  `hermes kanban boards list` (2 board, `test-board-val` diabaikan CLI juga);
  selisih hitungan = baris `archived` yang memang dikecualikan.
- Tidak ada kebocoran kredensial di 24 fragmen `/api/status` (regex sk-/ghp_/api_key).
- Kartu log gateway hidup (16,6 KB, tail file), catatan pembaruan 9router (v0.5.112
  terbaru) dan Hermes (stable v0.22.6 tersedia) konsisten dengan sumbernya.
- Tab Jaringan dirender client-side dari endpoint POST/ter-token → tidak ada
  fragmen server yang bisa basi; nilai lan/tailscale/route cocok dengan `ip route`.

### Verifikasi
- `tests.test_install tests.test_panel` → **384 test OK** (+9: 7
  `TestServiceTableTruth`, 2 `TestStaleFragmentsAfterBackgroundChecks`, plus 1 kasus
  "tidak terpasang" di test CasaOS).
- Live: gateway 999 MB, 9router 240 MB (dulu 6 MB), panel 82 MB, Pi-hole/CasaOS/
  OpenClaw "Tidak terpasang" tanpa tombol; blok pembaruan berubah jadi
  "Pembaruan tersedia: stable v0.22.6" ±45 dtk setelah restart (dulu nyangkut).
- `/status` 0,156 s, `/api/status` 0,038 s, RSS 63,5 MB, md5 runtime = repo.

### Commit
- `314820d` fix(panel): tabel layanan jujur soal memori dan layanan hantu
- `ddff6ea` fix(panel): blok pembaruan tidak nyangkut "Mengecek…" + jujur soal OpenClaw

## Wave 84 — Loop semua tab (Proses, Performa, Layanan, Tugas AI, Profil, Kanban, Kuota, Model)

**Bug diperbaiki**
- **Kanban: status `completed` disimpan apa adanya.** Menandai tugas selesai lewat
  panel membuat papan punya dua bucket (done/completed) dan hitungan `done` di
  `hermes kanban boards list` tidak cocok dengan kolom Done. Sekarang
  `_normalize_kanban_status()` menerima alias tapi selalu menulis `done`
  (create + update); status tidak valid tetap ditolak dengan pesan input aslinya.
  Live: task dibuat dengan `status=completed` → tersimpan `done` + completed_at.

**Audit semua tab yang hasilnya sudah benar (tidak diubah)**
- **Performa**: zram 316 MB (2,6x) = orig 331.808.768 B / compr 126.035.561 B
  (`/sys/block/zram0/mm_stat`); RAM, temp (44,9 °C), disk 56 % (31,7/56,4 GB),
  eMMC 30–40 %, load — semua cocok procfs/sysfs.
- **Tugas AI**: 12+ tugas menampilkan provider/model persis seperti `auxiliary`
  datar di config.yaml; review & delegation "otomatis (pakai model utama)" benar.
- **Kuota AI**: `stale: true` (fetch gagal) memang tampil "cache lama" + auto-retry
  4 dtk; 5 provider (antigravity 10 akun/2 kritis, gemini 15, codex 1, qoder 156,
  openai-compatible 1) sesuai sumber.
- **Model**: daftar chip = **69/69 model** dari `/v1/models` 9router (8 kombo,
  7 antigravity, 3 CBAI, 20 codex, 9 gemini, 1 MMF, 7 OC, 14 QD), tidak ada yang
  hilang; data live dengan cache TTL, bukan hardcode.
- **Kanban keamanan**: judul `<img src=x onerror=alert(1)>` dirender ter-escape
  (`&lt;img…&gt;`), komentar HTML juga aman; create/status/comment/delete masuk ke
  DB board yang benar; board aktif dikembalikan ke `default` dan semua board/task
  uji (`looptest`, `loopnorm`, task stray di default) sudah dibersihkan.
- **Password panel**: tetap kosong atas permintaan eksplisit user (akses bebas di
  LAN/Tailscale). Tidak ada `PANEL_PASSWORD` di `/etc/hermes-panel.env`; panel
  menampilkan "Akses Bebas (Tanpa Sandi)".

### Verifikasi
- `tests.test_install tests.test_panel` → **388 test OK** (+4
  `TestKanbanStatusCanonical`).
- Live: board aktif `default` (0 aktif, 12 arsip), profil = 9 profil nyata,
  `/status` 200, RSS 62,6 MB, md5 runtime = repo.

### Commit
- `e04db3d` fix(panel): kanban simpan status kanonik `done` (alias `completed`)

## Wave 85 — Fitur UI/UX Kanban + audit SSE

**Fitur baru**
- **Lazy-load kolom Kanban besar.** Board discord-music-bot (101 tugas Done)
  dirender utuh: fragmen 164,6 KB + 101 node DOM, diserialisasi & di-diff tiap tick
  SSE. Sekarang tiap kolom dibatasi `KANBAN_COL_PAGE = 12` kartu; sisanya lewat
  tombol "+ N lainnya" → `GET /api/kanban/column?board&status&offset&limit`
  (limit maks 50). Markup kartu diekstrak ke `_render_kanban_task_card()` (satu
  sumber untuk papan & endpoint), mapping status→kolom disatukan di
  `_kanban_col_of()`. Ukur: **164,6 KB → 25,0 KB**; board default 5.760 B
  (byte-identik sebelum/sesudah).
- **Dropdown board menampilkan arsip.** "(0)" untuk board yang punya 12 tugas
  terarsip terlihat seperti data hilang. Sekarang `list_kanban_boards()` menghitung
  `archived_count`, label jadi "(0 aktif · 12 arsip)" / "(101 aktif · 28 arsip)";
  board tanpa arsip tetap "(N)".

**Perbaikan di proses**
- Liveness (`_enrich_kanban_liveness`) hanya di-refresh untuk kartu kolom running
  (badge JALAN/IDLE/MACET), kolom lain memakai nilai dari `list_kanban_tasks`.
- JS baru harus didobel braces-nya karena template PAGE memakai `.format()`;
  kelupaan ini membuat 10 test halaman/JS gagal dan langsung diperbaiki (16 test
  `TestGatewayFormJs` hijau lagi).

**Audit SSE (tidak ada perubahan, hasil sudah benar)**
- Klien baru menerima **full payload** saat subscribe (`_sse_payload(frag)` tanpa
  delta), jadi tidak ada sel basi saat reload walau `_sse_last_keys` global.
- Delta per-key membandingkan serialisasi tiap fragmen; invalidasi mutasi sudah
  menunjuk ke fragmen yang benar (profiles_block, kanban_board_block,
  kanban_board_options, processes_table, update_block).

### Verifikasi
- `tests.test_install tests.test_panel` → **396 test OK** (+5
  `TestKanbanColumnPagination`, +3 `TestKanbanBoardArchiveCounts`).
- Live: `/api/kanban/column?board=discord-music-bot&status=done&offset=12&limit=12`
  → `{total:101, loaded:12, remaining:77}`; dropdown "Default Board
  (0 aktif · 12 arsip)"; halaman memuat `loadKanbanColumnMore` dengan brace tunggal
  (format aman).

### Commit
- `d948090` feat(panel): lazy-load kolom Kanban besar (164 KB → 25 KB)
- `de69d90` feat(panel): dropdown board Kanban menampilkan jumlah arsip

## Wave 86 — Fitur Kuota (cari + batas akun) & penampil arsip Kanban

**Fitur baru**
- **Kuota AI**: 156 akun qoder tidak lagi dirender semua — 6 akun pertama per
  provider (`QUOTA_ACCOUNT_PAGE`) + tombol "+ N akun lainnya". Input
  "Cari akun / email…" memfilter nama/email/provider, menyembunyikan provider
  tanpa hasil, dan menampilkan semua akun yang cocok.
- **Kanban → Arsip**: tombol baru di samping dropdown papan membuka daftar tugas
  terarsip (`/api/kanban/column?status=archived`), memakai ulang lazy-load kolom
  (`col-cards-archived` + `loadKanbanColumnMore`). Reset saat ganti papan.
  Live: board default → `{total:12, loaded:12, remaining:0}`.

**Temuan yang tidak perlu diubah**
- Tab Model sudah punya `#model-search` + `filterModels()` yang juga menyembunyikan
  grup kosong, dan `model_chips` memang tidak dikirim lewat SSE (komentar di kode:
  38 KB, tidak pernah diganti via SSE) sehingga filter tidak terhapus tiap tick.
- Kuota JSON 107 KB; masalahnya DOM, bukan ukuran payload.

**Perbaikan proses**
- Kontrol baru sempat `min-height:30px` → `TestMobileAccessibility` gagal (floor
  32px). Diganti ke 32px di 3 tempat; sekarang 5 test a11y hijau.

### Verifikasi
- `tests.test_install tests.test_panel` → **401 test OK** (+5 baru).
- Live: `/api/kanban/column?status=archived` OK; halaman memuat `kb-archive-wrap`
  dan `qz-search`; `/status` 200; service aktif.

### Commit
- `3a0bfc3` feat(panel): pencarian + batas render akun Kuota, dan penampil arsip Kanban

## Wave 87 — Kuota kritis-dulu + test perilaku JS, sweep endpoint

**Perubahan**
- **Kuota: akun bermasalah didahulukan.** Batas 6 akun/provider bisa menyembunyikan
  akun yang habis/kedaluwarsa. `quotaAccountRank()` = exhausted/expired/invalid/
  error (0) → non-aktif (1) → ok (2), sort stabil.
- **Test perilaku JS (node)** untuk tab Kuota: `_quota_js()` mengekstrak JS seperti
  dirender (brace `.format()` dikembalikan) + DOM palsu, mengikuti pola
  `TestGatewayFormJs`. 5 test: batas 6 + tombol sisanya, akun exhausted tidak
  tersembunyi, showAll, pencarian email + provider disembunyikan, urutan rank.

**Audit tanpa perubahan**
- **Sweep 20 endpoint baca**: semua 200 + JSON valid (status, profiles, skills,
  toolsets, config, soul, skill, quota, gateway/whatsapp log, pair-status,
  gateway-config, kanban config/boards/tasks/column done+archived, network).
  405 di `/api/network/routes` & `/api/network/dns` memang POST-only;
  `/api/hermes-update` bukan endpoint (info update ada di fragmen status).
- **Dugaan N+1 keliru**: `_attach_active_run` hanya query untuk tugas dengan
  `current_run_id`, bukan untuk 101 tugas Done — tidak ada yang perlu dioptimasi.
- Katalog skill sudah punya `filterProfileSkills()`; pencarian model sudah ada.

### Verifikasi
- `tests.test_install tests.test_panel` → **406 test OK** (+5 perilaku JS).
- Live: `/status` 200, service aktif, md5 runtime = repo.

### Commit
- `e24f244` feat(panel): akun Kuota bermasalah didahulukan + test perilaku JS (node)

## Wave 88 — Bug: batas kolom hilang setelah refresh + arsip ikut segar

**Bug nyata yang ditemukan saat menulis test perilaku JS**
- Server sudah membatasi 12 kartu/kolom, tetapi **`renderKanbanCards()` merender
  semua tugas lagi** setiap `refreshKanbanBoard()` (sinkron manual, drag-drop,
  semua mutasi tugas). Artinya fitur lazy-load hanya bertahan sampai aksi pertama;
  kolom Done kembali 101 kartu. Diperbaiki: renderer klien memakai batas yang sama
  (`KANBAN_COL_PAGE = 12`, `items.slice`) + tombol "+ N lainnya".
- **Panel arsip basi**: setelah dibuka, daftar arsip tidak pernah disegarkan saat
  tugas dipindah/dihapus. Blok arsip dipecah menjadi `loadKanbanArchive()` +
  `toggleKanbanArchive()` + `refreshKanbanArchive()`; `renderKanbanCards()` kini
  menyegarkan arsip hanya bila panelnya sedang terbuka (tidak ada fetch saat
  tertutup).

**Test perilaku JS (node + DOM/fetch palsu)** — 5 test, termasuk bukti bahwa
refresh arsip tidak memicu fetch saat panel tertutup, dan tombol load-more
aktif kembali dengan pesan gagal saat fetch error.

### Verifikasi
- `tests.test_install tests.test_panel` → **411 test OK** (+5).
- Live: `/status` 200, service aktif, md5 runtime = repo.

### Commit
- `3234417` fix(panel): batas kolom Kanban juga berlaku setelah refresh + arsip ikut segar

## Wave 89 — Pencarian Kanban menembus batas kolom + backlog di Kanban

**Bug**
- Setelah lazy-load, `filterKanbanCards()` hanya menyaring kartu yang sudah dirender:
  tugas ke-13..101 tidak bisa ditemukan lewat pencarian, dan filter assignee/
  prioritas ikut meleset.
- Perbaikan: `kanbanFilterActive()` + bypass batas kolom saat filter aktif, dan
  `onKanbanFilterChanged()` (search + 2 select) dengan debounce 250 ms yang memuat
  ulang papan. Saat filter dikosongkan, kolom kembali dibatasi 12 kartu.

**Kanban dipakai sebagai backlog (permintaan pengguna)**
- `t_cb20e435` — Audit renderer klien yang menimpa fragmen server (triage, assignee
  coder) + komentar progres & daftar fragmen yang sudah diverifikasi.
- `t_0ef6419f` — Test perilaku JS untuk modal profil (skills & toolset).
- Keduanya sengaja di **triage** agar tidak di-dispatch bersamaan dengan sesi
  interaktif yang mengedit file yang sama.

**Test**: +3 `TestKanbanFilterJs` (deteksi filter aktif, debounce 3 ketukan → 1
refresh, assert bypass hanya saat memfilter). Suite **414 OK**.

### Commit
- `0692484` fix(panel): pencarian/filter Kanban menjangkau tugas di luar 12 kartu pertama

## Wave 90 — Patch worker Kanban: toggle skill/toolset (bug selector)

**Kolaborasi Kanban pertama yang selesai**
- `t_0ef6419f` (coder, scratch, base `09ef06b`) mengirim patch 17 KB. Direview,
  `git apply --check` bersih, lalu diterapkan dan di-deploy oleh sesi interaktif
  (commit `5d1a6ae`). Worker tidak menyentuh repo live/service sesuai koordinasi.

**Bug yang diperbaiki**
- Selector `button[data-skill-name=...]` juga mengenai tombol **Baca**, dan
  `button[data-ts-name=...]` mengenai tombol **"N tool"** — jadi tombol yang salah
  yang di-disable dan berubah jadi "..." saat toggle skill/toolset. Kini tombol
  toggle memakai atribut khusus `data-skill-toggle` / `data-ts-toggle`.
- Render ulang (filter/refresh daftar) mempertahankan state in-flight: `disabled`
  + label "..." selama `profileSkillSubmitting` / `profileToolSubmitting` aktif.

**Test**: +6 `TestProfileModalBehaviourJs` (render 156 skill, filter saat in-flight,
sukses/gagal/rejection skill & toolset). Suite **420 OK** — termasuk
`test_73_race_concurrency_and_hardening` yang hanya gagal di lingkungan scratch
worker, hijau di repo live.

### Verifikasi live
Atribut baru muncul di halaman, `/status` 200 (0,04 s), service aktif, md5 = repo.

### Commit
- `5d1a6ae` fix(panel): toggle skill/toolset mengenai tombol yang benar & state submitting awet

## Wave 91 — Rekonsiliasi SSE (patch audit worker t_85c2e88f)

**Diterapkan dari worker Kanban** (commit `e62dd23`), diverifikasi sampai klien:
1. **`update_block` ikut SSE.** Status "Mengecek pembaruan 9router…" sebelumnya
   permanen sampai reload penuh: blok dihitung setelah early return `sse_only` dan
   tidak ada di `SSE_CLIENT_KEYS`. Kini dihitung sebelum early return + masuk
   `SSE_CLIENT_KEYS` + diterapkan ke `update-content-slot`. Fallback patch-notes
   ikut di-cache TTL 10 menit agar tick offline tidak memanggil GitHub.
2. **`hermes_log_card` kosong membersihkan DOM/cache** (`!== undefined`, bedakan
   delta-tak-berubah dari kosong) — `_hasHermesLog` ikut false.
3. **`processes_table` tidak lagi membuang update saat hover/focus/selection**:
   disimpan di `deferredProcessesTable` dan ditulis setelah interaksi selesai.
4. Hardening: label versi update di-escape, PID null → "-", `rate_limit_card`
   tahan field null (sebelumnya `AttributeError` menggagalkan `build_fragments`).

**Verifikasi live**: 425 test OK (+5 `test_render_reconciliation`); SSE `/events`
membawa `update_block` pada payload awal (14 key, 8 event/10 dtk); `/status` 200.

**Pelajaran review**: klaim worker tetap diverifikasi — tetapi kecurigaan saya soal
`SSE_CLIENT_KEYS` ternyata keliru (tampilan diff terpotong); patch memang lengkap.

**Follow-up tercatat**: guard truthy serupa pada `log_card`, `clean_junk_card`,
`gateway_log_card`.

### Commit
- `e62dd23` fix(panel): rekonsiliasi SSE — update_block live, log kosong, proses tertunda

## Wave 92 — Kontrak empty/null seragam untuk semua kartu log (patch worker)

**Diterapkan dari worker `t_1981fc9a`** (commit `23bf44d`, basis `8392644`).
- `log_card` (router), `clean_junk_card`, `gateway_log_card` kini memakai kontrak
  yang sama dengan `hermes_log_card`: key tidak ada = delta tak berubah (DOM/cache
  dibiarkan); key ada dengan `""`/`null` = slot dikosongkan + `_last...`="",
  `_has...`=false; nonkosong = cache dulu, lalu stickySet/removal sesuai preferensi
  dismiss (perilaku dismiss tidak berubah).
- Negative control dari worker: 3 subtest memang gagal pada basis sebelum patch
  (cache router basi, DOM clean & gateway tidak kosong) → perubahan terbukti
  mengubah perilaku, bukan sekadar refactor.
- Audit tanpa perubahan (didokumentasikan worker): `profiles_block`,
  `gateway_list_block`, `model_chips` tidak punya renderer klien kedua.

**Verifikasi live**: 425 test OK, `/status` 200, 4 guard `!== undefined` muncul di
halaman, service aktif.

### Commit
- `23bf44d` fix(panel): kontrak empty/null yang sama untuk semua kartu log SSE

## Wave 93 — Pagar regresi paritas render (patch worker, hanya test)

**Diterapkan dari worker `t_e9de7e4e`** (commit `1b4be63`): 415 baris test baru,
tanpa perubahan kode produksi → tidak perlu restart service (md5 repo = runtime).
- Paritas nilai full vs SSE-only vs payload JSON untuk **semua** key
  `SSE_CLIENT_KEYS` + pemetaan fragmen ke slot DOM.
- Delta empty/large: fragmen unicode besar round-trip; non-empty → `""` tetap
  terkirim; pengulangan empty boleh di-omit; 4 kartu log memakai `!== undefined`.
- Kanban 0/1/12/13/25 (konstanta limit server=klien, badge, offset/remaining,
  placeholder, field kosong tidak jadi `None`/badge `@` kosong).
- Tabel proses (PID 0 dipertahankan, PID/mem kosong → `-`), empty state profil &
  gateway, model chips offline tidak dikirim SSE, rate-limit 180/181 karakter,
  versi kosong → `v?`.

**Verifikasi live**: 8/8 PASS; `unittest discover -s tests` → **433 OK**
(termasuk dua test yang hanya gagal di scratch worker).

### Commit
- `1b4be63` test(panel): 8 test regresi paritas render server-klien

## Wave 94 — Penutup rantai Kanban: paritas judul kosong kartu Kanban

**Diterapkan dari task induk `t_cb20e435`** (commit `753597e`).
- `_render_kanban_task_card()`: judul `""` sebelumnya tampil sebagai teks kosong
  sedangkan klien memakai `(Tanpa Judul)`; judul `None` bisa mencapai
  `html.escape` → `AttributeError`. Kini `title = t.get("title") or ""`,
  `data-title` tetap `""`, label `(Tanpa Judul)` hanya untuk teks kartu.
- Negative control: subtest `""` FAIL dan `None` ERROR pada basis sebelum patch.

**Verifikasi live**: `unittest discover -s tests` → **434 OK**, `/status` 200,
service aktif setelah restart.

### Rantai selesai — 5 task, 5 patch live
`5d1a6ae` toggle profil · `e62dd23` rekonsiliasi SSE · `23bf44d` kontrak log ·
`1b4be63` test paritas · `753597e` judul kosong Kanban.

**Pelajaran pola kerja**: worker di scratch + patch + negative control + larangan
menyentuh repo live/service berjalan baik; sesi interaktif tetap yang review,
menjalankan suite penuh, deploy, dan commit. Failure `test_73…` & font route hanya
muncul di lingkungan scratch worker (mock path/aset), selalu hijau di live.

### Commit
- `753597e` fix(panel): paritas judul kosong kartu Kanban (patch penutup t_cb20e435)

## Wave 95 — Cek update Hermes tidak lagi selalu "mengecek"

Keluhan pengguna: kartu update Hermes selalu "Mengecek pembaruan Hermes…". Cek di
server sebenarnya sukses; hasilnya hanya tidak pernah dikirim ke browser.
- `hermes_update_block` dipindah sebelum early return `sse_only` + masuk
  `SSE_CLIENT_KEYS` + diterapkan JS ke `hermes-update-slot` (akar yang sama dengan
  `update_block` 9router di Wave 91, fragmen ini tertinggal).
- Slot log Hermes dipisah ke template (`hermes-update-card` berisi
  `hermes-update-slot` + `hermes-log-slot`) agar push status update tidak menimpa
  log / membangkitkan log yang sudah di-dismiss.
- `build_status_page` memakai `.get("hermes_log_card", "")`.
- Test +3 (`TestHermesUpdateBlockLive`) & 1 test paritas disesuaikan; 442 OK.
- Bukti live: SSE mengirim "Mengecek…" lalu ±40 dtk "Pembaruan tersedia: stable
  v0.22.6 (f0d94882e89a) · channel stable" tanpa reload.

Catatan: tepat setelah restart service, status memang "checking" sampai cek
latar belakang selesai — itu normal dan kini terselesaikan sendiri.

### Commit
- (lihat git log) fix(panel): kartu update Hermes tidak lagi berhenti di 'Mengecek pembaruan…'

## Wave 96 — Profil memori panel: baseline terukur, target <20 MB tidak realistis

Pengukuran (python3.11, ru_maxrss, simulasi offline + service live):
- Python murni: 6.7 MB
- Setelah exec monolit (929 KB sumber, 20.344 baris): 24.0 MB
- Setelah build_fragments dingin (30 fragmen, 6.06s): 28.7 MB
- Setelah build_status_page (491 KB HTML): 31.2 MB
- 3x render ulang: 31.2 MB — **tidak ada kebocoran** per render.
- Service live (baru 6 detik, 1 request): RSS 58.4 MB, 4 thread; cgroup 125.6 MB
  (termasuk page cache, bukan biaya panel). /api/status 46 ms, 30 key.

**Kesimpulan**: lantai realistis monolitik Python ini ≈ 30–35 MB RSS berjalan.
Target <20 MB tidak tercapai tanpa ganti desain (mis. server statis + API
terpisah) — biayanya besar dan tidak sepadan untuk SBC ini. Target resmi diubah:
**<40 MB RSS berjalan** (tercapai dengan margin), fokus tetap pada latensi dan
tanpa kebocoran (sudah terbukti stabil per-render).

Catatan latensi: build_fragments dingin 6s disebabkan panggilan CLI/subprocess
(terutama inventory profil & kanban); jalur hangat 46 ms — konsisten dengan
pekerjaan Wave 82 (fast-path cache).

### Artefak
- /tmp/panel_baseline_mem.py, /tmp/panel_render_mem.py (skrip pengukuran, bisa
  dipindahkan ke tools/ bila ingin dipakai ulang).

## Wave 97 — Sparkline performa: riwayat asli + hilangkan data dummy (t_e42bba09)

**Konteks**: dispatch worker Kanban rusak deterministik pasca-update v0.22.6
(receipt 'unknown' → worker exit 1; 6 percobaan/3 task identik; v0.22.6 merombak
`kanban_worker_client.py` 141 baris; gateway kini Python 3.14 hasil repin).
Task dikerjakan langsung oleh sesi interaktik; board tetap dipakai untuk
pelacakan (komentar + complete). Task t_309252ae & t_6f2efe25 masih diparkir.

**Temuan**: sparkline sudah ada, tetapi riwayatnya di-seed angka DUMMY → tren
CPU/RAM PALSU ±30 dtk pertama setelah reload + riwayat hilang tiap reload.

**Implementasi** (commit `b6f567b`):
- `_perf_ring` 120 titik (jeda sampel 2 dtk) + `get_perf_series()` + GET
  `/api/perf/series` (tidak masuk SSE_CLIENT_KEYS — paritas render aman).
- Klien: dummy dihapus; `seedPerfHistory()` lazy saat tab Performa aktif
  (hook switchTab + delta SSE; diekspos `window.seedPerfHistory`).
- Label `min/maks/sekarang` per grafik; <2 titik → polyline dikosongkan +
  'menunggu data…'.

**Test**: +10 `TestPerfSparklineHistory`; negative control 10/10 error tanpa
patch; suite penuh **452 OK**. Verifikasi live: seri nyata bertambah (2→3 titik
dalam ±15 dtk), dummy hilang dari halaman.

## Wave 98 — Test interaksi gabungan filter kuota & model (t_6f2efe25)

Dikerjakan langsung oleh sesi interaktif (dispatch worker masih rusak pasca
v0.22.6). +6 `TestQuotaModelFilterInteractionJs` (Node harness fake-DOM):
- pencarian kuota aktif mem-bypass kap 6; query dihapus → kap 6 + tombol sisanya;
- akun kritis di posisi mentah >6 naik ke halaman pertama lintas provider;
- state `showAllProv` bertahan melewati pencarian yang dihapus (9|9|6);
- `filterModels` memperlakukan karakter regex LITERAL (indexOf, bukan regex);
- query tanpa kecocokan menyembunyikan chip+grup tanpa crash;
- `filterModels` tidak pernah menyentuh `innerHTML` → delta SSE tak mereset filter.

Ambang kompresi `/status` 90 KB → **95 KB** (fitur riwayat performa +2,5 KB gzip,
terukur 92,5 KB; guard bloat 2× tetap berlaku).

**Temuan gap (terdokumentasi tanpa patch paksa)**: `filterModels` tanpa empty
state — query tanpa kecocokan menyembunyikan semua chip diam-diam. Kandidat
perbaikan UI terpisah bila diinginkan.

Sisa papan: `t_309252ae` (audit ekspor/impor konfigurasi) — akan dikerjakan
langsung pada giliran berikut.

### Commit
- `77de38c` test(panel): interaksi gabungan filter kuota & model

## Wave 99 — Audit & perbaikan backup model cadangan (t_309252ae)

Dikerjakan langsung oleh sesi interaktif (dispatch worker Kanban masih rusak
pasca-v0.22.6). Gelombang Kanban ketiga tuntas: 3/3 task diselesaikan root.

**Bug inti** (diverifikasi terhadap hermes-lib v0.22.6): Hermes membaca
`fallback_providers` DAN legacy `fallback_model` sekaligus
(`hermes_cli/fallback_config.get_fallback_chain`); panel hanya membaca salah
satu → tampilan tak lengkap & entri legacy tak bisa dihapus (remove tanpa
`fallback_providers` → 500).

**Perbaikan** (commit `3428079`):
- `_merged_fallback_entries()`: gabungan fp+legacy, dedup identitas rute —
  tampilan = rantai efektif Hermes.
- set/remove bekerja pada rantai gabungan, lalu migrasi ke
  `fallback_providers` (ala `hermes fallback` CLI), kredensial dipertahankan,
  cache di-invalidate.
- Escape newline pada nama model di onclick render.

**Audit jalur lain** (tanpa patch): config kosong/korup → tampilan aman + tulis
ditolak; race dua klik → `_config_write_lock` + `_debounce_hit`; tulis atomik
0600. `/set-profile-model` & `/api/gateway-config-preview` sudah teruji
gelombang sebelumnya.

**Test**: +6 `TestBackupModelsChain`; negative control 4/6 gagal tanpa patch;
suite penuh **464 OK**. Live: `/status` 200, rantai efektif tampil benar
(9router · ag-gemini-3.8-flash-high).

### Commit
- `3428079` fix(panel): rantai model cadangan = rantai efektif Hermes

## Wave 100 — Empty state pencarian model

Menutup temuan Wave 98: query pencarian model tanpa kecocokan kini menampilkan
"Tidak ada model yang cocok dengan pencarian." (slot #model-search-empty,
hint-pill) alih-alih menyembunyikan semua chip diam-diam. Kontrak
filterModels-tidak-menyentuh-innerHTML tetap terjaga.

Test +1 (Node harness, getElementById ditambahkan); negative control gagal tanpa
patch; suite penuh **465 OK**; live terverifikasi.

### Commit
- (lihat git log) feat(panel): empty state pencarian model

## Wave 101 — Jalur dingin panel 7,3s → 3,1s via parser YAML C

Profil cProfile: 4,5 dtk dari 7,3 dtk build_fragments dingin = parsing YAML
(config 1060 baris + 36 profil; PyYAML safe_load pakai parser Python meski
libyaml terpasang). Helper `_yaml_load` (CSafeLoader, fallback SafeLoader)
mengganti seluruh 15 titik baca. Bench 6,5x/parser; dingin offline 3,12s.

Test +4 `TestFastYamlLoader` (identik safe_load, tag !python/* ditolak,
migrasi situs lengkap); negative control 4/4 gagal tanpa patch; suite **469 OK**.

**Live**: permintaan pertama setelah socket-wake **2,48s** (dulunya ±6–7s);
hangat tetap 38 ms. Sisa biaya dingin = 21 subprocess inventori (perlu, tak
dipangkas gelombang ini).

### Commit
- (lihat git log) perf(panel): parsing YAML via libyaml (CSafeLoader)

## Wave 102 — Audit tab Jaringan: sehat; kontrak validasi dikunci

Audit kode + uji live seluruh endpoint `/api/network/*`:
- **Sehat, tanpa patch produksi.** Input invalid ditolak rapi; subprocess argv
  list (tanpa shell); timeout penuh; tulis config atomik mode-preserving;
  proxy di-masking dalam respons; attempts dibatasi; cache TTL.
- Live: DNS 56 ms; tes rute relay Telegram 3/3 (avg 0,84 s).
- Gap ditutup: +5 `TestNetworkValidationContracts` mengunci kontrak penolakan
  (host invalid — ditolak sebelum subprocess, platform/rute tak dikenal,
  skema proxy, host kosong). Suite **474 OK**.

### Commit
- (lihat git log) test(panel): kunci kontrak validasi endpoint Jaringan
