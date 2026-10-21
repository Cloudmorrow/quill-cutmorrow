"""CutMorrow's renderer: a program beside the server, with ffmpeg.

The core starts it with the Quill and keeps it running. It does three things,
through the record API as the administrator who installed CutMorrow:

- **the frames API** (`[[apis]] id = "frames"`): an HTTP server on `$PORT`,
  which the core forwards `/api/q/cutmorrow/frames/…` to. `info` is what
  ffprobe says about a file, `source` a frame of one, `frame` a frame of a
  project's cut — the top video track's piece fitted, its blurs drawn, the
  crop applied. Each answers in well under the core's 30 seconds once the
  file is in the cache;
- **filling in**: a piece with no length gets its source's (from its
  offset on); a project with no size or frame rate gets its first video's;
- **exports**: a project whose status is *queued* is rendered with one
  ffmpeg filter graph (graph.py) and the result is put in My Files ›
  CutMorrow Exports, made the first time; the project says *done* and
  points at it, or *failed* and why.

Only the standard library is used, and ffmpeg and ffprobe on the service's
PATH, where the server put them: the manifest requires the `ffmpeg` runtime.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import graph

PROJECT, PIECE, EFFECT, MEDIA, FILE = (
    "cutmorrow.project",
    "cutmorrow.piece",
    "cutmorrow.effect",
    "cutmorrow.media",
    "file",
)
# Every export lands here, in the drive of whoever the renderer runs as.
EXPORTS = ("my-files", "CutMorrow Exports")
EVERY = 3.0
FRAME_TIMEOUT = 25.0
NO_FFMPEG = "ffmpeg is missing on the server; install CutMorrow again and it comes with it"


def log(*parts) -> None:
    print(time.strftime("%H:%M:%S"), *parts, flush=True)


# -- the record API ----------------------------------------------------------------------
class ApiError(Exception):
    def __init__(self, said: str, status: int = 0) -> None:
        super().__init__(said)
        self.status = status


class Store:
    """The server's record API, with the Quill's token; and a cache of the files' bytes."""

    def __init__(self, url: str, token: str, cache: Path) -> None:
        self.url, self.token, self.cache = url.rstrip("/"), token, cache
        self._locks: dict[str, threading.Lock] = {}
        self._guard = threading.Lock()

    def api(self, method: str, path: str, *, body=None, query=None, data=None, headers=None, timeout=60):
        url = self.url + path + ("?" + urllib.parse.urlencode(query) if query else "")
        payload = json.dumps(body).encode() if body is not None else data
        request = urllib.request.Request(url, method=method, data=payload)
        request.add_header("Authorization", "Bearer " + self.token)
        if body is not None:
            request.add_header("Content-Type", "application/json")
        for key, value in (headers or {}).items():
            request.add_header(key, value)
        try:
            with urllib.request.urlopen(request, timeout=timeout) as answer:
                raw = answer.read()
        except urllib.error.HTTPError as exc:
            said = exc.read().decode("utf-8", "replace")[:300]
            raise ApiError(f"{method} {path}: {exc.code} {said}", exc.code) from None
        except urllib.error.URLError as exc:
            raise ApiError(f"{method} {path}: {exc.reason}") from None
        return json.loads(raw) if raw else None

    def records(self, model: str, **where) -> list[dict]:
        found = self.api("GET", f"/api/records/{model}", query=where or None)
        return found.get("records", []) if isinstance(found, dict) else list(found or [])

    def record(self, model: str, record_id: str) -> dict:
        return self.api("GET", f"/api/records/{model}/{urllib.parse.quote(record_id, safe='')}")

    def create(self, model: str, fields: dict) -> dict:
        return self.api("POST", f"/api/records/{model}", body={"fields": fields})

    def patch(self, model: str, record_id: str, fields: dict) -> dict:
        return self.api(
            "PATCH", f"/api/records/{model}/{urllib.parse.quote(record_id, safe='')}", body={"fields": fields}
        )

    def upload(self, share: str, folder: str, name: str, source: Path) -> dict:
        with source.open("rb") as handle:
            return self.api(
                "POST",
                f"/api/records/{FILE}/upload",
                query={"share": share, "folder": folder, "name": name},
                data=handle,
                headers={"Content-Type": "application/octet-stream", "Content-Length": str(source.stat().st_size)},
                timeout=600,
            )

    def path_of(self, file: dict) -> Path:
        """The file's bytes, fetched once into the cache, again when it changed."""
        fields = file["fields"]
        key = hashlib.sha1(file["id"].encode()).hexdigest()[:16]
        stamp = re.sub(r"[^0-9A-Za-z]+", "", f"{fields.get('size', 0)}{fields.get('modified', '')}")
        suffix = Path(str(fields.get("name") or "")).suffix or ".bin"
        target = self.cache / f"{key}-{stamp}{suffix}"
        if target.exists():
            return target
        with self._guard:
            lock = self._locks.setdefault(key, threading.Lock())
        with lock:
            if target.exists():
                return target
            self.cache.mkdir(parents=True, exist_ok=True)
            for old in self.cache.glob(f"{key}-*"):
                old.unlink(missing_ok=True)
            part = target.with_name(target.name + ".part")
            request = urllib.request.Request(
                f"{self.url}/api/records/{FILE}/{urllib.parse.quote(file['id'], safe='')}/content"
            )
            request.add_header("Authorization", "Bearer " + self.token)
            log("fetching", fields.get("path") or fields.get("name"))
            try:
                with urllib.request.urlopen(request, timeout=600) as answer, part.open("wb") as out:
                    shutil.copyfileobj(answer, out, 1 << 20)
            except urllib.error.HTTPError as exc:
                part.unlink(missing_ok=True)
                raise ApiError(f"fetching {fields.get('name')}: {exc.code}", exc.code) from None
            part.replace(target)
        return target


