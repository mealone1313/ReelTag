#!/usr/bin/env python3
"""Desktop video tag manager using a per-user SQLite database."""

import hashlib
import base64
import json
import math
import os
import queue
import random
import re
import secrets
import socket
import socketserver
import shutil
import sqlite3
import subprocess
import tempfile
import threading
import time
from concurrent.futures import Future, ThreadPoolExecutor
from urllib.parse import unquote, urlsplit
from urllib.parse import urlencode
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen
from pathlib import Path
from collections.abc import Callable

DATA_DIR = Path(
    os.environ.get("XDG_DATA_HOME", Path.home() / ".local" / "share")
).expanduser() / "reeltag"
DB_DIR = DATA_DIR
DB_PATH = DB_DIR / ".video-tags.sqlite3"
TAG_BRIDGE_HOST = "127.0.0.1"
TAG_BRIDGE_PORT = 47653
VLC_HTTP_PORTS = {"VLC": 42153, "VLC (Flatpak)": 42154}
MEDIA_PLAYER_COMMANDS = (
    ("VLC", "vlc"),
    ("MPV", "mpv"),
    ("Celluloid", "celluloid"),
    ("GNOME Videos", "totem"),
    ("Haruna", "haruna"),
    ("SMPlayer", "smplayer"),
    ("Parole", "parole"),
    ("Dragon Player", "dragon"),
    ("Clapper", "clapper"),
    ("MPlayer", "mplayer"),
)
VIDEO_EXTENSIONS = {
    ".3g2", ".3gp", ".avi", ".flv", ".m2ts", ".m4v", ".mkv", ".mov",
    ".mp4", ".mpeg", ".mpg", ".mts", ".ogv", ".ts", ".vob", ".webm", ".wmv",
}


def connect_database(db_path: Path = DB_PATH) -> sqlite3.Connection:
    connection = sqlite3.connect(db_path)
    connection.execute("PRAGMA foreign_keys = ON")
    return connection


def initialize_database(db_path: Path = DB_PATH) -> None:
    if db_path == DB_PATH:
        DB_DIR.mkdir(parents=True, exist_ok=True)
    else:
        db_path.parent.mkdir(parents=True, exist_ok=True)
    with connect_database(db_path) as connection:
        connection.executescript(
            """
            CREATE TABLE IF NOT EXISTS videos (
                path TEXT PRIMARY KEY,
                file_hash TEXT
            );
            CREATE TABLE IF NOT EXISTS video_tags (
                video_path TEXT NOT NULL REFERENCES videos(path) ON DELETE CASCADE,
                tag TEXT NOT NULL COLLATE NOCASE,
                PRIMARY KEY (video_path, tag)
            );
            """
        )
        columns = {
            row[1] for row in connection.execute("PRAGMA table_info(videos)")
        }
        if "file_hash" not in columns:
            connection.execute("ALTER TABLE videos ADD COLUMN file_hash TEXT")
            for (video_path,) in connection.execute("SELECT path FROM videos").fetchall():
                if not Path(video_path).is_file():
                    continue
                file_hash = hash_video_file(video_path)
                existing = connection.execute(
                    "SELECT path FROM videos WHERE file_hash = ?",
                    (file_hash,),
                ).fetchone()
                if existing:
                    existing_path = existing[0]
                    connection.execute(
                        """
                        INSERT INTO video_tags(video_path, tag)
                        SELECT ?, tag FROM video_tags WHERE video_path = ?
                        ON CONFLICT(video_path, tag) DO NOTHING
                        """,
                        (existing_path, video_path),
                    )
                    connection.execute(
                        "DELETE FROM videos WHERE path = ?", (video_path,)
                    )
                else:
                    connection.execute(
                        "UPDATE videos SET file_hash = ? WHERE path = ?",
                        (file_hash, video_path),
                    )
        connection.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS videos_file_hash_idx "
            "ON videos(file_hash) WHERE file_hash IS NOT NULL"
        )


