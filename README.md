# Chronicle Shorts for Chromebook

Turns a long video into finished vertical shorts for YouTube Shorts, TikTok and Instagram Reels. It has two modes:

- **Documentary** – your own narrated explainers: 3–5 complete stories of 60–90 seconds.
- **Stream / podcast** – streamer VODs, podcasts and influencer videos (for example Whop clipping campaigns): many 20–60 second moments, found from the speech *and* from loud reactions and laughter in the audio.

You upload the video, pick the mode and click **Generate Shorts**; the app does the rest.

```
UPLOAD MP4 → ANALYZE VIDEO → SELECT BEST MOMENTS → GENERATE CLIPS → DOWNLOAD SHORTS
```

Each short comes out as:

- a 1080×1920 (9:16) MP4 with H.264 video and AAC audio, loudness normalised for social platforms
- a suggested title, a caption/description and hashtags
- the original timestamps, plus a short explanation of why that moment was picked
- a `.txt` file with all of the above, ready to copy into the upload form

---

## Stream / podcast mode

Pick **Stream / podcast** above the Generate button. The options:

| Option | What it does |
|---|---|
| **Layout** | *Whole picture* (over a blurred copy), *Full screen* (crops to where the action is) or *Facecam on top, gameplay below*. For the split layout, choose the webcam's corner and size and line the gold box up with the webcam on the preview. |
| **Captions** | *Clean* (2–3 words, gold names) or *Pop* (1–2 big bold words, the spoken word in yellow). |
| **Clips** | Automatic (about one clip per 8 minutes of stream, up to 30) or a fixed number. |
| **AI model** | Opus 5.5, Sonnet 5.5 or Haiku 5.5. Needs an API key; without one the free offline scorer is used. |
| **Whop campaign brief** | Paste the campaign's rules. Clip lengths follow its limits, captions get the required hashtags, @mentions and phrases, and every finished clip gets a checklist (passes / needs a fix / check by eye). |

How stream clips are chosen:

- **Loudness spikes.** The audio is measured every half second and compared with the minutes around it. Big jumps (shouting, laughing, hype) mark likely highlights.
- **Speech.** Claude reads the whole transcript with those spikes marked. Multi-hour VODs are read in one-hour parts, so nothing is too long. Without a key, the offline scorer looks for hype language, laughter, questions, a clean start and a payoff near the end.
- Stream clips keep natural speed (change `CH_STREAM_SPEED` if you like them faster) and skip scene detection, which would take very long on a multi-hour VOD.

**Results log.** Under each finished clip, *Posted it? Log the results* lets you type in its views, earnings and link. Totals appear in the **Results log** panel. Once 3 or more clips of a kind are logged, Claude is shown your best and worst performers as examples the next time it picks moments, so its picks learn what works for your accounts.

**Long VODs take time on a Chromebook.** Transcribing is the slow part: expect roughly the video's own length or more on a mid-range Chromebook with the `base` model (`tiny` is faster). Progress is saved, so a stopped job resumes.

---

## How it works on a Chromebook

ChromeOS can run Linux apps in a built-in, sealed-off Linux environment. Chronicle Shorts runs its editing engine there (Python, FFmpeg and the Whisper speech model). You use it from Chrome like any website.

| Part | What it is | Where it runs |
|---|---|---|
| **Studio page** | The dark editing-suite page you work in | Chrome, at `http://penguin.linux.test:8765` |
| **Editing engine** | Cuts, captions, reframes and exports the shorts | The Chromebook's Linux environment |
| **Claude API** *(optional)* | Reads the transcript and makes the editorial choices | Anthropic's servers |

Your video never leaves your Chromebook. If you add a Claude key, only the **text transcript** is sent to Claude.

The Linux environment is only reachable from the Chromebook itself, not from other devices on your network.

---

## Install (once, about 15 minutes)

### 1. Turn on Linux

1. Open **Settings** → **About ChromeOS** → **Developers** → **Linux development environment** → **Turn on**. On older ChromeOS versions it's under **Settings → Advanced → Developers**.
2. When it asks for **disk size**, choose **20 GB or more**. Uploads, the speech model and finished shorts are all stored there.
3. Wait for it to finish. A **Terminal** window opens.

If the option is missing or greyed out, the Chromebook is managed by a school or workplace that has blocked Linux. Chronicle Shorts can't run on it.

### 2. Put the project in Linux

1. Open the **Files** app and find `chronicle-shorts-chromebook.zip` in **Downloads**.
2. Drag it onto **Linux files** in the left sidebar.

