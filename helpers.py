# helpers.py (GEN 1)
import struct
import pathlib
from PIL import Image, ImageDraw
from dump import find_path, dump_minimal_map
import time
import json
import re
from enum import IntEnum

DEFAULT_ROM = 'c.gbc'

# Image capture dimensions
GBA_WIDTH = 240
GBA_HEIGHT = 160
GB_WIDTH = 160
GB_HEIGHT = 144
BYTES_PER_PIXEL = 4
MINI_MAP_SIZE = None #(21,21)  # 21x21 tiles for the minimap, None = Full Map


GBA_RASTER_SIZE = GBA_WIDTH * GBA_HEIGHT * BYTES_PER_PIXEL
GB_RASTER_SIZE = GB_WIDTH * GB_HEIGHT * BYTES_PER_PIXEL
SIZE_MAP = {
    GBA_RASTER_SIZE: (GBA_WIDTH, GBA_HEIGHT),
    GB_RASTER_SIZE: (GB_WIDTH, GB_HEIGHT),
}


def _flush_socket(sock) -> None:
    """
    Drain any pending data from sock so that our next recv()
    only sees the fresh response to the command we send.
    """
    # Switch to non-blocking so recv() returns immediately if no data
    sock.setblocking(False)
    try:
        while True:
            data = sock.recv(4096)
            if not data:
                break
    except (BlockingIOError, OSError):
        # No more data to read
        pass
    finally:
        # Go back to blocking mode
        sock.setblocking(True)


def capture(sock, filename: str = "latest.png", cell_size: int = 16) -> None:
    # flush any leftover bytes
    _flush_socket(sock)

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

    # build image from raw data
    img = Image.frombytes("RGBA", size, bytes(data), "raw", "ARGB")

    # draw the 16×16 grid
    draw = ImageDraw.Draw(img)
    w, h = img.size
    grid_color = (255, 0, 0, 128)  # semi-transparent red

    for x in range(0, w + 1, cell_size):
        draw.line(((x, 0), (x, h)), fill=grid_color)
    for y in range(0, h + 1, cell_size):
        draw.line(((0, y), (w, y)), fill=grid_color)

    # save
    path = pathlib.Path(filename)
    img.save(path)

def readrange(sock, address: str, length: str) -> bytes:
    _flush_socket(sock)
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
    _flush_socket(sock)
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

def touch_controls_path_find(mapid, currentPos, screenCoords):
    """
    Translate the screentouch to worldspace and gets actions to navigate.
    Player is always at [4,4] ([0,0] is upper left cell)
    """
    x = int(screenCoords[0]) - 4
    y = int(screenCoords[1]) - 4
    print(f"POS: {int(currentPos[0])},{int(currentPos[1])}, Translated: {x},{y}, Desination: {int(currentPos[0]) + x},{int(currentPos[1]) + y}")
    destination = [max(int(currentPos[0]) + x, 0), max(int(currentPos[1]) + y, 0)]
    actions = find_path(DEFAULT_ROM, mapid, currentPos, destination)
    if(actions == None):
        return "[PATH BLOCKED OR INVALID UNWALKABLE DESTINATION]\n"
    return actions # None if there is no valid path
    

def get_state(sock) -> str:
    _flush_socket(sock)
    return send_command(sock, "state")

def get_party_text(sock) -> str:
    _flush_socket(sock)
    party = []
    header = readrange(sock, "0xD163", "8")
    count = header[0]
    species_map = get_species_map()
    for slot in range(count):
        data_addr = 0xD163 + 0x08 + slot * 44
        name_addr = 0xD163 + 0x152 + slot * 10
        d = readrange(sock, hex(data_addr), "44")
        raw_name = readrange(sock, hex(name_addr), "10")
        internal_id = header[1 + slot]

        # Now expect 4-tuple: (dex_no, mon_name, type1, type2)
        dex_no, mon_name, type1, type2 = species_map.get(
            internal_id,
            (None, f"ID 0x{internal_id:02X}", None, None)
        )

        hp_cur = struct.unpack(">H", d[1:3])[0]
        level = d[0x21]
        hp_max = struct.unpack(">H", d[0x22:0x24])[0]
        nickname = decode_pokemon_text(raw_name) or "(no nick)"

        # Build a types string, e.g. "Grass/Poison" or just "Fire"
        types = type1 if type1 else ""
        if type2:
            types += f"/{type2}"
        
        mon = {"name": mon_name, "level": level, "type": type1, "hp": hp_cur, "maxHp": hp_max, "nickname": nickname}
        party.append(mon)
    return party


