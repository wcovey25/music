# Handoff: finishing Group D (interface) of the Mac → Windows port

> **Completed.** Group D is done; see the Group D table in `PORT_NOTES.md` for what was ported, adapted or skipped and
> what is untested. This file is kept as the record of the plan.

Groups A (networking), B (search and matching), C (sound and clean versions) and E (protection) are **done and
committed** on `claude/loving-ptolemy-1hu6b5`. **Group D, the interface polish, is not started.** This file has
everything needed to finish it. Read `PORT_NOTES.md` first; it holds the inventory and the status tables.

## 0. Rules (from the person who owns this repo — keep them)

- Work only in `windows_linux/`. **Never** touch V1/V2 or the user's real music folders. **Don't** create `macos/` or
  `sync_macos.py`. **No new Linux code** (Linux paths may stay as "can't say" fallbacks). **Delete nothing.**
- On Windows run Python only as `py -3`. In this container use the venv at `$S/venv/bin/python` (3.11 with Tk), where
  `S=/tmp/claude-0/-home-user-music/5d74a34e-7ac5-5134-9e73-6835767e256b/scratchpad`. If the scratchpad is gone, make a
  venv with `pip install -r requirements.txt` and use `xvfb-run` for anything with a window.
- Tests use temp folders and local servers only, **never the internet**. `tests/helpers.py` already sets
  `MUSICDL_HOME` to a temp dir and `MUSICDL_NO_PREWARM`, and blocks yt-dlp searches.
- **The user's own work wins** in these places; don't take the Mac version of them: `meta/artwork.py`, `meta/tags.py`,
  `core/catalog.py`, `core/release.py`, `core/models.py`, `tests/test_meta.py`, the netio items (`ITUNES_LIMIT`,
  `throttle=`, `Memo.forget`), and the engine's metadata and cover pipeline. The Windows `explicit` tag means
  **1 explicit, 2 clean**.
- Be honest. Anything not run on a real Windows PC is labelled **untested** in the README and in PORT_NOTES.
- Every ported feature gets offline tests. Update both READMEs in the "Limits, honestly" style and keep the test count
  in `README.md` → *For developers* current. Update `PORT_NOTES.md`.
- Commit messages end with the two attribution lines used in the earlier commits (`git log -3` shows them). Don't put
  model names in commits or in pushed files. Push with `git push -u origin claude/loving-ptolemy-1hu6b5`. **No PR**
  unless asked.

## 1. Tools

- **The Mac reference** (read-only) is in `$S/macpkg/musicdl/…`, laid out like `windows_linux/musicdl/`. The Mac
  README is `$S/mac/To Upload - Mac Version/MAC__README.md`.
- **Diff one file:** `$S/d.sh ui/app.py` runs `diff -u` with the Windows file first and the Mac file second.
- **Headless screenshots:** `tests/ui_shots.py` (committed). Run it as
  `xvfb-run -a -s "-screen 0 1280x900x24" python tests/ui_shots.py OUT light|dark`. It starts the real `App` in a temp
  `MUSICDL_HOME` with an installed copy that has one changed file and fake cleaners. Then it clicks (`cv.event_generate`)
  and scrolls (`scroll_to`) through the main page, the Sound tab, suggestions, Activity with the close-match chooser,
  and Settings. ImageMagick's `import` takes the shots, and exceptions go to `OUT/errors.txt`. Extend it with steps for
  each D feature and look at the PNGs (the Read tool shows images) in both themes. B/C/E were checked this way; they
  showed no errors and the layout was clean.
- **If the scratchpad is gone** (new container): the Mac reference came from the user's upload ("To Upload - Mac
  Version"). Ask for it again, unpack it, and point `d.sh`-style diffs at it. The flat `MAC__*.py` names map to the
  package paths (`MAC__ui__dashboard.py` → `musicdl/ui/dashboard.py`).
- **Full suite:** `cd tests && $S/venv/bin/python -m unittest discover`. It took about 2.5 minutes here. The
  "disk full: pausing" and "[Errno 28]" lines are simulated and expected.

