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
