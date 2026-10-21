"""The renderer on real media: tiny videos and a sound made by ffmpeg, a
timeline with a gap, an overlap, a blur and a crop, exported and drawn.

The record API is stood in for by a dict (the service only lists, reads,
patches and uploads), so this runs without a server; it is skipped where
ffmpeg is not installed.
"""

import io
import json
import shutil
import subprocess
import urllib.error
import urllib.request
from pathlib import Path

import pytest

ffmpeg_missing = not shutil.which("ffmpeg") or not shutil.which("ffprobe")
pytestmark = pytest.mark.skipif(ffmpeg_missing, reason="ffmpeg is not installed here")

PIL = pytest.importorskip("PIL.Image")
import render

PROJECT, PIECE, EFFECT, FILE = render.PROJECT, render.PIECE, render.EFFECT, render.FILE


class FakeStore:
    """The record API as the renderer uses it, over dicts and a folder."""

    def __init__(self, root: Path) -> None:
        self.root = root
        self.data: dict[str, dict[str, dict]] = {}
        self.n = 0

    def add(self, model: str, fields: dict, *, id_: str | None = None, owner: str = "admin") -> dict:
        self.n += 1
        record = {"id": id_ or f"r{self.n}", "model": model, "owner": owner, "fields": dict(fields)}
        self.data.setdefault(model, {})[record["id"]] = record
        return record

    def records(self, model: str, **where) -> list[dict]:
        return [r for r in self.data.get(model, {}).values() if all(r["fields"].get(k) == v for k, v in where.items())]

    def record(self, model: str, record_id: str) -> dict:
        try:
            return self.data[model][record_id]
        except KeyError:
            raise render.ApiError(f"GET {model}/{record_id}: 404", 404) from None

    def create(self, model: str, fields: dict) -> dict:
        return self.add(model, fields)

    def patch(self, model: str, record_id: str, fields: dict) -> dict:
        self.record(model, record_id)["fields"].update(fields)
        return self.data[model][record_id]

    def file(self, path: Path, folder: str = "Videos") -> dict:
        fields = {"share": "my-files", "folder": folder, "name": path.name, "kind": "file", "size": path.stat().st_size,
                  "modified": str(path.stat().st_mtime_ns), "local": str(path)}  # fmt: skip
        return self.add(FILE, fields, id_=f"f_{folder}/{path.name}")

    def path_of(self, file: dict) -> Path:
        return Path(file["fields"]["local"])

    def upload(self, share: str, folder: str, name: str, source: Path) -> dict:
        target = self.root / "uploaded" / name
        target.parent.mkdir(exist_ok=True)
        shutil.copy(source, target)
        return self.file(target, folder)


def make(out: Path, *inputs: str, audio: bool = True) -> Path:
    args = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y"]
    for source in inputs:
        args += ["-f", "lavfi", "-i", source]
    if out.suffix == ".m4a":
        args += ["-c:a", "aac"]
    else:
        args += ["-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p"]
        args += ["-c:a", "aac", "-shortest"] if audio else []
    subprocess.run([*args, str(out)], check=True)
    return out


