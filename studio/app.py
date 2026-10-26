"""Rangya Studio — local dashboard for automatic talking-head video editing.

Run:  python -m studio.app   (then open http://127.0.0.1:8000)
Env:  HOST (default 127.0.0.1; use 0.0.0.0 to open it to your LAN), PORT (default 8000)
"""
import json
import os
import re
import subprocess
import sys
import secrets
import shutil
import threading
import time
from pathlib import Path

from flask import Flask, abort, jsonify, request, send_file, send_from_directory

from . import jobs
from .engine.common import (DEFAULT_STYLE, PROJECTS, STYLE_FILE, deep_merge, env, load_style,
                            probe, project_style, read_json, run, write_env, write_json)
from .engine.copilot import Copilot, run_copilot, snapshot
from .engine import effects as fxe
from .engine import taste
from .engine.captions import output_words
from .engine.cutter import build_edl
from .engine.media import AUDIO_EXT, VIDEO_EXT as MEDIA_VIDEO_EXT, broll_clips, music_file
from .engine.render import signature
from .engine.decisions import emphasis_set, load_cuts, word_states
from .engine.textrender import FONT_CHOICES, font_label

STATIC = Path(__file__).parent / "static"
VIDEO_EXT = {".mp4", ".mov", ".m4v", ".mkv", ".webm", ".avi", ".mts", ".mxf"}
MEDIA = {"preview.mp4", "final.mp4", "captions.srt", "thumb.jpg", "fxstill.png"}

app = Flask(__name__, static_folder=None)


def pdir_for(pid):
    if not re.fullmatch(r"[a-z0-9-]+", pid or ""):
        abort(404)
    p = PROJECTS / pid
    if not (p / "project.json").exists():
        abort(404)
    return p


def summary(p):
    proj = read_json(p / "project.json")
    st = jobs.get_state(p)
    return {"id": p.name, "name": proj["name"], "created": proj["created"],
            "duration": proj["duration"], "width": proj["width"], "height": proj["height"],
            "state": st, "settings": project_style(p),
            "has_transcript": (p / "transcript.json").exists(), "dirty": is_dirty(p, st)}


def is_dirty(p, st):
    """True when settings or edits differ from what the current preview was rendered with."""
    prev = st.get("outputs", {}).get("preview")
    if not prev or not (p / "transcript.json").exists():
        return False
    return prev.get("sig") != signature(p)


PER_VIDEO = {"format", "language"}


def _flat(d, prefix=""):
    out = {}
    for k, v in d.items():
        key = f"{prefix}{k}"
        if isinstance(v, dict):
            out.update(_flat(v, key + "."))
        else:
            out[key] = v
    return out


def remember_selection(before, after):
    """What you select is what every new video gets: save changed style/effect choices globally."""
    a, b = _flat(before), _flat(after)
    patch = {}
    for key, val in b.items():
        if key.split(".")[0] in PER_VIDEO or a.get(key) == val:
            continue
        d = patch
        parts = key.split(".")
        for k in parts[:-1]:
            d = d.setdefault(k, {})
        d[parts[-1]] = val
    if patch:
        cur = read_json(STYLE_FILE) if STYLE_FILE.exists() else {}
        write_json(STYLE_FILE, deep_merge(cur, patch))


@app.get("/")
def index():
    return send_from_directory(STATIC, "index.html")


@app.get("/static/<path:name>")
def static_file(name):
    return send_from_directory(STATIC, name)


@app.get("/api/config")
def get_config():
    key = env("ELEVENLABS_API_KEY", "")
    return jsonify({"has_key": bool(key), "key_hint": f"…{key[-4:]}" if key else "",
                    "model": env("ELEVENLABS_STT_MODEL", "scribe_v1"),
                    "style": load_style(), "defaults": DEFAULT_STYLE,
                    "fonts": [[n, font_label(n)] for n in FONT_CHOICES]})


@app.post("/api/config")
def set_config():
    body = request.get_json(force=True)
    upd = {}
    if body.get("elevenlabs_key"):
        upd["ELEVENLABS_API_KEY"] = body["elevenlabs_key"].strip()
    if body.get("model"):
        upd["ELEVENLABS_STT_MODEL"] = body["model"].strip()
    if upd:
        write_env(upd)
    if "style" in body:
        write_json(STYLE_FILE, deep_merge(load_style(), body["style"]))
    return get_config()


