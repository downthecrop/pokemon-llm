# --- run.py ---

import subprocess
import socket
import time
import os
import sys
import select
import asyncio
import websockets
import json
import logging
import datetime
from helpers import (
    capture,
    readrange,
    get_party_text,
    get_badges_text,
    get_location,
    prep_llm,
    print_battle,
    DEFAULT_ROM
)

# Make run_auto_loop async compatible later
from llmdriver import run_auto_loop, MODEL as LLM_MODEL_NAME # <-- Import model name

# --- WebSocket Server Configuration ---
WEBSOCKET_PORT = 8765
logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s:%(name)s: %(message)s')
log = logging.getLogger('run.py') # Use specific logger

# --- WebSocket Globals & State ---
connected_clients = set()
# Initialize state - llmdriver will update this
# Use the structure from the mock server, but maybe less detailed initially
state = {
    "actions": 0,
    "badges": [],
    "gameStatus": "0h 0m 0s",
    "goals": { "primary": 'Initializing...', "secondary": [], "tertiary": '' },
    "otherGoals": 'Initializing...',
    "currentTeam": [],
    "modelName": LLM_MODEL_NAME, # <-- Use imported model name
    "tokensUsed": 0,
    "ggValue": 0, # Can be updated by llmdriver if needed
    "summaryValue": 0, # Can be updated by llmdriver if needed
    "minimapLocation": "Unknown",
    "log_entries": [] # Store recent log entries here maybe? Or llmdriver sends them directly
}
start_time = datetime.datetime.now()

# --- WebSocket Server Functions (Copied from mock server) ---

async def broadcast(message):
    """Sends a JSON message to all connected clients."""
    if not connected_clients:
        return

    message_json = json.dumps(message)
    send_tasks = [client.send(message_json) for client in connected_clients]
    if not send_tasks:
        return

    results = await asyncio.gather(*send_tasks, return_exceptions=True)

    disconnected_clients = set()
    clients_list = list(connected_clients)
    for i, result in enumerate(results):
        if isinstance(result, Exception):
            if i < len(clients_list):
                client = clients_list[i]
                log.warning(f"WS: Failed to send to client {client.remote_address}: {result}. Removing.")
                disconnected_clients.add(client)
            else:
                 log.error(f"WS: Index {i} out of bounds for clients list of size {len(clients_list)} during error handling.")

    connected_clients.difference_update(disconnected_clients)


async def send_full_state(websocket):
    """Sends the complete current state to a newly connected client."""
    try:
        # Send a deep copy to avoid potential race conditions during serialization
        state_copy = json.loads(json.dumps(state)) # Simple deep copy via JSON
        await websocket.send(json.dumps(state_copy))
        log.info(f"WS: Sent full initial state to {websocket.remote_address}")
    except websockets.exceptions.ConnectionClosed:
        log.warning(f"WS: Failed to send initial state to {websocket.remote_address}, client disconnected early.")
    except Exception as e:
         log.error(f"WS: Error sending full state to {websocket.remote_address}: {e}", exc_info=True)


async def handler(websocket):
    """Handles a new WebSocket connection."""
    log.info(f"WS: Client connected: {websocket.remote_address}")
    connected_clients.add(websocket)
    try:
        await send_full_state(websocket)
        async for message in websocket:
            log.info(f"WS: Received message from {websocket.remote_address}: {message} (ignored)")
            # Handle client commands if needed in the future
    except websockets.exceptions.ConnectionClosedOK:
        log.info(f"WS: Client {websocket.remote_address} disconnected gracefully.")
    except websockets.exceptions.ConnectionClosedError as e:
        log.warning(f"WS: Client {websocket.remote_address} connection closed with error: {e}")
    except Exception as e:
        log.error(f"WS: Error in handler for {websocket.remote_address}: {e}", exc_info=True)
    finally:
        connected_clients.discard(websocket)
        log.info(f"WS: Client disconnected: {websocket.remote_address}. Remaining clients: {len(connected_clients)}")

async def start_websocket_server():
    """Starts the WebSocket server."""
    # Note: We removed the periodic_updates task. Updates come from llmdriver now.
    async with websockets.serve(handler, "localhost", WEBSOCKET_PORT):
        log.info(f"WebSocket server started on ws://localhost:{WEBSOCKET_PORT}")
        await asyncio.Future() # Run forever

# --- End WebSocket Server Functions ---


# --- mGBA Configuration --- (Keep existing)
PORT = 8888
MGBA_EXE = '/Applications/mGBA.app/Contents/MacOS/mGBA' # Adjust if needed
LUA_SCRIPT = './socketserver.lua' # Adjust if needed

