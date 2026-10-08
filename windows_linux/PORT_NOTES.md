# Porting the macOS edition's features to Windows — notes

Reference: the 80 flat `MAC__*.py` files + `MAC__README.md` (read-only). Target: this tree (`windows_linux/`).
Status words: **done** (ported, tested offline) · **merged** (both sides changed the file; merged by hand) ·
**skipped** (with the reason) · **untested** (works in tests, never seen on a real machine / service).

Test environment used for this port: a Linux container with CPython 3.11.15 + Tk 9.0 (no Windows machine was
available). Windows-only code paths (ctypes calls into kernel32/user32/dwmapi, winsound, `.ico` use by the taskbar,
`attrib +R`) are covered by tests that simulate their readings, and are marked **untested on Windows** below.

## 1. Inventory (real diff of the Mac reference against this tree, before any change)

### Files only in the Mac edition (18)

| File | Lines | Decision |
| --- | ---: | --- |
| core/netconn.py | 311 | port (A1) |
| core/netstats.py | 148 | port (A2) |
| resources.py | 475 | port (A4) |
| telemetry/monitor.py | 122 | port (A4) |
| core/closematch.py | 210 | port (B1) |
| ingest/suggest.py | 502 | port (B2) |
| audio/process.py | 351 | port (C1) |
| ui/painter.py | 86 | port (D1) |
| ui/viz.py | 352 | port (D1) |
| ui/dashboard.py | 531 | port (D1) |
| ui/card3d.py | 203 | port (D2) |
| ui/spectrum.py | 325 | port (D3) |
| ui/welcome.py | 339 | port (D5, without the Music-app step) |
| ui/icon.py | 307 | port (D6, plus a multi-size `.ico`) |
| core/shield.py | 576 | port (E1, Windows read-only attribute instead of macOS flags) |
| core/music.py | 226 | **skipped** — Apple Music hand-off (macOS only) |
| macglass.py | 217 | **skipped** — Liquid Glass (macOS 26 only) |
| macretina.py | 200 | **skipped** — Retina 2× picture placement (macOS only); Windows keeps its DPI-scale `S` |

### Files only in this tree (1)

| File | Decision |
| --- | --- |
| core/release.py | **yours only** — kept as is |

### Files identical in both (26)

`__init__.py`, `ai/*` (4), `audio/__init__.py`, `core/__init__.py`, `core/dedupe.py`, `core/disk.py`, `core/text.py`,
`ingest/__init__.py`, `ingest/amazon.py`, `base.py`, `browser.py`, `clean.py`, `generic.py`, `pandora.py`, `sheet.py`,
`spotify.py`, `webmeta.py`, `youtube.py`, `meta/__init__.py`, `meta/lyrics.py`, `meta/naming.py`, `ui/__init__.py`,
`ui/runner.py` — nothing to do.

### Files in both that differ (34)

Diff lines are `diff | grep -c '^[<>]'` (mac vs this tree). "Newer" says whose side carries the newer work.

