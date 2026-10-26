"""Editorial decisions without an LLM: retakes, false starts, self-talk, emphasis words.

Every removal carries a reason so the dashboard can show it and the user can undo it.
"""
import re

from .common import is_filler, norm_word, read_json, write_json

LOOKAHEAD = 20.0          # seconds to look ahead for a repeated take
START_GAP = 0.45          # a pause this long starts a new "attempt"
STUTTER_WORDS = {"i", "the", "a", "an", "and", "so", "we", "you", "it", "this", "to", "but",
                 "my", "in", "of", "is", "that's", "it's", "i'm", "if", "what", "when"}
SELF_TALK = [
    r"\b(let me|lemme) (start|say|do|try) (that |this |it )?(again|over)\b",
    r"\b(start|starting) (again|over)\b",
    r"\bone more time\b",
    r"\bis (this|it) recording\b",
    r"\b(cut|scratch) that\b",
    r"\bwhere was i\b",
    r"\bhold on,? let me\b",
    r"\bi messed (that|it) up\b",
]
NUMBER_WORDS = {"one", "two", "three", "four", "five", "six", "seven", "eight", "nine", "ten",
                "hundred", "thousand", "million", "billion", "percent", "first", "second", "third",
                "double", "triple", "half", "zero"}
LAUGHS = {"laughter", "laughs", "laughing", "laugh", "chuckles"}


def content_tokens(words):
    return [w for w in words if w["type"] == "word" and not is_filler(w) and norm_word(w["text"])]


def attempt_starts(toks):
    starts = []
    for k, w in enumerate(toks):
        if k == 0:
            starts.append(k)
            continue
        prev = toks[k - 1]
        if prev["text"][-1:] in ".?!,;:।॥" or w["start"] - prev["end"] >= START_GAP:
            starts.append(k)
    return starts


def find_retakes(words):
    """[(first_word_i, last_word_i, reason)] for abandoned or repeated takes."""
    toks = content_tokens(words)
    norm = [norm_word(t["text"]) for t in toks]
    starts = attempt_starts(toks)
    out, covered_until = [], -1
    for n, s in enumerate(starts):
        if s <= covered_until or s + 2 >= len(toks):
            continue
        key = norm[s:s + 3]
        for r in starts[n + 1:]:
            if toks[r]["start"] - toks[s]["start"] > LOOKAHEAD:
                break
            if norm[r:r + 3] != key:
                continue
            attempt = norm[s:r]
            redo = norm[r:r + len(attempt)]
            same = sum(1 for a, b in zip(attempt, redo) if a == b) / max(1, len(attempt))
            finished = any(t["text"][-1:] in ".?!।॥" for t in toks[s:r - 1])
            # A finished, different sentence that merely starts the same way is not a retake.
            if finished and same < 0.6 and r - s > 12:
                break
            out.append((toks[s]["i"], toks[r]["i"] - 1, "retake"))
            covered_until = r - 1
            break

    # Stutters: "I I think", "the the"
    for a, b in zip(toks, toks[1:]):
        na, nb = norm_word(a["text"]), norm_word(b["text"])
        if na == nb and na in STUTTER_WORDS and b["start"] - a["end"] < 0.6:
            if not any(x <= a["i"] <= y for x, y, _ in out):
                out.append((a["i"], b["i"] - 1, "stutter"))
    return out


def find_self_talk(words):
    """Whole sentences containing recording chatter ("let me start again", "is this recording")."""
    out, sent = [], []
    for w in words:
        sent.append(w)
        if w["text"][-1:] in ".?!।॥" or w is words[-1]:
            text = " ".join(norm_word(x["text"]) for x in sent if x["type"] == "word")
            if any(re.search(p, text) for p in SELF_TALK):
                out.append((sent[0]["i"], sent[-1]["i"], "self-talk"))
            sent = []
    return out


def auto_emphasis(words):
    out = []
    for w in words:
        if w["type"] != "word":
            continue
        t = w["text"]
        n = norm_word(t)
        if (re.search(r"\d", t) or n in NUMBER_WORDS or t[:1] in "$₹€£"
                or t.endswith("!") or (len(n) > 1 and t.strip(".,!?").isupper())):
            out.append(w["i"])
    return out


def detect(pdir):
    """Run automatic detection and store it in cuts.json, preserving the user's own edits."""
    words = read_json(pdir / "transcript.json")["words"]
    path = pdir / "cuts.json"
    cuts = read_json(path) if path.exists() else {}
    cuts["auto"] = sorted(find_retakes(words) + find_self_talk(words))
    cuts["auto_emphasis"] = auto_emphasis(words)
    for k in ("user_remove", "user_keep", "emphasis_add", "emphasis_remove"):
        cuts.setdefault(k, [])
    write_json(path, cuts)
    return cuts


def load_cuts(pdir):
    path = pdir / "cuts.json"
    return read_json(path) if path.exists() else {
        "auto": [], "auto_emphasis": [], "user_remove": [], "user_keep": [],
        "emphasis_add": [], "emphasis_remove": []}


def word_states(words, cuts, cut_style):
    """Per word: None if kept, else the reason it is removed."""
    user_keep, user_remove = set(cuts.get("user_keep", [])), set(cuts.get("user_remove", []))
    auto = {}
    if cut_style.get("remove_retakes", True):
        for a, b, reason in cuts.get("auto", []):
            for i in range(a, b + 1):
                auto.setdefault(i, reason)
    out = []
    for w in words:
        i = w["i"]
        if i in user_keep:
            out.append(None)
        elif i in user_remove:
            out.append("you")
        elif i in auto:
            out.append(auto[i])
        elif w["type"] == "event":
            label = w["text"].strip("()[] ").lower()
            out.append("sound" if cut_style["remove_events"] and label not in LAUGHS else None)
        elif cut_style["remove_fillers"] and is_filler(w):
            out.append("filler")
        else:
            out.append(None)
    return out


def emphasis_set(cuts, caption_style):
    s = set(cuts.get("auto_emphasis", [])) if caption_style.get("auto_emphasis", True) else set()
    s |= set(cuts.get("emphasis_add", []))
    s -= set(cuts.get("emphasis_remove", []))
    return s
