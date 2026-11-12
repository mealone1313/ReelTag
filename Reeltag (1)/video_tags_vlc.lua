-- VLC extension for sending the playing local video and tags to Reeltag.

local dialog
local tag_input
local status_label

function descriptor()
    return {
        title = "Reeltag",
        version = "1.0",
        author = "Reeltag",
        shortdesc = "Tag the playing video",
        description = "Add searchable tags to the current video in Reeltag.",
        capabilities = { "menu" }
    }
end

function menu()
    return { "Tag current video" }
end

function trigger_menu(_id)
    activate()
end

local function encode_field(value)
    return (string.gsub(value, "([^A-Za-z0-9%-_%.~])", function(character)
        return string.format("%%%02X", string.byte(character))
    end))
end

local function save_tags()
    local item = vlc.input.item()
    if not item then
        status_label:set_text("Play a video in VLC first.")
        return
    end

    local uri = item:uri()
    if not uri or string.sub(uri, 1, 7) ~= "file://" then
        status_label:set_text("Only local video files can be tagged.")
        return
    end

    local tags = tag_input:get_text()
    if string.gsub(tags, "[,%s]", "") == "" then
        status_label:set_text("Enter one or more tags separated by commas.")
        return
    end

    local fd = vlc.net.connect_tcp("127.0.0.1", 47653)
    if not fd or fd < 0 then
        status_label:set_text("Open the Reeltag app, then try again.")
        return
    end

    local request = encode_field(uri) .. "\t" .. encode_field(tags) .. "\n"
    vlc.net.send(fd, request)
    local response = vlc.net.recv(fd, 4096)
    vlc.net.close(fd)

    if response and string.sub(response, 1, 3) == "OK\n" then
        status_label:set_text("Tags saved.")
        tag_input:set_text("")
    elseif response and string.sub(response, 1, 6) == "ERROR\t" then
        status_label:set_text(string.gsub(response, "^ERROR\t", "Error: "))
    else
        status_label:set_text("Could not save tags. Is Reeltag open?")
    end
end

function activate()
    if dialog then
        dialog:show()
        return
    end

    dialog = vlc.dialog("Tag current video")
    dialog:add_label("Tags for the video currently playing in VLC:", 1, 1, 3, 1)
    tag_input = dialog:add_text_input("", 1, 2, 3, 1)
    dialog:add_button("Save tags", save_tags, 1, 3, 1, 1)
    status_label = dialog:add_label("", 1, 4, 3, 1)
    dialog:show()
end

function deactivate()
    if dialog then
        dialog:delete()
        dialog = nil
        tag_input = nil
        status_label = nil
    end
end

function close()
    deactivate()
end
