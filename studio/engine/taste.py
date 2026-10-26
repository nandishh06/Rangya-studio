"""Taste: a small local preference model learned from how the user edits — no AI service.

Signals (strongest first):
  final render     → approval of the current settings and every effect in it
  add effect       → likes that effect, on that kind of moment (number, list, "never", hook, CTA…)
  remove auto pick → dislikes that effect on that kind of moment
  tick / untick    → mild like / dislike
  setting change   → preferred value for that setting
Scores decay a little on every new signal, so recent choices count most and each fine-tune
moves the model. "Auto taste" turns the model back into settings + effect choices.
"""
import time

from .common import ROOT, read_json, write_json

TASTE_FILE = ROOT / "taste.json"

NUM_KEYS = {  # setting → (min, max, decimals)
    "cut.max_pause": (0.1, 1.5, 2), "cut.pad_after": (0.03, 0.5, 2), "cut.pad_before": (0.02, 0.4, 2),
    "speed": (0.9, 1.3, 2), "zoom.amount": (0.02, 0.3, 2), "captions.size": (0.03, 0.14, 3),
    "captions.position": (0.1, 0.9, 2), "captions.words": (1, 6, 0), "audio.music_volume": (-40, -6, 0),
    "broll.every": (2.0, 30.0, 1), "broll.length": (0.8, 8.0, 1),
}
CAT_KEYS = ["captions.style", "captions.font", "captions.script", "captions.uppercase", "captions.color",
            "captions.highlight", "captions.emphasis", "captions.enabled", "zoom.mode", "look.grade",
            "effects.density", "audio.music", "audio.denoise", "broll.enabled", "cut.remove_fillers",
            "cut.remove_retakes", "cut.remove_events"]
LABELS = {
    "hook": "your opening hook", "number": "numbers you say", "negation": "“never / wrong / don't”",
    "spot": "“honestly / truth / secret”", "list": "lists of steps", "cta-follow": "“follow” at the end",
    "cta-comment": "“comment …” at the end", "before-after": "before / after", "emphatic": "short punchy lines",
    "moment": "other key moments",
}


def load():
    if not TASTE_FILE.exists():
        return bootstrap()
    t = read_json(TASTE_FILE)
    t.setdefault("signals", 0)
    t.setdefault("finals", 0)
    t.setdefault("auto_new", False)
    for k in ("fx", "ctx", "dur", "num", "cat"):
        t.setdefault(k, {})
    t.setdefault("density", None)
    t.setdefault("history", [])
    return t


def bootstrap():
    """First run: start from the effects the user already added by hand in their projects."""
    from .common import PROJECTS
    t = {"signals": 0, "finals": 0, "auto_new": False, "fx": {}, "ctx": {}, "dur": {}, "num": {}, "cat": {},
         "density": None, "history": []}
    for cuts in sorted(PROJECTS.glob("*/cuts.json")):
        for e in read_json(cuts).get("effects", []):
            _bump(t, e["fx"], "moment", 2.0)
            t["signals"] += 1
    if t["signals"]:
        _note(t, "start", f"learned from {t['signals']} effects you added before")
        t["signals"] -= 1
    save(t)
    return t


def save(t):
    write_json(TASTE_FILE, t)


def _get(d, path):
    for k in path.split("."):
        if not isinstance(d, dict) or k not in d:
            return None
        d = d[k]
    return d


def _decay(t, factor):
    for table in ("fx", "ctx"):
        for k in list(t[table]):
            t[table][k] *= factor
            if abs(t[table][k]) < 0.05:
                del t[table][k]
    for k, votes in t["cat"].items():
        for v in list(votes):
            votes[v] *= factor


def _bump(t, fx, trigger, w):
    t["fx"][fx] = t["fx"].get(fx, 0.0) + w
    if trigger:
        key = f"{trigger}|{fx}"
        t["ctx"][key] = t["ctx"].get(key, 0.0) + w


def _note(t, kind, detail):
    t["signals"] += 1
    t["history"] = (t["history"] + [{"t": round(time.time()), "kind": kind, "detail": detail}])[-60:]


def _num(t, key, value, alpha):
    if value is None or isinstance(value, bool):
        return
    cur = t["num"].get(key)
    t["num"][key] = [float(value), 1] if cur is None else [cur[0] + alpha * (float(value) - cur[0]), cur[1] + 1]


def _cat(t, key, value, w):
    if value is None:
        return
    votes = t["cat"].setdefault(key, {})
    for v in list(votes):
        votes[v] *= 0.85
    v = str(value).lower() if isinstance(value, bool) else str(value)
    votes[v] = votes.get(v, 0.0) + w


# ---------- learning ----------
def learn_settings(old, new, weight=0.6):
    """A settings change (Style tab, copilot, Auto ticks)."""
    t = load()
    changed = []
    for key in NUM_KEYS:
        a, b = _get(old, key), _get(new, key)
        if b is not None and a != b:
            _num(t, key, b, 0.4)
            changed.append(key)
    for key in CAT_KEYS:
        a, b = _get(old, key), _get(new, key)
        if b is not None and a != b:
            _cat(t, key, b, weight)
            changed.append(key)
    a_allow = set(_get(old, "effects.allow") or [])
    b_allow = set(_get(new, "effects.allow") or [])
    for fx in b_allow - a_allow:
        _bump(t, fx, None, 1.0)
        changed.append(f"+{fx}")
    for fx in a_allow - b_allow:
        _bump(t, fx, None, -2.0)
        changed.append(f"-{fx}")
    if changed:
        _decay(t, 0.98)
        _note(t, "settings", ", ".join(changed[:6]))
        save(t)