@app.get("/api/projects")
def list_projects():
    PROJECTS.mkdir(exist_ok=True)
    items = [summary(p.parent) for p in PROJECTS.glob("*/project.json")]
    items.sort(key=lambda x: x["created"], reverse=True)
    return jsonify(items)


@app.post("/api/projects")
def create_project():
    f = request.files.get("video")
    if not f or not f.filename:
        return jsonify({"error": "No video file uploaded"}), 400
    ext = Path(f.filename).suffix.lower()
    if ext not in VIDEO_EXT:
        return jsonify({"error": f"Unsupported file type {ext}"}), 400
    slug = re.sub(r"[^a-z0-9]+", "-", Path(f.filename).stem.lower()).strip("-")[:40] or "video"
    pid = f"{slug}-{secrets.token_hex(3)}"
    p = PROJECTS / pid
    p.mkdir(parents=True)
    src = p / f"source{ext}"
    f.save(src)
    try:
        info = probe(src)
        if not info["has_audio"]:
            raise ValueError("this video has no audio track to transcribe")
    except Exception as e:
        shutil.rmtree(p, ignore_errors=True)
        return jsonify({"error": f"Can't read that video: {e}"}), 400
    write_json(p / "project.json", {"name": Path(f.filename).stem, "source": str(src),
                                     "created": time.time(), "use_taste": request.form.get("taste") == "1", **info})
    over = {}
    if request.form.get("format"):
        over["format"] = request.form["format"]
    if request.form.get("language") is not None:
        over["language"] = request.form["language"].strip()
    if request.form.get("captions") in ("0", "1"):
        over["captions"] = {"enabled": request.form["captions"] == "1"}
    write_json(p / "settings.json", over)
    try:
        run(["ffmpeg", "-y", "-v", "error", "-ss", f"{min(1.0, info['duration'] / 3):.2f}", "-i", src,
             "-frames:v", "1", "-vf", "scale=320:-2", p / "thumb.jpg"])
    except RuntimeError:
        pass
    jobs.enqueue(p, "process")
    return jsonify(summary(p))


@app.get("/api/projects/<pid>")
def get_project(pid):
    p = pdir_for(pid)
    s = summary(p)
    if (p / "edl.json").exists():
        s["stats"] = read_json(p / "edl.json")["stats"]
    return jsonify(s)


@app.delete("/api/projects/<pid>")
def delete_project(pid):
    p = pdir_for(pid)
    if jobs.get_state(p).get("status") in ("running", "queued"):
        return jsonify({"error": "This video is rendering — wait for it to finish, then delete it"}), 409
    shutil.rmtree(p)
    return jsonify({"ok": True})


@app.post("/api/projects/<pid>/run")
def run_job(pid):
    p = pdir_for(pid)
    job = request.get_json(force=True).get("job", "preview")
    if job not in ("process", "preview", "final"):
        abort(400)
    if jobs.get_state(p).get("status") in ("queued", "running"):
        return jsonify({"error": "A job is already running for this video"}), 409
    jobs.enqueue(p, job)
    return jsonify(summary(p))


def transcript_payload(p):
    words = read_json(p / "transcript.json")["words"]
    style = project_style(p)
    cuts = load_cuts(p)
    states = word_states(words, cuts, style["cut"])
    emph = emphasis_set(cuts, style["captions"])
    out_t = {}
    if (p / "edl.json").exists():
        edl = read_json(p / "edl.json")
        t, k = 0.0, edl.get("speed", 1.0)
        for seg in edl["segments"]:
            for i in seg["words"]:
                out_t[i] = round(t + (max(words[i]["start"], seg["start"]) - seg["start"]) / k, 3)
            t += (seg["end"] - seg["start"]) / k
        stats = edl["stats"]
    else:
        stats = None
    return {"words": [[w["text"], round(w["start"], 2), w["type"] == "event"] for w in words],
            "removed": {i: r for i, r in enumerate(states) if r},
            "emphasis": sorted(emph), "out_time": out_t, "stats": stats}


@app.get("/api/projects/<pid>/transcript")
def get_transcript(pid):
    p = pdir_for(pid)
    if not (p / "transcript.json").exists():
        return jsonify({"words": []})
    return jsonify(transcript_payload(p))