def start_mgba_with_scripting(rom_path=None, port=PORT):
    rom_path = rom_path or os.path.join(os.path.dirname(__file__), DEFAULT_ROM)
    if not os.path.exists(rom_path):
        log.error(f"ROM file not found: {rom_path}")
        sys.exit(1)
    if not os.path.exists(MGBA_EXE):
         log.error(f"mGBA executable not found: {MGBA_EXE}")
         sys.exit(1)
    if not os.path.exists(LUA_SCRIPT):
        log.error(f"Lua script not found: {LUA_SCRIPT}")
        sys.exit(1)

    cmd = [MGBA_EXE, '--script', LUA_SCRIPT, rom_path]
    log.info(f"Starting mGBA: {' '.join(cmd)}")
    # Use Popen with DEVNULL for cleaner output, handle potential errors
    try:
        proc = subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE) # Capture stderr
    except FileNotFoundError:
        log.error(f"Failed to start mGBA. Ensure '{MGBA_EXE}' is correct and executable.")
        sys.exit(1)
    except Exception as e:
        log.error(f"Error starting mGBA: {e}")
        sys.exit(1)

    # Wait for mGBA and Lua script to initialize
    time.sleep(3) # Increased sleep slightly

    # Check if process exited early
    if proc.poll() is not None:
        stderr_output = proc.stderr.read().decode(errors='ignore')
        log.error(f"mGBA process terminated unexpectedly. Exit code: {proc.returncode}")
        log.error(f"mGBA stderr:\n{stderr_output}")
        sys.exit(1)


    # Attempt to connect to the socket
    sock = None
    for _ in range(5): # Retry connection a few times
        try:
            sock = socket.create_connection(('localhost', port), timeout=2)
            sock.setblocking(True) # Keep blocking for simplicity here
            log.info(f"Connected to mGBA scripting server on port {port}")
            return proc, sock
        except (ConnectionRefusedError, socket.timeout) as e:
            log.warning(f"Connection attempt failed: {e}. Retrying...")
            time.sleep(1)
        except Exception as e:
            log.error(f"Unexpected error connecting to mGBA socket: {e}")
            proc.terminate()
            proc.wait()
            sys.exit(1)

    log.error("Failed to connect to mGBA scripting server after multiple attempts.")
    if proc and proc.poll() is None:
        proc.terminate()
        proc.wait()
    sys.exit(1)


# ─── Console command wrappers ─────────────────────────
# (Keep existing cmd_ functions: cmd_party, cmd_badges, cmd_location, cmd_capture, cmd_prep)
def cmd_party(sock):
    text = get_party_text(sock)
    print(text)
    return text

def cmd_badges(sock):
    text = get_badges_text(sock)
    print(text)
    return text

def cmd_location(sock):
    loc = get_location(sock)
    if loc is None:
        print("No map data available.")
        return None
    # Unpack map data if needed, or just print raw for now
    print(f"Location data: {loc}") # Simplified from original
    return loc

def cmd_capture(sock, filename=None):
    fn = filename or 'latest.png'
    try:
        capture(sock, fn)
        print(f"Captured image to {fn}")
        return fn
    except Exception as e:
        print(f"[CAPTURE error] {e}")
        return None

def cmd_prep(sock):
    try:
        data = prep_llm(sock)
        print(json.dumps(data, indent=2)) # Print nicely formatted data
        return data
    except Exception as e:
        print(f"[PREP error] {e}")
        return None


# ─── Interactive console (Keep as is, but it won't run concurrently with WS in --auto) ───
def interactive_console(sock):
    log.info("Starting interactive console. WebSocket server is NOT running in this mode.")
    # (Keep the existing synchronous interactive_console logic)
    # ... existing interactive_console code ...
    sock_fd = sock.fileno()
    stdin_fd = sys.stdin.fileno()
    prompt_shown = False
    try:
        while True:
            if not prompt_shown:
                sys.stdout.write("> ")
                sys.stdout.flush()
                prompt_shown = True

            rlist, _, _ = select.select([stdin_fd, sock_fd], [], [], 0.1)

            if sock_fd in rlist:
                try:
                    data = sock.recv(4096)
                    if not data:
                        print("\n[Socket closed by server]")
                        break
                    text = data.decode('utf-8', errors='replace')
                    sys.stdout.write("\r" + text) # Overwrite prompt if server sent something
                    prompt_shown = False # Need to show prompt again
                except OSError as e:
                    print(f"\n[Socket recv error] {e}")
                    break
                continue

            if stdin_fd in rlist:
                line = sys.stdin.readline()
                prompt_shown = False # Need to show prompt again after command
                if not line: # Handle EOF (Ctrl+D)
                    print("\nEOF received.")
                    break
                cmd = line.strip().lower()

                if cmd in ("quit", "exit"): break
                if cmd.startswith("cap"):
                    parts = cmd.split(maxsplit=1)
                    fn = parts[1] if len(parts) > 1 else None
                    cmd_capture(sock, fn) # Error handled inside
                    continue
                if cmd.startswith("readrange"):
                    parts = cmd.split()
                    if len(parts) != 3:
                        print("Usage: readrange <address> <length>")
                    else:
                        _, addr, length = parts
                        try:
                            readrange(sock, addr, length)
                            print(f"Saved dump to dump.bin")
                        except Exception as e:
                            print(f"[READRANGE error] {e}")
                    continue
                if cmd == "party":
                    cmd_party(sock) # Error handled inside
                    continue
                if cmd == "badges":
                    cmd_badges(sock) # Error handled inside
                    continue
                if cmd in ("loc", "location", "pos", "position"):
                    cmd_location(sock) # Error handled inside
                    continue
                if cmd in ("battle", "inbattle"):
                    try:
                        print_battle(sock)
                    except Exception as e:
                        print(f"[BATTLE error] {e}")
                    continue
                if cmd == "prep":
                    cmd_prep(sock) # Error handled inside
                    continue

                # Forward other commands to mGBA
                if not line.endswith("\n"): line += "\n"
                try:
                    sock.sendall(line.encode('utf-8'))
                except OSError as e:
                    print(f"[Send error] {e}")
                    break
                # Wait briefly for potential response from mGBA script via socket
                # time.sleep(0.1) # Removed - rely on select loop

    except KeyboardInterrupt:
        print("\nInterrupted. Exiting console.")
    except Exception as e:
        print(f"\nUnexpected error in console: {e}") # Catch other errors

