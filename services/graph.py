"""The arithmetic of CutMorrow's renderer: a timeline to ffmpeg commands.

Nothing here runs ffmpeg or talks to the server, so it is tested as plain
functions. The words are the timeline kit's (docs/TIMELINE.md in the core):

- a *project* is a dict with `width`, `height`, `fps`, `tracks` and `crop`;
- a *piece* is a dict with `track`, `start`, `offset`, `length`, `volume`,
  `muted`, and what the renderer read from its source: `path`, `video`,
  `audio`, `width`, `height`; and its `effects`;
- an *effect* is a dict with `strength` (1 to 100) and `keyframes`: dicts
  with `t` (seconds from the piece's start) and `x`, `y`, `w`, `h` (percent
  of the frame, before the crop).

Between two keyframes a box moves and resizes in a straight line; before the
first and after the last it stays put. An effect is on from its first
keyframe to its last, and through the whole piece when it has one.

The export is one filter graph: a black canvas of the project's size for the
whole cut; each video piece fitted onto it (letterboxed), its blurs drawn,
and laid over the canvas from its start for its length, the bottom track
first, so a higher track covers a lower one; then the crop, and the size
asked for. Every audible piece's sound is delayed to its start and mixed.

A blur is a mask: a small grey picture per effect, drawn by `geq` from the
box's expressions in `T` (the piece's own time), scaled up to the frame and
used as the alpha of a blurred copy laid over the picture. Blurring the
whole frame once per strength and masking it is what lets a box change size
from frame to frame, which `crop` cannot.
"""

from __future__ import annotations

import json
from itertools import pairwise

MASK_SCALE = 4  # a mask is drawn at a quarter of the frame, then scaled up
MIN_BOX = 2
GIF_FPS = 15.0
TRACKS = [
    {"id": "V2", "kind": "video", "name": "Video 2"},
    {"id": "V1", "kind": "video", "name": "Video 1"},
    {"id": "A1", "kind": "audio", "name": "Audio 1"},
    {"id": "A2", "kind": "audio", "name": "Audio 2"},
]
SIZES = {"project": None, "1080p": 1080, "720p": 720, "480p": 480}
# What each format is: its file's suffix, whether it has a picture and
# sound, and how ffmpeg encodes them.
FORMATS = {
    "mp4": {
        "suffix": ".mp4",
        "video": ["-c:v", "libx264", "-preset", "veryfast", "-crf", "20", "-pix_fmt", "yuv420p"],
        "audio": ["-c:a", "aac", "-b:a", "160k"],
        "extra": ["-movflags", "+faststart"],
    },
    "webm": {
        "suffix": ".webm",
        "video": ["-c:v", "libvpx-vp9", "-b:v", "0", "-crf", "33", "-row-mt", "1"]
        + ["-deadline", "good", "-cpu-used", "4", "-pix_fmt", "yuv420p"],
        "audio": ["-c:a", "libopus", "-b:a", "128k"],
        "extra": [],
    },
    "mov": {
        "suffix": ".mov",
        "video": ["-c:v", "libx264", "-preset", "veryfast", "-crf", "20", "-pix_fmt", "yuv420p"],
        "audio": ["-c:a", "aac", "-b:a", "160k"],
        "extra": ["-movflags", "+faststart"],
    },
    "gif": {"suffix": ".gif", "video": ["-loop", "0"], "audio": None, "extra": []},
    "m4a": {
        "suffix": ".m4a",
        "video": None,
        "audio": ["-c:a", "aac", "-b:a", "192k"],
        "extra": ["-movflags", "+faststart"],
    },
}


# -- reading what the store gives --------------------------------------------------
def number(value, fallback: float = 0.0) -> float:
    """A decimal field, which the store keeps as text, as a float."""
    try:
        return float(value)
    except (TypeError, ValueError):
        return fallback


def loaded(value, fallback):
    """A json field: as the API gives it, or as text."""
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except ValueError:
            return fallback
    return fallback if value is None else value


