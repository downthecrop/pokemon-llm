# --- interactive.py ---

import sys
import select
import json
import logging
from helpers import (
    capture,
    readrange,
    get_party_text,
    get_badges_text,
    get_location,
    send_command,
    prep_llm,
    print_battle,
    touch_controls_path_find
)

log = logging.getLogger('interactive')

# ─── Console command wrappers ─────────────────────────

def cmd_party(sock):
    """Fetches and prints party text."""
    try:
        text = get_party_text(sock)
        print(text)
        return text
    except Exception as e:
        print(f"[PARTY error] {e}")
        log.error(f"Error fetching party: {e}", exc_info=True)
        return None

def cmd_badges(sock):
    """Fetches and prints badges text."""
    try:
        text = get_badges_text(sock)
        print(text)
        return text
    except Exception as e:
        print(f"[BADGES error] {e}")
        log.error(f"Error fetching badges: {e}", exc_info=True)
        return None

def cmd_location(sock):
    """Fetches and prints location data."""
    try:
        loc = get_location(sock)
        if loc is None:
            print("No map data available.")
            return None
        print(f"Location data: {loc}")
        return loc
    except Exception as e:
        print(f"[LOCATION error] {e}")
        log.error(f"Error fetching location: {e}", exc_info=True)
        return None

def cmd_capture(sock, filename=None):
    """Captures the game screen."""
    fn = filename or "latest.png"
    try:
        capture(sock, fn)
        print(f"Captured image to {fn}")
        return fn
    except Exception as e:
        print(f"[CAPTURE error] {e}")
        log.error(f"Error capturing screen: {e}", exc_info=True)
        return None
    
def cmd_touch(sock, pos):
    logging.info(pos)
    xy = pos.split(",")
    x = xy[0]
    y = xy[1]
    loc = get_location(sock)
    if loc is None:
        return

    mid, current_x, current_y, _, _ = loc
    path_actions = touch_controls_path_find(mid, [int(current_x), int(current_y)], [int(x),int(y)])
    if(path_actions != None):
        send_command(sock, path_actions)
    else:
        logging.info("Invalid Path")
    

def cmd_prep(sock):
    """Prepares and prints data for the LLM."""
    try:
        data = prep_llm(sock)
        print(json.dumps(data, indent=2))
        return data
    except Exception as e:
        print(f"[PREP error] {e}")
        log.error(f"Error preparing LLM data: {e}", exc_info=True)
        return None

def cmd_readrange(sock, addr_str, length_str):
    """Reads a memory range and saves to dump.bin."""
    try:
        readrange(sock, addr_str, length_str)
        print(f"Saved memory dump ({addr_str}, len {length_str}) to dump.bin")
    except ValueError as e:
        print(f"[READRANGE usage error] {e}") # e.g., invalid hex/int
    except Exception as e:
        print(f"[READRANGE error] {e}")
        log.error(f"Error reading range {addr_str} len {length_str}: {e}", exc_info=True)

def cmd_print_battle(sock):
    """Prints current battle state if in battle."""
    try:
        print_battle(sock)
    except Exception as e:
        print(f"[BATTLE error] {e}")
        log.error(f"Error printing battle state: {e}", exc_info=True)


# ─── Interactive console Loop ─────────────────────────

def interactive_console(sock):
    """Runs the interactive command console loop."""
    log.info("Starting interactive console. Type 'quit' or 'exit' to stop.")
    sock_fd = sock.fileno()
    stdin_fd = sys.stdin.fileno()
    prompt_shown = False

    try:
        while True:
            if not prompt_shown:
                sys.stdout.write("> ")
                sys.stdout.flush()
                prompt_shown = True

            # Use select to wait for input from stdin or the socket
            rlist, _, _ = select.select([stdin_fd, sock_fd], [], [], 0.1) # Timeout helps prevent busy-waiting

            # Check for incoming data from mGBA (e.g., script prints)
            if sock_fd in rlist:
                try:
                    data = sock.recv(4096)
                    if not data:
                        print("\n[Socket closed by mGBA server]")
                        log.warning("mGBA socket closed unexpectedly.")
                        break
                    text = data.decode('utf-8', errors='replace').strip()
                    if text:
                        sys.stdout.write("\r" + text + "\n")
                        prompt_shown = False
                except OSError as e:
                    print(f"\n[Socket recv error] {e}")
                    log.error(f"Socket receive error: {e}", exc_info=True)
                    break
                continue

            if stdin_fd in rlist:
                line = sys.stdin.readline()
                prompt_shown = False
                if not line:
                    print("\nEOF received.")
                    break
                cmd_full = line.strip()
                if not cmd_full:
                    continue

                parts = cmd_full.split(maxsplit=2)
                cmd = parts[0].lower()

                if cmd in ("quit", "exit"): break
                elif cmd.startswith("cap"): # Allow 'cap' or 'capture'
                    fn = parts[1] if len(parts) > 1 else None
                    cmd_capture(sock, fn)
                elif cmd == "readrange":
                    if len(parts) != 3:
                        print("Usage: readrange <address> <length>")
                        print("  Example: readrange 0x020244E8 100")
                    else:
                        cmd_readrange(sock, parts[1], parts[2])
                elif cmd == "touch":
                    if len(parts) != 2:
                        print("Usage: touch x,y")
                    else:
                        cmd_touch(sock, parts[1])
                elif cmd == "party": cmd_party(sock)
                elif cmd == "badges": cmd_badges(sock)
                elif cmd == "prep": cmd_prep(sock)
                elif cmd in ("loc", "location", "pos", "position"): cmd_location(sock)
                elif cmd in ("battle", "inbattle"): cmd_print_battle(sock)
                else:
                    # Forward unknown commands directly to mGBA Lua script
                    log.debug(f"Forwarding command to mGBA: {cmd_full}")
                    if not cmd_full.endswith("\n"):
                        cmd_full += "\n"
                    try:
                        sock.sendall(cmd_full.encode('utf-8'))
                    except OSError as e:
                        print(f"[Send error] {e}")
                        log.error(f"Socket send error: {e}", exc_info=True)
                        break # Exit loop on send error

    except KeyboardInterrupt:
        print("\nInterrupted. Exiting console.")
        log.info("Interactive console interrupted by user (Ctrl+C).")
    except Exception as e:
        print(f"\nUnexpected error in console: {e}")
        log.exception("Unexpected error in interactive_console")
    finally:
        log.info("Interactive console loop finished.")