def hash_video_file(video_path: str) -> str:
    digest = hashlib.sha256()
    with Path(video_path).expanduser().open("rb") as video_file:
        for chunk in iter(lambda: video_file.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def register_video(db_path: Path, video_path: str) -> str:
    resolved_path = str(Path(video_path).expanduser().resolve())
    if not Path(resolved_path).is_file():
        raise FileNotFoundError(f"Video file not found: {resolved_path}")
    file_hash = hash_video_file(resolved_path)

    with connect_database(db_path) as connection:
        existing = connection.execute(
            "SELECT path FROM videos WHERE file_hash = ?",
            (file_hash,),
        ).fetchone()
        if existing:
            previous_path = existing[0]
            if previous_path != resolved_path:
                connection.execute(
                    """
                    DELETE FROM videos
                    WHERE path = ? AND (file_hash IS NULL OR file_hash != ?)
                    """,
                    (resolved_path, file_hash),
                )
                connection.execute(
                    """
                    INSERT INTO videos(path, file_hash) VALUES (?, NULL)
                    ON CONFLICT(path) DO NOTHING
                    """,
                    (resolved_path,),
                )
                connection.execute(
                    """
                    INSERT INTO video_tags(video_path, tag)
                    SELECT ?, tag FROM video_tags WHERE video_path = ?
                    ON CONFLICT(video_path, tag) DO NOTHING
                    """,
                    (resolved_path, previous_path),
                )
                connection.execute(
                    "DELETE FROM videos WHERE path = ?", (previous_path,)
                )
                connection.execute(
                    "UPDATE videos SET file_hash = ? WHERE path = ?",
                    (file_hash, resolved_path),
                )
            return resolved_path

        connection.execute(
            """
            INSERT INTO videos(path, file_hash) VALUES (?, ?)
            ON CONFLICT(path) DO UPDATE SET file_hash = excluded.file_hash
            """,
            (resolved_path, file_hash),
        )
    return resolved_path


def register_videos(db_path: Path, video_paths: list[str]) -> list[str]:
    return [register_video(db_path, video_path) for video_path in video_paths]


def generate_video_thumbnail(video_path: str, cache_dir: Path) -> Path:
    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        raise FileNotFoundError(
            "Video previews need ffmpeg. On Fedora, install it with: "
            "sudo dnf install ffmpeg-free"
        )

    source = Path(video_path).expanduser().resolve()
    if not source.is_file():
        raise FileNotFoundError(f"Video file not found: {source}")

    stat = source.stat()
    cache_key = hashlib.sha256(
        f"{source}\0{stat.st_mtime_ns}\0{stat.st_size}".encode("utf-8")
    ).hexdigest()
    thumbnail = cache_dir / f"{cache_key}.png"
    if thumbnail.is_file():
        return thumbnail

    cache_dir.mkdir(parents=True, exist_ok=True)
    temporary = cache_dir / f".{cache_key}.{os.getpid()}.{threading.get_ident()}.png"
    try:
        subprocess.run(
            [
                ffmpeg,
                "-hide_banner",
                "-loglevel",
                "error",
                "-nostdin",
                "-y",
                "-ss",
                "1",
                "-i",
                str(source),
                "-frames:v",
                "1",
                "-vf",
                "scale=320:180:force_original_aspect_ratio=decrease,"
                "pad=320:180:(ow-iw)/2:(oh-ih)/2",
                str(temporary),
            ],
            check=True,
            capture_output=True,
            timeout=20,
        )
        if not temporary.is_file() or temporary.stat().st_size == 0:
            raise RuntimeError(f"ffmpeg did not create a thumbnail for {source.name}.")
        temporary.replace(thumbnail)
    except subprocess.TimeoutExpired as error:
        raise RuntimeError(f"Thumbnail generation timed out for {source.name}.") from error
    except subprocess.CalledProcessError as error:
        detail = error.stderr.decode("utf-8", errors="replace").strip()
        raise RuntimeError(
            f"Could not create a thumbnail for {source.name}: "
            f"{detail or 'ffmpeg failed'}"
        ) from error
    finally:
        temporary.unlink(missing_ok=True)

    return thumbnail


def probe_video_duration(video_path: str) -> float:
    ffprobe = shutil.which("ffprobe")
    if not ffprobe:
        raise FileNotFoundError(
            "Video runtime summaries need ffprobe. Install ffmpeg to provide it."
        )

    try:
        result = subprocess.run(
            [
                ffprobe,
                "-v",
                "error",
                "-show_entries",
                "format=duration",
                "-of",
                "json",
                video_path,
            ],
            check=True,
            capture_output=True,
            text=True,
            timeout=20,
        )
        duration = float(json.loads(result.stdout)["format"]["duration"])
    except subprocess.TimeoutExpired as error:
        raise RuntimeError(
            f"Reading the runtime timed out for {Path(video_path).name}."
        ) from error
    except (subprocess.CalledProcessError, KeyError, TypeError, ValueError) as error:
        raise RuntimeError(
            f"Could not read the runtime for {Path(video_path).name}."
        ) from error

    if not math.isfinite(duration) or duration < 0:
        raise RuntimeError(f"Invalid runtime for {Path(video_path).name}.")
    return duration


def format_video_runtime(seconds: float) -> str:
    total_seconds = max(0, round(seconds))
    hours, remainder = divmod(total_seconds, 3600)
    minutes, seconds = divmod(remainder, 60)
    if hours:
        return f"{hours}:{minutes:02d}:{seconds:02d}"
    return f"{minutes}:{seconds:02d}"


def add_tags_to_videos(
    db_path: Path, video_paths: list[str], tags: list[str]
) -> None:
    normalized_tags = [tag.strip() for tag in tags if tag.strip()]
    if not normalized_tags:
        raise ValueError("Enter at least one tag.")

    normalized_paths = [register_video(db_path, path) for path in video_paths]
    with connect_database(db_path) as connection:
        connection.executemany(
            "INSERT INTO videos(path) VALUES (?) ON CONFLICT(path) DO NOTHING",
            [(video_path,) for video_path in normalized_paths],
        )
        connection.executemany(
            """
            INSERT INTO video_tags(video_path, tag) VALUES (?, ?)
            ON CONFLICT(video_path, tag) DO NOTHING
            """,
            [
                (video_path, tag)
                for video_path in normalized_paths
                for tag in normalized_tags
            ],
        )


def add_video_tags(db_path: Path, video_path: str, tags: list[str]) -> None:
    add_tags_to_videos(db_path, [video_path], tags)


def remove_video_tags(db_path: Path, video_paths: list[str], tags: list[str]) -> int:
    normalized_tags = [tag.strip() for tag in tags if tag.strip()]
    if not normalized_tags:
        raise ValueError("Enter one or more tags to remove.")

    normalized_paths = [
        str(Path(video_path).expanduser().resolve()) for video_path in video_paths
    ]
    with connect_database(db_path) as connection:
        cursor = connection.executemany(
            """
            DELETE FROM video_tags
            WHERE video_path = ? AND tag = ? COLLATE NOCASE
            """,
            [
                (video_path, tag)
                for video_path in normalized_paths
                for tag in normalized_tags
            ],
        )
        return cursor.rowcount


def remove_videos_from_library(db_path: Path, video_paths: list[str]) -> int:
    normalized_paths = [
        str(Path(video_path).expanduser().resolve()) for video_path in video_paths
    ]
    with connect_database(db_path) as connection:
        cursor = connection.executemany(
            "DELETE FROM videos WHERE path = ?",
            [(video_path,) for video_path in normalized_paths],
        )
        return cursor.rowcount


def add_tags_from_media_uri(uri: str, tag_text: str) -> str:
    parsed_uri = urlsplit(uri)
    if parsed_uri.scheme != "file" or parsed_uri.netloc not in ("", "localhost"):
        raise ValueError("Live tagging supports local video files only.")

    video_path = Path(unquote(parsed_uri.path)).resolve()
    if not video_path.is_file():
        raise FileNotFoundError(f"Video file not found: {video_path}")

    tags = [tag.strip() for tag in tag_text.split(",") if tag.strip()]
    if not tags:
        raise ValueError("Enter one or more comma-separated tags.")

    initialize_database(DB_PATH)
    add_video_tags(DB_PATH, str(video_path), tags)
    return str(video_path)


def parse_search_query(query: str) -> list[list[str]]:
    groups = []
    for group in query.split(","):
        terms = [
            term.strip()
            for term in re.split(r"\s+and\s+", group, flags=re.IGNORECASE)
            if term.strip()
        ]
        if terms:
            groups.append(terms)
    return groups


class LiveTagRequestHandler(socketserver.StreamRequestHandler):
    def handle(self) -> None:
        try:
            request = self.rfile.readline(1_048_577)
            if not request.endswith(b"\n") or len(request) > 1_048_576:
                raise ValueError("Invalid request.")
            fields = request[:-1].decode("ascii").split("\t")
            if len(fields) != 2:
                raise ValueError("Invalid request.")

            video_path = add_tags_from_media_uri(
                unquote(fields[0]), unquote(fields[1])
            )
        except (
            OSError,
            RuntimeError,
            sqlite3.Error,
            UnicodeError,
            ValueError,
        ) as error:
            response = f"ERROR\t{error}\n"
            self.wfile.write(response.encode("utf-8", errors="replace"))
            return

        self.wfile.write(b"OK\n")
        callback = getattr(self.server, "tagged_callback", None)
        if callback is not None:
            callback(video_path)


class LiveTagBridge(socketserver.ThreadingTCPServer):
    allow_reuse_address = True
    daemon_threads = True

    def __init__(self) -> None:
        super().__init__(
            (TAG_BRIDGE_HOST, TAG_BRIDGE_PORT), LiveTagRequestHandler
        )
        self.tagged_callback: Callable[[str], None] | None = None


def search_videos(db_path: Path, query: str = "") -> list[tuple[str, str]]:
    search_groups = parse_search_query(query)
    match_conditions = []
    parameters = []
    for terms in search_groups:
        term_conditions = []
        for term in terms:
            term_conditions.append(
                """
                EXISTS (
                    SELECT 1
                    FROM video_tags AS matching
                    WHERE matching.video_path = v.path
                      AND instr(lower(matching.tag), lower(?)) > 0
                )
                """
            )
            parameters.append(term)
        match_conditions.append("(" + " AND ".join(term_conditions) + ")")
    where_clause = " OR ".join(match_conditions) or "1"
    with connect_database(db_path) as connection:
        rows = connection.execute(
            f"""
            SELECT v.path, (
                SELECT group_concat(ordered.tag, ', ')
                FROM (
                    SELECT tag
                    FROM video_tags
                    WHERE video_path = v.path
                    ORDER BY tag COLLATE NOCASE
                ) AS ordered
            )
            FROM videos AS v
            WHERE {where_clause}
            ORDER BY v.path COLLATE NOCASE
            """,
            parameters,
        )
        return [(path, tags or "") for path, tags in rows.fetchall()]


def get_library_video_count(db_path: Path) -> int:
    with connect_database(db_path) as connection:
        (count,) = connection.execute("SELECT COUNT(*) FROM videos").fetchone()
        return count


def shuffle_video_rows(videos: list[tuple[str, str]]) -> list[tuple[str, str]]:
    shuffled = videos.copy()
    random.shuffle(shuffled)
    return shuffled


def get_videos_by_paths(
    db_path: Path, video_paths: list[str]
) -> list[tuple[str, str]]:
    normalized_paths = list(
        dict.fromkeys(str(Path(path).expanduser().resolve()) for path in video_paths)
    )
    if not normalized_paths:
        return []

    with connect_database(db_path) as connection:
        connection.execute(
            "CREATE TEMP TABLE selected_video_paths "
            "(path TEXT PRIMARY KEY, position INTEGER NOT NULL)"
        )
        connection.executemany(
            "INSERT INTO selected_video_paths(path, position) VALUES (?, ?)",
            [(path, index) for index, path in enumerate(normalized_paths)],
        )
        rows = connection.execute(
            """
            SELECT v.path, (
                SELECT group_concat(ordered.tag, ', ')
                FROM (
                    SELECT tag
                    FROM video_tags
                    WHERE video_path = v.path
                    ORDER BY tag COLLATE NOCASE
                ) AS ordered
            )
            FROM selected_video_paths AS selected
            JOIN videos AS v ON v.path = selected.path
            ORDER BY selected.position
            """
        )
        return [(path, tags or "") for path, tags in rows.fetchall()]


def available_media_players() -> list[tuple[str, tuple[str, ...]]]:
    players = []
    seen_commands = set()
    for label, command in MEDIA_PLAYER_COMMANDS:
        executable = shutil.which(command)
        if executable and executable not in seen_commands:
            players.append((label, (executable,)))
            seen_commands.add(executable)

    flatpak = shutil.which("flatpak")
    if flatpak:
        try:
            installed_apps = subprocess.run(
                [flatpak, "list", "--app", "--columns=application"],
                check=True,
                capture_output=True,
                text=True,
                timeout=10,
            ).stdout.splitlines()
        except (OSError, subprocess.SubprocessError):
            installed_apps = []
        if "org.videolan.VLC" in installed_apps:
            players.append(
                (
                    "VLC (Flatpak)",
                    (
                        flatpak,
                        "run",
                        "org.videolan.VLC",
                    ),
                )
            )
    return players


def build_player_argv(
    command: tuple[str, ...] | None,
    video_paths: list[str],
    player_args: list[str] | None = None,
) -> list[str]:
    if not video_paths:
        raise ValueError("Select at least one video.")
    extra_args = player_args or []
    if command is None:
        if len(video_paths) != 1:
            raise ValueError(
                "Choose a specific media player to play multiple videos as a playlist."
            )
        return ["xdg-open", video_paths[0]]
    if "@@u" in command:
        uris = [Path(path).resolve().as_uri() for path in video_paths]
        marker_index = command.index("@@u")
        return [
            *command[:marker_index],
            *extra_args,
            "@@u",
            *uris,
            "@@",
        ]
    return [*command, *extra_args, *video_paths]


def normalize_media_path(value: str) -> str | None:
    parsed = urlsplit(value)
    if parsed.scheme and parsed.scheme != "file":
        return None
    if parsed.scheme == "file":
        if parsed.netloc not in ("", "localhost"):
            return None
        value = unquote(parsed.path)
    return str(Path(value).expanduser().resolve())


def unique_media_paths(video_paths: list[str]) -> list[str]:
    unique_paths = []
    seen = set()
    for path in video_paths:
        normalized = normalize_media_path(path)
        if normalized is not None and normalized not in seen:
            unique_paths.append(normalized)
            seen.add(normalized)
    return unique_paths


def mpv_ipc_path() -> Path:
    runtime_directory = Path(
        os.environ.get("XDG_RUNTIME_DIR", tempfile.gettempdir())
    ) / f"reeltag-{getattr(os, 'getuid', lambda: 0)()}"
    runtime_directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    runtime_directory.chmod(0o700)
    return runtime_directory / "mpv.sock"


def send_mpv_command(socket_path: Path, command: list[str]) -> object:
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as connection:
        connection.settimeout(3)
        connection.connect(str(socket_path))
        request_id = time.monotonic_ns()
        connection.sendall(
            (json.dumps({"command": command, "request_id": request_id}) + "\n")
            .encode("utf-8")
        )
        response_buffer = b""
        while True:
            chunk = connection.recv(65536)
            if not chunk:
                raise ConnectionError("mpv closed its playlist connection.")
            response_buffer += chunk
            while b"\n" in response_buffer:
                line, response_buffer = response_buffer.split(b"\n", 1)
                response = json.loads(line)
                if response.get("request_id") != request_id:
                    continue
                if response.get("error") != "success":
                    raise RuntimeError(
                        f"mpv playlist command failed: {response.get('error')}."
                    )
                return response.get("data")


def mpv_playlist_paths(socket_path: Path) -> set[str]:
    playlist = send_mpv_command(socket_path, ["get_property", "playlist"])
    if not isinstance(playlist, list):
        raise RuntimeError("mpv returned an invalid playlist.")
    paths = set()
    for item in playlist:
        if not isinstance(item, dict):
            continue
        filename = item.get("filename")
        if isinstance(filename, str):
            normalized = normalize_media_path(filename)
            if normalized is not None:
                paths.add(normalized)
    return paths


def get_vlc_http_password() -> str:
    password_path = vlc_http_password_path()
    state_directory = password_path.parent
    state_directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    state_directory.chmod(0o700)
    try:
        descriptor = os.open(
            password_path,
            os.O_CREAT | os.O_EXCL | os.O_WRONLY,
            0o600,
        )
    except FileExistsError:
        password = password_path.read_text(encoding="ascii").strip()
        if not password:
            raise RuntimeError("The saved VLC playlist password is empty.")
        password_path.chmod(0o600)
        return password

    password = secrets.token_urlsafe(32)
    with os.fdopen(descriptor, "w", encoding="ascii") as password_file:
        password_file.write(password + "\n")
    return password


def vlc_http_password_path() -> Path:
    return (
        Path(os.environ.get("XDG_STATE_HOME", Path.home() / ".local" / "state"))
        / "reeltag"
        / "vlc-http-password"
    )


def read_vlc_http_password() -> str | None:
    password_path = vlc_http_password_path()
    if not password_path.is_file():
        return None
    password = password_path.read_text(encoding="ascii").strip()
    if not password:
        raise RuntimeError("The saved VLC playlist password is empty.")
    return password


def vlc_http_request(
    port: int,
    password: str,
    endpoint: str,
    parameters: dict[str, str] | None = None,
) -> dict[str, object]:
    query = "?" + urlencode(parameters) if parameters else ""
    request = Request(f"http://127.0.0.1:{port}{endpoint}{query}")
    credentials = base64.b64encode(f":{password}".encode("utf-8")).decode("ascii")
    request.add_header("Authorization", f"Basic {credentials}")
    try:
        with urlopen(request, timeout=3) as response:
            result = json.loads(response.read().decode("utf-8"))
    except HTTPError as error:
        raise RuntimeError(
            f"VLC playlist interface returned HTTP {error.code}."
        ) from error
    except (URLError, OSError, TimeoutError) as error:
        raise ConnectionError("VLC playlist interface is not available.") from error
    except (UnicodeError, json.JSONDecodeError) as error:
        raise RuntimeError("VLC returned an invalid playlist response.") from error
    if not isinstance(result, dict):
        raise RuntimeError("VLC returned an invalid playlist response.")
    return result


def vlc_playlist_paths(port: int, password: str) -> set[str]:
    playlist = vlc_http_request(port, password, "/requests/playlist.json")
    paths = set()
    children = playlist.get("children", [])
    pending = children if isinstance(children, list) else []
    while pending:
        item = pending.pop()
        if not isinstance(item, dict):
            continue
        children = item.get("children", [])
        if isinstance(children, list):
            pending.extend(children)
        uri = item.get("uri")
        if isinstance(uri, str):
            normalized = normalize_media_path(uri)
            if normalized is not None:
                paths.add(normalized)
    return paths


def append_paths_to_vlc_playlist(
    port: int, password: str, video_paths: list[str]
) -> tuple[int, int]:
    existing_paths = vlc_playlist_paths(port, password)
    queued_paths = unique_media_paths(video_paths)
    added = 0
    for path in queued_paths:
        if path in existing_paths:
            continue
        vlc_http_request(
            port,
            password,
            "/requests/status.json",
            {"command": "in_enqueue", "input": Path(path).as_uri()},
        )
        existing_paths.add(path)
        added += 1
    return added, len(video_paths) - added


def append_paths_to_vlc_mpris_playlist(
    video_paths: list[str],
) -> tuple[int, int]:
    try:
        import dbus
    except ImportError as error:
        raise RuntimeError(
            "VLC is running without Reeltag playlist control. Install the "
            "python3-dbus package or start playback from Reeltag first."
        ) from error

    try:
        bus = dbus.SessionBus()
        bus_names = bus.list_names() or []
        destinations = sorted(
            name
            for name in bus_names
            if name == "org.mpris.MediaPlayer2.vlc"
            or name.startswith("org.mpris.MediaPlayer2.vlc.")
        )
    except dbus.DBusException as error:
        raise RuntimeError(
            f"Could not connect to the running VLC playlist: {error}"
        ) from error
    if not destinations:
        raise ConnectionError("No running VLC playlist is available.")

    last_error: Exception | None = None
    for destination in destinations:
        try:
            player = bus.get_object(destination, "/org/mpris/MediaPlayer2")
            properties = dbus.Interface(
                player, dbus_interface="org.freedesktop.DBus.Properties"
            )
            identity = str(
                properties.Get("org.mpris.MediaPlayer2", "Identity")
            )
            if identity != "VLC media player":
                continue

            track_list = dbus.Interface(
                player, dbus_interface="org.mpris.MediaPlayer2.TrackList"
            )
            tracks = properties.Get(
                "org.mpris.MediaPlayer2.TrackList", "Tracks"
            )
            metadata = track_list.GetTracksMetadata(tracks)
            existing_paths = set()
            for item in metadata:
                uri = item.get("xesam:url")
                if uri is not None:
                    normalized = normalize_media_path(str(uri))
                    if normalized is not None:
                        existing_paths.add(normalized)

            queued_paths = unique_media_paths(video_paths)
            added = 0
            for path in queued_paths:
                if path in existing_paths:
                    continue
                dbus.Interface(
                    player, dbus_interface="org.mpris.MediaPlayer2.Player"
                ).OpenUri(Path(path).as_uri())
                existing_paths.add(path)
                added += 1
            return added, len(video_paths) - added
        except dbus.DBusException as error:
            last_error = error

    if last_error is not None:
        raise RuntimeError(
            f"Could not add videos to the running VLC playlist: {last_error}"
        ) from last_error
    raise ConnectionError(
        "No accessible running VLC playlist. Start playback from Reeltag first; "
        "Add results will then append without opening another instance."
    )


def append_paths_to_mpv_playlist(
    socket_path: Path, video_paths: list[str]
) -> tuple[int, int]:
    try:
        existing_paths = mpv_playlist_paths(socket_path)
    except OSError as error:
        raise ConnectionError(
            "No accessible running MPV playlist. Start playback from Reeltag "
            "first; Add results will then append without opening another instance."
        ) from error
    queued_paths = unique_media_paths(video_paths)
    added = 0
    for path in queued_paths:
        if path in existing_paths:
            continue
        flag = "append-play" if not existing_paths and added == 0 else "append"
        send_mpv_command(socket_path, ["loadfile", path, flag])
        existing_paths.add(path)
        added += 1
    return added, len(video_paths) - added


def replace_mpv_playlist(socket_path: Path, video_paths: list[str]) -> None:
    paths = unique_media_paths(video_paths)
    if not paths:
        raise ValueError("There are no videos to play.")
    send_mpv_command(socket_path, ["loadfile", paths[0], "replace"])
    for path in paths[1:]:
        send_mpv_command(socket_path, ["loadfile", path, "append"])


def replace_vlc_playlist(
    port: int, password: str, video_paths: list[str]
) -> tuple[int, int]:
    paths = unique_media_paths(video_paths)
    if not paths:
        raise ValueError("There are no videos to play.")
    vlc_http_request(
        port, password, "/requests/status.json", {"command": "pl_empty"}
    )
    added, _ = append_paths_to_vlc_playlist(port, password, paths)
    vlc_http_request(
        port, password, "/requests/status.json", {"command": "pl_play"}
    )
    return added, len(video_paths) - added


def main() -> None:
    import tkinter as tk
    from tkinter import filedialog, messagebox, simpledialog, ttk
    import tkinter.font as tkfont

    palette = {
        "background": "#251e1d",
        "surface": "#332624",
        "surface_active": "#4b3029",
        "field": "#1d1716",
        "foreground": "#ffe0b5",
        "muted": "#d0a982",
        "accent": "#eb8f42",
        "accent_active": "#d97832",
        "border": "#704238",
        "selection": "#69241d",
    }

    class RoundedButton(tk.Canvas):
        def __init__(
            self,
            parent: tk.Misc,
            text: str,
            command: Callable[[], None],
            style: str = "TButton",
        ) -> None:
            self.text = text
            self.command = command
            self.primary = style == "Primary.TButton"
            self.hovered = False
            self.pressed = False
            self.focused = False
            self.font = tkfont.Font(
                root=parent,
                family="Sans",
                size=10,
                weight="bold" if self.primary else "normal",
            )
            self.button_width = max(72, self.font.measure(text) + 32)
            self.button_height = 40
            super().__init__(
                parent,
                width=self.button_width,
                height=self.button_height,
                background=palette["background"],
                borderwidth=0,
                highlightthickness=0,
                takefocus=True,
                cursor="hand2",
            )
            self.bind("<Configure>", lambda _event: self.redraw())
            self.bind("<Enter>", self.on_enter)
            self.bind("<Leave>", self.on_leave)
            self.bind("<ButtonPress-1>", self.on_press)
            self.bind("<ButtonRelease-1>", self.on_release)
            self.bind("<Return>", self.on_activate)
            self.bind("<space>", self.on_activate)
            self.bind("<FocusIn>", self.on_focus)
            self.bind("<FocusOut>", self.on_blur)
            self.redraw()

        def rounded_points(self, width: float, height: float) -> list[float]:
            radius = min(13.0, height / 2)
            points: list[float] = []
            corners = (
                (width - radius, radius, -90),
                (width - radius, height - radius, 0),
                (radius, height - radius, 90),
                (radius, radius, 180),
            )
            for center_x, center_y, start_angle in corners:
                for step in range(7):
                    angle = math.radians(start_angle + step * 15)
                    points.extend(
                        (
                            center_x + radius * math.cos(angle),
                            center_y + radius * math.sin(angle),
                        )
                    )
            return points

        def redraw(self) -> None:
            self.delete("all")
            width = max(self.winfo_width(), self.button_width)
            height = max(self.winfo_height(), self.button_height)
            if self.primary:
                fill = palette["accent_active"] if self.pressed else (
                    "#f4a656" if self.hovered else palette["accent"]
                )
                text_color = palette["background"]
            else:
                fill = (
                    palette["accent_active"]
                    if self.pressed
                    else "#5b3930" if self.hovered else palette["surface_active"]
                )
                text_color = palette["foreground"]
            outline = palette["accent"] if self.focused else fill
            self.create_polygon(
                self.rounded_points(width - 2, height - 2),
                fill=fill,
                outline=outline,
                width=2 if self.focused else 1,
                smooth=True,
                splinesteps=12,
            )
            self.create_text(
                width / 2,
                height / 2,
                text=self.text,
                fill=text_color,
                font=self.font,
                anchor="center",
            )

        def on_enter(self, _event: object) -> None:
            self.hovered = True
            self.redraw()

        def on_leave(self, _event: object) -> None:
            self.hovered = False
            self.pressed = False
            self.redraw()

        def on_press(self, _event: object) -> None:
            self.pressed = True
            self.focus_set()
            self.redraw()

        def on_release(self, event: object) -> None:
            should_activate = (
                self.pressed
                and 0 <= event.x <= self.winfo_width()
                and 0 <= event.y <= self.winfo_height()
            )
            self.pressed = False
            self.redraw()
            if should_activate:
                self.command()

        def on_activate(self, _event: object) -> str:
            self.command()
            return "break"

        def on_focus(self, _event: object) -> None:
            self.focused = True
            self.redraw()

        def on_blur(self, _event: object) -> None:
            self.focused = False
            self.redraw()

    class VideoPicker:
        def __init__(
            self, parent: tk.Tk, title: str, initial_directory: Path, cache_dir: Path
        ) -> None:
            self.cache_dir = cache_dir
            self.selected_paths: list[str] = []
            self.items: dict[str, Path] = {}
            self.selected_items: set[str] = set()
            self.selected_file_paths: list[Path] = []
            self.thumbnail_paths: dict[Path, Path] = {}
            self.thumbnail_errors: dict[Path, str] = {}
            self.thumbnail_photos: dict[str, tk.PhotoImage] = {}
            self.preview_photo: tk.PhotoImage | None = None
            self.preview_path: Path | None = None
            self.preview_selected_count = 0
            self.directory_generation = 0
            self.thumbnail_results: queue.Queue[
                tuple[int, str, Future[Path]]
            ] = queue.Queue()
            self.thumbnail_futures: set[Future[Path]] = set()
            self.thumbnail_executor = ThreadPoolExecutor(
                max_workers=2, thread_name_prefix="reeltag-thumbnails"
            )
            self.thumbnail_poll_id: str | None = None
            self.is_open = True

            self.window = tk.Toplevel(parent)
            self.window.title(title)
            self.window.geometry("900x560")
            self.window.minsize(700, 420)
            self.window.transient(parent)
            self.window.configure(background=palette["background"])
            self.window.grab_set()
            self.window.protocol("WM_DELETE_WINDOW", self.cancel)

            location = ttk.Frame(self.window, padding=(16, 16, 16, 10))
            location.pack(fill="x")
            ttk.Label(location, text="Folder").pack(side="left", padx=(0, 8))
            self.directory = tk.StringVar(value=str(initial_directory))
            self.directory_entry = ttk.Entry(location, textvariable=self.directory)
            self.directory_entry.pack(side="left", fill="x", expand=True, padx=(0, 8))
            self.directory_entry.bind("<Return>", lambda _event: self.load_directory())
            RoundedButton(location, text="Go", command=self.load_directory).pack(
                side="left"
            )
            RoundedButton(
                location, text="Browse…", command=self.browse_directory
            ).pack(side="left", padx=(8, 0))
            RoundedButton(location, text="Up", command=self.go_up).pack(
                side="left", padx=(8, 0)
            )

            body = ttk.Frame(self.window, padding=(16, 0, 16, 10))
            body.pack(fill="both", expand=True)
            list_frame = ttk.Frame(body)
            list_frame.pack(side="left", fill="both", expand=True)
            list_controls = ttk.Frame(list_frame)
            list_controls.pack(fill="x", pady=(0, 6))
            ttk.Label(
                list_controls,
                text="Click a row or circle to select",
                style="Muted.TLabel",
            ).pack(side="left")
            self.select_all_text = tk.StringVar(value="Select all")
            ttk.Button(
                list_controls,
                textvariable=self.select_all_text,
                command=self.toggle_select_all,
            ).pack(side="right")

            self.placeholder_photo = tk.PhotoImage(
                master=self.window, width=80, height=45
            )
            self.placeholder_photo.put(
                palette["surface_active"], to=(0, 0, 80, 45)
            )
            self.files = ttk.Treeview(
                list_frame,
                columns=("selected", "kind"),
                show=("tree", "headings"),
                selectmode="none",
                style="Picker.Treeview",
            )
            self.files.heading("#0", text="Video files")
            self.files.heading("selected", text="Select")
            self.files.heading("kind", text="Type")
            self.files.column("#0", width=300, minwidth=180)
            self.files.column("selected", width=58, minwidth=58, anchor="center")
            self.files.column("kind", width=90, minwidth=70, anchor="center")
            self.files.tag_configure("picked", background=palette["selection"])
            file_scrollbar = ttk.Scrollbar(
                list_frame, orient="vertical", command=self.files.yview
            )
            self.files.configure(yscrollcommand=file_scrollbar.set)
            self.files.pack(side="left", fill="both", expand=True)
            file_scrollbar.pack(side="right", fill="y")
            self.files.bind("<Button-1>", self.toggle_clicked_item)

            preview = ttk.Frame(body, padding=(16, 8), style="Surface.TFrame")
            preview.pack(side="right", fill="y", padx=(14, 0))
            ttk.Label(preview, text="Preview").pack(anchor="w", pady=(0, 8))
            self.preview_image = ttk.Label(
                preview,
                text="Select a video",
                anchor="center",
                justify="center",
                width=38,
            )
            self.preview_image.pack(fill="x")
            self.preview_details = ttk.Label(
                preview, text="", style="Muted.TLabel", wraplength=250
            )
            self.preview_details.pack(anchor="w", fill="x", pady=(10, 0))
            ttk.Label(
                preview, text="Selected videos", style="Muted.TLabel"
            ).pack(anchor="w", pady=(18, 6))
            selected_frame = ttk.Frame(preview)
            selected_frame.pack(fill="both", expand=True)
            self.selected_files = tk.Listbox(
                selected_frame,
                height=8,
                width=34,
                selectmode="browse",
                exportselection=False,
                background=palette["field"],
                foreground=palette["foreground"],
                selectbackground=palette["selection"],
                selectforeground=palette["foreground"],
                highlightbackground=palette["border"],
                highlightcolor=palette["accent"],
                relief="flat",
                activestyle="none",
            )
            selected_scrollbar = ttk.Scrollbar(
                selected_frame, orient="vertical", command=self.selected_files.yview
            )
            self.selected_files.configure(yscrollcommand=selected_scrollbar.set)
            self.selected_files.pack(side="left", fill="both", expand=True)
            selected_scrollbar.pack(side="right", fill="y")
            self.selected_files.bind(
                "<<ListboxSelect>>", self.show_highlighted_preview
            )

            footer = ttk.Frame(self.window, padding=(16, 8, 16, 16))
            footer.pack(fill="x")
            self.selection_status = tk.StringVar(value="Select one or more videos.")
            ttk.Label(
                footer, textvariable=self.selection_status, style="Muted.TLabel"
            ).pack(side="left", fill="x", expand=True)
            RoundedButton(footer, text="Cancel", command=self.cancel).pack(
                side="right", padx=(8, 0)
            )
            RoundedButton(
                footer,
                text="Choose selected",
                command=self.accept,
                style="Primary.TButton",
            ).pack(side="right")

            self.load_directory()
            self.schedule_thumbnail_poll()
            parent.wait_window(self.window)

        def load_directory(self) -> None:
            directory = Path(self.directory.get()).expanduser()
            if not directory.is_dir():
                messagebox.showerror(
                    "Folder not found",
                    f"That folder does not exist:\n{directory}",
                    parent=self.window,
                )
                return

            try:
                paths = sorted(
                    (
                        path
                        for path in directory.iterdir()
                        if path.is_file() and path.suffix.lower() in VIDEO_EXTENSIONS
                    ),
                    key=lambda path: path.name.casefold(),
                )
            except OSError as error:
                messagebox.showerror(
                    "Could not read folder", str(error), parent=self.window
                )
                return

            self.directory_generation += 1
            generation = self.directory_generation
            for future in self.thumbnail_futures:
                future.cancel()
            self.thumbnail_futures.clear()
            self.files.delete(*self.files.get_children())
            self.items.clear()
            self.selected_items.clear()
            self.selected_file_paths = []
            self.thumbnail_paths.clear()
            self.thumbnail_errors.clear()
            self.thumbnail_photos.clear()
            self.selected_files.delete(0, "end")
            self.clear_preview("Select a video")
            self.select_all_text.set("Select all")
            self.directory.set(str(directory.resolve()))
            for index, path in enumerate(paths):
                item_id = f"video-{generation}-{index}"
                self.items[item_id] = path
                self.files.insert(
                    "",
                    "end",
                    iid=item_id,
                    text=path.name,
                    image=self.placeholder_photo,
                    values=("○", path.suffix.lower().lstrip(".").upper()),
                )
                future = self.thumbnail_executor.submit(
                    generate_video_thumbnail, str(path), self.cache_dir
                )
                self.thumbnail_futures.add(future)
                future.add_done_callback(
                    lambda completed, item=item_id, current=generation: (
                        self.thumbnail_results.put((current, item, completed))
                    )
                )
            self.selection_status.set(
                f"{len(paths)} video(s) found; 0 selected."
                if paths
                else f"No video files found in {directory.name or directory}."
            )
            self.schedule_thumbnail_poll()

        def browse_directory(self) -> None:
            path = filedialog.askdirectory(
                parent=self.window,
                title="Browse for videos",
                initialdir=self.directory.get(),
            )
            if path:
                self.directory.set(path)
                self.load_directory()

        def go_up(self) -> None:
            current = Path(self.directory.get())
            if current.parent != current:
                self.directory.set(str(current.parent))
                self.load_directory()

        def clear_preview(self, message: str) -> None:
            self.preview_photo = None
            self.preview_path = None
            self.preview_selected_count = 0
            self.preview_image.configure(image="", text=message)
            self.preview_details.configure(text="")

        def toggle_clicked_item(self, event: tk.Event[tk.Misc]) -> str:
            item_id = self.files.identify_row(event.y)
            if item_id in self.items:
                if item_id in self.selected_items:
                    self.selected_items.remove(item_id)
                    preferred_item = None
                else:
                    self.selected_items.add(item_id)
                    preferred_item = item_id
                self.refresh_selection(preferred_item)
            return "break"

        def toggle_select_all(self) -> None:
            if self.items and len(self.selected_items) == len(self.items):
                self.selected_items.clear()
                self.refresh_selection()
                return
            self.selected_items = set(self.items)
            self.refresh_selection()

        def refresh_selection(self, preferred_item: str | None = None) -> None:
            for item_id in self.items:
                selected = item_id in self.selected_items
                self.files.set(item_id, "selected", "●" if selected else "○")
                self.files.item(item_id, tags=("picked",) if selected else ())

            selected_item_ids = [
                item_id for item_id in self.items if item_id in self.selected_items
            ]
            selected = [self.items[item_id] for item_id in selected_item_ids]
            self.selected_file_paths = selected
            self.selected_files.delete(0, "end")
            for path in selected:
                self.selected_files.insert("end", path.name)

            all_selected = bool(self.items) and len(self.selected_items) == len(
                self.items
            )
            self.select_all_text.set(
                "Deselect all" if all_selected else "Select all"
            )
            self.selection_status.set(
                f"{len(selected)} of {len(self.items)} video(s) selected."
            )
            if not selected:
                self.clear_preview("Select a video")
                return

            preview_index = (
                selected_item_ids.index(preferred_item)
                if preferred_item in selected_item_ids
                else 0
            )
            self.selected_files.selection_set(preview_index)
            self.selected_files.see(preview_index)
            self.show_preview_for_path(selected[preview_index], len(selected))

        def show_highlighted_preview(self, _event: object = None) -> None:
            selection = self.selected_files.curselection()
            if not selection:
                return
            path = self.selected_file_paths[selection[0]]
            self.show_preview_for_path(path, len(self.selected_file_paths))

        def show_preview_for_path(self, path: Path, selected_count: int) -> None:
            self.preview_path = path
            self.preview_selected_count = selected_count
            self.update_preview()

        def update_preview(self) -> None:
            path = self.preview_path
            if path is None:
                self.clear_preview("Select a video")
                return

            details = (
                f"{path.name}\n{path.parent}\n\n"
                f"{self.preview_selected_count} video(s) selected."
            )
            thumbnail = self.thumbnail_paths.get(path)
            if thumbnail is None:
                error = self.thumbnail_errors.get(path)
                self.preview_photo = None
                self.preview_image.configure(
                    image="",
                    text=(
                        f"Thumbnail unavailable\n{error}"
                        if error
                        else "Loading thumbnail…"
                    ),
                )
                self.preview_details.configure(text=details)
                return

            try:
                self.preview_photo = tk.PhotoImage(
                    master=self.window, file=str(thumbnail)
                )
                self.preview_image.configure(image=self.preview_photo, text="")
                self.preview_details.configure(text=details)
            except tk.TclError as error:
                self.thumbnail_errors[path] = str(error)
                self.preview_photo = None
                self.preview_image.configure(
                    image="", text=f"Thumbnail unavailable\n{error}"
                )
                self.preview_details.configure(text=details)

        def schedule_thumbnail_poll(self) -> None:
            if (
                self.is_open
                and self.thumbnail_futures
                and self.thumbnail_poll_id is None
            ):
                self.thumbnail_poll_id = self.window.after(
                    100, self.poll_thumbnail_results
                )

        def poll_thumbnail_results(self) -> None:
            self.thumbnail_poll_id = None
            if not self.is_open:
                return
            while True:
                try:
                    generation, item_id, future = self.thumbnail_results.get_nowait()
                except queue.Empty:
                    break
                self.thumbnail_futures.discard(future)
                if generation != self.directory_generation or item_id not in self.items:
                    continue
                path = self.items[item_id]
                try:
                    thumbnail = future.result()
                    photo = tk.PhotoImage(master=self.window, file=str(thumbnail))
                    photo = photo.subsample(4, 4)
                except (OSError, RuntimeError, tk.TclError) as error:
                    self.thumbnail_errors[path] = str(error)
                    if path == self.preview_path:
                        self.update_preview()
                    continue
                self.thumbnail_paths[path] = thumbnail
                self.thumbnail_photos[item_id] = photo
                self.files.item(item_id, image=photo)
                if path == self.preview_path:
                    self.update_preview()
            self.schedule_thumbnail_poll()

        def accept(self) -> None:
            self.selected_paths = [
                str(self.items[item])
                for item in self.items
                if item in self.selected_items
            ]
            if not self.selected_paths:
                messagebox.showinfo(
                    "Select videos",
                    "Select one or more video files first.",
                    parent=self.window,
                )
                return
            self.close()
            self.window.destroy()

        def cancel(self) -> None:
            self.selected_paths = []
            self.close()
            self.window.destroy()

        def close(self) -> None:
            self.is_open = False
            if self.thumbnail_poll_id is not None:
                self.window.after_cancel(self.thumbnail_poll_id)
                self.thumbnail_poll_id = None
            for future in self.thumbnail_futures:
                future.cancel()
            self.thumbnail_executor.shutdown(wait=False, cancel_futures=True)

    class VideoTagsApp:
        def __init__(self, root: tk.Tk) -> None:
            self.root = root
            self.current_video: str | None = None
            self.last_video_directory = Path.home()
            self.thumbnail_cache = DB_DIR / ".video-tags-thumbnails"
            self.library_preview_photo: tk.PhotoImage | None = None
            self.imported_paths: list[str] = []
            self.result_thumbnail_generation = 0
            self.result_thumbnail_futures: set[Future[Path]] = set()
            self.result_thumbnail_results: queue.Queue[
                tuple[int, str, str, Future[Path]]
            ] = queue.Queue()
            self.result_thumbnail_poll_id: str | None = None
            self.result_thumbnail_executor = ThreadPoolExecutor(
                max_workers=2, thread_name_prefix="reeltag-result-thumbnails"
            )
            self.result_thumbnail_photos: dict[str, tk.PhotoImage] = {}
            self.result_duration_generation = 0
            self.result_duration_futures: set[Future[float]] = set()
            self.result_duration_results: queue.Queue[
                tuple[int, str, str, tuple[int, int], Future[float]]
            ] = queue.Queue()
            self.result_duration_poll_id: str | None = None
            self.result_duration_executor = ThreadPoolExecutor(
                max_workers=2, thread_name_prefix="reeltag-result-durations"
            )
            self.result_duration_cache: dict[str, tuple[int, int, float]] = {}
            self.result_durations: dict[str, float] = {}
            self.result_duration_errors: dict[str, str] = {}
            self.result_duration_pending = 0
            self.player_processes: dict[str, subprocess.Popen[bytes]] = {}
            self._mpv_socket_path: Path | None = None
            self._vlc_http_password: str | None = None
            self.player_by_label: dict[str, tuple[str, ...] | None] = {
                "System default": None
            }
            self.player_by_label.update(available_media_players())
            self.player_var = tk.StringVar(value="System default")
            root.title("Reeltag")
            root.geometry("1040x660")
            root.minsize(760, 500)
            root.configure(background=palette["background"])

            style = ttk.Style(root)
            if "clam" in style.theme_names():
                style.theme_use("clam")
            style.configure(
                ".",
                background=palette["background"],
                foreground=palette["foreground"],
                font=("Sans", 10),
            )
            style.configure("TFrame", background=palette["background"])
            style.configure(
                "Surface.TFrame",
                background=palette["surface"],
                relief="flat",
            )
            style.configure(
                "TLabel",
                background=palette["background"],
                foreground=palette["foreground"],
            )
            style.configure(
                "Muted.TLabel",
                background=palette["background"],
                foreground=palette["muted"],
            )
            style.configure(
                "Title.TLabel",
                background=palette["background"],
                foreground=palette["foreground"],
                font=("Sans", 21, "bold"),
            )
            style.configure(
                "TButton",
                background=palette["surface_active"],
                foreground=palette["foreground"],
                bordercolor=palette["border"],
                padding=(14, 9),
                relief="flat",
                borderwidth=0,
                focusthickness=0,
            )
            style.map(
                "TButton",
                background=[
                    ("pressed", palette["accent_active"]),
                    ("active", "#5b3930"),
                ],
                foreground=[
                    ("disabled", palette["muted"]),
                    ("pressed", palette["foreground"]),
                    ("active", palette["foreground"]),
                ],
            )
            style.configure(
                "Primary.TButton",
                background=palette["accent"],
                foreground=palette["background"],
                bordercolor=palette["accent"],
                font=("Sans", 10, "bold"),
            )
            style.map(
                "Primary.TButton",
                background=[
                    ("pressed", palette["accent_active"]),
                    ("active", "#f4a656"),
                ],
                foreground=[("active", palette["background"])],
            )
            style.configure(
                "TEntry",
                fieldbackground=palette["field"],
                foreground=palette["foreground"],
                insertcolor=palette["foreground"],
                bordercolor=palette["border"],
                lightcolor=palette["border"],
                darkcolor=palette["border"],
                padding=8,
            )
            style.configure(
                "TCombobox",
                fieldbackground=palette["field"],
                background=palette["surface_active"],
                foreground=palette["foreground"],
                arrowcolor=palette["accent"],
                bordercolor=palette["border"],
                padding=6,
            )
            style.map(
                "TCombobox",
                fieldbackground=[("readonly", palette["field"])],
                foreground=[("readonly", palette["foreground"])],
                selectbackground=[("readonly", palette["field"])],
                selectforeground=[("readonly", palette["foreground"])],
            )
            style.configure(
                "Treeview",
                background=palette["surface"],
                fieldbackground=palette["surface"],
                foreground=palette["foreground"],
                bordercolor=palette["border"],
                rowheight=58,
                relief="flat",
            )
            style.configure(
                "Picker.Treeview",
                background=palette["surface"],
                fieldbackground=palette["surface"],
                foreground=palette["foreground"],
                bordercolor=palette["border"],
                rowheight=58,
                relief="flat",
            )
            style.map(
                "Treeview",
                background=[("selected", palette["selection"])],
                foreground=[("selected", palette["foreground"])],
            )
            style.map(
                "Picker.Treeview",
                background=[("selected", palette["selection"])],
                foreground=[("selected", palette["foreground"])],
            )
            style.configure(
                "Treeview.Heading",
                background=palette["field"],
                foreground=palette["muted"],
                bordercolor=palette["border"],
                padding=(10, 9),
                font=("Sans", 9, "bold"),
            )
            style.map(
                "Treeview.Heading",
                background=[("active", palette["surface_active"])],
                foreground=[("active", palette["foreground"])],
            )
            style.configure(
                "Vertical.TScrollbar",
                background=palette["surface_active"],
                troughcolor=palette["background"],
                bordercolor=palette["background"],
                arrowcolor=palette["muted"],
            )

            header = ttk.Frame(root, padding=(22, 18, 22, 8))
            header.pack(fill="x")
            ttk.Label(
                header,
                text="Reeltag",
                style="Title.TLabel",
                anchor="center",
            ).pack(fill="x")
            ttk.Label(
                header,
                text="Search your library, add tags, and build playlists.",
                style="Muted.TLabel",
                anchor="center",
            ).pack(fill="x", pady=(3, 0))

            toolbar = ttk.Frame(root, padding=(22, 12, 22, 12))
            toolbar.pack(fill="x")
            search_controls = ttk.Frame(toolbar)
            search_controls.pack(anchor="center")
            ttk.Label(
                search_controls, text="Search tags", anchor="center", justify="center"
            ).pack(side="left", padx=(0, 8))
            self.query = tk.StringVar()
            search_entry = ttk.Entry(
                search_controls, textvariable=self.query, width=32, justify="center"
            )
            search_entry.pack(side="left", padx=(6, 8))
            search_entry.bind("<Return>", lambda _event: self.refresh())
            RoundedButton(
                search_controls,
                text="Search",
                command=self.refresh,
                style="Primary.TButton",
            ).pack(side="left")
            RoundedButton(search_controls, text="Clear", command=self.clear_search).pack(
                side="left", padx=(8, 0)
            )
            ttk.Label(
                toolbar,
                text="Separate tags with commas for any match; use AND to require all tags.",
                style="Muted.TLabel",
                anchor="center",
                justify="center",
            ).pack(anchor="center", pady=(6, 0))

            player_row = ttk.Frame(root, padding=(22, 4, 22, 12))
            player_row.pack(fill="x")
            player_controls = ttk.Frame(player_row)
            player_controls.pack(anchor="center")
            ttk.Label(
                player_controls,
                text="Media player:",
                anchor="center",
                justify="center",
            ).pack(side="left")
            player_choices = list(self.player_by_label)
            self.player_picker = ttk.Combobox(
                player_controls,
                textvariable=self.player_var,
                values=player_choices,
                state="readonly",
                width=20,
                justify="center",
            )
            self.player_picker.pack(side="left", padx=(6, 8))
            self.player_picker.bind("<<ComboboxSelected>>", self.on_player_changed)
            RoundedButton(
                player_controls, text="Choose other…", command=self.choose_player
            ).pack(side="left")
            if len(player_choices) == 1:
                ttk.Label(
                    player_controls,
                    text="Install a media player such as VLC or MPV to select it here.",
                    anchor="center",
                    justify="center",
                ).pack(side="left", padx=8)

            actions = ttk.Frame(root, padding=(22, 4, 22, 10))
            actions.pack(fill="x")
            primary_actions = ttk.Frame(actions)
            primary_actions.pack(anchor="center")
            RoundedButton(
                primary_actions,
                text="Add videos to library…",
                command=self.add_new_video,
            ).pack(side="left")
            RoundedButton(
                primary_actions,
                text="Shuffle results",
                command=self.shuffle_search_results,
            ).pack(side="left", padx=8)
            RoundedButton(
                primary_actions,
                text="Play all displayed videos",
                command=self.play_search_results,
            ).pack(side="left")
            RoundedButton(
                primary_actions,
                text="Add results to playlist",
                command=self.add_results_to_playlist,
            ).pack(side="left", padx=(8, 0))
            secondary_actions = ttk.Frame(actions)
            secondary_actions.pack(anchor="center", pady=(8, 0))
            RoundedButton(
                secondary_actions,
                text="Clear selection",
                command=self.clear_video_selection,
            ).pack(side="left", padx=(8, 0))
            RoundedButton(
                secondary_actions,
                text="Remove selected from library…",
                command=self.remove_selected_videos,
            ).pack(side="left", padx=(8, 0))

            tagging = ttk.Frame(root, padding=(22, 8, 22, 14))
            tagging.pack(fill="x")
            self.current_video_label = tk.StringVar(value="Current video: none")
            ttk.Label(
                tagging,
                textvariable=self.current_video_label,
                style="Muted.TLabel",
                anchor="center",
                justify="center",
            ).pack(fill="x", pady=(0, 8))
            tag_controls = ttk.Frame(tagging)
            tag_controls.pack(anchor="center")
            self.new_tags = tk.StringVar()
            tag_entry = ttk.Entry(
                tag_controls,
                textvariable=self.new_tags,
                width=36,
                justify="center",
            )
            tag_entry.pack(side="left", padx=(0, 8))
            tag_entry.bind("<Return>", lambda _event: self.add_tags_to_current())
            self.tag_actions_button = RoundedButton(
                tag_controls,
                text="Tag actions ▾",
                command=self.show_tag_actions_menu,
            )
            self.tag_actions_button.pack(side="left")

            content = ttk.Frame(root, padding=(22, 4, 22, 10))
            content.pack(fill="both", expand=True)
            self.preview_panel = ttk.Frame(
                content, padding=14, style="Surface.TFrame"
            )
            ttk.Label(
                self.preview_panel,
                text="Selected video preview",
                anchor="center",
                justify="center",
            ).pack(fill="x", pady=(0, 10))
            self.library_preview = ttk.Label(
                self.preview_panel,
                text="Select a video to preview it",
                anchor="center",
                justify="center",
                width=42,
            )
            self.library_preview.pack(side="left", padx=(0, 16))
            self.library_preview_details = ttk.Label(
                self.preview_panel,
                text="",
                style="Muted.TLabel",
                wraplength=650,
                justify="center",
                anchor="center",
            )
            self.library_preview_details.pack(
                side="left", fill="both", expand=True, anchor="w"
            )

            self.results_frame = ttk.Frame(content)
            self.results_summary = tk.StringVar(value="Results: 0 videos")
            ttk.Label(
                self.results_frame,
                textvariable=self.results_summary,
                style="Muted.TLabel",
                anchor="center",
                justify="center",
            ).pack(fill="x", pady=(0, 6))
            self.results = ttk.Treeview(
                self.results_frame,
                columns=("video", "tags"),
                show=("tree", "headings"),
                selectmode="extended",
            )
            self.results.heading("#0", text="Preview", anchor="center")
            self.results.heading("video", text="Video")
            self.results.heading("tags", text="Tags")
            self.results.column("#0", width=100, minwidth=88, stretch=False, anchor="center")
            self.results.heading("video", anchor="center")
            self.results.heading("tags", anchor="center")
            self.results.column("video", width=500, minwidth=180, anchor="center")
            self.results.column("tags", width=260, minwidth=120, anchor="center")
            scrollbar = ttk.Scrollbar(
                self.results_frame, orient="vertical", command=self.results.yview
            )
            self.results_scrollbar = scrollbar
            self.results.configure(yscrollcommand=scrollbar.set)
            self.results.pack(side="left", fill="both", expand=True)
            self.results_scrollbar.pack(side="right", fill="y")
            self.results.bind("<Double-1>", lambda _event: self.open_selected())
            self.results.bind("<<TreeviewSelect>>", self.on_video_selected)

            self.status = tk.StringVar()
            self.library_count = tk.StringVar(value="Library: 0 videos")
            status_bar = ttk.Frame(root, padding=(22, 10))
            status_bar.pack(fill="x")
            ttk.Label(
                status_bar,
                textvariable=self.library_count,
                style="Muted.TLabel",
            ).pack(fill="x", anchor="center")
            ttk.Label(
                status_bar, textvariable=self.status, style="Muted.TLabel"
            ).pack(fill="x", anchor="center")
            self.bridge_error: str | None = None
            try:
                self.bridge = LiveTagBridge()

                def refresh_after_live_tag(_path: str) -> None:
                    root.after(0, self.refresh)

                self.bridge.tagged_callback = refresh_after_live_tag
                threading.Thread(
                    target=self.bridge.serve_forever,
                    name="live-tag-bridge",
                    daemon=True,
                ).start()
            except OSError as error:
                self.bridge = None
                self.bridge_error = f"Live tag bridge unavailable: {error}"
            root.protocol("WM_DELETE_WINDOW", self.close)
            self.refresh()

        def on_player_changed(self, _event: object = None) -> None:
            self.player_picker.selection_clear()

        def show_tag_actions_menu(self) -> None:
            menu = tk.Menu(
                self.root,
                tearoff=False,
                background=palette["surface"],
                foreground=palette["foreground"],
                activebackground=palette["selection"],
                activeforeground=palette["foreground"],
                borderwidth=0,
                font=("Sans", 10),
            )
            self.tag_actions_menu = menu
            menu.add_command(
                label="Add tags to current video",
                command=self.add_tags_to_current,
                justify="center",
            )
            menu.add_command(
                label="Add tags to selected videos",
                command=self.add_tags_to_selected,
                justify="center",
            )
            menu.add_separator()
            menu.add_command(
                label="Remove tags from selected videos",
                command=self.remove_tags_from_selected,
                justify="center",
            )
            menu.add_command(
                label="Remove tags from current video",
                command=self.remove_tags_from_current,
                justify="center",
            )
            button = self.tag_actions_button
            menu.post(button.winfo_rootx(), button.winfo_rooty() + button.winfo_height())

        def choose_player(self) -> None:
            path = filedialog.askopenfilename(
                parent=self.root,
                title="Choose a media player executable",
                filetypes=[("Applications", "*"), ("All files", "*")],
            )
            if not path:
                return
            if not os.access(path, os.X_OK):
                messagebox.showerror(
                    "Not an executable", "Choose an executable media player file."
                )
                return
            label = f"Custom: {Path(path).name}"
            self.player_by_label[label] = (path,)
            self.player_picker.configure(
                values=[*self.player_by_label]
            )
            self.player_var.set(label)
            self.status.set(f"Using media player: {path}")

        def refresh(self) -> None:
            search_term = self.query.get().strip()
            self.result_thumbnail_generation += 1
            generation = self.result_thumbnail_generation
            for future in self.result_thumbnail_futures:
                future.cancel()
            self.result_thumbnail_futures.clear()
            self.result_thumbnail_photos.clear()
            self.result_duration_generation += 1
            duration_generation = self.result_duration_generation
            for future in self.result_duration_futures:
                future.cancel()
            self.result_duration_futures.clear()
            self.result_durations.clear()
            self.result_duration_errors.clear()
            self.result_duration_pending = 0
            self.results.delete(*self.results.get_children())
            has_visible_rows = bool(search_term or self.imported_paths)
            if has_visible_rows:
                self.preview_panel.pack(fill="x", pady=(0, 10))
                self.results_frame.pack(fill="both", expand=True)
            else:
                self.results_frame.pack_forget()
                self.preview_panel.pack_forget()

            try:
                initialize_database()
                library_count = get_library_video_count(DB_PATH)
                if search_term:
                    videos = search_videos(DB_PATH, search_term)
                elif self.imported_paths:
                    videos = get_videos_by_paths(DB_PATH, self.imported_paths)
                else:
                    videos = []
            except (OSError, sqlite3.Error, RuntimeError) as error:
                self.status.set(str(error))
                return

            self.library_count.set(
                f"Library: {library_count} video"
                f"{'' if library_count == 1 else 's'}"
            )

            for path, tags in videos:
                item_id = self.results.insert("", "end", values=(path, tags))
                future = self.result_thumbnail_executor.submit(
                    generate_video_thumbnail, path, self.thumbnail_cache
                )
                self.result_thumbnail_futures.add(future)
                future.add_done_callback(
                    lambda completed, item=item_id, video_path=path, current=generation: (
                        self.result_thumbnail_results.put(
                            (current, item, video_path, completed)
                        )
                    )
                )
                try:
                    stat = Path(path).stat()
                    signature = (stat.st_mtime_ns, stat.st_size)
                except OSError as error:
                    self.result_duration_errors[item_id] = str(error)
                    continue

                cached_duration = self.result_duration_cache.get(path)
                if cached_duration and cached_duration[:2] == signature:
                    self.result_durations[item_id] = cached_duration[2]
                    continue

                self.result_duration_pending += 1
                duration_future = self.result_duration_executor.submit(
                    probe_video_duration, path
                )
                self.result_duration_futures.add(duration_future)
                duration_future.add_done_callback(
                    lambda completed, item=item_id, video_path=path,
                    file_signature=signature, current=duration_generation: (
                        self.result_duration_results.put(
                            (
                                current,
                                item,
                                video_path,
                                file_signature,
                                completed,
                            )
                        )
                    )
                )
            self.update_results_summary(len(videos))
            self.schedule_result_thumbnail_poll()
            self.schedule_result_duration_poll()
            self.clear_library_preview("Select a video")
            status = (
                f"{len(videos)} video(s) found for '{search_term}'."
                if search_term
                else "Ready."
            )
            if self.bridge_error:
                status += f" {self.bridge_error}"
            else:
                status += " Live tag bridge is ready."
            self.status.set(status)

        def show_imported_videos(self, video_paths: list[str]) -> None:
            self.imported_paths = video_paths
            self.query.set("")
            self.refresh()
            items_by_path = {
                self.results.item(item, "values")[0]: item
                for item in self.results.get_children()
            }
            selected_items = [
                items_by_path[path]
                for path in video_paths
                if path in items_by_path
            ]
            if selected_items:
                self.results.selection_set(*selected_items)
                self.results.focus(selected_items[0])
                self.results.see(selected_items[0])
                self.on_video_selected()
                self.status.set(
                    f"Added and selected {len(selected_items)} video(s). "
                    "The selected filename and thumbnail are shown above."
                )
            elif video_paths:
                self.status.set(
                    "The videos were added, but none could be displayed in the library."
                )

        def close(self) -> None:
            if self.bridge is not None:
                self.bridge.shutdown()
                self.bridge.server_close()
            if self.result_thumbnail_poll_id is not None:
                self.root.after_cancel(self.result_thumbnail_poll_id)
                self.result_thumbnail_poll_id = None
            for future in self.result_thumbnail_futures:
                future.cancel()
            self.result_thumbnail_executor.shutdown(
                wait=False, cancel_futures=True
            )
            if self.result_duration_poll_id is not None:
                self.root.after_cancel(self.result_duration_poll_id)
                self.result_duration_poll_id = None
            for future in self.result_duration_futures:
                future.cancel()
            self.result_duration_executor.shutdown(
                wait=False, cancel_futures=True
            )
            self.root.destroy()

        def schedule_result_thumbnail_poll(self) -> None:
            if (
                self.result_thumbnail_futures
                and self.result_thumbnail_poll_id is None
            ):
                self.result_thumbnail_poll_id = self.root.after(
                    100, self.poll_result_thumbnail_results
                )

        def poll_result_thumbnail_results(self) -> None:
            self.result_thumbnail_poll_id = None
            visible_items = set(self.results.get_children())
            while True:
                try:
                    generation, item_id, path, future = (
                        self.result_thumbnail_results.get_nowait()
                    )
                except queue.Empty:
                    break
                self.result_thumbnail_futures.discard(future)
                if (
                    generation != self.result_thumbnail_generation
                    or item_id not in visible_items
                    or self.results.item(item_id, "values")[0] != path
                ):
                    continue
                try:
                    thumbnail = future.result()
                    photo = tk.PhotoImage(master=self.root, file=str(thumbnail))
                    photo = photo.subsample(4, 4)
                except (OSError, RuntimeError, tk.TclError):
                    self.results.item(item_id, text="Unavailable")
                    continue
                self.result_thumbnail_photos[item_id] = photo
                self.results.item(item_id, image=photo, text="")
            self.schedule_result_thumbnail_poll()

        def update_results_summary(self, result_count: int) -> None:
            count_label = (
                f"{result_count} video"
                f"{'' if result_count == 1 else 's'}"
            )
            completed = len(self.result_durations) + len(self.result_duration_errors)
            runtime = format_video_runtime(sum(self.result_durations.values()))
            summary = f"Results: {count_label} | Total runtime: {runtime}"
            if self.result_duration_pending:
                summary += (
                    f" | Checking runtime: {completed}/"
                    f"{completed + self.result_duration_pending}"
                )
            if self.result_duration_errors:
                summary += (
                    f" | {len(self.result_duration_errors)} runtime(s) unavailable: "
                    f"{next(iter(self.result_duration_errors.values()))}"
                )
            self.results_summary.set(summary)

        def schedule_result_duration_poll(self) -> None:
            if (
                self.result_duration_futures
                and self.result_duration_poll_id is None
            ):
                self.result_duration_poll_id = self.root.after(
                    100, self.poll_result_duration_results
                )

        def poll_result_duration_results(self) -> None:
            self.result_duration_poll_id = None
            visible_items = set(self.results.get_children())
            result_count = len(visible_items)
            while True:
                try:
                    (
                        generation,
                        item_id,
                        path,
                        signature,
                        future,
                    ) = self.result_duration_results.get_nowait()
                except queue.Empty:
                    break
                self.result_duration_futures.discard(future)
                if (
                    generation != self.result_duration_generation
                    or item_id not in visible_items
                    or self.results.item(item_id, "values")[0] != path
                ):
                    continue
                self.result_duration_pending -= 1
                try:
                    duration = future.result()
                except (OSError, RuntimeError, subprocess.SubprocessError, ValueError) as error:
                    self.result_duration_errors[item_id] = str(error)
                    continue
                self.result_durations[item_id] = duration
                self.result_duration_cache[path] = (
                    signature[0],
                    signature[1],
                    duration,
                )
            self.update_results_summary(result_count)
            self.schedule_result_duration_poll()

        def on_video_selected(self, _event: object = None) -> None:
            selection = self.results.selection()
            if not selection:
                self.clear_library_preview("Select a video")
                return
            self.preview_panel.pack(fill="x", pady=(0, 10))
            self.results_frame.pack(fill="both", expand=True)
            path = self.results.item(selection[0], "values")[0]
            self.set_current_video(path)
            self.show_library_preview(path)

        def clear_library_preview(self, message: str) -> None:
            self.library_preview_photo = None
            self.library_preview.configure(image="", text=message)
            self.library_preview_details.configure(text="")

        def show_library_preview(self, path: str) -> None:
            selected = self.results.selection()
            tags = next(
                (
                    values[1]
                    for item in selected
                    if (values := self.results.item(item, "values"))
                    and values[0] == path
                ),
                "",
            )
            details = f"File: {Path(path).name}\nLocation: {path}"
            if tags:
                details += f"\nTags: {tags}"
            if len(selected) > 1:
                details += f"\n\n{len(selected)} videos selected"
            try:
                thumbnail = generate_video_thumbnail(path, self.thumbnail_cache)
                self.library_preview_photo = tk.PhotoImage(
                    master=self.root, file=str(thumbnail)
                )
                self.library_preview.configure(image=self.library_preview_photo, text="")
                self.library_preview_details.configure(text=details)
            except (OSError, RuntimeError, tk.TclError) as error:
                self.clear_library_preview(f"Thumbnail unavailable\n{error}")
                self.library_preview_details.configure(text=details)

        def set_current_video(self, path: str) -> None:
            self.current_video = path
            self.current_video_label.set(f"Current video: {Path(path).name}")

        def clear_search(self) -> None:
            self.query.set("")
            self.imported_paths = []
            self.refresh()

        def shuffle_search_results(self) -> None:
            children = self.results.get_children()
            if not children:
                messagebox.showinfo(
                    "No search results",
                    "Search for videos before shuffling the results.",
                )
                return

            videos: list[tuple[str, str]] = []
            for item in children:
                values = self.results.item(item, "values")
                videos.append((values[0], values[1]))
            shuffled_videos = shuffle_video_rows(videos)
            items_by_path = {
                self.results.item(item, "values")[0]: item
                for item in children
            }
            for position, (path, _tags) in enumerate(shuffled_videos):
                self.results.move(items_by_path[path], "", position)

            self.status.set(f"Shuffled {len(shuffled_videos)} search result(s).")

        def request_tags(self) -> list[str] | None:
            value = simpledialog.askstring(
                "Video tags",
                "Enter tags separated by commas:",
                parent=self.root,
            )
            if value is None:
                return None
            tags = [tag.strip() for tag in value.split(",") if tag.strip()]
            if not tags:
                messagebox.showwarning(
                    "No tags entered", "Enter one or more comma-separated tags."
                )
                return None
            return tags

        def save_tags(self, path: str, tags: list[str]) -> bool:
            try:
                initialize_database()
                add_video_tags(DB_PATH, path, tags)
            except (OSError, sqlite3.Error, RuntimeError, ValueError) as error:
                messagebox.showerror("Could not save tags", str(error))
                return False
            self.refresh()
            return True

        def add_tags_to_current(self) -> None:
            if not self.current_video:
                messagebox.showinfo(
                    "Choose a video", "Choose or select a video before adding tags."
                )
                return
            tags = self.get_entered_tags()
            if tags and self.save_tags(self.current_video, tags):
                self.new_tags.set("")

        def get_entered_tags(self) -> list[str] | None:
            tags = [tag.strip() for tag in self.new_tags.get().split(",") if tag.strip()]
            if not tags:
                messagebox.showwarning(
                    "No tags entered", "Enter one or more comma-separated tags."
                )
                return None
            return tags

        def add_tags_to_selected(self) -> None:
            selection = self.results.selection()
            if not selection:
                messagebox.showinfo(
                    "Select videos", "Select one or more videos from the list first."
                )
                return
            tags = self.get_entered_tags()
            if not tags:
                return
            paths = [self.results.item(item, "values")[0] for item in selection]
            try:
                initialize_database()
                add_tags_to_videos(DB_PATH, paths, tags)
            except (OSError, sqlite3.Error, RuntimeError, ValueError) as error:
                messagebox.showerror("Could not save tags", str(error))
                return
            self.new_tags.set("")
            self.refresh()
            self.status.set(f"Added tags to {len(paths)} video(s).")

        def clear_video_selection(self) -> None:
            selection = self.results.selection()
            if selection:
                self.results.selection_remove(*selection)
            self.status.set("Video selection cleared.")

        def remove_selected_videos(self) -> None:
            selection = self.results.selection()
            if not selection:
                messagebox.showinfo(
                    "Select videos",
                    "Select one or more videos to remove from the library.",
                )
                return

            paths = [self.results.item(item, "values")[0] for item in selection]
            confirmed = messagebox.askyesno(
                "Remove videos from library",
                f"Remove {len(paths)} selected video(s) and their tags from the "
                "library?\n\nThe video files on disk will not be deleted.",
                icon="warning",
            )
            if not confirmed:
                return

            try:
                initialize_database()
                remove_videos_from_library(DB_PATH, paths)
            except (OSError, sqlite3.Error, RuntimeError) as error:
                messagebox.showerror("Could not remove videos", str(error))
                return

            if self.current_video in paths:
                self.current_video = None
                self.current_video_label.set("Current video: none")
            self.refresh()
            self.status.set(
                f"Removed {len(paths)} video(s) from the library. Files were kept."
            )

        def remove_tags_from_paths(self, paths: list[str]) -> None:
            tags = self.get_entered_tags()
            if not tags:
                return
            try:
                initialize_database()
                removed_count = remove_video_tags(DB_PATH, paths, tags)
            except (OSError, sqlite3.Error, RuntimeError, ValueError) as error:
                messagebox.showerror("Could not remove tags", str(error))
                return

            self.new_tags.set("")
            self.refresh()
            self.status.set(
                f"Removed {removed_count} tag assignment(s) from "
                f"{len(paths)} video(s)."
            )

        def remove_tags_from_selected(self) -> None:
            selection = self.results.selection()
            if not selection:
                messagebox.showinfo(
                    "Select videos",
                    "Select one or more videos to remove tags from.",
                )
                return
            paths = [self.results.item(item, "values")[0] for item in selection]
            self.remove_tags_from_paths(paths)

        def remove_tags_from_current(self) -> None:
            if not self.current_video:
                messagebox.showinfo(
                    "Choose a video", "Choose or select a video first."
                )
                return
            self.remove_tags_from_paths([self.current_video])

        def choose_video_files(self, title: str) -> list[str]:
            try:
                initialize_database()
            except (OSError, sqlite3.Error, RuntimeError) as error:
                messagebox.showerror("Video library unavailable", str(error))
                return []

            picker = VideoPicker(
                self.root,
                title,
                self.last_video_directory,
                self.thumbnail_cache,
            )
            self.last_video_directory = Path(picker.directory.get()).expanduser()
            return picker.selected_paths

        def add_new_video(self) -> None:
            paths = self.choose_video_files("Choose videos to add to your library")
            if not paths:
                return
            try:
                initialize_database()
                self.status.set(
                    f"Adding {len(paths)} video(s). Hashing files may take a while..."
                )
                self.root.update_idletasks()
                registered_paths = register_videos(DB_PATH, list(paths))
            except (OSError, sqlite3.Error, RuntimeError, ValueError) as error:
                messagebox.showerror("Could not add videos", str(error))
                return
            self.show_imported_videos(registered_paths)

        def choose_video_to_play(self) -> None:
            paths = self.choose_video_files("Choose videos to play")
            if not paths:
                return
            try:
                initialize_database()
                self.status.set(
                    f"Preparing {len(paths)} video(s). "
                    "Hashing files may take a while..."
                )
                self.root.update_idletasks()
                registered_paths = register_videos(DB_PATH, list(paths))
            except (OSError, sqlite3.Error, RuntimeError) as error:
                messagebox.showerror("Could not open video", str(error))
                return
            if len(registered_paths) == 1:
                self.set_current_video(registered_paths[0])
            self.refresh()
            self.play_videos(registered_paths)

        def play_selected(self) -> None:
            selection = self.results.selection()
            if not selection:
                messagebox.showinfo("Select a video", "Select a video first.")
                return
            path = self.results.item(selection[0], "values")[0]
            self.set_current_video(path)
            self.play_videos([path])

        def play_search_results(self) -> None:
            items = self.results.get_children()
            if not items:
                messagebox.showinfo(
                    "No search results", "There are no videos to add to a playlist."
                )
                return
            paths = [self.results.item(item, "values")[0] for item in items]
            self.play_videos(paths)

        def start_vlc_playlist_interface(
            self, label: str, initial_paths: list[str]
        ) -> tuple[bool, int, str]:
            port = VLC_HTTP_PORTS[label]
            if self._vlc_http_password is None:
                self._vlc_http_password = get_vlc_http_password()
            password = self._vlc_http_password
            try:
                vlc_playlist_paths(port, password)
                return False, port, password
            except ConnectionError:
                pass

            command = self.player_by_label[label]
            if command is None:
                raise ValueError(f"{label} is not configured.")
            player_args = [
                "--no-one-instance",
                "--extraintf=http",
                "--http-host=127.0.0.1",
                f"--http-port={port}",
                f"--http-password={password}",
            ]
            argv = build_player_argv(
                command,
                unique_media_paths(initial_paths),
                player_args,
            )
            process = subprocess.Popen(argv, start_new_session=True)
            self.player_processes[label] = process

            deadline = time.monotonic() + 15
            while time.monotonic() < deadline:
                if process.poll() is not None:
                    raise RuntimeError(f"{label} exited before its playlist was ready.")
                try:
                    vlc_playlist_paths(port, password)
                    return True, port, password
                except ConnectionError:
                    time.sleep(0.1)
            raise RuntimeError(f"{label} did not start its playlist interface.")

        def start_mpv_playlist_interface(
            self, initial_paths: list[str]
        ) -> tuple[bool, Path]:
            if self._mpv_socket_path is None:
                self._mpv_socket_path = mpv_ipc_path()
            socket_path = self._mpv_socket_path
            try:
                mpv_playlist_paths(socket_path)
                return False, socket_path
            except (ConnectionError, OSError):
                socket_path.unlink(missing_ok=True)

            command = self.player_by_label["MPV"]
            if command is None:
                raise ValueError("MPV is not configured.")
            player_args = [
                "--idle=yes",
                f"--input-ipc-server={socket_path}",
            ]
            argv = build_player_argv(
                command,
                unique_media_paths(initial_paths),
                player_args,
            )
            process = subprocess.Popen(argv, start_new_session=True)
            self.player_processes["MPV"] = process

            deadline = time.monotonic() + 15
            while time.monotonic() < deadline:
                if process.poll() is not None:
                    raise RuntimeError("MPV exited before its playlist was ready.")
                try:
                    mpv_playlist_paths(socket_path)
                    return True, socket_path
                except (ConnectionError, OSError):
                    time.sleep(0.1)
            raise RuntimeError("MPV did not start its playlist interface.")

        def add_results_to_playlist(self) -> None:
            items = self.results.get_children()
            if not items:
                messagebox.showinfo(
                    "No search results",
                    "Search for videos before adding results to a playlist.",
                )
                return

            paths = [self.results.item(item, "values")[0] for item in items]
            label = self.player_var.get()
            try:
                if label == "MPV":
                    socket_path = self._mpv_socket_path or mpv_ipc_path()
                    added, skipped = append_paths_to_mpv_playlist(
                        socket_path, paths
                    )
                elif label in VLC_HTTP_PORTS:
                    port = VLC_HTTP_PORTS[label]
                    password = (
                        self._vlc_http_password or read_vlc_http_password()
                    )
                    if password is None:
                        added, skipped = append_paths_to_vlc_mpris_playlist(paths)
                    else:
                        try:
                            added, skipped = append_paths_to_vlc_playlist(
                                port, password, paths
                            )
                        except (ConnectionError, RuntimeError):
                            added, skipped = append_paths_to_vlc_mpris_playlist(
                                paths
                            )
                else:
                    raise ValueError(
                        "Adding results to a running playlist is supported for "
                        "VLC and MPV. Choose one of them in Media player."
                    )
            except (OSError, RuntimeError, ValueError, subprocess.SubprocessError) as error:
                messagebox.showerror("Could not add results to playlist", str(error))
                return

            self.status.set(
                f"Added {added} video(s) to {label}; skipped {skipped} "
                "duplicate video(s)."
            )

        def play_videos(self, paths: list[str]) -> None:
            if len(paths) == 1:
                self.set_current_video(paths[0])
            label = self.player_var.get()
            command = self.player_by_label.get(label)
            try:
                if label == "MPV":
                    started, socket_path = self.start_mpv_playlist_interface(paths)
                    if not started:
                        replace_mpv_playlist(socket_path, paths)
                elif label in VLC_HTTP_PORTS:
                    started, port, password = self.start_vlc_playlist_interface(
                        label, paths
                    )
                    if not started:
                        replace_vlc_playlist(port, password, paths)
                else:
                    argv = build_player_argv(command, paths)
                    subprocess.Popen(argv, start_new_session=True)
            except (
                OSError,
                RuntimeError,
                ValueError,
                subprocess.SubprocessError,
            ) as error:
                messagebox.showerror("Could not start media player", str(error))
                return
            if len(paths) == 1:
                self.status.set(
                    f"Playing {Path(paths[0]).name} — you can add tags here while it plays."
                )
            else:
                self.status.set(
                    f"Sent {len(paths)} videos to {self.player_var.get()} as a playlist."
                )

        def open_selected(self) -> None:
            self.play_selected()

    root = tk.Tk()
    VideoTagsApp(root)
    root.mainloop()


if __name__ == "__main__":
    main()
