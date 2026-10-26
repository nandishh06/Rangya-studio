"""Word-by-word highlighted captions (burned in) and an SRT sidecar, on the edited timeline.

Captions are shaped with HarfBuzz, drawn into a strip of PNGs, played back as a timed image stream
(ffmpeg concat demuxer) and overlaid on the video — this works with any ffmpeg build,
including ones without libass/freetype.
"""
from PIL import Image, ImageFilter

from .common import is_filler
from .textrender import get_font
from .translit import to_latin


def output_words(edl, words, script="original"):
    """Kept spoken words with times on the output timeline (optionally in English letters)."""
    out, t, k = [], 0.0, edl.get("speed", 1.0)
    for seg in edl["segments"]:
        for i in seg["words"]:
            w = words[i]
            if w["type"] != "word" or is_filler(w):
                continue
            s = t + (min(max(w["start"], seg["start"]), seg["end"]) - seg["start"]) / k
            e = t + (max(min(w["end"], seg["end"]), seg["start"]) - seg["start"]) / k
            text = to_latin(w["text"]) if script == "latin" else w["text"]
            if (not out or out[-1]["text"][-1:] in ".?!।॥") and text[:1].islower():
                text = text[0].upper() + text[1:]  # sentence start after a cut
            out.append({"i": i, "text": text, "start": s, "end": max(e, s + 0.04)})
        t += (seg["end"] - seg["start"]) / k
    return out


def group_cues(ow, max_words, max_chars=32, max_secs=3.0):
    cues, cur = [], []
    for w in ow:
        if cur:
            text = " ".join(x["text"] for x in cur + [w])
            if (len(cur) >= max_words or len(text) > max_chars or w["start"] - cur[-1]["end"] > 0.6
                    or w["end"] - cur[0]["start"] > max_secs):
                cues.append(cur)
                cur = []
        cur.append(w)
        if w["text"][-1:] in ".?!।॥":
            cues.append(cur)
            cur = []
    if cur:
        cues.append(cur)
    return cues


def srt_time(t):
    ms = int(round(t * 1000))
    h, ms = divmod(ms, 3600000)
    m, ms = divmod(ms, 60000)
    s, ms = divmod(ms, 1000)
    return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"


def write_srt(path, ow):
    cues = group_cues(ow, max_words=8, max_chars=42, max_secs=3.5)
    lines = []
    for n, c in enumerate(cues, 1):
        end = c[-1]["end"] + 0.3
        if n < len(cues):
            end = min(end, cues[n][0]["start"])
        lines += [str(n), f"{srt_time(c[0]['start'])} --> {srt_time(end)}",
                  " ".join(x["text"] for x in c), ""]
    path.write_text("\n".join(lines), encoding="utf-8")


def hex_rgb(h):
    h = h.lstrip("#")
    return tuple(int(h[k:k + 2], 16) for k in (0, 2, 4))


class Painter:
    """Lays out a caption cue once, then paints one image per active word."""

    def __init__(self, W, H, cs):
        self.W, self.cs = W, cs
        self.size = max(12, int(cs["size"] * min(W, H)))
        self.stroke = max(2, self.size // 10)
        self.line_h = int(self.size * 1.5)
        self.box_h = self.line_h * 2 + self.stroke * 4
        self.max_w = int(W * 0.86)
        self.choice = cs.get("font", "Poppins")
        self.space = get_font(" ", self.size, self.choice).shape(" ")[1]
        self.y = int(cs["position"] * H - self.box_h / 2)
        self.y = min(max(self.y, 0), H - self.box_h)

    def prepare(self, texts):
        shaped = []
        for t in texts:
            f = get_font(t, self.size, self.choice)
            glyphs, w = f.shape(t)
            shaped.append((f, glyphs, w))
        lines, cur, cur_w = [], [], 0.0
        for k, (_, _, w) in enumerate(shaped):
            if cur and cur_w + self.space + w > self.max_w:
                lines.append(cur)
                cur, cur_w = [], 0.0
            cur_w += (self.space if cur else 0) + w
            cur.append(k)
        if cur:
            lines.append(cur)
        lines = lines[:2]
        masks = [None] * len(texts)
        union = Image.new("L", (self.W, self.box_h), 0)
        top = (self.box_h - self.line_h * len(lines)) // 2
        for line in lines:
            total = sum(shaped[k][2] for k in line) + self.space * (len(line) - 1)
            x = (self.W - total) / 2
            baseline = top + self.size * 1.08
            for k in line:
                f, glyphs, w = shaped[k]
                m = Image.new("L", (self.W, self.box_h), 0)
                f.render(m, x, baseline, glyphs)
                masks[k] = m
                union.paste(255, (0, 0), m)
                x += w + self.space
            top += self.line_h
        # Outline: grow the text shape outward by roughly `stroke` pixels.
        outline = union.filter(ImageFilter.GaussianBlur(self.stroke * 0.6)).point(lambda v: min(255, v * 6))
        return masks, outline

    def draw(self, prepared, active, colors):
        masks, outline = prepared
        img = Image.new("RGBA", (self.W, self.box_h), (0, 0, 0, 0))
        img.paste((0, 0, 0, 235), (0, 0), outline)
        for k, m in enumerate(masks):
            if m is None:
                continue
            fill = hex_rgb(self.cs["highlight"]) if k == active else colors[k]
            img.paste(fill + (255,), (0, 0), m)
        return img


def build_caption_track(workdir, edl, words, emphasis, cs, W, H, total):
    """Render caption PNGs + an ffmpeg concat list. Returns (list_path, y_offset)."""
    for old in workdir.glob("cap_*.png"):
        old.unlink()
    p = Painter(W, H, cs)
    ow = output_words(edl, words, cs.get("script", "original"))
    cues = group_cues(ow, max(1, int(cs["words"])))
    blank = workdir / "cap_blank.png"
    Image.new("RGBA", (W, p.box_h), (0, 0, 0, 0)).save(blank)

    entries, t, n = [], 0.0, 0
    base, emph = hex_rgb(cs["color"]), hex_rgb(cs["emphasis"])
    for ci, cue in enumerate(cues):
        cue_end = cue[-1]["end"] + 0.25
        if ci + 1 < len(cues):
            cue_end = min(cue_end, cues[ci + 1][0]["start"])
        if cue[0]["start"] > t + 0.01:
            entries.append((blank, cue[0]["start"] - t))
            t = cue[0]["start"]
        texts = [w["text"].upper() if cs["uppercase"] else w["text"] for w in cue]
        colors = [emph if w["i"] in emphasis else base for w in cue]
        prepared = p.prepare(texts)
        for k, w in enumerate(cue):
            nxt = cue[k + 1]["start"] if k + 1 < len(cue) else cue_end
            if nxt - t <= 0.005:
                continue
            path = workdir / f"cap_{n:05d}.png"
            p.draw(prepared, k, colors).save(path, compress_level=1)
            n += 1
            entries.append((path, nxt - t))
            t = nxt
    entries.append((blank, max(0.04, total - t)))
    lst = workdir / "captions.txt"
    body = "".join(f"file '{e[0].name}'\nduration {e[1]:.4f}\n" for e in entries)
    body += f"file '{entries[-1][0].name}'\n"  # concat demuxer needs the last file repeated
    lst.write_text(body, encoding="utf-8")
    return lst, p.y
