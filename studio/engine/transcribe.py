"""ElevenLabs Scribe speech-to-text with word-level timestamps."""
import requests

from .common import env, read_json, run, write_json

API_URL = "https://api.elevenlabs.io/v1/speech-to-text"


def extract_audio(src, dst):
    run(["ffmpeg", "-y", "-v", "error", "-i", src, "-vn", "-ac", "1", "-ar", "16000",
         "-c:a", "libmp3lame", "-b:a", "48k", dst])


def transcribe(pdir, language="", log=print):
    """Write pdir/transcript.json. Skips if it already exists."""
    out = pdir / "transcript.json"
    if out.exists():
        return read_json(out)
    proj = read_json(pdir / "project.json")
    audio = pdir / "audio.mp3"
    if not audio.exists():
        log("Extracting audio…")
        extract_audio(proj["source"], audio)

    key = env("ELEVENLABS_API_KEY")
    if not key:
        raise RuntimeError("No ElevenLabs API key. Add it in Settings (top right), then press Retry.")
    model = env("ELEVENLABS_STT_MODEL", "scribe_v1")
    log(f"Transcribing with ElevenLabs ({model})…")
    data = {"model_id": model, "tag_audio_events": "true", "timestamps_granularity": "word"}
    if language:
        data["language_code"] = language
    with open(audio, "rb") as f:
        r = requests.post(API_URL, headers={"xi-api-key": key}, data=data,
                          files={"file": ("audio.mp3", f, "audio/mpeg")}, timeout=3600)
    if r.status_code == 401:
        raise RuntimeError("ElevenLabs rejected the API key (401). Check it in Settings.")
    if r.status_code >= 400:
        raise RuntimeError(f"ElevenLabs error {r.status_code}: {r.text[:400]}")
    res = r.json()

    words = []
    for w in res.get("words", []):
        if w.get("type") == "spacing" or w.get("start") is None:
            continue
        text = (w.get("text") or "").strip()
        if not text:
            continue
        words.append({
            "text": text,
            "start": float(w["start"]),
            "end": max(float(w["end"]), float(w["start"])),
            "type": "event" if w.get("type") == "audio_event" else "word",
        })
    words.sort(key=lambda w: w["start"])
    for i, w in enumerate(words):
        w["i"] = i
    t = {"engine": f"elevenlabs:{model}", "language": res.get("language_code"),
         "duration": proj["duration"], "words": words}
    write_json(out, t)
    log(f"Transcribed {len(words)} words.")
    return t
