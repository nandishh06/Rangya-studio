"""Shared helpers: paths, ffmpeg/ffprobe, style settings, time mapping."""
import copy
import json
import os
import re
import subprocess
from fractions import Fraction
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
PROJECTS = ROOT / "projects"
STYLE_FILE = ROOT / "style.json"
ENV_FILE = ROOT / ".env"
FONT_DIR = Path(__file__).resolve().parents[1] / "fonts"

FILLERS = {"um", "uh", "umm", "uhh", "erm", "er", "ah", "uhm", "hmm", "mm", "mhm"}

DEFAULT_STYLE = {
    "format": "original",          # original | 9:16 | 16:9
    "language": "",                # ISO code for ElevenLabs, "" = auto-detect
    "cut": {
        "max_pause": 0.18,         # pauses longer than this are cut down
        "pad_before": 0.05,        # breathing room before the first word after a cut
        "pad_after": 0.06,         # …and after the last word before a cut
        "remove_fillers": True,
        "remove_events": True,     # coughs, throat clears, breaths (laughs are kept)
        "remove_retakes": True,    # false starts and repeated takes (keeps the last take)
        "min_segment": 0.25,
        "silence_db": -38,         # quieter than this counts as silence (raise toward -30 in noisy rooms)
    },
    "zoom": {
        "mode": "punch",           # punch (alternate zoom on cuts) | push (slow push-in) | none
        "amount": 0.06,            # 0.06 = 6% zoom
    },
    "captions": {
        "enabled": True,
        "words": 5,                # max words on screen at once (kinetic: words per phrase)
        "position": 0.72,          # vertical centre of captions, fraction of frame height
        "size": 0.065,             # font size as a fraction of the frame's short side
        "uppercase": False,
        "color": "#FFFFFF",
        "highlight": "#FFD43B",    # word being spoken
        "emphasis": "#A5D8FF",     # emphasised / accent words
        "auto_emphasis": True,
        "style": "kinetic",        # classic (subtitle line) | kinetic (stacked words beside the face)
        "font": "Helvetica",       # Helvetica | Helvetica Neue | Arial | Poppins | Arimo
        "script": "latin",         # latin = English letters ("dekho"), original = as spoken ("देखो")
    },
    "speed": 1.08,                 # playback speed of the talking (1.1 = 10% faster)
    "look": {"enhance": False, "grade": "cinematic"},   # grade: none | vivid | cinematic | warm | moody
    "effects": {"auto": True, "density": "medium", "exclude": [],   # face-tracking effects pack
                "allow": ["lean-in", "liquid-glass-graph", "todo", "reject-stamp", "spotlight", "swipe", "tap",
                          "comment-box", "backdrop-word"]},                 # effects the auto-editor may use
    "audio": {"loudness": -14.0, "true_peak": -1.0, "highpass": 80, "denoise": False,
              "music": True, "music_volume": -20},   # music plays only if a track is uploaded
    "broll": {"enabled": True, "every": 4.5, "length": 2.4},  # used only if B-roll clips are uploaded
    "preview": {"short_side": 720, "preset": "veryfast", "crf": 28},
    "final": {"short_side": 1080, "preset": "medium", "crf": 18},
}


def deep_merge(base, over):
    out = copy.deepcopy(base)
    for k, v in (over or {}).items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = deep_merge(out[k], v)
        else:
            out[k] = v
    return out


def load_style():
    """Global style = defaults overlaid with the user's saved style.json."""
    saved = read_json(STYLE_FILE) if STYLE_FILE.exists() else {}
    return deep_merge(DEFAULT_STYLE, saved)


def project_style(pdir):
    """A project's effective settings: global style + per-project overrides."""
    over = read_json(pdir / "settings.json") if (pdir / "settings.json").exists() else {}
    return deep_merge(load_style(), over)


def read_env():
    vals = {}
    if ENV_FILE.exists():
        for line in ENV_FILE.read_text().splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                vals[k.strip()] = v.strip().strip('"').strip("'")
    return vals


