# Rangya Studio

A local dashboard that edits talking-head videos automatically — the workflow from
"How I Edit My Videos Using Claude Code", without needing Claude or any AI subscription.
The only paid service is ElevenLabs speech-to-text (≈ $0.22 per hour of audio).

Upload a raw recording → it transcribes it, cuts dead air, "um/uh", coughs, retakes, false
starts and "let me start again" moments, adds word-by-word captions and zooms, reframes to
9:16 or 16:9, levels the audio for YouTube, and shows you a preview. Fix anything by clicking
words in the transcript, tune the style, then render the 1080p final.

## Install (once)

1. **Python 3.12** (3.10–3.12 work; 3.13 works but effects can't go behind your head) —
   https://www.python.org/downloads/ (Windows: tick "Add python.exe to PATH").
2. **ffmpeg**
   - Windows: `winget install Gyan.FFmpeg` (then reopen the terminal)
   - macOS: `brew install ffmpeg`
3. Copy this folder to the PC.

## Run

- **Windows:** double-click `start.bat`
- **macOS / Linux:** `./start.sh`

The first run installs its own Python packages into `.venv/` (needs internet once), then opens
**http://127.0.0.1:8000**. Keep the window open while you use the dashboard.

Open **Settings** (top right) and paste your **ElevenLabs API key** (elevenlabs.io → Developers →
API keys; Speech-to-Text permission is enough). It is saved only on this PC, in `.env`.

Options: set `PORT=9000` to change the port; `HOST=0.0.0.0` to open it to other devices on your
Wi-Fi (anyone on the network can then use it).

## Using it

1. **+ New video** → drop the file, choose format (same / 9:16 / 16:9), captions and language
   (choosing the language is more accurate than auto-detect for Hindi, Kannada, etc.).
   **Your selection:** every style and effect choice you make (Style tab, Effects ticks, copilot style
   requests) is saved automatically and every new upload uses exactly that — never another video's
   cuts or placed effects. Tick **Start from my taste** on the upload screen to also apply the
   learned taste.
2. Wait for **Preview ready** (progress shows in the sidebar). Multiple uploads queue up.
   - The player's **Original** tab plays the untouched video you uploaded.
   - **↺ Start over** goes back to a clean first edit of the original (all edits, effects and style
     changes on that video are cleared). "undo" in the copilot brings your edit back; the copilot
     also understands "start over" / "go back to the original".
3. **✦ Copilot tab** — type what you want in plain words; it applies the changes, tells you
   exactly what it changed, and re-renders the preview. Combine requests with commas/"and":
   - "tighter pauses, slow zoom and make it vertical"
   - "cut the part where I say January" · "remove 0:12 to 0:20" · "cut the first 3 seconds"
   - "bigger captions at the top, one word at a time, highlight green"
   - "make it more energetic" / "calm" / "clean professional look"
   - "make every caption in English and use Helvetica font" · "captions in Hindi script"
   - "highlight the word Fundnext" · "remove background noise" · "render final" · "undo"
   - add "for all my videos" / "from now on" to make it your default style
   - Hinglish basics work: "captions bade karo", "January wala part hatao", "pauses kam karo"
   - **"make it like the reel"** — the cinematic Instagram-reel look in one go: kinetic captions
     (words stack beside your face, one big italic-serif or CAPS key word), very tight pacing,
     1.08× speed, cinematic grade, B-roll every few seconds and a music bed under your voice
   - "kinetic captions" / "classic captions" · "speed it up" / "1.1x" · "cinematic / warm / moody look"
   - "add music", "music quieter", "no music" · "add b-roll", "b-roll every 3 seconds",
     "b-roll when I talk about trading", "no b-roll"
   Typos in command words are auto-corrected ("helavtica" → Helvetica).
   Type **help** for the full list. It's rule-based (no AI, works offline), so it says
   "I didn't understand" for open-ended requests instead of guessing.
4. **✨ Effects tab** — all 42 face-tracking effects (orbit, spotlight, giant word behind you, glass
   graph, checklist, LED sign, comment box, magazine cover…), grouped by type with thumbnails.
   - Tick **Auto** on the ones the editor may use by itself — every new video then gets them
     automatically where they fit (a graph when you say a number, a checklist when you list steps,
     a stamp on "never", a Follow button on your call-to-action, ticked extras on key moments).
   - **No made-up words:** every word an effect shows is either something you said or something you
     typed. Template effects (magazine cover, profile card, receipt, tier list…) are marked
     "needs your text" and are never picked automatically.
   - Press **Add** to place one exactly: at the playhead, when you say a word, or from–to seconds,
     with your own text. **Preview on my video** shows a still of it on your clip first.
   - **Effects in this video** lists everything in the current preview (yours and auto picks) with
     ✕ to remove any one, **Remove all effects** (this video only), **Plain video** (this video only:
     no effects *and* no colour filter, zoom, speed-up or kinetic captions) and **Turn effects &
     filters back on** (back to your selection). The chips under the player have ✕ too, and
     "undo" in the copilot reverses a removal.
   - Or just ask the copilot: "spotlight when I say honestly", "orbit from 3 to 6 seconds",
     "remove old effects".
4a. **My taste + ✦ Auto taste** — the dashboard learns how *you* edit, locally (nothing is sent
   anywhere): effects you add and on what kind of moment (your hook, numbers, lists, "never",
   "honestly", your call-to-action…), auto effects you remove, Auto ticks, style changes, and —
   most strongly — every video you render as final. Recent choices count most.
   - **✦ Auto taste** (next to Update preview) rebuilds the video your way: your learned style,
     your favourite effect for each kind of moment, your usual amount of effects. Tweak anything
     afterwards and the model updates, so the next Auto taste is closer.
   - **My taste** tab shows what it learned and a confidence meter. **Use my taste on new uploads**
     (off by default) pre-ticks "Start from my taste" on the upload screen. **Reset my taste** forgets it.
4b. **Media tab** — upload **B-roll clips** (short shots of you working, walking, your setup…) and
   one **music track**. B-roll is cut in automatically (never over your opening hook or the
   ending, starting on cuts to hide jumps); music loops, fades and dips under your voice.
5. **Transcript tab** — removed words are struck through and colour-coded by reason.
   - Click a word: cut it / bring it back · Shift+click: apply to a range
   - Alt+click: highlight the word in captions · Ctrl/⌘+click: jump the preview there
6. **Style tab** — pause length, breath after sentences, filler/retake removal, zoom style,
   caption words/position/size/colours, colour boost, noise reduction.
   **Save as my default style** makes every future video use it.
7. **Update preview** after changes (only changed parts re-render), then **Render final** and
   download the video + `.srt` captions.

## Good to know

- Retake detection is rule-based: when you restart a sentence, the earlier attempt is cut and
  the last one kept. Check the yellow "Retake" words in the transcript — one click restores any.
- Captions default to **Helvetica Bold in English letters**: Hindi/Kannada/Tamil… speech is written
  the Hinglish way ("dekho", "kya", "hain"). Switch to "As spoken" in Style (or tell the copilot
  "captions in Hindi script") for native script. This is transliteration, not translation —
  translation would need an AI model and would break the word-by-word timing.
- Helvetica is an Apple font and can't be shipped: Macs use the system Helvetica; Windows uses
  Arial Bold (or the bundled Arimo look-alike). To use real Helvetica on Windows, put your licensed
  `Helvetica-Bold.ttf/.otf` in `studio/fonts/` — any font file dropped there can be chosen by
  telling the copilot "use <font name> font".
- Effects pack: [claude-video-effects](https://github.com/Vibha-Ramprakash/claude-video-effects) by
  Vibha Ramprakash (MIT) — see `studio/effects/`. Effects render only around where they play, so
  long videos stay fast.
- Videos with background music already mixed in edit worse (music hides pauses and gets
  chopped at cuts). Record voice only; add music afterwards.
- Disk: each project keeps a copy of the source plus renders in `projects/`. Delete projects
  from the dashboard when done.
- Fonts: Poppins and Noto Sans, SIL Open Font License (`studio/fonts/OFL.txt`).
