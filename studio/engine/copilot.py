"""Rule-based editing copilot: plain-English (and some Hinglish) requests → edits.

No AI model: a request is split into instructions, each instruction is matched against
editing intents, and every change is reported back. Unknown instructions are reported too.
"""
import difflib
import re

from .common import deep_merge, norm_word, project_style, read_json, write_json
from .effects import ALIASES as FX_ALIASES, DEFAULT_DUR, NAMES as FX_NAMES
from .media import broll_clips, music_file
from .textrender import FONT_CHOICES, _user_font, font_label
from .decisions import load_cuts

COLORS = {
    "white": "#FFFFFF", "yellow": "#FFD43B", "gold": "#FAB005", "orange": "#FF6B35",
    "red": "#FA5252", "pink": "#F06595", "purple": "#9775FA", "violet": "#7950F2",
    "blue": "#4DABF7", "cyan": "#3BC9DB", "teal": "#20C997", "green": "#51CF66",
    "lime": "#94D82D", "black": "#000000", "grey": "#ADB5BD", "gray": "#ADB5BD",
}
CUT_VERBS = r"(?:cut|remove|delete|drop|trim|get rid of|take out|hatao|hata do|nikalo|nikal do)"
KEEP_VERBS = r"(?:keep|restore|bring back|put back|don'?t cut|do not cut|undo cut|wapas)"
PHRASE_TRIGGER = (r"(?:where i (?:say|said|talk about|talked about|mention|mentioned|speak about|am talking about)"
                  r"|(?:the )?part (?:where|with|about|that says)|(?:the )?(?:sentence|line|bit|section) (?:with|about|where|that says)"
                  r"|mentioning|talking about|about|that says|saying|wala part|wali line)")
TIME = r"(\d{1,2}:\d{2}(?:\.\d+)?|\d+(?:\.\d+)?)\s*(?:s|sec|secs|seconds)?"
SUBJECTS = [("captions", r"\b(captions?|subtitles?|subs|text|fonts?|typeface|words? on screen|highlight)\b"),
            ("zoom", r"\bzoom"), ("pacing", r"\b(pauses?|pacing|gaps?|silences?)\b"),
            ("music", r"\b(music|song|bgm|soundtrack)\b"), ("b-roll", r"\bb[- ]?rolls?\b|\bbroll\b"),
            ("speed", r"\bspeed\b")]
# A quoted phrase: opening quote not preceded by a letter, so apostrophes in "don't" don't count.
QUOTED = r"(?<!\w)[\"“‘']([^\"”’'\n]{1,80})[\"”’'](?!\w)"

HELP = """Things you can ask (combine them with "and" or commas):
• Pacing — "tighter pauses", "more breathing room", "max pause 0.5s", "keep the ums"
• Cuts — "cut the part where I say 'January'", "remove 0:12 to 0:20", "cut the first 3 seconds",
  "bring back the sentence about payouts", "restore everything", "undo"
• Captions — "captions in English", "use Helvetica font", "bigger captions", "captions at the top",
  "one word at a time", "yellow highlight",
  "white text", "all caps", "no captions", "highlight the word Fundnext"
• Zoom — "slow zoom", "punch in on cuts", "more zoom", "no zoom", "zoom 12%"
• Format & look — "make it vertical", "16:9", "boost colours", "remove background noise"
• Vibe presets — "make it more energetic", "calm and relaxed", "clean professional look"
• Reel look — "make it like the reel" (kinetic captions, tight pacing, 1.08× speed, cinematic grade, B-roll, music)
• Kinetic captions — "kinetic captions" / "classic captions"
• Speed — "speed it up", "1.1x", "10% faster", "normal speed"
• Colour — "cinematic look", "warm", "moody", "vivid", "natural colours"
• Music — "add music", "music quieter", "music -18 dB", "no music" (upload a track in Media)
• B-roll — "add b-roll", "more b-roll", "b-roll every 3 seconds", "b-roll when I talk about trading", "no b-roll"
• Effects (42, face-tracking) — "spotlight when I say honestly", "orbit from 3 to 6 seconds",
  "backdrop word with the word PAYOUTS at the start", "glass graph to 197K when I say followers",
  "comment box typing GUIDE at the end", "no stamp", "more effects", "list effects",
  "remove old effects" / "remove all effects" (this video only), "remove the spotlight", "auto effects on"
• Output — "render final", "save this as my default style" (or add "for all my videos")
Hinglish works for common ones: "captions bade karo", "January wala part hatao", "pauses kam karo"."""


def _clamp(v, lo, hi):
    return max(lo, min(hi, v))


def parse_time(s):
    s = s.strip()
    if ":" in s:
        m, sec = s.split(":", 1)
        return int(m) * 60 + float(sec)
    return float(s)


def fmt(t):
    return f"{int(t // 60)}:{t % 60:04.1f}"


