"""Render an edl to video: per-segment encode (cached) → join → captions + loudness."""
import hashlib
import json
import re
import subprocess
from concurrent.futures import ThreadPoolExecutor, as_completed

from .captions import build_caption_track, output_words, write_srt
from .common import out_duration, output_size, project_style, read_json, run, src_to_out
from .decisions import emphasis_set, load_cuts
from .effects import plan as plan_effects, render_windows, zone
from .kinetic import build_kinetic_track
from .media import broll_clips, detect_faces, music_file, plan_broll

GRADES = {
    "vivid": "eq=contrast=1.06:saturation=1.15:gamma=0.98,unsharp=5:5:0.35",
    "cinematic": "eq=contrast=1.08:saturation=0.92:gamma=0.97,"
                 "colorbalance=rs=-0.03:bs=0.05:rh=0.06:gh=0.02:bh=-0.05,vignette=PI/5",
    "warm": "colorbalance=rs=0.04:rm=0.05:bm=-0.04:bh=-0.04,eq=saturation=1.08:contrast=1.04,vignette=PI/6",
    "moody": "eq=contrast=1.12:brightness=-0.035:saturation=0.85,colorbalance=bs=0.06:rh=0.03,vignette=PI/4",
}


def grade_filter(look):
    g = look.get("grade", "none")
    if g == "none" and look.get("enhance"):
        g = "vivid"
    return GRADES.get(g)

OUT_NAMES = {"preview": "preview.mp4", "final": "final.mp4"}


def video_filter(seg, proj, fmt, W, H, fps, look, speed=1.0):
    f = []
    z = float(seg.get("zoom", 1.0))
    if z > 1.001:
        # Zoom in, crop back to the original frame; bias upward to keep heads in frame.
        f.append(f"scale=trunc(iw*{z:.4f}/2)*2:-2")
        f.append(f"crop=trunc(iw/{z:.4f}/2)*2:trunc(ih/{z:.4f}/2)*2:(iw-ow)/2:(ih-oh)*0.35")

    src_ar = proj["width"] / proj["height"]
    tgt_ar = W / H
    if fmt == "original" or abs(src_ar - tgt_ar) < 0.01:
        f.append(f"scale={W}:{H}")
    elif src_ar > tgt_ar:
        # Wider source → centre crop (e.g. 16:9 recording to a 9:16 reel).
        f.append(f"crop=trunc(ih*{tgt_ar:.5f}/2)*2:ih:(iw-ow)/2:0,scale={W}:{H}")
    else:
        # Taller source → fit, with a blurred copy filling the sides.
        f.append(f"split[ra][rb];[ra]scale={W}:{H}:force_original_aspect_ratio=increase,crop={W}:{H},"
                 f"boxblur=24:2,eq=brightness=-0.08[bg];[rb]scale={W}:{H}:force_original_aspect_ratio=decrease[fg];"
                 f"[bg][fg]overlay=(W-w)/2:(H-h)/2")

    push = float(seg.get("push", 0.0))
    if push > 0.001:
        frames = max(1, round((seg["end"] - seg["start"]) / speed * fps))
        # Supersample first so the slow push is smooth instead of jittery.
        f.append(f"scale={W * 2}:{H * 2}")
        f.append(f"zoompan=z='1+{push:.4f}*on/{frames}':x='iw/2-iw/zoom/2':y='(ih-ih/zoom)*0.4'"
                 f":d=1:s={W}x{H}:fps={fps:.5f}")
    g = grade_filter(look)
    if g:
        f.append(g)
    f.append("setsar=1,format=yuv420p")
    return ",".join(f)


def encode_segment(seg, proj, vf, af, enc, cache, speed=1.0):
    """Encode one kept segment. Trimming happens inside the filter graph (after a fast seek):
    ffmpeg 8 deadlocks when output-side -t/-frames limits meet atempo/apad."""
    fps = proj["fps_float"]
    src = seg["end"] - seg["start"]
    frames = max(1, round(src / speed * fps))
    dur = frames / fps
    key = json.dumps([proj["source"], seg["start"], frames, vf, af, enc, speed])
    out = cache / (hashlib.sha1(key.encode()).hexdigest()[:16] + ".mkv")
    if out.exists():
        return out
    fade = min(0.012, dur / 4)
    tempo = [f"atempo={speed:.4f}"] if abs(speed - 1) > 1e-3 else []
    audio = ([f"atrim=duration={src + 0.05:.6f}", "asetpts=PTS-STARTPTS"] + tempo + af +
             [f"afade=t=in:d={fade:.4f}", f"afade=t=out:st={dur - fade:.6f}:d={fade:.4f}",
              f"apad=whole_dur={dur:.6f}", f"atrim=end={dur:.6f}"])
    video = (f"[0:v]trim=duration={src + 0.1:.6f},setpts=(PTS-STARTPTS)/{speed:.4f},"
             f"fps={proj['fps']},{vf}[v]")
    tmp = out.with_name(out.stem + ".part.mkv")
    run(["ffmpeg", "-y", "-v", "error", "-ss", f"{seg['start']:.6f}", "-i", proj["source"],
         "-filter_complex", f"{video};[0:a]{','.join(audio)}[a]", "-map", "[v]", "-map", "[a]",
         "-frames:v", str(frames), "-c:v", "libx264", "-preset", enc["preset"], "-crf", str(enc["crf"]),
         "-c:a", "pcm_s16le", "-ar", "48000", "-ac", "2", tmp])
    tmp.replace(out)
    return out


