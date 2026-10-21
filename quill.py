"""Vidmorrow: the code behind the Clips screen and the buttons.

The editor itself is Cloudmorrow's timeline kit, declared in quill.toml:
projects, the pieces on their tracks and the blurs on the pieces, with the
kit's own field names. This is the rest, run in the sandbox as whoever
presses it: the list of material in My Files › Videos, starting a project
from one, and asking for an export. The video itself is never touched here:
services/render.py, a program beside the server, draws the frames, fills in
lengths and sizes, and renders the exports.
"""

import contextlib

from cloudmorrow.quill import action, error, go, redraw, toast, ui, view

PROJECT, PIECE, MEDIA, FILE = "vidmorrow.project", "vidmorrow.piece", "vidmorrow.media", "file"
DRIVE, FOLDER = "my-files", "Videos"
TRACKS = [
    {"id": "V2", "kind": "video", "name": "Video 2"},
    {"id": "V1", "kind": "video", "name": "Video 1"},
    {"id": "A1", "kind": "audio", "name": "Audio 1"},
    {"id": "A2", "kind": "audio", "name": "Audio 2"},
]
VIDEO_SUFFIXES = (".mp4", ".mov", ".m4v", ".mkv", ".webm", ".avi", ".mts", ".m2ts", ".3gp", ".mpg", ".mpeg", ".ogv")
AUDIO_SUFFIXES = (".mp3", ".m4a", ".aac", ".wav", ".flac", ".ogg", ".oga", ".opus", ".wma", ".aif", ".aiff")
FORMATS = {"mp4": "MP4", "webm": "WebM", "mov": "MOV", "gif": "GIF", "m4a": "M4A"}
STATUS_TONE = {"editing": "neutral", "queued": "info", "rendering": "warn", "done": "good", "failed": "bad"}
STATUS_SAID = {
    "editing": "Editing",
    "queued": "Waiting to be exported",
    "rendering": "Exporting",
    "done": "Exported",
    "failed": "The export failed",
}


# -- numbers and words ---------------------------------------------------------------------
def number(value, fallback=0.0):
    """A decimal field, which the store keeps as text, as a float."""
    try:
        return float(value)
    except (TypeError, ValueError):
        return fallback


def clock(seconds):
    """Seconds as 1:03.5 — minutes, seconds and tenths, only what is needed."""
    seconds = max(0.0, number(seconds))
    minutes, rest = divmod(round(seconds, 1), 60)
    said = f"{rest:04.1f}" if rest != int(rest) else f"{int(rest):02d}"
    return f"{int(minutes)}:{said}"


def bytes_said(size):
    size = number(size)
    for unit in ("bytes", "KB", "MB", "GB"):
        if size < 1000 or unit == "GB":
            return f"{size:.0f} {unit}" if unit == "bytes" else f"{size:.1f} {unit}"
        size /= 1000
    return ""


def kind_of(record):
    """'video', 'audio', or None for a file that is neither."""
    name = str(record.get("name") or "").lower()
    mime = str(record.get("mime") or "")
    if mime.startswith("video/") or name.endswith(VIDEO_SUFFIXES):
        return "video"
    if mime.startswith("audio/") or name.endswith(AUDIO_SUFFIXES):
        return "audio"
    return None


def material(ctx):
    """Every video and sound under My Files › Videos; None when the folder is not there."""
    try:
        files = ctx.records.list(FILE, share=DRIVE, within=FOLDER)
    except Exception:  # noqa: BLE001 — no folder yet, or a drive the server cannot show
        return None
    found = [f for f in files if f.get("kind") == "file" and kind_of(f)]
    return sorted(found, key=lambda f: (str(f.get("folder") or "").lower(), str(f.get("name") or "").lower()))


def known_lengths(ctx):
    """How long each file is, as the renderer read it; a file changed since is not known."""
    return {
        m["source"]: (number(m["duration"]), str(m.get("modified") or ""))
        for m in ctx.records.list(MEDIA)
        if number(m.get("duration")) > 0
    }


def length_of(lengths, file):
    length, modified = lengths.get(file.id, (0.0, ""))
    return length if length and modified == str(file.get("modified") or "") else None