# --- Main Execution Logic ---
async def main_async(auto):
    """Asynchronous main function to run mGBA, WebSocket server, and optionally the LLM loop."""
    proc = sock = None
    websocket_task = None
    llm_task = None

    try:
        proc, sock = start_mgba_with_scripting()

        if auto:
            log.info("Auto mode enabled. Starting WebSocket server and LLM driver.")
            # Start the WebSocket server in the background
            websocket_task = asyncio.create_task(start_websocket_server())

            # Start the LLM driver loop
            # Pass the shared state dictionary and the broadcast function
            llm_task = asyncio.create_task(run_auto_loop(sock, state, broadcast, interval=10.0))

            # Wait for either task to complete (or run forever if they don't)
            # If one fails, we might want to stop the other.
            done, pending = await asyncio.wait(
                [websocket_task, llm_task],
                return_when=asyncio.FIRST_COMPLETED,
            )

            # Log results or errors from completed tasks
            for task in done:
                try:
                    result = task.result()
                    log.info(f"Task {task.get_name()} finished with result: {result}")
                except Exception as e:
                    log.error(f"Task {task.get_name()} raised an exception: {e}", exc_info=True)

            # Cancel pending tasks if one finished/failed
            for task in pending:
                log.info(f"Cancelling pending task: {task.get_name()}")
                task.cancel()
                try:
                    await task # Allow cancellation to propagate
                except asyncio.CancelledError:
                    log.info(f"Task {task.get_name()} successfully cancelled.")
                except Exception as e:
                    log.error(f"Error during cancellation of task {task.get_name()}: {e}", exc_info=True)

        else:
            # Run synchronous console - WebSocket server won't run here.
            interactive_console(sock)

    except Exception as e:
        log.error(f"An error occurred in main_async: {e}", exc_info=True)
    finally:
        log.info("Cleaning up...")
        if sock:
            try:
                sock.close()
                log.info("mGBA socket closed.")
            except Exception as e:
                log.error(f"Error closing mGBA socket: {e}")
        if proc and proc.poll() is None:
            log.info("Terminating mGBA process...")
            proc.terminate()
            try:
                proc.wait(timeout=5) # Wait max 5 seconds
                log.info("mGBA process terminated.")
            except subprocess.TimeoutExpired:
                log.warning("mGBA process did not terminate gracefully, killing.")
                proc.kill()
                proc.wait()
        # Ensure asyncio tasks are cancelled if main loop exits unexpectedly
        if websocket_task and not websocket_task.done():
            websocket_task.cancel()
        if llm_task and not llm_task.done():
            llm_task.cancel()
        # Gather cancelled tasks to prevent warnings
        tasks_to_gather = [t for t in [websocket_task, llm_task] if t and not t.done()]
        if tasks_to_gather:
             await asyncio.gather(*tasks_to_gather, return_exceptions=True)

        log.info("Cleanup complete. Exiting.")


if __name__ == '__main__':
    auto = '--auto' in sys.argv
    if auto:
        try:
            asyncio.run(main_async(auto=True))
        except KeyboardInterrupt:
            log.info("KeyboardInterrupt received, stopping...")
        finally:
            log.info("Async main finished.")
    else:
        proc = sock = None
        try:
            proc, sock = start_mgba_with_scripting()
            interactive_console(sock)
        except KeyboardInterrupt:
             log.info("KeyboardInterrupt received, stopping...")
        except Exception as e:
            log.error(f"Error in synchronous main: {e}", exc_info=True)
        finally:
            if sock:
                try: sock.close()
                except: pass
            if proc and proc.poll() is None:
                proc.terminate()
                proc.wait()
            log.info("Synchronous main finished.")