# -- ffmpeg ------------------------------------------------------------------------------
def run(command: list[str], *, timeout: float | None = None, text: bool = True) -> subprocess.CompletedProcess:
    return subprocess.run(command, capture_output=True, text=text, timeout=timeout, check=False)


def failed_said(result: subprocess.CompletedProcess) -> str:
    err = result.stderr.decode("utf-8", "replace") if isinstance(result.stderr, bytes) else (result.stderr or "")
    lines = [line for line in err.strip().splitlines() if line.strip()]
    return (lines[-1] if lines else f"ffmpeg exited with {result.returncode}")[:300]


class Refused(Exception):
    """A frames request that cannot be answered: (status, what to say)."""

    def __init__(self, status: int, said: str) -> None:
        super().__init__(said)
        self.status = status


class Renderer:
    """Everything that needs ffmpeg, over a store (the record API, or a test's stand-in)."""

    def __init__(self, store, home: Path | None = None) -> None:
        self.store = store
        self.home = home or Path(tempfile.gettempdir())
        self.ffmpeg = shutil.which("ffmpeg")
        self.ffprobe = shutil.which("ffprobe")
        self._info: dict[tuple, dict] = {}

    @property
    def ready(self) -> bool:
        return bool(self.ffmpeg and self.ffprobe)

    # -- what a file is --------------------------------------------------------------
    def file(self, file_id: str, user: str | None = None) -> dict:
        try:
            found = self.store.record(FILE, file_id)
        except ApiError as exc:
            raise Refused(404 if exc.status in (403, 404) else 502, f"no such file: {file_id}") from None
        self._mine(found, user)
        return found

    @staticmethod
    def _mine(record: dict, user: str | None) -> None:
        """The renderer reads as the administrator who installed it; a person is shown only their own."""
        if user and record.get("owner") and record["owner"] != user:
            raise Refused(404, "not found")

    def info(self, file: dict) -> dict:
        fields = file["fields"]
        key = (file["id"], fields.get("modified"), fields.get("size"))
        if key not in self._info:
            path = self.store.path_of(file)
            result = run(graph.probe_command(self.ffprobe, str(path)), timeout=FRAME_TIMEOUT)
            if result.returncode != 0:
                raise Refused(422, f"not a video or a sound the server can read: {failed_said(result)}")
            try:
                self._info[key] = graph.parse_info(json.loads(result.stdout or "{}"))
            except (ValueError, json.JSONDecodeError) as exc:
                raise Refused(422, f"not a video or a sound the server can read: {exc}") from None
            self.remember(file, self._info[key])
        return dict(self._info[key])

    def remember(self, file: dict, info: dict) -> None:
        """Keep what ffprobe said as a media record, for the Clips screen and new projects."""
        fields = {"source": file["id"], "name": file["fields"].get("name") or "",
                  "modified": str(file["fields"].get("modified") or ""), **info}  # fmt: skip
        try:
            known = self.store.records(MEDIA, source=file["id"])
            if not known:
                self.store.create(MEDIA, fields)
            elif any(str(known[0]["fields"].get(k)) != str(v) for k, v in fields.items()):
                self.store.patch(MEDIA, known[0]["id"], fields)
        except ApiError as exc:
            log("remembering", fields["name"], "-", exc)

    def source_jpeg(self, file_id: str, t: float, width: int, user: str | None = None) -> bytes:
        file = self.file(file_id, user)
        info = self.info(file)
        if not info["video"]:
            raise Refused(422, "it has no picture")
        t = min(max(0.0, t), max(0.0, info["duration"] - 1 / (info["fps"] or 25)))
        return self._jpeg(graph.source_command(self.ffmpeg, str(self.store.path_of(file)), t, width))

    # -- a project, as graph.py wants it --------------------------------------------------
    def project(self, project_id: str, user: str | None = None) -> dict:
        try:
            found = self.store.record(PROJECT, project_id)
        except ApiError as exc:
            raise Refused(404 if exc.status in (403, 404) else 502, f"no such project: {project_id}") from None
        self._mine(found, user)
        return found

    def pieces(self, project: dict, *, with_effects: bool = True) -> list[dict]:
        """The project's pieces with what their sources are, and their effects."""
        out = []
        for piece in self.store.records(PIECE, project=project["id"]):
            fields = piece["fields"]
            if not fields.get("source") or graph.number(fields.get("length")) <= 0:
                continue
            try:
                file = self.store.record(FILE, fields["source"])
            except ApiError:
                raise Refused(409, f"the file of {fields.get('name') or 'a piece'} is gone from My Files") from None
            info = self.info(file)
            effects = []
            if with_effects and info["video"]:
                effects = [
                    {"strength": e["fields"].get("strength") or 20, "keyframes": e["fields"].get("keyframes")}
                    for e in self.store.records(EFFECT, piece=piece["id"])
                    if (e["fields"].get("kind") or "blur") == "blur"
                ]
            out.append(
                {
                    "id": piece["id"],
                    "track": fields.get("track") or "",
                    "start": graph.number(fields.get("start")),
                    "offset": graph.number(fields.get("offset")),
                    "length": graph.number(fields.get("length")),
                    "volume": graph.number(fields.get("volume"), 1.0)
                    if fields.get("volume") not in (None, "")
                    else 1.0,
                    "muted": bool(fields.get("muted")),
                    "path": str(self.store.path_of(file)),
                    "file": file,
                    "video": info["video"],
                    "audio": info["audio"],
                    "width": info["width"],
                    "height": info["height"],
                    "fps": info["fps"],
                    "effects": effects,
                }
            )
        return out

    @staticmethod
    def settings(project: dict, pieces: list[dict]) -> dict:
        """The project's picture: its own size and frame rate, or its first video's while it has none."""
        fields = dict(project["fields"])
        if not (fields.get("width") and fields.get("height") and graph.number(fields.get("fps"))):
            first = next((p for p in sorted(pieces, key=lambda p: p["start"]) if p["video"]), None)
            if first is not None:
                fields["width"] = fields.get("width") or first["width"]
                fields["height"] = fields.get("height") or first["height"]
                fields["fps"] = graph.number(fields.get("fps")) or first["fps"] or 25
        return fields

    def frame_jpeg(self, project_id: str, t: float, width: int, user: str | None = None) -> bytes:
        project = self.project(project_id, user)
        pieces = self.pieces(project)
        return self._jpeg(graph.frame_command(self.ffmpeg, self.settings(project, pieces), pieces, max(0.0, t), width))

    def _jpeg(self, command: list[str]) -> bytes:
        try:
            result = run(command, timeout=FRAME_TIMEOUT, text=False)
        except subprocess.TimeoutExpired:
            raise Refused(504, "drawing the frame took too long") from None
        if result.returncode != 0 or not result.stdout:
            raise Refused(500, f"ffmpeg could not draw it: {failed_said(result)}")
        return result.stdout

    # -- filling in what the kit left blank -----------------------------------------------
    def fill(self) -> None:
        """Pieces without a length get their source's; projects without a size get their first video's."""
        blank_projects = {}
        for project in self.store.records(PROJECT):
            f = project["fields"]
            if not (f.get("width") and f.get("height") and graph.number(f.get("fps"))):
                blank_projects[project["id"]] = project
        for piece in self.store.records(PIECE):
            fields = piece["fields"]
            needs_length = fields.get("length") in (None, "") or graph.number(fields.get("length")) <= 0
            if not (needs_length or fields.get("project") in blank_projects) or not fields.get("source"):
                continue
            try:
                info = self.info(self.store.record(FILE, fields["source"]))
            except (Refused, ApiError) as exc:
                log("piece", piece["id"], "-", exc)
                continue
            if needs_length:
                length = max(0.0, info["duration"] - graph.number(fields.get("offset")))
                self.store.patch(PIECE, piece["id"], {"length": round(length, 3)})
                log("length of", fields.get("name"), "is", length)
            project = blank_projects.get(fields.get("project"))
            if project is not None and info["video"]:
                f = project["fields"]
                said = {
                    "width": f.get("width") or info["width"],
                    "height": f.get("height") or info["height"],
                    "fps": graph.number(f.get("fps")) or info["fps"] or 25,
                }
                self.store.patch(PROJECT, project["id"], said)
                blank_projects.pop(project["id"])
                log("project", f.get("name"), "is", said)

    # -- exports ---------------------------------------------------------------------
    def render_queued(self) -> None:
        for project in self.store.records(PROJECT, status="queued"):
            self.render(project)

    def render(self, project: dict) -> dict:
        """Render one project's export; the project as it is after, done or failed."""
        fields = project["fields"]
        if not self.ready:
            return self.store.patch(PROJECT, project["id"], {"status": "failed", "message": NO_FFMPEG})
        self.store.patch(PROJECT, project["id"], {"status": "rendering", "message": "Rendering"})
        fmt = fields.get("format") or "mp4"
        size = fields.get("size") or "project"
        started = time.time()
        try:
            pieces = self.pieces(project)
            if not pieces:
                raise ValueError("there is nothing on the tracks")
            share, folder = EXPORTS
            ensure_folder(self.store, share, folder)
            name = unique_name(
                self.store, share, folder, export_stem(fields.get("export_name") or fields.get("name")), fmt
            )
            self.home.mkdir(parents=True, exist_ok=True)
            with tempfile.TemporaryDirectory(dir=self.home) as tmp:
                out = Path(tmp) / ("out" + graph.FORMATS[fmt]["suffix"])
                command = graph.export_command(self.ffmpeg, self.settings(project, pieces), pieces, fmt, size, str(out))
                log("rendering", fields.get("name"), "as", fmt, size, "from", len(pieces), "pieces")
                result = run(command)
                if result.returncode != 0 or not out.exists():
                    raise ValueError(f"ffmpeg: {failed_said(result)}")
                made = self.store.upload(share, folder, name, out)
        except (ValueError, Refused, ApiError, OSError) as exc:
            log("render", fields.get("name"), "failed:", exc)
            return self.store.patch(
                PROJECT, project["id"], {"status": "failed", "message": f"The export failed: {exc}"[:400]}
            )
        where = "/".join(x for x in ("My Files" if share == "my-files" else share, folder) if x)
        said = f"Exported {name} to {where} in {time.time() - started:.0f} s"
        log(said)
        return self.store.patch(PROJECT, project["id"], {"status": "done", "message": said, "output": made["id"]})


