"""Vidmorrow's tests: the real record store, files and gate, and quill.py, on this machine.

`cm quill test` runs them; `cm quill test --sandbox` runs the same tests
with quill.py in the sandbox, as a server runs it. The renderer (a service)
is tested on its own in test_render.py, and its arithmetic in test_graph.py.
"""

import contextlib

import pytest
from cloudmorrow.quill.testing import Harness

PROJECT, PIECE, EFFECT, MEDIA, FILE = (
    "vidmorrow.project",
    "vidmorrow.piece",
    "vidmorrow.effect",
    "vidmorrow.media",
    "file",
)


@pytest.fixture()
def q():
    with Harness(".") as harness:
        yield harness


def a_file(q, name="holiday.mp4", folder="Videos"):
    """A file in My Files, put there the way an upload is: a few bytes with the right name is enough for the records."""
    parts = folder.split("/")
    for depth in range(1, len(parts) + 1):
        with contextlib.suppress(Exception):  # there already
            q.seed(FILE, share="my-files", path="/".join(parts[:depth]), kind="folder")
    part = q._tmp / name
    part.write_bytes(b"\x00\x00\x00\x18ftypmp42")
    q.state.records.put(q._principal(), FILE, {"share": "my-files", "folder": folder, "name": name}, part)
    return next(f for f in q.list(FILE, share="my-files", folder=folder) if f["name"] == name)


def test_the_timeline_screen_and_its_datamodels_check_out(q):
    import tomllib
    from pathlib import Path

    manifest = tomllib.loads(Path("quill.toml").read_text())
    (editor,) = [s for s in manifest["screens"] if s.get("kit") == "timeline"]
    assert editor["pieces"] == PIECE and editor["effects"] == EFFECT and editor["frames"] == "frames"
    assert editor["media"] == {"share": "my-files", "within": "Videos"} and editor["export"] == "export"
    assert [s["id"] for s in manifest["screens"]] == ["clips", "editor"]
    assert manifest["apis"] == [{"id": "frames", "service": "render", "prefix": "frames"}]
    assert manifest["requires"]["runtimes"] == ["ffmpeg"]
    project = q.seed(PROJECT, name="Holiday")
    assert project["status"] == "editing"
    piece = q.seed(PIECE, project=project.id, track="V1", source="f_x", name="a.mp4", start=1.5, offset=0, length=3)
    effect = q.seed(EFFECT, piece=piece.id, label="Face", keyframes=[{"t": 0, "x": 1, "y": 2, "w": 3, "h": 4}])
    assert effect["kind"] == "blur" and effect["keyframes"][0]["w"] == 3
    q.delete(PROJECT, project.id)
    assert q.list(PIECE) == [] and q.list(EFFECT) == [], "a project takes its pieces and their effects with it"


def test_with_nothing_in_videos_the_clips_say_where_to_put_them(q):
    text = q.view("clips").text()
    assert "Put videos" in text and "Videos folder of My Files" in text
    assert [f["name"] for f in q.list(FILE, share="my-files", folder="") if f["name"] == "Videos"] == ["Videos"]


def test_every_video_and_sound_under_videos_is_listed_subfolders_too(q):
    a_file(q, "holiday.mp4")
    a_file(q, "song.mp3")
    a_file(q, "notes.txt")
    a_file(q, "beach.mov", folder="Videos/2026/Summer")
    a_file(q, "elsewhere.mp4", folder="Documents")
    view = q.view("clips")
    (table,) = view.find("table")
    rows = {r["fields"]["name"]: r["fields"] for r in table["records"]}
    assert set(rows) == {"holiday.mp4", "song.mp3", "beach.mov"}
    assert rows["beach.mov"]["where"] == "2026/Summer" and rows["holiday.mp4"]["where"] == "—"
    assert rows["song.mp3"]["what"] == "Sound" and rows["holiday.mp4"]["what"] == "Video"
    assert rows["holiday.mp4"]["bytes"] == "12 bytes" and rows["holiday.mp4"]["length"] == ""
    assert table["actions"] == ["edit-video"]


