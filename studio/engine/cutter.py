"""Turn transcript + decisions into an edit decision list (edl.json)."""
import math
import re

from .common import project_style, read_json, run, write_json
from .decisions import load_cuts, word_states


def subtract(segs, cut_start, cut_end):
    out = []
    for s in segs:
        if s["end"] <= cut_start or s["start"] >= cut_end:
            out.append(s)
            continue
        if s["start"] < cut_start:
            out.append({**s, "end": cut_start})
        if s["end"] > cut_end:
            out.append({**s, "start": cut_end})
    return out


def detect_silences(pdir, proj, db, min_dur):
    """[(start, end)] of stretches quieter than `db` dBFS lasting ≥ min_dur (cached)."""
    cache = pdir / "silences.json"
    key = f"{db}:{min_dur:.3f}"
    if cache.exists():
        c = read_json(cache)
        if c.get("key") == key:
            return c["silences"]
    src = pdir / "audio.mp3"
    r = run(["ffmpeg", "-hide_banner", "-i", src if src.exists() else proj["source"],
             "-af", f"silencedetect=noise={db}dB:d={min_dur:.3f}", "-f", "null", "-"])
    out, start = [], None
    for line in r.stderr.splitlines():
        m = re.search(r"silence_start: (-?[\d.]+)", line)
        if m:
            start = max(0.0, float(m.group(1)))
        m = re.search(r"silence_end: ([\d.]+)", line)
        if m and start is not None:
            out.append((start, float(m.group(1))))
            start = None
    write_json(cache, {"key": key, "silences": out})
    return out


def build_edl(pdir):
    proj = read_json(pdir / "project.json")
    words = read_json(pdir / "transcript.json")["words"]
    style = project_style(pdir)
    st = style["cut"]
    cuts = load_cuts(pdir)
    states = word_states(words, cuts, st)
    kept = [s is None for s in states]
    dur = proj["duration"]
    frame = 1.0 / proj["fps_float"]

    # Runs of kept words; a removed word or a long pause forces a cut.
    segs = []
    for k, w in enumerate(words):
        if not kept[k]:
            continue
        prev_any = words[k - 1] if k > 0 else None
        next_any = words[k + 1] if k + 1 < len(words) else None
        start = min(max(w["start"] - st["pad_before"], prev_any["end"] if prev_any else 0.0), w["start"])
        end = max(min(w["end"] + st["pad_after"], next_any["start"] if next_any else dur), w["end"])
        prev_kept = k > 0 and kept[k - 1]
        if segs and ((prev_kept and w["start"] - prev_any["end"] <= st["max_pause"])
                     or start <= segs[-1]["_pad_end"]):
            segs[-1]["end"] = max(segs[-1]["end"], w["end"])
            segs[-1]["_pad_end"] = end
            continue
        segs.append({"start": start, "end": w["end"], "_pad_end": end})
    for s in segs:
        s["end"] = s.pop("_pad_end")

    # Safety net: word timestamps sometimes stretch over pauses, so also cut real silences.
    min_sil = st["max_pause"] + st["pad_before"] + st["pad_after"]
    for s0, s1 in detect_silences(pdir, proj, st["silence_db"], min_sil):
        segs = subtract(segs, s0 + st["pad_after"], s1 - st["pad_before"])
    segs.sort(key=lambda s: s["start"])

    # Snap to the frame grid, merge near-touching segments, drop slivers.
    snapped = []
    for s in segs:
        a = math.floor(s["start"] / frame + 1e-6) * frame
        b = min(math.ceil(s["end"] / frame - 1e-6) * frame, dur)
        if b - a < st["min_segment"]:
            continue
        if snapped and a - snapped[-1]["end"] < 2 * frame:
            snapped[-1]["end"] = round(b, 6)
            continue
        snapped.append({"start": round(a, 6), "end": round(b, 6)})

    # Attribute each kept word to the segment it overlaps most (for captions & the editor).
    owner = {}
    j = 0
    for k, w in enumerate(words):
        if not kept[k]:
            continue
        while j < len(snapped) - 1 and snapped[j]["end"] <= w["start"]:
            j += 1
        best = max((c for c in (j - 1, j, j + 1) if 0 <= c < len(snapped)), default=None,
                   key=lambda c: min(snapped[c]["end"], w["end"]) - max(snapped[c]["start"], w["start"]))
        if best is not None and min(snapped[best]["end"], w["end"]) > max(snapped[best]["start"], w["start"]) - 1e-3:
            owner.setdefault(best, []).append(w["i"])
    final = []
    for c, s in enumerate(snapped):
        s["words"] = owner.get(c, [])
        final.append(s)

    z = style["zoom"]
    flip = False
    for s in final:
        s["zoom"], s["push"] = 1.0, 0.0
        if z["mode"] == "punch":
            s["zoom"] = 1.0 + z["amount"] if flip else 1.0
            flip = not flip
        elif z["mode"] == "push":
            s["push"] = z["amount"]

    speed = float(style.get("speed", 1.0))
    out_dur = sum(s["end"] - s["start"] for s in final) / speed
    counts = {}
    for r in states:
        if r:
            counts[r] = counts.get(r, 0) + 1
    edl = {"segments": final, "speed": speed, "stats": {
        "source_duration": round(dur, 2), "output_duration": round(out_dur, 2),
        "cuts": max(0, len(final) - 1), "removed_words": counts}}
    write_json(pdir / "edl.json", edl)
    return edl