@app.post("/api/projects/<pid>/words")
def edit_words(pid):
    """{"action": "cut"|"keep"|"reset"|"emph"|"unemph", "ids": [...]}"""
    p = pdir_for(pid)
    body = request.get_json(force=True)
    ids = {int(i) for i in body.get("ids", [])}
    cuts = load_cuts(p)
    rm, keep = set(cuts.get("user_remove", [])), set(cuts.get("user_keep", []))
    ea, er = set(cuts.get("emphasis_add", [])), set(cuts.get("emphasis_remove", []))
    a = body.get("action")
    if a == "cut":
        rm |= ids
        keep -= ids
    elif a == "keep":
        keep |= ids
        rm -= ids
    elif a == "reset":
        rm -= ids
        keep -= ids
    elif a == "emph":
        ea |= ids
        er -= ids
    elif a == "unemph":
        er |= ids
        ea -= ids
    else:
        abort(400)
    cuts.update(user_remove=sorted(rm), user_keep=sorted(keep),
                emphasis_add=sorted(ea), emphasis_remove=sorted(er))
    write_json(p / "cuts.json", cuts)
    build_edl(p)
    return jsonify(transcript_payload(p))


@app.post("/api/projects/<pid>/settings")
def project_settings(pid):
    p = pdir_for(pid)
    body = request.get_json(force=True)
    cur = read_json(p / "settings.json") if (p / "settings.json").exists() else {}
    before = project_style(p)
    write_json(p / "settings.json", deep_merge(cur, body.get("settings", {})))
    taste.learn_settings(before, project_style(p))
    remember_selection(before, project_style(p))
    if body.get("save_as_default"):
        style = project_style(p)
        style.pop("language", None)
        write_json(STYLE_FILE, style)
    if (p / "transcript.json").exists():
        build_edl(p)
    return jsonify(summary(p))


@app.get("/api/projects/<pid>/assets")
def list_assets(pid):
    p = pdir_for(pid)
    out = {"broll": [], "music": None}
    for c in broll_clips(p):
        try:
            d = probe(c)["duration"]
        except Exception:
            d = None
        out["broll"].append({"name": c.name, "duration": d})
    m = music_file(p)
    out["music"] = m.name if m else None
    return jsonify(out)


@app.post("/api/projects/<pid>/assets")
def upload_assets(pid):
    """multipart: kind=broll|music, files=<one or more files>"""
    p = pdir_for(pid)
    kind = request.form.get("kind")
    files = request.files.getlist("files")
    if kind not in ("broll", "music") or not files:
        return jsonify({"error": "Choose B-roll clips or a music file"}), 400
    allowed = MEDIA_VIDEO_EXT if kind == "broll" else AUDIO_EXT
    d = p / kind
    d.mkdir(exist_ok=True)
    if kind == "music":
        for old in d.glob("*"):
            old.unlink()
        files = files[:1]
    saved = []
    for f in files:
        ext = Path(f.filename).suffix.lower()
        if ext not in allowed:
            return jsonify({"error": f"{f.filename}: unsupported file type"}), 400
        name = re.sub(r"[^A-Za-z0-9._-]+", "-", Path(f.filename).stem)[:50] + ext
        f.save(d / name)
        saved.append(name)
    return list_assets(pid)


@app.delete("/api/projects/<pid>/assets/<kind>/<name>")
def delete_asset(pid, kind, name):
    p = pdir_for(pid)
    if kind not in ("broll", "music") or "/" in name or "\\" in name:
        abort(400)
    f = p / kind / name
    if f.exists():
        f.unlink()
    return list_assets(pid)


@app.get("/api/effects")
def effects_catalog():
    return jsonify(fxe.catalog())


def user_effects(p):
    cuts = load_cuts(p)
    words = read_json(p / "transcript.json")["words"] if (p / "transcript.json").exists() else []
    total = read_json(p / "edl.json")["stats"]["output_duration"] if (p / "edl.json").exists() else None
    out = []
    for n, e in enumerate(cuts.get("effects", [])):
        start = e.get("start", 0)
        if total:
            start = max(0.0, min(start, total - min(e.get("dur", 2.5), total) - 0.05))
        when = (f"when you say “{words[e['word']]['text']}”" if "word" in e and e["word"] < len(words)
                else f"at {start:.1f}s")
        out.append({"n": n, "fx": e["fx"], "when": when, "dur": e.get("dur"), "text": e.get("text") or e.get("to")})
    return out