def ensure_folder(store, share: str, folder: str) -> None:
    """The exports' folder, made the first time there is something to put in it."""
    try:
        if any(
            f["fields"].get("kind") == "folder" and f["fields"].get("name") == folder
            for f in store.records(FILE, share=share, folder="")
        ):
            return
        store.create(FILE, {"share": share, "folder": "", "name": folder, "kind": "folder"})
    except ApiError as exc:
        # Made meanwhile is as good; anything else, the upload says.
        log("the exports folder:", exc)


def export_stem(name) -> str:
    """What an export is called, without the suffix; nothing a file name cannot hold."""
    stem = re.sub(r'[\\/:*?"<>|\x00-\x1f]+', " ", str(name or "")).strip(" .")
    return stem or "Export"


def unique_name(store, share: str, folder: str, stem: str, fmt: str) -> str:
    """`<stem>.<suffix>`, or `<stem> 2.<suffix>` and so on when that is taken in the folder."""
    suffix = graph.FORMATS[fmt]["suffix"]
    try:
        taken = {str(f["fields"].get("name", "")).lower() for f in store.records(FILE, share=share, folder=folder)}
    except ApiError:
        taken = set()
    name, n = f"{stem}{suffix}", 1
    while name.lower() in taken:
        n += 1
        name = f"{stem} {n}{suffix}"
    return name


