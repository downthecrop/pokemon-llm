-- socketserver.lua  ── TCP control of mGBA + CAP screenshot + READRANGE command
-- Directions : U D L R          • Triggers : LT RT
-- Start / Sel: S (START)  s (SELECT)
-- Extra      : CAP  ➜ send ARGB raster (length header + pixels)
--           : READRANGE <address> <length>  ➜ send memory bytes (length header + data)
-- Copy to …/mGBA.app/Contents/Resources/scripts/   Run with:
--     mGBA --script socketserver.lua <rom>

--------------------------------------------------------------------------
--  CONFIG -----------------------------------------------------------------
--------------------------------------------------------------------------
local LISTEN_PORT   = 8888   -- TCP port for Python client
local HOLD_FRAMES   = 4      -- frames to keep any pressed key down
local QUEUE_SPACING = 30     -- frames between queued inputs

--------------------------------------------------------------------------
--  mGBA GLOBALS -----------------------------------------------------------
--------------------------------------------------------------------------
local socket, console, emu = socket, console, emu   -- mGBA globals
local string_pack = string.pack                     -- Lua ≥5.3

--------------------------------------------------------------------------
--  KEY MAP & ALIASES ------------------------------------------------------
--------------------------------------------------------------------------
local KEY_INDEX = { A=0, B=1, SELECT=2, START=3, RIGHT=4, LEFT=5, UP=6, DOWN=7, R=8, L=9 }
local KEY_ALIAS = {
   U="UP", u="UP",  D="DOWN", d="DOWN",  L="LEFT", l="LEFT",
   R="RIGHT", r="RIGHT", LT="L", lt="L", RT="R", rt="R",
   S="START", s="SELECT",
}
local KEY_MASK = {}
for k,i in pairs(KEY_INDEX) do
   KEY_MASK[k] = 1 << i
end

--------------------------------------------------------------------------
--  UTILITY: READ PLAYER POS (GEN1) --------------------------------------
--------------------------------------------------------------------------
local function getPlayerPos()
   -- read the two one-byte tile-coord addresses
   local bx = emu:readRange(0xD362, 1)
   local by = emu:readRange(0xD361, 1)
   if not bx or not by then return nil, nil end
   return string.byte(bx, 1), string.byte(by, 1)
end

--------------------------------------------------------------------------
--  UTILITY: CHECK INTERFACE STATES (GEN1) -------------------------------
-- menu: CC51–CC52, battle: CCD5, conversation: CC00
--------------------------------------------------------------------------
local function isInMenu()
   local b = emu:readRange(0xCC51, 2)
   if not b then return false end
   local hi, lo = string.byte(b,1), string.byte(b,2)
   return (hi ~= 0 or lo ~= 0)
end

local function isInBattle()
   -- D057: non-zero whenever the battle engine is active
   local flag = emu:readRange(0xD057, 1)
   return flag and string.byte(flag, 1) ~= 0
end

-- CC50 == 0x5F  → text engine is running
-- CC54 != 0x30  → the actual textbox is on screen
local function isInDialogue()
   local ptr = emu:readRange(0xCC50, 1)
   local flg = emu:readRange(0xCC54, 1)
   if not ptr or not flg then return false end
   return string.byte(ptr,1) == 0x5F and string.byte(flg,1) ~= 0x30
end

local function getState()
   if     isInBattle()       then return "battle"
   elseif isInMenu()         then return "menu"
   elseif isInDialogue() then return "dialogue"
   else                      return "roam"
   end
end

--------------------------------------------------------------------------
--  SOCKET HOUSEKEEPING ----------------------------------------------------
--------------------------------------------------------------------------
local server, clients, nextID = nil, {}, 1
local function log(id,m)   console:log  ("Socket "..id.." "..m) end
local function err(id,m)   console:error("Socket "..id.." ERROR: "..m) end
local function stop(id)    if clients[id] then clients[id]:close(); clients[id]=nil end end

--------------------------------------------------------------------------
--  INPUT QUEUE STATE ------------------------------------------------------
--------------------------------------------------------------------------
local inputQueue = nil

--------------------------------------------------------------------------
--  4-FRAME AUTO-RELEASE + QUEUE PROCESSING -------------------------------
--------------------------------------------------------------------------
local hold = {}
local function stepAutoRelease()
   -- auto-release any held keys
   if next(hold) then
      local rel = 0
      for k,t in pairs(hold) do
         t = t - 1
         if t <= 0 then
            rel = rel | KEY_MASK[k]
            hold[k] = nil
         else
            hold[k] = t
         end
      end
      if rel ~= 0 then emu:clearKeys(rel) end
   end

   -- process queued inputs
   if inputQueue then
      inputQueue.framesUntilNext = inputQueue.framesUntilNext - 1
      if inputQueue.framesUntilNext <= 0 then
         local i = inputQueue.idx

         -- movement verification (only in roam state)
         if getState() == "roam" and i > 1 then
            local x,y = getPlayerPos()
            if x == inputQueue.prevX and y == inputQueue.prevY then
               inputQueue.sock:send("NO_MOVEMENT_DETECTED\n")
               inputQueue = nil
               return
            end
         end

         if i <= #inputQueue.tokens then
            inputQueue.prevX, inputQueue.prevY = getPlayerPos()
            local key = inputQueue.tokens[i]
            emu:addKeys(KEY_MASK[key])
            hold[key] = HOLD_FRAMES
            inputQueue.idx = i + 1
            inputQueue.framesUntilNext = QUEUE_SPACING
         else
            inputQueue.sock:send("QUEUE_COMPLETE\n")
            inputQueue = nil
         end
      end
   end
