# Music Downloader V3 — Windows & Linux

Paste a playlist, album or song link (or just type a name) and get tagged audio files with cover art in your music
folder. A calm, glassy window; no terminal.

- **Optimized mode** (default): pick *Good*, *Better* or *Best* and press Download.
- **Advanced mode**: MP3, AAC, FLAC, ALAC or WAV; bitrate, sample rate, bit depth, your own ffmpeg flags, a **Sound**
  tab (even volume, silence trim, fades, EQ), lyrics, naming templates, a spectrum of what the chosen format keeps, and a
  **Live** dashboard (speed ribbon, a pillar per processor core, songs in flight, how fast each server answers).
- **A search bar**: type a name and pick from suggestions; songs that can't be found exactly get **close matches** to
  choose from; **clean versions** on request; files never bigger than their source deserves.
- **Protection**: the app checks its own files at every start and puts back what a cleaner deleted.
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
   name such as `Abbey Road Beatles` — which also lists suggestions as you type (see *The search bar*).
2. Check the song list tile, pick a quality (**Optimized**) or open **Advanced**, press **Download**.
3. Watch it work: percentage, current song, time left. **Stop** is always there and winds down cleanly. Finished songs
   show up in **Activity**; anything that needs a look (not found, wrong length, low quality) is under **Attention**.
4. **Open folder** when it's done. Running the same list again skips what you already have.

The app remembers your settings, but not the last link or list: it always opens with an empty box.

Songs go straight into your music folder (default `Music\Music Downloader` in your home folder) as
`Title - Artist.mp3`. In Advanced → **Naming** use `/` for folders, e.g. `{artist}/{album}/{track} {title}`.
Change the folder under **Settings** (the gear).

**Settings** covers: *Automatic* performance (measures your CPU and connection and picks how many songs to fetch at
once), accuracy (length tolerance, minimum quality, replace low-quality files, YouTube fallback, better versions, close
matches), **Songs** (clean versions; in Optimized mode **Polish every song**, which evens out the volume and trims dead
silence at the ends — in Advanced the same controls are on the **Sound** tab), appearance (Auto / Light / Dark), sounds
and volume, the launch animation, **Motion** (*Full* / *Reduced*), the music folder, AI Mode, and **Protection** (see
*Keeping the app whole*).

### The search bar

The top box is also a search bar. Type a song, an artist, an album, a genre or a mood — anything that isn't a link —
and a moment later a short list drops down: songs, albums, artists, playlists and genres that match, the best guess
first, each saying who it is by. Use **↑ ↓** and **Enter** (or click) to pick one; **Esc** closes the list and leaves
what you typed alone. The rest of the window stays usable while the list is open. You still see the song list before
anything downloads.

- A **song** is that song, an **album** the whole album, an **artist** their top songs, a **playlist** its first 300
  songs and a **genre** its current chart.
- The suggestions come from Deezer's public search (no account or key; only what you type is sent). They are ranked by
  how much of what you typed each result explains, how well known it is and whether it is the real thing: a cover,
  karaoke or remix is pushed down unless you typed that word. What was seen before is kept on this computer
  (`suggest_index.json` in the settings folder, at most 1,500 entries), so a familiar name is suggested at once.
- Offline, or when that search can't be reached, no list appears and nothing else changes: links, lists, files and
  pressing Enter on a name work as before.

*Tested against a local fake of Deezer's search; the ranking was not tuned against the real service from Windows.*

### Quality and "match the source"

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

- a "hi-res" file (96 kHz or 24-bit) is checked too: one made from CD audio (empty low bits, nothing above the CD
  range) is not written bigger than CD audio.

The Activity list says "No lossless source · MP3 160 kbps" or "Matched to source · …" for such songs and the end of the
run says how many there were.

In **Advanced** mode this is the **Match the source** switch (Format section), on by default. Choose MP3 320 or FLAC and
the source decides how much of it is used; switch it off and the format is written exactly as chosen, bigger than the
source if need be — the old behaviour, for anyone who wants a fixed bitrate regardless.

*Limits, honestly:* the listening check recognises MP3/AAC-style origins from about 64 up to 192 kbps (the encoder's
cut-off is a clear cliff there). From about 224 kbps up the cut-off is too close to the top of the spectrum to tell
from a real recording, so a 256–320 kbps origin or a full-band Opus rip is believed — it errs on the side of trusting a
file, because turning a real lossless file into MP3 is the worse mistake. Music with a deliberate digital low-pass can
look lossy, though only a total silence above the cut-off counts. The sizes shown in the quality card are an upper limit
("up to about …").

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

### Songs that can't be found exactly

On a long playlist a few songs are always hard to place: spelled differently on YouTube, only a live take uploaded,
credited to another artist. Instead of just saying "not found", the app searches *around* the song — the title alone,
then the artist, the song's words and "lyrics" — and keeps what could plausibly be that music, each labelled in plain
words (*Live version*, *0:42 longer*, *Uploaded by …*). Karaoke, tutorials, reaction videos, nightcore / slowed / 8D
versions, mashups and hour-long loops are never offered.

