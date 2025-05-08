import subprocess
import socket
import time
import os
import sys
import asyncio
import logging

from helpers import DEFAULT_ROM
from interactive import interactive_console
from llmdriver import run_auto_loop, MODEL
from helpers import send_command
from websocket_service import broadcast_message, run_server_forever as start_websocket_service
from benchmark import load

# --- Configuration (excluding WebSocket specific) ---
PORT = 8888 # mGBA socket port
LOAD_SAVESTATE = False # should we load a savestate? Updated by CLI
MGBA_EXE = '/Applications/mGBA.app/Contents/MacOS/mGBA' # Adjust if needed
LUA_SCRIPT = './socketserver.lua' # Adjust if needed
benchmark_path = None   # default: no external benchmark

logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s:%(name)s: %(message)s')
log = logging.getLogger("main")

# Initialize state - llmdriver will update this
state = {
    "actions": 0,
    "badges": [],
    "gameStatus": "0h 0m 0s",
    "goals": { "primary": 'Initializing...', "secondary": 'Initializing...', "tertiary": 'Initializing...' },
    "otherGoals": 'Initializing...',
    "currentTeam": [],
    "modelName": MODEL,
    "tokensUsed": 0,
    "ggValue": 0,
    "summaryValue": 0,
    "minimapLocation": "Unknown",
    "log_entries": []
}

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
            if(LOAD_SAVESTATE): # Check the global LOAD_SAVESTATE flag
                log.info("LOAD_SAVESTATE is True, attempting to load savestate 1.")
                send_command(sock, "LOADSTATE 1")
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
async def main_async(auto, max_loops_arg=None): # Added max_loops_arg
    """Asynchronous main function to run mGBA, WebSocket server, and optionally the LLM loop."""
    proc = sock = None
    websocket_task = None
    llm_task = None
    tasks_to_await = []

    try:
        # LOAD_SAVESTATE global will be used by start_mgba_with_scripting
        proc, sock = start_mgba_with_scripting()

        if auto:
            log.info("Auto mode enabled. Starting WebSocket server and LLM driver.")
            # Start the WebSocket server (passing the shared 'state' dictionary)
            websocket_task = asyncio.create_task(start_websocket_service(state), name="WebSocketService")
            tasks_to_await.append(websocket_task)

            benchmark = None
            if benchmark_path is not None:
                try:
                    benchmark = load(benchmark_path)
                    log.info("Loaded custom benchmark from %s → %s", benchmark_path, type(benchmark).__name__)
                    max_loops_arg = benchmark.max_loops
                except Exception as e:
                    log.critical("Failed to load benchmark file: %s", e, exc_info=True)
                    sys.exit(1)

            # Start the LLM driver loop (passing the imported broadcast_message function)
            if max_loops_arg is not None:
                log.info(f"Starting LLM driver loop (max_loops: {max_loops_arg})...")
                llm_task = asyncio.create_task(
                    run_auto_loop(sock, state, broadcast_message, interval=13.0, max_loops=max_loops_arg, benchmark=benchmark),
                    name="LLMDriverLoop"
                )
            else:
                log.info("Starting LLM driver loop...")
                llm_task = asyncio.create_task(
                    run_auto_loop(sock, state, broadcast_message, interval=13.0), # Original call
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
        
        pending_cancellations = [t for t in tasks_to_await if t and t.cancelled()] 
        for t in tasks_to_await: 
            if t and not t.done() and t not in pending_cancellations: 
                pending_cancellations.append(t)
        
        if pending_cancellations:
            await asyncio.gather(*pending_cancellations, return_exceptions=True)


        if sock:
            try:
                log.info("Sending quit command to mGBA script...")
                try:
                    sock.sendall(b"quit\n")
                    await asyncio.sleep(0.2) 
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
                await asyncio.to_thread(proc.wait, timeout=5)
                log.info("mGBA process terminated.")
            except subprocess.TimeoutExpired:
                log.warning("mGBA process did not terminate gracefully, killing.")
                proc.kill()
                try:
                    await asyncio.to_thread(proc.wait) 
                except Exception as wait_err:
                    log.error(f"Error waiting for mGBA process after kill: {wait_err}")
            except Exception as e:
                 log.error(f"Error waiting for mGBA process termination: {e}")

        log.info("Async cleanup complete.")


if __name__ == '__main__':
    # Default values for command line arguments
    auto_mode = False
    parsed_max_loops = None
    # LOAD_SAVESTATE is a global, initialized to False at the top of the script.

    cli_args = sys.argv[1:]
    i = 0
    while i < len(cli_args):
        arg = cli_args[i]
        if arg == '--auto':
            auto_mode = True
            i += 1
        elif arg == '--load_savestate':
            LOAD_SAVESTATE = True 
            log.info("Command line argument: --load_savestate detected. LOAD_SAVESTATE set to True.")
            i += 1
        elif arg == '--benchmark':
            if i + 1 >= len(cli_args):
                log.error("--benchmark requires a file path argument")
                sys.exit(1)
            benchmark_path = cli_args[i + 1]
            i += 2
        elif arg == '--max_loops':
            if i + 1 < len(cli_args):
                try:
                    value = int(cli_args[i+1])
                    if value <= 0:
                        log.error("--max_loops value must be a positive integer.")
                        sys.exit(1)
                    parsed_max_loops = value
                    log.info(f"Command line argument: --max_loops set to {parsed_max_loops}.")
                    i += 2 
                except ValueError:
                    log.error(f"--max_loops expects an integer value, got: {cli_args[i+1]}")
                    sys.exit(1)
            else:
                log.error("--max_loops requires an argument (number of loops).")
                sys.exit(1)
        else:
            log.warning(f"Unknown command line argument: {arg}")
            i += 1

    if auto_mode:
        try:
            asyncio.run(main_async(auto=True, max_loops_arg=parsed_max_loops))
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
            proc, sock = start_mgba_with_scripting() 
            interactive_console(sock) 
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
                    log.info("Sending quit command to mGBA script...")
                    try:
                        sock.sendall(b"quit\n")
                        time.sleep(0.2) 
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