# -- the frames API ------------------------------------------------------------------------
def serve(renderer: Renderer, port: int, host: str = "127.0.0.1") -> ThreadingHTTPServer:
    """The frames API on *port*, in a thread of its own; the server, to shut down."""

    class Frames(BaseHTTPRequestHandler):
        server_version = "cutmorrow-frames"

        def log_message(self, fmt, *args) -> None:  # quiet: the core logs requests
            pass

        def do_GET(self) -> None:
            url = urllib.parse.urlsplit(self.path)
            what = url.path.strip("/").removeprefix("frames").strip("/")
            query = {k: v[-1] for k, v in urllib.parse.parse_qs(url.query).items()}
            user = self.headers.get("X-Cloudmorrow-User") or None
            try:
                if not renderer.ready:
                    raise Refused(503, NO_FFMPEG)
                if what == "info":
                    self._json(200, renderer.info(renderer.file(_need(query, "file"), user)))
                elif what == "source":
                    self._jpeg(renderer.source_jpeg(_need(query, "file"), _t(query), _w(query), user))
                elif what == "frame":
                    self._jpeg(renderer.frame_jpeg(_need(query, "project"), _t(query), _w(query), user))
                else:
                    raise Refused(404, "the frames API answers info, source and frame")
            except Refused as exc:
                self._json(exc.status, {"detail": str(exc)})
            except ApiError as exc:
                self._json(502, {"detail": str(exc)})
            except Exception as exc:  # noqa: BLE001 — said, not a dropped connection
                log("frames:", repr(exc))
                self._json(500, {"detail": str(exc)[:300]})

        def _json(self, status: int, body: dict) -> None:
            raw = json.dumps(body).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)

        def _jpeg(self, raw: bytes) -> None:
            self.send_response(200)
            self.send_header("Content-Type", "image/jpeg")
            self.send_header("Content-Length", str(len(raw)))
            self.send_header("Cache-Control", "private, max-age=60")
            self.end_headers()
            self.wfile.write(raw)

    server = ThreadingHTTPServer((host, port), Frames)
    server.daemon_threads = True
    threading.Thread(target=server.serve_forever, name="frames", daemon=True).start()
    return server


