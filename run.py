# --- run.py ---

import subprocess
import socket
import time
import os
import sys
import asyncio
import websockets
import json
import logging
from helpers import DEFAULT_ROM

from interactive import interactive_console
from llmdriver import run_auto_loop, MODEL

# --- WebSocket Server Configuration ---
WEBSOCKET_PORT = 8765
PORT = 8888
MGBA_EXE = '/Applications/mGBA.app/Contents/MacOS/mGBA' # Adjust if needed
LUA_SCRIPT = './socketserver.lua' # Adjust if needed

connected_clients = set()
logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s:%(name)s: %(message)s')

log = logging.getLogger('run.py')

# Initialize state - llmdriver will update this
state = {
    "actions": 0,
    "badges": [],
    "gameStatus": "0h 0m 0s",
    "goals": { "primary": 'Initializing...', "secondary": [], "tertiary": '' },
    "otherGoals": 'Initializing...',
    "currentTeam": [],
    "modelName": MODEL,
    "tokensUsed": 0,
    "ggValue": 0,
    "summaryValue": 0,
    "minimapLocation": "Unknown",
    "log_entries": []
}

async def broadcast(message):
    """Sends a JSON message to all connected clients."""
    if not connected_clients:
        return

    message_json = json.dumps(message)
    # Use create_task for better concurrency handling if many clients exist
    send_tasks = [asyncio.create_task(client.send(message_json)) for client in connected_clients]
    if not send_tasks:
        return

    results = await asyncio.gather(*send_tasks, return_exceptions=True)

    disconnected_clients = set()
    clients_list = list(connected_clients) # Create stable list for indexing
    for i, result in enumerate(results):
        if isinstance(result, Exception):
            # Ensure index is valid before accessing clients_list
            if i < len(clients_list):
                client = clients_list[i]
                log.warning(f"WS: Failed to send to client {client.remote_address}: {result}. Removing.")
                disconnected_clients.add(client)
            else:
                 # This case should theoretically not happen with gather but added defensively
                 log.error(f"WS: Index {i} out of bounds for clients list of size {len(clients_list)} during error handling.")

    connected_clients.difference_update(disconnected_clients)


async def send_full_state(websocket):
    """Sends the complete current state to a newly connected client."""
    try:
        # Ensure state is JSON serializable before sending
        state_copy = json.loads(json.dumps(state)) # Simple deep copy via JSON
        await websocket.send(json.dumps(state_copy))
        log.info(f"WS: Sent full initial state to {websocket.remote_address}")
    except websockets.exceptions.ConnectionClosed:
        log.warning(f"WS: Failed to send initial state to {websocket.remote_address}, client disconnected before send completed.")
    except TypeError as e:
         log.error(f"WS: State is not JSON serializable: {e}. State: {state}", exc_info=True)
    except Exception as e:
         log.error(f"WS: Error sending full state to {websocket.remote_address}: {e}", exc_info=True)


