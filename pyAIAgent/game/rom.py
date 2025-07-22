import sys

def read_u8(data, offset):
    if offset < 0 or offset >= len(data):
        raise IndexError(f"read_u8 OOB: off={offset} len={len(data)}")
    return data[offset]

def read_u16(data, offset):
    if offset < 0 or offset + 1 >= len(data):
        raise IndexError(f"read_u16 OOB: off={offset} len={len(data)}")
    return data[offset] | (data[offset + 1] << 8)

def gb_to_file_offset(ptr, bank):
    if ptr < 0x4000:
        return ptr
    if bank < 0:
        raise ValueError(f"Invalid bank: {bank}")
    return (bank * 0x4000) + (ptr - 0x4000)

def load_map(rom, map_id):
    ptr_table, bank_table = 0x01AE, 0xC23D
    ptr_offset, bank_offset = ptr_table + map_id * 2, bank_table + map_id
    if ptr_offset + 1 >= len(rom) or bank_offset >= len(rom):
        raise ValueError(f"Map ID {map_id} out of table bounds.")
    ptr, bank = read_u16(rom, ptr_offset), read_u8(rom, bank_offset)
    num_banks = len(rom) // 0x4000
    if bank >= num_banks:
        raise ValueError(f"Map {map_id} bank {bank} out of ROM range ({num_banks}).")
    header_off = gb_to_file_offset(ptr, bank)
    if header_off + 5 > len(rom):
        raise ValueError(f"Map {map_id} header offset {header_off:06X} OOB.")
    tileset_id = read_u8(rom, header_off)
    height, width = read_u8(rom, header_off + 1), read_u8(rom, header_off + 2)
    map_data_ptr = read_u16(rom, header_off + 3)
    map_data_off = gb_to_file_offset(map_data_ptr, bank)
    if width <= 0 or height <= 0:
        raise ValueError(f"Map {map_id} invalid dimensions: {width}x{height}")
    expected_size = width * height
    size = min(expected_size, max(0, len(rom) - map_data_off))
    if size < expected_size:
        print(f"Warning: Map {map_id} data truncated ({size}/{expected_size}). Padding.", file=sys.stderr)
    map_data = rom[map_data_off: map_data_off + size] + b'\x00' * (expected_size - size)
    return tileset_id, width, height, map_data

def load_tileset_header(rom, tileset_id):
    base, header_size = 0xC7BE, 12
    header_table_offset = base + tileset_id * header_size
    if header_table_offset + header_size > len(rom):
        raise ValueError(f"Tileset {tileset_id} header offset OOB.")
    bank = read_u8(rom, header_table_offset)
    ptrs = {
        k: read_u16(rom, header_table_offset + off)
        for k, off in {"blocks": 1, "tiles": 3, "collision": 5, "interaction": 7}.items()
    }
    num_banks = len(rom) // 0x4000
    if bank >= num_banks:
        raise ValueError(f"Tileset {tileset_id} bank {bank} out of ROM range ({num_banks}).")
    return bank, ptrs["blocks"], ptrs["tiles"], ptrs["collision"], ptrs["interaction"]

def load_collision_data(rom, collision_ptr, bank):
    col_off = gb_to_file_offset(collision_ptr, bank)
    if col_off >= len(rom):
        raise ValueError("Collision pointer OOB.")
    collision = set()
    idx = col_off
    while idx < len(rom) and rom[idx] != 0xFF:
        collision.add(rom[idx])
        idx += 1
    return collision

def load_block_data(rom, blocks_ptr, bank, map_data):
    blk_off = gb_to_file_offset(blocks_ptr, bank)
    if blk_off >= len(rom):
        raise ValueError("Blocks pointer OOB.")
    max_bidx = max(map_data) if map_data else 0
    req_count = max_bidx + 1
    max_possible = max(0, (len(rom) - blk_off) // 16)
    count = min(req_count, max_possible)
    if count < req_count:
        print(f"Warning: Block data truncated ({count}/{req_count}).", file=sys.stderr)
    return [rom[blk_off + i * 16: blk_off + i * 16 + 16].ljust(16, b'\x00') for i in range(count)]

def load_tile_graphics(rom, tiles_ptr, bank, blocks, walkable_tiles):
    tile_off = gb_to_file_offset(tiles_ptr, bank)
    if tile_off >= len(rom):
        raise ValueError("Tiles pointer OOB.")
    max_id = max(walkable_tiles) if walkable_tiles else 0
    for b in blocks:
        max_id = max(max_id, max(b) if b else 0)
    max_possible = max(0, (len(rom) - tile_off) // 16)
    count = min(max_possible, max(max_id + 1, 128))  # Load used tiles + basics
    print(f"Max Tile ID used: {max_id}. Loading {count} tiles.", file=sys.stderr)
    tiles = []
    for i in range(count):
        d = rom[tile_off + i * 16: tile_off + i * 16 + 16]
        if len(d) < 16:
            print(f"Warning: ROM ended loading tiles ({i}/{count}).", file=sys.stderr)
            break
        tiles.append(d)
    if not tiles:
        print("Warning: No tile graphics loaded.", file=sys.stderr)
    return tiles