### 3. Run the installer

In the **Terminal** (open it from the launcher; choose **penguin** if asked), type each line and press Enter. To paste in the Terminal, use **Ctrl+Shift+V**.

```bash
unzip chronicle-shorts-chromebook.zip
```

```bash
cd chronicle-shorts-chromebook
```

```bash
bash install.sh
```

The installer:

- checks there's enough space in Linux,
- installs Python and FFmpeg,
- installs the engine,
- downloads the speech model (about 150 MB),
- adds a **Chronicle Shorts** app to your launcher,
- opens the studio in Chrome.

### 4. Optional: turn on Claude analysis (recommended)

1. Create an API key at <https://console.anthropic.com> (pay-as-you-go, no subscription).
2. In the Terminal, inside the `chronicle-shorts-chromebook` folder, run:

```bash
nano .env
```

3. Paste the key after `ANTHROPIC_API_KEY=` using **Ctrl+Shift+V**.
4. Save with **Ctrl+O**, then **Enter**. Quit with **Ctrl+X**.
5. Click **Stop Chronicle Shorts** in the launcher, then **Chronicle Shorts** again.

Without a key the app still works, using the free offline story scorer.

---

## Use it (every documentary)

1. Click **Chronicle Shorts** in the launcher (the circle button, bottom-left). It may be inside the **Linux apps** folder. Chrome opens the studio.
2. Drop your finished MP4 on the page, or click **Choose MP4**. Files from Downloads and Google Drive both work.
3. Click **Generate Shorts**.
4. When it finishes, preview each short, copy the title, description and hashtags, and click **Download MP4** or **Download all (ZIP)**. Downloads land in your Chromebook's **Downloads** folder.

Keep the Chromebook plugged in with the lid open while it works. If it goes to sleep, processing pauses and carries on when it wakes.

When you're done for the day, click **Stop Chronicle Shorts** in the launcher. That frees up memory. If a job was mid-way, the next time you open the studio it shows **Resume** and carries on.

**If the studio page doesn't load**, try <http://localhost:8765> instead. If neither works, start it from the Terminal to see what's wrong:

```bash
cd ~/chronicle-shorts-chromebook && ./start.sh
```

### How long it takes

Times vary a lot between Chromebooks. A rough guide for a 20-minute documentary:

| Chromebook | Total time |
|---|---|
| Intel Core i3/i5/i7 or Ryzen, 8 GB RAM | about 15–30 min |
| Celeron, Pentium or MediaTek, 4 GB RAM | about 45–90 min |

Transcription is the slowest step and only happens once per video; a resumed or retried job reuses it.

- **Fast Chromebook (i5/i7, 8 GB+):** set `CH_WHISPER_MODEL=small` in `.env` for more accurate captions, especially names.
- **Very slow Chromebook:** set `CH_WHISPER_MODEL=tiny`.

---

## What the app decides for you

**Transcription.** Whisper transcribes the narration with a timestamp on every word.

**Visual analysis.** FFmpeg detects scene changes (a pacing signal) and black letterbox bars (removed before reframing).

**Choosing the moments.** The whole documentary is analysed before anything is picked.

- **Claude engine** (with a key): Claude reads the complete transcript plus pacing data. It ranks 6–10 candidate stories on storytelling, historical significance, curiosity, pacing and standalone value, then selects the best 3–5. It writes the titles, descriptions, hashtags and explanations using only facts the narration contains.
- **Offline engine** (no key): every window of whole sentences between 28 and 104 seconds is scored on its opening hook, ending, dramatic events, dates and figures, names and pacing. Story boundaries are detected from pauses, scene cuts and shifts in vocabulary, so a short never glues two stories together or stops before the payoff. Intros, outros, sponsor reads and "subscribe" sections are avoided.

Every pick is checked against the real transcript: clips start and end on sentence boundaries and never overlap.

**Cutting.** Silences over 0.7 s are shortened. Every word of narration is kept.

**Speed.** Speed comes from the speaking rate: 1.15× for slow narration, down to 1.05× for fast narration. Pitch is preserved.

**Vertical reframing.** The footage is never stretched. The animation sits over a blurred copy of itself. It's shown larger (cropped to 4:3 or 3:2) when the important detail is in the centre, and as the full frame otherwise.

**Captions.** Captions are word-synchronised, 2–3 words at a time. The spoken word pops slightly larger, and names, dates and numbers are gold. They sit below the picture, inside the area platform buttons don't cover.