class Copilot:
    def __init__(self, pdir):
        self.pdir = pdir
        self.over = read_json(pdir / "settings.json") if (pdir / "settings.json").exists() else {}
        self.style = project_style(pdir)
        self.cuts = load_cuts(pdir)
        self.has_tx = (pdir / "transcript.json").exists()
        self.words = read_json(pdir / "transcript.json")["words"] if self.has_tx else []
        self.edl = read_json(pdir / "edl.json") if (pdir / "edl.json").exists() else None
        self.done, self.warn, self.unknown = [], [], []
        self.changed = False
        self.want = None            # "final" | "preview" | None
        self.no_render = False
        self.save_default = False
        self.undo = False
        self.last_words_action = None
        self.quiet = False

    # ---------- helpers ----------
    def get(self, path):
        cur = self.style
        for k in path.split("."):
            cur = cur[k]
        return cur

    def set(self, path, value, label, show=None):
        old = self.get(path)
        if old == value:
            if not self.quiet:
                self.done.append(f"{label} is already {show(value) if show else value}")
            return
        keys = path.split(".")
        patch = value
        for k in reversed(keys):
            patch = {k: patch}
        self.over = deep_merge(self.over, patch)
        self.style = deep_merge(self.style, patch)
        self.changed = True
        s = show or (lambda v: v)
        self.done.append(f"{label}: {s(old)} → {s(value)}")

    def sentences(self, clauses=False):
        """Word-index groups split at sentence ends (and, for clauses, commas and pauses)."""
        ends = ".?!।॥,;:" if clauses else ".?!।॥"
        out, cur = [], []
        for k, w in enumerate(self.words):
            cur.append(w["i"])
            nxt = self.words[k + 1] if k + 1 < len(self.words) else None
            if w["text"][-1:] in ends or (clauses and nxt and nxt["start"] - w["end"] >= 0.45):
                out.append(cur)
                cur = []
        if cur:
            out.append(cur)
        return out

    def find_phrase(self, phrase, word_level):
        """Word indices matching `phrase`: exact sequence for words, best sentences otherwise."""
        q = [t for t in (norm_word(x) for x in phrase.split()) if t]
        if not q:
            return []
        norm = [norm_word(w["text"]) for w in self.words]
        hits = []
        for k in range(len(norm)):
            if norm[k:k + len(q)] == q:
                hits.extend(range(k, k + len(q)))
        if word_level:
            return hits
        stop = {"the", "a", "an", "i", "to", "of", "and", "is", "it", "that", "this", "my", "me"}
        key = [t for t in q if t not in stop] or q
        chosen = []
        for sent in self.sentences(clauses=True):
            toks = {norm[i] for i in sent}
            if hits and any(i in sent for i in hits):
                chosen.append(sent)
            elif not hits and sum(1 for t in key if t in toks) / len(key) >= 0.75:
                chosen.append(sent)
        return [i for s in chosen for i in s]

    def out_range_words(self, t0, t1):
        """Word indices whose position on the preview timeline falls in [t0, t1]."""
        if not self.edl:
            return []
        ids, t, k = [], 0.0, self.edl.get("speed", 1.0)
        for seg in self.edl["segments"]:
            for i in seg["words"]:
                ot = t + (max(self.words[i]["start"], seg["start"]) - seg["start"]) / k
                if t0 <= ot <= t1:
                    ids.append(i)
            t += (seg["end"] - seg["start"]) / k
        return ids

    def apply_words(self, ids, action, what):
        if not ids:
            self.warn.append(f"Couldn't find {what} in the transcript")
            return
        rm, keep = set(self.cuts["user_remove"]), set(self.cuts["user_keep"])
        if action == "cut":
            rm |= set(ids)
            keep -= set(ids)
        else:
            keep |= set(ids)
            rm -= set(ids)
        self.cuts["user_remove"], self.cuts["user_keep"] = sorted(rm), sorted(keep)
        self.changed = True
        self.last_words_action = action
        preview = " ".join(self.words[i]["text"] for i in ids[:10]) + (" …" if len(ids) > 10 else "")
        verb = "Cut" if action == "cut" else "Kept"
        self.done.append(f"{verb} {len(ids)} words ({what}): “{preview}”")
        if action == "cut" and len(ids) > 15:
            self.warn.append("That's a long stretch — say “undo” if it's too much, or put the exact words "
                             "in quotes (e.g. cut \"word1 word2\") to cut only those")

    # ---------- content edits (cuts / restores / emphasis) ----------
    def content(self, c):
        if re.fullmatch(r"\s*(undo|undo that|undo last( change)?|go back)\s*", c):
            self.undo = True
            return True
        if re.search(r"\b(restore|reset|undo|bring back|clear)\b.*\b(all|everything|every)\b", c) and \
                not re.search(r"\b(caption|zoom|style|setting)", c):
            n = len(self.cuts["user_remove"]) + len(self.cuts["user_keep"])
            self.cuts["user_remove"], self.cuts["user_keep"] = [], []
            self.changed = True
            self.done.append(f"Reset all your manual cuts/keeps ({n} words) — automatic cuts still apply")
            return True
        if not self.has_tx and re.search(CUT_VERBS + "|" + KEEP_VERBS, c) and \
                re.search(PHRASE_TRIGGER + r"|\d", c):
            self.warn.append("The transcript isn't ready yet — wait for the first preview, then ask again")
            return True

        cut_m = re.search(r"\b" + CUT_VERBS + r"\b", c)
        keep_m = re.search(r"\b" + KEEP_VERBS, c)
        action = "keep" if keep_m and (not cut_m or keep_m.start() <= cut_m.start()) else "cut"
        has_verb = re.search(r"\b(" + CUT_VERBS + "|" + KEEP_VERBS + r")\b", c)

        m = re.search(r"(?:from|between)?\s*" + TIME + r"\s*(?:to|-|–|until|till|and)\s*" + TIME, c)
        if has_verb and m:
            t0, t1 = sorted((parse_time(m.group(1)), parse_time(m.group(2))))
            self.apply_words(self.out_range_words(t0, t1), action, f"{fmt(t0)}–{fmt(t1)} of the preview")
            return True
        m = re.search(r"\b(first|last|starting|opening|ending|final)\s+" + TIME, c)
        if has_verb and m and self.edl:
            n = parse_time(m.group(2))
            total = self.edl["stats"]["output_duration"]
            rng = (0, n) if m.group(1) in ("first", "starting", "opening") else (total - n, total)
            self.apply_words(self.out_range_words(*rng), action, f"{m.group(1)} {n:g}s")
            return True
        m = re.search(r"\b(first|last|opening|closing|final)\s+(sentence|line)\b", c)
        if has_verb and m:
            sents = [s for s in self.sentences() if s]
            if sents:
                s = sents[0] if m.group(1) in ("first", "opening") else sents[-1]
                self.apply_words(s, action, f"{m.group(1)} sentence")
            return True

        quoted = re.search(QUOTED, c)
        trig = re.search(PHRASE_TRIGGER + r"\s+(.+)$", c)
        hing = re.search(r"^(.+?)\s+(?:wala|wali|wale)\s+(?:part|line|hissa)", c)
        word_level = bool(re.search(r"\bthe words?\b", c))
        if has_verb and (quoted or trig or hing or word_level):
            if quoted:
                phrase = quoted.group(1)
            elif hing:
                phrase = re.sub(r"^\s*" + CUT_VERBS + r"\s+", "", hing.group(1))
            elif trig:
                phrase = trig.group(1)
            else:
                phrase = re.sub(r"^.*?\bthe words?\s+", "", c)
            phrase = re.sub(r"\b(" + CUT_VERBS + r")\b.*$", "", phrase).strip(" .") or phrase
            phrase = re.sub(r"^(?:where\s+)?i\s+(?:say|said|talk about|talked about|mention|mentioned|"
                            r"speak about|am talking about)\s+", "", phrase).strip() or phrase
            self.apply_words(self.find_phrase(phrase, word_level), action,
                             f"{'word' if word_level else 'part'} “{phrase}”")
            return True

        m = re.search(r"\b(highlight|emphasi[sz]e|bold|stress)\s+(?:the\s+words?\s+)?[\"“']?([^\"”']+?)[\"”']?\s*$", c)
        if m and not re.search(r"\b(colou?r|captions?|numbers?)\b", m.group(2)) and \
                not any(k in m.group(2).split() for k in COLORS):
            ids = self.find_phrase(m.group(2), word_level=True)
            if ids:
                self.cuts["emphasis_add"] = sorted(set(self.cuts.get("emphasis_add", [])) | set(ids))
                self.cuts["emphasis_remove"] = sorted(set(self.cuts.get("emphasis_remove", [])) - set(ids))
                self.changed = True
                self.done.append(f"Highlighted “{m.group(2)}” in captions ({len(ids)} words)")
            else:
                self.warn.append(f"Couldn't find “{m.group(2)}” to highlight")
            return True
        return False

    @staticmethod
    def colour_targets(c):
        """Pair each colour in an instruction with the nearest target word.

        "captions yellow highlight green" → text yellow, highlight green.
        """
        toks = re.findall(r"#[0-9a-f]{6}|[\w']+", c)
        kinds = {}
        for j, t in enumerate(toks):
            if re.fullmatch(r"highlight|active|current|spoken|speaking", t):
                kinds[j] = "highlight"
            elif re.fullmatch(r"emphasis|keywords?|important|numbers?", t):
                kinds[j] = "emphasis"
            elif re.fullmatch(r"text|font|captions?|subtitles?|words", t):
                kinds[j] = kinds.get(j - 1) if kinds.get(j - 1) in ("emphasis", "highlight") else "color"
        cols = [(j, t.upper() if t.startswith("#") else COLORS[t]) for j, t in enumerate(toks)
                if t.startswith("#") or t in COLORS]
        assigned, used = {}, set()
        # "highlight green" (target just before) beats "green highlight" (target just after).
        for off in (-1, 1, -2, 2):
            for j, value in cols:
                k = j + off
                if j not in assigned and k in kinds and kinds[k] not in used:
                    assigned[j] = kinds[k]
                    used.add(kinds[k])
        out = []
        for j, value in cols:
            target = assigned.get(j) or ("color" if "color" not in used else None)
            if target:
                used.add(target)
                out.append((target, value))
        return out

    # ---------- effects (the 42-effect pack) ----------
    AMBIGUOUS_FX = {"moody", "noise", "tap", "timeline", "swipe"}
    # Aliases that only mean an effect when the user also says "effect" (they're common words otherwise).
    VAGUE_ALIASES = {"follow", "comment", "cover", "headline", "freeze", "keys", "receipt", "bill", "zoom in", "punch in",
                     "punch-in", "push in", "counter", "arrow", "tunnel", "doodles", "graph", "widgets", "toggles", "led"}

    def effect_name(self, c):
        has_word = re.search(r"\b(effects?|fx|animation|sticker)\b", c)
        for name in sorted(FX_NAMES, key=len, reverse=True):
            if re.search(r"\b" + re.escape(name).replace(r"\-", r"[- ]?") + r"\b", c):
                if name in self.AMBIGUOUS_FX and not has_word and not re.search(r"\b(add|use|put|do|give|show)\b", c):
                    continue
                return name
        for alias in sorted(FX_ALIASES, key=len, reverse=True):
            if (has_word or alias not in self.VAGUE_ALIASES) and re.search(r"\b" + re.escape(alias) + r"\b", c):
                return FX_ALIASES[alias]
        return None

    def effects(self, c):
        es = self.style["effects"]
        if re.search(r"\b(list|which|what|show)\b.*\beffects\b|\beffects? (list|names)\b", c):
            self.done.append("Effects I can add (say e.g. “orbit from 3 to 6 seconds” or “spotlight when I say honestly”):\n"
                             + ", ".join(FX_NAMES))
            return True
        name = self.effect_name(c)
        general = re.search(r"\b(effects|fx)\b", c) and not name
        neg = re.search(r"^\s*(no|without|stop|never|don'?t)\b|\b(remove|delete|disable|turn off|get rid of|"
                        r"don'?t (want|use|add|put)|no more)\b", c)
        if general:
            if neg or re.search(r"\b(clear|reset)\b", c):
                self.set("effects.auto", False, "Auto effects on this video")
                if re.search(r"\b(all|any|every|old|previous|existing|clear|reset)\b", c) or neg:
                    if self.cuts.get("effects") or self.cuts.get("fx_blocked"):
                        self.cuts["effects"], self.cuts["fx_blocked"] = [], []
                        self.changed = True
                    self.done.append("Removed all effects from this video (say “auto effects on” to bring them back)")
            elif re.search(r"\b(more|lots|extra|frequent)\b", c):
                order = ["low", "medium", "high"]
                self.set("effects.auto", True, "Auto effects")
                self.set("effects.density", order[min(2, order.index(es.get("density", "medium")) + 1)], "Effect amount")
            elif re.search(r"\b(fewer|less|subtle|minimal)\b", c):
                order = ["low", "medium", "high"]
                self.set("effects.density", order[max(0, order.index(es.get("density", "medium")) - 1)], "Effect amount")
            else:
                self.set("effects.auto", True, "Auto effects")
            return True
        if not name:
            return False
        if neg:
            before = len(self.cuts.get("effects", []))
            self.cuts["effects"] = [e for e in self.cuts.get("effects", []) if e["fx"] != name]
            prev = read_json(self.pdir / "state.json").get("outputs", {}).get("preview", {}).get("effects", []) \
                if (self.pdir / "state.json").exists() else []
            for e in prev:                                # block the auto picks of it in this video too
                if e["fx"] == name and e.get("why") != "you asked":
                    self.cuts.setdefault("fx_blocked", []).append({"fx": name, "t": round(e["start"], 2)})
                    self.changed = True
            allow = [n for n in es.get("allow", []) if n != name]
            if allow != es.get("allow", []):
                self.set("effects.allow", allow, "Auto effects")
                self.done[-1] = f"Auto effects won't use {name} any more"
            if before != len(self.cuts["effects"]):
                self.changed = True
                self.done.append(f"Removed your {name} effect")
            return True
        return self.add_effect(name, c)

    def add_effect(self, name, c):
        total = self.edl["stats"]["output_duration"] if self.edl else None
        dur = DEFAULT_DUR.get(name, 2.5)
        entry = {"fx": name}
        opts_from_to = None
        if name == "liquid-glass-graph":
            m = re.search(r"\bto\s+([$₹]?[\d.,]+\s*(?:k|m|%|lakh|l|cr|crore)?)", c)
            if m and re.search(r"[kKmM%lr]|\d{3,}", m.group(1)):
                opts_from_to = m.group(1).replace(" ", "").upper()
                f = re.search(r"\bfrom\s+([$₹]?[\d.,]+\s*[km%]?)\s+to", c)
                entry["from"], entry["to"] = (f.group(1).upper() if f else "0"), opts_from_to
        txt = re.search(r"(?:with the words?|the word|with|text|typing|saying|that says|title|reading)\s+[\"“']?"
                        r"([^\"”']+?)[\"”']?(?:\s+(?:behind me|behind|on screen|above|below|when|from|at|for)\b|$)", c)
        when = re.search(r"\b(?:when|where|as) i (?:say|said|mention|talk about)\s+[\"“']?([^\"”',]+?)[\"”']?"
                         r"(?:\s+(?:with|and|for|from)\b|$)", c)
        rng = None if opts_from_to else re.search(
            r"(?:from\s+)?(\d+(?:\.\d+)?)\s*(?:s|sec|secs|seconds)?\s*(?:to|-|–|till|until)\s*(\d+(?:\.\d+)?)\s*(?:s|sec|secs|seconds)?", c)
        first = re.search(r"\bfirst\s+(\d+(?:\.\d+)?)\s*(?:s|sec|seconds)", c)
        at = re.search(r"\bat\s+(\d+(?:\.\d+)?)\s*(?:s|sec|seconds)\b", c)
        where = ""
        if when and self.has_tx:
            ids = self.find_phrase(when.group(1), word_level=True)
            if not ids:
                self.warn.append(f"Couldn't find “{when.group(1)}” — the {name} effect wasn't added")
                return True
            entry["word"] = ids[0]
            where = f"when you say “{when.group(1)}”"
        elif rng:
            a, b = sorted((float(rng.group(1)), float(rng.group(2))))
            entry["start"], dur = a, max(0.6, b - a)
            where = f"from {a:g}s to {b:g}s"
        elif first:
            entry["start"], dur = 0.1, float(first.group(1))
            where = f"for the first {first.group(1)}s"
        elif at:
            entry["start"] = float(at.group(1))
            where = f"at {at.group(1)}s"
        elif re.search(r"\b(end|ending|outro|last)\b", c) and total:
            entry["start"] = max(0.0, total - dur - 0.4)
            where = "at the end"
        elif re.search(r"\b(start|beginning|intro|hook|opening)\b", c):
            entry["start"] = 0.15
            where = "at the start"
        elif total:
            entry["start"] = round(total * 0.45, 2)
            where = f"at {entry['start']:g}s (say “when I say …” or “from 3 to 6 seconds” to move it)"
        else:
            self.warn.append("The video isn't processed yet — try again after the first preview")
            return True
        if txt and name not in ("liquid-glass-graph",) or (txt and name == "liquid-glass-graph" and not opts_from_to):
            t = txt.group(1).strip(" .")
            if t and not re.fullmatch(r"\d+(\.\d+)?\s*(s|sec|seconds)?", t):
                entry["text"] = t.upper() if name in ("backdrop-word", "reject-stamp", "spotlight", "comment-box") else t
        if name == "tap":
            entry["at"] = None                      # resolved at render: 0.6s after it appears
        entry["dur"] = round(dur, 2)
        entry = {k: v for k, v in entry.items() if v is not None}
        self.cuts.setdefault("effects", []).append(entry)
        self.changed = True
        extra = f" with “{entry['text']}”" if entry.get("text") else (f" counting to {entry['to']}" if entry.get("to") else "")
        self.done.append(f"Added {name}{extra} {where}")
        return True

    # ---------- settings ----------
    def settings(self, c):
        hit = False
        S = lambda v: f"{v:.2f}s"
        P = lambda v: f"{round(v * 100)}%"

        if re.search(r"\b(help|what can you do|commands|examples)\b", c):
            self.done.append(HELP)
            return True

        # the "cinematic reel" look (kinetic captions, tight pacing, grade, B-roll, music)
        if re.search(r"\b(like|same as|copy|match)\b.*\b(reel|video|that|this|syed|insta\w*)\b|\breel (style|look|edit)\b|"
                     r"\b(cinematic|aesthetic|instagram|insta) (reel|edit|style)\b|\b(instagram|insta|aesthetic) look\b|\bfx ?syed\b", c):
            self.done.append("Cinematic reel look:")
            self.quiet = True
            self.set("captions.style", "kinetic", "Caption style")
            self.set("captions.enabled", True, "Burned-in captions")
            self.set("captions.words", 5, "Words per phrase")
            self.set("captions.uppercase", False, "UPPERCASE captions")
            self.set("captions.color", "#FFFFFF", "Caption text colour")
            self.set("captions.emphasis", "#A5D8FF", "Accent colour")
            self.set("cut.max_pause", min(self.get("cut.max_pause"), 0.18), "Longest pause", S)
            self.set("cut.pad_after", min(self.get("cut.pad_after"), 0.06), "Breath after sentences", S)
            self.set("cut.pad_before", min(self.get("cut.pad_before"), 0.05), "Breath before sentences", S)
            self.set("speed", 1.08, "Speed", lambda v: f"{v:g}×")
            self.set("zoom.mode", "punch", "Zoom")
            self.set("zoom.amount", 0.06, "Zoom amount", P)
            self.set("look.grade", "cinematic", "Colour grade")
            self.set("audio.music", True, "Background music")
            self.set("broll.enabled", True, "B-roll")
            missing = [x for x, ok in (("B-roll clips", broll_clips(self.pdir)), ("a music track", music_file(self.pdir))) if not ok]
            if missing:
                self.warn.append(f"For the full look, upload {' and '.join(missing)} in the Media tab — the reel cuts to "
                                 "B-roll every few seconds and has quiet music under the voice")
            self.quiet = False
            if self.done[-1] == "Cinematic reel look:":
                self.done[-1] = "Already in the cinematic reel look"
            return True

        # vibe presets
        vibe = re.search(r"\b(exciting|energetic|energy|hype|dynamic|engaging|punchy|viral|fast)\b|"
                         r"\b(calm|chill|relaxed|soft|slow paced|gentle)\b|\b(professional|clean|corporate|minimal|simple)\b", c)
        if vibe and not re.search(r"\b(captions?|zoom|pauses?)\b", c):
            if vibe.group(1):
                self.done.append("Energetic style:")
                self.set("cut.max_pause", min(self.get("cut.max_pause"), 0.22), "Longest pause", S)
                self.set("cut.pad_after", min(self.get("cut.pad_after"), 0.1), "Breath after sentences", S)
                self.set("zoom.mode", "punch", "Zoom")
                self.set("zoom.amount", 0.12, "Zoom amount", P)
                self.set("captions.words", 2, "Words on screen")
                self.set("captions.uppercase", True, "UPPERCASE captions")
            elif vibe.group(2):
                self.done.append("Calm style:")
                self.set("cut.max_pause", max(self.get("cut.max_pause"), 0.55), "Longest pause", S)
                self.set("cut.pad_after", max(self.get("cut.pad_after"), 0.22), "Breath after sentences", S)
                self.set("zoom.mode", "push", "Zoom")
                self.set("zoom.amount", 0.05, "Zoom amount", P)
                self.set("captions.words", 4, "Words on screen")
                self.set("captions.uppercase", False, "UPPERCASE captions")
            else:
                self.done.append("Clean professional style:")
                self.set("cut.max_pause", 0.4, "Longest pause", S)
                self.set("zoom.mode", "push", "Zoom")
                self.set("zoom.amount", 0.04, "Zoom amount", P)
                self.set("captions.words", 5, "Words on screen")
                self.set("captions.uppercase", False, "UPPERCASE captions")
                self.set("captions.highlight", "#FFFFFF", "Spoken-word colour")
            hit = True

        # format
        if re.search(r"\b(vertical|9[:x/ ]16|portrait|reels?|shorts|tiktok)\b", c):
            self.set("format", "9:16", "Format")
            hit = True
        elif re.search(r"\b(horizontal|16[:x/ ]9|landscape|widescreen)\b", c) or \
                re.search(r"\b(format|make it|for)\b.*\byoutube\b", c):
            self.set("format", "16:9", "Format")
            hit = True
        elif re.search(r"\b(original|same as (the )?recording)\b.*\b(format|size|ratio)\b|\boriginal format\b", c):
            self.set("format", "original", "Format")
            hit = True

        # noise first, so "remove background noise" isn't read as "remove coughs"
        denoise = re.search(r"\b(background noise|hiss|hum|denoise|noise reduction|reduce noise|clean (up )?(the )?audio)\b", c)
        if denoise:
            on = not re.search(r"\b(no|don'?t|disable|turn off|without)\b", c)
            self.set("audio.denoise", on, "Noise reduction")
            hit = True

        # fillers / coughs / retakes
        keep_v = re.search(r"\b(keep|don'?t (remove|cut)|leave|stop removing)\b", c)
        if re.search(r"\b(ums?|uhs?|umms?|fillers?|filler words)\b", c):
            self.set("cut.remove_fillers", not keep_v, "Remove um/uh")
            hit = True
        if not denoise and re.search(r"\b(coughs?|throat|noises?|sounds?|breaths?)\b", c):
            self.set("cut.remove_events", not keep_v, "Remove coughs & noises")
            hit = True
        if re.search(r"\b(retakes?|false starts?|repeats?|mistakes?|stutters?)\b", c):
            self.set("cut.remove_retakes", not keep_v, "Remove retakes & false starts")
            hit = True

        # pacing
        cur = self.get("cut.max_pause")
        m = re.search(r"\b(?:max(?:imum)?|longest)?\s*pauses?\s*(?:to|=|of|at)?\s*(\d+(?:\.\d+)?)\s*(?:s|sec|seconds)\b", c)
        if m:
            self.set("cut.max_pause", _clamp(float(m.group(1)), 0.1, 2.0), "Longest pause", S)
            hit = True
        elif not re.search(r"speed|%\s*faster|\dx\b", c) and re.search(r"\b(tighter|tight|faster|snappier|quicker|punchier|fast[- ]paced|less (pauses?|gaps?|silence)|"
                       r"shorter pauses?|cut more|pauses? kam|tez|jaldi)\b", c):
            self.set("cut.max_pause", round(_clamp(cur * 0.7, 0.12, 2.0), 2), "Longest pause", S)
            self.set("cut.pad_after", round(_clamp(self.get("cut.pad_after") * 0.8, 0.04, 0.5), 2), "Breath after sentences", S)
            hit = True
        elif re.search(r"\b(slower|relaxed|more natural|breath(e|ing)( room)?|less tight|too (fast|tight|rushed)|rushed|"
                       r"longer pauses?|more pauses?|pauses? zyada|aaram se)\b", c):
            self.set("cut.max_pause", round(_clamp(cur * 1.4, 0.12, 2.0), 2), "Longest pause", S)
            self.set("cut.pad_after", round(_clamp(self.get("cut.pad_after") * 1.3, 0.04, 0.5), 2), "Breath after sentences", S)
            hit = True

        # zoom
        if re.search(r"\bzoom|punch[- ]?in|push[- ]?in|ken burns\b", c):
            hit = True
            if re.search(r"\b(no|remove|disable|turn off|without|stop)\b.*zoom|zoom\b.*\b(off|hatao)\b", c):
                self.set("zoom.mode", "none", "Zoom")
            else:
                if re.search(r"\b(slow|push|ken burns|gradual|smooth)\b", c):
                    self.set("zoom.mode", "push", "Zoom")
                elif re.search(r"\b(punch|jump|on (the )?cuts|snap)\b", c):
                    self.set("zoom.mode", "punch", "Zoom")
                elif self.get("zoom.mode") == "none":
                    self.set("zoom.mode", "punch", "Zoom")
                amt = self.get("zoom.amount")
                m = re.search(r"(\d+(?:\.\d+)?)\s*%", c)
                if m:
                    self.set("zoom.amount", _clamp(float(m.group(1)) / 100, 0.02, 0.3), "Zoom amount", P)
                elif re.search(r"\b(more|bigger|stronger|deeper|zyada)\b", c):
                    self.set("zoom.amount", round(_clamp(amt + 0.04, 0.02, 0.3), 2), "Zoom amount", P)
                elif re.search(r"\b(less|subtle|smaller|lighter|kam)\b", c):
                    self.set("zoom.amount", round(_clamp(amt - 0.03, 0.02, 0.3), 2), "Zoom amount", P)

        # captions
        cap = re.search(SUBJECTS[0][1], c)
        if cap or re.search(r"\b(one|single|two|three|four|five|\d)\s+words?\b|\b(all caps|uppercase|lowercase)\b", c):
            if re.search(r"\b(no|remove|disable|turn off|without|hide|hatao)\b.*\b(captions?|subtitles?|subs)\b|"
                         r"\b(captions?|subtitles?)\b.*\b(off|hatao)\b", c):
                self.set("captions.enabled", False, "Burned-in captions")
                hit = True
            elif re.search(r"\b(add|turn on|enable|show|put)\b.*\b(captions?|subtitles?)\b|\bcaptions? on\b", c):
                self.set("captions.enabled", True, "Burned-in captions")
                hit = True
            size = self.get("captions.size")
            m = re.search(r"\b(?:size|font)\s*(?:to|=)?\s*(\d+(?:\.\d+)?)\s*%?", c)
            if re.search(r"\b(bigger|larger|large|big|increase|bade|bada|badi)\b", c):
                pct = re.search(r"(\d+)\s*%", c)
                k = 1 + (int(pct.group(1)) / 100 if pct else 0.2)
                self.set("captions.size", round(_clamp(size * k, 0.03, 0.14), 3), "Caption size", lambda v: f"{v * 100:.1f}")
                hit = True
            elif re.search(r"\b(smaller|small|tiny|decrease|reduce|chote|chota|choti)\b", c):
                pct = re.search(r"(\d+)\s*%", c)
                k = 1 - (int(pct.group(1)) / 100 if pct else 0.15)
                self.set("captions.size", round(_clamp(size * k, 0.03, 0.14), 3), "Caption size", lambda v: f"{v * 100:.1f}")
                hit = True
            elif m:
                self.set("captions.size", _clamp(float(m.group(1)) / 100, 0.03, 0.14), "Caption size", lambda v: f"{v * 100:.1f}")
                hit = True
            pos = self.get("captions.position")
            for pat, val in ((r"\b(top|upar|upper)\b", 0.16), (r"\b(middle|center|centre|beech)\b", 0.5),
                             (r"\b(bottom|neeche|lower third)\b", 0.82)):
                if re.search(pat, c):
                    self.set("captions.position", val, "Caption position", P)
                    hit = True
                    break
            else:
                if re.search(r"\b(higher|move (it |them )?up|raise|up|upwards)\b", c) and not re.search(r"\bspeed", c):
                    self.set("captions.position", round(_clamp(pos - 0.08, 0.1, 0.9), 2), "Caption position", P)
                    hit = True
                elif re.search(r"\b(lower|move (it |them )?down|down|downwards|neeche)\b", c):
                    self.set("captions.position", round(_clamp(pos + 0.08, 0.1, 0.9), 2), "Caption position", P)
                    hit = True
            nums = {"one": 1, "single": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6}
            m = re.search(r"\b(one|single|two|three|four|five|six|\d)\s+words?\b", c)
            if m:
                n = nums.get(m.group(1)) or int(m.group(1))
                self.set("captions.words", int(_clamp(n, 1, 6)), "Words on screen")
                hit = True
            elif re.search(r"\bmore words\b", c):
                self.set("captions.words", int(_clamp(self.get("captions.words") + 1, 1, 6)), "Words on screen")
                hit = True
            elif re.search(r"\bfewer words|less words\b", c):
                self.set("captions.words", int(_clamp(self.get("captions.words") - 1, 1, 6)), "Words on screen")
                hit = True
            if re.search(r"\b(all caps|uppercase|capital letters|caps)\b", c) and not re.search(r"\bno caps\b", c):
                self.set("captions.uppercase", True, "UPPERCASE captions")
                hit = True
            elif re.search(r"\b(lowercase|normal case|sentence case|no caps)\b", c):
                self.set("captions.uppercase", False, "UPPERCASE captions")
                hit = True
            if re.search(r"\b(numbers?)\b", c) and re.search(r"\b(highlight|emphasi)", c):
                self.set("captions.auto_emphasis", not re.search(r"\b(don'?t|no|stop)\b", c), "Auto-highlight numbers")
                hit = True
            if re.search(r"\b(kinetic|floating|stacked|stacking|animated|cinematic|aesthetic|reel[- ]style)\b", c):
                self.set("captions.style", "kinetic", "Caption style")
                hit = True
            elif re.search(r"\b(classic|normal|simple|regular|basic|subtitle[- ]style|bottom)\b.*\b(captions?|subtitles?|style)\b|"
                           r"\b(captions?|subtitles?)\b.*\b(classic|normal|simple|regular|basic)\b", c):
                self.set("captions.style", "classic", "Caption style")
                hit = True
            if re.search(r"\b(english|roman|latin|hinglish|transliterat\w*|translat\w*)\b", c):
                self.set("captions.script", "latin", "Caption letters",
                         lambda v: "English letters" if v == "latin" else "as spoken")
                if re.search(r"\btranslat", c):
                    self.warn.append("True translation needs an AI model, so captions use English letters for what "
                                     "you say (“dekho”, “kya”) — that keeps the word-by-word timing exact")
                hit = True
            elif re.search(r"\b(original|devanagari|native|as spoken)\b|\b(hindi|kannada|tamil|telugu|marathi|bengali)"
                           r"\s+(script|letters|lipi)\b|\bin (hindi|kannada|tamil|telugu|marathi|bengali)\b", c):
                self.set("captions.script", "original", "Caption letters",
                         lambda v: "English letters" if v == "latin" else "as spoken")
                hit = True
            for target, value in self.colour_targets(c):
                label = {"highlight": "Spoken-word colour", "emphasis": "Emphasis colour",
                         "color": "Caption text colour"}[target]
                self.set(f"captions.{target}", value, label)
                hit = True

        # caption font
        named = next((f for f in sorted(FONT_CHOICES, key=len, reverse=True)
                      if re.search(r"\b" + f.lower() + r"\b", c)), None)
        asked = re.search(r"\b(?:fonts?|typeface)\s+(?:to\s+|as\s+|=\s*)?([a-z0-9][a-z0-9\- ]{1,30})|"
                          r"\buse\s+([a-z0-9][a-z0-9\- ]{1,30}?)\s+fonts?\b", c)
        if named:
            self.set("captions.font", named, "Caption font")
            label = font_label(named)
            if "isn't installed" in label:
                self.warn.append(label + " (put your own font file in studio/fonts to use it)")
            hit = True
        elif asked:
            want = (asked.group(1) or asked.group(2)).strip()
            want = re.sub(r"\b(for|in|on|please|captions?|subtitles?|text)\b.*$", "", want).strip()
            if want and _user_font(want):
                self.set("captions.font", want.title(), "Caption font")
            elif want:
                self.warn.append(f"I don't have the font “{want}”. Available: {', '.join(FONT_CHOICES)} — or drop "
                                 f"a .ttf/.otf file with that name into the studio/fonts folder and ask again")
            hit = True

        # colour grade
        if re.search(r"\b(cinematic|film(ic)?( look)?|moody|dark look|warm|golden|vivid|vibrant|saturat\w*|contrast|"
                     r"pop|enhance|grade|grading|colou?r(s| look| boost)?|natural colou?rs?|lut|filter)\b", c) and \
                (not cap or re.search(r"\b(look|grade|grading)\b", c)):
            if re.search(r"\b(no|natural|remove|turn off|disable|without|plain|original)\b", c):
                grade = "none"
            elif re.search(r"\bcinematic|film", c):
                grade = "cinematic"
            elif re.search(r"\b(moody|dark)\b", c):
                grade = "moody"
            elif re.search(r"\b(warm|golden)\b", c):
                grade = "warm"
            else:
                grade = "vivid"
            self.set("look.grade", grade, "Colour grade")
            if grade == "none" and self.get("look.enhance"):
                self.set("look.enhance", False, "Colour boost")
            hit = True

        # talking speed
        m = re.search(r"\b(\d(?:\.\d+)?)\s*x\b(?!\d)", c)
        pct = re.search(r"(\d+)\s*%\s*faster", c)
        if m and re.search(r"\b(speed|faster|slower|play|talk|voice)\b|^\s*\d", c):
            self.set("speed", round(_clamp(float(m.group(1)), 0.8, 1.5), 2), "Speed", lambda v: f"{v:g}×")
            hit = True
        elif pct:
            self.set("speed", round(_clamp(1 + int(pct.group(1)) / 100, 0.8, 1.5), 2), "Speed", lambda v: f"{v:g}×")
            hit = True
        elif re.search(r"\b(normal|real|original|natural) speed\b|\bno speed( ?up)?\b", c):
            self.set("speed", 1.0, "Speed", lambda v: f"{v:g}×")
            hit = True
        elif re.search(r"\bspeed (it |the video |me |up )?(up|faster)|\bspeed up\b|\bfaster (talking|speech|voice|delivery)\b", c):
            self.set("speed", round(_clamp(self.get("speed") + 0.05, 0.8, 1.5), 2), "Speed", lambda v: f"{v:g}×")
            hit = True
        elif re.search(r"\bslow (it |me )?down\b|\bslower (talking|speech|voice)\b", c):
            self.set("speed", round(_clamp(self.get("speed") - 0.05, 0.8, 1.5), 2), "Speed", lambda v: f"{v:g}×")
            hit = True

        # background music
        if re.search(r"\b(music|song|track|bgm|beat|soundtrack)\b", c):
            hit = True
            vol = self.get("audio.music_volume")
            dbm = re.search(r"(-\d+)\s*db", c)
            if re.search(r"\b(no|remove|mute|without|turn off|disable|stop)\b", c):
                self.set("audio.music", False, "Background music")
            elif dbm:
                self.set("audio.music_volume", _clamp(int(dbm.group(1)), -40, -6), "Music volume", lambda v: f"{v} dB")
            elif re.search(r"\b(louder|higher|more|up|increase|boost)\b", c):
                self.set("audio.music_volume", _clamp(vol + 4, -40, -6), "Music volume", lambda v: f"{v} dB")
            elif re.search(r"\b(quieter|lower|softer|less|down|decrease|reduce|subtle)\b", c):
                self.set("audio.music_volume", _clamp(vol - 4, -40, -6), "Music volume", lambda v: f"{v} dB")
            else:
                self.set("audio.music", True, "Background music")
            if self.get("audio.music") and not music_file(self.pdir):
                self.warn.append("No music track uploaded yet — add one in the Media tab and it'll play under your "
                                 "voice (automatically lowered while you speak)")

        # B-roll
        if re.search(r"\bb[- ]?rolls?\b|\bbroll\b|\bcutaways?\b", c):
            hit = True
            every, length = self.get("broll.every"), self.get("broll.length")
            ev = re.search(r"every\s+(\d+(?:\.\d+)?)\s*(?:s|sec|seconds)", c)
            ln = re.search(r"(?:for|of|lasting|last)\s+(\d+(?:\.\d+)?)\s*(?:s|sec|seconds)|(\d+(?:\.\d+)?)\s*(?:s|sec|second)\s+(?:b[- ]?roll|clips?)", c)
            pin = re.search(PHRASE_TRIGGER + r"\s+(.+)$", c) or re.search(r"\bwhen i (?:say|mention|talk about)\s+(.+)$", c)
            if re.search(r"\b(no|remove|without|turn off|disable|stop)\b", c):
                self.set("broll.enabled", False, "B-roll")
            elif pin:
                phrase = re.sub(r"^(?:where\s+)?i\s+(?:say|said|talk about|mention|mentioned)\s+", "", pin.group(pin.lastindex)).strip(" .")
                ids = self.find_phrase(phrase, word_level=True) or self.find_phrase(phrase, word_level=False)
                if ids:
                    pins = sorted(set(self.cuts.get("broll_pins", [])) | {ids[0]})
                    self.cuts["broll_pins"] = pins
                    self.changed = True
                    self.set("broll.enabled", True, "B-roll")
                    self.done.append(f"B-roll will cut in when you say “{phrase}”")
                else:
                    self.warn.append(f"Couldn't find “{phrase}” for the B-roll")
            else:
                if ev:
                    self.set("broll.every", _clamp(float(ev.group(1)), 2.0, 30.0), "B-roll every", S)
                elif re.search(r"\b(more|frequent|often|lots)\b", c):
                    self.set("broll.every", round(_clamp(every - 1.0, length + 0.8, 30.0), 1), "B-roll every", S)
                elif re.search(r"\b(less|fewer|rarely|occasional)\b", c):
                    self.set("broll.every", round(_clamp(every + 1.5, 2.0, 30.0), 1), "B-roll every", S)
                if ln:
                    v = float(ln.group(1) or ln.group(2))
                    self.set("broll.length", _clamp(v, 0.8, 8.0), "B-roll clip length", S)
                elif re.search(r"\blonger\b", c):
                    self.set("broll.length", round(_clamp(length + 0.6, 0.8, 8.0), 1), "B-roll clip length", S)
                elif re.search(r"\bshorter\b", c):
                    self.set("broll.length", round(_clamp(length - 0.5, 0.8, 8.0), 1), "B-roll clip length", S)
                self.quiet = True
                self.set("broll.enabled", True, "B-roll")
                self.quiet = False
            if self.get("broll.enabled") and not broll_clips(self.pdir):
                self.warn.append("No B-roll clips uploaded yet — add some in the Media tab (any short clips of you "
                                 "working, walking, your setup…) and they'll be cut in automatically")

        # output
        if re.search(r"\b(render|export|final|download|finish)\b", c) and not re.search(r"\bpreview\b", c):
            self.want = "final"
            hit = True
        elif re.search(r"\b(preview|show me|update|re-?render)\b", c):
            self.want = self.want or "preview"
            hit = True
        if re.search(r"\b(don'?t|no) (render|preview)\b", c):
            self.no_render = True
            hit = True
        if re.search(r"\b(save|remember|keep)\b.*\b(default|style|preset|always)\b|\bmy style\b|"
                     r"\b(every|all|future|next) (my )?videos?\b|\bfrom now on\b|\bby default\b|\balways\b", c):
            self.save_default = True
            hit = True
        return hit