@app.get("/api/projects/<pid>/effects")
def list_user_effects(pid):
    return jsonify(user_effects(pdir_for(pid)))


def video_effects(p):
    """Everything in the current preview: your effects (with their index) and the auto picks."""
    st = jobs.get_state(p)
    shown = st.get("outputs", {}).get("preview", {}).get("effects", [])
    mine = user_effects(p)
    used, out = set(), []
    for e in shown:
        if e.get("why") == "you asked":
            n = next((u["n"] for u in mine if u["fx"] == e["fx"] and u["n"] not in used), None)
            if n is not None:
                used.add(n)
            out.append({**e, "source": "you", "n": n})
        else:
            out.append({**e, "source": "auto"})
    auto_on = project_style(p)["effects"].get("auto", True)
    blocked = len(load_cuts(p).get("fx_blocked", []))
    return {"effects": out, "auto": auto_on, "blocked": blocked}


@app.get("/api/projects/<pid>/effects/current")
def current_effects(pid):
    return jsonify(video_effects(pdir_for(pid)))


@app.post("/api/projects/<pid>/effects/remove")
def remove_effect(pid):
    """{"source": "you", "n": 2} or {"source": "auto", "fx": "spotlight", "start": 8.7}"""
    p = pdir_for(pid)
    b = request.get_json(force=True)
    snapshot(p)
    cuts = load_cuts(p)
    if b.get("source") == "you" and b.get("n") is not None:
        n = int(b["n"])
        if 0 <= n < len(cuts.get("effects", [])):
            gone = cuts["effects"].pop(n)
            taste.learn_effect("remove_own", gone["fx"], fx_trigger(p, gone))
    else:
        cuts.setdefault("fx_blocked", []).append({"fx": b["fx"], "t": round(float(b["start"]), 2)})
        shown = jobs.get_state(p).get("outputs", {}).get("preview", {}).get("effects", [])
        trig = next((x.get("trigger") for x in shown if x["fx"] == b["fx"] and abs(x["start"] - float(b["start"])) < 1), None)
        taste.learn_effect("reject_auto", b["fx"], trig)
    write_json(p / "cuts.json", cuts)
    start_preview(p)
    return jsonify({**video_effects(p), "user": user_effects(p), "project": summary(p)})


@app.post("/api/projects/<pid>/effects/clear")
def clear_effects(pid):
    """Remove every effect from this video (and stop auto effects here), or bring auto effects back."""
    p = pdir_for(pid)
    body = request.get_json(force=True)
    restore = body.get("restore", False)
    plain = body.get("plain", False)
    snapshot(p)
    cuts = load_cuts(p)
    if not restore:
        shown = jobs.get_state(p).get("outputs", {}).get("preview", {}).get("effects", [])
        if shown:
            taste.learn_clear(shown, read_json(p / "edl.json")["stats"]["output_duration"] / 60)
        cuts["effects"] = []
    cuts["fx_blocked"] = []
    write_json(p / "cuts.json", cuts)
    over = read_json(p / "settings.json") if (p / "settings.json").exists() else {}
    patch = {"effects": {"auto": bool(restore)}}
    if plain:      # this video only: no filters either — natural colour, no zoom, normal speed, simple captions
        patch.update({"look": {"grade": "none", "enhance": False}, "zoom": {"mode": "none"}, "speed": 1.0,
                      "captions": {"style": "classic"}, "broll": {"enabled": False}, "audio": {"music": False}})
    elif restore:  # back to your selection for filters too
        for k in ("look", "zoom", "speed", "broll"):
            over.pop(k, None)
        over.get("captions", {}).pop("style", None)
        over.get("audio", {}).pop("music", None)
    write_json(p / "settings.json", deep_merge(over, patch))
    start_preview(p)
    return jsonify({**video_effects(p), "user": user_effects(p), "project": summary(p)})