def keyframes_of(raw) -> list[dict]:
    """An effect's keyframes, cleaned: numbers, in time order, the broken ones dropped."""
    out = []
    for k in loaded(raw, []):
        if not isinstance(k, dict):
            continue
        try:
            out.append({name: float(k[name]) for name in ("t", "x", "y", "w", "h")})
        except (KeyError, TypeError, ValueError):
            continue
    return sorted(out, key=lambda k: k["t"])


def tracks_of(raw) -> list[dict]:
    """The project's tracks, top to bottom; the kit's four when it says none."""
    tracks = loaded(raw, None)
    if isinstance(tracks, list) and tracks and all(isinstance(t, dict) and t.get("id") for t in tracks):
        return [{"kind": "video", "name": t["id"], **t} for t in tracks]
    return [dict(t) for t in TRACKS]


# -- values over time -------------------------------------------------------------
def points(keyframes: list[dict], field: str) -> list[tuple[float, float]]:
    """(time, value) pairs of one field, in time order."""
    return sorted((float(k["t"]), float(k[field])) for k in keyframes)


def value_at(pts: list[tuple[float, float]], t: float) -> float:
    """The value at *t*: straight lines between the points, held outside them."""
    if not pts:
        raise ValueError("no keyframes")
    if t <= pts[0][0]:
        return pts[0][1]
    for (t0, v0), (t1, v1) in pairwise(pts):
        if t <= t1:
            return v0 if t1 == t0 else v0 + (v1 - v0) * (t - t0) / (t1 - t0)
    return pts[-1][1]


def expression(pts: list[tuple[float, float]], var: str = "T") -> str:
    """`value_at` as an ffmpeg expression in *var*."""
    if not pts:
        raise ValueError("no keyframes")
    said = _num(pts[-1][1])
    for (t0, v0), (t1, v1) in reversed(list(pairwise(pts))):
        if t1 == t0:
            continue
        line = f"{_num(v0)}+({_num(v1 - v0)})*({var}-{_num(t0)})/{_num(t1 - t0)}"
        said = f"if(lt({var},{_num(t1)}),{line},{said})"
    return f"if(lt({var},{_num(pts[0][0])}),{_num(pts[0][1])},{said})"


def span(keyframes: list[dict], length: float) -> tuple[float, float]:
    """From when to when an effect is on, in the piece's own seconds."""
    times = sorted(float(k["t"]) for k in keyframes)
    return (times[0], times[-1]) if len(times) > 1 else (0.0, length + 1)


def on_at(effect: dict, t: float, length: float) -> bool:
    keyframes = effect.get("keyframes") or []
    if not keyframes:
        return False
    start, end = span(keyframes, length)
    return start - 0.001 <= t <= end + 0.001


def _num(value: float) -> str:
    return f"{float(value):.4f}".rstrip("0").rstrip(".") or "0"


# -- pixels -----------------------------------------------------------------------
def even(value: float, floor: int = MIN_BOX) -> int:
    return max(floor, round(value / 2) * 2)


def canvas(project: dict) -> tuple[int, int]:
    """The project's picture, in even pixels (yuv420p wants them)."""
    return even(number(project.get("width"), 1280)), even(number(project.get("height"), 720))


def crop_px(crop, width: int, height: int) -> tuple[int, int, int, int]:
    """The crop, percent of the frame, as even pixels on it; all of it when blank."""
    crop = loaded(crop, None)
    if not isinstance(crop, dict):
        return 0, 0, width, height
    x, y = number(crop.get("x"), 0), number(crop.get("y"), 0)
    w, h = number(crop.get("w"), 100), number(crop.get("h"), 100)
    x, y = min(max(x, 0.0), 99.0), min(max(y, 0.0), 99.0)
    w, h = min(max(w, 1.0), 100 - x), min(max(h, 1.0), 100 - y)
    px, py = even(x * width / 100, 0), even(y * height / 100, 0)
    pw, ph = min(even(w * width / 100), width - px), min(even(h * height / 100), height - py)
    return px, py, max(MIN_BOX, pw - pw % 2), max(MIN_BOX, ph - ph % 2)