def split_instructions(text):
    """Split a request into instructions, keeping quoted phrases and time ranges intact."""
    quotes = {}

    def stash(m):
        key = f"\x00{len(quotes)}\x00"
        quotes[key] = m.group(0)
        return key

    t = re.sub(QUOTED, stash, text.strip())
    t = re.sub(r"\b(between\s+[\d:.]+\s*(?:s|sec|seconds)?)\s+and\s+", r"\1 to ", t, flags=re.I)
    parts = re.split(r"\s*(?:[,;\n]|\.\s+|\bthen\b|\band also\b|\balso\b|\band\b|\baur\b|\bplus\b)\s*", t, flags=re.I)
    out = []
    for p in parts:
        for k, v in quotes.items():
            p = p.replace(k, v)
        p = p.strip(" .")
        if p:
            out.append(p)
    return out


VOCAB = sorted(set("""captions caption subtitles helvetica english hinglish bigger smaller larger highlight
uppercase lowercase position vertical horizontal zoom music background cinematic energetic professional
remove restore tighter faster slower pauses kinetic classic emphasis colour color yellow orange purple green
white black render preview default speed broll quieter louder warm moody vivid natural poppins arial
every seconds font fonts letters script stacked floating relaxed breathing fillers coughs retakes noise
words bottom middle center centre higher lower format landscape portrait instagram reel style""".split()))


