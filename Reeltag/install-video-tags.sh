#!/usr/bin/env bash
set -euo pipefail

source_dir=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
app_dir="$HOME/.local/opt/reeltag"
desktop_dir="$HOME/.local/share/applications"
icon_dir="$HOME/.local/share/icons/hicolor/256x256/apps"
data_dir="${XDG_DATA_HOME:-$HOME/.local/share}"
config_dir="${XDG_CONFIG_HOME:-$HOME/.config}"
native_vlc_extension_installed=0
flatpak_vlc_extension_installed=0
mpv_script_installed=0

install_vlc_extension() {
  local extension_dir="$1"
  local extension_source="$source_dir/ReelTags_vlc.lua"
  mkdir -p "$extension_dir"
  if [[ ! -f "$extension_source" ]]; then
    extension_source="$source_dir/Reeltags_vlc.lua"
  fi
  if [[ ! -f "$extension_source" ]]; then
    extension_source="$source_dir/video_tags_vlc.lua"
  fi
  install -m 644 "$extension_source" "$extension_dir/reeltag.lua"
  rm -f "$extension_dir/reelag.lua" "$extension_dir/video_tags.lua"
}

if ! python3 -c 'import tkinter' >/dev/null 2>&1; then
  printf '%s\n' \
    "Tkinter is required but is not installed." \
    "On Fedora, install it with: sudo dnf install python3-tkinter" \
    "On Debian or Ubuntu, install it with: sudo apt install python3-tk"
  exit 1
fi

mkdir -p "$app_dir" "$desktop_dir" "$icon_dir"
app_source="$source_dir/ReelTag.py"
if [[ ! -f "$app_source" ]]; then
  app_source="$source_dir/video_tags_app.py"
fi
install -m 755 "$app_source" "$app_dir/reeltag.py"
install -m 644 "$source_dir/reeltag.png" "$icon_dir/reeltag.png"

if command -v vlc >/dev/null 2>&1; then
  extension_dir="$data_dir/vlc/lua/extensions"
  install_vlc_extension "$extension_dir"
  native_vlc_extension_installed=1
fi

if command -v mpv >/dev/null 2>&1; then
  script_dir="$config_dir/mpv/scripts"
  script_source="$source_dir/Reeltags_mpv.lua"
  if [[ ! -f "$script_source" ]]; then
    script_source="$source_dir/video_tags_mpv.lua"
  fi
  mkdir -p "$script_dir"
  install -m 644 "$script_source" "$script_dir/reeltag.lua"
  mpv_script_installed=1
fi

if command -v flatpak >/dev/null 2>&1 &&
  flatpak list --app --columns=application 2>/dev/null |
    grep -Fxq 'org.videolan.VLC'; then
  extension_dir="$HOME/.var/app/org.videolan.VLC/data/vlc/lua/extensions"
  install_vlc_extension "$extension_dir"
  flatpak_vlc_extension_installed=1
fi

cat >"$desktop_dir/reeltag.desktop" <<EOF
[Desktop Entry]
Type=Application
Name=Reeltag
Comment=Play videos in your chosen player and add searchable tags
Exec=python3 "$app_dir/reeltag.py"
Icon=$icon_dir/reeltag.png
Terminal=false
Categories=AudioVideo;Video;
StartupWMClass=Reeltag
EOF
chmod 644 "$desktop_dir/reeltag.desktop"
rm -f "$desktop_dir/reelag.desktop" "$desktop_dir/video-tags.desktop"

printf 'Installed. Search for "Reeltag" in your applications menu.\n'
if ! command -v ffmpeg >/dev/null 2>&1; then
  printf '%s\n' \
    'Video thumbnail previews need ffmpeg, which was not found.' \
    'On Fedora, install it with: sudo dnf install ffmpeg-free'
fi
if (( native_vlc_extension_installed )); then
  printf '%s\n' \
    'Installed the VLC extension for native VLC (including RPM packages). Restart VLC, then open Tools > Plugins and extensions > Extensions.' \
    'Keep Reeltag open while tagging; VLC sends tags over the local Reeltag bridge.'
fi
if (( flatpak_vlc_extension_installed )); then
  printf '%s\n' \
    'Installed the VLC extension for VLC Flatpak. Restart VLC, then open Tools > Plugins and extensions > Extensions.' \
    'Keep Reeltag open while tagging; VLC Flatpak needs network access to reach the local Reeltag bridge.'
fi
if (( !native_vlc_extension_installed && !flatpak_vlc_extension_installed )); then
  printf '%s\n' \
    'VLC was not detected, so its extension was not installed.' \
    'Install VLC and run this installer again to add the extension.'
fi
if (( mpv_script_installed )); then
  printf '%s\n' \
    'Installed the mpv tagging script. Restart mpv, then press Ctrl+Shift+T while a video is playing.' \
    'Keep Reeltag open while using the mpv script; it passes tags over a private localhost connection.'
else
  printf '%s\n' \
    'mpv was not detected, so its tagging script was not installed.' \
    'Install mpv and run this installer again to add the script.'
fi