def out_size(size: str, width: int, height: int) -> tuple[int, int]:
    """The exported picture: the cropped one as it is, or its shorter side made 1080, 720 or 480."""
    side = SIZES.get(size or "project")
    if not side:
        return even(width), even(height)
    if width >= height:
        return even(width * side / height), even(side)
    return even(side), even(height * side / width)


def fit(src_w: int, src_h: int, width: int, height: int) -> tuple[int, int, int, int]:
    """A picture fitted into the frame, its shape kept: (w, h, x, y), the bars around it."""
    if src_w <= 0 or src_h <= 0:
        return width, height, 0, 0
    scale = min(width / src_w, height / src_h)
    w, h = min(width, even(src_w * scale)), min(height, even(src_h * scale))
    return w, h, (width - w) // 2 // 2 * 2, (height - h) // 2 // 2 * 2


def box_px(keyframes: list[dict], t: float, width: int, height: int) -> tuple[int, int, int, int]:
    """The box at *t*, in whole, even pixels, kept on the frame."""
    x = value_at(points(keyframes, "x"), t) * width / 100
    y = value_at(points(keyframes, "y"), t) * height / 100
    w = even(value_at(points(keyframes, "w"), t) * width / 100)
    h = even(value_at(points(keyframes, "h"), t) * height / 100)
    w, h = min(w, even(width)), min(h, even(height))
    x = min(max(0, even(x, 0)), width - w)
    y = min(max(0, even(y, 0)), height - h)
    return x, y, w, h


