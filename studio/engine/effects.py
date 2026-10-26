"""Face-tracking effects (the 42-effect pack in studio/effects) — planning and rendering.

Auto Effects reads the edited transcript and places effects where they fit (a punch-in on the
hook, a glass graph when you say a number, a checklist when you list steps, a stamp on "never",
a Follow button on your call-to-action…). Effects the user asks for (via the copilot) are kept in
cuts.json["effects"], anchored to a word so they survive re-cuts.

Rendering runs the pack only on short windows around each effect and overlays the result, so a
long video with a few effects stays fast.
"""
import hashlib
import json
import re
import subprocess
import sys
from pathlib import Path

from .captions import output_words
from .common import run
from .translit import to_latin

FX_DIR = Path(__file__).resolve().parents[1] / "effects"
FX_SCRIPT = FX_DIR / "scripts" / "fx.py"
_SRC = FX_SCRIPT.read_text(encoding="utf-8")
NAMES = re.findall(r"@effect\('([a-z-]+)'", _SRC)
DESCS = dict(re.findall(r"@effect\('([a-z-]+)', '([^']*)'\)", _SRC))
DESCS.update({  # Rangya: describe what the effect shows here (demo texts were removed)
    "glass-widgets": "frosted glass cards float around your head, with your big number, name and @handle",
    "tier-list": "a tier list drops in front of you and the item you type flies into S tier",
    "checklist": "a task list builds under your chin: each step you list spins, then ticks green",
    "comment-box": "an Instagram comment sheet above your head types the word from your call-to-action and posts it",
    "match-cut": "news pages made of your own words cut on one word, locked in a red box behind your head",
})
CATEGORIES = {
    "Camera & look": ["lean-in", "locked-on", "slow-push", "rabbit-hole", "dot-reveal", "moody", "swipe"],
    "Big text": ["backdrop-word", "spotlight", "led-sign", "match-cut", "reject-stamp", "red-collage", "pastel-word",
                 "headline-box", "split-headline", "cover-star", "scribble", "pause-point", "uninstall"],
    "Cards & UI": ["todo", "checklist", "apple-toggles", "comment-box", "tap", "the-bill", "glass-widgets",
                   "liquid-glass-graph", "tier-list", "countdown-flip", "timeline", "check-pills", "headline-wall"],
    "Reels & images": ["noise", "lineup", "orbit", "carousel-spin", "phone-fan", "profile-wall", "reel-frame",
                       "broll-split", "shortcut-rain"],
}
# What the main `text` option means for each effect (shown in the picker); "|" separates lines/items.
TEXT_HINT = {
    "backdrop-word": "Giant word behind you", "spotlight": "Glowing word behind you (optional)", "led-sign": "Sign text (lines with |)",
    "match-cut": "The word", "reject-stamp": "Stamp text", "red-collage": "Big red word", "pastel-word": "Giant word",
    "headline-box": "Two lines, e.g. How I edit|in 4 minutes", "split-headline": "Headline lines with |",
    "cover-star": "Magazine title", "scribble": "Label", "pause-point": "Label", "uninstall": "App name",
    "todo": "Items with |", "checklist": "Steps with |", "apple-toggles": "Rows with |", "comment-box": "Comment text",
    "the-bill": "Lines with |", "glass-widgets": "Big number", "liquid-glass-graph": "Label (set the number below)",
    "tier-list": "Title", "timeline": "Title", "check-pills": "Pills with |", "headline-wall": "Headlines with |",
    "lineup": "Tile names with |", "locked-on": "Tag", "reel-frame": "@handle", "broll-split": "Price-tag text",
    "shortcut-rain": "Keys with |", "phone-fan": "Headline lines with |", "profile-wall": "Label",
}
# Template effects: several text slots (names, handles, cover lines, stats) that only the user can supply.
# They are never auto-picked and need typed text when added — so nothing on screen is made up.
NEEDS_TEXT = {"the-bill", "cover-star", "glass-widgets", "pastel-word", "check-pills", "timeline", "profile-wall",
              "tier-list", "broll-split", "headline-wall", "uninstall", "reel-frame", "lineup"}
