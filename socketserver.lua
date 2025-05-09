-- socketserver.lua  ── TCP control of mGBA + CAP screenshot + READRANGE command
-- Directions : U D L R          • Triggers : LT RT
-- Start / Sel: S (START)  s (SELECT)
-- Extra      : CAP  ➜ send ARGB raster (length header + pixels)
--           : READRANGE <address> <length>  ➜ send memory bytes (length header + data)
--           : LOADSTATE <slot> [flags] ➜ load save state (flags default to 29)
--           : INPUT_DISPLAY_ON ➜ control input display visibility
-- Copy to …/mGBA.app/Contents/Resources/scripts/   Run with:
--     mGBA --script socketserver.lua <rom>
-- modified from https://github.com/mgba-emu/mgba/blob/master/res/scripts/socketserver.lua
--------------------------------------------------------------------------
--  CONFIG -----------------------------------------------------------------
--------------------------------------------------------------------------
local LISTEN_PORT   = 8888   -- TCP port for Python client
local HOLD_FRAMES   = 6      -- frames to keep any pressed key down
local QUEUE_SPACING = 30     -- frames between queued inputs

--------------------------------------------------------------------------
--  mGBA GLOBALS -----------------------------------------------------------
--------------------------------------------------------------------------
local socket, console, emu = socket, console, emu   -- mGBA globals
local string_pack = string.pack                     -- Lua ≥5.3
-- Globals needed for the input display feature later in the script
local C, canvas, image, util, callbacks = C, canvas, image, util, callbacks

--------------------------------------------------------------------------
--  KEY MAP & ALIASES ------------------------------------------------------
--------------------------------------------------------------------------
local KEY_INDEX = { A=0, B=1, SELECT=2, START=3, RIGHT=4, LEFT=5, UP=6, DOWN=7, R=8, L=9 }
local KEY_ALIAS = {
   U="UP", u="UP",  D="DOWN", d="DOWN",  L="LEFT", l="LEFT",
   R="RIGHT", r="RIGHT", LT="L", lt="L", RT="R", rt="R", -- Fixed LT/RT alias to L/R GBA buttons
   S="START", s="SELECT",
}
local KEY_MASK = {}
for k,i in pairs(KEY_INDEX) do
   KEY_MASK[k] = 1 << i
end