def probe(path: Path) -> dict:
    said = subprocess.run(
        ["ffprobe", "-v", "error", "-show_streams", "-show_format", "-of", "json", str(path)],
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    return json.loads(said)


def frame_at(path: Path, t: float, tmp: Path):
    out = tmp / f"frame-{t}.png"
    subprocess.run(
        [
            "ffmpeg",
            "-hide_banner",
            "-loglevel",
            "error",
            "-y",
            "-ss",
            str(t),
            "-i",
            str(path),
            "-frames:v",
            "1",
            str(out),
        ],
        check=True,
    )
    return PIL.open(out).convert("L")


def roughness(picture, box) -> float:
    """How much neighbouring pixels differ inside *box*: low where it is blurred."""
    region = picture.crop(box)
    w, h = region.size
    px = region.load()
    total = sum(abs(px[x, y] - px[x + 1, y]) + abs(px[x, y] - px[x, y + 1]) for x in range(w - 1) for y in range(h - 1))
    return total / ((w - 1) * (h - 1))


@pytest.fixture(scope="module")
def timeline(tmp_path_factory):
    """Two pieces with a gap on V1, one over them on V2, a sound on A1, a moving blur, a crop."""
    tmp = tmp_path_factory.mktemp("media")
    a = make(tmp / "a.mp4", "testsrc2=size=320x180:rate=25:duration=3", "sine=frequency=440:duration=3")
    b = make(tmp / "b.mp4", "testsrc2=size=320x180:rate=25:duration=2", "sine=frequency=660:duration=2")
    c = make(tmp / "c.mp4", "color=c=red:size=160x160:rate=25:duration=2", audio=False)
    song = make(tmp / "song.m4a", "sine=frequency=220:duration=4")
    store = FakeStore(tmp)
    files = {p.name: store.file(p) for p in (a, b, c, song)}
    project = store.add(
        PROJECT,
        {"name": "Holiday cut", "status": "editing", "crop": {"x": 0, "y": 0, "w": 75, "h": 100},
         "tracks": render.graph.TRACKS},
    )  # fmt: skip
    pid = project["id"]
    first = store.add(PIECE, {"project": pid, "track": "V1", "source": files["a.mp4"]["id"], "name": "a.mp4",
                              "start": 0, "offset": 0.5, "length": 2})  # fmt: skip
    store.add(PIECE, {"project": pid, "track": "V1", "source": files["b.mp4"]["id"], "name": "b.mp4", "start": 3,
                      "offset": 0, "length": None})  # fmt: skip
    store.add(PIECE, {"project": pid, "track": "V2", "source": files["c.mp4"]["id"], "name": "c.mp4", "start": 1.5,
                      "offset": 0, "length": "1"})  # fmt: skip
    store.add(PIECE, {"project": pid, "track": "A1", "source": files["song.m4a"]["id"], "name": "song.m4a",
                      "start": 0.5, "offset": 0, "length": 3, "volume": 0.5})  # fmt: skip
    store.add(EFFECT, {"piece": first["id"], "kind": "blur", "label": "Face", "strength": 40,
                       "keyframes": [{"t": 0, "x": 5, "y": 5, "w": 40, "h": 50}, {"t": 2, "x": 30, "y": 30, "w": 40, "h": 50}]})  # fmt: skip
    renderer = render.Renderer(store, tmp / "work")
    renderer.fill()
    return store, renderer, project, tmp


def test_filling_in_gives_pieces_their_length_and_the_project_its_picture(timeline):
    store, _, project, _ = timeline
    b = next(p for p in store.records(PIECE) if p["fields"]["name"] == "b.mp4")
    assert abs(b["fields"]["length"] - 2.0) < 0.1
    f = store.record(PROJECT, project["id"])["fields"]
    assert (f["width"], f["height"], f["fps"]) == (320, 180, 25.0)
    known = {m["fields"]["name"]: m["fields"] for m in store.records(render.MEDIA)}
    assert known["a.mp4"]["duration"] == 3.0 and known["a.mp4"]["audio"] is True, "remembered for the Clips screen"
    assert known["b.mp4"]["video"] is True and abs(known["b.mp4"]["duration"] - 2.0) < 0.1


def test_the_export_is_one_file_beside_the_first_video_cut_cropped_and_blurred(timeline, tmp_path):
    store, renderer, project, _ = timeline
    store.patch(
        PROJECT, project["id"], {"status": "queued", "format": "mp4", "size": "project", "export_name": "Holiday cut"}
    )
    done = renderer.render(store.record(PROJECT, project["id"]))["fields"]
    assert done["status"] == "done", done["message"]
    out = store.record(FILE, done["output"])
    assert out["fields"]["folder"] == "Videos" and out["fields"]["name"] == "Holiday cut.mp4"
    path = store.path_of(out)
    info = probe(path)
    streams = {s["codec_type"]: s for s in info["streams"]}
    assert set(streams) == {"video", "audio"}
    assert (streams["video"]["width"], streams["video"]["height"]) == (240, 180), "75% of 320 wide: the crop"
    assert abs(float(info["format"]["duration"]) - 5.0) < 0.2

    # At 0.4 s the blur's box is at 10%, 10%, 40% × 50% of 320 × 180: inside smooth, outside sharp.
    picture = frame_at(path, 0.4, tmp_path)
    inside = roughness(picture, (40, 26, 152, 100))
    outside = roughness(picture, (170, 100, 238, 178))
    assert inside * 3 < outside, (inside, outside)
    # V2's red square covers V1 from 1.5 s, letterboxed; at 2.7 s there is nothing: black.
    over = frame_at(path, 2.0, tmp_path)
    assert over.getpixel((160, 90)) < 120 and over.getpixel((10, 90)) < 30, "red in the middle, bars at the side"
    gap = frame_at(path, 2.7, tmp_path)
    assert max(gap.tobytes()) < 40

    # A second export of the same name does not overwrite the first.
    store.patch(PROJECT, project["id"], {"status": "queued", "format": "m4a"})
    again = renderer.render(store.record(PROJECT, project["id"]))["fields"]
    assert again["status"] == "done" and store.record(FILE, again["output"])["fields"]["name"] == "Holiday cut.m4a"
    store.patch(PROJECT, project["id"], {"status": "queued", "format": "mp4", "size": "480p"})
    third = renderer.render(store.record(PROJECT, project["id"]))["fields"]
    small = store.record(FILE, third["output"])
    assert small["fields"]["name"] == "Holiday cut 2.mp4"
    video = next(s for s in probe(store.path_of(small))["streams"] if s["codec_type"] == "video")
    assert (video["width"], video["height"]) == (640, 480)


def test_a_failed_export_says_why(timeline):
    store, renderer, _, _ = timeline
    empty = store.add(PROJECT, {"name": "Empty", "status": "queued", "width": 320, "height": 180, "fps": 25})
    done = renderer.render(empty)["fields"]
    assert done["status"] == "failed" and "nothing on the tracks" in done["message"]


def test_the_frames_api_answers_info_source_and_frame(timeline):
    store, renderer, project, _ = timeline
    server = render.serve(renderer, 0)
    base = f"http://127.0.0.1:{server.server_address[1]}/frames"
    try:
        a = next(f for f in store.records(FILE) if f["fields"]["name"] == "a.mp4")
        with urllib.request.urlopen(f"{base}/info?file={a['id']}", timeout=30) as answer:
            info = json.loads(answer.read())
        assert info == {"duration": 3.0, "width": 320, "height": 180, "fps": 25.0, "audio": True, "video": True}

        with urllib.request.urlopen(f"{base}/frame?project={project['id']}&t=0.4&w=200", timeout=30) as answer:
            assert answer.headers["Content-Type"] == "image/jpeg"
            picture = PIL.open(io.BytesIO(answer.read()))
        assert picture.format == "JPEG" and picture.size == (200, 150), "the crop's 240 × 180, 200 wide"

        with urllib.request.urlopen(f"{base}/source?file={a['id']}&t=1&w=160", timeout=30) as answer:
            assert PIL.open(io.BytesIO(answer.read())).size == (160, 90)

        # Somebody else is shown nothing of the administrator's.
        request = urllib.request.Request(
            f"{base}/frame?project={project['id']}&t=0", headers={"X-Cloudmorrow-User": "sam"}
        )
        with pytest.raises(urllib.error.HTTPError) as refused:
            urllib.request.urlopen(request, timeout=30)
        assert refused.value.code == 404
        with pytest.raises(urllib.error.HTTPError) as missing:
            urllib.request.urlopen(f"{base}/frame", timeout=30)
        assert missing.value.code == 400
    finally:
        server.shutdown()