# A second text slot some effects have (option key, label in the picker).
SECOND_FIELD = {"cover-star": ("lines", "Cover lines with |"), "pastel-word": ("handle", "@handle"),
                "profile-wall": ("label", "Counter label"), "tier-list": ("item", "What flies into S tier"),
                "timeline": ("sub", "Subtitle"), "glass-widgets": ("handle", "@handle"),
                "headline-wall": ("total", "Total, e.g. 672K"), "check-pills": ("handle", "@handle"),
                "red-collage": ("lines", "Lines with |"), "checklist": ("title", "Card title"),
                "todo": ("title", "Card title"), "split-headline": ("hl", "Word to highlight")}
# Only placed by their trigger rules (a number you say, a list, "comment X", "follow", "never"…).
RULE_ONLY = {"liquid-glass-graph", "todo", "checklist", "apple-toggles", "comment-box", "tap", "reject-stamp",
             "countdown-flip", "swipe"}
# Effects the auto-editor uses out of the box (users tick more in the Effects tab).
DEFAULT_AUTO = ["lean-in", "liquid-glass-graph", "todo", "reject-stamp", "spotlight", "swipe", "tap", "comment-box",
                "backdrop-word"]
# Shape of the text each effect needs when the auto-editor fills it from what you say.
WORD_TEXT = {"backdrop-word", "spotlight", "match-cut", "red-collage", "scribble", "pause-point", "reject-stamp"}
LINES_TEXT = {"led-sign", "headline-box", "split-headline", "phone-fan"}


# Numbering from the effects pack README; the first 36 appear (labelled) in static/effects-preview.jpg, a 9×4 grid.
README_ORDER = ["noise", "lineup", "locked-on", "orbit", "lean-in", "backdrop-word", "spotlight", "the-bill", "moody",
                "swipe", "todo", "comment-box", "tap", "pause-point", "uninstall", "reel-frame", "shortcut-rain",
                "reject-stamp", "countdown-flip", "rabbit-hole", "cover-star", "scribble", "glass-widgets", "red-collage",
                "split-headline", "pastel-word", "check-pills", "timeline", "headline-box", "profile-wall", "tier-list",
                "broll-split", "phone-fan", "dot-reveal", "headline-wall", "checklist", "liquid-glass-graph",
                "carousel-spin", "match-cut", "apple-toggles", "led-sign", "slow-push"]