def effect_entry(p, b):
    """Build a cuts.json effect from the picker form, or raise ValueError."""
    fx = b.get("fx")
    if fx not in fxe.NAMES:
        raise ValueError("Unknown effect")
    e = {"fx": fx}
    mode = b.get("mode", "playhead")
    if mode == "word":
        phrase = (b.get("word") or "").strip()
        ids = Copilot(p).find_phrase(phrase, word_level=True) if phrase else []
        if not ids:
            raise ValueError(f"Couldn't find “{phrase}” in what you say")
        e["word"] = ids[0]
        e["dur"] = fxe.DEFAULT_DUR.get(fx, 2.5)
    elif mode == "range":
        a, z = sorted((float(b.get("from", 0)), float(b.get("to", 0))))
        if z - a < 0.5:
            raise ValueError("Make the effect at least half a second long")
        e["start"], e["dur"] = round(a, 2), round(z - a, 2)
    else:
        e["dur"] = fxe.DEFAULT_DUR.get(fx, 2.5)
        e["start"] = round(max(0.0, float(b.get("t", 0))), 2)
        if (p / "edl.json").exists():
            total = read_json(p / "edl.json")["stats"]["output_duration"]
            e["start"] = round(max(0.0, min(e["start"], total - e["dur"] - 0.05)), 2)  # fit inside the video
    text = (b.get("text") or "").strip()
    if text:
        e["text"] = text
    elif fx in fxe.NEEDS_TEXT:
        raise ValueError(f"{fx} needs your own text — type it in the Text box (nothing is made up for you)")
    second = fxe.SECOND_FIELD.get(fx)
    if second and (b.get("second") or "").strip():
        e[second[0]] = b["second"].strip()
    if fx == "liquid-glass-graph" and (b.get("value") or "").strip():
        e["from"], e["to"] = "0", b["value"].strip().upper()
    return e


def fx_trigger(p, e):
    """Kind of moment an effect sits on (hook, number, list, …) — what taste learns from."""
    if not (p / "edl.json").exists():
        return "moment"
    edl = read_json(p / "edl.json")
    words = read_json(p / "transcript.json")["words"]
    ws = output_words(edl, words, "original")
    t = e.get("start")
    if "word" in e:
        t = next((w["start"] for w in ws if w["i"] == e["word"]), t)
    return fxe.trigger_at(ws, ws, edl["stats"]["output_duration"], t or 0.0)


def learn_cut_changes(p, before, after):
    """Taste signals from a change to cuts.json (effects added / auto picks removed)."""
    old = [json.dumps(x, sort_keys=True) for x in before.get("effects", [])]
    for e in after.get("effects", []):
        if json.dumps(e, sort_keys=True) not in old:
            taste.learn_effect("add", e["fx"], fx_trigger(p, e), e.get("dur"))
    shown = jobs.get_state(p).get("outputs", {}).get("preview", {}).get("effects", [])
    for b in after.get("fx_blocked", [])[len(before.get("fx_blocked", [])):]:
        trig = next((x.get("trigger") for x in shown if x["fx"] == b["fx"] and abs(x["start"] - b["t"]) < 1), None)
        taste.learn_effect("reject_auto", b["fx"], trig)


@app.post("/api/projects/<pid>/effects")
def add_user_effect(pid):
    p = pdir_for(pid)
    try:
        e = effect_entry(p, request.get_json(force=True))
    except ValueError as err:
        return jsonify({"error": str(err)}), 400
    cuts = load_cuts(p)
    if any(x.get("fx") == e["fx"] and x.get("start") == e.get("start") and x.get("word") == e.get("word")
           for x in cuts.get("effects", [])):
        return jsonify({"error": f"{e['fx']} is already there — move the playhead or pick another moment"}), 400
    snapshot(p)
    cuts.setdefault("effects", []).append(e)
    write_json(p / "cuts.json", cuts)
    taste.learn_effect("add", e["fx"], fx_trigger(p, e), e.get("dur"))
    return jsonify({"effects": user_effects(p), "job": start_preview(p), "project": summary(p)})


def start_preview(p):
    """Re-render the preview after an edit (or right after the current job)."""
    if not (p / "transcript.json").exists():
        return None
    return jobs.request_job(p, "preview")


@app.delete("/api/projects/<pid>/effects/<int:n>")
def delete_user_effect(pid, n):
    p = pdir_for(pid)
    cuts = load_cuts(p)
    if 0 <= n < len(cuts.get("effects", [])):
        snapshot(p)
        cuts["effects"].pop(n)
        write_json(p / "cuts.json", cuts)
        start_preview(p)
    return jsonify({"effects": user_effects(p), "project": summary(p)})


