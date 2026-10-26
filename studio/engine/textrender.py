"""Shaped text rendering (HarfBuzz + FreeType) and caption font selection.

Pillow's built-in text drawing can't shape Devanagari/Kannada/Tamil etc. (vowel signs and
conjuncts come out wrong) unless the system has libraqm + fribidi. HarfBuzz/FreeType wheels
work everywhere, so captions shape the same on macOS and Windows.
"""
import os
import sys
from functools import lru_cache
from pathlib import Path

import freetype
import uharfbuzz as hb
from PIL import Image

from .common import FONT_DIR

POPPINS = FONT_DIR / "Poppins-ExtraBold.ttf"     # Latin + Devanagari
SCRIPT_FONTS = [
    (0x0900, 0x097F, "Poppins-ExtraBold.ttf"),
    (0x0C80, 0x0CFF, "NotoSansKannada.ttf"),
    (0x0B80, 0x0BFF, "NotoSansTamil.ttf"),
    (0x0C00, 0x0C7F, "NotoSansTelugu.ttf"),
    (0x0D00, 0x0D7F, "NotoSansMalayalam.ttf"),
    (0x0980, 0x09FF, "NotoSansBengali.ttf"),
    (0x0A80, 0x0AFF, "NotoSansGujarati.ttf"),
    (0x0A00, 0x0A7F, "NotoSansGurmukhi.ttf"),
]
BUNDLED = {p.name for p in FONT_DIR.glob("*.tt[fc]")}

# Caption font choices → where to look, in order. Helvetica is an Apple font and can't be
# shipped, so it's taken from the system; Arial / Arimo (free, same metrics) stand in for it.
_WIN = Path(os.environ.get("WINDIR", r"C:\Windows")) / "Fonts"
_WIN_USER = Path(os.environ.get("LOCALAPPDATA", "")) / "Microsoft" / "Windows" / "Fonts"
SYSTEM = {
    "Helvetica": ["/System/Library/Fonts/Helvetica.ttc", "/Library/Fonts/Helvetica.ttc",
                  _WIN / "Helvetica-Bold.ttf", _WIN / "HelveticaBold.ttf", _WIN_USER / "Helvetica-Bold.ttf",
                  _WIN_USER / "Helvetica Bold.ttf", "/usr/share/fonts/truetype/helvetica/Helvetica-Bold.ttf"],
    "Helvetica Neue": ["/System/Library/Fonts/HelveticaNeue.ttc", _WIN_USER / "HelveticaNeue-Bold.ttf",
                       _WIN / "HelveticaNeue-Bold.ttf"],
    "Arial": ["/System/Library/Fonts/Supplemental/Arial Bold.ttf", "/Library/Fonts/Arial Bold.ttf",
              _WIN / "arialbd.ttf", "/usr/share/fonts/truetype/msttcorefonts/Arial_Bold.ttf"],
}
FALLBACKS = {"Helvetica": ["Helvetica Neue", "Arial", "Arimo"], "Helvetica Neue": ["Helvetica", "Arial", "Arimo"],
             "Arial": ["Arimo"]}
FONT_CHOICES = ["Helvetica", "Helvetica Neue", "Arial", "Poppins", "Arimo"]


def _bold_index(path):
    """Index of the Bold face inside a .ttc collection (0 for single fonts)."""
    try:
        n = freetype.Face(str(path)).num_faces
    except Exception:
        return None
    best = 0
    for i in range(n):
        style = freetype.Face(str(path), i).style_name.decode("utf-8", "ignore").lower()
        if style == "bold":
            return i
        if "bold" in style and "oblique" not in style and "italic" not in style:
            best = i
    return best


def _user_font(name):
    """A font file the user dropped into studio/fonts whose name matches (e.g. Helvetica-Bold.otf)."""
    key = name.lower().replace(" ", "")
    files = [p for p in FONT_DIR.glob("*") if p.suffix.lower() in (".ttf", ".otf", ".ttc")
             and p.name not in BUNDLED and key in p.stem.lower().replace(" ", "").replace("-", "")]
    files.sort(key=lambda p: ("bold" not in p.stem.lower(), len(p.name)))
    return files[0] if files else None