def run_with_progress(cmd, total, cb):
    """Run ffmpeg, reporting fraction done from its time= output."""
    p = subprocess.Popen([str(c) for c in cmd], stderr=subprocess.PIPE, stdout=subprocess.DEVNULL, stdin=subprocess.DEVNULL,
                         text=True, encoding="utf-8", errors="replace")
    tail = []
    for line in p.stderr:
        tail = (tail + [line])[-12:]
        m = re.search(r"time=(\d+):(\d+):([\d.]+)", line)
        if m and total > 0:
            t = int(m.group(1)) * 3600 + int(m.group(2)) * 60 + float(m.group(3))
            cb(min(1.0, t / total))
    if p.wait() != 0:
        raise RuntimeError("ffmpeg failed:\n" + "".join(tail))


def signature(pdir):
    """Fingerprint of everything that affects the output (settings + edit decisions)."""
    style = project_style(pdir)
    style.pop("language", None)
    cuts = (pdir / "cuts.json").read_text(encoding="utf-8") if (pdir / "cuts.json").exists() else ""
    media = [p.name for p in broll_clips(pdir)] + [str(music_file(pdir))]
    return hashlib.sha1((json.dumps(style, sort_keys=True) + cuts + json.dumps(media)).encode()).hexdigest()[:12]


