#!/usr/bin/env python3

import subprocess
import socket
import time
import os
import sys
import select
from helpers import (
    capture,
    readrange,
    send_command,
    get_state,
    get_party_text,
    get_badges_text,
    get_location,
    prep_llm,
    print_battle,
    DEFAULT_ROM
)

from openaidriver import run_auto_loop  # new import

PORT = 8888
MGBA_EXE = '/Applications/mGBA.app/Contents/MacOS/mGBA'
LUA_SCRIPT = './socketserver.lua'

def start_mgba_with_scripting(rom_path=None, port=PORT):
    rom_path = rom_path or os.path.join(os.path.dirname(__file__), DEFAULT_ROM)
    cmd = [MGBA_EXE, '--script', LUA_SCRIPT, rom_path]
    print("Starting mGBA:", *cmd)
    proc = subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.STDOUT)
    time.sleep(2)
    sock = socket.create_connection(('localhost', port))
    sock.setblocking(True)
    print(f"Connected to mGBA scripting server on port {port}\n")
    return proc, sock


# ─── Console command wrappers ─────────────────────────

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
    mid, tile_x, tile_y, facing = loc
    print(f"Map ID: {mid} (0x{mid:02X})")
    print(f"Tile Pos: X={tile_x}, Y={tile_y}")
    print(f"Facing: {facing}")
    return loc


def cmd_capture(sock, filename=None):
    fn = filename or 'latest.png'
    capture(sock, fn)
    print(f"Captured image to {fn}")
    return fn


def cmd_prep(sock):
    data = prep_llm(sock)

    print(data["party"])
    print(f"State: {data['state']}")
    print(data["badges"])

    if data["position"] is None:
        print("Position: N/A")
        print("Facing: N/A")
    else:
        x, y = data["position"]
        print(f"Position: {x}, {y}")
        print(f"Facing: {data['facing']}")

    return data


# ─── Interactive console ─────────────────────────────────
def interactive_console(sock):
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
                data = sock.recv(4096)
                if not data:
                    print("\n[Socket closed by server]")
                    break
                text = data.decode('utf-8', errors='replace')
                sys.stdout.write("\r" + text)
                prompt_shown = False
                continue

            if stdin_fd in rlist:
                line = sys.stdin.readline()
                prompt_shown = False
                if not line:
                    break
                cmd = line.strip().lower()

                if cmd in ("quit", "exit"): break
                if cmd.startswith("cap"):
                    parts = cmd.split(maxsplit=1)
                    fn = parts[1] if len(parts) > 1 else None
                    try:
                        cmd_capture(sock, fn)
                    except Exception as e:
                        print(f"[CAP error] {e}")
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
                    try:
                        cmd_party(sock)
                    except Exception as e:
                        print(f"[PARTY error] {e}")
                    continue
                if cmd == "badges":
                    try:
                        cmd_badges(sock)
                    except Exception as e:
                        print(f"[BADGES error] {e}")
                    continue
                if cmd in ("loc", "location", "pos", "position"):
                    try:
                        cmd_location(sock)
                    except Exception as e:
                        print(f"[LOCATION error] {e}")
                    continue
                if cmd in ("battle", "inbattle"):
                    try:
                        print_battle(sock)
                    except Exception as e:
                        print(f"[BATTLE error] {e}")
                    continue
                if cmd == "prep":
                    try:
                        cmd_prep(sock)
                    except Exception as e:
                        print(f"[PREP error] {e}")
                    continue

                # forward other commands to mGBA
                if not line.endswith("\n"): line += "\n"
                try:
                    sock.sendall(line.encode('utf-8'))
                except OSError as e:
                    print(f"[Send error] {e}")
                    break
    except KeyboardInterrupt:
        print("\nInterrupted. Exiting console.")


def main(auto):
    proc = sock = None
    try:
        proc, sock = start_mgba_with_scripting()
        if auto:
            run_auto_loop(sock, interval=5.0)
        else:
            interactive_console(sock)
    finally:
        if sock:
            try: sock.close()
            except: pass
        if proc and proc.poll() is None:
            proc.terminate()
            proc.wait()
        print("Cleaned up and exiting.")


if __name__ == '__main__':
    auto = '--auto' in sys.argv
    main(auto)