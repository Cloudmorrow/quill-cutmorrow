"""The renderer's arithmetic: a timeline to boxes, sizes and ffmpeg commands, with no server and no ffmpeg."""

import graph
import pytest

MOVE = [{"t": 1, "x": 10, "y": 10, "w": 20, "h": 30}, {"t": 3, "x": 50, "y": 30, "w": 40, "h": 50}]
TRACKS = graph.TRACKS


def piece(track="V1", start=0.0, length=4.0, offset=0.0, **more):
    base = {"track": track, "start": start, "offset": offset, "length": length, "path": f"/m/{track}-{start}.mp4",
            "video": True, "audio": True, "width": 640, "height": 360, "effects": []}  # fmt: skip
    return base | more


def test_a_value_moves_in_a_straight_line_and_holds_outside():
    pts = graph.points(MOVE, "x")
    assert [graph.value_at(pts, t) for t in (0, 1, 2, 3, 4)] == [10, 10, 30, 50, 50]


def test_the_expression_says_the_same_as_the_function():
    pts = graph.points(MOVE, "w")
    assert graph.expression(pts) == "if(lt(T,1),20,if(lt(T,3),20+(20)*(T-1)/2,40))"
    assert graph.expression([(2.0, 5.0)]) == "if(lt(T,2),5,5)"


def test_an_effect_is_on_from_its_first_keyframe_to_its_last_or_the_whole_piece():
    assert graph.span(MOVE, 10) == (1, 3)
    assert graph.span(MOVE[:1], 10) == (0, 11)
    assert not graph.on_at({"keyframes": MOVE}, 0.5, 10) and graph.on_at({"keyframes": MOVE}, 2, 10)
    assert graph.on_at({"keyframes": MOVE[:1]}, 0.0, 10) and graph.on_at({"keyframes": MOVE[:1]}, 9.9, 10)


def test_keyframes_are_read_as_the_store_gives_them():
    raw = '[{"t": "2", "x": 1, "y": 2, "w": 3, "h": 4}, {"t": 0.5, "x": 5, "y": 5, "w": 5, "h": 5}, {"t": 1}]'
    assert [k["t"] for k in graph.keyframes_of(raw)] == [0.5, 2.0]
    assert graph.keyframes_of(None) == [] and graph.keyframes_of("not json") == []


def test_tracks_default_to_the_kits_four():
    assert [t["id"] for t in graph.tracks_of(None)] == ["V2", "V1", "A1", "A2"]
    assert graph.tracks_of([{"id": "V1"}]) == [{"id": "V1", "kind": "video", "name": "V1"}]


def test_boxes_crops_and_sizes_are_even_pixels_on_the_frame():
    assert graph.box_px(MOVE, 2, 640, 360) == (192, 72, 192, 144)
    assert graph.box_px([{"t": 0, "x": 95, "y": 95, "w": 20, "h": 20}], 0, 640, 360) == (512, 288, 128, 72)
    assert graph.crop_px(None, 640, 360) == (0, 0, 640, 360)
    assert graph.crop_px({"x": 10, "y": 10, "w": 50, "h": 50}, 640, 360) == (64, 36, 320, 180)
    assert graph.crop_px({"x": 90, "y": 0, "w": 50, "h": 100}, 640, 360) == (576, 0, 64, 360)
    assert graph.out_size("project", 320, 180) == (320, 180)
    assert graph.out_size("720p", 1920, 1080) == (1280, 720)
    assert graph.out_size("1080p", 1080, 1920) == (1080, 1920)
    assert graph.out_size("480p", 1000, 1000) == (480, 480)


def test_a_picture_is_fitted_into_the_frame_with_bars():
    assert graph.fit(640, 360, 640, 360) == (640, 360, 0, 0)
    assert graph.fit(360, 640, 640, 360) == (202, 360, 218, 0)
    assert graph.fit(1920, 800, 1280, 720) == (1280, 534, 0, 92)


def test_the_top_video_track_wins_and_a_gap_is_black():
    low, high = piece("V1", 0, 4), piece("V2", 2, 1)
    sound = piece("A1", 0, 10, video=False)
    pieces = [low, high, sound]
    assert graph.top_piece_at(pieces, TRACKS, 1) is low
    assert graph.top_piece_at(pieces, TRACKS, 2.5) is high
    assert graph.top_piece_at(pieces, TRACKS, 5) is None
    assert graph.video_layers(pieces, TRACKS) == [low, high]
    assert graph.duration(pieces) == 10


def test_every_unmuted_piece_with_sound_is_heard():
    pieces = [piece("V1"), piece("V2", muted=True), piece("A1", audio=False), piece("A2", volume=0), piece("X9")]
    assert [p["track"] for p in graph.audible(pieces, TRACKS)] == ["V1"]


