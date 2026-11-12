[README.md](https://github.com/user-attachments/files/34759911/README.md)
# Reeltag

Reeltag is a desktop app for organizing a video library with searchable tags.
It stores video paths and tags in SQLite; it does not move or copy your video
files.

## Requirements

- Python 3 with Tkinter
- FFmpeg (`ffmpeg` and `ffprobe`) for video thumbnails and runtime summaries
- VLC or mpv for the live-tagging extensions and running-playlist controls

VLC, mpv, and FFmpeg are optional for launching the basic app, but their
features will not be available if they are missing.

## Install

From the project directory, run:

```bash
./install-video-tags.sh
```

The installer places the application in `~/.local/opt/reeltag`, creates a
desktop-menu entry, and installs the VLC or mpv tagging integration when those
players are detected. Restart the players after installing their integrations.
Then launch **Reeltag** from the applications menu.

If Tkinter is missing, install it with your distribution's package manager. For
example:

```bash
# Fedora
sudo dnf install python3-tkinter

# Debian or Ubuntu
sudo apt install python3-tk
```

FFmpeg provides thumbnail previews and `ffprobe`, which Reeltag uses to
calculate the total runtime of search results.

## Use the library

1. Open Reeltag and choose **Add videos to library…**.
2. Enter a tag or tags in the tag field, separated by commas, and use **Tag
   actions** to add them to the current video or selected videos.
3. Search tags in the **Search tags** field:
   - Separate terms with commas to match videos with **any** of the terms.
   - Join terms with `AND` to require **all** terms. For example:
     `comedy AND drama`.
4. Select **Search** or press Enter. Matching videos appear in the results
   table, with thumbnails when FFmpeg is available. The table shows the result
   count and combined runtime as well.
5. Use **Shuffle results** to randomize the current results table, or **Play all
   displayed videos** to play its contents in the displayed order.

Use **Clear** to clear the search and its imported-video filter. The database
and tags are stored in `~/.local/share/reeltag/.video-tags.sqlite3` (or
`$XDG_DATA_HOME/reeltag/.video-tags.sqlite3` when `XDG_DATA_HOME` is set).
If the legacy database is present and the new database does not yet exist,
Reeltag copies the legacy data from:

```text
/media/veracrypt1/.video-tags.sqlite3
```

VeraCrypt is not required for normal use.

## Live tagging

Keep Reeltag open while tagging so the player can send tags to its local
tagging bridge. Enter multiple tags separated by commas.

- **VLC (RPM/native):** Restart VLC, then open **Tools → Plugins and extensions
  → Extensions → Reeltag**.
- **VLC Flatpak:** The installer places the extension in the Flatpak's VLC data
  directory. Restart VLC and open the same Extensions menu. The Flatpak needs
  network access to connect to Reeltag's local bridge.
- **mpv:** Restart mpv, play a video, and press **Ctrl+Shift+T**. Enter
  comma-separated tags and press Enter to save; press Escape to cancel.

Live tagging currently supports local video files.

## Add results to a running playlist

Choose **VLC**, **VLC (Flatpak)**, or **MPV** in the media-player selector, then
select **Add results to playlist**. Reeltag appends the current results to an
accessible running playlist and skips videos already present. This action does
not start another player instance. If Reeltag cannot access the running
player's playlist, use **Play all displayed videos** to start playback through
Reeltag first, then try adding results again.

For mpv, Reeltag can control the playlist when mpv was started by Reeltag, which
sets up a private local IPC socket. A separately launched mpv instance without
that socket is not discoverable for playlist changes.

## Project files

- `video_tags_app.py` — desktop application, database, search, and player
  integration
- `video_tags_vlc.lua` — VLC live-tagging extension
- `video_tags_mpv.lua` — mpv live-tagging script
- `install-video-tags.sh` — user-level installer
