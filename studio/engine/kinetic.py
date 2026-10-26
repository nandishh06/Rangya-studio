"""Kinetic captions — the "cinematic reel" look.

Words appear one by one as they're spoken and stack into a phrase placed in the empty space
beside the speaker's face. Small, tightly-tracked sans words with one key word per phrase set
large — alternately in an italic serif or bold caps — and a soft shadow instead of an outline.
"""
from PIL import Image, ImageFilter

from .captions import hex_rgb, output_words
from .common import FONT_DIR
from .media import face_at
from .textrender import _font, get_font

SERIF = FONT_DIR / "InstrumentSerif-Italic.ttf"
STOP = {"a", "an", "the", "i", "to", "of", "and", "is", "it", "in", "on", "at", "for", "my", "me", "we",
        "you", "so", "but", "or", "be", "was", "are", "i'm", "it's", "that", "this", "with", "as", "if",
        "do", "just", "our", "your", "he", "she", "they", "them", "us", "up", "into", "from", "by", "not",
        "se", "ki", "ka", "ke", "hai", "hain", "ko", "aur", "bhi", "toh", "to", "mein", "ne", "ek", "yeh"}


def _norm(t):
    return "".join(ch for ch in t.lower() if ch.isalnum() or ch == "'")


def phrases(ow, max_words):
    out, cur = [], []
    for w in ow:
        if cur and (len(cur) >= max_words or w["start"] - cur[-1]["end"] > 0.3
                    or w["end"] - cur[0]["start"] > 2.6):
            out.append(cur)
            cur = []
        cur.append(w)
        if w["text"][-1:] in ".?!,;:।":
            out.append(cur)
            cur = []
    if cur:
        out.append(cur)
    return out


def key_index(ph, emphasis):
    for k, w in enumerate(ph):
        if w["i"] in emphasis:
            return k
    cands = [(len(_norm(w["text"])) + (5 if any(c.isdigit() for c in w["text"]) else 0), k)
             for k, w in enumerate(ph) if _norm(w["text"]) not in STOP and len(_norm(w["text"])) >= 4]
    return max(cands)[1] if cands else None


class Kinetic:
    def __init__(self, W, H, cs):
        self.W, self.H, self.cs = W, H, cs
        # Phones need bigger type: on vertical video size the text off the width, a bit larger.
        self.base = cs["size"] * (H if W > H else W * 1.5)
        self.choice = cs.get("font", "Helvetica")
        self.landscape = W > H
        self.max_w = (0.34 if self.landscape else 0.72) * W

    def word_style(self, text, role, n):
        """(font, display text, size, tracking) for a word's role in its phrase."""
        if role == "serif":
            size = int(self.base * 1.55)
            f = _font(str(SERIF), 0, size, 400) if SERIF.exists() else get_font(text, size, self.choice)
            if not f.covers(text):
                f = get_font(text, size, self.choice)
            return f, text.strip(".,!?"), size, 0.0
        if role == "caps":
            size = int(self.base * 1.1)
            t = text.strip(".,!?").upper()
            return get_font(t, size, self.choice), t, size, -0.03
        size = int(self.base * (0.44 if _norm(text) in STOP else 0.56))
        return get_font(text, size, self.choice), text, size, -0.025

    def layout(self, ph, key, role):
        """Shape and stack the phrase's words. Returns (block_w, block_h, items)."""
        items = []
        for k, w in enumerate(ph):
            r = role if k == key else "plain"
            f, text, size, track = self.word_style(w["text"], r, len(ph))
            glyphs, width = f.shape(text)
            if track:
                glyphs = [(g, adv + track * size, xo, yo) for g, adv, xo, yo in glyphs]
                width += track * size * len(glyphs)
            items.append({"k": k, "font": f, "glyphs": glyphs, "w": width, "size": size, "role": r})
        lines, cur, cur_w = [], [], 0.0
        for it in items:
            gap = it["size"] * 0.24
            big = it["role"] != "plain"
            if cur and (big or cur[-1]["role"] != "plain" or cur_w + gap + it["w"] > self.max_w):
                lines.append(cur)
                cur, cur_w = [], 0.0
            cur_w += (gap if cur else 0) + it["w"]
            cur.append(it)
        if cur:
            lines.append(cur)
        y, bw = 0.0, 0.0
        for line in lines:
            lh = max(i["size"] for i in line)
            width = sum(i["w"] for i in line) + sum(i["size"] * 0.24 for i in line[1:])
            bw = max(bw, width)
            x = -width / 2
            for i in line:
                i["x"], i["base"] = x, y + lh * 0.82
                x += i["w"] + i["size"] * 0.24
            y += lh * (0.86 if any(i["role"] == "serif" for i in line) else 0.98)
        return bw, y + self.base * 0.3, items

    def place(self, bw, bh, face, n, avoid=None):
        W, H = self.W, self.H
        jitter = (-0.07, 0.0, 0.07)[n % 3]
        if face is None:
            cx, cy = (0.3 * W, 0.5 * H) if self.landscape else (0.5 * W, 0.66 * H)
        elif self.landscape:
            fx, fy, fw = face[0] * W, face[1] * H, face[2] * W
            left = (0.04 * W, fx - fw * 0.85)
            right = (fx + fw * 0.85, 0.96 * W)
            room_l, room_r = left[1] - left[0], right[1] - right[0]
            side = left if room_l >= room_r else right
            cx = (side[0] + side[1]) / 2
            cy = fy + (jitter + 0.04) * H
        else:
            fx, fy, fw = face[0] * W, face[1] * H, face[2] * W
            cx = W / 2 + jitter * W * 1.2
            head_top = fy - fw * 0.8
            room_above = head_top - 0.05 * H >= bh
            if avoid == "top":                       # an effect owns the space above the head
                room_above = False
            elif avoid == "bottom":
                room_above = True
            if room_above:                           # calm space above the head (wall, sky…)
                cy = head_top - bh / 2 - 0.02 * H
            else:                                    # otherwise below the chin
                cy = fy + fw * 1.3 + bh / 2
        cx = min(max(cx, bw / 2 + 0.04 * W), W - bw / 2 - 0.04 * W)
        cy = min(max(cy, bh / 2 + 0.05 * H), H - bh / 2 - 0.06 * H)
        return int(cx - bw / 2), int(cy - bh / 2)

    def frame(self, items, upto, partial, bw, bh, colors):
        """Full-frame RGBA with words [0..upto] visible; word `upto` at `partial` opacity."""
        pad = int(self.base)
        bwp, bhp = int(bw) + pad * 2, int(bh) + pad * 2
        masks = []
        union = Image.new("L", (bwp, bhp), 0)
        for it in items[:upto + 1]:
            m = Image.new("L", (bwp, bhp), 0)
            it["font"].render(m, pad + bw / 2 + it["x"], pad + it["base"], it["glyphs"])
            if it["k"] == upto and partial < 1:
                m = m.point(lambda v: int(v * partial))
            masks.append((it, m))
            union.paste(255, (0, 0), m)
        shadow = union.filter(ImageFilter.GaussianBlur(max(2, self.base * 0.1))).point(lambda v: min(255, int(v * 0.8)))
        img = Image.new("RGBA", (bwp, bhp), (0, 0, 0, 0))
        img.paste((0, 0, 0, 255), (0, max(1, int(self.base * 0.03))), shadow)
        for it, m in masks:
            img.paste(colors[it["k"]] + (255,), (0, 0), m)
        return img, pad


