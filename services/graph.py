"""The arithmetic of Vidmorrow's renderer: keyframes to ffmpeg filter graphs.

Nothing here runs ffmpeg or talks to the server, so it is tested as plain
functions. An *effect* is a dict with its `strength` (1 to 100) and its
`keyframes`: dicts with `at` (seconds) and `x`, `y`, `w`, `h` (percent of
the frame), in any order. Between two keyframes the box moves and resizes
in a straight line; before the first and after the last it stays put. An
effect is seen from its first keyframe to its last, or to the end of the
clip when it has only one.

The export blurs each box with a mask: a small grey picture per effect,
drawn by `geq` from the box's expressions in `T` (the frame's time), scaled
up to the frame and used as the alpha of a blurred copy laid over the
original. Blurring the whole frame once per strength and masking it is
what lets a box change size from frame to frame, which `crop` cannot.
"""

from __future__ import annotations

from itertools import pairwise

MASK_SCALE = 4  # the mask is drawn at a quarter of the frame, then scaled up
MIN_BOX = 2


# -- values over time -------------------------------------------------------------
def points(keyframes: list[dict], field: str) -> list[tuple[float, float]]:
    """(time, value) pairs of one field, in time order."""
    return sorted((float(k["at"]), float(k[field])) for k in keyframes)


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


def span(keyframes: list[dict], duration: float) -> tuple[float, float]:
    """From when to when an effect is seen."""
    times = sorted(float(k["at"]) for k in keyframes)
    return (times[0], times[-1]) if len(times) > 1 else (times[0], duration + 1)


def _num(value: float) -> str:
    return f"{float(value):.4f}".rstrip("0").rstrip(".") or "0"


# -- pixels -----------------------------------------------------------------------
def even(value: float, floor: int = MIN_BOX) -> int:
    return max(floor, round(value / 2) * 2)


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


