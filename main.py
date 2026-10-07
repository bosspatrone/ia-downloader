from contextlib import asynccontextmanager
from fastapi import FastAPI, HTTPException
from fastapi.responses import HTMLResponse, StreamingResponse, FileResponse
from pathlib import Path
from pydantic import BaseModel
import asyncio, json, os, re, shutil, time, uuid
from typing import Dict, List, Set

AUDIO_ROOT = "/music"
HISTORY_FILE = "/music/.ia-history"
BLOCK_FILE   = "/music/.ia-blocklist"   # permanently deleted identifiers: never download again
CATEGORIES = ["Video Game", "Music", "Classical", "Jazz", "Anime", "Sound Effects"]
JOBS_FILE  = "/music/.ia-jobs.json"
QUEUE_FILE = "/music/.ia-queue.json"
WAITING    = ("pending_meta", "queued")

jobs: Dict[str, dict] = {}
job_queue: asyncio.Queue = asyncio.Queue()
queue_list: List[str] = []
paused = False


def save_queue():
    # Written via a temp file so a crash mid-write can't leave a truncated queue
    try:
        tmp = QUEUE_FILE + ".tmp"
        with open(tmp, "w") as f:
            json.dump(queue_list, f)
        os.replace(tmp, QUEUE_FILE)
    except Exception:
        pass


def load_queue() -> List[str]:
    try:
        with open(QUEUE_FILE) as f:
            data = json.load(f)
        return [jid for jid in data if isinstance(jid, str)]
    except (FileNotFoundError, json.JSONDecodeError):
        return []


def save_jobs():
    try:
        data = {}
        for jid, job in jobs.items():
            entry = {k: v for k, v in job.items() if k != "downloaded" and not k.startswith("_")}
            entry["downloaded"] = sorted(job.get("downloaded", set()))
            data[jid] = entry
        with open(JOBS_FILE, "w") as f:
            json.dump(data, f)
    except Exception:
        pass
    save_queue()


def load_jobs() -> List[str]:
    """Load jobs; return ids that were mid-download when the app stopped."""
    interrupted: List[str] = []
    try:
        with open(JOBS_FILE) as f:
            data = json.load(f)
        for jid, job in data.items():
            if job.get("status") in ("running", "verifying"):
                # Same as pausing mid-download: back to queued; ia download
                # picks up from the files already on disk
                job["status"] = "queued"
                job["error"]  = None
                interrupted.append(jid)
            # queued and pending_meta jobs keep their status; lifespan() re-queues them
            job["downloaded"] = set(job.get("downloaded", []))
            if "file_states" not in job:
                expected = job.get("expected", [])
                job["file_states"] = {f: 100 for f in expected} if job.get("status") == "done" else {f: 0 for f in expected}
            jobs[jid] = job
    except (FileNotFoundError, json.JSONDecodeError):
        pass
    return interrupted


def history_contains(identifier: str) -> bool:
    try:
        with open(HISTORY_FILE) as f:
            return identifier in {line.strip() for line in f}
    except FileNotFoundError:
        return False


def history_add(identifier: str):
    with open(HISTORY_FILE, "a") as f:
        f.write(identifier + "\n")


def history_remove(identifier: str):
    try:
        with open(HISTORY_FILE) as f:
            lines = [line for line in f if line.strip() != identifier]
        with open(HISTORY_FILE, "w") as f:
            f.writelines(lines)
    except FileNotFoundError:
        pass


def blocklist() -> Set[str]:
    try:
        with open(BLOCK_FILE) as f:
            return {line.strip() for line in f if line.strip()}
    except FileNotFoundError:
        return set()


def blocklist_add(identifier: str):
    if identifier not in blocklist():
        with open(BLOCK_FILE, "a") as f:
            f.write(identifier + "\n")


def _auto_categorize(md: dict) -> str:
    subjects = md.get("subject", [])
    if isinstance(subjects, str):
        subjects = [subjects]
    collections = md.get("collection", [])
    if isinstance(collections, str):
        collections = [collections]
    text = " ".join([
        str(md.get("title", "")),
        str(md.get("description", "")),
        " ".join(str(s) for s in subjects),
        " ".join(str(c) for c in collections),
    ]).lower()
    if any(k in text for k in ["classical", "orchestra", "symphony", "concerto", "sonata", "baroque",
                                "opera", "chamber music", "string quartet"]):
        return "Classical"
    if any(k in text for k in ["jazz", "swing", "bebop", "bossa nova", "blues", "dixieland", "big band"]):
        return "Jazz"
    if any(k in text for k in ["anime", "japanese animation", "j-pop", "jpop", "vocaloid"]):
        return "Anime"
    if any(k in text for k in ["sound effect", "sfx", "foley", "field recording", "sound design"]):
        return "Sound Effects"
    if any(k in text for k in ["pop", "rock", "hip hop", "hip-hop", "r&b", "electronic", "folk", "country",
                                "singer-songwriter", "album", "compilation", "lp", "ep", "single"]):
        return "Music"
    return "Video Game"


def _pick(d, *keys):
    for k in keys:
        v = d.get(k)
        if v:
            return v
    return None


def _parse_meta(meta: dict, identifier: str, category: str) -> dict | None:
    """Parse ia metadata into job fields. Returns None if no audio files found."""
    md = meta.get("metadata", {})
    raw_artist = _pick(md, "creator", "artist")
    if isinstance(raw_artist, list):
        artist = "Various Artists" if len(raw_artist) > 1 else raw_artist[0]
    else:
        artist = raw_artist or "Unknown Artist"
    album  = _pick(md, "album", "title") or "Unknown Album"
    artist = re.sub(r'[/\\:*?"<>|]', '_', str(artist).strip())
    album  = re.sub(r'[/\\:*?"<>|]', '_', str(album).strip())

    files = meta.get("files", [])

    def _with_ext(*exts, alac=None):
        # Case-insensitive: some uploads use .MP3 / .FLAC. For .m4a, alac=True/False
        # splits Apple Lossless from AAC using archive.org's per-file "format" label.
        out = []
        for f in files:
            if not f.get("name", "").lower().endswith(exts):
                continue
            if alac is not None and (f.get("format") == "Apple Lossless Audio") != alac:
                continue
            out.append(f)
        return out

    # Preference order: lossless (FLAC, ALAC), then MP3, Ogg Vorbis, and AAC .m4a last
    for pick, fmt in (
        (lambda: _with_ext(".flac"),             "FLAC"),
        (lambda: _with_ext(".m4a", alac=True),   "ALAC"),
        (lambda: _with_ext(".mp3"),              "MP3"),
        (lambda: _with_ext(".ogg", ".oga"),      "OGG"),
        (lambda: _with_ext(".m4a", alac=False),  "M4A"),
    ):
        chosen = pick()
        if chosen:
            break
    else:
        # No loose audio: fall back to uploaded archives; run_download extracts them
        chosen = [f for f in files
                  if f.get("name", "").lower().endswith((".zip", ".7z"))
                  and f.get("format") in ("ZIP", "7z") and f.get("source") == "original"]
        if not chosen:
            return None
        kinds = {f["name"].rsplit(".", 1)[1].upper() for f in chosen}
        fmt = kinds.pop() if len(kinds) == 1 else "ZIP"
    # ia download's --glob is case-sensitive; match the extensions as actually
    # spelled ("|" separates alternative patterns)
    glob = "|".join(sorted({"*." + f["name"].rsplit(".", 1)[1] for f in chosen}))

    expected = [f["name"] for f in chosen]
    expected_sizes = {}
    for f in chosen:
        try:
            expected_sizes[f["name"]] = int(f.get("size", 0))
        except (ValueError, TypeError):
            expected_sizes[f["name"]] = 0

    if category == "Auto":
        category = _auto_categorize(md)
    elif category not in CATEGORIES:
        category = "Video Game"

    duplicate = history_contains(identifier)
    dest = f"{AUDIO_ROOT}/{category}/{identifier}"

    return {
        "artist": artist,
        "album":  album,
        "dest":   dest,
        "expected":       expected,
        "expected_sizes": expected_sizes,
        "downloaded":     set(),
        "file_states":    {f: 0 for f in expected},
        "glob":      glob,
        "verify":    duplicate,
        "fmt":       fmt,
        "archive":   fmt in ("ZIP", "7Z"),
        "category":  category,
        "duplicate": duplicate,
    }


META_RETRY_DELAYS = (5, 20)  # seconds between attempts when archive.org doesn't answer


