"""Vidmorrow: the screens and buttons of a video editor for one job.

The data is declared in quill.toml and datamodels/: a clip cut from a file
in the person's drive, the blurs on it, and the keyframes that say where
each blur is at a moment. This is the code behind the two screens and the
buttons, run in the sandbox as whoever presses them. The video itself is
never touched here: services/render.py, a program beside the server, reads
the clip's size, draws a picture of every keyframe and renders the export.
"""

import contextlib

from cloudmorrow.quill import Invalid, action, error, go, hook, redraw, toast, ui, view

CLIP, EFFECT, KEYFRAME, FILE = "vidmorrow.clip", "vidmorrow.effect", "vidmorrow.keyframe", "file"
DRIVE, FOLDER = "my-files", "Vidmorrow"
VIDEO_SUFFIXES = (".mp4", ".mov", ".m4v", ".mkv", ".webm", ".avi", ".mts", ".m2ts", ".3gp", ".mpg", ".mpeg")
STATUS_TONE = {
    "new": "info",
    "ready": "neutral",
    "queued": "info",
    "rendering": "warn",
    "done": "good",
    "failed": "bad",
}
STATUS_SAID = {
    "new": "Waiting for the renderer to read it",
    "ready": "Ready to edit",
    "queued": "Waiting to be rendered",
    "rendering": "Rendering",
    "done": "Exported",
    "failed": "The last render failed",
}


# -- numbers and words ---------------------------------------------------------------------
def number(value, fallback=0.0):
    """A decimal field, which the store keeps as text, as a float."""
    try:
        return float(value)
    except (TypeError, ValueError):
        return fallback


def clock(seconds):
    """Seconds as 0:03.5 — minutes, seconds and tenths, only what is needed."""
    seconds = max(0.0, number(seconds))
    minutes, rest = divmod(seconds, 60)
    rest = round(rest, 1)
    said = f"{rest:04.1f}" if rest != int(rest) else f"{int(rest):02d}"
    return f"{int(minutes)}:{said}"


def box_said(record):
    """A keyframe's box in words: 35,25 30×40 (percent)."""
    return f"{number(record['x']):g},{number(record['y']):g} {number(record['w']):g}×{number(record['h']):g}"


def check_box(x, y, w, h):
    """A box must be on the frame and have some size; said plainly when it is not."""
    x, y, w, h = (number(v) for v in (x, y, w, h))
    if w <= 0 or h <= 0:
        raise Invalid("The box needs a width and a height above 0")
    if x < 0 or y < 0 or x + w > 100 or y + h > 100:
        raise Invalid("The box must stay on the frame: left + width and top + height are at most 100")
    return x, y, w, h


def is_video(record):
    name = str(record.get("name") or "").lower()
    return str(record.get("mime") or "").startswith("video/") or name.endswith(VIDEO_SUFFIXES)


def videos_in_folder(ctx):
    """The videos in the Vidmorrow folder; None when it cannot be read.

    Listing files seeds the welcome note, which makes the folder the first time."""
    try:
        files = ctx.records.list(FILE, share=DRIVE, folder=FOLDER)
    except Exception:  # noqa: BLE001 — no folder, or a drive the server cannot show
        return None
    return [f for f in files if f.get("kind") == "file" and is_video(f)]


def keyframes_of(ctx, effect_id):
    return sorted(ctx.records.list(KEYFRAME, effect=effect_id), key=lambda k: number(k["at"]))


def span_said(keyframes):
    if not keyframes:
        return "no keyframes yet"
    first, last = number(keyframes[0]["at"]), number(keyframes[-1]["at"])
    if len(keyframes) == 1:
        return f"from {clock(first)} to the end"
    return f"from {clock(first)} to {clock(last)}"


