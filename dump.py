#!/usr/bin/env python3
import argparse
from collections import deque
from PIL import Image, ImageDraw, ImageFont
import sys

__all__ = ["find_path", "dump_minimal_map"]

SPECIAL_FEATURE_TILE_IDS = {
    0x04, 0x05, 0x0C, 0x0D, 0x14, 0x15, 0x1C, 0x1D, 0x64, 0x65, 0x6C, 0x6D,
    0x66, 0x67, 0x6E, 0x6F, 0x7B, 0x5A, 0x5B, 0x5C, 0x5D, 0x30, 0x31, 0x32,
    0x33, 0x3A, 0x3B, 0x70, 0x71, 0x78, 0x79, 0x0E, 0x0F, 0x82, 0x83, 0x0A,
    0x0B, 0x1A, 0x1B,
}

# --- Low-level ROM helpers ---
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

# --- Map/Tileset Loading ---
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

# --- Graphics & Grid Logic ---
def decode_tile(tile_bytes):
    if len(tile_bytes) < 16:
        tile_bytes += b'\x00' * (16 - len(tile_bytes))
    pixels = [[0] * 8 for _ in range(8)]
    for r in range(8):
        p0, p1 = tile_bytes[r], tile_bytes[r + 8]
        for c in range(8):
            pixels[r][c] = ((p1 >> (7 - c)) & 1) << 1 | ((p0 >> (7 - c)) & 1)
    return pixels

def build_quadrant_walkability(width, height, map_data, blocks, walkable_tiles):
    cols, rows = width * 2, height * 2
    grid = [[False] * cols for _ in range(rows)]
    for by in range(height):
        for bx in range(width):
            map_idx = by * width + bx
            if map_idx >= len(map_data):
                continue
            bidx = map_data[map_idx]
            if bidx >= len(blocks):
                continue
            subtiles = blocks[bidx]
            if len(subtiles) < 16:
                continue
            for qr in range(2):
                for qc in range(2):
                    col_idx = (qr * 2 + 1) * 4 + (qc * 2 + 0)
                    if col_idx >= len(subtiles):
                        continue
                    gy, gx = by * 2 + qr, bx * 2 + qc
                    if 0 <= gy < rows and 0 <= gx < cols:
                        grid[gy][gx] = (subtiles[col_idx] in walkable_tiles)
    return grid

def calculate_walkable_special_quadrants(width, height, map_data, blocks, grid_data, debug_tiles=False):
    special_quadrants = set()
    if not grid_data or not grid_data[0]:
        return special_quadrants
    grid_h, grid_w = len(grid_data), len(grid_data[0])
    if debug_tiles:
        print("Scanning for WALKABLE special quadrants & tile IDs...", file=sys.stderr)

    for by in range(height):
        for bx in range(width):
            map_idx = by * width + bx
            if map_idx >= len(map_data) or map_data[map_idx] >= len(blocks):
                continue
            bidx = map_data[map_idx]
            block_def = blocks[bidx]
            if len(block_def) < 16:
                continue

            for gqy in range(2):
                for gqx in range(2):
                    gx, gy = bx * 2 + gqx, by * 2 + gqy
                    if not (0 <= gy < grid_h and 0 <= gx < grid_w):
                        continue

                    is_walkable = grid_data[gy][gx]
                    indices = [(gqy * 2 + r) * 4 + (gqx * 2 + c) for r in range(2) for c in range(2)]
                    tile_ids = [block_def[i] if i < len(block_def) else None for i in indices]

                    is_special = (
                        all(tid in SPECIAL_FEATURE_TILE_IDS for tid in tile_ids if tid is not None)
                        and None not in tile_ids
                    )

                    if debug_tiles:
                        tiles_str = ", ".join(
                            [f"0x{tid:02X}" if tid is not None else "N/A" for tid in tile_ids]
                        )
                        walk_str = "Walkable" if is_walkable else "Blocked"
                        special_str = (
                            "Special"
                            if is_special
                            else ("Partial" if any(tid in SPECIAL_FEATURE_TILE_IDS for tid in tile_ids if tid is not None) else "Normal")
                        )
                        print(f"DEBUG: ({gx:>2},{gy:>2}) Blk({bx},{by}) ID 0x{bidx:02X} -> [{tiles_str}] ({walk_str}, {special_str})", file=sys.stderr)

                    if is_special and is_walkable:
                        special_quadrants.add((gx, gy))
                        if debug_tiles:
                            print(f"DEBUG: -> Added ({gx},{gy})", file=sys.stderr)

    return special_quadrants

