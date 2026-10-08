# Music Downloader V3 — Windows & Linux

Paste a playlist, album or song link (or just type a name) and get tagged audio files with cover art in your music
folder. A calm, glassy window; no terminal.

- **Optimized mode** (default): pick *Good*, *Better* or *Best* and press Download.
- **Advanced mode**: MP3, AAC, FLAC, ALAC or WAV; bitrate, sample rate, bit depth, your own ffmpeg flags, lyrics,
  naming templates, and a live dashboard (speed graph, searches per minute, service latency, time left).
- **AI Mode** (optional): fixes messy song details, builds a playlist from a description, adds genres. Works with a
  local Ollama server or your own OpenAI / Anthropic / Gemini key.

---

## Start it

### Windows 10 / 11

1. Install Python 3.9 or newer from <https://www.python.org/downloads/> (keep the **py launcher** option ticked).
2. Install ffmpeg (it converts the songs): open PowerShell and run

   ```
   winget install Gyan.FFmpeg
   ```

   then sign out of Windows and back in once.
3. Double-click **`setup_windows.bat`** (installs the Python packages; first time only).
4. Double-click **`MusicDownloader.pyw`**. That is how you start it from now on. No console window appears.

Prefer a command line? `py -3 -m pip install -r requirements.txt`, then `py -3 -m musicdl`.

### Linux (X11 or Wayland desktop)

1. Install Python with Tk, and ffmpeg:

   ```bash
   sudo apt install python3 python3-tk python3-venv ffmpeg      # Debian, Ubuntu, Mint
   sudo dnf install python3 python3-tkinter ffmpeg              # Fedora (ffmpeg needs RPM Fusion)
   sudo pacman -S python tk ffmpeg                              # Arch
   ```
2. In this folder, run:

   ```bash
   bash run.sh
   ```

   The first run builds a private Python environment in `.venv` (about a minute, needs internet). After that it opens
   straight away. `run.sh` hands the terminal back and the window stays open on its own.
3. Optional: `bash run.sh --shortcut` adds **Music Downloader** to your applications menu, so you never need the
   terminal again (`--no-shortcut` removes it). `bash run.sh --foreground` keeps it attached to the terminal, which
   shows errors if something won't start. `bash run.sh --update` refreshes yt-dlp (do this if downloads from YouTube
   stop working; the sites change often).

---

## Using it

1. **Paste or type** in the top box, then press **Find songs** (or Enter). It understands:
   Spotify, Apple Music, YouTube Music, YouTube, Amazon Music and Pandora links; other sites yt-dlp knows
   (SoundCloud, Bandcamp …); a CSV/XLSX list (the **Spreadsheet** chip); several lines of `Artist - Title`; or a plain
   name such as `Abbey Road Beatles`.
2. Check the song list tile, pick a quality (**Optimized**) or open **Advanced**, press **Download**.
3. Watch it work: percentage, current song, time left. **Stop** is always there and winds down cleanly. Finished songs
   show up in **Activity**; anything that needs a look (not found, wrong length, low quality) is under **Attention**.
4. **Open folder** when it's done. Running the same list again skips what you already have.

The app remembers your settings, but not the last link or list: it always opens with an empty box.

Songs go straight into your music folder (default `Music\Music Downloader` in your home folder) as
`Title - Artist.mp3`. In Advanced → **Naming** use `/` for folders, e.g. `{artist}/{album}/{track} {title}`.
Change the folder under **Settings** (the gear).

**Settings** covers: *Automatic* performance (measures your CPU and connection and picks how many songs to fetch at
once), accuracy (length tolerance, minimum quality, replace low-quality files, YouTube fallback), appearance
(Auto / Light / Dark), sounds and volume, the launch animation, the music folder, and AI Mode.

### Quality and the source cap (Optimized mode)

Pick **Good**, **Better** or **Best**, and — top right of the quality card — what you play your music on:

| | Apple | Windows · Android |
| --- | --- | --- |
| Good | AAC 160 kbps | MP3 192 kbps |
| Better | AAC 256 kbps | MP3 320 kbps |
| Best | ALAC (lossless) | FLAC (lossless) |

(Windows · Android is the default here; the Mac edition starts on Apple.) The choice applies to songs you download from
now on. Songs already in your folder are not fetched again just because you switched.

**A file is never bigger than its source deserves.** Most sources are not lossless (a YouTube stream is roughly
130–160 kbps), and a lossless file made from one is only a much larger copy of the same sound. So after downloading a
song the app looks at what it really got:

