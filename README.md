# Music Downloader V3

A lightweight music downloader with a calm, glassy interface. Paste a playlist, album or song link from Spotify,
Apple Music, YouTube Music, Amazon Music, YouTube, Pandora (or any other page), and get tagged audio files with cover
art. Two modes — **Optimized** (Good / Better / Best) and **Advanced** (formats, bitrates, flags, naming, live
telemetry) — plus an optional **AI Mode** (local Ollama, or your own OpenAI / Anthropic / Gemini key).

| Folder | For | Start with |
| --- | --- | --- |
| [`windows_linux/`](windows_linux/README.md) | Windows 10/11 and Linux | Windows: `setup_windows.bat`, then `MusicDownloader.pyw`. Linux: `bash run.sh` |
| [`macos/`](macos/README.md) | macOS | `./run.command` (then `./run.command --app`) |

V1 and V2 are untouched and live elsewhere. Both editions share one code base; `sync_macos.py` copies the shared
code from `windows_linux/` into `macos/` (only `musicdl/platform_.py` is different in each).

## How it is put together

| Piece | Where | Does |
| --- | --- | --- |
| UI | `musicdl/ui/` | Tk canvas painted with Pillow-made glass, animations, startup sequence |
| Core downloader | `musicdl/core/`, `musicdl/ingest/` | links → song lists; parallel, retrying, cancellable downloads |
| Metadata parser | `musicdl/meta/` | titles, tags, cover art, lyrics, file naming |
| Audio / DSP | `musicdl/audio/` | ffmpeg encoding, sound-cue synthesis (launch, clicks, completion) |
| Telemetry | `musicdl/telemetry/` | speed, latency, searches per minute, ETA |
| AI connectors | `musicdl/ai/` | Ollama, OpenAI, Anthropic, Gemini |

## What has and hasn't been checked

Checked on Windows 11 / Python 3.11:

- 108 automated tests pass (links, engine, tags, encoding, sounds, AI connectors against a local fake server).
- The window was viewed (dark and light, smallest and normal size, 100% and 150% display scaling): all pages.
- A real download end to end through the window (Optimized and Advanced), with live telemetry.
- Stress: 300–400 songs at 8 at once; memory stays flat, threads are cleaned up, a second run skips everything,
  Stop is quick and leaves nothing behind. A flood of 6,000 progress events keeps the window responsive.
- Starting from `MusicDownloader.pyw` opens the window with no console and closes cleanly.

Not checked — please treat these as untested:

- **Linux** and **macOS**: no machine to run them on. The shared code has no Windows-only calls outside `platform_.py`;
  the macOS `platform_.py` passes the same 108 tests on Windows, and `run.sh` / `run.command` were syntax-checked but
  never executed. Fonts, HiDPI scaling, sound playback and the macOS `.app` builder are the likeliest rough edges.
- **Real AI providers**: the connectors are tested against a fake local server that speaks each protocol; no real
  Ollama / OpenAI / Anthropic / Gemini account was used.
- **Pandora**: US-only; the reader was written against its page format but couldn't be tried from here.
- **How the sounds sound**: the cues are synthesised and play without errors, but nobody has listened to them.

## Mac features brought to Windows (branch `claude/loving-ptolemy-1hu6b5`)

The Windows edition is being brought level with the macOS edition. Details and status per file:
[`windows_linux/PORT_NOTES.md`](windows_linux/PORT_NOTES.md).

| Group | What | State |
| --- | --- | --- |
| A | Networking and speed: shared connection pool, happy eyeballs, per-host timing, hedged lookups, answer cache; a governor that adapts songs-at-once to CPU, battery and pushback | done |
| B | Search bar suggestions, close matches (**Choose…**), "match the source" quality in every mode | done |
| C | Sound finishing (Advanced → Sound, Optimized → Polish), clean versions | done |
| E | Protection: start-up file check and restore, read-only lock, settings and library backups, recovery `.bat` | done |
| D | Interface: Live dashboard, artwork tilt, spectrum, Motion setting, first-run sheets, new icon and `.ico`, new launch animation and sound | done |

Checked: 559 offline tests pass on Linux (CPython 3.11, Tk 9.0); the new screens (launch, first-run sheets, Live
during a run, spectrum, tilt, Settings) were looked at headless (Xvfb), light and dark, at the smallest window size; a
200-song stress run stays flat. **Not checked on a real Windows PC**: the Windows API readings (CPU, power, Animation
effects), the read-only lock, the recovery `.bat`, the Windows Security link, cleaner detection, starting by
double-click, how the new interface looks and how smoothly it runs there (125–200 % scaling, slower computers), the
taskbar `.ico`, the Controlled-folder-access check, and the real services behind suggestions and close matches (all
faked in tests). Nobody has listened to the new start-up sound. See the "Limits, honestly" notes in
[`windows_linux/README.md`](windows_linux/README.md).