# --- Pathfinding ---
def _bfs_find_path(grid, start, end):
    if not grid or not grid[0]:
        return None
    rows, cols = len(grid), len(grid[0])
    sx, sy = start; ex, ey = end
    oob = lambda x, y: not (0 <= x < cols and 0 <= y < rows)
    if oob(sx, sy):
        print(f"Error: Start {start} OOB ({cols}x{rows})", file=sys.stderr)
        return None
    if oob(ex, ey):
        print(f"Error: End {end} OOB ({cols}x{rows})", file=sys.stderr)
        return None
    if not grid[sy][sx]:
        print(f"Warning: Start {start} blocked.", file=sys.stderr)
    if not grid[ey][ex]:
        print(f"Warning: End {end} blocked.", file=sys.stderr)
        return None

    queue = deque([(sx, sy)])
    prev = {(sx, sy): None}
    dirs = [(1, 0, 'R'), (-1, 0, 'L'), (0, 1, 'D'), (0, -1, 'U')]

    while queue:
        x, y = queue.popleft()
        if (x, y) == (ex, ey):
            break
        for dx, dy, action in dirs:
            nx, ny = x + dx, y + dy
            if not oob(nx, ny) and grid[ny][nx] and (nx, ny) not in prev:
                prev[(nx, ny)] = (x, y, action)
                queue.append((nx, ny))
    else:
        return None

    actions, coords = [], []
    curr = (ex, ey)
    while prev[curr] is not None:
        coords.append(curr)
        px, py, action = prev[curr]
        actions.append(action)
        curr = (px, py)
    coords.append(start)
    return ''.join(reversed(actions)), list(reversed(coords))

# --- Public API ---
def find_path(rom_path, map_id, start, end):
    """Finds shortest path actions string between two points."""
    try:
        rom = open(rom_path, 'rb').read()
        tileset_id, width, height, map_data = load_map(rom, map_id)
        bank, blocks_ptr, _, collision_ptr, _ = load_tileset_header(rom, tileset_id)
        walkable_tiles = load_collision_data(rom, collision_ptr, bank)
        blocks = load_block_data(rom, blocks_ptr, bank, map_data)
        grid = build_quadrant_walkability(width, height, map_data, blocks, walkable_tiles)
        result = _bfs_find_path(grid, start, end)
        return (';'.join(result[0]) + ';') if result else None
    except (FileNotFoundError, IOError) as e:
        print(f"Error reading ROM '{rom_path}': {e}", file=sys.stderr)
        return None
    except (ValueError, IndexError) as e:
        print(f"Error processing data: {e}", file=sys.stderr)
        return None
    except Exception as e:
        print(f"Unexpected error in find_path: {e}", file=sys.stderr)
        return None