def render(pdir, mode, progress=lambda frac, msg: None, jobs=3):
    sig = signature(pdir)
    proj = read_json(pdir / "project.json")
    edl = read_json(pdir / "edl.json")
    words = read_json(pdir / "transcript.json")["words"]
    style = project_style(pdir)
    enc = style[mode]
    fmt = style["format"]
    W, H = output_size(proj, fmt, int(enc["short_side"]))
    aud = style["audio"]
    segs = edl["segments"]
    speed = float(edl.get("speed", 1.0))
    if not segs:
        raise RuntimeError("Nothing left to render — every word is cut. Restore some words.")
    cache = pdir / f".segments-{mode}"
    cache.mkdir(exist_ok=True)

    af = []
    if aud.get("denoise"):
        af.append("afftdn=nf=-25")
    if aud.get("highpass"):
        af.append(f"highpass=f={aud['highpass']}")

    # 1) segments
    results = [None] * len(segs)
    done = 0
    with ThreadPoolExecutor(max_workers=jobs) as ex:
        futs = {ex.submit(encode_segment, s, proj,
                          video_filter(s, proj, fmt, W, H, proj["fps_float"], style["look"], speed),
                          af, {"preset": enc["preset"], "crf": enc["crf"]}, cache, speed): k
                for k, s in enumerate(segs)}
        for fut in as_completed(futs):
            results[futs[fut]] = fut.result()
            done += 1
            progress(0.75 * done / len(segs), f"Cutting & zooming: segment {done}/{len(segs)}")
    used = {p.name for p in results}
    for p in cache.glob("*.mkv"):
        if p.name not in used:
            p.unlink()

    # 2) join (lossless)
    progress(0.76, "Joining segments…")
    lst = cache / "list.txt"
    lst.write_text("".join(f"file '{p.name}'\n" for p in results), encoding="utf-8")
    joined = cache / "joined.mkv"
    run(["ffmpeg", "-y", "-v", "error", "-f", "concat", "-safe", "0", "-i", lst, "-c", "copy", joined])
    total = out_duration(edl)

    # 3) loudness measurement
    progress(0.78, "Measuring loudness…")
    target = f"I={aud['loudness']}:TP={aud['true_peak']}:LRA=11"
    r = run(["ffmpeg", "-hide_banner", "-i", joined, "-map", "0:a", "-af",
             f"loudnorm={target}:print_format=json", "-f", "null", "-"])
    m = json.loads(r.stderr[r.stderr.rindex("{"):r.stderr.rindex("}") + 1])
    loud = (f"loudnorm={target}:measured_I={m['input_i']}:measured_TP={m['input_tp']}:"
            f"measured_LRA={m['input_lra']}:measured_thresh={m['input_thresh']}:"
            f"offset={m['target_offset']}:linear=true,aresample=48000")

    # 4) compose: B-roll, captions, music, loudness
    out = pdir / OUT_NAMES[mode]
    tmp_out = pdir / f".{mode}.tmp.mp4"
    cs = style["captions"]
    cuts = load_cuts(pdir)
    pins = [src_to_out(edl, words[i]["start"]) for i in cuts.get("broll_pins", []) if i < len(words)]
    broll = plan_broll(pdir, edl, words, style, total, [p for p in pins if p is not None])
    music = music_file(pdir) if aud.get("music") else None
    effects = plan_effects(cuts, edl, words, style, total, [(T, D) for _, _, T, D in broll])
    segsig = hashlib.sha1("".join(p.name for p in results).encode()).hexdigest()[:12]
    fx_clips = render_windows(cache, joined, segsig, effects, W, H, total,
                              lambda f, m: progress(0.79 + 0.01 * f, m))

    cmd = ["ffmpeg", "-y", "-hide_banner", "-i", joined]
    graph, v, idx = [], "[0:v]", 1
    for n, (clip, a, s0, s1) in enumerate(fx_clips):
        cmd += ["-i", clip]
        graph.append(f"[{idx}:v]scale={W}:{H},fps={proj['fps_float']:.5f},setsar=1,format=yuv420p,"
                     f"setpts=PTS-STARTPTS+{a:.3f}/TB[e{n}]")
        graph.append(f"{v}[e{n}]overlay=0:0:enable='between(t,{s0:.3f},{s1:.3f})':eof_action=pass[ve{n}]")
        v, idx = f"[ve{n}]", idx + 1
    g = grade_filter(style["look"])
    for n, (clip, off, T, D) in enumerate(broll):
        cmd += ["-ss", f"{off:.3f}", "-t", f"{D + 0.1:.3f}", "-i", clip]
        graph.append(f"[{idx}:v]scale={W}:{H}:force_original_aspect_ratio=increase,crop={W}:{H},"
                     f"fps={proj['fps_float']:.5f},{(g + ',') if g else ''}setsar=1,format=yuv420p,"
                     f"setpts=PTS-STARTPTS+{T:.3f}/TB[b{n}]")
        graph.append(f"{v}[b{n}]overlay=0:0:enable='between(t,{T:.3f},{T + D:.3f})':eof_action=pass[v{n}]")
        v, idx = f"[v{n}]", idx + 1
    if cs["enabled"]:
        progress(0.8, "Drawing captions…")
        emph = emphasis_set(cuts, cs)
        if cs.get("style") == "kinetic":
            progress(0.8, "Finding your face for caption placement…")
            faces = detect_faces(joined, W, H)
            progress(0.81, "Animating captions…")
            busy = [(e["start"], e["end"], zone(e["fx"])) for e in effects]
            cap_list, y = build_kinetic_track(cache, edl, words, emph, cs, W, H, total, faces, busy), 0
        else:
            cap_list, y = build_caption_track(cache, edl, words, emph, cs, W, H, total)
        cmd += ["-f", "concat", "-safe", "0", "-i", cap_list]
        graph.append(f"[{idx}:v]format=rgba[cap];{v}[cap]overlay=0:{y}:eof_action=pass:format=auto[vc]")
        v, idx = "[vc]", idx + 1
    graph.append(f"{v}format=yuv420p[v]")
    graph.append(f"[0:a]{loud}[voice]")
    if music:
        cmd += ["-stream_loop", "-1", "-i", music]
        fade_out = max(0.0, total - 1.5)
        graph.append(f"[{idx}:a]aresample=48000,aformat=channel_layouts=stereo,atrim=0:{total:.3f},"
                     f"volume={float(aud['music_volume']):.1f}dB,afade=t=in:d=0.8,"
                     f"afade=t=out:st={fade_out:.3f}:d=1.5[mus]")
        # Duck the music under the voice so speech always stays clear.
        graph.append("[voice]asplit[v1][v2];[mus][v2]sidechaincompress=threshold=0.02:ratio=8:attack=20:"
                     "release=400[duck];[v1][duck]amix=inputs=2:normalize=0:duration=first,"
                     "alimiter=limit=0.89[a]")
        idx += 1
    else:
        graph.append("[voice]anull[a]")
    cmd += ["-filter_complex", ";".join(graph), "-map", "[v]", "-map", "[a]", "-r", proj["fps"],
            "-t", f"{total:.3f}", "-c:v", "libx264", "-preset", enc["preset"], "-crf", str(enc["crf"]),
            "-c:a", "aac", "-b:a", "192k", "-movflags", "+faststart", tmp_out]
    run_with_progress(cmd, total, lambda f: progress(0.82 + 0.17 * f, "Finishing video…"))
    tmp_out.replace(out)
    joined.unlink()
    write_srt(pdir / "captions.srt", output_words(edl, words, cs.get("script", "original")))
    progress(1.0, "Done")
    return {"file": out.name, "width": W, "height": H, "duration": round(total, 2), "sig": sig,
            "broll": len(broll), "music": music.name if music else None,
            "effects": [{"fx": e["fx"], "start": e["start"], "end": e["end"], "why": e["why"],
                         "trigger": e.get("trigger", "moment")} for e in effects],
            "loudness_in": float(m["input_i"])}