def get_badges_text(sock) -> str:
    _flush_socket(sock)
    raw = readrange(sock, "0xD356", "1")
    flags = raw[0]
    names = ["Boulder","Cascade","Thunder","Rainbow","Soul","Marsh","Volcano","Earth"]
    have = [names[i] for i in range(8) if flags & (1 << i)]
    return have


def get_facing(sock) -> str:
    _flush_socket(sock)
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
    _flush_socket(sock)
    mid = readrange(sock, "0xD35E", "1")[0]
    mapName = get_location_name(mid)
    tile_x = readrange(sock, "0xD362", "1")[0]
    tile_y = readrange(sock, "0xD361", "1")[0]
    map_h_blocks = readrange(sock, "0xD368", "1")[0]
    map_w_blocks = readrange(sock, "0xD369", "1")[0]
    map_w_tiles = map_w_blocks * 2
    if map_w_tiles == 0:
        return None
    facing = get_facing(sock)
    return (mid, tile_x, tile_y, facing, mapName)


def prep_llm(sock) -> dict:
    _flush_socket(sock)
    capture(sock, "latest.png")
    time.sleep(0.1)
    _flush_socket(sock)
    loc = get_location(sock)
    mid = None
    mapName = None

    if loc:
        mid, x, y, facing, mapName = loc
        dump_minimal_map(DEFAULT_ROM, mid, (x, y), grid_lines=True, crop=MINI_MAP_SIZE).save("minimap.png")
        position = (x, y)
    else:
        # no map data or in battle → empty map
        open("minimap.png", "wb").close()
        position = None
        facing = None

    return {
        "party":   get_party_text(sock),
        "map_id": mid,
        "badges":  get_badges_text(sock),
        "position": position,
        "facing":  facing,
        "map_name": mapName,
    }