def place(record):
    """Where a file is under Videos: its subfolder, or nothing at the top."""
    folder = str(record.get("folder") or "")
    return folder[len(FOLDER) :].strip("/") if folder.startswith(FOLDER) else folder


# -- the clips ------------------------------------------------------------------------------
@view("clips")
def clips(ctx):
    found = material(ctx)
    if found is None:
        # The first look makes the folder, so there is somewhere to put them;
        # it may have been made meanwhile, or be in a drive that cannot have it.
        with contextlib.suppress(Exception):
            ctx.records.create(FILE, share=DRIVE, folder="", name=FOLDER, kind="folder")
        found = []
    lengths = known_lengths(ctx)
    rows = []
    for f in found:
        row = f.to_dict()
        row["fields"] = {
            **row["fields"],
            "where": place(f) or "—",
            "what": "Video" if kind_of(f) == "video" else "Sound",
            "bytes": bytes_said(f.get("size")),
            "length": clock(length_of(lengths, f)) if length_of(lengths, f) else "",
        }
        rows.append(row)
    parts = [
        ui.text("Clips", style="title"),
        ui.text("Everything in My Files › Videos, subfolders too. Exports land beside the originals.", style="muted"),
        ui.table(
            rows,
            columns=[("Folder", "where"), "name", ("Kind", "what"), ("Size", "bytes"), ("Length", "length")],
            open=False,
            actions=["edit-video"],
            empty="No videos yet. Put videos (or sounds) in the Videos folder of My Files — from Files, the desktop "
            "app's mount or WebDAV — and they are listed here.",
        ),
    ]
    projects = sorted(ctx.records.list(PROJECT), key=lambda p: p.updated_at, reverse=True)[:8]
    if projects:
        parts.append(ui.divider())
        parts.append(ui.text("Recent projects", style="subtitle"))
        for project in projects:
            status = project.get("status") or "editing"
            parts.append(
                ui.row(
                    ui.text(project["name"]),
                    ui.badge(STATUS_SAID.get(status, status), tone=STATUS_TONE.get(status, "neutral")),
                    project.get("output")
                    and ui.button("Open the export", open={"id": project["output"], "model": FILE}),
                    ui.button("Edit", go="editor", params={"project": project.id}),
                )
            )
    parts.append(ui.button("Refresh", action="refresh"))
    if not ctx.user.admin:
        parts.append(
            ui.text(
                "The renderer runs as the administrator who installed Vidmorrow, so for now it draws and "
                "exports their projects; yours wait until Quill services can run for everyone.",
                style="small",
            )
        )
    return ui.stack(*parts)


@action("edit_video")
def edit_video(ctx, video, name=""):
    kind = kind_of(video) if video.get("kind") == "file" else None
    if kind is None:
        return error(f"{video['name']} is neither a video nor a sound")
    title = (name or "").strip() or str(video["name"]).rsplit(".", 1)[0]
    project = ctx.records.create(PROJECT, name=title, tracks=TRACKS, status="editing")
    length = length_of(known_lengths(ctx), video)
    piece = {
        "project": project.id,
        "track": "V1" if kind == "video" else "A1",
        "source": video.id,
        "name": video["name"],
        "start": 0,
        "offset": 0,
        "volume": 1,
        "muted": False,
    }
    if length:
        piece["length"] = round(length, 3)
    ctx.records.create(PIECE, piece)
    return [toast(f"Editing {title}"), go("editor", project=project.id)]


@action("export")
def export(ctx, project, format="mp4", size="project", name=""):
    if project.get("status") in ("queued", "rendering"):
        return error("It is being exported already; wait for it to finish")
    pieces = ctx.records.list(PIECE, project=project.id)
    if not pieces:
        return error("Put something on the tracks first: there is nothing to export")
    if format == "m4a" and all(p.get("muted") for p in pieces):
        return error("Every piece is muted: a sound-only export would be silent")
    if format not in FORMATS:
        return error(f"No such format: {format}")
    ctx.records.patch(
        PROJECT,
        project.id,
        {
            "status": "queued",
            "message": f"Waiting to be exported as {FORMATS[format]}",
            "format": format,
            "size": size or "project",
            "export_name": (name or "").strip() or project["name"],
        },
    )
    return toast("Queued: the export lands beside the first video in My Files › Videos")


@action("refresh")
def refresh(ctx):
    return redraw()
