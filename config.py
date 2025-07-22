import sys
from pyAIAgent.utils.file_utils import find_mgba

PORT = 8888 # mGBA socket port
LOAD_SAVESTATE = False # should we load a savestate? Updated by CLI
LUA_SCRIPT = './socketserver.lua' # Adjust if needed
benchmark_path = None   # default: no external benchmark

MGBA_EXE = find_mgba() or None

# If it's still not found, exit
if not MGBA_EXE:
    print("Error: mGBA executable not found.")
    sys.exit(1)

