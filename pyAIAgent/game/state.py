import struct
import time
from pyAIAgent.game.graphics import dump_minimal_map, dump_minimap_map_array
from pyAIAgent.utils.socket_utils import readrange, send_command, _flush_socket
from pyAIAgent.utils.image_utils import capture
from pyAIAgent.game.data import get_species_map, get_location_name, decode_pokemon_text

DEFAULT_ROM = 'c.gbc'
MINI_MAP_SIZE = (21,21)

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
    map2D = ""

    if loc:
        mid, x, y, facing, mapName = loc
        dump_minimal_map(DEFAULT_ROM, mid, (x, y), grid_lines=True, crop=MINI_MAP_SIZE).save("minimap.png")
        map2D = dump_minimap_map_array(DEFAULT_ROM, mid, (x, y), crop=MINI_MAP_SIZE)
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
        "minimap_2d": map2D
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