**Settings → Close matches** decides what happens:

- **Ask** (default): the song is listed under Attention with a **Choose…** button that opens the options; pick one and
  that recording is downloaded. When a run ends with such songs you are asked once whether to review them.
- **Auto**: takes a close match by itself, but only a safe one — the same song by the same artist whose length differs
  by at most a quarter. Live takes, covers, remixes and uploads by someone else are always left for you to pick.
- **Off**: songs that can't be found exactly are just reported.

A recording you pick yourself is accepted even if its length differs, because you chose it. *Limits:* the wider search
looks on YouTube only, not the Internet Archive; it was tested against a faked YouTube search, not the real one.

### Finishing the sound (Sound tab, Polish)

In **Advanced → Sound**, each song can be finished in the same ffmpeg pass that writes it (no second encode, no extra
loss):

- **Even out the volume** to one loudness (Quiet −18, Balanced −14, Loud −11 LUFS — the scale streaming services use),
  with a limiter so a louder song can't clip. A boost is capped at 14 dB.
- **Trim silence** before the music starts and after it ends (Careful / Balanced / Tight), leaving a breath at the start
  and the last ring of the ending, with a soft fade at each cut. Never more than half a song is cut. The trimmed seconds
  still count when the song's length is checked, so a trimmed song is not taken for a wrong cut, now or on a later run.
- **Fades** (Off / Short / Long), **Enhance** (Clarity, Warmth, Bass boost, Vocal focus — a few dB each) and
  **Dynamics** (Gentle / Strong).
- **Quick setup** sets them all at once: *Playlist ready*, *Car & speakers*, *Late night*.

In Optimized mode the single switch **Settings → Songs → Polish every song** does *Playlist ready* (volume + trim). The
Activity list says what was done to each song ("Trimmed 2.1 s of silence · Volume +3.2 dB"). If a song can't be
analysed it is saved untouched and says "Sound options skipped" — a sound option never fails a song. Off by default:
with nothing switched on every song is saved exactly as downloaded. *Tested on synthetic tones and silence; nobody has
listened to the EQ presets on real music.*

### Explicit and clean versions

**Settings → Songs → Clean versions** (off by default, so you get the explicit versions, as released). With it on, the
search asks for the clean edit first ("… clean version") and prefers uploads that say *Clean*, *Radio Edit* or
*Censored*; a word that is part of the song's own name doesn't count (*Clean* by Taylor Swift is not a radio edit). A
song the catalogue says has no explicit words costs no extra search. The Explicit / Clean badge follows what the file
most likely *is* — the upload's own title first, then the catalogue — never the switch alone, so an explicit file is
never labelled clean; when no clean edit could be found the Activity list says "No clean version found". Existing files
are not re-tagged when you flip the switch. *Tested with faked searches; a real clean edit on YouTube is only as
findable as its title makes it.*

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

### The rest of the window

- **First run.** The first time it opens, a few short sheets: where songs go (keep the folder or **Choose…** another;
  the app writes a test file there, and if Windows Security's *Controlled folder access* blocks it, says so and how to
  allow the app), then **Protect the app** (locks its files read-only; names any cleaner or scanner installed). Every
  step has **Not now**, and all of it is in Settings later. If the start-up file check finds something while the
  sheets are up, its question waits until they are done. People updating from an earlier V3 also see the sheets once.
- **Live tab** (Advanced, during a download): time left with a likely range, songs done, speed (now / average / peak),
  searches per minute and API response time; a speed ribbon over the last minute; a glass pillar per processor core,
  filled to how busy it is; a capsule per song in flight, coloured by what it is doing (searching, downloading,
  encoding, tagging); and how fast each server answers (typical → slow end). On a small window some boxes are left
  out. The pictures are drawn on a background thread, so the window stays smooth.
- **Format tab spectrum** (Advanced, when the window is tall enough): bars from 20 Hz up, lit up to the highest
  frequency the chosen format and bitrate keep (*Up to about 16 kHz* for MP3 128, the full band for FLAC/WAV), with the
  dynamic range (96 dB, 144 dB for 24-bit). It glides when you change the format. It is an illustration of typical
  encoder cut-offs, not a measurement of your files.
- **Artwork tilt.** The cover on the list tile leans toward the pointer.
- **Motion.** *Reduced* (Settings → Experience) keeps the tile still, shortens fades and slides, and uses a simpler
  launch; it is also used when Windows' *Animation effects* is off (Settings → Accessibility → Visual effects).
- **Launch.** A new start-up animation (the icon forms, lights up and flies into the header) with a new start-up sound
  made to match. Press Esc, Space, Enter or click to skip it; switch it off under *Launch animation*.
- **Icon.** A new app icon; on Windows a multi-size `.ico` (16–256 px) is written to the cache folder so the taskbar and
  Alt-Tab get crisp small sizes.

