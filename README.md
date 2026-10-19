# Vidmorrow

A video editor for one job, done well: hide a face, a number plate or a
screen in a video, and keep the rest as it was. It is a
[Quill](https://github.com/Cloudmorrow/cloudmorrow/blob/main/docs/QUILLS.md)
for Cloudmorrow: the screens are drawn on the phone, in the web app and in
the terminal by Cloudmorrow itself, and the rendering is done beside your
own server, with an ffmpeg the install brings. Nothing leaves it.

## How it works

1. Put a video in the **Vidmorrow** folder of My Files, which is there from
   the first time you open Vidmorrow (the Files Quill, the desktop app's
   mount, WebDAV — any way in). It is listed on the Clips
   screen; *Edit this video* makes a clip of it.
2. **Add a blur**: what it hides, from when, and where the box is — left,
   top, width and height in percent of the frame, so it reads the same on a
   phone and in 4K — and how strongly.
3. **Add a keyframe** at a later moment with the box where it is then. In
   between, the box moves and resizes in a straight line. Open a keyframe to
   change it; delete one to take it out. Add as many blurs as the clip needs.
4. The renderer draws a **picture of every keyframe**, the blur as it will
   be with the box outlined, so you see what you are hiding before you render.
5. **Export.** The result lands beside the original, `<name> blurred.mp4`:
   H.264 and AAC that plays anywhere, the sound untouched.

A blur is seen from its first keyframe to its last; with only one keyframe,
from there to the end of the clip.

## What it adds to your Cloudmorrow

| | |
| --- | --- |
| Datamodels | uses the foundational `file`; introduces `vidmorrow.clip` (a video being edited, what the renderer read from it, its export), `vidmorrow.effect` (a blur, its strength) and `vidmorrow.keyframe` (where the box is at one moment, and its picture) |
| Screens | Clips and Editor (views) — on the phone, the web app, the terminal, `cm vidmorrow`, and to your assistant |
| Actions | Edit this video, Add a blur, Add a keyframe, Remove, Export, Refresh |
| Hooks | `keyframe_changed`: a changed keyframe asks the renderer for a new picture; a deleted one takes its picture with it |
| Datasets | `welcome`: a note on how it works in the Vidmorrow folder of everybody's drive, which is what makes the folder |
| Services, webhooks, APIs | `render`: a program beside the server (`services/render.py`), kept running, that reads new clips with ffprobe, draws the keyframes' pictures and renders the exports with ffmpeg, through the record API |

## What it needs

**ffmpeg**, which comes with the install: the manifest requires the `ffmpeg`
runtime, so the install sheet says *Adds to this server: ffmpeg 7.1.5 ·
119 MB*, and the server fetches the catalog's static build for its own
machine, checks it, and puts `ffmpeg` and `ffprobe` first on the renderer's
PATH. Nothing is installed on the system itself, and nobody opens a shell.
Removing Vidmorrow offers to take ffmpeg too when no other Quill needs it.
Should the program ever be missing all the same, a clip says so and waits.

The renderer runs as the administrator who installed Vidmorrow, the way
Quill services do today, so it renders the clips in *their* drive. Other
people's clips wait for services that run for everyone.

## Working on it

See [CLAUDE.md](CLAUDE.md). In short: `cm quill check`, `cm quill test`
(and `--sandbox`), then `cm quill dev --local`. The renderer's arithmetic
(keyframes to ffmpeg filter graphs) is `services/graph.py`, tested without a
server in `tests/test_graph.py`; the last test there renders a real clip when
ffmpeg is installed.