async def handler(websocket):
    """Handles a new WebSocket connection."""
    log.info(f"WS: Client connected: {websocket.remote_address}")
    connected_clients.add(websocket)
    try:
        await send_full_state(websocket)
        # Keep connection open, listen for messages (currently ignored)
        async for message in websocket:
            log.info(f"WS: Received message from {websocket.remote_address}: {message} (ignored)")
            # Future: Handle client commands here (e.g., request state refresh, send input?)
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
    # Updates now come from llmdriver via broadcast calls
    async with websockets.serve(handler, "localhost", WEBSOCKET_PORT):
        log.info(f"WebSocket server started on ws://localhost:{WEBSOCKET_PORT}")
        await asyncio.Future() # Run forever until cancelled

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
    try:
        # Redirect stdout to DEVNULL, capture stderr
        proc = subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True)
    except FileNotFoundError:
        log.error(f"Failed to start mGBA. Ensure '{MGBA_EXE}' is correct and executable.")
        sys.exit(1)
    except Exception as e:
        log.error(f"Error starting mGBA: {e}", exc_info=True)
        sys.exit(1)

    # Wait a bit for mGBA and Lua script to initialize the socket server
    time.sleep(3) # Might need adjustment based on system speed

    # Check if mGBA exited prematurely
    if proc.poll() is not None:
        stderr_output = proc.stderr.read() # Read captured stderr
        log.error(f"mGBA process terminated unexpectedly shortly after start. Exit code: {proc.returncode}")
        if stderr_output:
            log.error(f"mGBA stderr:\n{stderr_output.strip()}")
        else:
            log.error("mGBA stderr is empty.")
        sys.exit(1)

    # Attempt to connect to the mGBA socket
    sock = None
    retries = 5
    for attempt in range(retries):
        try:
            # create_connection handles both IPv4/IPv6
            sock = socket.create_connection(('localhost', port), timeout=2)
            # Keep blocking for simplicity in current setup (console/llmdriver manage reads)
            sock.setblocking(True)
            log.info(f"Connected to mGBA scripting server on port {port}")
            return proc, sock # Success
        except ConnectionRefusedError:
            log.warning(f"Connection to mGBA refused (attempt {attempt+1}/{retries}). Is mGBA running and script loaded?")
            if proc.poll() is not None: # Check again if mGBA died while waiting
                 stderr_output = proc.stderr.read()
                 log.error(f"mGBA process terminated while attempting to connect. Exit code: {proc.returncode}")
                 if stderr_output: log.error(f"mGBA stderr:\n{stderr_output.strip()}")
                 sys.exit(1)
            time.sleep(1.5) # Wait longer between retries
        except socket.timeout:
            log.warning(f"Connection to mGBA timed out (attempt {attempt+1}/{retries}).")
            time.sleep(1)
        except Exception as e:
            # Catch other potential socket errors
            log.error(f"Unexpected error connecting to mGBA socket: {e}", exc_info=True)
            if proc and proc.poll() is None:
                proc.terminate()
                proc.wait()
            sys.exit(1)

    # If loop finishes without returning, connection failed
    log.error(f"Failed to connect to mGBA scripting server at localhost:{port} after {retries} attempts.")
    if proc and proc.poll() is None:
        log.info("Terminating mGBA process due to connection failure.")
        proc.terminate()
        proc.wait()
    sys.exit(1)