*Tested on Linux (Tk 9, headless, light and dark, 1100×860), with offline tests of the layout and drawing code.
**Untested on a real Windows PC**: how these look and how smoothly they run there (DPI scaling 125–200 %, slower
computers), the taskbar/Alt-Tab icon, Windows' Animation-effects switch, and the Controlled-folder-access check. On a
computer whose processor kinds can't be read (all of them here) the cores show as one group.*

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

### Keeping the app whole (Protection)

Cleaners, virus scanners and half-finished updates sometimes delete or quarantine a few of a program's files, and the
program then fails to start with no hint why. **Settings → Protection** is the app's answer.

- **The check at each start.** The app keeps a list of its own files (size and SHA-256) and, outside the app folder, a
  copy of them (`%APPDATA%\MusicDownloader\shield\snapshot.zip`). A few seconds after the window opens it checks the
  files: a **deleted** file is put back from the copy at once; a **changed** file is only reported, and you decide
  (*Restore* or keep it), because it might be an edit you made; a real update ships its own list and is accepted.
  `MusicDownloader.pyw` does the "put back deleted files" part *before* the rest of the program is loaded, so a copy
  that lost files still starts — even if the checker itself was deleted, it is taken from the copy first.
- **Lock program files** sets Windows' read-only attribute on the app's files, so a program that deletes or rewrites
  files without asking is refused (Explorer can still delete them after a "This file is read-only" question). Off by
  default; switching it off unlocks everything.
- **Settings and your song folder's record are backed up** (the last three different copies, in the same `shield`
  folder). A damaged `settings.json` or `.musicdl.json` is set aside as `….damaged` and the newest good copy is put
  back; a record you deleted on purpose stays deleted (that is how a full re-check is asked for).
- **Cleaners and scanners** lists the ones installed (Microsoft Defender, CCleaner, Malwarebytes, Avast, AVG, Norton,
  Bitdefender, Kaspersky, McAfee, ESET, …). **Folders to exclude → Copy** puts the folders such a tool should leave alone
  on the clipboard; **Windows Security → Open** goes straight to Virus & threat protection settings, where Exclusions
  are.
- **Recovery script** opens the `shield` folder, which holds `Restore Music Downloader.bat`: if the whole app folder is
  deleted, double-click it to unpack the saved copy back where it was.

**What this cannot do:** an administrator (or a cleaner run as one) can still remove read-only files; the Python
packages and ffmpeg can't be rebuilt offline (the check names them; run `setup_windows.bat` again, which needs the
internet); adding exclusions in other programs is manual; the app is unsigned; there is no background service, so a
deletion is noticed at the next start, not instantly; a developer's git checkout is never sealed or locked. *Tested on
synthetic folders on Linux (the read-only attribute through Python's `os.chmod`, which is what sets it on Windows); the
lock, the `.bat` (cmd + PowerShell `Expand-Archive`), the Windows Security link and the cleaner detection have **not
been run on a real Windows PC**, nor against a real cleaner or scanner.*

### Where things are kept

| What | Windows | Linux |
| --- | --- | --- |
| Settings | `%APPDATA%\MusicDownloader` | `~/.config/musicdownloader` |
| Cache (sounds, lookups) | `%LOCALAPPDATA%\MusicDownloader\cache` | `~/.cache/musicdownloader` |
| Log (for troubleshooting) | `musicdl.log` in the settings folder (Settings → Log file) | same |
| Protection (copy of the app, backups, recovery script) | `%APPDATA%\MusicDownloader\shield` | `~/.config/musicdownloader/shield` |

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
  ui/         window: shell.py (canvas, hit regions, animation), glass.py (Pillow-drawn liquid glass), app.py + views_*.py,
              protect.py (Settings › Protection and the start-up check),
              welcome.py (first-run sheets), dashboard.py + viz.py + painter.py (Live tab), spectrum.py,
              card3d.py (artwork tilt), splash.py (launch), icon.py
  ingest/     links → song lists (Spotify, Apple, YouTube, Amazon, Pandora, web pages, spreadsheets, text search);
              suggest.py (the search bar's suggestions)
  core/       the downloader engine: sources, matching, parallel workers, retries, cleanup; closematch.py (songs that
              can't be found exactly); shield.py (keeping the app's own files whole, backups — stdlib only)
  meta/       tags, cover art, lyrics, file naming
  audio/      ffmpeg encoding, the source check (sampler.py), sound finishing (process.py), sound-cue synthesis
  telemetry/  speed, latency, searches per minute, ETA
  ai/         Ollama / OpenAI / Anthropic / Gemini connectors and the tasks built on them
  platform_.py   everything that differs per operating system
tests/        cd tests && py -3 -m unittest      (563 tests, no internet needed)
              py -3 stress.py 400 8              (400 songs, 8 at once: memory, threads, Stop)
              ui_shots.py                        (screenshots of the window for a look; needs a display)
```

The macOS edition shares this code; after changing anything here run `py -3 ../sync_macos.py` to carry it over.