end
callbacks:add("frame", stepAutoRelease)

--------------------------------------------------------------------------
--  CAPTURE ----------------------------------------------------------------
--------------------------------------------------------------------------
local function sendCapture(sock)
   local img = emu:screenshotToImage()
   if not img then sock:send("ERR no image\n"); return end
   local w,h = img.width, img.height
   local buf = {}
   for y=0,h-1 do
      for x=0,w-1 do
         buf[#buf+1] = string_pack(">I4", img:getPixel(x,y))
      end
   end
   local data = table.concat(buf)
   sock:send(string_pack(">I4", #data))
   sock:send(data)
end

--------------------------------------------------------------------------
--  READRANGE --------------------------------------------------------------
--------------------------------------------------------------------------
local function sendReadRange(sock, addr_str, len_str)
   local addr   = tonumber(addr_str) or tonumber(addr_str, 16)
   local length = tonumber(len_str)  or tonumber(len_str, 16)
   if not addr or not length or length < 1 then
      sock:send("ERR bad args\n")
      return
   end
   local data = emu:readRange(addr, length)
   if not data then sock:send("ERR read failed\n"); return end
   sock:send(string_pack(">I4", #data))
   sock:send(data)
end

--------------------------------------------------------------------------
--  COMMAND PARSER ---------------------------------------------------------
--------------------------------------------------------------------------
local function canonical(tok)
   return KEY_ALIAS[tok] or KEY_ALIAS[tok:upper()] or tok:upper()
end

local function parse(line, sock)
   line = line:match("^(.-)%s*$")
   if line == "" then return end

   -- report current state
   if line:upper() == "STATE" then
      sock:send(getState() .. "\n")
      return
   end

   -- queued-input syntax: tok1;tok2;...;
   if line:find(";") then
      local toks = {}
      for tok in line:gmatch("([^;]+)") do
         tok = tok:match("^%s*(.-)%s*$")
         if tok ~= "" then toks[#toks+1] = canonical(tok) end
      end
      if #toks > 1 then
         local px, py = getPlayerPos()
         inputQueue = {
            tokens          = toks,
            idx             = 1,
            framesUntilNext = 0,
            sock            = sock,
            prevX           = px,
            prevY           = py,
         }
         return
      else
         line = toks[1]
      end
   end

   -- single commands
   local a,l = line:match("^READRANGE%s+(%S+)%s+(%S+)$")
   if a and l then sendReadRange(sock, a, l); return end
   if line:upper() == "CAP" then sendCapture(sock); return end

   local num = line:match("^SET%s+(%S+)$")
   if num then
      local m = tonumber(num) or tonumber(num,16)
      if not m then return nil, "Bad number "..num end
      emu:setKeys(m)
      hold = {}
      return
   end

   -- individual key presses/releases
   local add, clr = 0, 0
   for tok in line:gmatch("%S+") do
      local op,name = tok:match("^([%+%-]?)(.+)$")
      name = canonical(name)
      if not KEY_MASK[name] then
         return nil, "Unknown key "..tok
      end
      if op == "-" then
         clr = clr | KEY_MASK[name]
         hold[name] = nil
      else
         add = add | KEY_MASK[name]
         hold[name] = HOLD_FRAMES
      end
   end
   if add ~= 0 then emu:addKeys(add) end
   if clr ~= 0 then emu:clearKeys(clr) end

   -- nothing matched: report unknown command
   sock:send("ERR unknown command\n")
end

--------------------------------------------------------------------------
--  SOCKET CALLBACKS -------------------------------------------------------
--------------------------------------------------------------------------
local function onRecv(id)
   local s = clients[id]
   while true do
      local line, err_msg = s:receive(4096)
      if not line then
         if err_msg ~= socket.ERRORS.AGAIN then err(id, err_msg); stop(id) end
         return
      end

      -- protect parse() from any runtime error
      local ok, perr = pcall(parse, line, s)
      if not ok then
         err(id, "parse exception: "..tostring(perr))
         s:send("ERR parse exception\n")
      elseif perr then
         err(id, perr)
         s:send("ERR "..perr.."\n")
      end
   end
end

local function onError(id, e)
   err(id, e)
   stop(id)
end

--------------------------------------------------------------------------
--  ACCEPT NEW CLIENTS -----------------------------------------------------
--------------------------------------------------------------------------
local function onAccept()
   local s,e = server:accept()
   if e then err("accept", e); return end
   local id = nextID; nextID = id + 1
   clients[id] = s
   s:add("received", function() onRecv(id) end)
   s:add("error",    function() onError(id) end)
   log(id, "connected")
end

--------------------------------------------------------------------------
--  START LISTENING --------------------------------------------------------
--------------------------------------------------------------------------
local function listen(port)
   while true do
      server, err = socket.bind(nil, port)
      if not server then
         if err == socket.ERRORS.ADDRESS_IN_USE then
            port = port + 1
         else err("bind", err); return end
      else
         local ok,e = server:listen()
         if ok then break else err("listen", e); return end
      end
   end
   console:log("Lua socket server listening on "..port)
   server:add("received", onAccept)
end

listen(LISTEN_PORT)