- a lossless source (FLAC/WAV from the Internet Archive) is kept lossless — FLAC, or ALAC for Apple;
- a lossy source is saved as MP3 (or AAC for Apple) at the nearest bitrate that holds everything it has — Best on a
  130 kbps source gives a ~160 kbps file, not a FLAC; Better is lowered the same way if the source can't fill 320;
  Good (192) is never raised;
- a file that *claims* to be lossless is listened to for a moment: lossy encoders leave a hard cut-off in the spectrum
  (about 16 kHz at 128 kbps), so a "FLAC" that is really a 128 kbps MP3 is caught and saved as such.

The Activity list says "No lossless source · MP3 160 kbps" for such songs and the end of the run says how many there
were. Limits, honestly: the spectrum check recognises 128–224 kbps MP3/AAC-style origins; it cannot tell a 256–320 kbps
origin or a full-band Opus rip from lossless, and it errs on the side of believing a file. The sizes shown in the
quality card are an upper limit ("up to about …"). **Advanced mode is not capped**: it writes exactly the format you
chose.

### Songs you already have

Before downloading, the app looks through your music folder (sub-folders too; MP3, FLAC, WAV and M4A) and recognises
songs by artist and title, not by file name. A song you already have is **never downloaded twice**, even if it is in
another format, in another folder, or was named differently.

The one exception is when you now ask for clearly better quality than what you have. Say you downloaded 100 songs at
*Good* and later add 100 more at *Best*: the new 100 simply download. For the old 100 a question appears before
anything is fetched: **Replace with higher quality?**, with *Keep Existing* or *Replace*.

- An old file is removed only **after** its better version has been downloaded, saved and checked. If the download
  fails, or nothing better exists, the old file stays.
- It only asks when the gain is real (roughly a third better or more) and a better source can exist. A 192 kbps file
  that was made from a 128 kbps source is not offered an upgrade to 320.
- **Next best:** when the quality you chose can't be found for a song (say no lossless copy exists), the best available
  is used instead, as an MP3 at the nearest bitrate rather than a larger FLAC copy of the same lossy sound. The Activity
  list says so ("no lossless version found").
- A song where nothing better was found is remembered for a month, so it isn't searched again on every run.
- **Settings → Better versions**: *Ask* (default), *Replace* (always, no question) or *Keep* (never replace).

### Artwork and song details

Every song gets a cover and the details players sort by, written the way each kind of player reads them.

**Cover art** is a square sRGB JPEG (baseline, so every player draws it), never stretched beyond its source:

| | Apple (`.m4a`, AAC / ALAC) | Windows · Android (MP3 / FLAC) |
| --- | --- | --- |
| Size | **1400 × 1400** (Apple's minimum for Apple Music artwork) | **1000 × 1000** |
| Typical weight | 190–590 KB | 120–260 KB |

Where it looks, in order, stopping at the first picture that is at least 600 px: the music service's own art → the
song's release in Apple's catalogue (asked for at the right size) → the Cover Art Archive → a picture inside the
downloaded file → the video's thumbnail (black bars and the "Topic" side bars cropped off). A picture that is a hair
under the size (1398 px) is brought up to it; smaller ones are kept as they are rather than blurred up. A network
hiccup is never remembered as "no artwork" — it is tried again — and when nothing is found the Activity list says so
and the song is tried again in a week.

**Details written** — Apple files: title, artist, **album artist**, album, release date, genre, **track n of m**,
**disc n of m**, compilation flag, the Explicit / Clean badge, media kind *Music*, ISRC and label. MP3 (ID3v2.3 with
UTF‑16 text, which Windows Explorer, Android and car stereos all read): the same, including `TYER`/`TDAT` for the date.
FLAC: `ALBUMARTIST`, `TRACKTOTAL`, `DISCTOTAL`, `DATE` and the rest, with the cover as a front-cover picture block.

**Which release.** A song is on many releases: the studio album, a live album, "Greatest Hits", a 2026 re-release. The
details come from Apple's catalogue (the same data Apple Music shows), with Deezer as the fallback, and the app picks
the original studio album: live, remix, karaoke, sped-up and re-recorded versions, compilations, soundtracks and box sets
are ranked below it, a deluxe edition stands in for its standard edition (track 1 of 10, not of 28), and the date is
the original release date. If the music service already named the album, that album is used and its numbers are never
taken from another one. Nothing the service or the file already says is overwritten.

**Songs you already have** that lack artwork or details (album artist, track n of m, date, genre) are completed the next
time you run the list — the Activity list says "Details + Artwork added" — without being downloaded again. Existing
tags are never changed, only blanks are filled (a year alone becomes the full date when the catalogue agrees).

Limits, honestly: which release is "the original" is a judgement, so for a song that mainly lives on compilations the
app can still pick one; the catalogue is the US storefront; Deezer-sourced details lack some fields; and this has been
verified by reading the files back, not by playing them in Apple Music, an iPhone or an Android phone.

### When the disk fills up

If your drive runs out of space the app does not mark songs as failed. It **pauses** and says **Disk full — paused**
(in amber), and the taskbar button flashes (on Linux a desktop notification appears) until you look at it. Free some
space, or change the folder, and downloading carries on by itself once there is room for the song plus a little spare.
Nothing is lost: the song that was in progress is retried, not skipped.

### Speed on long playlists

When a run starts the app looks at the computer: how many CPU cores, how much memory, and how fast the connection is
(the weekly *Automatic* measurement). From that it plans how many songs to work on at once and how many may be encoding
at once — about one per core, and about 10 Mbps of connection per song, never more than 12 — and starts at roughly 60%
of it. The first few encodes run at normal priority, the rest at below-normal priority so the window and your other
programs stay responsive.

For **10 songs or more** a governor then looks at the computer every couple of seconds and adjusts:

- **Faster while it pays.** If every place is busy, the CPUs have room, the connection isn't full and nothing is
  failing, it tries one more song at once and keeps it only if the throughput really rose; otherwise it steps back and
  rests four minutes before trying again.
- **Easier when something pushes back.** Errors, a site asking for fewer requests, or CPUs that stay fully busy take
  one away.
- **Gentler on a battery.** Unplugged it uses fewer songs at once and encodes at lower priority; with **battery saver**
  on it uses two at most. It speeds up again by itself when the charger is back.

The progress card says when it is holding back ("Easing off while on battery — 2 at once"). Shorter runs finish
before there is anything to learn, so they simply use the plan. With **Automatic** off, the number you set under
Settings is a ceiling that is never exceeded — it is only lowered for battery or pushback.

*Limits, honestly:* Windows gives an ordinary program no temperature reading, so the app cannot tell when your laptop
is hot (the heat rules of the Mac edition are in the code but always read "nominal" here); it relies on the CPU load and
the power state instead. Hybrid processors (performance + efficiency cores) are counted as one kind of core. The
readings (CPU load per core from the same counters Task Manager uses, memory, battery and battery saver) were written
against the Windows API documentation and are exercised by tests with simulated readings; **on a real Windows PC they
are untested**, as is how the governor behaves on a real long run.

### The progress picture

While it works, the progress card shows a live waveform that moves with the real download speed and flares when a song
finishes. It turns amber when paused for a full disk, and settles when everything is done. It is purely cosmetic.

### How it searches and downloads

- **Matching:** artist, title and length are compared with accents, apostrophes, `&`/`and`, number words ("4" / "four")
  and non-Latin titles taken into account. Karaoke, covers, instrumentals and the like are skipped; live, acoustic,
  demo, remix and similar versions are only used when nothing better exists (unless you asked for them). The length
  must fit.
- **Fewer, smarter searches:** YouTube is searched with up to three queries but stops as soon as one result clearly
  matches. The Internet Archive is only searched when you asked for more than YouTube can give (Best / lossless), or
  when YouTube found nothing convincing. Songs coming up next are searched while the current ones download.
- **Quality:** among results that match equally well, the better sound wins, but never more than the quality you chose.
  A "lossless" file that was clearly ripped from a video is not trusted as lossless.
- **Networking:** every thread shares one connection pool, so connections are reused instead of opened again for each
  song, and a connection the server closed while it sat idle is replaced at once and the call repeated for free. New
  connections try IPv6 and IPv4 side by side ("happy eyeballs", the family that worked is tried first next time) and
  names are remembered for a minute; the lookup services a run will use are connected to while the plan is made. Each
  site is paced to what it will accept and slows down automatically if it complains (and honours Retry-After; iTunes'
  "403" counts as "slow down"); a site that is down is skipped briefly instead of being retried over and over. Each
  host's round-trip time is measured, so timeouts fit the host rather than one number for all, and a lookup that runs
  slower than the host's usual tail sends a second copy and keeps whichever answers first (never to iTunes or
  MusicBrainz, which are strict about how often they are asked). Identical lookups made at the same moment are merged
  and recent answers remembered for a few minutes (a failed lookup never is). Interrupted downloads resume where they
  stopped, a connection whose speed collapses is dropped and picked up again, and a download is checked against the
  size the server announced. If a single connection is slow (under about 2 MB/s) a large file is fetched over several
  connections at once — only as many as actually make it faster. *Tested against local servers that misbehave on
  purpose (drops, throttling, slow answers); not measured on real downloads, so no real-world speed-up is claimed.*