def dump_minimal_map(rom_path, map_id, pos=None, grid_lines=False, debug_coords=False, debug_tiles=False, crop=None):
    """
    Dumps minimal map (walkability/special) with optional overlays and cropping.

    Args:
        rom_path (str): Path to ROM file.
        map_id (int): Map ID.
        pos (tuple[int,int] or None): Grid-quadrant to mark (gx, gy).
        grid_lines (bool): Whether to draw grid lines.
        debug_coords (bool): Whether to overlay coordinate text.
        debug_tiles (bool): Whether to print tile IDs during processing.
        crop (tuple[int,int] or None): If provided, crop width,height around `pos` in quadrants.

    Returns:
        PIL.Image.Image or None: The generated (and possibly cropped) image.
    """
    try:
        rom = open(rom_path, 'rb').read()
        tileset_id, width, height, map_data = load_map(rom, map_id)
        bank, blocks_ptr, _, collision_ptr, _ = load_tileset_header(rom, tileset_id)
        walkable_tiles = load_collision_data(rom, collision_ptr, bank)
        blocks = load_block_data(rom, blocks_ptr, bank, map_data)
        grid_data = build_quadrant_walkability(width, height, map_data, blocks, walkable_tiles)
        if not grid_data or not grid_data[0]:
            raise ValueError("Failed to build walkability grid.")
        grid_h, grid_w = len(grid_data), len(grid_data[0])

        walkable_special = calculate_walkable_special_quadrants(
            width, height, map_data, blocks, grid_data, debug_tiles
        )

        cell_size = 16
        img_w, img_h = grid_w * cell_size, grid_h * cell_size
        if img_w <= 0 or img_h <= 0:
            raise ValueError(f"Invalid image dims: {img_w}x{img_h}")

        img = Image.new('RGB', (img_w, img_h))
        draw = ImageDraw.Draw(img)

        colors = {
            'walk': (255, 255, 255),
            'block': (0, 0, 0),
            'special': (255, 165, 0),
            'marker': (0, 0, 255),
            'grid': (100, 100, 100),
            'debug_text': (0, 0, 255),
        }

        font = None
        if debug_coords:
            try:
                font = ImageFont.load_default(size=max(8, min(12, cell_size // 2 - 2)))
            except:
                font = ImageFont.load_default()

        # Draw walkability & special
        for y in range(grid_h):
            for x in range(grid_w):
                is_walkable = grid_data[y][x]
                is_special = (x, y) in walkable_special
                color = (
                    colors['special']
                    if is_special
                    else (colors['walk'] if is_walkable else colors['block'])
                )
                x0, y0 = x * cell_size, y * cell_size
                draw.rectangle(
                    [x0, y0, x0 + cell_size - 1, y0 + cell_size - 1],
                    fill=color
                )
                if debug_coords and font:
                    draw.text((x0 + 2, y0 + 1), f"{x},{y}", font=font, fill=colors['debug_text'])

        # Overlay grid lines if requested
        if grid_lines or debug_coords:
            for x_line in range(0, img_w, cell_size):
                draw.line([(x_line, 0), (x_line, img_h - 1)], fill=colors['grid'])
            for y_line in range(0, img_h, cell_size):
                draw.line([(0, y_line), (img_w - 1, y_line)], fill=colors['grid'])

        # Draw marker if pos provided
        if pos:
            px, py = pos
            if 0 <= px < grid_w and 0 <= py < grid_h:
                cx, cy = px * cell_size + cell_size // 2, py * cell_size + cell_size // 2
                radius = cell_size // 2 - 3
                draw.ellipse(
                    [(cx - radius, cy - radius), (cx + radius, cy + radius)],
                    fill=colors['marker'],
                    outline=colors['marker']
                )
            else:
                print(
                    f"Warning: Marker pos {pos} OOB ({grid_w}x{grid_h}).",
                    file=sys.stderr
                )

        # --- Cropping Logic inside dump_minimal_map ---
        if crop:
            if not pos:
                print("Warning: Cannot crop without `pos` in dump_minimal_map.", file=sys.stderr)
            else:
                try:
                    crop_w, crop_h = crop
                    half_w = crop_w // 2
                    half_h = crop_h // 2

                    left = pos[0] - half_w
                    right = pos[0] + half_w
                    top = pos[1] - half_h
                    bottom = pos[1] + half_h

                    # Clamp to grid boundaries
                    left = max(0, left)
                    right = min(grid_w - 1, right)
                    top = max(0, top)
                    bottom = min(grid_h - 1, bottom)

                    left_px = left * cell_size
                    top_px = top * cell_size
                    right_px = (right + 1) * cell_size
                    bottom_px = (bottom + 1) * cell_size

                    print(
                        f"[dump_minimal_map] Cropping to grid region x[{left}:{right}] "
                        f"y[{top}:{bottom}] -> px box ({left_px},{top_px},{right_px},{bottom_px})",
                        file=sys.stderr
                    )
                    img = img.crop((left_px, top_px, right_px, bottom_px))
                except Exception as e:
                    print(f"Warning: Invalid `crop` in dump_minimal_map or error cropping: {e}", file=sys.stderr)

        return img

    except (FileNotFoundError, IOError) as e:
        print(f"Error reading ROM '{rom_path}': {e}", file=sys.stderr)
        return None
    except (ValueError, IndexError) as e:
        print(f"Error processing minimal map: {e}", file=sys.stderr)
        return None
    except Exception as e:
        print(f"Unexpected error in dump_minimal_map: {e}", file=sys.stderr)
        return None


def dump_minimap_map_array(rom_path, map_id, pos=None, crop=None):
    """
    Dumps a minimal map as a semicolon-separated 2D array string.

    Each cell is represented by:
        'W' for walkable
        'B' for non-walkable (block)
        'O' for special (walkable but marked special)
        'P' for the player marker (overrides other symbols)

    Rows are joined by ';'. For example:
        "BBBWWWPWWO;WWWWWBWWWW;..."

    Args:
        rom_path (str): Path to ROM file.
        map_id (int): Map ID.
        pos (tuple[int,int] or None): Grid-quadrant to mark (gx, gy) as 'P'.
        crop (tuple[int,int] or None): If provided, crop width,height around `pos` in quadrants.

    Returns:
        str or None: Semicolon-separated rows string, or None on error.
    """
    try:
        rom = open(rom_path, 'rb').read()
        tileset_id, width, height, map_data = load_map(rom, map_id)
        bank, blocks_ptr, _, collision_ptr, _ = load_tileset_header(rom, tileset_id)
        walkable_tiles = load_collision_data(rom, collision_ptr, bank)
        blocks = load_block_data(rom, blocks_ptr, bank, map_data)
        grid_data = build_quadrant_walkability(width, height, map_data, blocks, walkable_tiles)
        if not grid_data or not grid_data[0]:
            raise ValueError("Failed to build walkability grid.")
        grid_h, grid_w = len(grid_data), len(grid_data[0])

        walkable_special = calculate_walkable_special_quadrants(
            width, height, map_data, blocks, grid_data, debug_tiles=False
        )

        # Determine cropping bounds in grid coordinates
        if crop:
            if not pos:
                print("Warning: Cannot crop without `pos` in dump_minimap_map_array.", file=sys.stderr)
                # Fall back to full grid if pos is missing
                left, right, top, bottom = 0, grid_w - 1, 0, grid_h - 1
            else:
                try:
                    crop_w, crop_h = crop
                    half_w = crop_w // 2
                    half_h = crop_h // 2

                    left = pos[0] - half_w
                    right = pos[0] + half_w
                    top = pos[1] - half_h
                    bottom = pos[1] + half_h

                    # Clamp to grid boundaries
                    left = max(0, left)
                    right = min(grid_w - 1, right)
                    top = max(0, top)
                    bottom = min(grid_h - 1, bottom)
                except Exception as e:
                    print(f"Warning: Invalid `crop` in dump_minimap_map_array or error computing bounds: {e}", file=sys.stderr)
                    left, right, top, bottom = 0, grid_w - 1, 0, grid_h - 1
        else:
            left, right, top, bottom = 0, grid_w - 1, 0, grid_h - 1

        rows = []
        for y in range(top, bottom + 1):
            row_chars = []
            for x in range(left, right + 1):
                # Player marker takes precedence
                if pos and x == pos[0] and y == pos[1]:
                    row_chars.append('P')
                else:
                    is_special = (x, y) in walkable_special
                    is_walkable = grid_data[y][x]
                    if is_special:
                        row_chars.append('O')
                    elif is_walkable:
                        row_chars.append('W')
                    else:
                        row_chars.append('B')
                # (Debug: could print tile IDs if desired, but omitted here)
            rows.append("".join(row_chars))

        return ";".join(rows)

    except (FileNotFoundError, IOError) as e:
        print(f"Error reading ROM '{rom_path}': {e}", file=sys.stderr)
        return None
    except (ValueError, IndexError) as e:
        print(f"Error processing minimal map array: {e}", file=sys.stderr)
        return None
    except Exception as e:
        print(f"Unexpected error in dump_minimap_map_array: {e}", file=sys.stderr)
        return None


# --- CLI Wrapper ---
def main():
    parser = argparse.ArgumentParser(
        description="Pokémon Red/Blue map tool: Render, pathfind, highlight, and optional cropping."
    )
    parser.add_argument('rom', help='Path to ROM file')
    parser.add_argument('map_id', type=int, help='Map ID')
    parser.add_argument('--start', '-s', help='Start gx,gy for pathfinding')
    parser.add_argument('--end', '-e', help='End gx,gy for pathfinding')
    parser.add_argument('--output', '-o', default='map.png', help='Output image file (map.png)')
    parser.add_argument('--debug', '-d', action='store_true',
                        help='Overlay grid lines and coordinates')
    parser.add_argument('--pos', help='Mark gx,gy with a blue circle')
    parser.add_argument('--crop', help='Crop width,height around pos in grid units (quadrants)')
    parser.add_argument('--minimal', '-m', action='store_true',
                        help='Generate minimal B/W/Orange walkability map')
    parser.add_argument('--debug-tiles', action='store_true',
                        help='Print tile IDs per quadrant during processing')
    args = parser.parse_args()

    path_result = None  # Stores (actions_str, path_coords)

    try:
        print(f"Loading ROM: {args.rom}", file=sys.stderr)
        rom = open(args.rom, 'rb').read()
        print(f"Loading Map ID: {args.map_id}", file=sys.stderr)
        tileset_id, width, height, map_data = load_map(rom, args.map_id)
        print(f"Map: {width}x{height} blocks ({width*2}x{height*2} quads), Tileset: {tileset_id}", file=sys.stderr)
        bank, blocks_ptr, tiles_ptr, collision_ptr, _ = load_tileset_header(rom, tileset_id)
        print(f"Tileset Header: Bank ${bank:02X}, Blocks ${blocks_ptr:04X}, Tiles ${tiles_ptr:04X}, Collision ${collision_ptr:04X}", file=sys.stderr)

        walkable_tiles = load_collision_data(rom, collision_ptr, bank)
        blocks = load_block_data(rom, blocks_ptr, bank, map_data)
        print("Building walkability grid...", file=sys.stderr)
        grid = build_quadrant_walkability(width, height, map_data, blocks, walkable_tiles)
        if not grid:
            print("Warning: Failed to build walkability grid.", file=sys.stderr)
        grid_h, grid_w = (height * 2, width * 2) if grid else (0, 0)

        walkable_special = calculate_walkable_special_quadrants(
            width, height, map_data, blocks, grid, args.debug_tiles
        )

    except (FileNotFoundError, IOError) as e:
        print(f"Error reading ROM: {e}", file=sys.stderr)
        return
    except (ValueError, IndexError) as e:
        print(f"Error loading map/tileset: {e}", file=sys.stderr)
        return
    except Exception as e:
        print(f"Unexpected error loading data: {e}", file=sys.stderr)
        return

    img = None

    # Parse --pos into a tuple if present
    pos_tuple = None
    if args.pos:
        try:
            pos_tuple = tuple(map(int, args.pos.split(',')))
        except:
            print(f"Warning: Invalid --pos format '{args.pos}'.", file=sys.stderr)

    # Parse --crop into a tuple if present
    crop_tuple = None
    if args.crop:
        try:
            crop_tuple = tuple(map(int, args.crop.split(',')))
        except:
            print(f"Warning: Invalid --crop format '{args.crop}'.", file=sys.stderr)

    if args.minimal:
        print(f"Generating minimal map...", file=sys.stderr)

        img = dump_minimal_map(
            args.rom,
            args.map_id,
            pos=pos_tuple,
            grid_lines=args.debug,
            debug_coords=args.debug,
            debug_tiles=args.debug_tiles,
            crop=crop_tuple
        )
        if img is None:
            print("Failed to generate minimal map.", file=sys.stderr)
            return

    else:
        print(f"Generating full map render...", file=sys.stderr)
        try:
            tiles = load_tile_graphics(rom, tiles_ptr, bank, blocks, walkable_tiles)
            img_w, img_h = width * 32, height * 32
            if img_w <= 0 or img_h <= 0:
                raise ValueError("Invalid image dimensions.")

            base = Image.new('P', (img_w, img_h))
            base.putpalette([255, 255, 255, 192, 192, 192, 96, 96, 96, 0, 0, 0] + [0] * 756)
            special_overlay = Image.new('RGBA', (img_w, img_h), (0, 0, 0, 0))
            special_draw = ImageDraw.Draw(special_overlay)
            walk_overlay = Image.new('RGBA', (img_w, img_h), (0, 0, 0, 0))
            walk_draw = ImageDraw.Draw(walk_overlay)

            orange_highlight = (255, 165, 0, 150)
            red_overlay = (255, 0, 0, 100)
            font = None
            if args.debug:
                try:
                    font = ImageFont.load_default(size=8)
                except:
                    font = ImageFont.load_default()

            print("Rendering base tiles and overlays...", file=sys.stderr)
            for by in range(height):
                for bx in range(width):
                    map_idx = by * width + bx
                    if map_idx >= len(map_data) or map_data[map_idx] >= len(blocks):
                        continue
                    bidx = map_data[map_idx]
                    block_def = blocks[bidx]
                    if len(block_def) < 16:
                        continue

                    for i, tid in enumerate(block_def):
                        if tid < len(tiles):
                            pixels = decode_tile(tiles[tid])
                            tx, ty = bx * 32 + (i % 4) * 8, by * 32 + (i // 4) * 8
                            for r in range(8):
                                for c in range(8):
                                    px, py = tx + c, ty + r
                                    if 0 <= px < img_w and 0 <= py < img_h:
                                        base.putpixel((px, py), pixels[r][c])

                    for gqy in range(2):
                        for gqx in range(2):
                            gx, gy = bx * 2 + gqx, by * 2 + gqy
                            if not (0 <= gy < grid_h and 0 <= gx < grid_w):
                                continue

                            q_x0, q_y0 = gx * 16, gy * 16

                            if (gx, gy) in walkable_special:
                                special_draw.rectangle(
                                    [q_x0, q_y0, q_x0 + 15, q_y0 + 15],
                                    fill=orange_highlight
                                )

                            if args.debug and not grid[gy][gx]:
                                walk_draw.rectangle(
                                    [q_x0, q_y0, q_x0 + 15, q_y0 + 15],
                                    fill=red_overlay
                                )

            print("Compositing layers...", file=sys.stderr)
            img_rgba = base.convert('RGBA')
            img_rgba = Image.alpha_composite(img_rgba, special_overlay)
            img_rgba = Image.alpha_composite(img_rgba, walk_overlay)
            img = img_rgba

            start_coord, end_coord = None, None
            try:
                if args.start:
                    start_coord = tuple(map(int, args.start.split(',')))
                if args.end:
                    end_coord = tuple(map(int, args.end.split(',')))
                if not start_coord and end_coord and pos_tuple:
                    start_coord = pos_tuple
            except:
                print("Warning: Invalid coordinate format.", file=sys.stderr)

            if start_coord and end_coord and grid:
                print(f"Finding path {start_coord} -> {end_coord}...", file=sys.stderr)
                path_result = _bfs_find_path(grid, start_coord, end_coord)
                if path_result:
                    actions, coords = path_result
                    print("Path Actions:", ';'.join(actions) + ';')
                    print("Drawing path...", file=sys.stderr)
                    pd = ImageDraw.Draw(img)
                    pts = [(x * 16 + 8, y * 16 + 8) for x, y in coords]
                    if len(pts) > 1:
                        pd.line(pts, fill=(0, 255, 0, 200), width=5)
                else:
                    print("Path not found.", file=sys.stderr)
            elif args.start or args.end:
                print("Warning: Pathfinding needs --start/--pos and --end.", file=sys.stderr)

            if pos_tuple:
                try:
                    px, py = pos_tuple
                    if grid and 0 <= px < grid_w and 0 <= py < grid_h:
                        print(f"Drawing marker at ({px},{py})...", file=sys.stderr)
                        md = ImageDraw.Draw(img)
                        cx, cy = px * 16 + 8, py * 16 + 8
                        radius = 7
                        md.ellipse(
                            [(cx - radius, cy - radius), (cx + radius, cy + radius)],
                            fill=(0, 0, 255, 180),
                            outline=(255, 255, 255, 220),
                            width=2
                        )
                    else:
                        print(f"Warning: Marker pos {pos_tuple} OOB or grid missing.", file=sys.stderr)
                except:
                    print(f"Warning: Invalid --pos format '{args.pos}'.", file=sys.stderr)

            if args.debug:
                print("Drawing debug grid/coordinates...", file=sys.stderr)
                gd = ImageDraw.Draw(img)
                line_col = (50, 50, 50, 100)
                txt_col = (200, 200, 255, 220)
                for x in range(0, img_w, 16):
                    gd.line([(x, 0), (x, img_h - 1)], fill=line_col)
                for y in range(0, img_h, 16):
                    gd.line([(0, y), (img_w - 1, y)], fill=line_col)
                if grid and font:
                    for gy in range(grid_h):
                        for gx in range(grid_w):
                            gd.text((gx * 16 + 1, gy * 16), f"{gx},{gy}", font=font, fill=txt_col)

        except (ValueError, IndexError) as e:
            print(f"Error during full render: {e}", file=sys.stderr)
            return
        except Exception as e:
            print(f"Unexpected error during full render: {e}", file=sys.stderr)
            return

    # --- Cropping Logic for full render only ---
    if img and crop_tuple and not args.minimal:
        if not pos_tuple:
            print("Warning: Cannot crop without --pos option.", file=sys.stderr)
        else:
            try:
                crop_w, crop_h = crop_tuple
                half_w = crop_w // 2
                half_h = crop_h // 2

                left = pos_tuple[0] - half_w
                right = pos_tuple[0] + half_w
                top = pos_tuple[1] - half_h
                bottom = pos_tuple[1] + half_h

                left = max(0, left)
                right = min(grid_w - 1, right)
                top = max(0, top)
                bottom = min(grid_h - 1, bottom)

                cell_size = 16
                left_px = left * cell_size
                top_px = top * cell_size
                right_px = (right + 1) * cell_size
                bottom_px = (bottom + 1) * cell_size

                print(
                    f"[full render] Cropping to grid region x[{left}:{right}] "
                    f"y[{top}:{bottom}] -> px box ({left_px},{top_px},{right_px},{bottom_px})",
                    file=sys.stderr
                )
                img = img.crop((left_px, top_px, right_px, bottom_px))
            except Exception as e:
                print(f"Warning: Invalid --crop format or error cropping: {e}", file=sys.stderr)

    # --- Save Output ---
    if img:
        try:
            needs_rgba = not args.minimal or args.debug or args.pos or args.crop
            save_mode = 'RGBA' if needs_rgba else 'RGB'
            if img.mode != save_mode:
                print(f"Converting image from {img.mode} to {save_mode} for saving.", file=sys.stderr)
                img = img.convert(save_mode)

            img.save(args.output)
            mode = "Minimal map" if args.minimal else "Map image"
            path = "with path" if path_result and not args.minimal else ""
            print(f"Saved {mode} {path} ({img.mode}) to {args.output}", file=sys.stderr)
        except Exception as e:
            print(f"Error saving image to {args.output}: {e}", file=sys.stderr)
    else:
        print("No image generated.", file=sys.stderr)

if __name__ == '__main__':
    main()