# -- the clips ------------------------------------------------------------------------------
@view("clips")
def clips(ctx):
    clips = sorted(ctx.records.list(CLIP), key=lambda c: c.created_at, reverse=True)
    videos = videos_in_folder(ctx)
    used = {c["source"] for c in clips} | {c["output"] for c in clips if c.get("output")}
    parts = [ui.text("Clips", style="title")]
    if clips:
        for clip in clips:
            parts.append(
                ui.row(
                    ui.text(clip["name"]),
                    ui.badge(STATUS_SAID[clip["status"]], tone=STATUS_TONE[clip["status"]]),
                    ui.button("Open", go="clip", params={"clip": clip.id}),
                )
            )
    else:
        parts.append(ui.text("No clips yet: pick a video below.", style="muted"))
    parts.append(ui.divider())
    parts.append(ui.text("Videos in My Files › Vidmorrow", style="subtitle"))
    if videos is None:
        parts.append(ui.text("The Vidmorrow folder in My Files could not be read; look at it in Files.", style="muted"))
    else:
        fresh = [v for v in videos if v.id not in used]
        parts.append(
            ui.table(
                fresh,
                columns=["name", ("Size", "size"), ("Changed", "modified")],
                open=False,
                actions=["new-clip"],
                empty="Put a video in the Vidmorrow folder of My Files, and it is listed here.",
            )
        )
    if not ctx.user.admin:
        parts.append(
            ui.text(
                "The renderer runs as the administrator who installed Vidmorrow, so for now it "
                "renders their clips; yours wait until Quill services can run for everyone.",
                style="small",
            )
        )
    return ui.stack(*parts)


@action("new_clip")
def new_clip(ctx, video, name=""):
    if video.get("kind") != "file" or not is_video(video):
        return error(f"{video['name']} is not a video")
    taken = ctx.records.list(CLIP, source=video.id)
    if taken:
        return go("clip", clip=taken[0].id)
    name = (name or "").strip() or str(video["name"]).rsplit(".", 1)[0]
    clip = ctx.records.create(CLIP, name=name, source=video.id, status="new")
    return [toast(f"Editing {name}"), go("clip", clip=clip.id)]


# -- one clip --------------------------------------------------------------------------------
@view("clip")
def clip(ctx):
    clip_id = ctx.params.get("clip") or (ctx.params.record.id if ctx.params.record is not None else None)
    if not clip_id:
        return _choose(ctx)
    try:
        clip = ctx.records.get(CLIP, clip_id)
    except Exception:  # noqa: BLE001 — gone, or somebody else's
        return _choose(ctx, "That clip is gone.")
    effects = sorted(ctx.records.list(EFFECT, clip=clip.id), key=lambda e: e.created_at)
    parts = [ui.text(clip["name"], style="title"), _about(clip)]
    for effect in effects:
        parts.append(ui.divider())
        parts.extend(_effect(ctx, clip, effect))
    parts.append(ui.divider())
    buttons = [ui.button("Add a blur", action="add-blur", record=clip, tone="primary")]
    if effects and clip["status"] in ("ready", "done", "failed"):
        buttons.append(ui.button("Export", action="export", record=clip, tone="primary"))
    buttons.append(ui.button("Refresh", action="refresh"))
    buttons.append(ui.button("All clips", go="clips"))
    parts.append(ui.row(*buttons))
    return ui.stack(*parts)


def _choose(ctx, said=""):
    clips = sorted(ctx.records.list(CLIP), key=lambda c: c.created_at, reverse=True)
    parts = [ui.text("Editor", style="title")]
    if said:
        parts.append(ui.text(said, style="muted"))
    if not clips:
        parts.append(ui.text("No clips yet. Pick a video on the Clips screen.", style="muted"))
    for clip in clips:
        parts.append(ui.row(ui.text(clip["name"]), ui.button("Open", go="clip", params={"clip": clip.id})))
    parts.append(ui.button("Clips", go="clips"))
    return ui.stack(*parts)


def _about(clip):
    status = clip["status"]
    facts = [ui.badge(STATUS_SAID[status], tone=STATUS_TONE[status])]
    if clip.get("width") and clip.get("height"):
        facts.append(ui.text(f"{clip['width']}×{clip['height']}", style="muted"))
    if clip.get("duration"):
        facts.append(ui.text(clock(clip["duration"]) + " long", style="muted"))
    if clip.get("fps"):
        facts.append(ui.text(f"{number(clip['fps']):g} fps", style="muted"))
    parts = [ui.row(*facts)]
    if clip.get("message"):
        parts.append(ui.text(clip["message"], style="small"))
    if status == "done" and clip.get("output"):
        parts.append(
            ui.row(
                ui.text("Exported beside the original in My Files › Vidmorrow.", style="muted"),
                ui.button("Open the export", open={"id": clip["output"], "model": FILE}),
            )
        )
    return ui.stack(*parts, gap="small")


