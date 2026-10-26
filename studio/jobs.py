"""Background job queue: one video job at a time, state persisted per project."""
import queue
import threading
import time
import traceback

from .engine.common import PROJECTS, project_style, read_json, write_json
from .engine.cutter import build_edl
from .engine.decisions import detect
from .engine.render import render
from .engine.transcribe import transcribe
from .engine import taste
from .engine.common import deep_merge
from .engine.effects import NEEDS_TEXT


def apply_taste_if_wanted(pdir):
    """New uploads: start from the learned taste (if the user turned that on and there is data)."""
    t = taste.load()
    proj = read_json(pdir / "project.json")
    if not (proj.get("use_taste") and taste.has_data(t)):
        return
    cur = project_style(pdir)
    patch, _ = taste.settings_patch(t, NEEDS_TEXT, cur["effects"].get("allow", []))
    over = read_json(pdir / "settings.json") if (pdir / "settings.json").exists() else {}
    # Things the user chose at upload (format, language, captions on/off) win over taste.
    write_json(pdir / "settings.json", deep_merge(patch, over))

_q = queue.Queue()
_lock = threading.Lock()


def state_path(pdir):
    return pdir / "state.json"


def get_state(pdir):
    p = state_path(pdir)
    return read_json(p) if p.exists() else {"status": "idle"}


def set_state(pdir, **kw):
    with _lock:
        s = get_state(pdir)
        s.update(kw)
        s["updated"] = time.time()
        write_json(state_path(pdir), s)
        return s


def busy(pdir):
    return get_state(pdir).get("status") in ("queued", "running")


def request_job(pdir, job):
    """Run now, or right after the current job if one is in progress ("final" wins)."""
    if busy(pdir):
        cur = get_state(pdir).get("next_job")
        set_state(pdir, next_job="final" if "final" in (cur, job) else job)
        return "queued-after"
    enqueue(pdir, job)
    return "started"


def enqueue(pdir, job):
    set_state(pdir, status="queued", job=job, stage="Waiting in queue…", progress=0.0, error=None)
    _q.put((pdir, job))
    if _thread is not None:
        ensure_worker()


def _run(pdir, job):
    def progress(frac, msg):
        set_state(pdir, progress=round(frac, 3), stage=msg)

    def log(msg):
        set_state(pdir, stage=msg)

    set_state(pdir, status="running", started=time.time())
    if job in ("process", "preview", "final"):
        if not (pdir / "transcript.json").exists():
            transcribe(pdir, language=project_style(pdir).get("language", ""), log=log)
        if job == "process" or not (pdir / "cuts.json").exists():
            log("Finding retakes, false starts and filler…")
            detect(pdir)
        log("Building the edit…")
        edl = build_edl(pdir)
        if job == "process":
            apply_taste_if_wanted(pdir)
        mode = "final" if job == "final" else "preview"
        info = render(pdir, mode, progress)
        if mode == "final":
            # A final render is the strongest taste signal: you approved this edit.
            taste.learn_final(project_style(pdir), info.get("effects", []), info["duration"] / 60)
        outputs = get_state(pdir).get("outputs", {})
        outputs[mode] = {**info, "rendered": time.time()}
        set_state(pdir, status="done", stage=f"{mode.capitalize()} ready", progress=1.0,
                  outputs=outputs, stats=edl["stats"])


def _alive(pdir):
    return (pdir / "project.json").exists()


def _one(pdir, job):
    """Run one job; a failure is shown on that video and never stops the worker."""
    if not _alive(pdir):
        return                                            # project was deleted while it waited
    try:
        _run(pdir, job)
    except Exception as e:  # surface every failure in the dashboard
        traceback.print_exc()
        if _alive(pdir):
            set_state(pdir, status="error", error=str(e), stage="Failed")
    if _alive(pdir):
        nxt = get_state(pdir).get("next_job")
        if nxt:
            set_state(pdir, next_job=None)
            enqueue(pdir, nxt)


def _worker():
    while True:
        pdir, job = _q.get()
        try:
            _one(pdir, job)
        except Exception:
            traceback.print_exc()                         # last line of defence: keep the worker alive
        finally:
            _q.task_done()


_thread = None


def ensure_worker():
    """Start the worker, or restart it if it ever died."""
    global _thread
    with _lock:
        if _thread is None or not _thread.is_alive():
            _thread = threading.Thread(target=_worker, daemon=True, name="render-worker")
            _thread.start()


def start():
    # Jobs interrupted by a restart are put back in the queue.
    for pdir in sorted(PROJECTS.glob("*/state.json")):
        s = read_json(pdir)
        if s.get("status") in ("queued", "running") and s.get("job"):
            enqueue(pdir.parent, s["job"])
    ensure_worker()