def fix_typos(text, known):
    """Correct misspelled command words ("helavtica" → "helvetica"), never transcript words or quotes."""
    parts = re.split(r"(\"[^\"]*\"|“[^”]*”|'[^']*')", text)
    for n, part in enumerate(parts):
        if n % 2:
            continue

        def fix(m):
            w = m.group(0)
            lw = w.lower()
            if len(lw) < 5 or lw in VOCAB or lw in known:
                return w
            hit = difflib.get_close_matches(lw, VOCAB, n=1, cutoff=0.8)
            return hit[0] if hit else w
        parts[n] = re.sub(r"[A-Za-z]+", fix, part)
    return "".join(parts)


def run_copilot(pdir, message):
    cp = Copilot(pdir)
    known = {norm_word(w["text"]) for w in cp.words}
    message = fix_typos(message, known)
    subject = None
    for raw in split_instructions(message):
        c = raw.lower()
        found = next((name for name, pat in SUBJECTS if re.search(pat, c)), None)
        if found:
            subject = found
        elif subject and not re.search(r"\b(" + CUT_VERBS + "|" + KEEP_VERBS + r")\b", c):
            c = f"{subject} {c}"     # "bigger captions and yellow" → "captions yellow"
        if cp.effects(c):
            continue
        if cp.content(c):
            continue
        if cp.settings(c):
            continue
        if cp.last_words_action and cp.has_tx and len(raw.split()) <= 6:
            # "cut where I talk about traders and payouts" → "payouts" continues the cut
            cp.apply_words(cp.find_phrase(raw, word_level=False), cp.last_words_action, f"part “{raw}”")
        else:
            cp.unknown.append(raw)

    if cp.undo:
        cp.done = [undo_last(pdir)]
        cp.changed = cp.changed or not cp.done[0].startswith("Nothing")
        return cp
    if cp.changed:
        snapshot(pdir)
        write_json(pdir / "settings.json", cp.over)
        write_json(pdir / "cuts.json", cp.cuts)
    return cp


def _raw(path):
    return read_json(path) if path.exists() else None


def snapshot(pdir):
    """Remember settings + cuts before a copilot change so "undo" can restore them."""
    path = pdir / "undo.json"
    stack = read_json(path) if path.exists() else []
    stack.append({"settings": _raw(pdir / "settings.json"), "cuts": _raw(pdir / "cuts.json")})
    write_json(path, stack[-20:])


def undo_last(pdir):
    path = pdir / "undo.json"
    stack = read_json(path) if path.exists() else []
    if not stack:
        return "Nothing to undo"
    snap = stack.pop()
    for name in ("settings", "cuts"):
        f = pdir / f"{name}.json"
        if snap[name] is None:
            f.unlink(missing_ok=True)
        else:
            write_json(f, snap[name])
    write_json(path, stack)
    return "Undid the last copilot change"
