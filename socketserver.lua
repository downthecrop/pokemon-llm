-- socketserver.lua  ── TCP control of mGBA + CAP screenshot + READRANGE command
-- Directions : U D L R          • Triggers : LT RT
-- Start / Sel: S (START)  s (SELECT)
-- Extra      : CAP  ➜ send ARGB raster (length header + pixels)
--           : READRANGE <address> <length>  ➜ send memory bytes (length header + data)
-- Copy to …/mGBA.app/Contents/Resources/scripts/   Run with:
--     mGBA --script socketserver.lua <rom>

---------------------------------------------------------------------------
--  CONFIG -----------------------------------------------------------------
---------------------------------------------------------------------------
local LISTEN_PORT  = 8888   -- TCP port for Python client
local HOLD_FRAMES  = 4      -- frames to keep any pressed key down
local QUEUE_SPACING = 30     -- frames between queued inputs
---------------------------------------------------------------------------

local socket, console, emu = socket, console, emu   -- mGBA globals
local string_pack = string.pack                     -- Lua ≥5.3

--------------------------------------------------------------------------
--  KEY MAP & ALIASES ------------------------------------------------------
--------------------------------------------------------------------------
local KEY_INDEX = { A=0,B=1,SELECT=2,START=3,RIGHT=4,LEFT=5,UP=6,DOWN=7,R=8,L=9 }
local KEY_ALIAS = {
   U="UP",u="UP",  D="DOWN",d="DOWN",  L="LEFT",l="LEFT",  R="RIGHT",r="RIGHT",
   LT="L",lt="L",  RT="R",rt="R",       S="START",  s="SELECT",
}
local KEY_MASK = {}
for k,i in pairs(KEY_INDEX) do KEY_MASK[k] = 1<<i end

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
--  4‑FRAME AUTO‑RELEASE + QUEUE PROCESSING --------------------------------
--------------------------------------------------------------------------
local hold = {}
local function stepAutoRelease()
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

   if inputQueue then
      inputQueue.framesUntilNext = inputQueue.framesUntilNext - 1
      if inputQueue.framesUntilNext <= 0 then
         local i = inputQueue.idx
         if i <= #inputQueue.tokens then
            local key = inputQueue.tokens[i]
            local mask = KEY_MASK[key]
            emu:addKeys(mask)
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
   if not data then sock:send("ERR read failed\n")
      return
   end
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
   if line:find(";") then
      local toks = {}
      for tok in line:gmatch("([^;]+)") do
         tok = tok:match("^%s*(.-)%s*$")
         if tok ~= "" then toks[#toks+1] = canonical(tok) end
      end
      if #toks > 1 then
         inputQueue = { tokens = toks, idx = 1, framesUntilNext = 0, sock = sock }
         return
      else
         line = toks[1]
      end
   end
   local a,l = line:match("^READRANGE%s+(%S+)%s+(%S+)$")
   if a and l then sendReadRange(sock, a, l); return end
   if line:upper() == "CAP" then sendCapture(sock); return end
   local num = line:match("^SET%s+(%S+)$")
   if num then
      local m = tonumber(num) or tonumber(num,16)
      if not m then return nil, "Bad number "..num end
      emu:setKeys(m); hold = {}; return
   end
   local add, clr = 0, 0
   for tok in line:gmatch("%S+") do
      local op, name = tok:match("^([%+%-]?)(.+)$")
      name = canonical(name)
      if not KEY_MASK[name] then return nil, "Unknown key "..tok end
      if op == "-" then clr = clr | KEY_MASK[name]; hold[name] = nil
      else add = add | KEY_MASK[name]; hold[name] = HOLD_FRAMES end
   end
   if add ~= 0 then emu:addKeys(add) end
   if clr ~= 0 then emu:clearKeys(clr) end
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
      local _, perr = parse(line, s)
      if perr then err(id, perr) end
   end
end

local function onError(id, e)
   err(id, e); stop(id)
end

--------------------------------------------------------------------------
--  ACCEPT NEW CLIENTS -----------------------------------------------------
--------------------------------------------------------------------------
local function onAccept()
   local s, e = server:accept()
   if e then err("accept", e); return end
   local id = nextID; nextID = id + 1
   clients[id] = s
   s:add("received", function() onRecv(id) end)
   s:add("error",    function() onError(id, e) end)
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
         local ok, e = server:listen()
         if ok then break else err("listen", e); return end
      end
   end
   console:log("Lua socket server listening on "..port)
   server:add("received", onAccept)
end

listen(LISTEN_PORT)