# --- Main Execution Logic ---
async def main_async(auto):
    """Asynchronous main function to run mGBA, WebSocket server, and optionally the LLM loop."""
    proc = sock = None
    websocket_task = None
    llm_task = None
    tasks_to_await = []

    try:
        proc, sock = start_mgba_with_scripting()

        if auto:
            log.info("Auto mode enabled. Starting WebSocket server and LLM driver.")
            # Start the WebSocket server
            websocket_task = asyncio.create_task(start_websocket_server(), name="WebSocketServer")
            tasks_to_await.append(websocket_task)

            # Start the LLM driver loop
            llm_task = asyncio.create_task(
                run_auto_loop(sock, state, broadcast, interval=10.0),
                name="LLMDriverLoop"
            )
            tasks_to_await.append(llm_task)

            # Wait for either task to complete (which usually indicates an error or shutdown)
            if tasks_to_await:
                done, pending = await asyncio.wait(
                    tasks_to_await,
                    return_when=asyncio.FIRST_COMPLETED,
                )

                # Log results or exceptions from completed tasks
                for task in done:
                    try:
                        result = task.result()
                        log.info(f"Task {task.get_name()} finished unexpectedly with result: {result}")
                    except asyncio.CancelledError:
                         log.info(f"Task {task.get_name()} was cancelled.")
                    except Exception as e:
                        log.error(f"Task {task.get_name()} raised an exception: {e}", exc_info=True)

                # Cancel any remaining pending tasks
                for task in pending:
                    log.info(f"Cancelling pending task: {task.get_name()}")
                    task.cancel()
                # Await the cancellation to complete
                if pending:
                    await asyncio.gather(*pending, return_exceptions=True)


        else:
            log.error("main_async should only be called with auto=True. Handling non-auto mode elsewhere.")
            # This path shouldn't be reached with the current __main__ structure.

    except Exception as e:
        log.error(f"An error occurred in main_async: {e}", exc_info=True)
    finally:
        log.info("Cleaning up async resources...")
        # Cancel tasks if they are still running (e.g., if main_async exits due to an error)
        for task in tasks_to_await:
             if task and not task.done():
                 log.info(f"Cancelling task {task.get_name()} during final cleanup.")
                 task.cancel()
        # Wait for cancellations to complete
        cancelled_tasks = [t for t in tasks_to_await if t and t.cancelled()]
        if cancelled_tasks:
            await asyncio.gather(*cancelled_tasks, return_exceptions=True)

        if sock:
            try:
                # Attempt graceful shutdown of Lua side if possible
                log.info("Sending quit command to mGBA script...")
                try:
                    sock.sendall(b"quit\n")
                    # Give it a moment to process before closing socket
                    await asyncio.sleep(0.2) # Use asyncio.sleep in async context
                except OSError as send_err:
                     log.warning(f"Could not send quit command to mGBA (socket likely closed): {send_err}")

                sock.close()
                log.info("mGBA socket closed.")
            except Exception as e:
                log.error(f"Error closing mGBA socket: {e}")
        if proc and proc.poll() is None:
            log.info("Terminating mGBA process...")
            proc.terminate()
            try:
                # Use asyncio.to_thread for potentially blocking wait in async context
                await asyncio.to_thread(proc.wait, timeout=5)
                log.info("mGBA process terminated.")
            except subprocess.TimeoutExpired:
                log.warning("mGBA process did not terminate gracefully, killing.")
                proc.kill()
                try:
                    await asyncio.to_thread(proc.wait) # Wait for kill
                except Exception as wait_err:
                    log.error(f"Error waiting for mGBA process after kill: {wait_err}")
            except Exception as e:
                 log.error(f"Error waiting for mGBA process termination: {e}")

        log.info("Async cleanup complete.")


if __name__ == '__main__':
    auto = '--auto' in sys.argv

    if auto:
        try:
            asyncio.run(main_async(auto=True))
        except KeyboardInterrupt:
            log.info("KeyboardInterrupt received, stopping async tasks...")
        except Exception as e:
            log.critical(f"Critical error in async execution: {e}", exc_info=True)
        finally:
            log.info("--- Async run finished ---")
    else:
        # --- Synchronous Mode ---
        log.info("Interactive mode enabled. WebSocket server and LLM driver will NOT run.")
        proc = sock = None
        try:
            # Start mGBA (synchronous is fine here)
            proc, sock = start_mgba_with_scripting()
            # Run the interactive console (which is blocking)
            interactive_console(sock) # Call the imported function
        except KeyboardInterrupt:
             log.info("KeyboardInterrupt received, stopping interactive console...")
        except SystemExit:
             log.info("SystemExit called, likely during mGBA startup. Exiting.")
        except Exception as e:
            log.critical(f"Critical error in synchronous execution: {e}", exc_info=True)
        finally:
            log.info("Cleaning up synchronous resources...")
            if sock:
                try:
                    # Attempt graceful shutdown of Lua side
                    log.info("Sending quit command to mGBA script...")
                    try:
                        sock.sendall(b"quit\n")
                        time.sleep(0.2) # Short pause
                    except OSError as send_err:
                        log.warning(f"Could not send quit command to mGBA (socket likely closed): {send_err}")
                    sock.close()
                    log.info("mGBA socket closed.")
                except Exception as e:
                     log.error(f"Error closing mGBA socket: {e}")
            if proc and proc.poll() is None:
                log.info("Terminating mGBA process...")
                proc.terminate()
                try:
                    proc.wait(timeout=5)
                    log.info("mGBA process terminated.")
                except subprocess.TimeoutExpired:
                    log.warning("mGBA process did not terminate gracefully, killing.")
                    proc.kill()
                    proc.wait()
                except Exception as e:
                     log.error(f"Error terminating mGBA process: {e}")
            log.info("--- Interactive run finished ---")