def fx_needs_body(d):
    return d.get("fx") == "match-cut"


@app.post("/api/projects/<pid>/effects/still")
def effect_still(pid):
    """Render one frame of an effect on this video (from the current preview) so users can try it."""
    p = pdir_for(pid)
    src = p / "preview.mp4"
    if not src.exists():
        return jsonify({"error": "Wait for the first preview, then try effects on it"}), 400
    b = request.get_json(force=True)
    try:
        e = effect_entry(p, {**b, "mode": "playhead"})
    except ValueError as err:
        return jsonify({"error": str(err)}), 400
    work = p / ".fxstill"
    work.mkdir(exist_ok=True)
    t = max(0.0, float(b.get("t", 0)) - 0.6)
    clip = work / "in.mp4"
    try:
        run(["ffmpeg", "-y", "-v", "error", "-ss", f"{t:.2f}", "-i", src, "-t", "3", "-an", "-c:v", "libx264",
             "-preset", "ultrafast", "-crf", "16", clip])
        local = {k: v for k, v in e.items() if k not in ("start", "dur", "word")}
        local.update(start=0.2, end=2.8)
        if (p / "edl.json").exists() and (not local.get("text") or fx_needs_body(local)):
            edl = read_json(p / "edl.json")
            words = read_json(p / "transcript.json")["words"]
            st = project_style(p)
            ws = output_words(edl, words, "original")
            disp = output_words(edl, words, st["captions"].get("script", "original"))
            if not local.get("text") and local["fx"] in fxe.WORD_TEXT | fxe.LINES_TEXT:
                local["text"] = fxe.spoken_text(ws, disp, float(b.get("t", 0)), float(b.get("t", 0)) + 2.5,
                                                local["fx"] in fxe.LINES_TEXT)
            if local["fx"] == "match-cut":
                local["body"] = " ".join(d["text"] for d in disp).lower()
        (work / "plan.json").write_text(json.dumps({"effects": [local]}))
        r = subprocess.run([sys.executable, str(fxe.FX_SCRIPT), "--plan", str(work / "plan.json"), str(clip),
                            str(p / "fxstill.png"), "--still", "1.8", "--width", "540"],
                           capture_output=True, text=True, stdin=subprocess.DEVNULL, timeout=180)
        if r.returncode != 0 or not (p / "fxstill.png").exists():
            raise RuntimeError((r.stderr or r.stdout).strip().splitlines()[-1:] or ["failed"])
    except Exception as err:
        return jsonify({"error": f"Couldn't preview that effect: {err}"}), 500
    return jsonify({"url": f"/api/projects/{pid}/media/fxstill.png?t={time.time()}"})


@app.get("/api/taste")
def taste_profile():
    return jsonify(taste.report(taste.load()))


@app.post("/api/taste")
def taste_options():
    b = request.get_json(force=True)
    t = taste.load()
    if b.get("reset"):
        taste.reset()
    elif "auto_new" in b:
        t["auto_new"] = bool(b["auto_new"])
        taste.save(t)
    return taste_profile()


def describe_patch(before, after):
    """Human-readable list of what Auto taste changed."""
    names = {"cut.max_pause": "longest pause", "cut.pad_after": "breath after sentences", "speed": "speed",
             "zoom.mode": "zoom", "zoom.amount": "zoom amount", "captions.style": "caption style",
             "captions.size": "caption size", "captions.position": "caption position", "captions.words": "words on screen",
             "captions.font": "font", "captions.uppercase": "uppercase", "captions.highlight": "spoken-word colour",
             "captions.color": "caption colour", "look.grade": "colour grade", "effects.density": "how many effects",
             "audio.music_volume": "music volume", "broll.every": "B-roll every", "effects.auto": "auto effects"}
    out = []
    for key, label in names.items():
        a, b = taste._get(before, key), taste._get(after, key)
        if a != b and b is not None:
            out.append(f"{label}: {a} → {b}")
    a, b = set(taste._get(before, "effects.allow") or []), set(taste._get(after, "effects.allow") or [])
    if b - a:
        out.append("now uses: " + ", ".join(sorted(b - a)))
    if a - b:
        out.append("stopped using: " + ", ".join(sorted(a - b)))
    return out