async def _fetch_meta(identifier: str) -> dict | None:
    """Item metadata; {} if the item doesn't exist; None if archive.org couldn't be
    reached even after retries. Only a failed request is retried: a missing item
    comes back as {} straight away, so retrying it would only waste time."""
    for delay in (*META_RETRY_DELAYS, None):
        proc = await asyncio.create_subprocess_exec(
            "ia", "metadata", identifier,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, _ = await proc.communicate()
        if proc.returncode == 0 and stdout.strip():
            try:
                return json.loads(stdout)
            except json.JSONDecodeError:
                pass
        if delay is None:
            return None
        await asyncio.sleep(delay)


async def _expand_collection(identifier: str) -> List[str]:
    proc = await asyncio.create_subprocess_exec(
        "ia", "search", f"collection:{identifier}", "--itemlist",
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout, _ = await proc.communicate()
    return [line.strip() for line in stdout.decode().splitlines() if line.strip()]


async def _resolve_and_download(job_id: str):
    job = jobs[job_id]
    if job.get("status") == "pending_meta":
        identifier = job["identifier"]
        meta = await _fetch_meta(identifier)
        if jobs.get(job_id) is not job or job.get("status") != "pending_meta":
            return  # removed or deleted while metadata was being fetched
        if not meta:
            job["status"] = "error"
            job["error"]  = (f"No metadata for '{identifier}'" if meta == {} else
                             "archive.org didn't respond (3 tries) — Retry later")
            save_jobs()
            return
        fields = _parse_meta(meta, identifier, job.get("category", "Auto"))
        if fields is None:
            job["status"] = "error"
            job["error"]  = "No FLAC or MP3 files found"
            save_jobs()
            return
        job.update(fields)
        job["status"] = "queued"
        os.makedirs(job["dest"], exist_ok=True)
        save_jobs()

    await run_download(job_id, job["glob"], job["verify"])


async def queue_worker():
    # job_queue is only a wake-up signal (one token per enqueue); queue_list is the
    # authoritative order, so insert(0, ...) for retry/pause really runs next.
    while True:
        await job_queue.get()
        job_id = None
        try:
            # Hold here while paused — job stays visible in queue_list
            while paused:
                await asyncio.sleep(0.5)
            if not queue_list:
                continue  # stale token (e.g. job deleted before it ran)
            job_id = queue_list.pop(0)
            job = jobs.get(job_id)
            if job:
                await _resolve_and_download(job_id)
        except Exception as exc:
            job = jobs.get(job_id)
            if job:
                job["status"] = "error"
                job["error"]  = str(exc)
        finally:
            job_queue.task_done()


@asynccontextmanager
async def lifespan(app):
    interrupted = load_jobs()
    # Rebuild the queue: the interrupted download first, then the saved order,
    # then any waiting job missing from the saved file (creation order)
    waiting = {jid for jid, job in jobs.items() if job.get("status") in WAITING}
    order: List[str] = []
    seen: Set[str] = set()
    for jid in [*interrupted, *load_queue(), *jobs.keys()]:
        if jid in waiting and jid not in seen:
            order.append(jid)
            seen.add(jid)
    queue_list[:] = order
    for jid in order:
        await job_queue.put(jid)
    save_jobs()
    asyncio.create_task(queue_worker())
    yield


app = FastAPI(lifespan=lifespan)

HTML = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>IA Music</title>
<link href="https://fonts.googleapis.com/css2?family=Orbitron:wght@600;700&family=Inter:wght@400;500&display=swap" rel="stylesheet">
<style>
:root {
  --bg: #01030a;
  --surface: rgba(3,12,28,0.88);
  --border: rgba(0,210,195,0.15);
  --teal: #00d2c3;
  --orange: #FF7022;
  --text: #c8c8d2;
  --dim: rgba(200,200,210,0.45);
  --green: #22c55e;
  --red: #ef4444;
}
*{box-sizing:border-box;margin:0;padding:0;}
body{background:var(--bg);color:var(--text);font-family:'Inter',system-ui,sans-serif;min-height:100vh;}
.input-row{display:flex;gap:.5rem;margin-bottom:.85rem;}
input[type=text]{flex:1;background:var(--surface);border:1px solid var(--border);color:var(--text);padding:.65rem 1rem;font-size:.875rem;font-family:inherit;border-radius:5px;outline:none;transition:border-color .2s;}
input[type=text]::placeholder{color:var(--dim);}
input[type=text]:focus{border-color:var(--teal);}
button{background:var(--teal);color:#01030a;border:none;padding:.65rem 1.25rem;font-family:'Orbitron',sans-serif;font-size:.65rem;font-weight:700;letter-spacing:.1em;text-transform:uppercase;cursor:pointer;border-radius:5px;transition:background .2s;white-space:nowrap;}
button:hover{background:#00f0e0;}
button:disabled{opacity:.35;cursor:not-allowed;}
select{background:var(--surface);border:1px solid var(--border);color:var(--text);padding:.65rem 1rem;font-size:.875rem;font-family:inherit;border-radius:5px;outline:none;cursor:pointer;transition:border-color .2s;min-width:160px;}
select:focus{border-color:var(--teal);}
select option{background:#01030a;}
.action-row{display:flex;gap:.5rem;margin-bottom:.85rem;flex-wrap:wrap;}
.act{background:transparent;border:1px solid var(--border);color:var(--dim);padding:.4rem .85rem;font-family:'Orbitron',sans-serif;font-size:.55rem;font-weight:700;letter-spacing:.1em;text-transform:uppercase;cursor:pointer;border-radius:5px;transition:border-color .2s,color .2s;}
.act:hover{border-color:var(--teal);color:var(--teal);}
.act.retry{border-color:rgba(0,210,195,.3);color:var(--teal);}
.act.retry:hover{background:rgba(0,210,195,.08);}
.act.pause-btn{border-color:rgba(255,112,34,.35);color:var(--orange);}
.act.pause-btn:hover{background:rgba(255,112,34,.08);}
.act.pause-btn.resumed{border-color:rgba(0,210,195,.5);color:var(--teal);}
.act.pause-btn.resumed:hover{background:rgba(0,210,195,.1);}
.filter-row{display:flex;gap:.35rem;margin-bottom:1.25rem;flex-wrap:wrap;}
.filter-btn{background:transparent;border:1px solid var(--border);color:var(--dim);padding:.3rem .75rem;font-family:'Orbitron',sans-serif;font-size:.52rem;font-weight:700;letter-spacing:.1em;text-transform:uppercase;cursor:pointer;border-radius:5px;transition:border-color .15s,color .15s;display:flex;align-items:center;gap:.35rem;}
.filter-btn:hover{border-color:rgba(0,210,195,.4);color:var(--text);}
.filter-btn.active{border-color:var(--teal);color:var(--teal);}
.filter-count{border-radius:3px;padding:.05rem .3rem;font-size:.5rem;min-width:1rem;text-align:center;display:none;}
.filter-count.has-items{display:inline-block;background:rgba(0,210,195,.12);color:var(--teal);}
.filter-btn:not(.active) .filter-count.has-items{background:rgba(200,200,210,.07);color:var(--dim);}

/* grid */
#jobs{display:grid;grid-template-columns:repeat(auto-fill,minmax(200px,1fr));gap:1rem;align-items:start;}
#jobs.alpha-view>*{order:0 !important;}
.load-more-row{grid-column:1/-1;order:100000;display:flex;justify-content:center;padding:.75rem 0;}

/* card */
.job{background:var(--surface);border:1px solid var(--border);border-radius:8px;overflow:hidden;display:flex;flex-direction:column;transition:border-color .4s;content-visibility:auto;contain-intrinsic-size:0 270px;}
.job.done{border-color:rgba(34,197,94,.35);}
.job.error{border-color:rgba(239,68,68,.35);}

/* thumbnail */
.card-thumb{position:relative;aspect-ratio:1/1;background:rgba(3,12,28,.95);overflow:hidden;flex-shrink:0;}
.cover-img{width:100%;height:100%;object-fit:cover;display:block;opacity:0;transition:opacity .4s,filter .4s;filter:grayscale(1) brightness(.55);}
.cover-img.loaded{opacity:1;}
.job.done .cover-img{filter:none;}
.cover-ph{width:100%;height:100%;display:flex;align-items:center;justify-content:center;}
.cover-ph svg{opacity:.18;}
.card-overlay{position:absolute;inset:0;display:flex;align-items:center;justify-content:center;pointer-events:none;transition:background .3s;}
.job.error .card-overlay{background:rgba(239,68,68,.1);}
.overlay-icon{opacity:0;transition:opacity .3s;}
.job.error .overlay-icon{opacity:1;}
.err-retry-btn{background:none;border:none;cursor:pointer;padding:0;pointer-events:auto;display:flex;align-items:center;justify-content:center;transition:transform .15s;}
.err-retry-btn:hover{transform:scale(1.12);}
.err-retry-btn .icon-restart{display:none;}
.err-retry-btn:hover .icon-x{display:none;}
.err-retry-btn:hover .icon-restart{display:block;}
.card-prog{position:absolute;bottom:0;left:0;right:0;height:3px;background:rgba(0,0,0,.4);}
.card-prog .bar-fill{height:100%;background:var(--teal);transition:width .5s ease;width:0%;}
.job.done .card-prog .bar-fill{background:var(--green);}
.job.error .card-prog .bar-fill{background:var(--red);}
.card-x{position:absolute;top:.35rem;right:.35rem;background:rgba(0,0,0,.6);border:none;color:rgba(255,255,255,.75);width:1.5rem;height:1.5rem;border-radius:50%;font-size:.7rem;cursor:pointer;display:flex;align-items:center;justify-content:center;padding:0;transition:background .15s,color .15s;font-family:inherit;font-weight:400;letter-spacing:0;text-transform:none;min-width:unset;line-height:1;}
.card-x:hover{background:var(--red);color:#fff;}
.job.removed{border-color:rgba(200,200,210,.25);}
.job.removed .overlay-icon{opacity:1;}
.card-menu{position:fixed;z-index:300;min-width:200px;background:rgba(1,5,18,.98);border:1px solid var(--border);border-radius:8px;padding:.3rem 0;box-shadow:0 8px 24px rgba(0,0,0,.5);display:none;}
.card-menu.open{display:block;}
.card-menu button{display:block;width:100%;text-align:left;background:none;border:none;color:var(--text);padding:.6rem 1rem;font-family:'Inter',sans-serif;font-size:.8rem;font-weight:400;letter-spacing:0;text-transform:none;cursor:pointer;min-width:unset;line-height:1.3;}
.card-menu button:hover{background:rgba(0,210,195,.08);color:var(--teal);}
.card-menu button.danger{color:var(--red);}
.card-menu button.danger:hover{background:rgba(239,68,68,.1);color:var(--red);}
.q-badge{position:absolute;top:.35rem;left:.35rem;background:rgba(0,0,0,.65);color:var(--dim);font-family:'Orbitron',sans-serif;font-size:.48rem;letter-spacing:.06em;padding:.2rem .4rem;border-radius:3px;display:none;}
.q-badge.visible{display:block;}

/* card body */
.card-body{padding:.6rem .75rem;flex:1;display:flex;flex-direction:column;gap:.18rem;}
.job-title{font-size:.78rem;color:var(--orange);font-weight:500;line-height:1.3;display:-webkit-box;-webkit-line-clamp:2;-webkit-box-orient:vertical;overflow:hidden;}
.job-album{font-size:.68rem;color:var(--dim);overflow:hidden;text-overflow:ellipsis;white-space:nowrap;}
.card-tags{display:flex;gap:.3rem;flex-wrap:wrap;margin-top:.3rem;}
.badge{font-family:'Orbitron',sans-serif;font-size:.52rem;letter-spacing:.1em;padding:.18rem .45rem;border-radius:3px;font-weight:700;flex-shrink:0;}
.flac{background:rgba(0,210,195,.12);color:var(--teal);border:1px solid rgba(0,210,195,.25);}
.mp3{background:rgba(255,112,34,.12);color:var(--orange);border:1px solid rgba(255,112,34,.25);}
.ogg{background:rgba(167,139,250,.12);color:#a78bfa;border:1px solid rgba(167,139,250,.3);}
.alac{background:rgba(34,197,94,.12);color:var(--green);border:1px solid rgba(34,197,94,.28);}
.m4a{background:rgba(244,114,182,.12);color:#f472b6;border:1px solid rgba(244,114,182,.3);}
.wav{background:rgba(56,189,248,.12);color:#38bdf8;border:1px solid rgba(56,189,248,.3);}
.fmt-badge.zip,.fmt-badge[class~="7z"]{background:rgba(250,204,21,.1);color:#facc15;border:1px solid rgba(250,204,21,.3);}
.cat-badge{background:rgba(200,200,210,.07);color:var(--dim);border:1px solid rgba(200,200,210,.15);cursor:pointer;transition:border-color .15s,color .15s;}
.cat-badge:hover{border-color:rgba(200,200,210,.4);color:var(--text);}
select.cat-sel{background:rgba(200,200,210,.07);color:var(--text);border:1px solid rgba(200,200,210,.4);padding:.18rem .4rem;font-family:'Orbitron',sans-serif;font-size:.52rem;letter-spacing:.1em;font-weight:700;border-radius:3px;cursor:pointer;outline:none;flex-shrink:0;}
select.cat-sel option{background:#01030a;}
.card-foot{display:flex;justify-content:space-between;align-items:center;font-size:.65rem;color:var(--dim);margin-top:auto;padding-top:.35rem;}
.s-run{color:var(--teal);}
.s-done{color:var(--green);}
.s-err{color:var(--red);}
.s-queue{color:var(--dim);}
.dup-banner{display:flex;align-items:center;gap:.35rem;font-size:.62rem;color:#fbbf24;background:rgba(251,191,36,.06);border:1px solid rgba(251,191,36,.18);border-radius:4px;padding:.25rem .5rem;margin:.2rem 0;letter-spacing:.03em;}

/* empty / filter-empty span full grid */
.empty{grid-column:1/-1;text-align:center;color:var(--dim);font-size:.8rem;padding:3rem 0;}
.filter-empty{grid-column:1/-1;text-align:center;color:var(--dim);font-size:.8rem;padding:3rem 0;display:none;}

/* card play button */
.card-play{position:absolute;inset:0;margin:auto;background:rgba(0,0,0,.62);border:1px solid rgba(0,210,195,.5);color:var(--teal);width:2.6rem;height:2.6rem;border-radius:50%;font-size:1rem;cursor:pointer;display:none;align-items:center;justify-content:center;padding:0;font-family:inherit;font-weight:400;letter-spacing:0;text-transform:none;min-width:unset;line-height:1;opacity:0;transition:opacity .18s,background .15s,border-color .15s;}
.card-play:hover{background:rgba(0,210,195,.22);border-color:var(--teal);}
.job.done .card-play{display:flex;}
.job.done:hover .card-play{opacity:1;}
.job.done.now-playing .card-play{opacity:1;background:rgba(0,210,195,.22);border-color:var(--teal);}
.card-front{position:absolute;inset:0;margin:auto;background:rgba(0,0,0,.62);border:1px solid rgba(0,210,195,.5);color:var(--teal);width:2.6rem;height:2.6rem;border-radius:50%;font-size:1.1rem;cursor:pointer;display:none;align-items:center;justify-content:center;padding:0;font-family:inherit;font-weight:400;letter-spacing:0;text-transform:none;min-width:unset;line-height:1;opacity:0;transition:opacity .18s,background .15s,border-color .15s;}
.card-front:hover{background:rgba(0,210,195,.22);border-color:var(--teal);}
.job[data-status="queued"] .card-front,.job[data-status="pending_meta"] .card-front{display:flex;}
.job[data-status="queued"]:hover .card-front,.job[data-status="pending_meta"]:hover .card-front{opacity:1;}

/* player bar */
body.has-player{padding-bottom:76px;}
.player-bar{position:fixed;bottom:0;left:0;right:0;height:68px;background:rgba(1,5,18,.96);border-top:1px solid var(--border);display:flex;align-items:center;gap:14px;padding:0 18px;z-index:200;backdrop-filter:blur(14px);}
.player-bar.hidden{display:none;}
.pl-art{width:44px;height:44px;border-radius:4px;object-fit:cover;flex-shrink:0;}
.pl-art-ph{width:44px;height:44px;border-radius:4px;flex-shrink:0;background:rgba(3,12,28,.95);display:flex;align-items:center;justify-content:center;opacity:.5;}
.pl-info{flex:1.2;min-width:0;}
.pl-track{font-size:.77rem;color:var(--text);white-space:nowrap;overflow:hidden;text-overflow:ellipsis;}
.pl-album{font-size:.63rem;color:var(--dim);white-space:nowrap;overflow:hidden;text-overflow:ellipsis;margin-top:.15rem;}
.pl-controls{display:flex;align-items:center;gap:6px;flex-shrink:0;}
.pl-btn{background:transparent;border:none;color:var(--text);cursor:pointer;padding:4px;width:30px;height:30px;display:flex;align-items:center;justify-content:center;border-radius:4px;font-family:inherit;font-size:1rem;font-weight:400;letter-spacing:0;text-transform:none;min-width:unset;line-height:1;transition:color .15s,background .15s;}
.pl-btn:hover{color:var(--teal);background:rgba(0,210,195,.08);}
.pl-btn.pl-play{width:38px;height:38px;background:var(--teal);color:#01030a;border-radius:50%;font-size:.95rem;}
.pl-btn.pl-play:hover{background:#00f0e0;}
.pl-progress{flex:2;min-width:80px;display:flex;align-items:center;gap:7px;}
.pl-time{font-size:.58rem;color:var(--dim);flex-shrink:0;font-family:'Orbitron',sans-serif;letter-spacing:.04em;min-width:2.4rem;text-align:center;}
input[type=range].pl-seek,input[type=range].pl-vol{-webkit-appearance:none;appearance:none;height:3px;border-radius:2px;background:rgba(200,200,210,.18);outline:none;cursor:pointer;}
input[type=range].pl-seek{flex:1;}
input[type=range].pl-vol{width:64px;flex-shrink:0;}
input[type=range].pl-seek::-webkit-slider-thumb,input[type=range].pl-vol::-webkit-slider-thumb{-webkit-appearance:none;width:11px;height:11px;border-radius:50%;background:var(--teal);cursor:pointer;}
input[type=range].pl-seek::-moz-range-thumb,input[type=range].pl-vol::-moz-range-thumb{width:11px;height:11px;border-radius:50%;background:var(--teal);border:none;cursor:pointer;}
.pl-extra{display:flex;align-items:center;gap:6px;flex-shrink:0;}
.pl-icon-btn{background:transparent;border:none;color:var(--dim);cursor:pointer;font-size:.9rem;padding:4px 5px;border-radius:4px;font-family:inherit;font-weight:400;letter-spacing:0;text-transform:none;min-width:unset;line-height:1;transition:color .15s;}
.pl-icon-btn.on{color:var(--teal);}
.pl-icon-btn:hover{color:var(--text);}

/* topbar */
.topbar{display:flex;align-items:center;gap:.75rem;padding:.75rem 1.5rem;border-bottom:1px solid var(--border);position:sticky;top:0;background:var(--bg);z-index:10;}
.topbar h1{flex-shrink:0;}
.search-wrap{flex:1;max-width:340px;margin-left:auto;position:relative;display:flex;align-items:center;}
.search-wrap svg{position:absolute;left:.65rem;color:var(--dim);pointer-events:none;flex-shrink:0;}
#search-input,#search-input-mobile{width:100%;background:rgba(3,12,28,.8);border:1px solid var(--border);color:var(--text);padding:.45rem .75rem .45rem 2rem;font-size:.8rem;font-family:'Inter',sans-serif;border-radius:5px;outline:none;transition:border-color .2s;}
#search-input::placeholder,#search-input-mobile::placeholder{color:var(--dim);}
#search-input:focus,#search-input-mobile:focus{border-color:var(--teal);}
.search-clear{position:absolute;right:.5rem;background:none;border:none;color:var(--dim);cursor:pointer;font-size:.85rem;padding:0 .2rem;display:none;min-width:unset;font-family:inherit;font-weight:400;letter-spacing:0;text-transform:none;line-height:1;}
.search-clear.visible{display:block;}
.search-clear:hover{color:var(--text);}
@media(max-width:600px){
  .search-wrap{display:none;}
  .topbar-search-mobile{display:flex;align-items:center;padding:.5rem 1rem;border-bottom:1px solid rgba(0,210,195,.07);position:sticky;top:57px;background:var(--bg);z-index:9;}
  .topbar-search-mobile .search-wrap{display:flex;max-width:100%;width:100%;margin:0;}
}
@media(min-width:601px){
  .topbar-search-mobile{display:none;}
}
/* phones: two or more album columns, tighter cards, compact player */
@media(max-width:600px){
  .topbar{padding-left:1rem;padding-right:1rem;}
  .filter-strip{padding:.5rem .75rem;}
  .grid-area{padding:.75rem;}
  #jobs{grid-template-columns:repeat(auto-fill,minmax(150px,1fr));gap:.6rem;}
  .card-body{padding:.45rem .55rem;}
  .job-title{font-size:.72rem;}
  .job-album{font-size:.64rem;}
  .card-tags{gap:.25rem;margin-top:.2rem;}
  .badge{font-size:.46rem;letter-spacing:.06em;padding:.15rem .35rem;}
  .card-foot{font-size:.6rem;}
  .dup-banner{font-size:.56rem;padding:.2rem .4rem;}
  .card-x{width:1.7rem;height:1.7rem;}
  .card-menu button{padding:.8rem 1rem;font-size:.9rem;}
  /* iOS zooms the page when focusing an input under 16px */
  #search-input-mobile{font-size:16px;}
  /* player: seek bar becomes a strip along the top edge; volume is hidden
     because iOS ignores page-set volume (hardware buttons only) */
  .player-bar{gap:8px;padding:0 10px;}
  .pl-progress{position:absolute;top:-6px;left:0;right:0;min-width:0;height:12px;}
  .pl-time,.pl-vol{display:none;}
  input[type=range].pl-seek::-webkit-slider-thumb{width:14px;height:14px;}
}
/* touch screens have no hover: show card actions instead of hiding them behind it */
@media(hover:none){
  .job.done .card-play{opacity:.9;}
  .job[data-status="queued"] .card-front,.job[data-status="pending_meta"] .card-front{opacity:.75;}
  .err-retry-btn .icon-x{display:none;}
  .err-retry-btn .icon-restart{display:block;}
}
h1{font-family:'Orbitron',sans-serif;font-size:1rem;letter-spacing:.12em;text-transform:uppercase;color:var(--orange);display:flex;align-items:center;gap:.6rem;}
.hamburger{background:transparent;border:1px solid var(--border);color:var(--dim);width:36px;height:36px;border-radius:6px;cursor:pointer;display:flex;align-items:center;justify-content:center;font-size:1rem;font-family:inherit;font-weight:400;letter-spacing:0;text-transform:none;min-width:unset;line-height:1;padding:0;transition:border-color .15s,color .15s;flex-shrink:0;}
.hamburger:hover{border-color:var(--teal);color:var(--teal);}
/* filter strip */
.filter-strip{padding:.6rem 1.5rem;border-bottom:1px solid rgba(0,210,195,.07);display:flex;gap:.35rem;flex-wrap:wrap;}
/* grid area */
.grid-area{padding:1rem 1.5rem;}
/* drawer overlay */
.drawer-overlay{position:fixed;inset:0;background:rgba(0,0,0,.5);z-index:150;opacity:0;pointer-events:none;transition:opacity .22s;}
.drawer-overlay.open{opacity:1;pointer-events:auto;}
/* drawer */
.drawer{position:fixed;top:0;right:0;bottom:0;width:300px;max-width:88vw;background:rgba(1,5,18,.98);border-left:1px solid var(--border);z-index:160;transform:translateX(100%);transition:transform .25s cubic-bezier(.4,0,.2,1);overflow-y:auto;display:flex;flex-direction:column;gap:.9rem;padding:1.25rem;backdrop-filter:blur(12px);}
.drawer.open{transform:translateX(0);}
.drawer-head{display:flex;align-items:center;justify-content:space-between;}
.drawer-title{font-family:'Orbitron',sans-serif;font-size:.68rem;letter-spacing:.14em;text-transform:uppercase;color:var(--teal);}
.drawer-close{background:transparent;border:none;color:var(--dim);cursor:pointer;font-size:1.1rem;padding:4px 6px;border-radius:4px;font-family:inherit;font-weight:400;letter-spacing:0;text-transform:none;min-width:unset;line-height:1;transition:color .15s;}
.drawer-close:hover{color:var(--text);}
.drawer hr{border:none;border-top:1px solid var(--border);margin:.1rem 0;}
.drawer-section{font-family:'Orbitron',sans-serif;font-size:.55rem;letter-spacing:.12em;text-transform:uppercase;color:var(--dim);margin-bottom:.5rem;}
/* iOS-style ring overlay on cover art */
.prog-ring-overlay{position:absolute;inset:0;display:flex;align-items:center;justify-content:center;pointer-events:auto;}
.prog-ring-svg{position:absolute;inset:0;width:100%;height:100%;pointer-events:none;}
.prog-ring-bg{fill:none;stroke:rgba(0,0,0,.35);stroke-width:5;}
.prog-ring-fill{fill:none;stroke:var(--teal);stroke-width:5;stroke-linecap:round;transform-origin:50% 50%;transform:rotate(-90deg);transition:stroke-dashoffset .6s ease;}
.prog-ring-btn{position:relative;z-index:1;background:rgba(0,0,0,.5);border:1px solid rgba(255,255,255,.18);color:#fff;width:2.4rem;height:2.4rem;border-radius:50%;cursor:pointer;display:flex;align-items:center;justify-content:center;font-size:1rem;font-family:inherit;font-weight:400;letter-spacing:0;text-transform:none;min-width:unset;line-height:1;transition:background .15s;padding:0;}
.prog-ring-btn:hover{background:rgba(0,0,0,.7);}
.has-ring .card-prog{opacity:0;}

.drawer-q-idle{font-size:.7rem;color:var(--dim);padding:.3rem 0;}
.drawer-q-now{display:flex;gap:.5rem;padding:.35rem 0 .5rem;}
.drawer-q-now-label{font-family:'Orbitron',sans-serif;font-size:.5rem;letter-spacing:.12em;text-transform:uppercase;color:var(--teal);flex-shrink:0;padding-top:.18rem;}
.drawer-q-now-info{flex:1;min-width:0;}
.drawer-q-now-titlerow{display:flex;justify-content:space-between;gap:.4rem;align-items:baseline;}
.drawer-q-now-name{font-size:.68rem;color:var(--text);overflow:hidden;text-overflow:ellipsis;white-space:nowrap;}
.drawer-q-now-pct{font-size:.55rem;color:var(--teal);flex-shrink:0;font-family:'Orbitron',sans-serif;letter-spacing:.04em;}
.drawer-q-now-bar{height:2px;background:rgba(200,200,210,.12);border-radius:1px;margin-top:.35rem;}
.drawer-q-now-fill{height:100%;background:var(--teal);border-radius:1px;transition:width .5s ease;width:0%;}
.drawer-q-next{display:flex;align-items:baseline;gap:.5rem;padding:.35rem 0 .35rem;border-top:1px solid rgba(255,255,255,.06);}
.drawer-q-next-label{font-family:'Orbitron',sans-serif;font-size:.5rem;letter-spacing:.12em;text-transform:uppercase;color:var(--dim);flex-shrink:0;}
.drawer-q-next-title{font-size:.68rem;color:var(--dim);white-space:nowrap;overflow:hidden;text-overflow:ellipsis;}
.drawer-q-count{font-size:.6rem;color:var(--dim);margin-top:.2rem;margin-bottom:.6rem;font-family:'Orbitron',sans-serif;letter-spacing:.05em;}
.drawer-q-failed{display:flex;align-items:center;justify-content:space-between;gap:.5rem;padding:.5rem 0 .3rem;border-top:1px solid rgba(255,255,255,.06);margin-top:.2rem;}
.drawer-q-failed-count{font-size:.6rem;color:#ff5a5a;font-family:'Orbitron',sans-serif;letter-spacing:.05em;}
</style>
</head>
<body>
<!-- topbar -->
<div class="topbar">
  <h1>
    <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.2"><path d="M9 18V5l12-2v13"/><circle cx="6" cy="18" r="3"/><circle cx="18" cy="16" r="3"/></svg>
    IA Music
  </h1>
  <div class="search-wrap">
    <svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.3"><circle cx="11" cy="11" r="8"/><path d="M21 21l-4.35-4.35"/></svg>
    <input id="search-input" type="text" placeholder="Search albums, artists…" autocomplete="off" oninput="onSearch(this.value)">
    <button class="search-clear" id="search-clear" onclick="clearSearch()" title="Clear">✕</button>
  </div>
  <button class="hamburger" onclick="toggleDrawer()" title="Menu" aria-label="Open menu">☰</button>
</div>
<!-- search bar — mobile only (below topbar) -->
<div class="topbar-search-mobile">
  <div class="search-wrap">
    <svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.3"><circle cx="11" cy="11" r="8"/><path d="M21 21l-4.35-4.35"/></svg>
    <input id="search-input-mobile" type="text" placeholder="Search albums, artists…" autocomplete="off" oninput="onSearch(this.value)">
    <button class="search-clear" id="search-clear-mobile" onclick="clearSearch()" title="Clear">✕</button>
  </div>
</div>

<!-- filter strip (always visible) -->
<div class="filter-strip">
  <button class="filter-btn active" data-filter="all" onclick="setFilter('all')">All<span class="filter-count" id="fc-all"></span></button>
  <button class="filter-btn" data-filter="active" onclick="setFilter('active')">Active<span class="filter-count" id="fc-active"></span></button>
  <button class="filter-btn" data-filter="completed" onclick="setFilter('completed')">Completed<span class="filter-count" id="fc-completed"></span></button>
  <button class="filter-btn" data-filter="failed" onclick="setFilter('failed')">Failed<span class="filter-count" id="fc-failed"></span></button>
  <button class="filter-btn" data-filter="hidden" onclick="setFilter('hidden')">Hidden<span class="filter-count" id="fc-hidden"></span></button>
</div>

<!-- card grid -->
<div class="grid-area">
  <div id="jobs"><p class="empty">No downloads yet.</p></div>
</div>

<!-- drawer overlay -->
<div class="drawer-overlay" id="drawer-overlay" onclick="closeDrawer()"></div>

<!-- drawer -->
<div class="drawer" id="drawer">
  <div class="drawer-head">
    <span class="drawer-title">IA Music</span>
    <button class="drawer-close" onclick="closeDrawer()" title="Close">✕</button>
  </div>
  <hr>
  <div>
    <div class="drawer-section">Download</div>
    <div class="input-row" style="margin-bottom:.6rem">
      <input type="text" id="url" placeholder="https://archive.org/details/…">
    </div>
    <div class="input-row">
      <select id="cat">
        <option value="Auto" selected>Auto-detect</option>
        <option value="Video Game">Video Game</option>
        <option value="Music">Music</option>
        <option value="Classical">Classical</option>
        <option value="Jazz">Jazz</option>
        <option value="Anime">Anime</option>
        <option value="Sound Effects">Sound Effects</option>
      </select>
      <button id="btn" onclick="go()">Download</button>
    </div>
  </div>
  <hr>
  <div>
    <div class="drawer-section">Queue</div>
    <div id="drawer-q-now" class="drawer-q-now" style="display:none">
      <span class="drawer-q-now-label">Now</span>
      <div class="drawer-q-now-info">
        <div class="drawer-q-now-titlerow">
          <span id="drawer-q-now-name" class="drawer-q-now-name"></span>
          <span id="drawer-q-now-pct"  class="drawer-q-now-pct"></span>
        </div>
        <div class="drawer-q-now-bar"><div id="drawer-q-now-fill" class="drawer-q-now-fill"></div></div>
      </div>
    </div>
    <div id="drawer-q-next" class="drawer-q-next" style="display:none">
      <span class="drawer-q-next-label">Next</span>
      <span id="drawer-q-next-title" class="drawer-q-next-title"></span>
    </div>
    <div id="drawer-q-idle" class="drawer-q-idle" style="display:none">Queue empty</div>
    <div id="drawer-q-count" class="drawer-q-count" style="display:none"></div>
    <div id="drawer-q-failed" class="drawer-q-failed" style="display:none">
      <span id="drawer-q-failed-count" class="drawer-q-failed-count"></span>
      <button class="act retry" onclick="retryFailed()">Retry Failed</button>
    </div>
  </div>
</div>

<div id="card-menu" class="card-menu"></div>
<div id="player-bar" class="player-bar hidden">
  <img id="pl-art" class="pl-art" src="" alt="" style="display:none">
  <div id="pl-art-ph" class="pl-art-ph"><svg width="22" height="22" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5"><path d="M9 18V5l12-2v13"/><circle cx="6" cy="18" r="3"/><circle cx="18" cy="16" r="3"/></svg></div>
  <div class="pl-info">
    <div class="pl-track" id="pl-track">—</div>
    <div class="pl-album" id="pl-album">—</div>
  </div>
  <div class="pl-controls">
    <button class="pl-btn" onclick="playerPrev()" title="Previous">⏮</button>
    <button class="pl-btn pl-play" id="pl-play" onclick="playerToggle()" title="Play / Pause">▶</button>
    <button class="pl-btn" onclick="playerNext()" title="Next">⏭</button>
  </div>
  <div class="pl-progress">
    <span class="pl-time" id="pl-cur">0:00</span>
    <input type="range" class="pl-seek" id="pl-seek" min="0" max="100" value="0" step="0.1">
    <span class="pl-time" id="pl-dur">0:00</span>
  </div>
  <div class="pl-extra">
    <button class="pl-icon-btn" id="pl-shuffle" onclick="toggleShuffle()" title="Shuffle">⇄</button>
    <button class="pl-icon-btn" id="pl-repeat" onclick="cycleRepeat()" title="Repeat">↺</button>
    <input type="range" class="pl-vol" id="pl-vol" min="0" max="1" step="0.01" value="1">
  </div>
</div>
<audio id="audio"></audio>

<script>
const urlEl = document.getElementById('url');
const catEl = document.getElementById('cat');
const btn   = document.getElementById('btn');
const jobsEl= document.getElementById('jobs');
jobsEl.classList.add('alpha-view'); // default filter is 'all'

urlEl.addEventListener('keydown', e => { if(e.key==='Enter') go(); });

/* ── lazy-load observer ── */
const imgObserver = new IntersectionObserver(entries => {
  entries.forEach(entry => {
    if(!entry.isIntersecting) return;
    const img = entry.target;
    const src = img.dataset.src;
    if(src) {
      img.src = src;
      img.onload  = () => img.classList.add('loaded');
      img.onerror = () => img.classList.add('loaded');
      delete img.dataset.src;
    }
    imgObserver.unobserve(img);
  });
}, { rootMargin: '200px' });

/* ── data store ── */
const jobData = new Map();
const _fc = {all:0, active:0, completed:0, failed:0, hidden:0};

function _fcDelta(d, sign) {
  if(d.hidden) { _fc.hidden += sign; return; }
  const s = d.status;
  _fc.all += sign;
  if(['queued','pending_meta','running','verifying','fetching'].includes(s||'')) _fc.active  += sign;
  if(s==='done')  _fc.completed += sign;
  if(s==='error') _fc.failed    += sign;
}
function _storeSet(id, data) {
  const old = jobData.get(id);
  if(old) _fcDelta(old, -1);
  jobData.set(id, data);
  _fcDelta(data, +1);
}
function _storeDelete(id) {
  const old = jobData.get(id);
  if(old) _fcDelta(old, -1);
  jobData.delete(id);
}
function _updateCountBadges() {
  ['all','active','completed','failed','hidden'].forEach(f => {
    const el = document.getElementById('fc-'+f);
    if(!el) return;
    const n = _fc[f];
    el.textContent = n||'';
    el.classList.toggle('has-items', n>0);
  });
  const failedRow   = document.getElementById('drawer-q-failed');
  const failedCount = document.getElementById('drawer-q-failed-count');
  if(failedRow) failedRow.style.display = _fc.failed > 0 ? '' : 'none';
  if(failedCount) failedCount.textContent = `${_fc.failed} failed`;
}
// compat shims
function updateFilterCounts() { _updateCountBadges(); }
function applyFilter() {}

/* ── filter + search ── */
let currentFilter = 'all';
let currentSearch = '';
const VIEW_SIZE   = 200;

function matchesFilter(d, f) {
  if(f==='hidden') return !!d.hidden;
  if(d.hidden) return false;
  if(f==='all') return true;
  const s = d.status||'';
  if(f==='active')    return ['queued','pending_meta','running','verifying','fetching'].includes(s);
  if(f==='completed') return s==='done';
  if(f==='failed')    return s==='error';
  return true;
}

function matchesSearch(d) {
  if(!currentSearch) return true;
  const q = currentSearch.toLowerCase();
  return (d.artist||'').toLowerCase().includes(q) ||
         (d.album||'').toLowerCase().includes(q)  ||
         (d.identifier||'').toLowerCase().includes(q);
}

function setFilter(f) {
  currentFilter = f;
  document.querySelectorAll('.filter-btn').forEach(b => b.classList.toggle('active', b.dataset.filter===f));
  jobsEl.classList.toggle('alpha-view', f === 'all');
  rebuildView();
}

function onSearch(val) {
  currentSearch = val.trim();
  // keep both inputs in sync
  document.getElementById('search-input').value = val;
  document.getElementById('search-input-mobile').value = val;
  ['search-clear','search-clear-mobile'].forEach(id => {
    document.getElementById(id)?.classList.toggle('visible', currentSearch.length > 0);
  });
  rebuildView();
}

function clearSearch() {
  onSearch('');
}

/* ── view ── */
function _albumKey(d) {
  return (d.album || d.artist || d.identifier || '').toLowerCase();
}

function _sortedMatching() {
  const entries = [...jobData.entries()]
    .filter(([,d]) => matchesFilter(d, currentFilter) && matchesSearch(d));
  if(currentFilter === 'all') {
    return entries.sort((a,b) => _albumKey(a[1]).localeCompare(_albumKey(b[1])));
  }
  return entries.sort((a,b) => calcOrder(a[1].status||'',a[1].queue_pos||0) - calcOrder(b[1].status||'',b[1].queue_pos||0));
}

function rebuildView() {
  jobsEl.querySelectorAll('.cover-img').forEach(img => imgObserver.unobserve(img));
  jobsEl.innerHTML = '';
  const matching = _sortedMatching();
  if(!matching.length) {
    const emptyMsg = currentSearch
      ? `No results for "${currentSearch}".`
      : `No ${currentFilter==='all'?'':currentFilter+' '}downloads yet.`;
    jobsEl.innerHTML = `<p class="empty">${emptyMsg}</p>`;
    _updateCountBadges(); return;
  }
  const frag = document.createDocumentFragment();
  matching.slice(0, VIEW_SIZE).forEach(([id,d]) => frag.appendChild(_makeCardEl(id,d)));
  jobsEl.appendChild(frag);
  jobsEl.querySelectorAll('.cover-img[data-src]').forEach(img => imgObserver.observe(img));
  _appendLoadMore(matching.length, VIEW_SIZE);
  _updateCountBadges();
}

function _appendLoadMore(total, shown) {
  const rem = total - shown;
  if(rem <= 0) return;
  const div = document.createElement('div');
  div.id = 'load-more'; div.className = 'load-more-row';
  div.innerHTML = `<button class="act" onclick="loadMoreCards()">Load ${Math.min(VIEW_SIZE,rem)} more &middot; ${rem} remaining</button>`;
  jobsEl.appendChild(div);
}

function loadMoreCards() {
  const shown = jobsEl.querySelectorAll('.job').length;
  const matching = _sortedMatching();
  document.getElementById('load-more')?.remove();
  const frag = document.createDocumentFragment();
  matching.slice(shown, shown+VIEW_SIZE).forEach(([id,d]) => frag.appendChild(_makeCardEl(id,d)));
  jobsEl.appendChild(frag);
  jobsEl.querySelectorAll('.cover-img[data-src]').forEach(img => imgObserver.observe(img));
  _appendLoadMore(matching.length, shown + Math.min(VIEW_SIZE, matching.length-shown));
}

/* ── sort via CSS order (no DOM shuffling) ── */
function calcOrder(status, queuePos) {
  if(status==='running'||status==='verifying') return 0;
  if(status==='fetching') return 50;
  if(status==='pending_meta'||status==='queued') {
    const pos = parseInt(queuePos, 10) || 0;
    return pos > 0 ? 100 + pos : 900;  // assigned positions 101–899; unpositioned stubs 900
  }
  if(status==='done')  return 1000;
  if(status==='removed') return 1500;
  if(status==='error') return 2000;
  return 500;
}

/* ── download ── */
let _collectionInProgress = false;

async function go() {
  const url = urlEl.value.trim();
  if(!url) return;
  btn.disabled = true;
  _collectionInProgress = false;

  const pendingId = 'p-' + Date.now();
  const rawId = url.split('/').filter(Boolean).pop();
  addCard(pendingId, {artist:'Resolving…', album:rawId, fmt:'—', cat:catEl.value, identifier:'', pct:0, status:'fetching', count:0, total:0});

  try {
    const res = await fetch('/api/download', {
      method:'POST', headers:{'Content-Type':'application/json'},
      body: JSON.stringify({url, category: catEl.value})
    });
    const data = await res.json();
    if(!res.ok) { patchCard(pendingId, {status:'error', msg:data.detail||'Error'}); btn.disabled=false; return; }

    removeCard(pendingId);

    if(data.collection) {
      _collectionInProgress = true;
      let i = 0;
      function batch() {
        const end = Math.min(i + 20, data.jobs.length);
        for(; i < end; i++) {
          const j = data.jobs[i];
          addCard(j.job_id, {artist:'…', album:j.identifier, fmt:'—', cat:j.category,
                             identifier:j.identifier, duplicate:false, pct:0, status:'pending_meta', count:0, total:0});
        }
        if(i < data.jobs.length) {
          requestAnimationFrame(batch);
        } else {
          urlEl.value='';
          btn.disabled=false;
          closeDrawer();
        }
      }
      requestAnimationFrame(batch);
    } else {
      addCard(data.job_id, {artist:data.artist, album:data.album, fmt:data.format, cat:data.category,
                            identifier:data.identifier||'', duplicate:data.duplicate, pct:0, status:'queued', count:0, total:data.total});
      urlEl.value='';
      btn.disabled=false;
      closeDrawer();
    }
  } catch(e) {
    patchCard(pendingId, {status:'error', msg:e.message});
    btn.disabled=false;
  }
}

// One shared progress stream for every job (one EventSource per job ran out of
// connections over HTTP/1.1). Messages carry changed jobs and/or the queue order.
let _progressES = null;
let _queuePos = new Map();

function startProgressStream() {
  if(_progressES) return;
  const es = new EventSource('/api/progress');
  _progressES = es;
  es.onmessage = e => {
    const m = JSON.parse(e.data);
    if(m.queue) _applyQueueOrder(m.queue);
    if(m.jobs) m.jobs.forEach(d => {
      patchCard(d.job_id, {pct:d.pct, count:d.count, total:d.total, status:d.status, msg:d.error||'',
                           queue_pos:_queuePos.get(d.job_id)||0, artist:d.artist, album:d.album,
                           category:d.category, fmt:d.fmt});
    });
  };
  es.onerror = () => { es.close(); _progressES = null; setTimeout(startProgressStream, 2000); };
}

function _applyQueueOrder(order) {
  const next = new Map(order.map((id, i) => [id, i+1]));
  _queuePos.forEach((_, id) => { if(!next.has(id)) _setQueuePos(id, 0); });
  next.forEach((pos, id) => _setQueuePos(id, pos));
  _queuePos = next;
  _refreshDrawerActive();
}

// Poll every 4 s for the pause state (progress itself arrives over the stream)
setInterval(async () => {
  try {
    const res = await fetch('/api/status');
    const data = await res.json();
    applyPauseState(data.paused);
  } catch(e) {}
}, 4000);

function cardId(id) { return 'j-'+id; }

const MUSIC_NOTE_SVG = `<svg width="64" height="64" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.2" stroke-linecap="round" stroke-linejoin="round"><path d="M9 18V5l12-2v13"/><circle cx="6" cy="18" r="3"/><circle cx="18" cy="16" r="3"/></svg>`;
const DUP_SVG = `<svg width="10" height="10" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5"><path d="M10.29 3.86L1.82 18a2 2 0 001.71 3h16.94a2 2 0 001.71-3L13.71 3.86a2 2 0 00-3.42 0z"/><line x1="12" y1="9" x2="12" y2="13"/><line x1="12" y1="17" x2="12.01" y2="17"/></svg>`;
const DONE_SVG  = `<svg width="40" height="40" viewBox="0 0 24 24" fill="none" stroke="var(--green)" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><circle cx="12" cy="12" r="10"/><polyline points="8,12.5 11,15.5 16,9"/></svg>`;
const ERR_SVG     = `<svg class="icon-x" width="36" height="36" viewBox="0 0 24 24" fill="none" stroke="var(--red)" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><circle cx="12" cy="12" r="10"/><line x1="15" y1="9" x2="9" y2="15"/><line x1="9" y1="9" x2="15" y2="15"/></svg>`;
const RESTART_SVG = `<svg class="icon-restart" width="36" height="36" viewBox="0 0 24 24" fill="none" stroke="var(--teal)" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M3 12a9 9 0 1 0 9-9 9.75 9.75 0 0 0-6.74 2.74L3 8"/><path d="M3 3v5h5"/></svg>`;
function _errOiHtml(id) {
  return `<button class="err-retry-btn" onclick="retryJob('${id}')" title="Retry">${ERR_SVG}${RESTART_SVG}</button>`;
}

function _cardFootState(d) {
  const s = d.status||'';
  if(s==='done')    return ['s-done',  'Complete'];
  if(s==='error')   return ['s-err',   d.error||d.msg||'Error'];
  if(s==='removed') return ['s-queue', 'Files removed'];
  if(s==='running'||s==='verifying') return ['s-run', d.pct>0?`${d.pct}%`:(s==='verifying'?'Verifying…':'Starting…')];
  if(s==='queued')  return ['s-queue', (d.queue_pos||0)>0?`Queued · #${d.queue_pos}`:'Queued…'];
  if(s==='fetching'||s==='pending_meta') return ['s-queue','Resolving…'];
  return ['s-queue','Queued…'];
}

function _makeCardEl(id, d) {
  const el = document.createElement('div');
  el.className = 'job';
  el.id = cardId(id);
  el.dataset.status   = d.status||'';
  el.dataset.queuePos = String(d.queue_pos||0);
  el.style.order      = calcOrder(d.status||'', d.queue_pos||0);
  if(d.status==='done')  el.classList.add('done');
  if(d.status==='error') el.classList.add('error');
  if(d.status==='removed') el.classList.add('removed');
  if(playerJobId===id)   el.classList.add('now-playing');

  const imgUrl = d.identifier ? `https://archive.org/services/img/${encodeURIComponent(d.identifier)}` : '';
  const thumb  = imgUrl ? `<img class="cover-img" data-src="${imgUrl}" alt="">` : `<div class="cover-ph">${MUSIC_NOTE_SVG}</div>`;
  const dupHtml  = d.duplicate ? `<div class="dup-banner">${DUP_SVG}Already in library</div>` : '';
  const oiHtml   = (d.status==='error'||d.status==='removed') ? _errOiHtml(id) : '';
  const qbVis    = (d.queue_pos||0)>0;
  const [footCls, footTxt] = _cardFootState(d);
  const ct       = d.total ? `${d.count||0} / ${d.total}` : '';

  el.innerHTML = `
    <div class="card-thumb">
      ${thumb}
      <div class="card-overlay"><span class="overlay-icon" id="${cardId(id)}-oi">${oiHtml}</span></div>
      <div class="card-prog"><div class="bar-fill" style="width:${d.pct||0}%"></div></div>
      <button class="card-x" onclick="openCardMenu(event,'${id}')" title="Options">✕</button>
      <button class="card-play" onclick="playAlbum('${id}')" title="Play">▶</button>
      <button class="card-front" onclick="moveToFront('${id}')" title="Move to front of queue">⤒</button>
      <span class="q-badge${qbVis?' visible':''}" id="${cardId(id)}-qb">${qbVis?'#'+(d.queue_pos):''}</span>
    </div>
    <div class="card-body">
      ${dupHtml}
      <div class="job-title">${esc(d.artist||'…')}</div>
      <div class="job-album">${esc(d.album||'')}</div>
      <div class="card-tags">
        <span class="badge cat-badge" onclick="editCategory('${id}')">${esc(d.cat||d.category||'')}</span>
        <span class="badge fmt-badge ${(d.fmt||'').toLowerCase()}">${esc(d.fmt||'—')}</span>
      </div>
      <div class="card-foot">
        <span class="${footCls}">${esc(footTxt)}</span>
        <span class="job-ct">${ct}</span>
      </div>
    </div>`;
  // Attach ring immediately if already running (e.g. after rebuildView)
  if(d.status==='running'||d.status==='verifying') {
    requestAnimationFrame(() => _updateRing(id, d.pct||0, _paused));
  }
  return el;
}

function addCard(id, d) {
  const merged = {...(jobData.get(id)||{}), ...d, job_id:id};
  _storeSet(id, merged);
  document.querySelector('.empty')?.remove();
  if(!document.getElementById(cardId(id)) && matchesFilter(merged, currentFilter)) {
    const el = _makeCardEl(id, merged);
    const lm = document.getElementById('load-more');
    if(lm) lm.before(el); else jobsEl.prepend(el);
    const img = el.querySelector('.cover-img[data-src]');
    if(img) imgObserver.observe(img);
  }
}

function patchCard(id, d) {
  // Always update the store
  const merged = {...(jobData.get(id)||{}), ...d, job_id:id};
  _storeSet(id, merged);
  _updateCountBadges();
  if(d.status || d.pct !== undefined) _refreshDrawerActive();

  const el = document.getElementById(cardId(id));
  if(!el) {
    // Not in DOM. If new status now matches the current filter and search, insert it.
    if(d.status && matchesFilter(merged, currentFilter) && matchesSearch(merged)) {
      const newEl = _makeCardEl(id, merged);
      const lm = document.getElementById('load-more');
      if(lm) lm.before(newEl); else jobsEl.prepend(newEl);
      const img = newEl.querySelector('.cover-img[data-src]');
      if(img) imgObserver.observe(img);
      document.querySelector('.empty')?.remove();
    }
    return;
  }

  if(d.artist) { const t=el.querySelector('.job-title'); if(t) t.textContent=d.artist; }
  if(d.album)  { const a=el.querySelector('.job-album'); if(a) a.textContent=d.album; }
  if(d.fmt && d.fmt!=='—') {
    const fb=el.querySelector('.fmt-badge');
    if(fb){ fb.textContent=d.fmt; fb.className=`badge fmt-badge ${d.fmt.toLowerCase()}`; }
  }
  if(d.category) {
    const cb=el.querySelector('.cat-badge');
    if(cb && cb.tagName==='SPAN') cb.textContent=d.category;
  }

  if(d.pct!==undefined) el.querySelector('.bar-fill').style.width=d.pct+'%';
  if(d.count!==undefined && d.total!==undefined)
    el.querySelector('.job-ct').textContent = d.total ? `${d.count} / ${d.total}` : '';

  const qb = document.getElementById(cardId(id)+'-qb');
  if(qb) {
    if((d.queue_pos||0) > 0){ qb.textContent='#'+d.queue_pos; qb.classList.add('visible'); }
    else qb.classList.remove('visible');
  }

  const oi  = document.getElementById(cardId(id)+'-oi');
  const msg = el.querySelector('.card-foot span:first-child');

  if(d.status) {
    // Only the current state's class may remain (a retried card used to stay red)
    el.classList.toggle('done',    d.status==='done');
    el.classList.toggle('error',   d.status==='error');
    el.classList.toggle('removed', d.status==='removed');
    if(oi && d.status!=='error' && d.status!=='removed') oi.innerHTML='';
  }

  if(d.status==='done') {
    el.classList.add('done');
    if(oi) oi.innerHTML='';
    if(msg){ msg.className='s-done'; msg.textContent='Complete'; }
    _removeRing(id);
  } else if(d.status==='error') {
    el.classList.add('error');
    if(oi) oi.innerHTML=_errOiHtml(id);
    if(msg){ msg.className='s-err'; msg.textContent=d.msg||'Error'; }
    _removeRing(id);
  } else if(d.status==='removed') {
    if(oi) oi.innerHTML=_errOiHtml(id);
    if(msg){ msg.className='s-queue'; msg.textContent='Files removed'; }
    el.querySelector('.bar-fill').style.width='0%';
    el.querySelector('.job-ct').textContent='';
    _removeRing(id);
  } else if(d.status==='running'||d.status==='verifying') {
    if(msg){ msg.className='s-run'; msg.textContent=d.pct>0?`${d.pct}%`:(d.status==='verifying'?'Verifying…':'Starting…'); }
    _updateRing(id, merged.pct||0, _paused);
  } else if(d.status==='queued') {
    if(msg){ msg.className='s-queue'; msg.textContent=(d.queue_pos||0)>0?`Queued · #${d.queue_pos}`:'Queued…'; }
    _removeRing(id);
  } else if(d.status==='fetching'||d.status==='pending_meta') {
    if(msg){ msg.className='s-queue'; msg.textContent='Resolving…'; }
    _removeRing(id);
  }

  // Update ring progress even without a status change (pct tick while running)
  if(d.pct !== undefined && !d.status && document.getElementById('ring-ov-'+id)) {
    _updateRing(id, d.pct, _paused);
  }

  if(d.queue_pos !== undefined) el.dataset.queuePos = String(d.queue_pos || 0);
  if(d.status) {
    el.dataset.status = d.status;
    if(currentFilter === 'all') {
      // Alpha view: cards stay in DOM position; no status-based reorder or removal
    } else if(d.status === 'done') {
      // Delay reorder so the card doesn't jump while the queue turns over
      setTimeout(() => {
        const live = document.getElementById(cardId(id));
        if(!live) return;
        live.style.order = calcOrder('done', 0);
        if(!matchesFilter({...(jobData.get(id)||{}), status:'done'}, currentFilter)) {
          const img = live.querySelector('.cover-img');
          if(img) imgObserver.unobserve(img);
          live.remove();
        }
      }, 6000);
    } else {
      el.style.order = calcOrder(d.status, d.queue_pos||el.dataset.queuePos||0);
      if(!matchesFilter(merged, currentFilter)) {
        const img = el.querySelector('.cover-img');
        if(img) imgObserver.unobserve(img);
        el.remove();
      }
    }
  }
}

function editCategory(jobId) {
  const el = document.getElementById(cardId(jobId));
  if(!el) return;
  const msg = el.querySelector('.card-foot span:first-child');
  if(msg && msg.classList.contains('s-run')) return;

  const badge = el.querySelector('.cat-badge');
  if(!badge || badge.tagName!=='SPAN') return;
  const current = badge.textContent.trim();

  const sel = document.createElement('select');
  sel.className = 'badge cat-sel';
  ['Video Game','Music','Classical','Jazz','Anime','Sound Effects'].forEach(c => {
    const opt = document.createElement('option');
    opt.value=c; opt.textContent=c;
    if(c===current) opt.selected=true;
    sel.appendChild(opt);
  });
  badge.replaceWith(sel);
  sel.focus();

  function restore(val) {
    if(!sel.parentElement) return;
    const span = document.createElement('span');
    span.className='badge cat-badge'; span.textContent=val;
    span.onclick=()=>editCategory(jobId);
    sel.replaceWith(span);
  }

  sel.addEventListener('change', async () => {
    const newCat = sel.value;
    restore(newCat);
    try {
      const res = await fetch(`/api/jobs/${jobId}/category`, {
        method:'PATCH', headers:{'Content-Type':'application/json'},
        body: JSON.stringify({category:newCat})
      });
      if(!res.ok) restore(current);
    } catch(e){ restore(current); }
  });
  sel.addEventListener('blur', () => { setTimeout(()=>restore(current), 150); });
}

function removeCard(id) {
  _storeDelete(id);
  const el = document.getElementById(cardId(id));
  if(el){ const img=el.querySelector('.cover-img'); if(img) imgObserver.unobserve(img); el.remove(); }
  _updateCountBadges();
  if(!jobsEl.querySelector('.job')) jobsEl.innerHTML='<p class="empty">No downloads yet.</p>';
}

/* ── card ✕ menu: hide / remove files / delete permanently ── */
let _menuFor = null;

function openCardMenu(ev, id) {
  ev.stopPropagation();
  const menu = document.getElementById('card-menu');
  if(_menuFor === id && menu.classList.contains('open')) { closeCardMenu(); return; }
  _menuFor = id;
  const d = jobData.get(id) || {};
  menu.innerHTML = `
    <button onclick="cardMenuAction('hide')">${d.hidden ? 'Unhide' : 'Hide'}</button>
    <button onclick="cardMenuAction('remove')">Remove files</button>
    <button class="danger" onclick="cardMenuAction('delete')">Delete permanently</button>`;
  menu.classList.add('open');
  const r = ev.currentTarget.getBoundingClientRect();
  const w = menu.offsetWidth, h = menu.offsetHeight;
  const left = Math.max(8, Math.min(r.right - w, window.innerWidth - w - 8));
  const top  = r.bottom + 4 + h > window.innerHeight - 8 ? r.top - h - 4 : r.bottom + 4;
  menu.style.left = left + 'px';
  menu.style.top  = Math.max(8, top) + 'px';
}

function closeCardMenu() {
  document.getElementById('card-menu')?.classList.remove('open');
  _menuFor = null;
}
document.addEventListener('click', e => { if(!e.target.closest('#card-menu')) closeCardMenu(); });
document.addEventListener('keydown', e => { if(e.key === 'Escape') closeCardMenu(); });
window.addEventListener('scroll', closeCardMenu, {passive:true});

async function cardMenuAction(action) {
  const id = _menuFor;
  closeCardMenu();
  if(!id) return;
  if(id.startsWith('p-')) { removeCard(id); return; }  // a failed add that never reached the server
  const d = jobData.get(id) || {};
  const name = d.album || d.identifier || 'this album';
  if(action === 'hide') {
    const hidden = !d.hidden;
    const res = await fetch(`/api/jobs/${id}/hide`, {method:'POST',
      headers:{'Content-Type':'application/json'}, body:JSON.stringify({hidden})});
    if(res.ok) _setHidden(id, hidden);
  } else if(action === 'remove') {
    if(!confirm(`Delete the downloaded files for “${name}”?\n\nThe album stays in the list, so you can download it again later.`)) return;
    const res = await fetch(`/api/jobs/${id}/remove-files`, {method:'POST'});
    if(!res.ok) return;
    (await res.json()).job_ids.forEach(jid => patchCard(jid, {status:'removed', pct:0, count:0, msg:''}));
  } else if(action === 'delete') {
    if(!confirm(`Permanently delete “${name}”?\n\nIts files are deleted and it goes on the do-not-download list, so it can't be added again.`)) return;
    const res = await fetch(`/api/jobs/${id}`, {method:'DELETE'});
    if(!res.ok) return;
    (await res.json()).job_ids.forEach(jid => removeCard(jid));
  }
}

function _setHidden(id, hidden) {
  const old = jobData.get(id);
  if(!old) return;
  const d = {...old, hidden};
  _storeSet(id, d);
  _updateCountBadges();
  const el = document.getElementById(cardId(id));
  if(el && !matchesFilter(d, currentFilter)) {
    const img = el.querySelector('.cover-img');
    if(img) imgObserver.unobserve(img);
    el.remove();
  }
  if(!jobsEl.querySelector('.job')) rebuildView();
}

function esc(s){ return String(s).replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;'); }

async function loadExisting() {
  const res = await fetch('/api/jobs');
  const list = await res.json();
  // Store all jobs — no DOM work yet
  list.forEach(j => {
    _storeSet(j.job_id, {
      job_id:j.job_id, identifier:j.identifier||'', artist:j.artist||'…', album:j.album||'',
      fmt:j.format||'—', cat:j.category, category:j.category, duplicate:j.duplicate||false,
      pct:j.pct||0, count:j.count||0, total:j.total||0,
      status:j.status, error:j.error||null, queue_pos:j.queue_pos||0, hidden:!!j.hidden
    });
  });
  rebuildView();
  startProgressStream();
}

async function clearAll() {
  await fetch('/api/clear', {method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify({mode:'all'})});
  jobData.clear();
  _fc.all = _fc.active = _fc.completed = _fc.failed = _fc.hidden = 0;
  jobsEl.innerHTML='<p class="empty">No downloads yet.</p>';
  _updateCountBadges();
}

async function clearDone() {
  await fetch('/api/clear', {method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify({mode:'done'})});
  [...jobData.keys()].filter(id => jobData.get(id)?.status==='done').forEach(id => _storeDelete(id));
  rebuildView();
}

async function retryJob(id) {
  const res = await fetch(`/api/jobs/${id}/retry`, {method:'POST'});
  if(!res.ok) return;
  const data = await res.json();
  const old = jobData.get(id)||{};
  patchCard(id, {...old, status:data.status, error:null});
}

async function moveToFront(id) {
  const res = await fetch(`/api/jobs/${id}/front`, {method:'POST'});
  if(!res.ok) return;
  _refreshQueuePositions();
}

// Every waiting job's position shifts after a reorder, so re-read them all once
// and patch store + DOM directly (patchCard per job is too slow at ~2k jobs).
async function _refreshQueuePositions() {
  try {
    const res = await fetch('/api/jobs');
    const list = await res.json();
    list.forEach(j => {
      if(j.status==='queued' || j.status==='pending_meta') _setQueuePos(j.job_id, j.queue_pos||0);
    });
    _refreshDrawerActive();
  } catch(e) {}
}

// Update one job's queue number in the store and on its card, without the full
// patchCard path (a reorder can shift ~2k jobs at once)
function _setQueuePos(id, pos) {
  const old = jobData.get(id);
  if(!old || (old.queue_pos||0)===pos) return;
  _storeSet(id, {...old, queue_pos:pos});
  const el = document.getElementById(cardId(id));
  if(!el) return;
  el.dataset.queuePos = String(pos);
  const qb = document.getElementById(cardId(id)+'-qb');
  if(qb){ if(pos>0){ qb.textContent='#'+pos; qb.classList.add('visible'); } else qb.classList.remove('visible'); }
  if(currentFilter!=='all') el.style.order = calcOrder(old.status, pos);
  if(old.status==='queued'){
    const msg = el.querySelector('.card-foot span:first-child');
    if(msg) msg.textContent = pos>0?`Queued · #${pos}`:'Queued…';
  }
}

async function retryFailed() {
  const res = await fetch('/api/clear', {method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify({mode:'retry_failed'})});
  const data = await res.json();
  data.retried.forEach(id => {
    const old = jobData.get(id)||{};
    _storeSet(id, {...old, status:'queued', error:null});
    const el = document.getElementById(cardId(id));
    if(el) {
      el.classList.remove('error');
      el.querySelector('.bar-fill').style.width='0%';
      el.querySelector('.job-ct').textContent='';
      const oi = document.getElementById(cardId(id)+'-oi');
      if(oi) oi.innerHTML='';
      const msg = el.querySelector('.card-foot span:first-child');
      if(msg){ msg.className='s-queue'; msg.textContent='Queued…'; }
    }
  });
  rebuildView();
}

/* ── media player ── */
const audio    = document.getElementById('audio');
const plBar    = document.getElementById('player-bar');
const plPlay   = document.getElementById('pl-play');
const plTrack  = document.getElementById('pl-track');
const plAlbum  = document.getElementById('pl-album');
const plCur    = document.getElementById('pl-cur');
const plDur    = document.getElementById('pl-dur');
const plSeek   = document.getElementById('pl-seek');
const plVol    = document.getElementById('pl-vol');
const plArtImg = document.getElementById('pl-art');
const plArtPh  = document.getElementById('pl-art-ph');

let playerQueue      = [];
let playerIdx        = 0;
let playerJobId      = null;
let playerShuffleOn  = false;
let playerRepeatMode = 'none'; // 'none' | 'all' | 'one'
let playerSeeking    = false;

function fmtTime(s) {
  if(!isFinite(s)||s<0) return '0:00';
  const m=Math.floor(s/60), sec=Math.floor(s%60);
  return `${m}:${sec.toString().padStart(2,'0')}`;
}

function shuffleArr(arr) {
  for(let i=arr.length-1;i>0;i--){const j=Math.floor(Math.random()*(i+1));[arr[i],arr[j]]=[arr[j],arr[i]];}
  return arr;
}

function playerSetArt(jobId) {
  const card = document.getElementById(cardId(jobId));
  const img  = card?.querySelector('.cover-img');
  if(img && img.src && !img.dataset.src) {
    plArtImg.src = img.src;
    plArtImg.style.display = '';
    plArtPh.style.display  = 'none';
  } else {
    plArtImg.style.display = 'none';
    plArtPh.style.display  = '';
  }
  const titleEl = card?.querySelector('.job-title');
  plAlbum.textContent = titleEl?.textContent || '';
}

function playerLoadTrack(idx) {
  if(idx < 0 || idx >= playerQueue.length) return;
  playerIdx = idx;
  const t = playerQueue[idx];
  audio.src  = t.url;
  plTrack.textContent = t.display;
  plSeek.value = '0';
  plSeek.max   = '100';
  plCur.textContent = '0:00';
  plDur.textContent = '0:00';
  playerSetArt(playerJobId);
}

async function playAlbum(jobId) {
  try {
    const res = await fetch(`/api/jobs/${jobId}/tracks`);
    if(!res.ok) { alert('No playable tracks found.'); return; }
    const tracks = await res.json();
    if(!tracks.length) { alert('No audio files found for this item.'); return; }

    if(playerJobId) document.getElementById(cardId(playerJobId))?.classList.remove('now-playing');
    playerJobId = jobId;
    document.getElementById(cardId(jobId))?.classList.add('now-playing');

    playerQueue = playerShuffleOn ? shuffleArr([...tracks]) : tracks;
    playerLoadTrack(0);
    audio.play();
    plBar.classList.remove('hidden');
    document.body.classList.add('has-player');
  } catch(e) { console.error(e); }
}

function playerToggle() {
  if(audio.paused) audio.play(); else audio.pause();
}

function playerPrev() {
  if(audio.currentTime > 3) { audio.currentTime = 0; return; }
  const prev = playerIdx > 0 ? playerIdx - 1 : playerQueue.length - 1;
  playerLoadTrack(prev);
  audio.play();
}

function playerNext() {
  if(playerRepeatMode === 'one') { audio.currentTime = 0; audio.play(); return; }
  const next = playerIdx + 1;
  if(next >= playerQueue.length) {
    if(playerRepeatMode === 'all') { playerLoadTrack(0); audio.play(); }
    // else stop — bar stays visible with current track
  } else {
    playerLoadTrack(next);
    audio.play();
  }
}

function toggleShuffle() {
  playerShuffleOn = !playerShuffleOn;
  document.getElementById('pl-shuffle').classList.toggle('on', playerShuffleOn);
  if(playerShuffleOn && playerQueue.length) {
    const cur = playerQueue[playerIdx];
    playerQueue = shuffleArr([...playerQueue]);
    playerIdx   = playerQueue.findIndex(t => t.url === cur.url);
  } else if(!playerShuffleOn && playerJobId) {
    // restore original order; keep current position
    fetch(`/api/jobs/${playerJobId}/tracks`).then(r=>r.json()).then(tracks => {
      const cur = playerQueue[playerIdx];
      playerQueue = tracks;
      playerIdx   = tracks.findIndex(t => t.url === cur.url);
    }).catch(()=>{});
  }
}

function cycleRepeat() {
  const modes = ['none','all','one'];
  playerRepeatMode = modes[(modes.indexOf(playerRepeatMode)+1)%3];
  const btn = document.getElementById('pl-repeat');
  btn.classList.toggle('on', playerRepeatMode !== 'none');
  btn.textContent = playerRepeatMode === 'one' ? '↺¹' : '↺';
  btn.title = playerRepeatMode === 'one' ? 'Repeat One' : playerRepeatMode === 'all' ? 'Repeat All' : 'Repeat Off';
}

audio.addEventListener('play',  () => { plPlay.textContent = '⏸'; });
audio.addEventListener('pause', () => { plPlay.textContent = '▶'; });
audio.addEventListener('ended', () => { playerNext(); });
audio.addEventListener('loadedmetadata', () => {
  plSeek.max = String(audio.duration || 100);
  plDur.textContent = fmtTime(audio.duration);
});
audio.addEventListener('timeupdate', () => {
  if(playerSeeking) return;
  plCur.textContent = fmtTime(audio.currentTime);
  plSeek.value = String(audio.currentTime);
});

plSeek.addEventListener('mousedown', ()=>{ playerSeeking=true; });
plSeek.addEventListener('touchstart', ()=>{ playerSeeking=true; }, {passive:true});
plSeek.addEventListener('input', ()=>{ plCur.textContent=fmtTime(Number(plSeek.value)); });
plSeek.addEventListener('change', ()=>{ audio.currentTime=Number(plSeek.value); playerSeeking=false; });
plVol.addEventListener('input', ()=>{ audio.volume=Number(plVol.value); });

/* ── drawer active-download display ── */
function _refreshDrawerActive() {
  const nowEl    = document.getElementById('drawer-q-now');
  const idleEl   = document.getElementById('drawer-q-idle');
  const nextEl   = document.getElementById('drawer-q-next');
  const nextTitle= document.getElementById('drawer-q-next-title');
  const countEl  = document.getElementById('drawer-q-count');
  if(!nowEl) return;

  const running = [...jobData.values()].find(d => d.status==='running'||d.status==='verifying');

  // next queued job: lowest queue_pos among queued/pending_meta
  const nextJob = [...jobData.values()]
    .filter(d => d.status==='queued'||d.status==='pending_meta')
    .sort((a,b) => (a.queue_pos||9999) - (b.queue_pos||9999))[0];

  // NOW row
  if(running) {
    const pct  = running.pct || 0;
    const name = running.album || running.artist || running.identifier || '—';
    const pctLabel = running.status==='verifying' ? 'Verifying…' : pct+'%';
    document.getElementById('drawer-q-now-name').textContent = name;
    document.getElementById('drawer-q-now-pct').textContent  = pctLabel;
    document.getElementById('drawer-q-now-fill').style.width = pct+'%';
    nowEl.style.display  = '';
    if(idleEl) idleEl.style.display = 'none';
  } else {
    nowEl.style.display = 'none';
    if(idleEl) idleEl.style.display = (_fc.active === 0 && _fc.failed === 0) ? '' : 'none';
  }

  // NEXT row
  if(nextEl && nextTitle) {
    if(running && nextJob) {
      nextTitle.textContent = nextJob.album || nextJob.artist || nextJob.identifier || '—';
      nextEl.style.display  = '';
    } else {
      nextEl.style.display = 'none';
    }
  }

  // remaining count (excludes the running item)
  const remaining = Math.max(0, _fc.active - (running ? 1 : 0));
  if(countEl) {
    if(remaining > 0) {
      countEl.textContent  = `${remaining} remaining`;
      countEl.style.display = '';
    } else {
      countEl.style.display = 'none';
    }
  }
}

/* ── drawer ── */
function toggleDrawer() {
  const open = document.getElementById('drawer').classList.toggle('open');
  document.getElementById('drawer-overlay').classList.toggle('open', open);
  if(open) { _refreshDrawerActive(); setTimeout(()=>document.getElementById('url')?.focus(), 260); }
}
function closeDrawer() {
  document.getElementById('drawer').classList.remove('open');
  document.getElementById('drawer-overlay').classList.remove('open');
}
document.addEventListener('keydown', e => { if(e.key==='Escape') closeDrawer(); });

/* ── progress ring ── */
const RING_R    = 44;
const RING_CIRC = +(2 * Math.PI * RING_R).toFixed(2); // 276.46

let _ringJobId = null;

function _ensureRing(id) {
  const el = document.getElementById(cardId(id));
  if(!el) return null;
  let ov = document.getElementById('ring-ov-'+id);
  if(!ov) {
    ov = document.createElement('div');
    ov.id = 'ring-ov-'+id;
    ov.className = 'prog-ring-overlay';
    ov.innerHTML = `
      <svg class="prog-ring-svg" viewBox="0 0 100 100">
        <circle class="prog-ring-bg"   cx="50" cy="50" r="${RING_R}"/>
        <circle class="prog-ring-fill" cx="50" cy="50" r="${RING_R}"
          id="ring-fill-${id}"
          stroke-dasharray="${RING_CIRC}"
          stroke-dashoffset="${RING_CIRC}"/>
      </svg>
      <button class="prog-ring-btn" id="ring-btn-${id}" onclick="togglePause()">⏸</button>`;
    el.querySelector('.card-thumb').appendChild(ov);
    el.classList.add('has-ring');
    _ringJobId = id;
  }
  return ov;
}

function _updateRing(id, pct, paused) {
  _ensureRing(id);
  const fill = document.getElementById('ring-fill-'+id);
  if(fill) fill.style.strokeDashoffset = RING_CIRC * (1 - (pct||0)/100);
  const btn = document.getElementById('ring-btn-'+id);
  if(btn) btn.textContent = paused ? '▶' : '⏸';
}

function _removeRing(id) {
  const ov = document.getElementById('ring-ov-'+id);
  if(ov) ov.remove();
  const el = document.getElementById(cardId(id));
  if(el) el.classList.remove('has-ring');
  if(_ringJobId === id) _ringJobId = null;
}

/* ── pause / resume ── */
let _paused = false;

function applyPauseState(p) {
  _paused = p;
  // Update ring button on active card
  if(_ringJobId) {
    const btn = document.getElementById('ring-btn-'+_ringJobId);
    if(btn) btn.textContent = p ? '▶' : '⏸';
  }
}

async function togglePause() {
  const endpoint = _paused ? '/api/resume' : '/api/pause';
  const res = await fetch(endpoint, {method:'POST'});
  const data = await res.json();
  applyPauseState(data.paused);
}

window.addEventListener('DOMContentLoaded', async () => {
  const [, status] = await Promise.all([loadExisting(), fetch('/api/status').then(r=>r.json())]);
  applyPauseState(status.paused);
  applyFilter();
});
</script>
</body>
</html>"""


class DownloadRequest(BaseModel):
    url: str
    category: str = "Auto"


class ClearRequest(BaseModel):
    mode: str  # "all", "done", "retry_failed"


class HideRequest(BaseModel):
    hidden: bool


class CategoryChangeRequest(BaseModel):
    category: str


@app.get("/", response_class=HTMLResponse)
async def index():
    return HTML


@app.get("/api/status")
async def get_status():
    running_job = None
    for jid, job in jobs.items():
        if job.get("status") in ("running", "verifying", "fetching"):
            running_job = {
                "job_id":     jid,
                "identifier": job.get("identifier", ""),
                "artist":     job.get("artist", ""),
                "album":      job.get("album", ""),
                "status":     job["status"],
                "category":   job.get("category", ""),
                "fmt":        job.get("fmt", "—"),
            }
            break
    return {"paused": paused, "running": running_job}


@app.post("/api/pause")
async def pause_all():
    global paused
    paused = True
    return {"ok": True, "paused": True}


@app.post("/api/resume")
async def resume_all():
    global paused
    paused = False
    return {"ok": True, "paused": False}


@app.get("/api/jobs")
async def list_jobs():
    ordered = sorted(jobs.items(), key=lambda x: x[1].get("created_at", 0), reverse=True)
    result = []
    for jid, job in ordered:
        exp    = job.get("expected", [])
        dl     = len(job.get("downloaded", set()))
        status = job.get("status", "error")
        if status in ("done", "error", "pending_meta"):
            fs = {}
        else:
            fs = job.get("file_states", {f: 0 for f in exp})
        result.append({
            "job_id":      jid,
            "identifier":  job.get("identifier", ""),
            "artist":      job.get("artist", "…"),
            "album":       job.get("album", ""),
            "format":      job.get("fmt", "—"),
            "category":    job.get("category", ""),
            "total":       len(exp),
            "count":       dl,
            "pct":         int(100 * dl / len(exp)) if exp else 0,
            "status":      status,
            "duplicate":   job.get("duplicate", False),
            "error":       job.get("error"),
            "queue_pos":   queue_list.index(jid) + 1 if jid in queue_list else 0,
            "file_states": fs,
            "hidden":      job.get("hidden", False),
        })
    return result


@app.post("/api/clear")
async def clear_jobs(req: ClearRequest):
    retried: List[str] = []
    if req.mode == "all":
        jobs.clear()
        queue_list.clear()
        while not job_queue.empty():
            try:
                job_queue.get_nowait()
                job_queue.task_done()
            except Exception:
                break
    elif req.mode == "done":
        for jid in [jid for jid, j in jobs.items() if j["status"] == "done"]:
            del jobs[jid]
    elif req.mode == "retry_failed":
        for jid, job in list(jobs.items()):
            if job["status"] == "error":
                is_stub = not job.get("expected")
                job["status"]     = "pending_meta" if is_stub else "queued"
                job["error"]      = None
                if not is_stub:
                    job["downloaded"]  = set()
                    job["file_states"] = {f: 0 for f in job.get("expected", [])}
                queue_list.append(jid)
                await job_queue.put(jid)
                retried.append(jid)
    save_jobs()
    return {"ok": True, "retried": retried}


@app.post("/api/jobs/{job_id}/retry")
async def retry_job(job_id: str):
    job = jobs.get(job_id)
    if not job:
        raise HTTPException(404, "Job not found")
    if job["status"] not in ("error", "removed"):
        raise HTTPException(400, "Job is not in error or removed state")
    is_stub = not job.get("expected")
    job["status"] = "pending_meta" if is_stub else "queued"
    job["error"]  = None
    if not is_stub:
        job["downloaded"]  = set()
        job["file_states"] = {f: 0 for f in job.get("expected", [])}
    queue_list.insert(0, job_id)
    await job_queue.put(job_id)
    save_jobs()
    return {"ok": True, "status": job["status"]}


@app.post("/api/jobs/{job_id}/front")
async def move_job_to_front(job_id: str):
    job = jobs.get(job_id)
    if not job:
        raise HTTPException(404, "Job not found")
    if job_id not in queue_list or job["status"] not in ("queued", "pending_meta"):
        raise HTTPException(400, "Job is not waiting in the queue")
    # Reorder only — the job already has its wake-up token in job_queue
    queue_list.remove(job_id)
    queue_list.insert(0, job_id)
    save_queue()
    return {"ok": True, "queue_pos": 1}


async def _cancel_job(job_id: str):
    """Take a job out of the queue and stop its download if one is running."""
    job = jobs[job_id]
    job["_run_id"] = None  # the running download, if any, exits without touching the job
    if job_id in queue_list:
        queue_list.remove(job_id)
    proc = job.get("_proc")
    if proc and proc.returncode is None:
        proc.kill()
        try:
            await asyncio.wait_for(proc.wait(), timeout=5)
        except asyncio.TimeoutError:
            pass


def _delete_album_dir(job: dict) -> bool:
    """Delete <AUDIO_ROOT>/<category>/<identifier>, refusing any other shape of path."""
    root   = Path(AUDIO_ROOT).resolve()
    target = Path(job.get("dest", "")).resolve()
    if target.parent.parent != root or target.name != job.get("identifier") or not target.is_dir():
        return False
    shutil.rmtree(target)
    return True


def _same_album(job: dict) -> List[str]:
    # The same item added twice shares one folder, so album actions apply to both
    return [jid for jid, j in jobs.items() if j.get("identifier") == job.get("identifier")]


@app.post("/api/jobs/{job_id}/hide")
async def hide_job(job_id: str, req: HideRequest):
    job = jobs.get(job_id)
    if not job:
        raise HTTPException(404, "Job not found")
    job["hidden"] = req.hidden
    save_jobs()
    return {"ok": True, "hidden": req.hidden}


@app.post("/api/jobs/{job_id}/remove-files")
async def remove_job_files(job_id: str):
    """Delete the album's files but keep its entry, so it can be downloaded again."""
    job = jobs.get(job_id)
    if not job:
        raise HTTPException(404, "Job not found")
    affected = _same_album(job)
    for jid in affected:
        await _cancel_job(jid)
    removed_dir = _delete_album_dir(job)
    for jid in affected:
        j = jobs[jid]
        j["status"]      = "removed"
        j["error"]       = None
        j["downloaded"]  = set()
        j["file_states"] = {f: 0 for f in j.get("expected", [])}
    history_remove(job["identifier"])
    save_jobs()
    return {"ok": True, "job_ids": affected, "removed_dir": removed_dir}


@app.delete("/api/jobs/{job_id}")
async def delete_job(job_id: str):
    """Permanent delete: files, entry, and a do-not-download list entry."""
    job = jobs.get(job_id)
    if not job:
        raise HTTPException(404, "Job not found")
    affected = _same_album(job)
    for jid in affected:
        await _cancel_job(jid)
    removed_dir = _delete_album_dir(job)
    for jid in affected:
        del jobs[jid]
    blocklist_add(job["identifier"])
    history_remove(job["identifier"])
    save_jobs()
    return {"ok": True, "job_ids": affected, "removed_dir": removed_dir}


@app.patch("/api/jobs/{job_id}/category")
async def change_category(job_id: str, req: CategoryChangeRequest):
    if job_id not in jobs:
        raise HTTPException(404, "Job not found")
    if req.category not in CATEGORIES:
        raise HTTPException(400, f"Invalid category")
    job = jobs[job_id]
    if job["status"] in ("running", "verifying"):
        raise HTTPException(409, "Cannot change category while download is in progress")

    old_dest = job.get("dest", "")
    new_dest = f"{AUDIO_ROOT}/{req.category}/{job['identifier']}"

    if old_dest != new_dest and os.path.exists(old_dest):
        os.makedirs(os.path.dirname(new_dest), exist_ok=True)
        shutil.move(old_dest, new_dest)

    job["category"] = req.category
    job["dest"]     = new_dest
    save_jobs()
    return {"ok": True, "category": req.category}


@app.post("/api/download")
async def start_download(req: DownloadRequest):
    identifier = req.url.strip().rstrip("/").split("/")[-1]
    if not identifier:
        raise HTTPException(400, "Invalid URL")
    blocked = blocklist()
    if identifier in blocked:
        raise HTTPException(409, f"'{identifier}' was permanently deleted and is on the do-not-download list")

    meta = await _fetch_meta(identifier)
    if meta is None:
        raise HTTPException(503, "archive.org didn't respond (3 tries) — try again later")
    if not meta:
        raise HTTPException(404, f"No metadata found for '{identifier}'")

    md = meta.get("metadata", {})

    # Collection: queue all child items as stubs
    if md.get("mediatype") == "collection":
        child_ids = await _expand_collection(identifier)
        if not child_ids:
            raise HTTPException(404, "Collection is empty or could not be listed")
        skipped   = [c for c in child_ids if c in blocked]
        child_ids = [c for c in child_ids if c not in blocked]
        if not child_ids:
            raise HTTPException(409, "Every item in this collection is on the do-not-download list")

        category = req.category if req.category in CATEGORIES else "Auto"
        result_jobs = []
        for child_id in child_ids:
            job_id = str(uuid.uuid4())[:8]
            jobs[job_id] = {
                "identifier":     child_id,
                "artist":         "…",
                "album":          child_id,
                "dest":           f"{AUDIO_ROOT}/{category}/{child_id}",
                "expected":       [],
                "expected_sizes": {},
                "downloaded":     set(),
                "file_states":    {},
                "status":         "pending_meta",
                "duplicate":      False,
                "glob":           "",
                "verify":         False,
                "fmt":            "—",
                "category":       category,
                "created_at":     time.time(),
                "error":          None,
            }
            queue_list.append(job_id)
            await job_queue.put(job_id)
            result_jobs.append({"job_id": job_id, "identifier": child_id,
                                 "status": "pending_meta", "category": category})
        save_jobs()
        return {"collection": True, "count": len(child_ids), "skipped_blocked": len(skipped),
                "identifier": identifier, "jobs": result_jobs}

    # Single item
    category = req.category if req.category in [*CATEGORIES, "Auto"] else "Auto"
    fields = _parse_meta(meta, identifier, category)
    if fields is None:
        raise HTTPException(404, "No FLAC or MP3 files found for this item")

    os.makedirs(fields["dest"], exist_ok=True)

    job_id = str(uuid.uuid4())[:8]
    jobs[job_id] = {
        "identifier": identifier,
        "created_at": time.time(),
        "error":      None,
        "status":     "queued",
        **fields,
    }

    queue_list.append(job_id)
    await job_queue.put(job_id)
    save_jobs()

    return {
        "job_id":    job_id,
        "identifier": identifier,
        "artist":    fields["artist"],
        "album":     fields["album"],
        "format":    fields["fmt"],
        "total":     len(fields["expected"]),
        "duplicate": fields["duplicate"],
        "category":  fields["category"],
    }


async def run_download(job_id: str, glob: str, verify: bool = False):
    job = jobs[job_id]
    run_id = object()
    job["_run_id"] = run_id  # _cancel_job() clears this; a stale run then exits quietly
    job["status"] = "verifying" if verify else "running"
    save_jobs()
    try:
        cmd = [
            "ia", "download", job["identifier"],
            "--destdir", job["dest"],
            "--no-directories", "--no-change-timestamp",
            "--glob", glob,
        ]
        # ia skips a finished file only when its mtime matches archive.org's, which
        # --no-change-timestamp prevents, so a resumed job would re-download every
        # file. With files already on disk, compare MD5s instead: matches are skipped.
        resuming = any(os.path.exists(os.path.join(job["dest"], name))
                       for name in job.get("expected", []))
        if verify or resuming:
            cmd.append("--checksum")
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
        )
        job["_proc"] = proc
        while proc.returncode is None:
            if paused:
                try:
                    proc.kill()
                    await proc.wait()
                except Exception:
                    pass
                job["_proc"] = None
                job["status"] = "queued"
                job["error"]  = None
                queue_list.insert(0, job_id)
                await job_queue.put(job_id)
                save_jobs()
                return
            await _refresh(job_id)
            try:
                await asyncio.wait_for(proc.wait(), timeout=2.0)
            except asyncio.TimeoutError:
                pass
        job["_proc"] = None
        if job.get("_run_id") is not run_id:
            return  # cancelled by remove-files / delete
        await _refresh(job_id)
        extract_err = None
        if proc.returncode == 0 and job.get("archive"):
            extract_err = await _extract_archives(job_id)
        job["status"] = "done" if proc.returncode == 0 and not extract_err else "error"
        if proc.returncode != 0:
            job["error"] = f"download exited {proc.returncode}"
        elif extract_err:
            job["error"] = extract_err
        elif not verify:
            history_add(job["identifier"])
        save_jobs()
    except Exception as exc:
        job["_proc"] = None
        if job.get("_run_id") is not run_id:
            return
        job["status"] = "error"
        job["error"]  = str(exc)
        save_jobs()


ARCHIVE_AUDIO_ORDER = (".flac", ".wav", ".aiff", ".aif", ".ape", ".wv",
                       ".mp3", ".ogg", ".oga", ".opus", ".m4a", ".aac")


async def _7z(*args: str):
    proc = await asyncio.create_subprocess_exec(
        "7z", *args, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT)
    out, _ = await proc.communicate()
    return proc.returncode, out.decode("utf-8", "replace")


async def _extract_archives(job_id: str) -> str | None:
    """Unpack the job's downloaded .zip/.7z files into dest, keeping only the best
    audio format found. The archives are deleted either way. Returns an error or None."""
    job      = jobs[job_id]
    dest     = Path(job["dest"])
    archives = [dest / name for name in job["expected"]]
    tmp      = dest / ".ia-extract"
    shutil.rmtree(tmp, ignore_errors=True)
    try:
        for arc in archives:
            rc, listing = await _7z("l", "-slt", str(arc))
            if rc != 0:
                return f"Could not read archive {arc.name}"
            unpacked = sum(int(n) for n in re.findall(r"^Size = (\d+)$", listing, re.M))
            if unpacked > shutil.disk_usage(dest).free - 1_000_000_000:
                return f"Not enough disk space to extract {arc.name}"
            out_dir = tmp / arc.stem if len(archives) > 1 else tmp
            rc, _ = await _7z("x", "-y", f"-o{out_dir}", str(arc))
            if rc != 0:
                return f"Extracting {arc.name} failed"

        # Only regular files that really sit inside tmp (no symlink escapes)
        root  = tmp.resolve()
        found = []
        for p in tmp.rglob("*"):
            if p.is_symlink() or not p.is_file():
                continue
            try:
                p.resolve().relative_to(root)
            except ValueError:
                continue
            found.append(p)
        exts = {p.suffix.lower() for p in found}
        best = next((e for e in ARCHIVE_AUDIO_ORDER if e in exts), None)
        if best is None:
            seen = ", ".join(sorted(e.lstrip(".") for e in exts if e)[:6]) or "nothing"
            return f"Archive has no supported audio (contains: {seen})"

        kept = []
        for p in sorted(found):
            if p.suffix.lower() != best:
                continue
            rel    = p.relative_to(tmp)
            target = dest / rel
            target.parent.mkdir(parents=True, exist_ok=True)
            os.replace(p, target)
            kept.append(str(rel))
        # From here on the job looks like a normal album: tracks, not archives
        job["expected"]       = kept
        job["expected_sizes"] = {r: os.path.getsize(dest / r) for r in kept}
        job["downloaded"]     = set(kept)
        job["file_states"]    = {r: 100 for r in kept}
        job["fmt"]            = best.lstrip(".").upper()
        job["archive"]        = False
        return None
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
        for arc in archives:
            try:
                arc.unlink()
            except OSError:
                pass


async def _refresh(job_id: str):
    job = jobs[job_id]
    try:
        on_disk: Set[str] = set()
        for root, _dirs, files in os.walk(job["dest"]):
            for f in files:
                rel = os.path.relpath(os.path.join(root, f), job["dest"])
                on_disk.add(rel)

        expected_sizes = job.get("expected_sizes", {})
        file_states: dict = {}
        completed: Set[str] = set()

        for fname in job["expected"]:
            if fname not in on_disk:
                file_states[fname] = 0
                continue
            ctrl = fname + ".aria2"
            expected_sz = expected_sizes.get(fname, 0)
            if expected_sz > 0:
                try:
                    actual_sz = os.path.getsize(os.path.join(job["dest"], fname))
                    if actual_sz >= expected_sz and ctrl not in on_disk:
                        file_states[fname] = 100
                        completed.add(fname)
                    else:
                        file_states[fname] = min(99, int(100 * actual_sz / expected_sz))
                except OSError:
                    file_states[fname] = 1
            else:
                if ctrl not in on_disk:
                    file_states[fname] = 100
                    completed.add(fname)
                else:
                    file_states[fname] = 1

        job["downloaded"] = completed
        job["file_states"] = file_states
    except OSError:
        pass


AUDIO_EXTS = {'.mp3', '.flac', '.ogg', '.oga', '.m4a', '.wav', '.opus', '.aac', '.ape', '.wv'}


@app.get("/api/jobs/{job_id}/tracks")
async def get_tracks(job_id: str):
    job = jobs.get(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")
    identifier = job.get("identifier", "")
    category   = job.get("category", "")
    if not identifier:
        raise HTTPException(status_code=404, detail="No identifier")
    base = Path(f"{AUDIO_ROOT}/{category}/{identifier}").resolve()
    if not base.exists():
        raise HTTPException(status_code=404, detail="Directory not found")
    tracks = []
    for root, dirs, files in os.walk(base):
        dirs.sort()
        for fname in sorted(files):
            if Path(fname).suffix.lower() in AUDIO_EXTS:
                full = Path(root) / fname
                rel  = str(full.relative_to(base))
                tracks.append({"filename": rel, "display": Path(fname).stem, "url": f"/media/{job_id}/{rel}"})
    return tracks


@app.get("/media/{job_id}/{path:path}")
async def serve_media(job_id: str, path: str):
    job = jobs.get(job_id)
    if not job:
        raise HTTPException(status_code=404)
    base      = Path(f"{AUDIO_ROOT}/{job.get('category','')}/{job.get('identifier','')}").resolve()
    file_path = (base / path).resolve()
    try:
        file_path.relative_to(base)
    except ValueError:
        raise HTTPException(status_code=403)
    if not file_path.is_file():
        raise HTTPException(status_code=404)
    return FileResponse(file_path)


def _progress_sig(job: dict) -> tuple:
    return (len(job.get("downloaded", ())), len(job.get("expected", [])), job["status"],
            job.get("error"), job.get("category", ""), job.get("fmt", "—"),
            job.get("artist", ""), job.get("album", ""))


def _progress_entry(job_id: str, job: dict) -> dict:
    count = len(job.get("downloaded", ()))
    total = len(job.get("expected", []))
    return {
        "job_id": job_id, "count": count, "total": total,
        "pct": int(100 * count / total) if total else 0,
        "status": job["status"], "error": job.get("error"),
        "artist": job.get("artist", ""), "album": job.get("album", ""),
        "category": job.get("category", ""), "fmt": job.get("fmt", "—"),
    }


@app.get("/api/progress")
async def progress_stream():
    """One stream for the whole page: changed jobs, plus the queue order whenever
    it changes (sent as one id list rather than a position update per job)."""
    async def generate():
        last: Dict[str, tuple] = {}
        last_queue: List[str] | None = None
        first = True
        idle_ticks = 0
        while True:
            changed = []
            for jid, job in list(jobs.items()):
                sig = _progress_sig(job)
                if last.get(jid) != sig:
                    # On connect the page has just loaded /api/jobs, so only resend
                    # jobs that may have moved on since then
                    if not first or job["status"] in ("running", "verifying", "queued"):
                        changed.append(_progress_entry(jid, job))
                    last[jid] = sig
            for jid in [k for k in last if k not in jobs]:
                del last[jid]
            msg = {}
            if changed:
                msg["jobs"] = changed
            if queue_list != last_queue:
                last_queue = list(queue_list)
                msg["queue"] = last_queue
            first = False
            if msg:
                idle_ticks = 0
                yield f"data: {json.dumps(msg)}\n\n"
            else:
                idle_ticks += 1
                if idle_ticks >= 16:
                    idle_ticks = 0
                    yield ": keep-alive\n\n"
            await asyncio.sleep(1.5)

    return StreamingResponse(
        generate(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache",
                 "X-Accel-Buffering": "no",
                 "Connection": "keep-alive"},
    )
