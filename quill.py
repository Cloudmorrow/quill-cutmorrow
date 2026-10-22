"""CutMorrow: the code behind the buttons.

The editor itself is Cloudmorrow's timeline kit, declared in quill.toml:
projects, the pieces on their tracks and the blurs on the pieces, with the
kit's own field names. This is the rest, run in the sandbox as whoever
presses it: starting a project from a video (a button on its sheet, in
Media or Files), and asking for an export. The video itself is never touched here:
services/render.py, a program beside the server, draws the frames, fills in
lengths and sizes, and renders the exports.
"""

from cloudmorrow.quill import action, error, go, redraw, toast

PROJECT, PIECE, MEDIA, FILE = "cutmorrow.project", "cutmorrow.piece", "cutmorrow.media", "file"
EXPORTS = "CutMorrow Exports"
TRACKS = [
    {"id": "V2", "kind": "video", "name": "Video 2"},
    {"id": "V1", "kind": "video", "name": "Video 1"},
    {"id": "A1", "kind": "audio", "name": "Audio 1"},
    {"id": "A2", "kind": "audio", "name": "Audio 2"},
]
VIDEO_SUFFIXES = (".mp4", ".mov", ".m4v", ".mkv", ".webm", ".avi", ".mts", ".m2ts", ".3gp", ".mpg", ".mpeg", ".ogv")
AUDIO_SUFFIXES = (".mp3", ".m4a", ".aac", ".wav", ".flac", ".ogg", ".oga", ".opus", ".wma", ".aif", ".aiff")
FORMATS = {"mp4": "MP4", "webm": "WebM", "mov": "MOV", "gif": "GIF", "m4a": "M4A"}


# -- numbers and words ---------------------------------------------------------------------
def number(value, fallback=0.0):
    """A decimal field, which the store keeps as text, as a float."""
    try:
        return float(value)
    except (TypeError, ValueError):
        return fallback


def kind_of(record):
    """'video', 'audio', or None for a file that is neither."""
    name = str(record.get("name") or "").lower()
    mime = str(record.get("mime") or "")
    if mime.startswith("video/") or name.endswith(VIDEO_SUFFIXES):
        return "video"
    if mime.startswith("audio/") or name.endswith(AUDIO_SUFFIXES):
        return "audio"
    return None


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


# -- buttons --------------------------------------------------------------------------------
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
    return toast(f"Queued: the export lands in My Files › {EXPORTS}")


@action("refresh")
def refresh(ctx):
    return redraw()