## 2. What is left, file by file

Diff sizes are the number of `<`/`>` lines between Windows and Mac, measured after B/C/E were done.

### Files that exist only on the Mac (copy them, then adapt)

| File | Lines | Does | Windows notes |
| --- | ---: | --- | --- |
| `ui/painter.py` | 86 | background thread that renders the dashboard pictures | `platform_.thread_priority` already exists (A4) |
| `ui/viz.py` | 352 | ribbon chart, core pillars, slot capsule, server range bars (PIL) | pure PIL; Windows `core_tiers()` returns None, so one tier |
| `ui/dashboard.py` | 531 | the **Live** tab as a dashboard (speed ribbon, a pillar per core, slots, latency bars) | replaces the Windows `draw_live`/`tick_live` in `views_advanced.py` |
| `ui/card3d.py` | 203 | `Tilt`: the artwork tile on card A leans toward the pointer | hooked in `views_source.py` (see below) |
| `ui/spectrum.py` | 325 | Format tab: a spectrum picture of what the chosen format keeps | hooked in `views_advanced.py` and `app.py` (see below) |
| `ui/welcome.py` | 339 | first-run sheets **plus** the shield logic | **Protection logic is already in `ui/protect.py`** (ProtectMixin). Port only the first-run sheets (`maybe_welcome`, `finish_welcome`, `welcome_folder*`, `welcome_protect*`). **Leave out the Music step** (`welcome_music*`, `ask_music_permission`, `on_perm`, `music_permission_text`) and the quarantine flag (`platform_.quarantined`/`clear_quarantine` are macOS only; Windows has no such flag worth clearing). Have it reuse ProtectMixin's methods instead of duplicating them |
| `ui/icon.py` | 307 | the new app icon (squircle, lit notes), cached on disk | Windows uses `gk.app_icon(256)` in `shell.py:39`. Use `icon.app_icon(size)` without `MAC_MARGIN`. Also write a **multi-size `.ico`** (16/24/32/48/64/128/256 via Pillow `save(..., sizes=[…])`) to `platform_.cache_dir()` and use `root.iconbitmap(default=path)` on Windows so the taskbar and Alt-Tab get crisp small sizes. Keep `iconphoto` as the fallback |

Skip, as PORT_NOTES says: `macglass.py`, `macretina.py`, `core/music.py`.

### Files on both sides that differ (merge by hand: take the D parts, keep everything Windows-side)

| File | Diff | What to take |
| --- | ---: | --- |
| `ui/splash.py` | 624 | the new launch sequence (icon flies into the header), the `CALM` path for Reduced motion. Check the Windows header layout (`app.draw_header`) for the landing spot |
| `ui/app.py` | 291 | `WelcomeMixin` in the class bases and `_init_welcome()`; `root.after(WELCOME_DELAY, maybe_welcome)` in `finish_start`; `tick_spec(now, dt)` inside `tick_card_b` (Mac line ~784); dashboard wiring. **Keep** the Windows `ProtectMixin`, `"suggest"`/`"shield"` events, picks, close matches and `RunState.settings`, which are all done |
| `ui/glass.py` | 220 | new chip, tile and badge drawing used by `card3d`, `spectrum` and `dashboard`. The Mac `d=` density argument is always 1 on Windows: keep the signatures with `d=1` defaults, or drop the argument |
| `ui/shell.py` | 173 | `reduced()` (Motion setting **or** `platform_.reduce_motion()`, which Windows already has). Animations get shorter or simpler when it is true. **Already done here:** `put()`, quiet popups, rich popup rows, `popup_move`/`popup_accept`, `on_sheet`, `open_sheet(follow=)` |
| `ui/views_advanced.py` | 140 | the `Spectrum` on the Format tab (`self.spec`; build it when `bh >= SPEC_MIN`, move the rows below it), `tick_spec`, `Dashboard` for the Live tab. **Already done:** the Sound tab, Match the source, six tabs |
| `ui/views_source.py` | 53 | `from .card3d import Tilt`; `self.a_items["tile"] = Tilt(self, gk.tile_image(...), x, y)` where the tile is drawn (Mac ~line 370). **Already done:** suggestions |
| `ui/views_settings.py` | 55 | the **Motion** row (`Full`/`Reduced`) under Experience. The rest is Apple Music, which is **skipped**. **Already done:** Close matches, Songs, Protection |
| `ui/charts.py`, `ui/fmt.py`, `ui/views_quality.py`, `ui/rows.py`, `ui/pulse.py` | 52 / 21 / 21 / 14 / 2 | small drawing and formatting changes the new views use. Take what the new files call |
| `config.py` | 47 | add `motion: str = "full"` (clamped to full or reduced) and `welcomed: bool = False`. **Skip** the Apple Music, native glass and `sound_style` fields |
| `audio/sounds.py`, `audio/synth.py` | 112 / 176 | **skip** the macOS system-sound style. Port the synth changes only if the new cues are app-made (no Apple sound files) |