**Checks.** Every output is verified as 1080×1920, H.264 and AAC before it's offered for download.

---

## Reliability

- **Bad files are rejected with a clear reason:** files that aren't video, corrupted or half-exported files, videos without audio, files that are too short or too long, and files too big for the free Linux space.
- **One failed short never loses the others.** Each failed short has its own **Retry** button.
- **Stopped jobs resume.** If you stop the app or the Chromebook restarts, the job shows **Resume** and continues where it left off.

---

## Costs

| Item | Cost |
|---|---|
| ChromeOS Linux, FFmpeg, Whisper, Python, the app | Free |
| Offline story analysis | Free |
| Claude analysis with `claude-opus-5-5` (default) | about $0.10–0.30 per documentary; more for a multi-hour stream |
| Claude analysis with `claude-sonnet-5-5` | about half of Opus |
| Claude analysis with `claude-haiku-5-5` | about $0.01 per documentary |

The studio shows the actual cost of each run (from the token counts Anthropic reports) under **Best moments**.

---

## Settings (`.env`)

Edit with `nano .env` in the project folder, then restart the app.

| Setting | Default | What it does |
|---|---|---|
| `ANTHROPIC_API_KEY` | empty | Turns on Claude analysis |
| `CH_CLAUDE_MODEL` | `claude-opus-5-5` | Which Claude model analyses the story |
| `CH_WHISPER_MODEL` | `base` | `tiny` is faster, `small` is more accurate |
| `CH_X264_PRESET` | `veryfast` | `medium` gives slightly smaller files but is ~3× slower |
| `CH_WATERMARK` | empty | Small text at the top of documentary clips (empty = none) |
| `CH_EXTRA_HASHTAGS` | empty | Hashtags added to every clip, e.g. `#mychannel #facts` |
| `CH_CLAUDE_CHUNK_MINUTES` | `60` | Long videos are read by Claude in parts of this length |
| `CH_STREAM_SPEED` | `1.0` | Playback speed of stream clips |
| `CH_MAX_SOURCE_MINUTES` | `480` | Longest video accepted |
| `CH_MAX_UPLOAD_GB` | `20` | Largest file accepted |

---

## Housekeeping

- **Free up space.** Every processed documentary is kept in the studio's **Library** until you click **Delete**. Uploads are full-size copies, so delete old ones once you've downloaded the shorts.
- **Update to a new version.** Put the new zip in **Linux files** and run `unzip -o chronicle-shorts-chromebook.zip` in your home folder, then `cd chronicle-shorts-chromebook` and `bash install.sh`. Your settings (`.env`) and library are kept.
- **Uninstall.** Delete the `chronicle-shorts-chromebook` folder from Linux files. To also remove the launcher apps, run:

```bash
rm ~/.local/share/applications/chronicle-shorts*.desktop
```

- **Batch mode.** To process a file without the browser:

```bash
.venv/bin/python -m app.cli "/mnt/chromeos/MyFiles/Downloads/Episode 12.mp4" --out ~/shorts-episode-12
```

The `/mnt/chromeos/...` path only works after you right-click **Downloads** in the Files app and choose **Share with Linux**.

---

## Limitations

- **Visual understanding is measured, not interpreted.** The app measures scene changes, motion and where detail sits, but story choices come from the narration.
- **One static framing per short.** It doesn't follow moving subjects.
- **Caption accuracy follows Whisper.** Unusual names can be misspelled. The `small` model helps if your Chromebook can handle it.
- **ARM Chromebooks** (MediaTek, Snapdragon) are supported, but they're slower at transcription than Intel or AMD models.
- **Managed Chromebooks** (school or work) usually block Linux, and the app can't run without it.

## Project layout

```
install.sh     one-step installer (system packages, engine, model, launcher icons)
start.sh       starts the engine in the background and opens Chrome
stop.sh        stops the engine
app/
  server.py      web server + API          pipeline.py   job queue, resume/retry, ZIP
  media.py       FFmpeg analysis/framing   transcribe.py Whisper speech-to-text
  analyze.py     Claude + offline moment selection (documentary and stream modes)
  brief.py       Whop campaign brief rules and per-clip checks
  results.py     results log that feeds back into Claude's picks
  render.py      cutting, speed, vertical layout, encoding, output checks
  captions.py    word-synchronised captions
  cli.py         batch mode
  static/index.html  the studio page
  assets/        caption font (SIL OFL) and app icon
tests/           unit tests, test-video generator, output verifier
```