def build_kinetic_track(workdir, edl, words, emphasis, cs, W, H, total, faces, busy=()):
    """busy: [(start, end, "top"|"bottom")] areas taken by effects while they play."""
    for old in workdir.glob("cap_*.png"):
        old.unlink()
    kin = Kinetic(W, H, cs)
    ow = output_words(edl, words, cs.get("script", "original"))
    phs = phrases(ow, max(2, int(cs["words"]) if int(cs["words"]) > 1 else 5))
    blank = workdir / "cap_blank.png"
    Image.new("RGBA", (W, H), (0, 0, 0, 0)).save(blank)
    base_col, accent = hex_rgb(cs["color"]), hex_rgb(cs["emphasis"])

    entries, t, n_img = [], 0.0, 0
    for pi, ph in enumerate(phs):
        key = key_index(ph, emphasis)
        role = "serif" if pi % 2 == 0 else "caps"
        if key is not None and role == "caps" and len(ph[key]["text"]) > 11:
            role = "serif"
        bw, bh, items = kin.layout(ph, key, role)
        end = ph[-1]["end"] + 0.35
        if pi + 1 < len(phs):
            end = min(end, phs[pi + 1][0]["start"])
        face = face_at(faces, ph[0]["start"], end)
        avoid = next((z for a, b, z in busy if z and ph[0]["start"] < b and end > a), None)
        x, y = kin.place(bw, bh, face, pi, avoid)
        colors = []
        for k, w in enumerate(ph):
            hot = (k == key and role == "caps" and pi % 4 == 1) or (w["i"] in emphasis and k != key)
            colors.append(accent if hot else base_col)
        if ph[0]["start"] > t + 0.01:
            entries.append((blank, ph[0]["start"] - t))
            t = ph[0]["start"]
        for k, w in enumerate(ph):
            nxt = ph[k + 1]["start"] if k + 1 < len(ph) else end
            for partial, span in ((0.45, 0.05), (1.0, None)):
                stop = min(nxt, t + span) if span else nxt
                if stop - t <= 0.004:
                    continue
                img, pad = kin.frame(items, k, partial, bw, bh, colors)
                full = Image.new("RGBA", (W, H), (0, 0, 0, 0))
                full.paste(img, (x - pad, y - pad), img)
                path = workdir / f"cap_{n_img:05d}.png"
                full.save(path, compress_level=1)
                n_img += 1
                entries.append((path, stop - t))
                t = stop
    entries.append((blank, max(0.04, total - t)))
    lst = workdir / "captions.txt"
    body = "".join(f"file '{p.name}'\nduration {d:.4f}\n" for p, d in entries)
    body += f"file '{entries[-1][0].name}'\n"
    lst.write_text(body, encoding="utf-8")
    return lst