--------------------------------------------------------------------------
--  UTILITY: TABLE TO STRING (FOR DEBUGGING) ----------------------------
--------------------------------------------------------------------------
local function table_to_string(tbl)
    if not tbl then return "nil" end
    local parts = {}
    for k, v in pairs(tbl) do
        parts[#parts + 1] = tostring(k) .. "=" .. tostring(v)
    end
    if #parts == 0 then return "{}" end
    return "{ " .. table.concat(parts, ", ") .. " }"
end

--------------------------------------------------------------------------
--  INPUT DISPLAY CONTROL STATE & FUNCTION -------------------------------
--------------------------------------------------------------------------
local g_input_display_is_visible = false -- Default: Input display is OFF

local function setInputDisplayVisibility(visible)
    if g_input_display_is_visible == visible then
        if visible and input_display and input_display.state and input_display.state.overlay then
             console:log("[INFO] Input Display is already ON.")
             return
        elseif not visible then
             console:log("[INFO] Input Display is already OFF.")
             return
        end
    end

    g_input_display_is_visible = visible

    if not input_display or not input_display.state then
        console:error("[ERROR] setInputDisplayVisibility: input_display or input_display.state is nil! Cannot proceed.")
        g_input_display_is_visible = not visible
        return
    end

    if g_input_display_is_visible then -- Turning ON
        console:log("[INFO] Input Display: Turning ON.")
        if input_display.state.reset then
            input_display.state.reset()
        else
            console:error("[ERROR] setInputDisplayVisibility: input_display.state.reset is nil when turning ON!")
            g_input_display_is_visible = false 
        end
    else -- Turning OFF
        console:log("[INFO] Input Display: Turning OFF.")
        if input_display.state.overlay then
            if input_display.state.painter then
                console:log("[DEBUG] Input Display: Clearing painter and updating overlay.")
                input_display.state.painter:clear(0x00000000) 
                input_display.state.overlay:update()
            else
                console:log("[WARN] Input Display: Painter is nil when trying to turn OFF, but overlay exists.")
            end
        else
            console:log("[DEBUG] Input Display: Overlay is nil when trying to turn OFF. Nothing to clear visually.")
        end
    end
end

--------------------------------------------------------------------------
--  UTILITY: CHECK INTERFACE STATES (GEN1) -------------------------------
-- menu: CC51–CC52, battle: CCD5, conversation: CC00
--------------------------------------------------------------------------
local function isInMenu()
   local b = emu:readRange(0xCC51, 2)
   if not b then return false end
   local hi, lo = string.byte(b,1), string.byte(b,2)
   local result = (hi ~= 0 or lo ~= 0)
   return result
end

local function isInBattle()
   local flag = emu:readRange(0xD057, 1)
   local result = flag and string.byte(flag, 1) ~= 0
   return result
end

local function isInDialogue()
   local ptr = emu:readRange(0xCC50, 1)
   local flg = emu:readRange(0xCC54, 1)
   if not ptr or not flg then return false end
   local ptr_val, flg_val = string.byte(ptr,1), string.byte(flg,1)
   local result = (ptr_val == 0x5F and flg_val ~= 0x30)
   return result
end

local function getState()
   console:log("[DEBUG] getState: Checking state...")
   local state
   if     isInBattle()       then state = "battle"
   elseif isInMenu()         then state = "menu"
   elseif isInDialogue() then state = "dialogue"
   else                      state = "roam"
   end
   console:log("[DEBUG] getState: Determined state = " .. state)
   return state
end

--------------------------------------------------------------------------
--  SOCKET HOUSEKEEPING ----------------------------------------------------
--------------------------------------------------------------------------
local server, clients, nextID = nil, {}, 1
local function log(id,m)   console:log  ("[INFO ] Socket "..id.." "..m) end
local function err(id,m)   console:error("[ERROR] Socket "..id.." ERROR: "..m) end
local function stop(id)
   if clients[id] then
      log(id, "closing connection.")
      clients[id]:close();
      clients[id]=nil
   else
       console:log("[DEBUG] stop: Attempted to stop non-existent client ID " .. id)
   end
end

--------------------------------------------------------------------------
--  INPUT QUEUE STATE ------------------------------------------------------
--------------------------------------------------------------------------
local inputQueue = nil

--------------------------------------------------------------------------
--  4-FRAME AUTO-RELEASE + QUEUE PROCESSING -------------------------------
--------------------------------------------------------------------------
local hold = {}
local function stepAutoRelease()
   if next(hold) then
      local rel = 0
      for k,t in pairs(hold) do
         t = t - 1
         if t <= 0 then
            console:log("[DEBUG] stepAutoRelease: Time expired for key '" .. k .. "'. Scheduling release.")
            rel = rel | KEY_MASK[k]
            hold[k] = nil
         else
            hold[k] = t
         end
      end
      if rel ~= 0 then
         console:log("[DEBUG] stepAutoRelease: Releasing keys with mask: " .. rel .. ". New hold: " .. table_to_string(hold))
         emu:clearKeys(rel)
      end
   end

   if inputQueue then
      inputQueue.framesUntilNext = inputQueue.framesUntilNext - 1
      if inputQueue.framesUntilNext <= 0 then
         console:log("[DEBUG] stepAutoRelease: Queue ready for next input (framesUntilNext <= 0).")
         local i = inputQueue.idx
         if i <= #inputQueue.tokens then
            local key = inputQueue.tokens[i]
            console:log("[DEBUG] stepAutoRelease: Queue executing index " .. i .. ", token: '" .. key .. "' (Mask: " .. KEY_MASK[key] .. ")")
            emu:addKeys(KEY_MASK[key])
            hold[key] = HOLD_FRAMES
            console:log("[DEBUG] stepAutoRelease: Added key '" .. key .. "' to hold for " .. HOLD_FRAMES .. " frames. New hold: " .. table_to_string(hold))
            inputQueue.idx = i + 1
            inputQueue.framesUntilNext = QUEUE_SPACING
            console:log("[DEBUG] stepAutoRelease: Queue index advanced to " .. inputQueue.idx .. ". Next input in " .. inputQueue.framesUntilNext .. " frames.")
         else
            console:log("[DEBUG] stepAutoRelease: Input queue finished processing all tokens.")
            local sock = inputQueue.sock
            if sock and clients[inputQueue.sockId] then
               sock:send("QUEUE_COMPLETE\n")
            else
               console:log("[DEBUG] stepAutoRelease: Queue finished, but client socket " .. (inputQueue.sockId or "??") .. " is no longer valid. Cannot send QUEUE_COMPLETE.")
            end
            inputQueue = nil
            console:log("[DEBUG] stepAutoRelease: inputQueue set to nil.")
         end
      end
   end
end
callbacks:add("frame", stepAutoRelease)

--------------------------------------------------------------------------
--  CAPTURE ----------------------------------------------------------------
--------------------------------------------------------------------------
local function sendCapture(sock, sockId)
   console:log("[DEBUG] sendCapture: Socket " .. sockId .. " requested CAP.")
   local img = emu:screenshotToImage()
   if not img then
      err(sockId, "emu:screenshotToImage failed.")
      sock:send("ERR no image\n");
      return
   end
   local w,h = img.width, img.height
   console:log("[DEBUG] sendCapture: Captured image " .. w .. "x" .. h)
   local buf = {}
   for y=0,h-1 do
      for x=0,w-1 do
         buf[#buf+1] = string_pack(">I4", img:getPixel(x,y))
      end
   end
   local data = table.concat(buf)
   local len_packed = string_pack(">I4", #data)
   console:log("[DEBUG] sendCapture: Sending image data (" .. #data .. " bytes) to socket " .. sockId)
   sock:send(len_packed)
   sock:send(data)
   console:log("[DEBUG] sendCapture: Image data sent.")
end

--------------------------------------------------------------------------
--  READRANGE --------------------------------------------------------------
--------------------------------------------------------------------------
local function sendReadRange(sock, sockId, addr_str, len_str)
   console:log("[DEBUG] sendReadRange: Socket " .. sockId .. " requested READRANGE " .. addr_str .. " " .. len_str)
   local addr   = tonumber(addr_str) or tonumber(addr_str, 16)
   local length = tonumber(len_str)  or tonumber(len_str, 16)
   if not addr or not length or length < 1 then
      err(sockId, "Bad arguments for READRANGE: addr=" .. tostring(addr) .. ", len=" .. tostring(length))
      sock:send("ERR bad args\n")
      return
   end
   console:log("[DEBUG] sendReadRange: Reading " .. length .. " bytes from address 0x" .. string.format("%X", addr))
   local data = emu:readRange(addr, length)
   if not data then
      err(sockId, "emu:readRange failed for addr=0x" .. string.format("%X", addr) .. ", len=" .. length)
      sock:send("ERR read failed\n");
      return
   end
   local len_packed = string_pack(">I4", #data)
   console:log("[DEBUG] sendReadRange: Sending memory data (" .. #data .. " bytes) to socket " .. sockId)
   sock:send(len_packed)
   sock:send(data)
   console:log("[DEBUG] sendReadRange: Memory data sent.")
end

--------------------------------------------------------------------------
--  COMMAND PARSER ---------------------------------------------------------
--------------------------------------------------------------------------
local function canonical(tok)
   return KEY_ALIAS[tok] or KEY_ALIAS[tok:upper()] or tok:upper()
end

local function parse(line, sock, sockId)
   console:log("[DEBUG] parse: Socket " .. sockId .. " received line: '" .. line .. "'")
   line = line:match("^(.-)%s*$")
   if line == "" then
       console:log("[DEBUG] parse: Line is empty after trimming.")
       return
   end

   local line_upper = line:upper()

   if line_upper == "STATE" then
      console:log("[DEBUG] parse: STATE command received.")
      local state_val = getState()
      console:log("[DEBUG] parse: Sending state '" .. state_val .. "' to socket " .. sockId)
      sock:send(state_val .. "\n")
      return
   end

   if line_upper == "INPUT_DISPLAY_ON" then
        console:log("[DEBUG] parse: INPUT_DISPLAY_ON command received.")
        setInputDisplayVisibility(true)
        sock:send("OK INPUT_DISPLAY_ON\n")
        return
   end

   if line:find(";") then
      console:log("[DEBUG] parse: Detected queue syntax ';'.")
      local toks = {}
      for tok in line:gmatch("([^;]+)") do
         tok = tok:match("^%s*(.-)%s*$")
         if tok ~= "" then
            local ctok = canonical(tok)
            if not KEY_MASK[ctok] then
               return nil, "Unknown key '" .. tok .. "' (canonical: '" .. ctok .. "') in queue"
            end
            toks[#toks+1] = ctok
            console:log("[DEBUG] parse: Added token '" .. ctok .. "' to queue.")
         end
      end
      if #toks > 0 then
         console:log("[DEBUG] parse: Setting up input queue with " .. #toks .. " tokens.")
         inputQueue = {
            tokens          = toks,
            idx             = 1,
            framesUntilNext = 0,
            sock            = sock,
            sockId          = sockId,
         }
         console:log("[DEBUG] parse: Input queue created: " .. table_to_string(inputQueue))
         return
      else
         console:log("[DEBUG] parse: Queue syntax found, but no valid tokens extracted.")
         return nil, "Queue command contained no valid tokens."
      end
   end

   local a,l = line:match("^READRANGE%s+(%S+)%s+(%S+)$")
   if a and l then
      console:log("[DEBUG] parse: READRANGE command received.")
      sendReadRange(sock, sockId, a, l)
      return
   end
   if line_upper == "CAP" then
      console:log("[DEBUG] parse: CAP command received.")
      sendCapture(sock, sockId)
      return
   end

   local slot_str, flags_str = line:match("^[Ll][Oo][Aa][Dd][Ss][Tt][Aa][Tt][Ee]%s+(%S+)%s*(%S*)$")
   if slot_str then
      console:log("[DEBUG] parse: LOADSTATE command received with slot: '" .. slot_str .. "'" .. ((flags_str and flags_str ~= "") and (" flags: '" .. flags_str .. "'") or " (default flags)"))
      local slot = tonumber(slot_str)
      local flags = 29
      if not slot then 
         local err_msg = "Invalid slot number for LOADSTATE: '" .. slot_str .. "'"
         err(sockId, err_msg); sock:send("ERR " .. err_msg .. "\n"); return
      end
      if slot < 0 then
          local err_msg = "Slot number for LOADSTATE must be non-negative: " .. slot
          err(sockId, err_msg); sock:send("ERR " .. err_msg .. "\n"); return
      end
      if math.floor(slot) ~= slot then
          local err_msg = "Slot number for LOADSTATE must be an integer: '" .. slot_str .. "'"
          err(sockId, err_msg); sock:send("ERR " .. err_msg .. "\n"); return
      end
      if flags_str and flags_str ~= "" then
         local parsed_flags = tonumber(flags_str)
         if not parsed_flags then
            local err_msg = "Invalid flags value for LOADSTATE: '" .. flags_str .. "'"
            err(sockId, err_msg); sock:send("ERR " .. err_msg .. "\n"); return
         end
         if math.floor(parsed_flags) ~= parsed_flags then
            local err_msg = "Flags for LOADSTATE must be an integer: '" .. flags_str .. "'"
            err(sockId, err_msg); sock:send("ERR " .. err_msg .. "\n"); return
         end
         flags = parsed_flags
      end
      console:log("[DEBUG] parse: Calling emu:loadStateSlot(" .. slot .. ", " .. flags .. ")")
      local success = emu:loadStateSlot(slot, flags)
      if success then
         console:log("[INFO ] parse: LOADSTATE successful for slot " .. slot .. " with flags " .. flags)
         sock:send("OK LOADSTATE slot " .. slot .. "\n")
         hold = {}
         console:log("[DEBUG] parse: Clearing hold table due to successful LOADSTATE.")
      else
         local err_msg = "emu:loadStateSlot failed for slot " .. slot .. " (flags " .. flags .. ")"
         err(sockId, err_msg); sock:send("ERR " .. err_msg .. "\n")
      end
      return
   end

   local num_str = line:match("^SET%s+(%S+)$")
   if num_str then
      console:log("[DEBUG] parse: SET command received with value: " .. num_str)
      local m = tonumber(num_str) or tonumber(num_str,16)
      if not m then return nil, "Bad number for SET: "..num_str end
      console:log("[DEBUG] parse: Setting keys directly to mask: " .. m)
      emu:setKeys(m)
      console:log("[DEBUG] parse: Clearing hold table due to SET command.")
      hold = {}
      return
   end

   console:log("[DEBUG] parse: Processing as individual key command(s).")
   local add, clr = 0, 0
   for tok in line:gmatch("%S+") do
      console:log("[DEBUG] parse: Processing token: '" .. tok .. "'")
      local op,name = tok:match("^([%+%-]?)(.+)$")
      name = canonical(name)
      if not KEY_MASK[name] then
         console:log("[DEBUG] parse: Unknown key name: '" .. name .. "' from token '" .. tok .. "'")
         return nil, "Unknown key "..tok
      end
      if op == "-" then
         console:log("[DEBUG] parse: Clearing key '" .. name .. "' (Mask: " .. KEY_MASK[name] .. ")")
         clr = clr | KEY_MASK[name]
         if hold[name] then
            console:log("[DEBUG] parse: Removing key '" .. name .. "' from hold table.")
            hold[name] = nil
         end
      else
         console:log("[DEBUG] parse: Adding key '" .. name .. "' (Mask: " .. KEY_MASK[name] .. ")")
         add = add | KEY_MASK[name]
         console:log("[DEBUG] parse: Adding key '" .. name .. "' to hold table for " .. HOLD_FRAMES .. " frames.")
         hold[name] = HOLD_FRAMES
      end
   end

   if add ~= 0 then
      console:log("[DEBUG] parse: Applying add mask: " .. add)
      emu:addKeys(add)
   end
   if clr ~= 0 then
      console:log("[DEBUG] parse: Applying clear mask: " .. clr)
      emu:clearKeys(clr)
   end
   console:log("[DEBUG] parse: Finished processing keys. Current hold: " .. table_to_string(hold))
   return
end

--------------------------------------------------------------------------
--  SOCKET CALLBACKS -------------------------------------------------------
--------------------------------------------------------------------------
local function onRecv(id)
   local s = clients[id]
   if not s then
       console:log("[DEBUG] onRecv: Called for non-existent client ID " .. id)
       return
   end
   console:log("[DEBUG] onRecv: Checking for data from socket " .. id)
   while true do
      local chunk, err_msg = s:receive(4096)
      if not chunk then
         if err_msg ~= socket.ERRORS.AGAIN then
            err(id, "Receive error: " .. tostring(err_msg))
            stop(id)
         end
         return
      end
      for line in chunk:gmatch("[^\r\n]+") do
         console:log("[DEBUG] onRecv: Received " .. #line .. " bytes from socket " .. id .. ": '" .. line .. "'")
         local ok, perr = pcall(parse, line, s, id)
         if not ok then
            err(id, "parse internal exception: " .. tostring(perr))
            pcall(s.send, s, "ERR parse internal exception\n")
         elseif perr then
            err(id, "Parse error: " .. perr)
            pcall(s.send, s, "ERR " .. perr .. "\n")
         end
      end
   end
end

local function onError(id, e)
   err(id, "Socket error event: " .. tostring(e))
   stop(id)
end

--------------------------------------------------------------------------
--  ACCEPT NEW CLIENTS -----------------------------------------------------
--------------------------------------------------------------------------
local function onAccept()
   console:log("[DEBUG] onAccept: Checking for new connection...")
   local s,e = server:accept()
   if not s then
       if e and e ~= socket.ERRORS.AGAIN then
           err("accept", "Failed to accept new connection: " .. tostring(e))
       end
       return
   end
   local id = nextID; nextID = id + 1
   clients[id] = s
   s:add("received", function() onRecv(id) end)
   s:add("error",    function(errMsg) onError(id, errMsg) end)
   log(id, "connected")
end

--------------------------------------------------------------------------
--  START LISTENING --------------------------------------------------------
--------------------------------------------------------------------------
local function listen(port)
   console:log("[DEBUG] listen: Attempting to bind server...")
   while true do
      server, bind_err = socket.bind(nil, port)
      if not server then
         if bind_err == socket.ERRORS.ADDRESS_IN_USE then
            console:log("[INFO ] listen: Port " .. port .. " in use, trying next...")
            port = port + 1
         else
            err("bind", "Failed to bind to any port: " .. tostring(bind_err))
            return
         end
      else
         local ok, listen_err = server:listen()
         if ok then
             console:log("[INFO ] listen: Server socket created.")
             break
         else
             err("listen", "Failed to listen on port " .. port .. ": " .. tostring(listen_err))
             server:close(); server = nil; return
         end
      end
   end
   console:log("[INFO ] Lua socket server listening on port "..port)
   server:add("received", onAccept)
   console:log("[DEBUG] listen: Added accept handler. Ready for connections.")
end


-- From https://github.com/mgba-emu/mgba/blob/master/res/scripts/input-display.lua
-- Modified for toggleable visibility.
input_display = {
	anchor = "topLeft",
	offset = { x = 0, y = 0 }
}

local state = {}
state.overlay = nil
state.painter = nil
state.drawButton = {
	[0] = function(st) st.painter:drawCircle(27, 6, 4) end,
	[1] = function(st) st.painter:drawCircle(23, 8, 4) end,
	[2] = function(st) st.painter:drawCircle(13, 11, 3) end,
	[3] = function(st) st.painter:drawCircle(18, 11, 3) end,
	[4] = function(st) st.painter:drawRectangle(9, 7, 4, 3) end,
	[5] = function(st) st.painter:drawRectangle(2, 7, 4, 3) end,
	[6] = function(st) st.painter:drawRectangle(6, 3, 3, 4) end,
	[7] = function(st) st.painter:drawRectangle(6, 10, 3, 4) end,
	[8] = function(st) st.painter:drawRectangle(28, 0, 4, 3) end,
	[9] = function(st) st.painter:drawRectangle(0, 0, 4, 3) end
}
state.maxKey = {
	[C.PLATFORM.GBA] = 9,
	[C.PLATFORM.GB] = 7,
}

function state.create()
	if state.overlay ~= nil then return true end
	if canvas == nil then
		console:error("[InputDisplay:Create] 'canvas' global is nil!")
		return false
	end
	state.overlay = canvas:newLayer(32, 16)
	if state.overlay == nil then
		console:error("[InputDisplay:Create] Failed to create overlay layer.")
		return false
	end
	state.painter = image.newPainter(state.overlay.image)
    if state.painter == nil then
        console:error("[InputDisplay:Create] Failed to create painter.")
        state.overlay:delete()
        state.overlay = nil
        return false
    end
	state.painter:setBlend(false)
	state.painter:setFill(true)
    console:log("[DEBUG] InputDisplay: Created overlay and painter.")
	return true
end

function state.update()
    if not g_input_display_is_visible then
        return
    end

    if not state.overlay or not state.painter then 
        if not state.create() then return end
    end

    state.painter:setFillColor(0x40808080) 
    state.painter:drawRectangle(0, 0, 32, 16)

	local endX = canvas:screenWidth() - 32
	local endY = canvas:screenHeight() - 16
	local anchors = {
		topLeft = {x = 0, y = 0}, top = {x = endX / 2, y = 0}, topRight = {x = endX, y = 0},
		left = {x = 0, y = endY / 2}, center = {x = endX / 2, y = endY / 2}, right = {x = endX, y = endY / 2},
		bottomLeft = {x = 0, y = endY}, bottom = {x = endX / 2, y = endY}, bottomRight = {x = endX, y = endY},
	}
	local anchor_conf = input_display.anchor or "topLeft"
    local pos = anchors[anchor_conf] or anchors.topLeft
	pos = {x = pos.x + (input_display.offset.x or 0), y = pos.y + (input_display.offset.y or 0)}
	state.overlay:setPosition(pos.x, pos.y)

    local platform_id = emu:platform()
	local maxKey = state.maxKey[platform_id]
    if not maxKey then
        maxKey = state.maxKey[C.PLATFORM.GB]
    end

	for key_idx = 0, maxKey do
		if emu:getKey(key_idx) ~= 0 then
			state.painter:setFillColor(0x80FFFFFF)
		else
			state.painter:setFillColor(0x40404040)
		end
		if state.drawButton[key_idx] then
		    state.drawButton[key_idx](state)
        end
	end

	state.overlay:update()
end

function state.reset()
    if not g_input_display_is_visible then -- Turning OFF or staying OFF
        if input_display.state.overlay then -- Check if overlay exists
            if input_display.state.painter then -- Check if painter exists
                console:log("[DEBUG] InputDisplay:Reset: Clearing painter for OFF state.")
                input_display.state.painter:clear(0x00000000)
                input_display.state.overlay:update()
            else
                console:log("[WARN] InputDisplay:Reset: Painter is nil during OFF state reset, but overlay exists.")
            end
        else
            console:log("[DEBUG] InputDisplay:Reset: Overlay is nil during OFF state reset. Nothing to clear.")
        end
        return
    end

    -- If g_input_display_is_visible is true (Turning ON or staying ON)
	if not state.create() then
        console:error("[InputDisplay:Reset] Failed to create overlay/painter for ON state. Display will not be shown.")
        return
    end
    console:log("[DEBUG] InputDisplay:Reset: Drawing background for ON state.")
	state.painter:setFillColor(0x40808080)
	state.painter:drawRectangle(0, 0, 32, 16)
	state.overlay:update()
end

input_display.state = state

state.reset()
callbacks:add("frame", state.update)
callbacks:add("start", state.reset)

console:log("[INFO ] mGBA Socket Server Script Starting...")
listen(LISTEN_PORT)
console:log("[INFO ] mGBA Socket Server Script Initialized (Input Display starts OFF).")