@lru_cache(maxsize=16)
def resolve_font(name):
    """(path, face_index, label) for a caption font choice, with sensible fallbacks."""
    for cand in [name] + FALLBACKS.get(name, []):
        user = _user_font(cand)
        if user:
            return str(user), _bold_index(user) or 0, f"{cand} ({user.name})"
        if cand == "Poppins":
            return str(POPPINS), 0, "Poppins ExtraBold"
        if cand == "Arimo":
            return str(FONT_DIR / "Arimo.ttf"), 0, "Arimo Bold (free Helvetica/Arial look-alike)"
        for p in SYSTEM.get(cand, []):
            if p and Path(p).exists():
                idx = _bold_index(p)
                if idx is not None:
                    return str(p), idx, f"{cand} Bold"
    return str(POPPINS), 0, "Poppins ExtraBold"


def font_label(name):
    label = resolve_font(name)[2]
    return label if label.startswith(name) else f"{name} isn't installed — using {label}"


class ShapedFont:
    def __init__(self, path, index, size, weight=800):
        self.size = size
        self.ft = freetype.Face(path, index)
        self.hb_font = hb.Font(hb.Face(hb.Blob.from_file_path(path), index))
        try:
            axes = self.ft.get_variation_info().axes
        except Exception:
            axes = []
        if axes:
            coords, variations = [], {}
            for ax in axes:
                v = min(max(weight, ax.minimum), ax.maximum) if ax.tag == "wght" else ax.default
                coords.append(v)
                variations[ax.tag] = v
            self.ft.set_var_design_coords(coords)
            self.hb_font.set_variations(variations)
        self.ft.set_char_size(int(size * 64))
        self.hb_font.scale = (int(size * 64), int(size * 64))

    def covers(self, text):
        return all(self.ft.get_char_index(ord(ch)) for ch in text if not ch.isspace())

    def shape(self, text):
        buf = hb.Buffer()
        buf.add_str(text)
        buf.guess_segment_properties()
        hb.shape(self.hb_font, buf, {"kern": True, "liga": True})
        glyphs = [(i.codepoint, p.x_advance / 64, p.x_offset / 64, p.y_offset / 64)
                  for i, p in zip(buf.glyph_infos, buf.glyph_positions)]
        return glyphs, sum(g[1] for g in glyphs)

    def render(self, mask, x, baseline, glyphs):
        """Draw shaped glyphs into an 'L' mask image."""
        for gid, adv, xo, yo in glyphs:
            self.ft.load_glyph(gid, freetype.FT_LOAD_RENDER | freetype.FT_LOAD_TARGET_LIGHT)
            g = self.ft.glyph
            bmp = g.bitmap
            if bmp.width and bmp.rows:
                im = Image.frombuffer("L", (bmp.width, bmp.rows), bytes(bmp.buffer), "raw", "L",
                                      bmp.pitch, 1)
                mask.paste(255, (int(round(x + xo + g.bitmap_left)),
                                 int(round(baseline - yo - g.bitmap_top))), im)
            x += adv


@lru_cache(maxsize=48)
def _font(path, index, size, weight):
    return ShapedFont(path, index, size, weight)


def get_font(text, size, choice="Poppins"):
    """Font for a word: the chosen caption font, or a script font if it lacks the glyphs."""
    path, index, _ = resolve_font(choice)
    weight = 700 if choice != "Poppins" else 800
    f = _font(path, index, size, weight)
    if f.covers(text):
        return f
    for ch in text:
        cp = ord(ch)
        for lo, hi, name in SCRIPT_FONTS:
            if lo <= cp <= hi and (FONT_DIR / name).exists():
                return _font(str(FONT_DIR / name), 0, size, 800)
    return _font(str(POPPINS), 0, size, 800)