def test_edit_this_video_starts_a_project_with_the_whole_video_on_v1(q):
    video = a_file(q)
    done = q.act("edit-video", video)
    assert done.ok and done.toast == "Editing holiday"
    (project,) = q.list(PROJECT)
    assert project["name"] == "holiday" and project["status"] == "editing"
    assert [t["id"] for t in project["tracks"]] == ["V2", "V1", "A1", "A2"]
    assert any(
        e["effect"] == "go" and e["screen"] == "editor" and e["params"] == {"project": project.id} for e in done.effects
    )
    (piece,) = q.list(PIECE, project=project.id)
    assert piece["track"] == "V1" and piece["source"] == video.id and piece["name"] == "holiday.mp4"
    assert float(piece["start"]) == 0 and float(piece["offset"]) == 0
    assert piece.get("length") in (None, ""), "the renderer fills it in from the file"

    # Once the renderer has read the file, the list says how long it is and the next project starts with it.
    q.seed(MEDIA, source=video.id, name="holiday.mp4", modified=video["modified"], duration=12.5, video=True)
    (table,) = q.view("clips").find("table")
    assert table["records"][0]["fields"]["length"] == "0:12.5"
    assert q.act("edit-video", video, name="Second cut").ok
    second = next(p for p in q.list(PROJECT) if p["name"] == "Second cut")
    (again,) = q.list(PIECE, project=second.id)
    assert float(again["length"]) == 12.5
    assert "Second cut" in q.view("clips").text()
    # A file changed since it was read is not known any more.
    q.seed(MEDIA, source="f_other", name="x.mp4", modified="long ago", duration=3)
    stale = a_file(q, "stale.mp4")
    q.seed(MEDIA, source=stale.id, name="stale.mp4", modified="long ago", duration=3)
    (table,) = q.view("clips").find("table")
    assert next(r for r in table["records"] if r["fields"]["name"] == "stale.mp4")["fields"]["length"] == ""


def test_a_sound_goes_on_the_first_audio_track_and_other_files_are_refused(q):
    assert q.act("edit-video", a_file(q, "song.mp3")).ok
    (piece,) = q.list(PIECE)
    assert piece["track"] == "A1"
    assert "neither a video nor a sound" in q.act("edit-video", a_file(q, "notes.txt")).error


def test_export_needs_pieces_and_queues_once_with_its_settings(q):
    project = q.seed(PROJECT, name="Holiday")
    assert "nothing to export" in q.act("export", project).error
    q.seed(PIECE, project=project.id, track="V1", source=a_file(q).id, name="holiday.mp4", start=0, length=4)
    done = q.act("export", project, format="webm", size="720p")
    assert done.ok and done.toast.startswith("Queued")
    after = q.get(PROJECT, project.id)
    assert (after["status"], after["format"], after["size"], after["export_name"]) == (
        "queued",
        "webm",
        "720p",
        "Holiday",
    )
    assert after["message"] == "Waiting to be exported as WebM"
    assert "being exported already" in q.act("export", project).error
    q.change(PROJECT, project.id, status="done")
    assert q.act("export", project, format="gif", name="Short one").ok
    assert q.get(PROJECT, project.id)["export_name"] == "Short one"
    q.change(PROJECT, project.id, status="failed")
    assert q.act("export", project, format="avi").error, "the form refuses a format there is not"


def test_a_sound_only_export_of_muted_pieces_is_refused(q):
    project = q.seed(PROJECT, name="Quiet")
    q.seed(
        PIECE, project=project.id, track="V1", source=a_file(q).id, name="holiday.mp4", start=0, length=4, muted=True
    )
    assert "silent" in q.act("export", project, format="m4a").error


def test_somebody_else_sees_only_their_own_projects(q):
    project = q.seed(PROJECT, name="Mine")
    sam = q.as_user("sam")
    assert "Mine" not in sam.view("clips").text()
    assert sam.act("export", project).refused


def test_refresh_draws_again(q):
    assert q.act("refresh").ok