def catalog():
    order = README_ORDER
    out = []
    for cat, names in CATEGORIES.items():
        for n in names:
            if n not in DESCS:
                continue
            i = order.index(n)
            out.append({"name": n, "desc": DESCS[n], "category": cat, "hint": TEXT_HINT.get(n),
                        "needs_text": n in NEEDS_TEXT, "auto_ok": n not in NEEDS_TEXT,
                        "second": SECOND_FIELD.get(n),
                        "thumb": [i % 9, i // 9] if i < 36 else None, "number": i + 1})
    return out
ALIASES = {"backdrop": "backdrop-word", "giant word": "backdrop-word", "stamp": "reject-stamp", "follow button": "tap",
           "follow": "tap", "comment": "comment-box", "countdown": "countdown-flip", "glass graph": "liquid-glass-graph",
           "graph": "liquid-glass-graph", "counter": "liquid-glass-graph", "widgets": "glass-widgets", "led": "led-sign",
           "to-do": "todo", "to do": "todo", "red stamp": "reject-stamp", "liquid glass": "liquid-glass-graph",
           "comment box": "comment-box", "led sign": "led-sign", "glass widgets": "glass-widgets", "checklist": "checklist", "push in": "slow-push", "punch in": "lean-in",
           "punch-in": "lean-in", "zoom in": "lean-in", "lock on": "locked-on", "receipt": "the-bill", "bill": "the-bill",
           "magazine": "cover-star", "cover": "cover-star", "toggles": "apple-toggles", "carousel": "carousel-spin",
           "tunnel": "rabbit-hole", "freeze": "pause-point", "arrow": "pause-point", "doodles": "scribble",
           "keys": "shortcut-rain", "headline": "headline-box", "before after": "swipe", "before and after": "swipe"}
DEFAULT_DUR = {"lean-in": 1.0, "reject-stamp": 1.4, "backdrop-word": 2.0, "spotlight": 2.4, "liquid-glass-graph": 3.0,
               "todo": 5.0, "checklist": 5.0, "tap": 2.0, "comment-box": 2.6, "countdown-flip": 3.0, "slow-push": 2.6,
               "swipe": 2.4, "locked-on": 2.4, "led-sign": 3.0, "orbit": 3.0, "glass-widgets": 3.0}
GAP = {"low": 14.0, "medium": 8.0, "high": 5.0}
# Where each effect's graphics sit, so captions can get out of the way.
ABOVE_HEAD = {"backdrop-word", "spotlight", "led-sign", "cover-star", "match-cut", "pastel-word", "red-collage", "the-bill",
              "lineup", "countdown-flip", "apple-toggles", "headline-wall", "noise", "profile-wall", "split-headline",
              "headline-box", "orbit", "carousel-spin", "phone-fan", "glass-widgets", "tier-list"}
BELOW_CHIN = {"liquid-glass-graph", "checklist", "todo", "comment-box", "tap", "broll-split"}


def zone(fx):
    return "top" if fx in ABOVE_HEAD else "bottom" if fx in BELOW_CHIN else None

NEG = {"never", "wrong", "mistake", "mistakes", "stop", "don't", "dont", "worst", "fail", "failed", "nahi", "mat", "galat"}
SPOT = {"honestly", "secret", "truth", "seriously", "imagine", "real", "actually"}
LIST_MARK = {"first", "second", "third", "fourth", "fifth", "firstly", "secondly", "thirdly", "step", "number", "next", "finally"}
STOP = {"a", "an", "the", "i", "to", "of", "and", "is", "it", "in", "on", "at", "for", "my", "me", "we", "you", "so", "but",
        "or", "be", "was", "are", "that", "this", "with", "as", "if", "do", "just", "your", "they", "se", "ki", "ka", "ke",
        "hai", "hain", "ko", "aur", "bhi", "toh", "to", "mein", "ne", "ek"}


def resolve_name(text):
    """Effect name mentioned in `text` (longest match first), or None."""
    t = text.lower()
    for name in sorted(NAMES, key=len, reverse=True):
        if re.search(r"\b" + re.escape(name).replace(r"\-", r"[- ]?") + r"\b", t):
            return name
    for alias in sorted(ALIASES, key=len, reverse=True):
        if re.search(r"\b" + re.escape(alias) + r"\b", t):
            return ALIASES[alias]
    return None


def _n(w):
    return re.sub(r"[^\w'%$₹]", "", w.lower())


def _number(ws, k):
    """('197K', label) if word k starts a number worth graphing."""
    t = ws[k]["text"].strip(".,!?")
    if not re.search(r"\d", t):
        return None
    nxt = _n(ws[k + 1]["text"]) if k + 1 < len(ws) else ""
    v = re.sub(r"[^\d.]", "", t)
    if not v or v == ".":
        return None
    num = float(v)
    suf = re.search(r"(k|m|%|l|cr)$", t.lower())
    if suf:
        s = suf.group(1).upper()
    elif nxt in ("k", "thousand", "hazaar"):
        s = "K"
    elif nxt in ("million", "m"):
        s = "M"
    elif nxt in ("percent", "%", "pct"):
        s = "%"
    elif nxt in ("lakh", "lakhs"):
        s = "L"
    elif nxt in ("crore", "crores", "cr"):
        s = "Cr"
    elif num >= 1000:
        num, s = num / 1000, "K"
    elif num < 10 and not t.startswith(("$", "₹")):
        return None                                   # "3 ways" is not a stat
    else:
        s = ""
    pre = "$" if "$" in t else "₹" if "₹" in t else ""
    val = f"{pre}{num:g}{s}"
    label_words = [w["text"] for w in ws[k + 1:k + 5] if _n(w["text"]) not in STOP and not re.search(r"\d", w["text"])]
    label = " ".join(label_words[:2]).strip(".,!?").capitalize() or "Growth"
    return val, label


def candidates(ws, disp, total):
    """Moments that suit an effect: dicts with trigger, priority, timing and the spoken words."""
    out = []

    def add(trigger, pri, start, dur, why, **data):
        out.append({"trigger": trigger, "pri": pri, "start": start, "dur": dur, "why": why, **data})

    if total > 5 and ws:
        first = [k for k, w in enumerate(ws) if w["start"] < 1.6]
        key = max(first, key=lambda k: len(_n(ws[k]["text"])) if _n(ws[k]["text"]) not in STOP else 0, default=None)
        if key is not None:
            add("hook", 5, max(0.05, ws[key]["start"] - 0.1), 1.0, "your hook",
                word=disp[key]["text"].strip(".,!?").upper())
    marks = []
    for k, w in enumerate(ws):
        n = _n(w["text"])
        num = _number(ws, k)
        word = disp[k]["text"].strip(".,!?").upper()
        if num:
            add("number", 3, w["start"], 3.0, f"you said {num[0]}", word=num[0], label=num[1], number=num[0])
        if n in NEG:
            add("negation", 2, w["start"], 1.4, f"you said “{disp[k]['text']}”", word=word)
        if n in SPOT:
            add("spot", 2, w["start"] - 0.2, 2.4, f"you said “{disp[k]['text']}”", word=word)
        if n in LIST_MARK:
            marks.append(k)
        if n == "before" and any(_n(x["text"]) == "after" for x in ws[k + 1:k + 15]):
            add("before-after", 3, w["start"], 2.4, "before / after")
        if w["start"] > total * 0.65:
            if n in ("follow", "subscribe"):
                add("cta-follow", 5, w["start"] - 0.3, 2.0, "your call-to-action", at=w["start"] + 0.6)
            if n == "comment" and k + 1 < len(ws):
                cw = re.sub(r"[^\w ]", "", disp[k + 1]["text"]).upper()
                if cw:
                    add("cta-comment", 5, w["start"] - 0.2, 2.6, f"“comment {cw}”", word=cw)
    if len(marks) >= 3:
        k0, k1 = marks[0], marks[-1]
        if ws[k1]["start"] - ws[k0]["start"] <= 25:
            items = []
            for j, m in enumerate(marks[:4]):
                stop = marks[j + 1] if j + 1 < len(marks) else m + 6
                after = []
                for d in disp[m + 1:min(stop, m + 7)]:
                    if _n(d["text"]) not in STOP:
                        after.append(d["text"].strip(".,!?"))
                    if d["text"][-1:] in ".!?।,":
                        break
                items.append(" ".join(after[:3]).capitalize() or disp[m]["text"])
            ticks = "|".join(f"{ws[m]['start'] + 0.4:.2f}" for m in marks[:4])
            start = ws[k0]["start"] - 0.3
            add("list", 4, start, min(8.0, ws[marks[min(3, len(marks) - 1)]]["start"] - start + 1.6),
                "the steps you listed", items="|".join(items), ticks=ticks)
    sent = []
    for k, w in enumerate(ws):
        sent.append(k)
        if w["text"][-1:] in ".!?।" or k == len(ws) - 1:
            if 1 <= len(sent) <= 5:
                best = max(sent, key=lambda j: len(_n(ws[j]["text"])) if _n(ws[j]["text"]) not in STOP else 0)
                word = disp[best]["text"].strip(".,!?").upper()
                if len(word) >= 3:
                    phrase = " ".join(disp[j]["text"].strip(".,!?") for j in sent)
                    add("emphatic", 1, ws[sent[0]]["start"], 2.0, f"you said “{word.title()}”", word=word, phrase=phrase)
            sent = []
    return out


# Which effects suit each kind of moment (first = default when there's no taste yet).
ALTERNATIVES = {
    "hook": ["lean-in", "locked-on", "slow-push", "dot-reveal", "rabbit-hole"],
    "number": ["liquid-glass-graph", "backdrop-word", "led-sign"],
    "negation": ["reject-stamp", "backdrop-word", "red-collage"],
    "spot": ["spotlight", "backdrop-word", "led-sign", "match-cut"],
    "list": ["todo", "checklist", "apple-toggles"],
    "cta-follow": ["tap"],
    "cta-comment": ["comment-box"],
    "before-after": ["swipe"],
    "emphatic": ["backdrop-word", "spotlight", "led-sign", "match-cut", "headline-box", "red-collage"],
}


def build_opts(fx, m):
    """Effect options from the moment's own words (never invented)."""
    word = m.get("word", "")
    if fx == "liquid-glass-graph":
        return {"text": m.get("label", ""), "from": "0", "to": m["number"]} if m.get("number") else None
    if fx in ("todo", "apple-toggles"):
        return {"text": m["items"], "ticks": m["ticks"]} if m.get("items") else None
    if fx == "checklist":
        return {"text": m["items"]} if m.get("items") else None
    if fx == "tap":
        return {"at": m.get("at")} if m.get("at") else {}
    if fx == "swipe":
        return {"grade": "pop"}
    if fx == "headline-box":
        parts = (m.get("phrase") or word).split()
        if not parts:
            return None
        mid = max(1, len(parts) // 2)
        return {"text": " ".join(parts[:mid]) + ("|" + " ".join(parts[mid:]) if len(parts) > 1 else "")}
    if fx in WORD_TEXT | {"comment-box", "led-sign"}:
        return {"text": word} if word else None
    return {}                                             # camera effects: no text at all


def pick_fx(m, allowed, taste):
    """Best effect for a moment: by taste score when learned, else the default order."""
    options = [fx for fx in ALTERNATIVES.get(m["trigger"], []) if fx in allowed]
    if not options:
        return None, 0.0
    if not taste:
        return options[0], 0.0
    from .taste import score
    scored = sorted(((score(taste, fx, m["trigger"]), -k, fx) for k, fx in enumerate(options)), reverse=True)
    best = scored[0]
    if best[0] < -1.0:
        return None, best[0]                              # you keep removing effects on this kind of moment
    return best[2], best[0]


RULE_FX = {"lean-in", "liquid-glass-graph", "reject-stamp", "spotlight", "todo", "swipe", "tap", "comment-box",
           "backdrop-word"}


def generic_fill(ws, disp, total, gap, pool, busy, avoid, taste=None):
    """Place ticked effects that have no trigger rule on key moments, favourites first."""
    if not pool or not ws:
        return []
    if taste:
        from .taste import score
        pool = sorted(pool, key=lambda fx: -score(taste, fx, "moment"))
        pool = [fx for fx in pool if score(taste, fx, "moment") > -1.0] or pool[:1]
    else:
        pool = sorted(pool)
    out, turn = [], 0
    phrase = []
    phrases = []
    for k, w in enumerate(ws):
        phrase.append(k)
        if w["text"][-1:] in ".!?,।" or k == len(ws) - 1 or (k + 1 < len(ws) and ws[k + 1]["start"] - w["end"] > 0.35):
            phrases.append(phrase)
            phrase = []
    for ph in phrases:
        start = ws[ph[0]]["start"]
        if start < 1.0:
            continue
        fx = pool[turn % len(pool)]
        dur = DEFAULT_DUR.get(fx, 2.6)
        end = min(total - 0.3, start + dur)
        if end - start < 1.0:
            continue
        taken = busy + out
        if any(start < b["end"] + 0.6 and end > b["start"] - 0.6 for b in taken):
            continue
        if any(abs(start - b["start"]) < gap for b in taken if b.get("why") != "you asked"):
            continue
        if any(start < b + d and end > b for b, d in avoid):
            continue
        key = max(ph, key=lambda j: len(_n(ws[j]["text"])) if _n(ws[j]["text"]) not in STOP else 0)
        word = disp[key]["text"].strip(".,!?").upper()
        opts = {}
        if fx in WORD_TEXT:
            if len(word) < 3:
                continue
            opts["text"] = word
            if fx == "red-collage":
                opts["lines"] = "|".join(disp[j]["text"].strip(".,!?") for j in ph[:3] if j != key)
        elif fx in LINES_TEXT:
            text = " ".join(disp[j]["text"] for j in ph).strip(".,!?")
            parts = text.split()
            mid = max(1, len(parts) // 2)
            opts["text"] = " ".join(parts[:mid]) + "|" + " ".join(parts[mid:]) if len(parts) > 3 else text
        out.append({"fx": fx, "start": round(start, 3), "end": round(end, 3), "trigger": "moment",
                    "why": f"key moment “{disp[key]['text']}”"})
        out[-1].update(opts)
        turn += 1
    return out


def trigger_at(ws, disp, total, t):
    """What kind of moment a user-placed effect sits on (for learning taste)."""
    near = [m for m in candidates(ws, disp, total) if abs(m["start"] - t) <= 1.5]
    return max(near, key=lambda m: m["pri"])["trigger"] if near else "moment"


def plan(pdir_cuts, edl, words, style, total, avoid):
    """Final effect list on the output timeline: user-requested ones + auto picks."""
    es = style["effects"]
    script = style["captions"].get("script", "original")
    ws = output_words(edl, words, "original")
    disp = output_words(edl, words, script)
    at_word = {w["i"]: w["start"] for w in ws}
    taste = None
    if es.get("taste"):
        from .taste import has_data, load
        t = load()
        taste = t if has_data(t) else None
    chosen = []

    for u in pdir_cuts.get("effects", []):
        start = at_word.get(u.get("word"), u.get("start"))
        if start is None:
            continue
        dur = min(float(u.get("dur") or DEFAULT_DUR.get(u["fx"], 2.5)), total)
        start = max(0.0, min(start, total - dur - 0.05))   # always play in full, even if placed at the very end
        opts = {k: v for k, v in u.items() if k not in ("fx", "word", "start", "dur", "end")}
        if u["fx"] in NEEDS_TEXT and not opts.get("text"):
            continue                                      # never show a template with empty/made-up text
        if not opts.get("text") and u["fx"] in WORD_TEXT | LINES_TEXT:
            spoken = spoken_text(ws, disp, start, start + dur, u["fx"] in LINES_TEXT)
            if not spoken:
                continue
            opts["text"] = spoken
        if "text" in opts and script == "latin":
            opts["text"] = to_latin(str(opts["text"]))
        chosen.append({"fx": u["fx"], "start": round(max(0.0, start), 3), "end": round(min(total, start + dur), 3),
                       "why": "you asked", "trigger": trigger_at(ws, disp, total, start), **opts})

    blocked = pdir_cuts.get("fx_blocked", [])

    def is_blocked(fx, start):
        return any(b["fx"] == fx and abs(b["t"] - start) < 1.0 for b in blocked)

    if es.get("auto"):
        gap = min(GAP.get(es.get("density", "medium"), 8.0), max(3.0, total / 3))  # short clips still get a few
        allowed = set(es.get("allow") or DEFAULT_AUTO) - NEEDS_TEXT
        excl = (set(NAMES) - allowed) | set(es.get("exclude", []))
        ok = allowed - excl
        moments = sorted(candidates(ws, disp, total), key=lambda m: (-m["pri"], m["start"]))
        auto = []
        for m in moments:
            fx, sc = pick_fx(m, ok, taste)
            if not fx:
                continue
            opts = build_opts(fx, m)
            if opts is None:
                continue
            pri = m["pri"] + max(-2.0, min(2.0, sc)) / 2
            start = max(0.05, m["start"])
            dur = (taste or {}).get("dur", {}).get(fx) or m["dur"]
            end = min(total - 0.3, start + dur)
            if end - start < 0.6 or is_blocked(fx, start):
                continue
            busy = chosen + auto
            if any(start < b["end"] + 0.6 and end > b["start"] - 0.6 for b in busy):
                continue
            if any(start < b + d and end > b for b, d in avoid):
                continue
            if pri < 4 and any(abs(start - b["start"]) < gap for b in auto if b["_pri"] < 4):
                continue
            auto.append({"fx": fx, "start": round(start, 3), "end": round(end, 3), "trigger": m["trigger"],
                         "why": f"{m['why']}" + (" · your taste" if taste and sc > 0.5 else ""), "_pri": pri, **opts})
            if len(auto) >= total / gap + 2:
                break
        auto += [g for g in generic_fill(ws, disp, total, gap, [e for e in ok if e not in RULE_FX | RULE_ONLY],
                                         chosen + auto, avoid, taste) if not is_blocked(g["fx"], g["start"])]
        for a in auto:
            a.pop("_pri", None)
            if "text" in a and script == "latin":
                a["text"] = to_latin(str(a["text"]))
        chosen += auto
    body = " ".join(d["text"] for d in disp).lower()
    for e in chosen:
        if e["fx"] == "match-cut":
            e["body"] = body                              # the "news pages" are filled with your own words
    return sorted(chosen, key=lambda e: e["start"])


def spoken_text(ws, disp, a, b, lines):
    """The words you actually say between a and b (key word, or a short phrase split in two lines)."""
    idx = [k for k, w in enumerate(ws) if a - 0.2 <= w["start"] <= b]
    if not idx:
        return ""
    if not lines:
        k = max(idx, key=lambda j: len(_n(ws[j]["text"])) if _n(ws[j]["text"]) not in STOP else 0)
        return disp[k]["text"].strip(".,!?").upper()
    parts = [disp[k]["text"].strip(".,!?") for k in idx[:8]]
    mid = max(1, len(parts) // 2)
    return " ".join(parts[:mid]) + ("|" + " ".join(parts[mid:]) if len(parts) > 3 else "")


def render_windows(cache, joined, segsig, effects, W, H, total, progress=lambda f, m: None):
    """Run the effects pack on short windows. Returns [(clip, window_start, fx_start, fx_end)]."""
    if not effects:
        return []
    pad = 0.6
    groups = []
    for e in effects:
        a, b = max(0.0, e["start"] - pad), min(total, e["end"] + pad)
        if groups and a <= groups[-1]["b"]:
            groups[-1]["b"] = max(groups[-1]["b"], b)
            groups[-1]["items"].append(e)
        else:
            groups.append({"a": a, "b": b, "items": [e]})
    out = []
    for n, g in enumerate(groups):
        progress(n / len(groups), f"Adding effects {n + 1}/{len(groups)}")
        local = []
        for e in g["items"]:
            d = {k: v for k, v in e.items() if k != "why"}
            d["start"], d["end"] = round(e["start"] - g["a"], 3), round(e["end"] - g["a"], 3)
            if "ticks" in d:
                d["ticks"] = "|".join(f"{float(t) - g['a']:.2f}" for t in str(d["ticks"]).split("|"))
            if "at" in d:
                d["at"] = round(float(d["at"]) - g["a"], 3)
            local.append(d)
        key = hashlib.sha1(json.dumps([segsig, g["a"], g["b"], local, W, H], sort_keys=True).encode()).hexdigest()[:16]
        clip = cache / f"fx_{key}.mp4"
        if not clip.exists():
            sub = cache / f"fxin_{key}.mp4"
            run(["ffmpeg", "-y", "-v", "error", "-ss", f"{g['a']:.3f}", "-i", joined, "-t", f"{g['b'] - g['a']:.3f}",
                 "-an", "-c:v", "libx264", "-preset", "veryfast", "-crf", "14", "-pix_fmt", "yuv420p", sub])
            plan_file = cache / f"fxplan_{key}.json"
            plan_file.write_text(json.dumps({"effects": local}), encoding="utf-8")
            r = subprocess.run([sys.executable, str(FX_SCRIPT), "--plan", str(plan_file), str(sub), str(clip),
                                "--width", str(min(W, H))], capture_output=True, text=True, stdin=subprocess.DEVNULL)
            sub.unlink(missing_ok=True)
            if r.returncode != 0 or not clip.exists():
                tail = "\n".join((r.stderr or r.stdout).strip().splitlines()[-8:])
                raise RuntimeError(f"Effect render failed ({', '.join(e['fx'] for e in local)}):\n{tail}")
        out.append((clip, g["a"], min(e["start"] for e in g["items"]), max(e["end"] for e in g["items"])))
    for old in cache.glob("fx_*.mp4"):
        if old not in [o[0] for o in out]:
            old.unlink()
    return out
