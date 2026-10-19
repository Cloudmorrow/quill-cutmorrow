"""The renderer's arithmetic: keyframes to boxes and filter graphs, with no server.

The last test renders a real clip with ffmpeg when it is installed, the
way the service does, and is skipped where it is not.
"""

import json
import shutil
import subprocess
from pathlib import Path

import graph
import pytest

MOVE = [{"at": 1, "x": 10, "y": 10, "w": 20, "h": 30}, {"at": 3, "x": 50, "y": 30, "w": 40, "h": 50}]


def test_a_value_moves_in_a_straight_line_and_holds_outside():
    pts = graph.points(MOVE, "x")
    assert [graph.value_at(pts, t) for t in (0, 1, 2, 3, 4)] == [10, 10, 30, 50, 50]


def test_the_expression_says_the_same_as_the_function():
    pts = graph.points(MOVE, "w")
    assert graph.expression(pts) == "if(lt(T,1),20,if(lt(T,3),20+(20)*(T-1)/2,40))"
    assert graph.expression([(2.0, 5.0)]) == "if(lt(T,2),5,5)"


def test_an_effect_is_seen_from_its_first_keyframe_to_its_last_or_the_end():
    assert graph.span(MOVE, 10) == (1, 3)
    assert graph.span(MOVE[:1], 10) == (1, 11)


def test_boxes_are_even_pixels_on_the_frame():
    assert graph.box_px(MOVE, 2, 640, 360) == (192, 72, 192, 144)
    assert graph.box_px([{"at": 0, "x": 95, "y": 95, "w": 20, "h": 20}], 0, 640, 360) == (512, 288, 128, 72)


def test_the_export_graph_blurs_once_per_strength_and_masks_each_effect():
    effects = [
        {"strength": 20, "keyframes": MOVE},
        {"strength": 20, "keyframes": MOVE[:1]},
        {"strength": 80, "keyframes": MOVE},
    ]
    said = graph.export_graph(effects, 1920, 1080, "25", 10)
    assert said.startswith("[0:v]setpts=PTS-STARTPTS,format=yuv420p,split=3[base][s0][s1]")
    assert said.count("boxblur=") == 2 and said.count("geq=lum=") == 3 and said.count("blend=all_mode=lighten") == 1
    assert said.endswith("overlay=shortest=1[o1];[o1]format=yuv420p[out]")
    assert graph.export_graph([], 1920, 1080, "25", 10) == "[0:v]setpts=PTS-STARTPTS,format=yuv420p[out]"


def test_a_preview_shows_only_what_is_seen_at_that_moment():
    effects = [
        {"strength": 20, "keyframes": MOVE},
        {"strength": 20, "keyframes": [{"at": 2.5, "x": 0, "y": 0, "w": 10, "h": 10}]},
    ]
    assert graph.preview_graph(effects, 2, 640, 360, 10).count("drawbox=") == 1
    assert graph.preview_graph(effects, 2.8, 640, 360, 10).count("drawbox=") == 2
    assert graph.preview_graph(effects, 0.5, 640, 360, 10) == "[0:v]format=yuv420p,scale=640:-2[out]"


def test_probe_output_is_read_and_refused_plainly():
    data = {
        "streams": [{"width": 1280, "height": 720, "r_frame_rate": "30000/1001", "avg_frame_rate": "30000/1001"}],
        "format": {"duration": "8.2"},
    }
    assert graph.parse_probe(data) == {"width": 1280, "height": 720, "fps": "30000/1001", "duration": 8.2}
    assert round(graph.fps_number("30000/1001"), 3) == 29.97
    with pytest.raises(ValueError):
        graph.parse_probe({"streams": []})


@pytest.mark.skipif(not shutil.which("ffmpeg") or not shutil.which("ffprobe"), reason="ffmpeg is not installed here")
def test_ffmpeg_accepts_the_graphs_and_the_export_keeps_the_sound(tmp_path: Path):
    src = tmp_path / "src.mp4"
    subprocess.run(
        ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-f", "lavfi", "-i", "testsrc2=size=320x180:rate=25:duration=2",
         "-f", "lavfi", "-i", "sine=frequency=440:duration=2", "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p",
         "-c:a", "aac", "-shortest", str(src)],
        check=True,
    )  # fmt: skip
    probe = subprocess.run(graph.probe_command("ffprobe", str(src)), capture_output=True, text=True, check=True)
    info = graph.parse_probe(json.loads(probe.stdout))
    assert (info["width"], info["height"]) == (320, 180)
    effects = [{"strength": 40, "keyframes": [{"at": 0.2, "x": 10, "y": 10, "w": 30, "h": 30}, {"at": 1.5, "x": 60, "y": 50, "w": 30, "h": 40}]},
               {"strength": 90, "keyframes": [{"at": 1, "x": 0, "y": 70, "w": 25, "h": 25}]}]  # fmt: skip
    out = tmp_path / "out.mp4"
    done = graph.export_graph(effects, info["width"], info["height"], info["fps"], info["duration"])
    rendered = subprocess.run(
        graph.export_command("ffmpeg", str(src), str(out), done), capture_output=True, text=True, check=False
    )
    assert rendered.returncode == 0, rendered.stderr
    streams = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "stream=codec_type", "-of", "csv=p=0", str(out)],
        capture_output=True,
        text=True,
        check=True,
    ).stdout.split()
    assert streams == ["video", "audio"]
    frame = tmp_path / "frame.png"
    picture = graph.preview_graph(effects, 1.2, info["width"], info["height"], info["duration"])
    drawn = subprocess.run(
        graph.preview_command("ffmpeg", str(src), 1.2, str(frame), picture), capture_output=True, text=True, check=False
    )
    assert drawn.returncode == 0 and frame.stat().st_size > 0, drawn.stderr
