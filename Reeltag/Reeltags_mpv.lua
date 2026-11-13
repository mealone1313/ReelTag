local utils = require "mp.utils"

local prompt_active = false
local entered_tags = ""
local input_bindings = {
    "reeltag-input",
    "reeltag-enter",
    "reeltag-kp-enter",
    "reeltag-backspace",
    "reeltag-delete",
    "reeltag-escape",
}

local request_script = [[
import socket
import sys
from pathlib import Path
from urllib.parse import quote

source = sys.argv[1]
if source.startswith("file://"):
    uri = source
else:
    path = Path(source).expanduser()
    if not path.is_absolute():
        path = Path(sys.argv[2]) / path
    uri = path.resolve().as_uri()

request = "\t".join(quote(value, safe="") for value in (uri, sys.argv[3])) + "\n"
with socket.create_connection(("127.0.0.1", 47653), timeout=5) as connection:
    connection.sendall(request.encode("ascii"))
    response = connection.makefile("rb").readline(4097).decode("utf-8", "replace")
sys.stdout.write(response)
if response != "OK\n":
    sys.exit(1)
]]

local function display_prompt(message)
    if message then
        mp.osd_message(message, 2)
        mp.add_timeout(2, function()
            if prompt_active then
                display_prompt()
            end
        end)
        return
    end
    mp.osd_message("Reeltag tags (comma-separated): " .. entered_tags .. "_")
end

local function stop_prompt()
    prompt_active = false
    for _, name in ipairs(input_bindings) do
        mp.remove_key_binding(name)
    end
end

local function save_tags()
    local path = mp.get_property("path")
    if not path then
        stop_prompt()
        mp.osd_message("Play a video in mpv first.", 3)
        return
    end
    if path:match("^%a[%w+%.%-]*://") and not path:match("^file://") then
        stop_prompt()
        mp.osd_message("Only local video files can be tagged.", 3)
        return
    end
    if entered_tags:gsub("[,%s]", "") == "" then
        display_prompt("Enter one or more tags separated by commas.")
        return
    end

    local tags = entered_tags
    stop_prompt()
    local success, result = pcall(utils.subprocess, {
        args = {
            "python3",
            "-c",
            request_script,
            path,
            mp.get_property("working-directory") or ".",
            tags,
        },
        capture_stdout = true,
        capture_stderr = true,
        playback_only = false,
        cancellable = false,
    })
    if not success or type(result) ~= "table" then
        mp.osd_message(
            "Could not start the Reeltag tag request: " .. tostring(result),
            5
        )
        return
    end
    local response = result.stdout or ""
    local error_message = response:match("^ERROR\t([^\n]*)")
    if result.status == 0 and response == "OK\n" then
        mp.osd_message("Tags saved.", 3)
    elseif error_message then
        mp.osd_message("Reeltag: " .. error_message, 5)
    else
        mp.osd_message(
            "Could not save tags. Keep Reeltag open and check that its tag bridge is available.",
            5
        )
    end
end

local function remove_last_character()
    local position = #entered_tags
    while position > 0 do
        local byte = entered_tags:byte(position)
        if byte < 128 or byte >= 192 then
            break
        end
        position = position - 1
    end
    entered_tags = entered_tags:sub(1, position - 1)
    display_prompt()
end

local function handle_text_key(event)
    if event.event ~= "press" and event.event ~= "repeat" then
        return
    end
    local key = event.key_name
    if key == "SPACE" then
        key = " "
    end
    if key and (#key == 1 or (key:byte(1) and key:byte(1) >= 128)) then
        if #entered_tags + #key <= 512 then
            entered_tags = entered_tags .. key
            display_prompt()
        end
    end
end

local function start_prompt()
    local path = mp.get_property("path")
    if not path then
        mp.osd_message("Play a video in mpv first.", 3)
        return
    end
    if prompt_active then
        return
    end

    prompt_active = true
    entered_tags = ""
    mp.add_forced_key_binding(
        "any_unicode",
        "reeltag-input",
        handle_text_key,
        { complex = true, repeatable = true }
    )
    mp.add_forced_key_binding("ENTER", "reeltag-enter", save_tags)
    mp.add_forced_key_binding("KP_ENTER", "reeltag-kp-enter", save_tags)
    mp.add_forced_key_binding(
        "BS", "reeltag-backspace", remove_last_character
    )
    mp.add_forced_key_binding(
        "DEL", "reeltag-delete", remove_last_character
    )
    mp.add_forced_key_binding("ESC", "reeltag-escape", function()
        stop_prompt()
        mp.osd_message("Tagging cancelled.", 2)
    end)
    display_prompt()
end

mp.add_key_binding("Ctrl+Shift+t", "reeltag-tag-current", start_prompt)
