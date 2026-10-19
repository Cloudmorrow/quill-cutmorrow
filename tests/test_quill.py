"""Vidmorrow's tests: the real record store, files and gate, and quill.py, on this machine.

`cm quill test` runs them; `cm quill test --sandbox` runs the same tests
with quill.py in the sandbox, as a server runs it. The renderer (a service)
is not started here; its arithmetic is tested in test_graph.py.
"""

import contextlib

import pytest
from cloudmorrow.quill.testing import Harness

CLIP, EFFECT, KEYFRAME, FILE = "vidmorrow.clip", "vidmorrow.effect", "vidmorrow.keyframe", "file"


@pytest.fixture()
def q():
    with Harness(".") as harness:
        yield harness


def a_video(q, name="holiday.mp4"):
    """A file in the Vidmorrow folder, put there the way an upload is: a few bytes with a video's name is enough for the records."""
    with contextlib.suppress(Exception):  # the welcome note made the folder already
        q.seed(FILE, share="my-files", path="Vidmorrow", kind="folder")
    part = q._tmp / name
    part.write_bytes(b"\x00\x00\x00\x18ftypmp42")
    q.state.records.put(q._principal(), FILE, {"share": "my-files", "folder": "Vidmorrow", "name": name}, part)
    return next(f for f in q.list(FILE, share="my-files", folder="Vidmorrow") if f["name"] == name)


def test_the_first_look_makes_the_folder_with_a_note_in_it(q):
    assert "listed here" in q.view("clips").text()
    assert [f["name"] for f in q.list(FILE, share="my-files", folder="Vidmorrow")] == ["How Vidmorrow works.md"]


def test_a_video_in_the_folder_becomes_a_clip(q):
    video = a_video(q)
    assert "holiday.mp4" in q.view("clips").text()
    done = q.act("new-clip", video)
    assert done.ok and done.toast == "Editing holiday"
    (clip,) = q.list(CLIP)
    assert clip["name"] == "holiday" and clip["source"] == video.id and clip["status"] == "new"
    # Pressed again, it goes to the clip there is; the video and the export are no longer offered.
    assert q.act("new-clip", video).ok and len(q.list(CLIP)) == 1
    export = a_video(q, "holiday blurred.mp4")
    q.change(CLIP, clip.id, output=export.id)
    offered = q.view("clips").text().split("Videos in")[1]
    assert "holiday.mp4" not in offered and "holiday blurred.mp4" not in offered


def test_a_blur_starts_with_one_keyframe_and_grows(q):
    clip = q.seed(
        CLIP, name="Holiday", source=a_video(q).id, status="ready", width=1920, height=1080, duration=12.5, fps=25
    )
    assert (
        q.act("add-blur", clip, label="Face", at=1.5, x=10, y=20, w=30, h=40, strength=35).toast
        == "Added Face at 0:01.5"
    )
    (effect,) = q.list(EFFECT, clip=clip.id)
    assert effect["label"] == "Face" and effect["strength"] == 35
    (first,) = q.list(KEYFRAME, effect=effect.id)
    assert (float(first["at"]), float(first["x"]), float(first["w"])) == (1.5, 10.0, 30.0)
    assert q.get(CLIP, clip.id)["wants_previews"] is True, "the hook asks the renderer for a picture"

    assert q.act("add-keyframe", effect, at=4, x=50, y=20, w=30, h=40).toast == "Keyframe at 0:04"
    assert q.act("add-keyframe", effect, at=4, x=1, y=1, w=1, h=1).error.startswith(
        "There is a keyframe at 0:04 already"
    )
    assert "above 0" in q.act("add-keyframe", effect, at=6, x=1, y=1, w=0, h=1).error
    assert "stay on the frame" in q.act("add-keyframe", effect, at=6, x=90, y=1, w=20, h=1).error
    assert len(q.list(KEYFRAME, effect=effect.id)) == 2

    editor = q.view("clip", clip=clip.id)
    text = editor.text()
    assert "Face" in text and "from 0:01.5 to 0:04" in text and "1920×1080" in text and "0:12.5 long" in text
    assert "Export" in editor.buttons


def test_removing_a_blur_takes_its_keyframes_and_their_pictures(q):
    clip = q.seed(CLIP, name="Holiday", source=a_video(q).id, status="ready", width=640, height=360, duration=4, fps=25)
    q.act("add-blur", clip, label="Plate", at=0, x=10, y=10, w=10, h=10)
    (effect,) = q.list(EFFECT, clip=clip.id)
    picture = q.seed(FILE, share="my-files", path="Vidmorrow/Previews/note.md", text="stands in for a picture")
    (keyframe,) = q.list(KEYFRAME, effect=effect.id)
    q.change(KEYFRAME, keyframe.id, preview=picture.id, preview_of="x")
    assert q.act("remove-effect", effect).toast == "Removed Plate"
    assert q.list(EFFECT, clip=clip.id) == [] and q.list(KEYFRAME, clip=clip.id) == []
    assert [f["name"] for f in q.list(FILE, share="my-files", folder="Vidmorrow/Previews")] == []


def test_export_waits_for_the_renderer_and_for_a_blur(q):
    clip = q.seed(CLIP, name="Holiday", source=a_video(q).id)
    assert "has not read this clip yet" in q.act("export", clip).error
    q.change(CLIP, clip.id, status="ready", width=640, height=360, duration=4, fps=25)
    assert "Add a blur first" in q.act("export", clip).error
    q.act("add-blur", clip, label="Face", at=0, x=10, y=10, w=10, h=10)
    assert q.act("export", clip).toast.startswith("Queued")
    assert q.get(CLIP, clip.id)["status"] == "queued"
    assert "being rendered already" in q.act("export", clip).error


def test_somebody_else_sees_only_their_own_clips(q):
    clip = q.seed(CLIP, name="Mine", source=a_video(q).id, status="ready", width=640, height=360, duration=4, fps=25)
    sam = q.as_user("sam")
    assert "Mine" not in sam.view("clips").text()
    assert "That clip is gone" in sam.view("clip", clip=clip.id).text()
    assert sam.act("add-blur", clip, label="Face", at=0, x=1, y=1, w=1, h=1).refused
