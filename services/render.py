"""Vidmorrow's renderer: a program beside the server, with ffmpeg.

The core starts it with the Quill and keeps it running. Every few seconds
it reads the clips through the record API, as the administrator who
installed Vidmorrow, and does what each one is waiting for:

- a *new* clip: read its size, length and frame rate with ffprobe;
- a clip whose keyframes changed (`wants_previews`): draw a picture of each
  changed keyframe, the blur as it will be and the box outlined, and put it
  in the Previews folder beside the video;
- a *queued* clip: render the export with ffmpeg and put it beside the
  original, then say so on the clip.

The arithmetic — keyframes to filter graphs — is in graph.py. Only the
standard library is used, and ffmpeg and ffprobe on the service's PATH, where
the server put them: the manifest requires the `ffmpeg` runtime, and the
install fetched it.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import graph

URL = os.environ.get("CLOUDMORROW_URL", "")
TOKEN = os.environ.get("CLOUDMORROW_TOKEN", "")
HOME = Path(os.environ.get("HOME") or tempfile.gettempdir())
CLIP, EFFECT, KEYFRAME, FILE = "vidmorrow.clip", "vidmorrow.effect", "vidmorrow.keyframe", "file"
PREVIEWS = "Previews"
EVERY = 3.0
NO_FFMPEG = "ffmpeg is missing on the server; install Vidmorrow again and it comes with it, then the renderer picks the clip up"


def log(*parts) -> None:
    print(time.strftime("%H:%M:%S"), *parts, flush=True)


# -- the record API ----------------------------------------------------------------------
class ApiError(Exception):
    pass


def api(method: str, path: str, *, body: dict | None = None, query: dict | None = None, data=None, headers=None):
    url = URL + path + ("?" + urllib.parse.urlencode(query) if query else "")
    payload = json.dumps(body).encode() if body is not None else data
    request = urllib.request.Request(url, method=method, data=payload)
    request.add_header("Authorization", "Bearer " + TOKEN)
    if body is not None:
        request.add_header("Content-Type", "application/json")
    for key, value in (headers or {}).items():
        request.add_header(key, value)
    try:
        with urllib.request.urlopen(request, timeout=600) as answer:
            raw = answer.read()
    except urllib.error.HTTPError as exc:
        said = exc.read().decode("utf-8", "replace")[:300]
        raise ApiError(f"{method} {path}: {exc.code} {said}") from None
    except urllib.error.URLError as exc:
        raise ApiError(f"{method} {path}: {exc.reason}") from None
    return json.loads(raw) if raw else None


def records(model: str, **where) -> list[dict]:
    found = api("GET", f"/api/records/{model}", query=where or None)
    return found.get("records", []) if isinstance(found, dict) else list(found or [])


def record(model: str, record_id: str) -> dict:
    return api("GET", f"/api/records/{model}/{record_id}")


def patch(model: str, record_id: str, fields: dict) -> dict:
    return api("PATCH", f"/api/records/{model}/{record_id}", body={"fields": fields})


def delete(model: str, record_id: str) -> None:
    try:
        api("DELETE", f"/api/records/{model}/{record_id}")
    except ApiError as exc:
        if " 404 " not in str(exc):
            raise


def download(file_id: str, target: Path) -> None:
    request = urllib.request.Request(f"{URL}/api/records/{FILE}/{file_id}/content")
    request.add_header("Authorization", "Bearer " + TOKEN)
    part = target.with_name(target.name + ".part")
    try:
        with urllib.request.urlopen(request, timeout=600) as answer, part.open("wb") as out:
            shutil.copyfileobj(answer, out, 1 << 20)
    except urllib.error.HTTPError as exc:
        part.unlink(missing_ok=True)
        raise ApiError(f"download {file_id}: {exc.code}") from None
    part.replace(target)


def upload(share: str, folder: str, name: str, source: Path) -> dict:
    with source.open("rb") as handle:
        return api(
            "POST",
            f"/api/records/{FILE}/upload",
            query={"share": share, "folder": folder, "name": name},
            data=handle,
            headers={"Content-Type": "application/octet-stream", "Content-Length": str(source.stat().st_size)},
        )


def ensure_folder(share: str, folder: str, name: str) -> None:
    try:
        api(
            "POST",
            f"/api/records/{FILE}",
            body={"fields": {"share": share, "folder": folder, "name": name, "kind": "folder"}},
        )
    except ApiError as exc:
        if "already" not in str(exc):
            raise


# -- the files -------------------------------------------------------------------------
def slug(text: str) -> str:
    return re.sub(r"[^A-Za-z0-9]+", "-", text).strip("-").lower() or "clip"


def source_of(clip: dict) -> tuple[dict, Path]:
    """The clip's video, fetched once into the renderer's own folder."""
    source = record(FILE, clip["fields"]["source"])
    fields = source["fields"]
    cache = HOME / "cache"
    cache.mkdir(parents=True, exist_ok=True)
    stamp = slug(f"{fields.get('size', 0)}-{fields.get('modified', '')}")
    suffix = Path(str(fields.get("name") or "")).suffix or ".mp4"
    target = cache / f"{source['id']}-{stamp}{suffix}"
    if not target.exists():
        for old in cache.glob(f"{source['id']}-*"):
            old.unlink(missing_ok=True)
        log("fetching", fields.get("name"))
        download(source["id"], target)
    return source, target


def run(command: list[str], *, timeout: float | None = None) -> subprocess.CompletedProcess:
    return subprocess.run(command, capture_output=True, text=True, timeout=timeout, check=False)


def failed_said(result: subprocess.CompletedProcess) -> str:
    lines = [line for line in (result.stderr or "").strip().splitlines() if line.strip()]
    return (lines[-1] if lines else f"ffmpeg exited with {result.returncode}")[:300]


# -- what a clip is waiting for ----------------------------------------------------------
class Renderer:
    def __init__(self) -> None:
        self.ffmpeg = shutil.which("ffmpeg")
        self.ffprobe = shutil.which("ffprobe")

    @property
    def ready(self) -> bool:
        return bool(self.ffmpeg and self.ffprobe)

    def tick(self) -> None:
        for clip in records(CLIP):
            try:
                self.serve(clip)
            except ApiError as exc:
                log("clip", clip["id"], "-", exc)
            except Exception as exc:  # noqa: BLE001 — one clip's trouble is not the next one's
                log("clip", clip["id"], "failed:", repr(exc))
                try:
                    patch(CLIP, clip["id"], {"status": "failed", "message": str(exc)[:300]})
                except ApiError:
                    pass

    def serve(self, clip: dict) -> None:
        fields = clip["fields"]
        status = fields.get("status")
        wants_probe = status == "new" or (status == "failed" and not fields.get("width"))
        if not self.ready:
            if status in ("new", "queued") and fields.get("message") != NO_FFMPEG:
                patch(CLIP, clip["id"], {"status": "failed", "message": NO_FFMPEG})
            return
        if wants_probe:
            clip = self.probe(clip)
            fields = clip["fields"]
            if fields.get("status") == "failed":
                return
        if fields.get("wants_previews"):
            self.previews(clip)
        if fields.get("status") == "queued":
            self.render(clip)

    def probe(self, clip: dict) -> dict:
        source, path = source_of(clip)
        result = run(graph.probe_command(self.ffprobe, str(path)), timeout=120)
        try:
            if result.returncode != 0:
                raise ValueError(failed_said(result))
            info = graph.parse_probe(json.loads(result.stdout or "{}"))
        except (ValueError, json.JSONDecodeError) as exc:
            log("probe", source["fields"].get("name"), "failed:", exc)
            return patch(CLIP, clip["id"], {"status": "failed", "message": f"Not a video the server can read: {exc}"})
        log("read", source["fields"].get("name"), info)
        return patch(
            CLIP,
            clip["id"],
            {
                "status": "ready",
                "message": "",
                "width": info["width"],
                "height": info["height"],
                "duration": round(info["duration"], 3),
                "fps": round(graph.fps_number(info["fps"]), 3),
            },
        )

    def effects_of(self, clip_id: str) -> list[dict]:
        keyframes = records(KEYFRAME, clip=clip_id)
        effects = []
        for effect in records(EFFECT, clip=clip_id):
            own = [k["fields"] for k in keyframes if k["fields"].get("effect") == effect["id"]]
            effects.append(
                {"id": effect["id"], "strength": int(effect["fields"].get("strength") or 20), "keyframes": own}
            )
        return effects

    def previews(self, clip: dict) -> None:
        patch(CLIP, clip["id"], {"wants_previews": False})
        fields = clip["fields"]
        width, height, duration = int(fields["width"]), int(fields["height"]), float(fields["duration"])
        effects = self.effects_of(clip["id"])
        strength = {e["id"]: e["strength"] for e in effects}
        source, path = source_of(clip)
        share, folder = str(source["fields"].get("share") or "my-files"), str(source["fields"].get("folder") or "")
        previews_folder = f"{folder}/{PREVIEWS}".strip("/")
        ensure_folder(share, folder, PREVIEWS)
        there = {f["id"] for f in records(FILE, share=share, folder=previews_folder)}
        for keyframe in records(KEYFRAME, clip=clip["id"]):
            k = keyframe["fields"]
            sig = (
                "|".join(str(k.get(name)) for name in ("at", "x", "y", "w", "h"))
                + f"|{strength.get(k.get('effect'), 20)}"
            )
            if k.get("preview_of") == sig and k.get("preview") in there:
                continue
            t = min(float(k["at"]), max(0.0, duration - 0.05))
            with tempfile.TemporaryDirectory(dir=HOME) as tmp:
                out = Path(tmp) / "frame.png"
                picture = graph.preview_graph(effects, t, width, height, duration)
                result = run(graph.preview_command(self.ffmpeg, str(path), t, str(out), picture), timeout=300)
                if result.returncode != 0 or not out.exists():
                    log("preview", keyframe["id"], "failed:", failed_said(result))
                    continue
                if k.get("preview"):
                    delete(FILE, k["preview"])
                name = f"{slug(fields.get('name', 'clip'))}-{keyframe['id']}.png"
                made = upload(share, previews_folder, name, out)
            patch(KEYFRAME, keyframe["id"], {"preview": made["id"], "preview_of": sig})
            log("drew", name, "at", t)

    def render(self, clip: dict) -> None:
        patch(CLIP, clip["id"], {"status": "rendering", "message": ""})
        fields = clip["fields"]
        width, height, duration = int(fields["width"]), int(fields["height"]), float(fields["duration"])
        fps = f"{float(fields.get('fps') or 25):.3f}"
        effects = self.effects_of(clip["id"])
        source, path = source_of(clip)
        share, folder = str(source["fields"].get("share") or "my-files"), str(source["fields"].get("folder") or "")
        stem = Path(str(source["fields"].get("name") or "clip")).stem
        started = time.time()
        with tempfile.TemporaryDirectory(dir=HOME) as tmp:
            out = Path(tmp) / f"{stem} blurred.mp4"
            command = graph.export_command(
                self.ffmpeg, str(path), str(out), graph.export_graph(effects, width, height, fps, duration)
            )
            log("rendering", fields.get("name"), "with", len(effects), "effects")
            result = run(command)
            if result.returncode != 0 or not out.exists():
                said = failed_said(result)
                log("render", fields.get("name"), "failed:", said)
                patch(CLIP, clip["id"], {"status": "failed", "message": f"The render failed: {said}"})
                return
            made = upload(share, folder, out.name, out)
        log("exported", made["fields"].get("name"), f"in {time.time() - started:.0f}s")
        patch(CLIP, clip["id"], {"status": "done", "message": "", "output": made["id"]})


def main() -> None:
    if not URL or not TOKEN:
        print("CLOUDMORROW_URL and CLOUDMORROW_TOKEN are needed: this is started by the server", file=sys.stderr)
        sys.exit(2)
    renderer = Renderer()
    log("up;", "ffmpeg at " + renderer.ffmpeg if renderer.ready else NO_FFMPEG)
    while True:
        try:
            renderer.tick()
        except ApiError as exc:
            log("the server:", exc)
        except Exception as exc:  # noqa: BLE001
            log("trouble:", repr(exc))
        time.sleep(EVERY)


if __name__ == "__main__":
    main()