def _effect(ctx, clip, effect):
    keyframes = keyframes_of(ctx, effect.id)
    parts = [
        ui.text(f"{effect['label']} · blur {effect.get('strength') or 20} · {span_said(keyframes)}", style="subtitle")
    ]
    pictures = [
        ui.image(record={"id": k["preview"], "model": FILE}, alt=f"{clock(k['at'])}: {box_said(k)}")
        for k in keyframes
        if k.get("preview")
    ]
    if pictures:
        parts.append(ui.row(*pictures))
    elif keyframes and clip["status"] != "new":
        parts.append(ui.text("Pictures of the keyframes are on their way; refresh in a moment.", style="small"))
    parts.append(
        ui.table(
            keyframes,
            columns=[("At", "at"), ("Left %", "x"), ("Top %", "y"), ("Width %", "w"), ("Height %", "h")],
            empty="No keyframes: the blur is nowhere yet.",
        )
    )
    last = keyframes[-1] if keyframes else None
    args = (
        {"at": round(number(last["at"]) + 1, 1), "x": last["x"], "y": last["y"], "w": last["w"], "h": last["h"]}
        if last is not None
        else {"at": 0, "x": 35, "y": 25, "w": 30, "h": 40}
    )
    parts.append(
        ui.row(
            ui.button("Add a keyframe", action="add-keyframe", record=effect, args=args),
            ui.button("Remove", action="remove-effect", record=effect, tone="danger"),
        )
    )
    return parts


# -- the buttons -------------------------------------------------------------------------
@action("add_blur")
def add_blur(ctx, clip, label="Blur", at=0, x=35, y=25, w=30, h=40, strength=20):
    x, y, w, h = check_box(x, y, w, h)
    if number(at) < 0:
        return error("A keyframe is at 0 seconds or later")
    strength = min(100, max(1, int(strength or 20)))
    effect = ctx.records.create(
        EFFECT, clip=clip.id, kind="blur", label=(label or "Blur").strip() or "Blur", strength=strength
    )
    ctx.records.create(KEYFRAME, clip=clip.id, effect=effect.id, at=number(at), x=x, y=y, w=w, h=h)
    return toast(f"Added {effect['label']} at {clock(at)}")


@action("add_keyframe")
def add_keyframe(ctx, effect, at, x, y, w, h):
    x, y, w, h = check_box(x, y, w, h)
    if number(at) < 0:
        return error("A keyframe is at 0 seconds or later")
    for other in ctx.records.list(KEYFRAME, effect=effect.id):
        if abs(number(other["at"]) - number(at)) < 0.001:
            return error(f"There is a keyframe at {clock(at)} already; open it to move it")
    ctx.records.create(KEYFRAME, clip=effect["clip"], effect=effect.id, at=number(at), x=x, y=y, w=w, h=h)
    return toast(f"Keyframe at {clock(at)}")


@action("remove_effect")
def remove_effect(ctx, effect):
    for keyframe in ctx.records.list(KEYFRAME, effect=effect.id):
        ctx.records.delete(KEYFRAME, keyframe.id)
    ctx.records.delete(EFFECT, effect.id)
    return toast(f"Removed {effect['label']}")


@action("export")
def export(ctx, clip):
    if clip["status"] == "new":
        return error("The renderer has not read this clip yet; try again in a moment")
    if clip["status"] in ("queued", "rendering"):
        return error("It is being rendered already")
    if not ctx.records.list(EFFECT, clip=clip.id):
        return error("Add a blur first: there is nothing to render")
    ctx.records.patch(CLIP, clip.id, {"status": "queued", "message": ""})
    return toast("Queued: the export lands beside the original when it is done")


@action("refresh")
def refresh(ctx):
    return redraw()


# -- keyframes and their pictures -----------------------------------------------------------
@hook
def keyframe_changed(ctx, change):
    """A changed keyframe wants a new picture; a deleted one takes its picture with it."""
    keyframe = change.record
    if change.action == "deleted":
        if keyframe.get("preview"):
            with contextlib.suppress(Exception):  # gone already
                ctx.records.delete(FILE, keyframe["preview"])
        return
    if change.action == "changed" and not ({"at", "x", "y", "w", "h"} & set(change.changed)):
        return
    try:
        clip = ctx.records.get(CLIP, keyframe["clip"])
    except Exception:  # noqa: BLE001
        return
    if not clip.get("wants_previews"):
        ctx.records.patch(CLIP, clip.id, {"wants_previews": True})