def write_env(updates):
    lines = ENV_FILE.read_text().splitlines() if ENV_FILE.exists() else []
    seen = set()
    for n, line in enumerate(lines):
        k = line.split("=", 1)[0].strip()
        if k in updates:
            lines[n] = f"{k}={updates[k]}"
            seen.add(k)
    lines += [f"{k}={v}" for k, v in updates.items() if k not in seen]
    ENV_FILE.write_text("\n".join(lines) + "\n")
    try:
        os.chmod(ENV_FILE, 0o600)
    except OSError:
        pass


def env(key, default=None):
    return os.environ.get(key) or read_env().get(key) or default


def run(cmd, env=None):
    """Run a command; raise with the tail of stderr on failure."""
    r = subprocess.run([str(c) for c in cmd], capture_output=True, text=True, env=env, stdin=subprocess.DEVNULL,
                       encoding="utf-8", errors="replace")
    if r.returncode != 0:
        tail = "\n".join(r.stderr.strip().splitlines()[-12:])
        raise RuntimeError(f"{Path(str(cmd[0])).name} failed:\n{tail}")
    return r


def probe(path):
    r = run(["ffprobe", "-v", "error", "-print_format", "json", "-show_streams", "-show_format", path])
    info = json.loads(r.stdout)
    v = next((s for s in info["streams"] if s["codec_type"] == "video"), None)
    a = next((s for s in info["streams"] if s["codec_type"] == "audio"), None)
    if v is None:
        raise ValueError("this file has no video stream")
    fps = Fraction(v.get("avg_frame_rate") or "0/1")
    if fps == 0:
        fps = Fraction(v["r_frame_rate"])
    # Phone footage is often variable-rate (e.g. 25770/443); snap to the nearest standard rate.
    std = [Fraction(24000, 1001), Fraction(24), Fraction(25), Fraction(30000, 1001), Fraction(30),
           Fraction(50), Fraction(60000, 1001), Fraction(60)]
    near = min(std, key=lambda r: abs(float(r) - float(fps)))
    if abs(float(near) - float(fps)) / float(near) < 0.05:
        fps = near
    w, h = int(v["width"]), int(v["height"])
    rot = 0
    for sd in v.get("side_data_list", []) or []:
        if "rotation" in sd:
            rot = int(sd["rotation"])
    if abs(rot) in (90, 270):
        w, h = h, w
    return {
        "duration": float(info["format"]["duration"]),
        "fps": f"{fps.numerator}/{fps.denominator}",
        "fps_float": float(fps),
        "width": w,
        "height": h,
        "has_audio": a is not None,
    }


def read_json(path):
    return json.loads(Path(path).read_text(encoding="utf-8"))


def write_json(path, data):
    path = Path(path)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    os.replace(tmp, path)


def fmt_time(t, ms=True):
    t = max(0.0, t)
    h, rem = divmod(t, 3600)
    m, s = divmod(rem, 60)
    base = f"{int(m):02d}:{s:05.2f}" if ms else f"{int(m):02d}:{int(s):02d}"
    return f"{int(h)}:{base}" if h >= 1 else base


def norm_word(text):
    return re.sub(r"[^\w']", "", text.lower())


def is_filler(w):
    return w["type"] == "word" and norm_word(w["text"]) in FILLERS


def output_map(edl):
    """[(src_start, src_end, out_start)] for each kept segment."""
    out, t, k = [], 0.0, edl.get("speed", 1.0)
    for seg in edl["segments"]:
        out.append((seg["start"], seg["end"], t))
        t += (seg["end"] - seg["start"]) / k
    return out


def src_to_out(edl, t):
    k = edl.get("speed", 1.0)
    for s, e, o in output_map(edl):
        if s <= t <= e:
            return o + (t - s) / k
    return None


def out_duration(edl):
    k = edl.get("speed", 1.0)
    return sum(s["end"] - s["start"] for s in edl["segments"]) / k


def output_size(proj, fmt, short_side):
    """Even-numbered output dimensions for a format at a given short-side resolution."""
    w, h = proj["width"], proj["height"]
    if fmt == "9:16":
        W, H = short_side, short_side * 16 / 9
    elif fmt == "16:9":
        W, H = short_side * 16 / 9, short_side
    else:
        k = short_side / min(w, h)
        if k > 1:  # never upscale the original
            k = 1
        W, H = w * k, h * k
    return int(round(W / 2) * 2), int(round(H / 2) * 2)
