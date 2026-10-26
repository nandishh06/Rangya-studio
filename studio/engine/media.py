"""Project media: face positions (for placing text), B-roll clips and background music."""
import subprocess
from pathlib import Path

from .common import probe

VIDEO_EXT = {".mp4", ".mov", ".m4v", ".mkv", ".webm", ".avi", ".mts"}
AUDIO_EXT = {".mp3", ".m4a", ".aac", ".wav", ".ogg", ".flac"}


def broll_clips(pdir):
    d = pdir / "broll"
    return sorted(p for p in d.glob("*") if p.suffix.lower() in VIDEO_EXT) if d.exists() else []


def music_file(pdir):
    d = pdir / "music"
    files = sorted(p for p in d.glob("*") if p.suffix.lower() in AUDIO_EXT) if d.exists() else []
    return files[0] if files else None


def detect_faces(video, W, H, sample_fps=2.0):
    """[(t, cx, cy, fw)] — largest face per sampled frame, normalised to 0..1 (empty if no OpenCV)."""
    try:
        import cv2
        import numpy as np
    except ImportError:
        return []
    model = Path(__file__).resolve().parents[1] / "effects" / "models" / "yunet.onnx"
    w = 320
    h = int(round(H * w / W / 2) * 2)
    p = subprocess.Popen(["ffmpeg", "-v", "error", "-i", str(video), "-vf", f"fps={sample_fps},scale={w}:{h},format=bgr24",
                          "-f", "rawvideo", "-"], stdout=subprocess.PIPE, stdin=subprocess.DEVNULL)
    det = cv2.FaceDetectorYN.create(str(model), "", (w, h), 0.6, 0.3, 20)
    out, n, size = [], 0, w * h * 3
    while True:
        buf = p.stdout.read(size)
        if len(buf) < size:
            break
        img = np.frombuffer(buf, np.uint8).reshape(h, w, 3)
        _, found = det.detect(img)
        if found is not None and len(found):
            x, y, fw, fh = max(found, key=lambda r: r[2] * r[3] * r[-1])[:4]
            out.append((n / sample_fps, float(x + fw / 2) / w, float(y + fh / 2) / h, float(fw) / w))
        n += 1
    p.wait()
    return out


def face_at(faces, t0, t1):
    """Median face (cx, cy, fw) between t0 and t1, or None."""
    sel = [f for f in faces if t0 - 0.5 <= f[0] <= t1 + 0.5]
    if not sel:
        return None
    sel.sort(key=lambda f: f[1])
    mid = sel[len(sel) // 2]
    return mid[1], mid[2], mid[3]


def plan_broll(pdir, edl, words, style, total, pins):
    """[(clip, offset, start, dur)] B-roll placements on the output timeline."""
    clips = broll_clips(pdir)
    br = style["broll"]
    if not clips or not br.get("enabled"):
        return []
    dur = float(br["length"])
    every = max(float(br["every"]), dur + 0.8)
    k = edl.get("speed", 1.0)

    # Cut points on the output timeline — B-roll starting on a cut hides the jump.
    cuts, t = [], 0.0
    for seg in edl["segments"]:
        cuts.append(t)
        t += (seg["end"] - seg["start"]) / k

    starts = []
    for T in pins:                                   # requested by the user ("b-roll when I say …")
        if 0 <= T < total - 0.5:
            starts.append(T)
    t = 2.5                                          # keep the hook on your face
    while t + dur < total - 3.0:                     # …and the call-to-action at the end
        near = [c for c in cuts if abs(c - t) <= 0.7]
        T = min(near, key=lambda c: abs(c - t)) if near else t
        if all(abs(T - s) >= dur + 0.5 for s in starts):
            starts.append(T)
        t = T + every
    starts.sort()

    lengths = {}
    out = []
    for n, T in enumerate(starts):
        clip = clips[n % len(clips)]
        if clip not in lengths:
            try:
                lengths[clip] = probe(clip)["duration"]
            except Exception:
                lengths[clip] = dur
        d = min(dur, total - T)
        span = max(0.0, lengths[clip] - d)
        uses = n // len(clips)
        offset = (span / 2 + uses * d * 1.3) % span if span > 0 else 0.0
        out.append((clip, offset, round(T, 3), round(d, 3)))
    return out