def radius(strength, width: int, height: int) -> int:
    """Blur radius in pixels: strength 20 on 1080p is about 22 pixels."""
    strength = min(100, max(1, int(number(strength, 20) or 20)))
    return max(1, min(round(strength * min(width, height) / 1000), min(width, height) // 4))


def blur(r: int, w: int, h: int) -> str:
    """A boxblur that fits a picture *w* by *h* (yuv420p: chroma is half)."""
    r = max(1, min(r, min(w, h) // 2 - 1 if min(w, h) > 3 else 1))
    return (
        f"boxblur=luma_radius={r}:luma_power=2:chroma_radius={max(1, min(r // 2, min(w, h) // 4 - 1))}:chroma_power=2"
    )


# -- the timeline -----------------------------------------------------------------
def duration(pieces: list[dict]) -> float:
    """Where the cut ends: the last piece's end."""
    return max((number(p["start"]) + number(p["length"]) for p in pieces), default=0.0)


def kinds(tracks: list[dict]) -> dict[str, str]:
    return {t["id"]: t.get("kind", "video") for t in tracks}


def video_layers(pieces: list[dict], tracks: list[dict]) -> list[dict]:
    """The pieces with a picture on video tracks, bottom track first, each track in time order."""
    order = {t["id"]: i for i, t in enumerate(tracks)}
    kind = kinds(tracks)
    seen = [p for p in pieces if kind.get(p["track"]) == "video" and p.get("video") and number(p["length"]) > 0]
    return sorted(seen, key=lambda p: (-order[p["track"]], number(p["start"])))


def top_piece_at(pieces: list[dict], tracks: list[dict], t: float) -> dict | None:
    """What is seen at *t*: the piece on the highest video track there, or None (black)."""
    on = [p for p in video_layers(pieces, tracks) if number(p["start"]) <= t < number(p["start"]) + number(p["length"])]
    return on[-1] if on else None


def audible(pieces: list[dict], tracks: list[dict]) -> list[dict]:
    """Every piece whose sound is heard: on a known track, not muted, its source has sound."""
    kind = kinds(tracks)
    return [
        p
        for p in pieces
        if p["track"] in kind
        and p.get("audio")
        and not p.get("muted")
        and number(p.get("volume"), 1.0) > 0
        and number(p["length"]) > 0
    ]


# -- the export -------------------------------------------------------------------
def blur_lines(effects: list[dict], width: int, height: int, fps: str, length: float, src: str, tag: str):
    """The lines that blur *effects* onto the stream *src* (the piece's own time, from 0); (lines, out label)."""
    effects = [e for e in effects if e.get("keyframes")]
    if not effects:
        return [], src
    groups: dict[int, list[dict]] = {}
    for effect in effects:
        groups.setdefault(radius(effect.get("strength", 20), width, height), []).append(effect)
    mw, mh = even(width / MASK_SCALE), even(height / MASK_SCALE)
    sx, sy = width / mw, height / mh
    outs = "".join(f"[{tag}s{i}]" for i in range(len(groups)))
    lines = [f"{src}split={len(groups) + 1}[{tag}base]{outs}"]
    previous = f"[{tag}base]"
    for g, (r, members) in enumerate(sorted(groups.items())):
        lines.append(f"[{tag}s{g}]{blur(r, width, height)}[{tag}blur{g}]")
        masks = []
        for m, effect in enumerate(members):
            keyframes = effect["keyframes"]
            start, end = span(keyframes, length)
            x, y = expression(points(keyframes, "x")), expression(points(keyframes, "y"))
            w, h = expression(points(keyframes, "w")), expression(points(keyframes, "h"))
            px, py = f"(X*{_num(sx)}+{_num(sx / 2)})*100/{width}", f"(Y*{_num(sy)}+{_num(sy / 2)})*100/{height}"
            lum = (
                f"255*between(T,{_num(start)},{_num(end)})*between({px},{x},({x})+({w}))*between({py},{y},({y})+({h}))"
            )
            label = f"[{tag}m{g}_{m}]"
            lines.append(f"color=c=black:s={mw}x{mh}:r={fps}:d={_num(length + 1)},format=gray,geq=lum='{lum}'{label}")
            masks.append(label)
        merged = masks[0]
        for m, mask in enumerate(masks[1:]):
            label = f"[{tag}mm{g}_{m}]"
            lines.append(f"{merged}{mask}blend=all_mode=lighten{label}")
            merged = label
        lines.append(f"{merged}scale={width}:{height}[{tag}mask{g}]")
        lines.append(f"[{tag}blur{g}][{tag}mask{g}]alphamerge[{tag}fx{g}]")
        label = f"[{tag}o{g}]"
        lines.append(f"{previous}[{tag}fx{g}]overlay=shortest=1:format=auto{label}")
        previous = label
    return lines, previous


def export_command(ffmpeg: str, project: dict, pieces: list[dict], fmt: str, size: str, target: str) -> list[str]:
    """The ffmpeg command that renders the cut to *target*. ValueError when there is nothing to render."""
    spec = FORMATS.get(fmt)
    if spec is None:
        raise ValueError(f"no such format: {fmt}")
    tracks = tracks_of(project.get("tracks"))
    width, height = canvas(project)
    fps_value = number(project.get("fps"), 25.0) or 25.0
    if fmt == "gif":
        fps_value = min(fps_value, GIF_FPS)
    fps = _num(fps_value)
    total = duration([p for p in pieces if p["track"] in kinds(tracks)])
    if total <= 0:
        raise ValueError("there is nothing on the tracks")
    layers = video_layers(pieces, tracks) if spec["video"] is not None else []
    heard = audible(pieces, tracks) if spec["audio"] is not None else []
    if spec["video"] is None and not heard:
        raise ValueError("nothing on the tracks has sound")

    # One input per piece, cut at the input: ffmpeg seeks instead of decoding from the top.
    inputs: list[str] = []
    index: dict[int, int] = {}
    for piece in [*layers, *[p for p in heard if p not in layers]]:
        index[id(piece)] = len(index)
        inputs += ["-ss", _num(number(piece["offset"])), "-t", _num(number(piece["length"]) + 0.1), "-i", piece["path"]]

    lines: list[str] = []
    maps: list[str] = []
    if spec["video"] is not None:
        lines.append(f"color=c=black:s={width}x{height}:r={fps}:d={_num(total)},format=yuv420p,setsar=1[bg]")
        below = "[bg]"
        for i, piece in enumerate(layers):
            k, start, length = index[id(piece)], number(piece["start"]), number(piece["length"])
            w, h, x, y = fit(int(number(piece.get("width"))), int(number(piece.get("height"))), width, height)
            lines.append(
                f"[{k}:v]setpts=PTS-STARTPTS,fps={fps},trim=duration={_num(length)},"
                f"scale={w}:{h},setsar=1,pad={width}:{height}:{x}:{y}:black,format=yuv420p[p{i}]"
            )
            effects = [{**e, "keyframes": keyframes_of(e.get("keyframes"))} for e in piece.get("effects") or []]
            blurred, out = blur_lines(effects, width, height, fps, length, f"[p{i}]", f"p{i}")
            lines += blurred
            lines.append(f"{out}setpts=PTS+{_num(start)}/TB[l{i}]")
            enable = f"between(t,{_num(start)},{_num(start + length)})"
            lines.append(f"{below}[l{i}]overlay=0:0:eof_action=pass:format=auto:enable='{enable}'[b{i}]")
            below = f"[b{i}]"
        cx, cy, cw, ch = crop_px(project.get("crop"), width, height)
        ow, oh = out_size(size, cw, ch)
        tail = f"crop={cw}:{ch}:{cx}:{cy},scale={ow}:{oh}:flags=bicubic,setsar=1"
        if fmt == "gif":
            lines.append(f"{below}{tail},split[g0][g1];[g0]palettegen=stats_mode=diff[pal];[g1][pal]paletteuse[vout]")
        else:
            lines.append(f"{below}{tail},format=yuv420p[vout]")
        maps += ["-map", "[vout]"]
    if heard:
        labels = []
        for i, piece in enumerate(heard):
            k, start = index[id(piece)], number(piece["start"])
            volume = number(piece.get("volume"), 1.0)
            lines.append(
                f"[{k}:a]asetpts=PTS-STARTPTS,atrim=duration={_num(number(piece['length']))},"
                f"aresample=48000,aformat=sample_fmts=fltp:channel_layouts=stereo,volume={_num(volume)},"
                f"adelay=delays={round(start * 1000)}:all=1[a{i}]"
            )
            labels.append(f"[a{i}]")
        mix = f"amix=inputs={len(labels)}:normalize=0:duration=longest," if len(labels) > 1 else ""
        lines.append(f"{''.join(labels)}{mix}apad=whole_dur={_num(total)},atrim=duration={_num(total)}[aout]")
        maps += ["-map", "[aout]"]
    command = [ffmpeg, "-hide_banner", "-nostdin", "-loglevel", "error", "-y", *inputs]
    command += ["-filter_complex", ";".join(lines), *maps]
    if spec["video"] is not None:
        command += spec["video"]
    if heard:
        command += spec["audio"]
    command += [*spec["extra"], "-t", _num(total), target]
    return command


# -- one moment, as a picture ---------------------------------------------------------
def frame_command(ffmpeg: str, project: dict, pieces: list[dict], t: float, out_width: int) -> list[str]:
    """A JPEG of the cut at *t* on stdout: the top piece fitted, its blurs, the crop, *out_width* wide."""
    tracks = tracks_of(project.get("tracks"))
    width, height = canvas(project)
    cx, cy, cw, ch = crop_px(project.get("crop"), width, height)
    tail = f"crop={cw}:{ch}:{cx}:{cy},scale={even(out_width)}:-2,format=yuvj420p[out]"
    piece = top_piece_at(pieces, tracks, t)
    if piece is None:
        graph = f"color=c=black:s={width}x{height}:d=1,format=yuv420p,{tail}"
        return _jpeg(ffmpeg, [], graph)
    rel = t - number(piece["start"])
    length = number(piece["length"])
    at = number(piece["offset"]) + rel
    w, h, x, y = fit(int(number(piece.get("width"))), int(number(piece.get("height"))), width, height)
    seen = []
    for effect in piece.get("effects") or []:
        keyframes = keyframes_of(effect.get("keyframes"))
        if on_at({"keyframes": keyframes}, rel, length):
            seen.append({**effect, "keyframes": keyframes})
    head = f"[0:v]scale={w}:{h},setsar=1,pad={width}:{height}:{x}:{y}:black,format=yuv420p"
    if not seen:
        return _jpeg(ffmpeg, ["-ss", _num(max(0.0, at)), "-i", piece["path"]], f"{head},{tail}")
    outs = "".join(f"[c{i}]" for i in range(len(seen)))
    lines = [f"{head},split={len(seen) + 1}[b]{outs}"]
    previous = "[b]"
    for i, effect in enumerate(seen):
        bx, by, bw, bh = box_px(effect["keyframes"], rel, width, height)
        r = radius(effect.get("strength", 20), width, height)
        lines.append(f"[c{i}]crop={bw}:{bh}:{bx}:{by},{blur(r, bw, bh)}[bl{i}]")
        lines.append(f"{previous}[bl{i}]overlay={bx}:{by}[o{i}]")
        previous = f"[o{i}]"
    lines.append(f"{previous}{tail}")
    return _jpeg(ffmpeg, ["-ss", _num(max(0.0, at)), "-i", piece["path"]], ";".join(lines))


def source_command(ffmpeg: str, path: str, t: float, out_width: int) -> list[str]:
    """A JPEG of a source's own frame at *t* on stdout, *out_width* wide."""
    graph = f"[0:v]scale={even(out_width)}:-2,format=yuvj420p[out]"
    return _jpeg(ffmpeg, ["-ss", _num(max(0.0, t)), "-i", path], graph)


def _jpeg(ffmpeg: str, inputs: list[str], graph: str) -> list[str]:
    if not graph.endswith("[out]"):
        graph += "[out]"
    return [
        ffmpeg, "-hide_banner", "-nostdin", "-loglevel", "error", *inputs,
        "-filter_complex", graph, "-map", "[out]",
        "-frames:v", "1", "-f", "image2pipe", "-c:v", "mjpeg", "-q:v", "4", "pipe:1",
    ]  # fmt: skip


# -- what a file is ---------------------------------------------------------------
def probe_command(ffprobe: str, source: str) -> list[str]:
    return [ffprobe, "-v", "error", "-show_streams", "-show_format", "-of", "json", source]


def parse_info(data: dict) -> dict:
    """ffprobe's JSON as the frames API's info: duration, size, fps, and whether it has picture and sound.

    ValueError when it is neither a video nor a sound."""
    streams = data.get("streams") or []
    pictures = [
        s for s in streams if s.get("codec_type") == "video" and not (s.get("disposition") or {}).get("attached_pic")
    ]
    sounds = [s for s in streams if s.get("codec_type") == "audio"]
    if not pictures and not sounds:
        raise ValueError("neither a picture nor a sound")
    picture = pictures[0] if pictures else {}
    durations = [number((data.get("format") or {}).get("duration"))]
    durations += [number(s.get("duration")) for s in (pictures[:1] + sounds[:1])]
    length = max(durations)
    if length <= 0:
        raise ValueError("no length")
    fps = 0.0
    if picture:
        for said in (picture.get("avg_frame_rate"), picture.get("r_frame_rate")):
            fps = fps_number(str(said or "0"))
            if 0 < fps < 1000:
                break
    return {
        "duration": round(length, 3),
        "width": int(picture.get("width") or 0),
        "height": int(picture.get("height") or 0),
        "fps": round(fps, 3) if fps else 0.0,
        "audio": bool(sounds),
        "video": bool(pictures),
    }


def fps_number(fps: str) -> float:
    try:
        if "/" in fps:
            a, b = fps.split("/", 1)
            return float(a) / float(b) if float(b) else 0.0
        return float(fps)
    except ValueError:
        return 0.0