def _need(query: dict, name: str) -> str:
    if not query.get(name):
        raise Refused(400, f"{name}= is needed")
    return query[name]


def _t(query: dict) -> float:
    try:
        return max(0.0, float(query.get("t") or 0))
    except ValueError:
        raise Refused(400, "t= is seconds") from None


def _w(query: dict) -> int:
    try:
        return min(3840, max(16, int(float(query.get("w") or 640))))
    except ValueError:
        raise Refused(400, "w= is pixels") from None


# -- running ---------------------------------------------------------------------------------
def every(what, name: str) -> None:
    while True:
        try:
            what()
        except ApiError as exc:
            log(name, "- the server:", exc)
        except Exception as exc:  # noqa: BLE001 — the next round may go better
            log(name, "- trouble:", repr(exc))
        time.sleep(EVERY)


def main() -> None:
    url, token = os.environ.get("CLOUDMORROW_URL", ""), os.environ.get("CLOUDMORROW_TOKEN", "")
    if not url or not token:
        print("CLOUDMORROW_URL and CLOUDMORROW_TOKEN are needed: this is started by the server", file=sys.stderr)
        sys.exit(2)
    home = Path(os.environ.get("HOME") or tempfile.gettempdir())
    renderer = Renderer(Store(url, token, home / "cache"), home / "work")
    log("up;", "ffmpeg at " + renderer.ffmpeg if renderer.ready else NO_FFMPEG)
    if os.environ.get("PORT"):
        serve(renderer, int(os.environ["PORT"]))
        log("frames on port", os.environ["PORT"])
    if renderer.ready:
        threading.Thread(target=every, args=(renderer.fill, "filling in"), name="fill", daemon=True).start()
    with contextlib.suppress(KeyboardInterrupt):
        every(renderer.render_queued, "exports")


if __name__ == "__main__":
    main()