@app.post("/api/projects/<pid>/taste/apply")
def apply_taste(pid):
    """Auto taste: rebuild this video from the learned taste, then render."""
    p = pdir_for(pid)
    t = taste.load()
    if not taste.has_data(t):
        return jsonify({"error": "I haven't learned your taste yet — add or remove a few effects, change the style, "
                                 "or render a final. Every change teaches me."}), 400
    before = project_style(p)
    patch, _ = taste.settings_patch(t, fxe.NEEDS_TEXT, before["effects"].get("allow", []))
    snapshot(p)
    over = read_json(p / "settings.json") if (p / "settings.json").exists() else {}
    keep = {k: over[k] for k in ("format", "language") if k in over}
    write_json(p / "settings.json", deep_merge(deep_merge(over, patch), keep))
    remember_selection(before, project_style(p))
    cuts = load_cuts(p)
    cuts["fx_blocked"] = []            # taste decides the auto picks now
    write_json(p / "cuts.json", cuts)
    changes = describe_patch(before, project_style(p))
    rep = taste.report(t)
    lines = [f"✦ Auto taste applied (confidence {rep['confidence']}%, learned from {rep['signals']} edits"
             f" and {rep['finals']} final renders)."]
    lines += [f"✓ {c}" for c in changes] or ["✓ Your style already matches your taste — refreshing the effect picks"]
    if rep["per_moment"]:
        lines.append("✓ Effects by moment: " + "; ".join(f"{m['moment']} → {m['fx']}" for m in rep["per_moment"]))
    lines.append("▶ Rendering the preview. Tweak anything — every change refines your taste.")
    job = start_preview(p)
    log = chat_log(p) + [{"role": "copilot", "text": "\n".join(lines), "t": time.time()}]
    write_json(p / "copilot.json", log[-200:])
    return jsonify({"reply": "\n".join(lines), "job": job, "project": summary(p)})


def chat_log(p):
    f = p / "copilot.json"
    return read_json(f) if f.exists() else []


@app.get("/api/projects/<pid>/copilot")
def copilot_history(pid):
    return jsonify(chat_log(pdir_for(pid)))


@app.post("/api/projects/<pid>/copilot")
def copilot(pid):
    p = pdir_for(pid)
    msg = (request.get_json(force=True).get("message") or "").strip()
    if not msg:
        return jsonify({"error": "Type what you'd like changed"}), 400
    if re.search(r"\b(start (over|again|fresh|from scratch)|reset (the |my |this )?(edit|video|everything)|"
                 r"go back to (the )?original|original video|from scratch|undo everything)\b", msg.lower()):
        job = reset_project(p)
        reply = ("↺ Started over from the original video — a clean first edit is rendering. "
                 "Say “undo” to get your previous edit back.")
        log = chat_log(p) + [{"role": "user", "text": msg, "t": time.time()},
                             {"role": "copilot", "text": reply, "t": time.time()}]
        write_json(p / "copilot.json", log[-200:])
        return jsonify({"reply": reply, "job": job, "project": summary(p)})
    before_style, before_cuts = project_style(p), load_cuts(p)
    cp = run_copilot(p, msg)
    if cp.changed and msg.strip().lower() not in ("undo", "undo that"):
        taste.learn_settings(before_style, project_style(p))
        remember_selection(before_style, project_style(p))
        learn_cut_changes(p, before_cuts, load_cuts(p))
    lines = [f"✓ {d}" if "\n" not in d else d for d in cp.done]
    lines += [f"⚠ {w}" for w in dict.fromkeys(cp.warn)]
    if cp.unknown:
        lines.append("? I didn't understand: " + "; ".join(f"“{u}”" for u in cp.unknown) +
                     " — type “help” to see what I can do.")
    has_tx = (p / "transcript.json").exists()
    if cp.changed and has_tx:
        build_edl(p)
    if cp.save_default:
        style = project_style(p)
        style.pop("language", None)
        write_json(STYLE_FILE, style)
        lines.append("✓ Saved as your default style for new videos")
    job = None
    if cp.want == "final":
        job = "final"
    elif (cp.changed or cp.want == "preview" or (cp.done and is_dirty(p, jobs.get_state(p)))) \
            and not cp.no_render:
        job = "preview"
    if job and not has_tx:
        lines.append("… Your settings are saved; they'll be used when the first preview finishes processing.")
    elif job:
        how = jobs.request_job(p, job)
        what = "final 1080p render" if job == "final" else "preview"
        lines.append(f"▶ Starting the {what}…" if how == "started"
                     else f"▶ The {what} will start as soon as the current job finishes.")
    elif not cp.changed and not lines:
        lines.append("Nothing to change.")
    reply = "\n".join(lines)
    log = chat_log(p)
    log += [{"role": "user", "text": msg, "t": time.time()},
            {"role": "copilot", "text": reply, "t": time.time()}]
    write_json(p / "copilot.json", log[-200:])
    return jsonify({"reply": reply, "job": job, "project": summary(p)})