def print_battle(sock) -> None:
    _flush_socket(sock)
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
  """
  Returns a dictionary mapping internal Pokémon Red/Blue IDs (hex)
  to a tuple containing: (Pokédex number, Name, Type1, Type2).
  Type2 is None if the Pokémon has only one type.
  """
  return {
        # Glitch / MissingNo. Entries (No Pokédex Number)
        0x00: (None, "'M (glitch)", "Bird", "Normal"),
        0x1F: (None, "MissingNo. (Gyaoon)", "Bird", "Normal"),
        0x20: (None, "MissingNo. (Nidoran♂-like Pokémon)", "Bird", "Normal"),
        0x32: (None, "MissingNo. (Balloonda)", "Bird", "Normal"),
        0x34: (None, "MissingNo. (Buu)", "Bird", "Normal"),
        0x38: (None, "MissingNo. (Deer)", "Bird", "Normal"),
        0x3D: (None, "MissingNo. (Elephant Pokémon)", "Bird", "Normal"),
        0x3E: (None, "MissingNo. (Crocky)", "Bird", "Normal"),
        0x3F: (None, "MissingNo. (Squid Pokémon 1)", "Bird", "Normal"),
        0x43: (None, "MissingNo. (Cactus)", "Bird", "Normal"),
        0x44: (None, "MissingNo. (Jaggu)", "Bird", "Normal"),
        0x45: (None, "MissingNo. (Zubat pre-evo)", "Bird", "Normal"),
        0x4F: (None, "MissingNo. (Fish Pokémon 1)", "Bird", "Normal"),
        0x50: (None, "MissingNo. (Fish Pokémon 2)", "Bird", "Normal"),
        0x51: (None, "MissingNo. (Vulpix pre-evo)", "Bird", "Normal"),
        0x56: (None, "MissingNo. (Frog-like Pokémon 1)", "Bird", "Normal"),
        0x57: (None, "MissingNo. (Frog-like Pokémon 2)", "Bird", "Normal"),
        0x5E: (None, "MissingNo. (Lizard Pokémon 2)", "Bird", "Normal"),
        0x5F: (None, "MissingNo. (Lizard Pokémon 3)", "Bird", "Normal"),
        0x73: (None, "MissingNo. [Unknown]", "Bird", "Normal"),
        0x79: (None, "MissingNo. [Unknown]", "Bird", "Normal"),
        0x7A: (None, "MissingNo. (Squid Pokémon 2)", "Bird", "Normal"),
        0x7F: (None, "MissingNo. (Golduck mid-evo)", "Bird", "Normal"),
        0x86: (None, "MissingNo. (Meowth pre-evo)", "Bird", "Normal"),
        0x87: (None, "MissingNo. [Unknown]", "Bird", "Normal"),
        0x89: (None, "MissingNo. (Gyaoon pre-evo)", "Bird", "Normal"),
        0x8C: (None, "MissingNo. (Magneton-like Pokémon)", "Bird", "Normal"),
        0x92: (None, "MissingNo. (Marowak evo)", "Bird", "Normal"),
        0x9C: (None, "MissingNo. (Goldeen pre-evo)", "Bird", "Normal"),
        0x9F: (None, "MissingNo. (Kotora)", "Bird", "Normal"),
        0xA0: (None, "MissingNo. (Raitora)", "Bird", "Normal"),
        0xA1: (None, "MissingNo. (Raitora evo)", "Bird", "Normal"),
        0xA2: (None, "MissingNo. (Ponyta pre-evo)", "Bird", "Normal"),
        0xAC: (None, "MissingNo. (Blastoise-like Pokémon)", "Bird", "Normal"),
        0xAE: (None, "MissingNo. (Lizard Pokémon 1)", "Bird", "Normal"),
        0xAF: (None, "MissingNo. (Gorochu)", "Bird", "Normal"),
        0xB5: (None, "MissingNo. (Original Wartortle evo)", "Bird", "Normal"),
        0xB6: (None, "MissingNo. (Kabutops Fossil)", "Bird", "Normal"),
        0xB7: (None, "MissingNo. (Aerodactyl Fossil)", "Bird", "Normal"),
        0xB8: (None, "MissingNo. (Pokémon Tower Ghost)", "Bird", "Normal"),

        # Pokédex Order
        0x99: (1, "Bulbasaur", "Grass", "Poison"),
        0x09: (2, "Ivysaur", "Grass", "Poison"),
        0x9A: (3, "Venusaur", "Grass", "Poison"),
        0xB0: (4, "Charmander", "Fire", None),
        0xB2: (5, "Charmeleon", "Fire", None),
        0xB4: (6, "Charizard", "Fire", "Flying"),
        0xB1: (7, "Squirtle", "Water", None),
        0xB3: (8, "Wartortle", "Water", None),
        0x1C: (9, "Blastoise", "Water", None),
        0x7B: (10, "Caterpie", "Bug", None),
        0x7C: (11, "Metapod", "Bug", None),
        0x7D: (12, "Butterfree", "Bug", "Flying"),
        0x70: (13, "Weedle", "Bug", "Poison"),
        0x71: (14, "Kakuna", "Bug", "Poison"),
        0x72: (15, "Beedrill", "Bug", "Poison"),
        0x24: (16, "Pidgey", "Normal", "Flying"),
        0x96: (17, "Pidgeotto", "Normal", "Flying"),
        0x97: (18, "Pidgeot", "Normal", "Flying"),
        0xA5: (19, "Rattata", "Normal", None),
        0xA6: (20, "Raticate", "Normal", None),
        0x05: (21, "Spearow", "Normal", "Flying"),
        0x23: (22, "Fearow", "Normal", "Flying"),
        0x6C: (23, "Ekans", "Poison", None),
        0x2D: (24, "Arbok", "Poison", None),
        0x54: (25, "Pikachu", "Electric", None),
        0x55: (26, "Raichu", "Electric", None),
        0x60: (27, "Sandshrew", "Ground", None),
        0x61: (28, "Sandslash", "Ground", None),
        0x0F: (29, "Nidoran♀", "Poison", None),
        0xA8: (30, "Nidorina", "Poison", None),
        0x10: (31, "Nidoqueen", "Poison", "Ground"),
        0x03: (32, "Nidoran♂", "Poison", None),
        0xA7: (33, "Nidorino", "Poison", None),
        0x07: (34, "Nidoking", "Poison", "Ground"),
        0x04: (35, "Clefairy", "Normal", None),
        0x8E: (36, "Clefable", "Normal", None),
        0x52: (37, "Vulpix", "Fire", None),
        0x53: (38, "Ninetales", "Fire", None),
        0x64: (39, "Jigglypuff", "Normal", None),
        0x65: (40, "Wigglytuff", "Normal", None),
        0x6B: (41, "Zubat", "Poison", "Flying"),
        0x82: (42, "Golbat", "Poison", "Flying"),
        0xB9: (43, "Oddish", "Grass", "Poison"),
        0xBA: (44, "Gloom", "Grass", "Poison"),
        0xBB: (45, "Vileplume", "Grass", "Poison"),
        0x6D: (46, "Paras", "Bug", "Grass"),
        0x2E: (47, "Parasect", "Bug", "Grass"),
        0x41: (48, "Venonat", "Bug", "Poison"),
        0x77: (49, "Venomoth", "Bug", "Poison"),
        0x3B: (50, "Diglett", "Ground", None),
        0x76: (51, "Dugtrio", "Ground", None),
        0x4D: (52, "Meowth", "Normal", None),
        0x90: (53, "Persian", "Normal", None),
        0x2F: (54, "Psyduck", "Water", None),
        0x80: (55, "Golduck", "Water", None),
        0x39: (56, "Mankey", "Fighting", None),
        0x75: (57, "Primeape", "Fighting", None),
        0x21: (58, "Growlithe", "Fire", None),
        0x14: (59, "Arcanine", "Fire", None),
        0x47: (60, "Poliwag", "Water", None),
        0x6E: (61, "Poliwhirl", "Water", None),
        0x6F: (62, "Poliwrath", "Water", "Fighting"),
        0x94: (63, "Abra", "Psychic", None),
        0x26: (64, "Kadabra", "Psychic", None),
        0x95: (65, "Alakazam", "Psychic", None),
        0x6A: (66, "Machop", "Fighting", None),
        0x29: (67, "Machoke", "Fighting", None),
        0x7E: (68, "Machamp", "Fighting", None),
        0xBC: (69, "Bellsprout", "Grass", "Poison"),
        0xBD: (70, "Weepinbell", "Grass", "Poison"),
        0xBE: (71, "Victreebel", "Grass", "Poison"),
        0x18: (72, "Tentacool", "Water", "Poison"),
        0x9B: (73, "Tentacruel", "Water", "Poison"),
        0xA9: (74, "Geodude", "Rock", "Ground"),
        0x27: (75, "Graveler", "Rock", "Ground"),
        0x31: (76, "Golem", "Rock", "Ground"),
        0xA3: (77, "Ponyta", "Fire", None),
        0xA4: (78, "Rapidash", "Fire", None),
        0x25: (79, "Slowpoke", "Water", "Psychic"),
        0x08: (80, "Slowbro", "Water", "Psychic"),
        0xAD: (81, "Magnemite", "Electric", None),
        0x36: (82, "Magneton", "Electric", None),
        0x40: (83, "Farfetch'd", "Normal", "Flying"),
        0x46: (84, "Doduo", "Normal", "Flying"),
        0x74: (85, "Dodrio", "Normal", "Flying"),
        0x3A: (86, "Seel", "Water", None),
        0x78: (87, "Dewgong", "Water", "Ice"),
        0x0D: (88, "Grimer", "Poison", None),
        0x88: (89, "Muk", "Poison", None),
        0x17: (90, "Shellder", "Water", None),
        0x8B: (91, "Cloyster", "Water", "Ice"),
        0x19: (92, "Gastly", "Ghost", "Poison"),
        0x93: (93, "Haunter", "Ghost", "Poison"),
        0x0E: (94, "Gengar", "Ghost", "Poison"),
        0x22: (95, "Onix", "Rock", "Ground"),
        0x30: (96, "Drowzee", "Psychic", None),
        0x81: (97, "Hypno", "Psychic", None),
        0x4E: (98, "Krabby", "Water", None),
        0x8A: (99, "Kingler", "Water", None),
        0x06: (100, "Voltorb", "Electric", None),
        0x8D: (101, "Electrode", "Electric", None),
        0x0C: (102, "Exeggcute", "Grass", "Psychic"),
        0x0A: (103, "Exeggutor", "Grass", "Psychic"),
        0x11: (104, "Cubone", "Ground", None),
        0x91: (105, "Marowak", "Ground", None),
        0x2B: (106, "Hitmonlee", "Fighting", None),
        0x2C: (107, "Hitmonchan", "Fighting", None),
        0x0B: (108, "Lickitung", "Normal", None),
        0x37: (109, "Koffing", "Poison", None),
        0x8F: (110, "Weezing", "Poison", None),
        0x12: (111, "Rhyhorn", "Ground", "Rock"),
        0x01: (112, "Rhydon", "Ground", "Rock"),
        0x28: (113, "Chansey", "Normal", None),
        0x1E: (114, "Tangela", "Grass", None),
        0x02: (115, "Kangaskhan", "Normal", None),
        0x5C: (116, "Horsea", "Water", None),
        0x5D: (117, "Seadra", "Water", None),
        0x9D: (118, "Goldeen", "Water", None),
        0x9E: (119, "Seaking", "Water", None),
        0x1B: (120, "Staryu", "Water", None),
        0x98: (121, "Starmie", "Water", "Psychic"),
        0x2A: (122, "Mr. Mime", "Psychic", None),
        0x1A: (123, "Scyther", "Bug", "Flying"),
        0x48: (124, "Jynx", "Ice", "Psychic"),
        0x35: (125, "Electabuzz", "Electric", None),
        0x33: (126, "Magmar", "Fire", None),
        0x1D: (127, "Pinsir", "Bug", None),
        0x3C: (128, "Tauros", "Normal", None),
        0x85: (129, "Magikarp", "Water", None),
        0x16: (130, "Gyarados", "Water", "Flying"),
        0x13: (131, "Lapras", "Water", "Ice"),
        0x4C: (132, "Ditto", "Normal", None),
        0x66: (133, "Eevee", "Normal", None),
        0x69: (134, "Vaporeon", "Water", None),
        0x68: (135, "Jolteon", "Electric", None),
        0x67: (136, "Flareon", "Fire", None),
        0xAA: (137, "Porygon", "Normal", None),
        0x62: (138, "Omanyte", "Rock", "Water"),
        0x63: (139, "Omastar", "Rock", "Water"),
        0x5A: (140, "Kabuto", "Rock", "Water"),
        0x5B: (141, "Kabutops", "Rock", "Water"),
        0xAB: (142, "Aerodactyl", "Rock", "Flying"),
        0x84: (143, "Snorlax", "Normal", None),
        0x4A: (144, "Articuno", "Ice", "Flying"),
        0x4B: (145, "Zapdos", "Electric", "Flying"),
        0x49: (146, "Moltres", "Fire", "Flying"),
        0x58: (147, "Dratini", "Dragon", None),
        0x59: (148, "Dragonair", "Dragon", None),
        0x42: (149, "Dragonite", "Dragon", "Flying"),
        0x83: (150, "Mewtwo", "Psychic", None),
        0x15: (151, "Mew", "Psychic", None),
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

def parse_optional_fenced_json(text):
    """
    Parses JSON from `text`, which may be either:
      - Enclosed in triple-backtick fences (``` or ```json … ```)
      - Plain JSON without fences

    Returns:
        The deserialized Python object.

    Raises:
        ValueError: if JSON parsing fails or (if fenced) no valid fence is found.
    """
    # Try to find a fenced JSON block first
    fence_pattern = r'```(?:json)?\s*\n(.*?)\n```'
    m = re.search(fence_pattern, text, flags=re.DOTALL | re.IGNORECASE)
    if m:
        json_str = m.group(1)
    else:
        # No fence: assume the entire text is JSON
        json_str = text.strip()
    
    try:
        j = json.loads(json_str)
        return j
    except:
        return {}

# from https://github.com/davidhershey/ClaudePlaysPokemonStarter/blob/main/agent/memory_reader.py
class MapLocation(IntEnum):
    """Maps location IDs to their names"""

    PALLET_TOWN = 0x00
    VIRIDIAN_CITY = 0x01
    PEWTER_CITY = 0x02
    CERULEAN_CITY = 0x03
    LAVENDER_TOWN = 0x04
    VERMILION_CITY = 0x05
    CELADON_CITY = 0x06
    FUCHSIA_CITY = 0x07
    CINNABAR_ISLAND = 0x08
    INDIGO_PLATEAU = 0x09
    SAFFRON_CITY = 0x0A
    UNUSED_0B = 0x0B
    ROUTE_1 = 0x0C
    ROUTE_2 = 0x0D
    ROUTE_3 = 0x0E
    ROUTE_4 = 0x0F
    ROUTE_5 = 0x10
    ROUTE_6 = 0x11
    ROUTE_7 = 0x12
    ROUTE_8 = 0x13
    ROUTE_9 = 0x14
    ROUTE_10 = 0x15
    ROUTE_11 = 0x16
    ROUTE_12 = 0x17
    ROUTE_13 = 0x18
    ROUTE_14 = 0x19
    ROUTE_15 = 0x1A
    ROUTE_16 = 0x1B
    ROUTE_17 = 0x1C
    ROUTE_18 = 0x1D
    ROUTE_19 = 0x1E
    ROUTE_20 = 0x1F
    ROUTE_21 = 0x20
    ROUTE_22 = 0x21
    ROUTE_23 = 0x22
    ROUTE_24 = 0x23
    ROUTE_25 = 0x24
    PLAYERS_HOUSE_1F = 0x25
    PLAYERS_HOUSE_2F = 0x26
    RIVALS_HOUSE = 0x27
    OAKS_LAB = 0x28
    VIRIDIAN_POKECENTER = 0x29
    VIRIDIAN_MART = 0x2A
    VIRIDIAN_SCHOOL = 0x2B
    VIRIDIAN_HOUSE = 0x2C
    VIRIDIAN_GYM = 0x2D
    DIGLETTS_CAVE_ROUTE2 = 0x2E
    VIRIDIAN_FOREST_NORTH_GATE = 0x2F
    ROUTE_2_HOUSE = 0x30
    ROUTE_2_GATE = 0x31
    VIRIDIAN_FOREST_SOUTH_GATE = 0x32
    VIRIDIAN_FOREST = 0x33
    MUSEUM_1F = 0x34
    MUSEUM_2F = 0x35
    PEWTER_GYM = 0x36
    PEWTER_HOUSE_1 = 0x37
    PEWTER_MART = 0x38
    PEWTER_HOUSE_2 = 0x39
    PEWTER_POKECENTER = 0x3A
    MT_MOON_1F = 0x3B
    MT_MOON_B1F = 0x3C
    MT_MOON_B2F = 0x3D
    CERULEAN_TRASHED_HOUSE = 0x3E
    CERULEAN_TRADE_HOUSE = 0x3F
    CERULEAN_POKECENTER = 0x40
    CERULEAN_GYM = 0x41
    BIKE_SHOP = 0x42
    CERULEAN_MART = 0x43
    MT_MOON_POKECENTER = 0x44
    ROUTE_5_GATE = 0x46
    UNDERGROUND_PATH_ROUTE5 = 0x47
    DAYCARE = 0x48
    ROUTE_6_GATE = 0x49
    UNDERGROUND_PATH_ROUTE6 = 0x4A
    ROUTE_7_GATE = 0x4C
    UNDERGROUND_PATH_ROUTE7 = 0x4D
    ROUTE_8_GATE = 0x4F
    UNDERGROUND_PATH_ROUTE8 = 0x50
    ROCK_TUNNEL_POKECENTER = 0x51
    ROCK_TUNNEL_1F = 0x52
    POWER_PLANT = 0x53
    ROUTE_11_GATE_1F = 0x54
    DIGLETTS_CAVE_ROUTE11 = 0x55
    ROUTE_11_GATE_2F = 0x56
    ROUTE_12_GATE_1F = 0x57
    BILLS_HOUSE = 0x58
    VERMILION_POKECENTER = 0x59
    FAN_CLUB = 0x5A
    VERMILION_MART = 0x5B
    VERMILION_GYM = 0x5C
    VERMILION_HOUSE_1 = 0x5D
    VERMILION_DOCK = 0x5E
    SS_ANNE_1F = 0x5F
    SS_ANNE_2F = 0x60
    SS_ANNE_3F = 0x61
    SS_ANNE_B1F = 0x62
    SS_ANNE_BOW = 0x63
    SS_ANNE_KITCHEN = 0x64
    SS_ANNE_CAPTAINS_ROOM = 0x65
    SS_ANNE_1F_ROOMS = 0x66
    SS_ANNE_2F_ROOMS = 0x67
    SS_ANNE_B1F_ROOMS = 0x68
    VICTORY_ROAD_1F = 0x6C
    LANCE = 0x71
    HALL_OF_FAME = 0x76
    UNDERGROUND_PATH_NS = 0x77
    CHAMPIONS_ROOM = 0x78
    UNDERGROUND_PATH_WE = 0x79
    CELADON_MART_1F = 0x7A
    CELADON_MART_2F = 0x7B
    CELADON_MART_3F = 0x7C
    CELADON_MART_4F = 0x7D
    CELADON_MART_ROOF = 0x7E
    CELADON_MART_ELEVATOR = 0x7F
    CELADON_MANSION_1F = 0x80
    CELADON_MANSION_2F = 0x81
    CELADON_MANSION_3F = 0x82
    CELADON_MANSION_ROOF = 0x83
    CELADON_MANSION_ROOF_HOUSE = 0x84
    CELADON_POKECENTER = 0x85
    CELADON_GYM = 0x86
    GAME_CORNER = 0x87
    CELADON_MART_5F = 0x88
    GAME_CORNER_PRIZE_ROOM = 0x89
    CELADON_DINER = 0x8A
    CELADON_HOUSE = 0x8B
    CELADON_HOTEL = 0x8C
    LAVENDER_POKECENTER = 0x8D
    POKEMON_TOWER_1F = 0x8E
    POKEMON_TOWER_2F = 0x8F
    POKEMON_TOWER_3F = 0x90
    POKEMON_TOWER_4F = 0x91
    POKEMON_TOWER_5F = 0x92
    POKEMON_TOWER_6F = 0x93
    POKEMON_TOWER_7F = 0x94
    LAVENDER_HOUSE_1 = 0x95
    LAVENDER_MART = 0x96
    LAVENDER_HOUSE_2 = 0x97
    FUCHSIA_MART = 0x98
    FUCHSIA_HOUSE_1 = 0x99
    FUCHSIA_POKECENTER = 0x9A
    FUCHSIA_HOUSE_2 = 0x9B
    SAFARI_ZONE_ENTRANCE = 0x9C
    FUCHSIA_GYM = 0x9D
    FUCHSIA_MEETING_ROOM = 0x9E
    SEAFOAM_ISLANDS_B1F = 0x9F
    SEAFOAM_ISLANDS_B2F = 0xA0
    SEAFOAM_ISLANDS_B3F = 0xA1
    SEAFOAM_ISLANDS_B4F = 0xA2
    VERMILION_HOUSE_2 = 0xA3
    VERMILION_HOUSE_3 = 0xA4
    POKEMON_MANSION_1F = 0xA5
    CINNABAR_GYM = 0xA6
    CINNABAR_LAB_1 = 0xA7
    CINNABAR_LAB_2 = 0xA8
    CINNABAR_LAB_3 = 0xA9
    CINNABAR_LAB_4 = 0xAA
    CINNABAR_POKECENTER = 0xAB
    CINNABAR_MART = 0xAC
    INDIGO_PLATEAU_LOBBY = 0xAE
    COPYCATS_HOUSE_1F = 0xAF
    COPYCATS_HOUSE_2F = 0xB0
    FIGHTING_DOJO = 0xB1
    SAFFRON_GYM = 0xB2
    SAFFRON_HOUSE_1 = 0xB3
    SAFFRON_MART = 0xB4
    SILPH_CO_1F = 0xB5
    SAFFRON_POKECENTER = 0xB6
    SAFFRON_HOUSE_2 = 0xB7
    ROUTE_15_GATE_1F = 0xB8
    ROUTE_15_GATE_2F = 0xB9
    ROUTE_16_GATE_1F = 0xBA
    ROUTE_16_GATE_2F = 0xBB
    ROUTE_16_HOUSE = 0xBC
    ROUTE_12_HOUSE = 0xBD
    ROUTE_18_GATE_1F = 0xBE
    ROUTE_18_GATE_2F = 0xBF
    SEAFOAM_ISLANDS_1F = 0xC0
    ROUTE_22_GATE = 0xC1
    VICTORY_ROAD_2F = 0xC2
    ROUTE_12_GATE_2F = 0xC3
    VERMILION_HOUSE_4 = 0xC4
    DIGLETTS_CAVE = 0xC5
    VICTORY_ROAD_3F = 0xC6
    ROCKET_HIDEOUT_B1F = 0xC7
    ROCKET_HIDEOUT_B2F = 0xC8
    ROCKET_HIDEOUT_B3F = 0xC9
    ROCKET_HIDEOUT_B4F = 0xCA
    ROCKET_HIDEOUT_ELEVATOR = 0xCB
    SILPH_CO_2F = 0xCF
    SILPH_CO_3F = 0xD0
    SILPH_CO_4F = 0xD1
    SILPH_CO_5F = 0xD2
    SILPH_CO_6F = 0xD3
    SILPH_CO_7F = 0xD4
    SILPH_CO_8F = 0xD5
    POKEMON_MANSION_2F = 0xD6
    POKEMON_MANSION_3F = 0xD7
    POKEMON_MANSION_B1F = 0xD8
    SAFARI_ZONE_EAST = 0xD9
    SAFARI_ZONE_NORTH = 0xDA
    SAFARI_ZONE_WEST = 0xDB
    SAFARI_ZONE_CENTER = 0xDC
    SAFARI_ZONE_CENTER_REST_HOUSE = 0xDD
    SAFARI_ZONE_SECRET_HOUSE = 0xDE
    SAFARI_ZONE_WEST_REST_HOUSE = 0xDF
    SAFARI_ZONE_EAST_REST_HOUSE = 0xE0
    SAFARI_ZONE_NORTH_REST_HOUSE = 0xE1
    CERULEAN_CAVE_2F = 0xE2
    CERULEAN_CAVE_B1F = 0xE3
    CERULEAN_CAVE_1F = 0xE4
    NAME_RATERS_HOUSE = 0xE5
    CERULEAN_BADGE_HOUSE = 0xE6
    ROCK_TUNNEL_B1F = 0xE8
    SILPH_CO_9F = 0xE9
    SILPH_CO_10F = 0xEA
    SILPH_CO_11F = 0xEB
    SILPH_CO_ELEVATOR = 0xEC
    TRADE_CENTER = 0xEF
    COLOSSEUM = 0xF0
    LORELEI = 0xF5
    BRUNO = 0xF6
    AGATHA = 0xF7

def get_location_name(value: int) -> str | None:
    """Return the enum name for a given int, or None if invalid."""
    try:
        return MapLocation(value).name
    except ValueError:
        return None