### Watch out for

- **First run must not fight Protection.** On the Mac, `on_shield` waits until `welcomed` before showing the "files
  changed" sheet (`welcome.py:79`). Do the same in `protect.py` `on_shield` once `welcomed` exists, so two sheets never
  stack. Existing users have no `welcomed` key, so they would see the walk-through once. Decide whether that is
  wanted (the Mac does show it) and say so in the README.
- `views_advanced.py` currently clamps the tab bar to `min(bw - 56, 540)`. Make sure the spectrum and the rows still fit
  at the smallest window size (look at `shell.py` minsize) at 100 % and 150 % scaling (`S`).
- The Windows `S` (DPI scale) still applies everywhere. The Mac's `D` (Retina) is 1 here. `self.put()` is just
  `create_image`; keep calling it so the code stays shared.
- `views_activity.py` is already identical to the Mac's.
- New canvas tags must be added to the `CARD_B` / `EVERYTHING` tuples in `app.py` if they live on card B. Otherwise
  page changes leave them behind.

## 3. Tests to add (offline)

- `viz`: geometry helpers (`ribbon_geometry`, `pillar_layout`, `capsule_cells`, `log_x`) and that each picture function
  returns an image of the asked size, in both themes.
- `spectrum`: `target()` and `settings_target()` for each format and bitrate (MP3 128 → about 16 kHz, FLAC → full band),
  `to_x`/`to_hz` round trip, and `image()` size.
- `card3d`: `corners`/`coefficients` (the identity at no tilt), `quantize`, and a `Tilt` on a headless Tk that follows
  `<Motion>` and settles back.
- `icon`: sizes, the `.ico` holding every size (open it with Pillow and read `.info['sizes']`), and the disk cache keyed
  on its fingerprint.
- `welcome`: shown once, never again after `welcomed`; "Not now" ends it; the protect step calls `set_shield_lock`.
- `config`: `motion` and `welcomed` clamp and round trip; `Shell.reduced()` with the setting and with
  `platform_.reduce_motion` stubbed.
- Extend `tests/ui_shots.py` so it opens every tab, Live during a fake run, Settings, and the first-run
  sheets, in light and dark, with no exceptions (`report_callback_exception` collects them).

## 4. Then finish the port

1. Run the full suite and fix what breaks. Run `tests/stress.py 400 8` if time allows.
2. Look at the screenshots in light and dark (main page, every Advanced tab, Live during a run, Settings, the first-run
   sheets, the splash).
3. Update `windows_linux/README.md`: Live dashboard, artwork tilt, spectrum, Motion, first run, icon. Mark each
   **untested on a real Windows PC**. Update the test count. In `PORT_NOTES.md`, fill in a Group D table with the same
   status words, and finish the "untested" list at the end.
4. In the top-level `README.md`, update the "Mac features brought to Windows" list and the test count.
5. Commit and push (see §0).