def radius(strength: int, width: int, height: int) -> int:
    """Blur radius in pixels: strength 20 on 1080p is about 22 pixels."""
    strength = min(100, max(1, int(strength or 20)))
    return max(1, min(round(strength * min(width, height) / 1000), min(width, height) // 4))


def blur(r: int, w: int, h: int) -> str:
    """A boxblur that fits a picture *w* by *h* (yuv420p: chroma is half)."""
    r = max(1, min(r, min(w, h) // 2 - 1 if min(w, h) > 3 else 1))
    return (
        f"boxblur=luma_radius={r}:luma_power=2:chroma_radius={max(1, min(r // 2, min(w, h) // 4 - 1))}:chroma_power=2"
    )


# -- the export -------------------------------------------------------------------
def export_graph(effects: list[dict], width: int, height: int, fps: str, duration: float) -> str:
    """The `-filter_complex` that renders *effects* onto the clip; the result is `[out]`."""
    effects = [e for e in effects if e.get("keyframes")]
    if not effects:
        return "[0:v]setpts=PTS-STARTPTS,format=yuv420p[out]"
    groups: dict[int, list[dict]] = {}
    for effect in effects:
        groups.setdefault(radius(effect.get("strength", 20), width, height), []).append(effect)
    mw, mh = even(width / MASK_SCALE), even(height / MASK_SCALE)
    sx, sy = width / mw, height / mh
    outs = "".join(f"[s{i}]" for i in range(len(groups)))
    lines = [f"[0:v]setpts=PTS-STARTPTS,format=yuv420p,split={len(groups) + 1}[base]{outs}"]
    previous = "[base]"
    for g, (r, members) in enumerate(sorted(groups.items())):
        lines.append(f"[s{g}]{blur(r, width, height)}[blur{g}]")
        masks = []
        for m, effect in enumerate(members):
            keyframes = effect["keyframes"]
            start, end = span(keyframes, duration)
            x, y = expression(points(keyframes, "x")), expression(points(keyframes, "y"))
            w, h = expression(points(keyframes, "w")), expression(points(keyframes, "h"))
            px, py = f"(X*{_num(sx)}+{_num(sx / 2)})*100/{width}", f"(Y*{_num(sy)}+{_num(sy / 2)})*100/{height}"
            lum = (
                f"255*between(T,{_num(start)},{_num(end)})*between({px},{x},({x})+({w}))*between({py},{y},({y})+({h}))"
            )
            label = f"[m{g}_{m}]"
            lines.append(f"color=c=black:s={mw}x{mh}:r={fps}:d={_num(duration + 2)},format=gray,geq=lum='{lum}'{label}")
            masks.append(label)
        merged = masks[0]
        for m, mask in enumerate(masks[1:]):
            label = f"[mm{g}_{m}]"
            lines.append(f"{merged}{mask}blend=all_mode=lighten{label}")
            merged = label
        lines.append(f"{merged}scale={width}:{height}[mask{g}]")
        lines.append(f"[blur{g}][mask{g}]alphamerge[fx{g}]")
        label = f"[o{g}]"
        lines.append(f"{previous}[fx{g}]overlay=shortest=1{label}")
        previous = label
    lines.append(f"{previous}format=yuv420p[out]")
    return ";".join(lines)


def export_command(ffmpeg: str, source: str, target: str, graph: str) -> list[str]:
    return [
        ffmpeg, "-hide_banner", "-nostdin", "-loglevel", "error", "-y",
        "-i", source,
        "-filter_complex", graph,
        "-map", "[out]", "-map", "0:a?",
        "-c:v", "libx264", "-preset", "veryfast", "-crf", "20", "-pix_fmt", "yuv420p",
        "-c:a", "aac", "-b:a", "160k",
        "-movflags", "+faststart",
        target,
    ]  # fmt: skip


# -- a picture of one moment -------------------------------------------------------
def preview_graph(effects: list[dict], t: float, width: int, height: int, duration: float, out_width: int = 640) -> str:
    """One frame at *t*: the blurs as they will be, each box outlined; `[out]`."""
    seen = []
    for effect in effects:
        if not effect.get("keyframes"):
            continue
        start, end = span(effect["keyframes"], duration)
        if start - 0.001 <= t <= end + 0.001:
            seen.append(effect)
    if not seen:
        return f"[0:v]format=yuv420p,scale={out_width}:-2[out]"
    outs = "".join(f"[c{i}]" for i in range(len(seen)))
    lines = [f"[0:v]format=yuv420p,split={len(seen) + 1}[b]{outs}"]
    previous, boxes = "[b]", []
    for i, effect in enumerate(seen):
        x, y, w, h = box_px(effect["keyframes"], t, width, height)
        r = radius(effect.get("strength", 20), width, height)
        lines.append(f"[c{i}]crop={w}:{h}:{x}:{y},{blur(r, w, h)}[bl{i}]")
        lines.append(f"{previous}[bl{i}]overlay={x}:{y}[o{i}]")
        previous = f"[o{i}]"
        boxes.append(f"drawbox=x={x}:y={y}:w={w}:h={h}:color=red@0.9:thickness={max(2, width // 320)}")
    lines.append(f"{previous}{','.join(boxes)},scale={out_width}:-2[out]")
    return ";".join(lines)


def preview_command(ffmpeg: str, source: str, t: float, target: str, graph: str) -> list[str]:
    return [
        ffmpeg, "-hide_banner", "-nostdin", "-loglevel", "error", "-y",
        "-ss", _num(max(0.0, t)), "-i", source,
        "-filter_complex", graph, "-map", "[out]",
        "-frames:v", "1", target,
    ]  # fmt: skip


def probe_command(ffprobe: str, source: str) -> list[str]:
    return [
        ffprobe, "-v", "error", "-select_streams", "v:0",
        "-show_entries", "stream=width,height,r_frame_rate,avg_frame_rate,duration:format=duration",
        "-of", "json", source,
    ]  # fmt: skip


def parse_probe(data: dict) -> dict:
    """ffprobe's JSON as `{width, height, fps, duration}`; ValueError when it is not a video."""
    streams = data.get("streams") or []
    if not streams:
        raise ValueError("no video stream")
    stream = streams[0]
    width, height = int(stream.get("width") or 0), int(stream.get("height") or 0)
    if width < 2 or height < 2:
        raise ValueError("no picture size")
    fps = str(stream.get("avg_frame_rate") or stream.get("r_frame_rate") or "25")
    if fps in ("0/0", "0") or fps.endswith("/0"):
        fps = str(stream.get("r_frame_rate") or "25")
    duration = float(stream.get("duration") or (data.get("format") or {}).get("duration") or 0)
    if duration <= 0:
        raise ValueError("no length")
    return {"width": width, "height": height, "fps": fps, "duration": duration}


def fps_number(fps: str) -> float:
    if "/" in fps:
        a, b = fps.split("/", 1)
        return float(a) / float(b) if float(b) else 0.0
    return float(fps)