def learn_effect(kind, fx, trigger, dur=None):
    """kind: add | remove_own | reject_auto."""
    t = load()
    w = {"add": 3.0, "remove_own": -1.5, "reject_auto": -3.0}[kind]
    _decay(t, 0.98)
    _bump(t, fx, trigger or "moment", w)
    if kind == "add" and dur:
        cur = t["dur"].get(fx)
        t["dur"][fx] = float(dur) if cur is None else cur + 0.4 * (float(dur) - cur)
    _note(t, kind, f"{fx} on {LABELS.get(trigger or 'moment', trigger)}")
    save(t)


def learn_clear(effects, minutes):
    t = load()
    _decay(t, 0.98)
    for e in effects:
        _bump(t, e["fx"], e.get("trigger"), -1.0)
    t["density"] = 0.0 if t["density"] is None else t["density"] * 0.6
    _note(t, "clear", f"removed {len(effects)} effects")
    save(t)


def learn_final(style, effects, minutes):
    """A final render = the user approves this video: reinforce everything in it."""
    t = load()
    _decay(t, 0.95)
    for key in NUM_KEYS:
        _num(t, key, _get(style, key), 0.3)
    for key in CAT_KEYS:
        _cat(t, key, _get(style, key), 1.0)
    for e in effects:
        _bump(t, e["fx"], e.get("trigger") or "moment", 1.0)
    rate = len(effects) / max(minutes, 0.1)
    t["density"] = rate if t["density"] is None else t["density"] + 0.35 * (rate - t["density"])
    t["finals"] += 1
    _note(t, "final", f"{len(effects)} effects approved")
    save(t)


def reset():
    keep = load().get("auto_new", False)
    save({"auto_new": keep})


# ---------- using the model ----------
def score(t, fx, trigger):
    return t["ctx"].get(f"{trigger}|{fx}", 0.0) + 0.5 * t["fx"].get(fx, 0.0)


def has_data(t):
    return t["signals"] >= 3 or t["finals"] >= 1


def settings_patch(t, needs_text=frozenset(), current_allow=()):
    """Learned settings as a nested patch (only where there's evidence)."""
    patch, notes = {}, []

    def put(path, value):
        d = patch
        keys = path.split(".")
        for k in keys[:-1]:
            d = d.setdefault(k, {})
        d[keys[-1]] = value

    for key, (lo, hi, dp) in NUM_KEYS.items():
        v = t["num"].get(key)
        if v:
            val = round(min(hi, max(lo, v[0])), dp)
            put(key, int(val) if dp == 0 else val)
            notes.append(key)
    for key in CAT_KEYS:
        votes = t["cat"].get(key)
        if not votes:
            continue
        best = max(votes, key=votes.get)
        if votes[best] < 0.5:
            continue
        val = {"true": True, "false": False}.get(best, best)
        put(key, val)
        notes.append(key)
    liked = [fx for fx, s in sorted(t["fx"].items(), key=lambda kv: -kv[1]) if s > 0.5 and fx not in needs_text]
    disliked = {fx for fx, s in t["fx"].items() if s < -0.5}
    if liked or disliked:
        allow = list(dict.fromkeys(liked + [fx for fx in current_allow if fx not in needs_text]))
        put("effects.allow", [fx for fx in allow if fx not in disliked])
    if t["density"] is not None:
        d = t["density"]
        if d < 0.8 and t["finals"] >= 1:
            put("effects.auto", False)
        else:
            put("effects.auto", True)
            put("effects.density", "low" if d < 4.5 else "medium" if d < 9 else "high")
    put("effects.taste", True)
    return patch, notes


def report(t):
    likes = [(fx, round(s, 1)) for fx, s in sorted(t["fx"].items(), key=lambda kv: -kv[1]) if s > 0.5][:8]
    dislikes = [(fx, round(s, 1)) for fx, s in sorted(t["fx"].items(), key=lambda kv: kv[1]) if s < -0.5][:6]
    per = {}
    for key, s in t["ctx"].items():
        trig, fx = key.split("|", 1)
        if s > 0.5 and (trig not in per or s > per[trig][1]):
            per[trig] = (fx, round(s, 1))
    style = {}
    for key, v in t["num"].items():
        style[key] = round(v[0], NUM_KEYS.get(key, (0, 0, 2))[2])
    for key, votes in t["cat"].items():
        if votes:
            best = max(votes, key=votes.get)
            if votes[best] >= 0.5:
                style[key] = best
    level = min(100, t["signals"] * 4 + t["finals"] * 20)
    return {"signals": t["signals"], "finals": t["finals"], "confidence": level, "auto_new": t["auto_new"],
            "likes": likes, "dislikes": dislikes,
            "per_moment": [{"moment": LABELS.get(k, k), "fx": v[0], "score": v[1]} for k, v in per.items()],
            "style": style, "density": None if t["density"] is None else round(t["density"], 1),
            "history": list(reversed(t["history"][-12:]))}
