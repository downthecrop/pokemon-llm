# helpers.py (GEN 1)
import struct
import pathlib
import os
from PIL import Image
from dump import find_path, dump_minimal_map

DEFAULT_ROM = 'c.gbc'

# Image capture dimensions
GBA_WIDTH = 240
GBA_HEIGHT = 160
GB_WIDTH = 160
GB_HEIGHT = 144
BYTES_PER_PIXEL = 4

GBA_RASTER_SIZE = GBA_WIDTH * GBA_HEIGHT * BYTES_PER_PIXEL
GB_RASTER_SIZE = GB_WIDTH * GB_HEIGHT * BYTES_PER_PIXEL
SIZE_MAP = {
    GBA_RASTER_SIZE: (GBA_WIDTH, GBA_HEIGHT),
    GB_RASTER_SIZE: (GB_WIDTH, GB_HEIGHT),
}


def capture(sock, filename: str = "latest.png") -> None:
    sock.sendall(b"CAP\n")
    hdr = sock.recv(4)
    if len(hdr) < 4:
        raise RuntimeError("socket closed during CAP header")
    length = struct.unpack(">I", hdr)[0]
    data = bytearray()
    while len(data) < length:
        chunk = sock.recv(length - len(data))
        if not chunk:
            raise RuntimeError("socket closed mid-image")
        data.extend(chunk)
    size = SIZE_MAP.get(length)
    if size is None:
        raise RuntimeError(f"unexpected raster size {length} bytes")
    path = pathlib.Path(filename)
    try:
        img = Image.frombytes("RGBA", size, bytes(data), "raw", "ARGB")
        img.save(path)
    except ModuleNotFoundError:
        path.write_bytes(data)


def readrange(sock, address: str, length: str) -> bytes:
    cmd = f"READRANGE {address} {length}\n".encode('utf-8')
    sock.sendall(cmd)
    hdr = sock.recv(4)
    if len(hdr) < 4:
        raise RuntimeError("socket closed during READRANGE header")
    size = struct.unpack(">I", hdr)[0]
    data = bytearray()
    while len(data) < size:
        chunk = sock.recv(size - len(data))
        if not chunk:
            raise RuntimeError("socket closed mid-dump")
        data.extend(chunk)
    return bytes(data)


def send_command(sock, cmd: str) -> str:
    sock.sendall((cmd.strip() + "\n").encode('utf-8'))
    data = bytearray()
    while True:
        chunk = sock.recv(4096)
        if not chunk:
            raise RuntimeError("socket closed before full response")
        data.extend(chunk)
        if b"\n" in chunk:
            break
    return data.decode('utf-8').rstrip("\n")


def get_state(sock) -> str:
    return send_command(sock, "state")


def get_species_map():
    # TODO: implement species map loading
    raise NotImplementedError


def decode_pokemon_text(raw: bytes) -> str:
    # TODO: implement text decoding
    raise NotImplementedError


def get_party_text(sock) -> str:
    header = readrange(sock, "0xD163", "8")
    count = header[0]
    lines: list[str] = []
    if count == 0:
        lines.append("Your party is empty.")
    else:
        lines.append(f"You have {count} Pokémon in your party:")
        from helpers import get_species_map, decode_pokemon_text
        species_map = get_species_map()
        for slot in range(count):
            data_addr = 0xD163 + 0x08 + slot * 44
            name_addr = 0xD163 + 0x152 + slot * 10
            d = readrange(sock, hex(data_addr), "44")
            raw_name = readrange(sock, hex(name_addr), "10")
            internal_id = header[1 + slot]
            dex_no, mon_name = species_map.get(
                internal_id, (None, f"ID 0x{internal_id:02X}")
            )
            hp_cur = struct.unpack(">H", d[1:3])[0]
            level = d[0x21]
            hp_max = struct.unpack(">H", d[0x22:0x24])[0]
            nickname = decode_pokemon_text(raw_name) or "(no nick)"
            label = f"#{dex_no:03} {mon_name}" if dex_no else mon_name
            lines.append(
                f" • Slot {slot+1}: {label} — '{nickname}', lvl {level}, HP {hp_cur}/{hp_max}"
            )
    return "\n".join(lines)


def get_badges_text(sock) -> str:
    raw = readrange(sock, "0xD356", "1")
    flags = raw[0]
    names = ["Boulder","Cascade","Thunder","Rainbow","Soul","Marsh","Volcano","Earth"]
    have = [names[i] for i in range(8) if flags & (1 << i)]
    return "Badges: " + (", ".join(have) if have else "none")


def get_facing(sock) -> str:
    raw = readrange(sock, "0xC109", "1")[0]
    code = raw & 0xC
    if code == 0x0:
        return "down"
    elif code == 0x4:
        return "up"
    elif code == 0x8:
        return "left"
    elif code == 0xC:
        return "right"
    else:
        return f"unknown(0x{raw:02X})"


def get_location(sock) -> tuple[int, int, int, str] | None:
    mid = readrange(sock, "0xD35E", "1")[0]
    tile_x = readrange(sock, "0xD362", "1")[0]
    tile_y = readrange(sock, "0xD361", "1")[0]
    map_h_blocks = readrange(sock, "0xD368", "1")[0]
    map_w_blocks = readrange(sock, "0xD369", "1")[0]
    map_w_tiles = map_w_blocks * 2
    if map_w_tiles == 0:
        return None
    facing = get_facing(sock)
    return (mid, tile_x, tile_y, facing)


def prep_llm(sock) -> dict | None:
    loc = get_location(sock)
    if loc is None:
        return None
    mid, tile_x, tile_y, facing = loc
    img = dump_minimal_map(DEFAULT_ROM, mid, (tile_x, tile_y))
    img.save("minimap.png")
    capture(sock, "latest.png")
    return {
        "state": get_state(sock),
        "party": get_party_text(sock),
        "badges": get_badges_text(sock),
        "position": (tile_x, tile_y),
        "facing": facing,
    }


def print_battle(sock) -> None:
    cur = readrange(sock, hex(0xD057), "1")[0]
    if cur == 0:
        print("Not currently in a battle.")
        return
    b = readrange(sock, hex(0xD05A), "1")[0]
    types = {
        0xF0: "Wild Battle",
        0xED: "Trainer Battle",
        0xEA: "Gym Leader Battle",
        0xF3: "Final Battle",
        0xF6: "Defeated Trainer",
        0xF9: "Defeated Wild Pokémon",
        0xFC: "Defeated Champion/Gym"
    }
    label = types.get(b, f"Unknown (0x{b:02X})")
    print(f"In battle: {label}")



# ──────────── Internal → Pokédex mapping ────────────
def get_species_map():
 return {
        0x01: (1, "Bulbasaur"),
        0x02: (2, "Ivysaur"),
        0x03: (3, "Venusaur"),
        0xB0: (4, "Charmander"),
        0xB2: (5, "Charmeleon"),
        0xB4: (6, "Charizard"),
        0xB1: (7, "Squirtle"),
        0xB3: (8, "Wartortle"),
        0xB5: (9, "Blastoise"),
    }

def decode_pokemon_text(raw_bytes: bytes) -> str:
    out = []
    for b in raw_bytes:
        if b == 0x50:
            break
        if 0x80 <= b <= 0x99:
            out.append(chr(ord('A') + (b - 0x80)))
        elif 0xA0 <= b <= 0xB9:
            out.append(chr(ord('a') + (b - 0xA0)))
        elif b == 0x7F:
            out.append(' ')
        elif b == 0xE0:
            out.append('é')
        else:
            out.append('?')
    return ''.join(out)