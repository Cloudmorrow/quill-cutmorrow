# CutMorrow

A video editor for the videos and photos in your own drive: drag clips and
photos onto video and audio tracks, cut, trim and rearrange them, put one
small over another, blur what should not be seen with keyframes, crop, and
export a new file. It is a
[Quill](https://github.com/Cloudmorrow/cloudmorrow/blob/main/docs/QUILLS.md)
for Cloudmorrow: the editor is Cloudmorrow's timeline kit, drawn on the
phone, in the web app, in the terminal and on the command line by
Cloudmorrow itself, and the pictures and exports are made beside your own
server, with an ffmpeg the install brings. Nothing leaves it.

## How it works

1. Open **CutMorrow**: it starts on your projects. The new button asks for
   a name and a video size — 1080p, 720p, 4K, vertical, square, a size of
   your own, or the size of the first clip you add.
2. In the editor, the **media** beside the picture are the videos, photos
   and sounds in your drive, as tiles (another drive or share is chosen
   above them). Drag one onto a track where it should go — it snaps to the
   pieces already there — or tap it to put it at the playhead, after
   whatever is on the track; on the phone, **Add clip** brings the tiles up.
   A photo stays five seconds, or as long as you trim it. Move pieces along
   the timeline and between tracks, trim either end, split at the playhead,
   mute a piece or set its volume. A higher video track covers a lower one;
   every piece's sound is mixed. Or start from a video or a photo in
   **Media** (or Files): **Edit this video** on it makes a project with all
   of it on the first track.
3. **Make smaller** on a piece puts its picture in a box over whatever is on
   the tracks under it: drag the box on the viewer to move and resize it, or
   type where it goes, and **Fill the picture** makes it whole again. So a
   photo or a second clip can sit in a corner of the first.
4. **Add blur** on a piece and drag its box over a face, a number plate or a
   screen. Every change sets a keyframe at the playhead; between keyframes
   the box moves and resizes in a straight line. Blurs belong to their piece,
   so moving the piece takes them with it.
5. **Crop** the whole picture, if you like.
6. **Export**: MP4 (H.264 and AAC, plays anywhere), WebM (VP9 and Opus), MOV,
   GIF (no sound) or M4A (the sound alone), as the project or at 1080p, 720p
   or 480p. The file lands in My Files › **CutMorrow Exports**, under the
   name you give it.

## What it adds to your Cloudmorrow

| | |
| --- | --- |
| Datamodels | uses the foundational `file`; introduces `cutmorrow.project` (a timeline: its picture size, frame rate, tracks, crop, export settings and the status of its export), `cutmorrow.piece` (part of a file on a track: where it starts, where in the file it is cut from, how long, its volume, and its place in the picture), `cutmorrow.effect` (a blur on a piece and its keyframes) and `cutmorrow.media` (what the renderer read from a file: its length, size, whether it has sound, and whether it is a photo) |
| Screens | CutMorrow (the projects and the editor: the timeline kit) — on the phone, the web app, the terminal, `cm cutmorrow`, and to your assistant |
| Actions | Edit this video (on a video's or a photo's sheet, in Media or Files), Export (format, size, name), Refresh |
| Quills | requires [Media](https://github.com/Cloudmorrow/quill-media), which shows the videos and photos in your drive and is installed with CutMorrow |
| Services, APIs | `render`: a program beside the server (`services/render.py`), kept running, with ffmpeg. It serves the `frames` API the editor's viewer draws from (`/api/q/cutmorrow/frames/info`, `/source`, `/frame`), fills in the lengths and sizes the editor leaves blank, and renders the exports, through the record API |

## What it needs

**Media**, which comes with the install: the manifest requires the `media`
Quill (`[requires] quills`), so installing CutMorrow installs Media first,
from the catalog, and Media cannot be removed while CutMorrow is here.
Media works without CutMorrow.

**ffmpeg**, which comes with the install: the manifest requires the `ffmpeg`
runtime, so the install sheet says what it adds to the server, and the
server fetches the catalog's static build for its own machine, checks it,
and puts `ffmpeg` and `ffprobe` first on the renderer's PATH. Nothing is
installed on the system itself, and nobody opens a shell. Removing CutMorrow
offers to take ffmpeg too when no other Quill needs it. Should the program
ever be missing all the same, an export says so.

The renderer runs as the administrator who installed CutMorrow, the way
Quill services do today, so it draws and renders the projects in *their*
drive. Other people's projects wait for services that run for everyone.

## Working on it

See [CLAUDE.md](CLAUDE.md). In short: `cm quill check`, `cm quill test`
(and `--sandbox`), then `cm quill dev --local`. The renderer's arithmetic
(a timeline to ffmpeg filter graphs) is `services/graph.py`, tested without a
server in `tests/test_graph.py`; `tests/test_render.py` exports a real
timeline made of tiny ffmpeg-generated media and looks at its pixels, when
ffmpeg is installed.