def test_the_export_lays_pieces_bottom_to_top_and_mixes_the_sound():
    pieces = [
        piece(
            "V1", 0, 2, offset=1, effects=[{"strength": 20, "keyframes": MOVE}, {"strength": 80, "keyframes": MOVE[:1]}]
        ),
        piece("V1", 3, 1),
        piece("V2", 1, 1.5, video=True, audio=False),
        piece("A1", 0, 4, video=False, volume=0.5),
    ]
    project = {"width": 640, "height": 360, "fps": 25, "crop": {"x": 0, "y": 0, "w": 50, "h": 100}}
    command = graph.export_command("ffmpeg", project, pieces, "mp4", "project", "/out.mp4")
    said = command[command.index("-filter_complex") + 1]
    assert command.count("-i") == 4 and command[command.index("-ss") + 1] == "1"
    assert said.startswith("color=c=black:s=640x360:r=25:d=4,")
    assert said.index("[b0]") < said.index("[b1]") < said.index("[b2]"), "V1 first, then V2 over it"
    assert "setpts=PTS+3/TB" in said and "enable='between(t,1,2.5)'" in said
    assert said.count("boxblur=") == 2 and said.count("geq=lum=") == 2
    assert "crop=320:360:0:0,scale=320:360" in said
    assert said.count("adelay=") == 3 and "amix=inputs=3:normalize=0" in said and "volume=0.5" in said
    assert command[-3:] == ["-t", "4", "/out.mp4"] and "libx264" in command and "aac" in command


def test_each_format_encodes_as_it_says():
    pieces = [piece("V1", 0, 2)]
    project = {"width": 640, "height": 360, "fps": 30}
    webm = graph.export_command("ffmpeg", project, pieces, "webm", "480p", "/o.webm")
    assert "libvpx-vp9" in webm and "libopus" in webm and "scale=854:480" in webm[webm.index("-filter_complex") + 1]
    gif = graph.export_command("ffmpeg", project, pieces, "gif", "project", "/o.gif")
    assert "palettegen" in gif[gif.index("-filter_complex") + 1] and "[aout]" not in gif and "r=15" in " ".join(gif)
    m4a = graph.export_command("ffmpeg", project, pieces, "m4a", "project", "/o.m4a")
    assert "[vout]" not in " ".join(m4a) and "[aout]" in m4a
    with pytest.raises(ValueError, match="sound"):
        graph.export_command("ffmpeg", project, [piece("V1", audio=False)], "m4a", "project", "/o.m4a")
    with pytest.raises(ValueError, match="nothing"):
        graph.export_command("ffmpeg", project, [], "mp4", "project", "/o.mp4")


def test_a_frame_is_the_top_piece_at_its_own_time_or_black():
    effect = {"strength": 20, "keyframes": MOVE}
    pieces = [piece("V1", 10, 5, offset=2, effects=[effect])]
    project = {"width": 640, "height": 360, "crop": {"x": 10, "y": 10, "w": 50, "h": 50}}
    at = graph.frame_command("ffmpeg", project, pieces, 12, 320)
    assert at[at.index("-ss") + 1] == "4", "2 s into the piece is 4 s into its source"
    said = at[at.index("-filter_complex") + 1]
    assert "crop=192:144:192:72" in said and "boxblur" in said and "crop=320:180:64:36,scale=320:-2" in said
    gap = graph.frame_command("ffmpeg", project, pieces, 3, 320)
    assert "-i" not in gap and gap[gap.index("-filter_complex") + 1].startswith("color=c=black")
    before = graph.frame_command("ffmpeg", project, pieces, 10.5, 320)
    assert "boxblur" not in before[before.index("-filter_complex") + 1], "the blur starts at its first keyframe"


def test_probe_output_is_read_and_refused_plainly():
    data = {
        "streams": [
            {"codec_type": "video", "width": 1280, "height": 720, "avg_frame_rate": "30000/1001"},
            {"codec_type": "audio"},
        ],
        "format": {"duration": "8.2"},
    }
    assert graph.parse_info(data) == {
        "duration": 8.2,
        "width": 1280,
        "height": 720,
        "fps": 29.97,
        "audio": True,
        "video": True,
    }
    song = {
        "streams": [
            {"codec_type": "audio", "duration": "3"},
            {"codec_type": "video", "disposition": {"attached_pic": 1}},
        ]
    }
    assert graph.parse_info(song) == {"duration": 3, "width": 0, "height": 0, "fps": 0.0, "audio": True, "video": False}
    with pytest.raises(ValueError):
        graph.parse_info({"streams": []})