### AI Mode

Settings → **AI Mode** → switch on → choose a provider.

- **Ollama** (local, free, private): install it from <https://ollama.com>, run `ollama pull llama3.2`, leave the
  server address as it is.
- **OpenAI / Anthropic / Gemini**: paste your API key. It is stored in your system keychain when the optional
  `keyring` package is installed (`pip install keyring`), otherwise in a file only your user can read. The
  environment variables `OPENAI_API_KEY`, `ANTHROPIC_API_KEY`, `GEMINI_API_KEY` also work.

Press **Connect**, choose a model. Then: an **AI playlist** chip appears next to the others (describe what you want);
**Repair song details** cleans titles/artists/albums before download; **Add genres** tags new songs;
**Tidy my music folder** adds genres to songs you already have. Only titles, artists and albums are sent to a model —
never audio files or file paths.

### Where things are kept

| What | Windows | Linux |
| --- | --- | --- |
| Settings | `%APPDATA%\MusicDownloader` | `~/.config/musicdownloader` |
| Cache (sounds, lookups) | `%LOCALAPPDATA%\MusicDownloader\cache` | `~/.cache/musicdownloader` |
| Log (for troubleshooting) | `musicdl.log` in the settings folder (Settings → Log file) | same |

Delete those folders to reset the app completely. Your songs are never touched.

---

## Good to know

- **Where the audio comes from:** it searches the Internet Archive and YouTube for a recording whose artist, title and
  length match the song, and checks the length of what it downloaded. It will not always find a song; those are listed
  under Attention. Use it for music you have the right to download, and follow each service's terms.
- **Spotify:** only the first 100 songs of a playlist can be read without an account; you are told when a list is trimmed.
- **Amazon Music:** its pages need a browser to run their scripts, so Edge, Chrome, Chromium or Brave must be installed.
  Song lengths aren't on the page and are filled in from public catalogues.
- **Pandora:** United States only (other countries get an "unavailable" page, which is reported). Personal stations need
  a login and can't be read; Apple Music stations aren't supported either.
- **Lossless (Best):** the result is only as good as the source found; a lossy source can't become lossless, so in
  Optimized mode it is saved as MP3/AAC instead (see above) and the Activity list shows what each song actually has.
- **Memory:** long runs are designed to stay flat (the activity list keeps the newest 1,500 songs).

## Troubleshooting

| Symptom | Try |
| --- | --- |
| Nothing opens (Windows) | Run `py -3 -m musicdl --no-relaunch` in a terminal to see the error, or open the log file. |
| "ffmpeg not found" in Settings → About | Install it (see above); on Windows sign out and in afterwards. |
| Downloads from YouTube suddenly fail | `py -3 -m pip install -U yt-dlp` (Linux: `bash run.sh --update`). |
| No sound on Linux | Install one of `pulseaudio-utils` (paplay), `pipewire`, `alsa-utils` (aplay) or `sox`. Or switch sounds off in Settings. |

## For developers

```
musicdl/
  ui/         window: shell.py (canvas, hit regions, animation), glass.py (Pillow-drawn liquid glass), app.py + views_*.py
  ingest/     links → song lists (Spotify, Apple, YouTube, Amazon, Pandora, web pages, spreadsheets, text search)
  core/       the downloader engine: sources, matching, parallel workers, retries, cleanup
  meta/       tags, cover art, lyrics, file naming
  audio/      ffmpeg encoding, sound-cue synthesis and playback
  telemetry/  speed, latency, searches per minute, ETA
  ai/         Ollama / OpenAI / Anthropic / Gemini connectors and the tasks built on them
  platform_.py   everything that differs per operating system
tests/        cd tests && py -3 -m unittest      (336 tests, no internet needed)
              py -3 stress.py 400 8              (400 songs, 8 at once: memory, threads, Stop)
```

The macOS edition shares this code; after changing anything here run `py -3 ../sync_macos.py` to carry it over.
