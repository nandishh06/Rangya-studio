"""Indian scripts → English letters (Hinglish-style Roman), e.g. "देखो" → "dekho".

Rule-based and offline. All major Indian scripts share the same Unicode layout, so one table
covers Devanagari, Bengali, Gurmukhi, Gujarati, Tamil, Telugu, Kannada and Malayalam.
Hindi-style schwa deletion ("करना" → "karna", not "karana") is applied to northern scripts only.
"""
import re

BLOCKS = {  # block start → (name, delete schwa?)
    0x0900: ("devanagari", True), 0x0980: ("bengali", True), 0x0A00: ("gurmukhi", True),
    0x0A80: ("gujarati", True), 0x0B00: ("oriya", False), 0x0B80: ("tamil", False),
    0x0C00: ("telugu", False), 0x0C80: ("kannada", False), 0x0D00: ("malayalam", False),
}
VOWELS = {0x05: "a", 0x06: "a", 0x07: "i", 0x08: "i", 0x09: "u", 0x0A: "u", 0x0B: "ri", 0x0C: "li",
          0x0D: "e", 0x0E: "e", 0x0F: "e", 0x10: "ai", 0x11: "o", 0x12: "o", 0x13: "o", 0x14: "au",
          0x60: "ri", 0x61: "li"}
MATRAS = {0x3E: "a", 0x3F: "i", 0x40: "i", 0x41: "u", 0x42: "u", 0x43: "ri", 0x44: "ri", 0x45: "e",
          0x46: "e", 0x47: "e", 0x48: "ai", 0x49: "o", 0x4A: "o", 0x4B: "o", 0x4C: "au", 0x62: "li"}
CONS = {0x15: "k", 0x16: "kh", 0x17: "g", 0x18: "gh", 0x19: "n", 0x1A: "ch", 0x1B: "chh", 0x1C: "j",
        0x1D: "jh", 0x1E: "n", 0x1F: "t", 0x20: "th", 0x21: "d", 0x22: "dh", 0x23: "n", 0x24: "t",
        0x25: "th", 0x26: "d", 0x27: "dh", 0x28: "n", 0x29: "n", 0x2A: "p", 0x2B: "ph", 0x2C: "b",
        0x2D: "bh", 0x2E: "m", 0x2F: "y", 0x30: "r", 0x31: "r", 0x32: "l", 0x33: "l", 0x34: "zh",
        0x35: "v", 0x36: "sh", 0x37: "sh", 0x38: "s", 0x39: "h",
        0x58: "q", 0x59: "kh", 0x5A: "gh", 0x5B: "z", 0x5C: "d", 0x5D: "dh", 0x5E: "f", 0x5F: "y"}
NUKTA = {"k": "q", "kh": "kh", "g": "gh", "j": "z", "d": "d", "dh": "dh", "ph": "f", "y": "y"}
CHILLU = {0x7A: "n", 0x7B: "n", 0x7C: "r", 0x7D: "l", 0x7E: "l", 0x7F: "k"}  # Malayalam
LABIAL = ("p", "b", "m")


def _block(ch):
    cp = ord(ch)
    base = cp & ~0x7F
    return (base, cp - base) if base in BLOCKS else (None, None)


def _word(word):
    sylls, out = [], []   # syllable: [consonant, vowel(None=inherent, ""=none), suffix]
    schwa = True
    for ch in word:
        base, off = _block(ch)
        if base is None:
            if ch in "‌‍":
                continue
            sylls.append(["", "", ch])        # passthrough (Latin, punctuation)
            continue
        schwa = BLOCKS[base][1]
        if off in CONS:
            sylls.append([CONS[off], None, ""])
        elif off in VOWELS:
            sylls.append(["", VOWELS[off], ""])
        elif off in MATRAS and sylls and sylls[-1][0]:
            sylls[-1][1] = MATRAS[off]
        elif off == 0x4D and sylls:            # virama: no vowel
            sylls[-1][1] = ""
        elif off == 0x3C and sylls:            # nukta
            sylls[-1][0] = NUKTA.get(sylls[-1][0], sylls[-1][0])
        elif (off in (0x01, 0x02) or (base == 0x0A00 and off == 0x70)) and sylls:  # nasal marks (+ Gurmukhi tippi)
            sylls[-1][2] += "\x00"             # resolved below (n or m)
        elif base == 0x0A00 and off == 0x71:   # Gurmukhi addak: doubles the next consonant
            sylls.append(["", "", "\x01"])
        elif off == 0x03 and sylls:
            sylls[-1][2] += "h"
        elif off in CHILLU and base == 0x0D00:
            sylls.append([CHILLU[off], "", ""])
        elif 0x66 <= off <= 0x6F:
            sylls.append(["", "", str(off - 0x66)])
        elif off in (0x64, 0x65):
            sylls.append(["", "", "."])
        elif off == 0x50:
            sylls.append(["", "", "om"])

    if schwa:
        idx = [k for k, s in enumerate(sylls) if s[0] or s[1]]
        if len(idx) > 1 and sylls[idx[-1]][0] and sylls[idx[-1]][1] is None:
            sylls[idx[-1]][1] = ""                         # word-final schwa: "tak", not "taka"
        for n in range(1, len(idx) - 1):
            s, prev, nxt = sylls[idx[n]], sylls[idx[n - 1]], sylls[idx[n + 1]]
            if s[0] and s[1] is None and prev[1] != "" and nxt[0] and nxt[1] != "" and not s[2]:
                s[1] = ""                                   # "karana" → "karna"
    for k, (c, v, suf) in enumerate(sylls):
        vowel = "a" if v is None else v
        if c == "v" and vowel == "a":
            c = "w"                                         # "wala", "wajah"
        nxt = next((s[0] for s in sylls[k + 1:] if s[0]), "")
        nasal = "m" if nxt.startswith(LABIAL) or (not nxt and not schwa) else "n"
        out.append(c + vowel + suf.replace("\x00", nasal))
    text = "".join(out)
    text = re.sub(r"\x01([a-z])", r"\1\1", text).replace("\x01", "")
    return text.replace("chchh", "cch").replace("chch", "cch")


def to_latin(text):
    """Transliterate any Indian-script runs in `text`; leave everything else unchanged."""
    if not re.search(r"[ऀ-ൿ]", text):
        return text
    return re.sub(r"[ऀ-ൿ‌‍]+", lambda m: _word(m.group(0)), text)