| File | Diff lines | Newer | Plan |
| --- | ---: | --- | --- |
| core/netio.py | 542 | both | **merge** (A3): Mac shared pool, hedging, answer cache, stale retry, stall detection, adaptive segment count; keep yours: `ITUNES_LIMIT` values, `request(..., throttle=())` (iTunes 403), `Memo.forget` |
| core/engine.py | 630 | both | **merge**: Mac budget/governor worker pool, close matches, sound finishing, clean search, match-the-source, prewarm; keep yours: `_Transient`, `RECHECK`, `_cat(patient)`, `_wants_details`, `_missing_details`, `_complete`, `_make_cover/_cover_urls/_cover_for`, the details/cover extras pass, `det` / `details_tried`. Mac Apple-Music parts skipped. |
| core/catalog.py | 365 | **yours** | keep yours. (The Mac file has an older layout plus `mb_identify` (MusicBrainz lookup of songs with no id) — not requested, not ported; can be added on request.) |
| core/models.py | 9 | **yours** | keep yours (album_artist, totals, date, explicit 1/2, compilation, record_label); add only `Result.close` (B1) |
| meta/artwork.py | 202 | **yours** | keep yours |
| meta/tags.py | 164 | **yours** | keep yours (Mac has an older `advisory` field; your `explicit` 1/2 → `rtng` etc. stays) |
| core/quality.py | 48 | Mac | merge (B3): `fit` keeps sample rate/flags, `fit_pcm`, `match_note` |
| audio/sampler.py | 164 | Mac | merge (B3): wider band (64–192 kbps), digital-silence floor, three segments, `pcm_truth` for hi-res |
| audio/transcode.py | 70 | Mac | merge (C1): `run()`, `probe_pcm`, `encode(af=…)`, `writes_16bit` |
| config.py | 79 | both | merge: new fields (match_source, sound, clean_versions, close_match, motion, welcomed, shield_lock); Apple Music / native glass / sound_style fields skipped; backups (E2) |
| core/sources.py | 57 | Mac | merge (C2): clean query, `version_of` / `version_bias`, answer caching |
| autotune.py | 26 | Mac | merge (A4): uses resources.plan; stays the fallback |
| telemetry/stats.py | 178 | Mac | merge (A4/D1): time-constant smoothing, active-time average, ETA range, monitor sampling |
| telemetry/__init__.py | 3 | Mac | merge |
| platform_.py | 1245 | — | Windows functions added with the Mac names/signatures (A4, D4, E1); Mac-only functions not added |
| core/library.py | 5 | Mac | merge (`records()`) |
| ingest/apple.py, ingest/search.py | 5 / 16 | Mac | merge (`ttl=` caching, hedging). Mac's `explicit=-1/1` from these was **not** taken: your `explicit` means 1 explicit / 2 clean and comes from the catalogue |
| __main__.py | 4 | Mac | merge (`sys.setswitchinterval(0.001)`) |
| audio/sounds.py, audio/synth.py | 112 / 176 | Mac | sounds: system-sound style **skipped** (no Apple sounds on Windows); synth: see D4 |
| ui/* (app, shell, glass, charts, fmt, pulse, rows, splash, views_*) | 2–624 | Mac | merge per D1–D7; Retina (`d=` density, `put`) reduced to Windows' single density |

## 2. Progress

(updated as each group lands; see the end of this file for what is untested)

### Group A — networking & speed: **done** (merged where noted)

| Item | Status | Notes |
| --- | --- | --- |
| A1 core/netconn.py | done | as the Mac file; Windows note on WSAEWOULDBLOCK / select's except-set in the docstring. Tests: `test_netfast.py` (name cache, address interleaving, a hung address raced past, one pool for all threads, no cookies, prewarm reuse) |
| A2 core/netstats.py | done | unchanged from the Mac file (pure Python). Tests: RFC 6298 maths, timeouts, percentiles, thread safety |
| A3 core/netio.py | merged | Mac layer + yours kept: `ITUNES_LIMIT` (1.6 s / 1.2–14 s, now also `hedge_ok=False`), `request(..., throttle=())` → slows the limiter (15 s hold) like a 429, `Memo.forget`. `pressure()` also counts a throttled limiter. `prewarm()` is off when `MUSICDL_NO_PREWARM` is set (tests/helpers.py sets it so tests never reach the real services). `ttl=`/`hedge=` added to Deezer search, iTunes lookups and archive.org searches. Tests: hedging (wins, strict hosts never hedged, Stop), cache (ttl, copies, merging, failures not kept), stale-connection free retry, fitted timeouts, stall detection, iTunes 403 still pushback |
| A4 resources.py / telemetry | merged | `resources.py`, `telemetry/monitor.py`, `telemetry/stats.py` from the Mac; wording adapted; governor messages say "Easing off …" for battery / battery saver / site pushback / busy CPU (`pace` event carries `easing`; the progress card shows it only while easing). Engine worker pool rebuilt on `Budget` + `Governor` (≥10 songs), manual number = ceiling, encodes and the source check go through `Budget.cpu`. `autotune.py` uses `resources.plan` and keeps the old rule as fallback |
| A4 platform_.py | done (**untested on Windows**) | `hardware`, `physical_cores` (GetLogicalProcessorInformationEx), `cpu_ticks` (GetSystemTimes), `cpu_cores` (NtQuerySystemInformation class 8), `core_tiers` → None, `thermal_state` → 0 always, `power_source` (GetSystemPowerStatus: battery + battery saver), `compute_priority` (BELOW_NORMAL / IDLE priority class), `thread_priority` (SetThreadPriority), `reduce_motion` (SPI_GETCLIENTAREAANIMATION). Non-Windows: "can't say" values (no new Linux code). Parsers tested with synthetic data; a `skipUnless(win32)` test exercises the real calls on Windows |
| `__main__.py` | merged | `sys.setswitchinterval(0.001)` |

### Group B — search & matching: **done**

| Item | Status | Notes |
| --- | --- | --- |
| B1 core/closematch.py | done | as the Mac file. `models.Result.close` added (the only change to your models.py). Engine: Ask / Auto / Off (`close_match`), a picked recording (`extra={"picked": True}`) is accepted whatever its length. UI: **Choose…** pill on the Activity row → chooser popup (rich rows: title, channel · why, length) → one-song job on the "pick" worker; one question at the end of a run. `views_activity.py` is the Mac file plus one line (the chooser opens right-aligned under its pill, so it stays on the card). Tests: `test_closematch.py` (never-offered list, labels, safety, the search ladder, failures, Ask/Auto/Off, a picked file) |
| B2 ingest/suggest.py | done | as the Mac file; index file `suggest_index.json` in the settings folder. Source bar: quiet popup (no sound, rest of the window stays clickable), ↑/↓/Enter/Esc, 70 ms debounce, partial answers, warm-up 1.5 s after start. Tests: `test_suggest.py` against a local fake Deezer (ranking, kinds and caps, picks → collections, offline no-op) |
| B3 match the source | merged | `quality.fit` / `fit_pcm` / `match_note`, `sampler` wider band (64–192 kbps), digital-silence floor, three segments, `pcm_truth` for hi-res. `Settings.match_source` (on) → `capped = not advanced or match_source`; Advanced → Format has the switch. Old engine/cap tests pin `match_source=False` (they check that Advanced writes what it is told); new tests cover the switch. Tests: `test_cap.py` (MatchSourceTests, PcmTruthTests on ffmpeg-made padded / upsampled / real hi-res files, three CapJob tests) |
| tests/helpers.py | changed | yt-dlp *searches* answer "nothing found" in every test (the close-match ladder would otherwise reach YouTube) |

### Group C — sound & clean versions: **done**

| Item | Status | Notes |
| --- | --- | --- |
| C1 audio/process.py | done | as the Mac file. `transcode.run`, `probe_pcm`, `encode(..., af=)`, `writes_16bit` merged. Engine `_finishing` analyses each download once; the chain goes into the one encode; trimmed seconds (`fx.removed`) count toward the length checks and are stored in the library record (`removed`) so the next run's scan doesn't take a trimmed song for a wrong cut. Settings: `level`, `level_target`, `trim`, `trim_db`, `fade`, `enhance`, `dynamics` (Advanced → **Sound** tab, with Quick setup) and `polish` (Optimized, Settings → Songs). Tests: `test_sound.py` (probe parsing, trim/gain/fade planning, real ffmpeg analysis, settings, profiles, a whole job with trim, a failing analysis never fails a song) |
| C2 clean versions | merged | `sources.version_of`, `version_bias`, `CLEAN_QUERY`, `queries_for`, `_convincing(asked)`; Windows-only `_may_have_clean` (your `explicit` is 0/1/2, so a catalogue answer of 0 means "no clean edit to look for"). Engine `_explicit` sets the tag from the upload's title first, then the catalogue — **your 1 explicit / 2 clean meaning is kept**, never set from the switch alone. Tests: `test_sound.py` (CleanVersionTests) |

### Group E — protection: **done (untested on Windows)**

| Item | Status | Notes |
| --- | --- | --- |
| E1 core/shield.py | done, adapted | manifest (size + SHA-256), snapshot zip in `%APPDATA%\MusicDownloader\shield`, check / restore deleted / report changed / accept update, `boot()` run by `MusicDownloader.pyw` before the package is imported (and the checker itself taken from the snapshot if it was deleted). macOS flags → Windows **read-only attribute** (`os.chmod` without `S_IWRITE`). `.app` bundle code removed; a git checkout is never sealed. Lifeline: `Restore Music Downloader.bat` (cmd + PowerShell `Expand-Archive`, `attrib -R` first; `%` and `'` escaped; CRLF). Fixed while testing: several backups within the same second could sort below older ones and be pruned at once (`_next_stamp`) — the Mac file has the same bug |
| E2 backups | done | settings.json (last 3 distinct copies; a damaged file is set aside as `.damaged`, `Settings.load` heals first) and, new on Windows, each song folder's `.musicdl.json` (`backup_library` after every run, `heal_library` when `Library` finds it unreadable; a deleted record stays deleted) |
| E3 UI | done | `ui/protect.py` (ProtectMixin: check 3.2 s after start, "files changed" / "needs repair" sheets, Settings → Protection: status, lock, check now, cleaners and scanners, folders to exclude, Windows Security link, recovery script). `platform_.cleaner_apps` (Program Files folders; `MUSICDL_APP_DIRS` for tests), `open_virus_settings` (`windowsdefender://threatsettings/`). The Mac's first-run walk-through is D5 |
| Tests | done | `test_shield.py` (33): seal/check/restore/report/accept/update, tampered snapshot refused, runtime losses named, lock/unlock, `boot`, the real `.pyw` start-up step in a subprocess, lifeline quoting and line endings, settings and library backups and healing, `Settings.load` on damaged files, cleaner detection |

### Group D — interface: **done (untested on Windows)**

| Item | Status | Notes |
| --- | --- | --- |
| D1 Live dashboard | done | `ui/painter.py`, `ui/viz.py`, `ui/dashboard.py` as the Mac files; dashboard wording: "battery saver" for Low Power Mode, the Apple Music line removed. `views_advanced.py` Live tab → `Dashboard`; `RunState.pace_songs` / `pace_state` from the `pace` event. Windows `core_tiers()` is None, so the cores are one group. Tests: `test_interface.py` (ribbon/pillar/capsule geometry, log scale, picture sizes in both themes, layout tiers stay inside the box, stage groups, server names, the four numbers, painter newest-only / token / survives an error) |
| D2 artwork tilt | done | `ui/card3d.py` as the Mac file; `views_source.py` draws the tile through `Tilt`; `shell.region(..., track=)` added. Tests: flat corners, identity transform, quantizing, one picture per tilt, a Tilt on Tk following the pointer, settling back, still with Reduced motion |
| D3 spectrum | done | `ui/spectrum.py` as the Mac file; Format tab builds it when the card is at least 470 pt tall (`SPEC_MIN`), `tick_spec` in `tick_card_b`. Tests: cut-offs per format/bitrate, nearest bitrate, never past the sample rate, 24-bit range, settings → target, scale round trip, picture size |
| D4 Motion + launch | merged | `config.motion` (full/reduced, clamped); Settings → Experience → Motion; `Shell.reduced()` = the setting or `platform_.reduce_motion()` (asked at most once a minute); reduced → shorter reveal/dismiss, no scene cross-fade, still tile, the splash's calm path. `ui/splash.py` from the Mac (icon forms, flies into the header; the page is built hidden under it with `paint(staged=True)`), Liquid-Glass parts removed. `audio/synth.py` from the Mac (new app-made start-up sound timed to the animation, TPDF dither); `startup.wav` regenerated; `sounds.py` system-sound style **skipped**. Fixed vs. the Mac: with the splash on, the Mac never starts the connection prewarm or the suggestion warm-up — here `start_followups()` does both from either path. Tests: frames of both paths, title timing, calm is shorter, `reduced()` with the setting / Windows switch / once-a-minute cache, config clamp and round trip |
| D5 first run | done, adapted | `ui/welcome.py` written for Windows: hello → folder (Choose…; a test file is written, and *Controlled folder access* gets its own sheet when the write is refused) → protect (uses ProtectMixin's `set_shield_lock`, `start_shield`, `cleaner_note`) → done. The Music-app permission step and the quarantine flag are **skipped** (macOS only). `config.welcomed`; `protect.on_shield` holds its sheet until `welcomed` and the walk-through shows it at the end. Existing users see the sheets once (as on the Mac). Tests: the whole walk, Not now, skipping protection, waits for the splash and other sheets, the held protection sheet, blocked folder, Choose…, `can_write`, `on_shield` gating |
| D6 icon | done, adapted | `ui/icon.py` without the macOS margin; `glass.app_icon` delegates to it. `ico_file()` writes a 16–256 px `.ico` (each size drawn for itself) to `cache/icons/app-<fingerprint>.ico`, older ones removed; `shell` uses `iconbitmap(default=…)` on Windows, `iconphoto` elsewhere. Tests: sizes, transparent corners, every size in the `.ico`, written once, stale ones removed |
| D7 small files | merged | `charts.py`, `fmt.py` (`duration_range`), `views_quality.py`, `rows.py` taken from the Mac (D-only differences); `shell.py`: `D = 1`, `tkphoto`/`photo`/`cached` with a scale argument, `corner()`, `scene_photo()`, skip keys for the splash, 250 ms loop while minimised |
| `tests/ui_shots.py` | extended | launch animation, first-run sheets, the held protection sheet, Live during a made-up run, the tilted tile, Settings → Experience; light and dark, 0 errors |

## 3. What has not been tried on a real Windows PC

Everything was tested on Linux (CPython 3.11, Tk 9.0) with 563 offline tests, and the window was looked at under Xvfb
(`tests/ui_shots.py`, light and dark). Not run on Windows:

- the Windows API readings of A4 (CPU, memory, power, priorities, Reduce motion);
- the read-only lock as Windows applies it, `Restore Music Downloader.bat`, the Windows Security link, cleaner
  detection against real installs, and any real cleaner or scanner;
- `MusicDownloader.pyw` started by double-click (the start-up step was run with `python` on Linux);
- real services: Deezer suggestions, YouTube close-match searches and clean-version searches were faked;
- how the EQ presets and dynamics sound on real music;
- the interface of Group D on Windows itself: drawing speed of the Live pictures and the launch on slower computers,
  125–200 % display scaling, the `.ico` in the taskbar and Alt-Tab, Windows' Animation-effects switch, and the
  Controlled-folder-access check; nobody has listened to the new start-up sound.