_orig_lock = threading.Lock()


@app.get("/api/projects/<pid>/original")
def original_video(pid):
    """The untouched upload, as a browser-friendly copy (made once, on first view)."""
    p = pdir_for(pid)
    out = p / "original.mp4"
    with _orig_lock:
        if not out.exists():
            proj = read_json(p / "project.json")
            tmp = p / ".original.tmp.mp4"
            run(["ffmpeg", "-y", "-v", "error", "-i", proj["source"], "-vf", "scale='if(gt(iw,ih),-2,720)':'if(gt(iw,ih),720,-2)'",
                 "-c:v", "libx264", "-preset", "veryfast", "-crf", "24", "-pix_fmt", "yuv420p",
                 "-c:a", "aac", "-b:a", "128k", "-movflags", "+faststart", tmp])
            tmp.replace(out)
    return send_file(out, conditional=True, max_age=0)


UPLOAD_KEYS = ("format", "language")


def reset_project(p):
    """Back to a clean first edit of the original: drop every edit, effect and style change."""
    snapshot(p)
    over = read_json(p / "settings.json") if (p / "settings.json").exists() else {}
    fresh = {k: over[k] for k in UPLOAD_KEYS if k in over}
    if "enabled" in over.get("captions", {}):
        fresh["captions"] = {"enabled": over["captions"]["enabled"]}
    write_json(p / "settings.json", fresh)
    cuts = load_cuts(p)
    write_json(p / "cuts.json", {"auto": cuts.get("auto", []), "auto_emphasis": cuts.get("auto_emphasis", []),
                                 "user_remove": [], "user_keep": [], "emphasis_add": [], "emphasis_remove": [],
                                 "effects": [], "fx_blocked": []})
    proj = read_json(p / "project.json")
    proj["use_taste"] = False
    write_json(p / "project.json", proj)
    if (p / "transcript.json").exists():
        build_edl(p)
    return start_preview(p)


@app.post("/api/projects/<pid>/reset")
def reset_edit(pid):
    p = pdir_for(pid)
    job = reset_project(p)
    log = chat_log(p) + [{"role": "copilot", "t": time.time(),
                          "text": "↺ Started over from the original video — a clean first edit is rendering. "
                                  "Say “undo” to get your previous edit back."}]
    write_json(p / "copilot.json", log[-200:])
    return jsonify({"job": job, "project": summary(p)})


@app.get("/api/projects/<pid>/media/<name>")
def media(pid, name):
    p = pdir_for(pid)
    if name not in MEDIA or not (p / name).exists():
        abort(404)
    dl = request.args.get("download")
    proj = read_json(p / "project.json")
    dl_name = f"{proj['name']}-{name}" if dl else None
    return send_file(p / name, conditional=True, as_attachment=bool(dl), download_name=dl_name,
                     max_age=0)


def main():
    PROJECTS.mkdir(exist_ok=True)
    if not shutil.which("ffmpeg") or not shutil.which("ffprobe"):
        raise SystemExit("ffmpeg/ffprobe not found on PATH — install ffmpeg first (see README.md).")
    jobs.start()
    host = os.environ.get("HOST", "127.0.0.1")
    port = int(os.environ.get("PORT", "8000"))
    print(f"\n  Rangya Studio → http://{'127.0.0.1' if host == '0.0.0.0' else host}:{port}\n")
    from waitress import serve
    serve(app, host=host, port=port, threads=8, max_request_body_size=20 * 1024 ** 3)


if __name__ == "__main__":
    main()
