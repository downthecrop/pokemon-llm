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

            label = f"#{dex_no:03} {mon_name}" if dex_no else mon_name
            lines.append(
                f" • Slot {slot+1}: {label} — Types: {types} — "
                f"'{nickname}', lvl {level}, HP {hp_cur}/{hp_max}"
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
        "party": get_party_text(sock),
        "state": get_state(sock),
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
        0x04: (35, "Clefairy", "Normal", None), # Note: Fairy type added later
        0x8E: (36, "Clefable", "Normal", None), # Note: Fairy type added later
        0x52: (37, "Vulpix", "Fire", None),
        0x53: (38, "Ninetales", "Fire", None),
        0x64: (39, "Jigglypuff", "Normal", None), # Note: Fairy type added later
        0x65: (40, "Wigglytuff", "Normal", None), # Note: Fairy type added later
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
        0xAD: (81, "Magnemite", "Electric", None), # Note: Steel type added later
        0x36: (82, "Magneton", "Electric", None), # Note: Steel type added later
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
        0x2A: (122, "Mr. Mime", "Psychic", None), # Note: Fairy type added later
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