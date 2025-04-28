#!/usr/bin/env python3
import argparse
from collections import deque
from PIL import Image, ImageDraw, ImageFont

__all__ = ["find_path", "dump_minimal_map"]

# --- Define Special Tile IDs at Module Level ---
SPECIAL_FEATURE_TILE_IDS = {
    # Doors (Examples from common town/building tilesets)
    0x04, 0x05, 0x0C, 0x0D, 0x14, 0x15, 0x1C, 0x1D, 0x64, 0x65, 0x6C, 0x6D,
    0x66, 0x67, 0x6E, 0x6F, 0x7B,
    # Mats (Examples from PokeCenter/Mart)
    0x5A, 0x5B, 0x5C, 0x5D,
    # Stairs (Examples from Caves/Buildings)
    0x30, 0x31, # Down pair (left/right) - Often on left wall
    0x32, 0x33, # Up pair (left/right)   - Often on right wall
    0x3A, 0x3B, # Building stairs? Misc features?
    # Ladders (Examples from Caves/Towers)
    0x70, 0x71, 0x78, 0x79,
    # Some overworld cave entrances might use these?
    0x0E, 0x0F,
    # Potentially warp tiles in buildings? (need verification)
    0x82, 0x83,
    # Stairs identified at (7,1) on a specific map (user request)
    0x0A, 0x0B, 0x1A, 0x1B,
}

# --- Low-level ROM helpers ---
# [No changes here]
def read_u8(data, offset):
    if offset < 0 or offset >= len(data):
        raise IndexError(f"read_u8: offset {offset} out of bounds for data length {len(data)}")
    return data[offset]

def read_u16(data, offset):
    if offset < 0 or offset + 1 >= len(data):
        raise IndexError(f"read_u16: offset {offset} out of bounds for data length {len(data)}")
    return data[offset] | (data[offset+1] << 8)

def gb_to_file_offset(ptr, bank):
    if ptr < 0x4000:
        return ptr
    if bank < 0:
        raise ValueError(f"Invalid negative bank number: {bank}")
    return (bank * 0x4000) + (ptr - 0x4000)

# --- Map loading ---
# [No changes here]
def load_map(rom, map_id):
    ptr_table  = 0x01AE
    bank_table = 0xC23D
    ptr_offset = ptr_table + map_id * 2
    bank_offset = bank_table + map_id
    if ptr_offset + 1 >= len(rom) or bank_offset >= len(rom):
        raise ValueError(f"Map ID {map_id} results in table offsets outside ROM bounds.")
    ptr = read_u16(rom, ptr_offset)
    bank = read_u8(rom, bank_offset)
    num_banks = len(rom) // 0x4000
    if bank >= num_banks:
         raise ValueError(f"Map {map_id} header bank {bank} is out of range for ROM size {len(rom)} ({num_banks} banks).")
    header_off = gb_to_file_offset(ptr, bank)
    if header_off + 5 > len(rom):
         raise ValueError(f"Map {map_id} header offset {header_off:06X} (Ptr ${ptr:04X} Bank ${bank:02X}) is out of bounds.")
    tileset_id = read_u8(rom, header_off)
    height = read_u8(rom, header_off+1)
    width = read_u8(rom, header_off+2)
    map_data_ptr = read_u16(rom, header_off+3)
    map_data_off = gb_to_file_offset(map_data_ptr, bank)

    if width <= 0 or height <= 0:
        raise ValueError(f"Map {map_id} has invalid dimensions: {width}x{height}")

    expected_size = width * height
    if map_data_off + expected_size > len(rom):
        actual_readable_size = len(rom) - map_data_off
        print(f"Warning: Map {map_id} data offset {map_data_off:06X} expected size {expected_size} exceeds ROM bounds ({len(rom)}). Reading only {actual_readable_size} bytes.")
        size = max(0, actual_readable_size)
    else:
        size = expected_size

    map_data = rom[map_data_off : map_data_off + size]
    if len(map_data) < expected_size:
        print(f"Warning: Map data for {map_id} is shorter ({len(map_data)}) than expected ({expected_size}). Padding with 0x00.")
        map_data += b'\x00' * (expected_size - len(map_data))

    return tileset_id, width, height, map_data

# --- Tileset header loading ---
# [No changes here]
def load_tileset_header(rom, tileset_id):
    base = 0xC7BE
    header_size = 12
    header_table_offset = base + tileset_id * header_size
    if header_table_offset + header_size > len(rom):
        raise ValueError(f"Tileset ID {tileset_id} results in header offset outside ROM bounds.")
    tileset_bank = read_u8(rom, header_table_offset)
    blocks_ptr = read_u16(rom, header_table_offset + 1)
    tiles_ptr = read_u16(rom, header_table_offset + 3)
    collision_ptr = read_u16(rom, header_table_offset + 5)
    interaction_ptr = read_u16(rom, header_table_offset + 7) # Interaction pointer used by some tools
    num_banks = len(rom) // 0x4000
    if tileset_bank >= num_banks:
         raise ValueError(f"Tileset {tileset_id} header bank {tileset_bank} is out of range for ROM size {len(rom)} ({num_banks} banks).")
    return tileset_bank, blocks_ptr, tiles_ptr, collision_ptr, interaction_ptr

# --- Decode a single 8×8 tile ---
# [No changes here]
def decode_tile(tile_bytes):
    if len(tile_bytes) < 16: tile_bytes = tile_bytes + b'\x00' * (16 - len(tile_bytes))
    elif len(tile_bytes) > 16: tile_bytes = tile_bytes[:16]
    pixels = [[0]*8 for _ in range(8)]
    for row in range(8):
        plane0_byte = tile_bytes[row]
        plane1_byte = tile_bytes[row + 8]
        for bit_pos in range(8):
            bit_index = 7 - bit_pos
            low = (plane0_byte >> bit_index) & 1
            high = (plane1_byte >> bit_index) & 1
            color_index = (high << 1) | low
            pixels[row][bit_pos] = color_index
    return pixels

# --- Build 2×2 quadrant walkability grid ---
# [No changes here]
def build_quadrant_walkability(width, height, map_data, blocks, walkable_tiles):
    cols = width * 2
    rows = height * 2
    grid = [[False]*cols for _ in range(rows)]
    for by in range(height):
        for bx in range(width):
            map_idx = by * width + bx
            if map_idx >= len(map_data): continue
            bidx = map_data[map_idx]
            if bidx >= len(blocks): continue
            subtiles = blocks[bidx]
            if len(subtiles) < 16: continue
            for qr in range(2):
                for qc in range(2):
                    collision_tile_index_in_block = (qr * 2 + 1) * 4 + (qc * 2 + 0)
                    if collision_tile_index_in_block >= len(subtiles): continue
                    tid = subtiles[collision_tile_index_in_block]
                    grid_y, grid_x = by*2+qr, bx*2+qc
                    if 0 <= grid_y < rows and 0 <= grid_x < cols:
                        grid[grid_y][grid_x] = (tid in walkable_tiles)
    return grid

# --- BFS pathfinder ---
# [No changes here]
def _bfs_find_path(grid, start, end):
    if not grid or not grid[0]: return None
    rows, cols = len(grid), len(grid[0])
    sx, sy = start; ex, ey = end
    if not (0 <= sx < cols and 0 <= sy < rows): print(f"Error: Start {start} out of grid bounds ({cols}x{rows})"); return None
    if not (0 <= ex < cols and 0 <= ey < rows): print(f"Error: End {end} out of grid bounds ({cols}x{rows})"); return None
    if not grid[sy][sx]: print(f"Warning: Start position {start} is not walkable.")
    if not grid[ey][ex]: print(f"Warning: End position {end} is not walkable."); return None
    queue = deque([(sx, sy)])
    prev = {(sx, sy): None}
    dirs = [(1, 0, 'R'), (-1, 0, 'L'), (0, 1, 'D'), (0, -1, 'U')]
    path_found = False
    while queue:
        x, y = queue.popleft()
        if (x, y) == (ex, ey): path_found = True; break
        for dx, dy, action in dirs:
            nx, ny = x + dx, y + dy
            if 0 <= nx < cols and 0 <= ny < rows and grid[ny][nx] and (nx, ny) not in prev:
                prev[(nx, ny)] = (x, y, action)
                queue.append((nx, ny))
    if not path_found: return None
    path_actions = []
    path_coords = []
    cur = (ex, ey)
    while prev[cur] is not None:
        px, py, action = prev[cur]
        path_actions.append(action)
        path_coords.append(cur)
        cur = (px, py)
    path_coords.append(start)
    path_actions.reverse()
    path_coords.reverse()
    actions_string = ''.join(path_actions)
    return actions_string, path_coords

# --- Public API ---
# [No changes here]
def find_path(rom_path, map_id, start, end):
    try:
        rom = open(rom_path, 'rb').read()
        tileset_id, width, height, map_data = load_map(rom, map_id)
        bank, blocks_ptr, tiles_ptr, collision_ptr, _ = load_tileset_header(rom, tileset_id)
        col_off = gb_to_file_offset(collision_ptr, bank)
        if col_off >= len(rom): raise ValueError("Collision pointer outside ROM.")
        collision = []
        idx = col_off
        while idx < len(rom):
            v = rom[idx]; idx += 1
            if v == 0xFF: break
            collision.append(v)
        walkable_tiles = set(collision)
        blk_off = gb_to_file_offset(blocks_ptr, bank)
        if blk_off >= len(rom): raise ValueError("Blocks pointer outside ROM.")
        max_bidx = 0
        if map_data: max_bidx = max(map_data)
        block_count = max_bidx + 1
        if blk_off + block_count * 16 > len(rom):
            block_count = max(0, (len(rom) - blk_off) // 16)
            print(f"Warning: Block data might be truncated (count {block_count}) in find_path.")
        blocks = [rom[blk_off+i*16:blk_off+i*16+16].ljust(16, b'\x00') for i in range(block_count)]
        grid = build_quadrant_walkability(width, height, map_data, blocks, walkable_tiles)
        result = _bfs_find_path(grid, start, end)
        if not result: return None
        actions, _ = result
        return ';'.join(actions) + ';'
    except (FileNotFoundError, IOError) as e: print(f"Error reading ROM file '{rom_path}': {e}"); return None
    except (ValueError, IndexError) as e: print(f"Error processing map/tileset data: {e}"); return None
    except Exception as e: print(f"An unexpected error occurred in find_path: {e}"); return None

# --- dump_minimal_map function ---
# [MODIFIED] - Added debug_tiles parameter
def dump_minimal_map(rom_path, map_id, pos=None, grid=False, debug=False, debug_tiles=False):
    """
    Dumps a minimal map showing walkability (white/black), highlighting special locations (orange).

    Args:
        rom_path (str): Path to the Pokemon Red/Blue ROM file.
        map_id (int): The ID of the map.
        pos (tuple, optional): (gx, gy) coords to mark blue. Defaults to None.
        grid (bool, optional): Draw grid lines. Defaults to False.
        debug (bool, optional): Draw grid lines and coordinates. Defaults to False.
        debug_tiles (bool, optional): Print tile IDs for each quadrant. Defaults to False.

    Returns:
        PIL.Image.Image or None: The generated map image, or None on error.
    """
    # Removed internal definition of SPECIAL_FEATURE_TILE_IDS, will use module-level one

    try:
        # --- Load Base Data ---
        rom = open(rom_path, 'rb').read()
        tileset_id, width, height, map_data = load_map(rom, map_id)
        bank, blocks_ptr, tiles_ptr, collision_ptr, _ = load_tileset_header(rom, tileset_id)

        # Load Collision Data
        col_off = gb_to_file_offset(collision_ptr, bank)
        if col_off >= len(rom): raise ValueError("Collision pointer outside ROM.")
        collision = []
        idx = col_off
        while idx < len(rom):
            v = rom[idx]; idx += 1
            if v == 0xFF: break
            collision.append(v)
        walkable_tiles = set(collision)

        # Load Blocks Data
        blk_off = gb_to_file_offset(blocks_ptr, bank)
        if blk_off >= len(rom): raise ValueError("Blocks pointer outside ROM.")
        max_bidx = 0
        if map_data: max_bidx = max(map_data)
        block_count = max_bidx + 1
        if blk_off + block_count * 16 > len(rom):
            block_count = max(0, (len(rom) - blk_off) // 16)
            print(f"Warning: Block data might be truncated (count {block_count}) in minimal map dump.")
        blocks = [rom[blk_off + i*16 : blk_off + i*16 + 16].ljust(16, b'\x00') for i in range(block_count)]

        # Build Walkability Grid
        grid_data = build_quadrant_walkability(width, height, map_data, blocks, walkable_tiles)
        if not grid_data or not grid_data[0]: raise ValueError("Failed to build walkability grid.")
        grid_h, grid_w = len(grid_data), len(grid_data[0])

        # --- Identify WALKABLE Special Quadrants ---
        walkable_special_quadrants = set()
        if debug_tiles: print("Scanning map blocks for WALKABLE special features and tile IDs (minimal map)...") # Debug context
        for by in range(height):
            for bx in range(width):
                map_idx = by*width + bx
                if map_idx >= len(map_data): continue
                bidx = map_data[map_idx]
                if bidx >= len(blocks): continue
                block_definition = blocks[bidx]
                if len(block_definition) < 16: continue

                for gqy in range(2):
                    for gqx in range(2):
                        gx, gy = bx * 2 + gqx, by * 2 + gqy

                        # Check Walkability FIRST
                        is_walkable_here = False
                        if 0 <= gy < grid_h and 0 <= gx < grid_w:
                            is_walkable_here = grid_data[gy][gx]
                        else:
                             if debug_tiles: print(f"DEBUG: Skipping Coord ({gx},{gy}) - outside grid bounds ({grid_w}x{grid_h}) or grid missing.")
                             continue # Skip if out of bounds

                        # Check if tiles are special
                        indices = [
                            (gqy * 2 + 0) * 4 + (gqx * 2 + 0), (gqy * 2 + 0) * 4 + (gqx * 2 + 1),
                            (gqy * 2 + 1) * 4 + (gqx * 2 + 0), (gqy * 2 + 1) * 4 + (gqx * 2 + 1)
                        ]

                        # --- [MODIFIED] Added debug_tiles conditional printing ---
                        tile_ids_in_quad = [block_definition[i] if i < len(block_definition) else None for i in indices]
                        if debug_tiles:
                            tile_str = ", ".join([f"0x{tid:02X}" if tid is not None else "N/A" for tid in tile_ids_in_quad])
                            walk_str = "Walkable" if is_walkable_here else "Blocked"
                            print(f"DEBUG: Coord ({gx:>2},{gy:>2}) Block ({bx:>2},{by:>2}) ID 0x{bidx:02X} -> Tiles: [{tile_str}] ({walk_str})")

                        all_special = True
                        is_any_special = False # Track if at least one was special
                        for tile_id in tile_ids_in_quad:
                            if tile_id is None: # Check for None from list comprehension above
                                all_special = False; break
                            if tile_id in SPECIAL_FEATURE_TILE_IDS: # Use module-level constant
                                is_any_special = True
                            else:
                                all_special = False; break

                        # Add to set ONLY IF special AND walkable
                        if all_special and is_walkable_here:
                            walkable_special_quadrants.add((gx, gy))
                            if debug_tiles: print(f"DEBUG: -> Adding ({gx},{gy}) to walkable special set.")
                        elif all_special and not is_walkable_here:
                             if debug_tiles: print(f"DEBUG: -> Coord ({gx},{gy}) is special but BLOCKED, not highlighting.")
                        elif is_any_special and debug_tiles and not all_special: # Only print if some but not all were special
                             print(f"DEBUG: -> Coord ({gx},{gy}) had *some* special tiles but not all 4 were.")
                        # --- End of debug_tiles printing ---


        # --- Prepare Image ---
        cell_size = 16
        img_w, img_h = grid_w * cell_size, grid_h * cell_size
        if img_w <= 0 or img_h <= 0: raise ValueError(f"Invalid image dimensions calculated: {img_w}x{img_h}")

        img = Image.new('RGB', (img_w, img_h))
        draw = ImageDraw.Draw(img)

        # --- Define Colors ---
        walkable_color = (255, 255, 255); blocked_color = (0, 0, 0)
        special_color = (255, 165, 0); marker_color = (0, 0, 255)
        grid_line_color = (100, 100, 100); debug_text_color = marker_color

        # Draw map cells
        for y in range(grid_h):
            for x in range(grid_w):
                is_walkable = grid_data[y][x]
                color = walkable_color if is_walkable else blocked_color
                if is_walkable and (x, y) in walkable_special_quadrants:
                    color = special_color
                x0, y0 = x * cell_size, y * cell_size
                draw.rectangle([x0, y0, x0 + cell_size - 1, y0 + cell_size - 1], fill=color)

        # Draw player position marker
        if pos:
            px, py = pos
            if 0 <= px < grid_w and 0 <= py < grid_h:
                cx, cy = px * cell_size + cell_size // 2, py * cell_size + cell_size // 2
                radius = cell_size // 2 - 3
                draw.ellipse([(cx - radius, cy - radius), (cx + radius, cy + radius)], fill=marker_color, outline=marker_color)
            else: print(f"Warning: Marker position {pos} is outside grid ({grid_w}x{grid_h}).")

        # Overlay grid lines
        if grid or debug:
            for x_line in range(0, img_w, cell_size): draw.line([(x_line, 0), (x_line, img_h - 1)], fill=grid_line_color)
            for y_line in range(0, img_h, cell_size): draw.line([(0, y_line), (img_w - 1, y_line)], fill=grid_line_color)

        # Overlay debug text
        if debug:
            try:
                font_size = max(8, min(12, cell_size // 2 - 2))
                font = ImageFont.load_default(size=font_size)
            except (AttributeError, OSError):
                try: font = ImageFont.load_default()
                except OSError: font = None; print("Warning: Cannot load default font for debug text.")
            if font:
                for gy in range(grid_h):
                    for gx in range(grid_w):
                        draw.text((gx * cell_size + 2, gy * cell_size + 1), f"{gx},{gy}", font=font, fill=debug_text_color)

        return img

    except (FileNotFoundError, IOError) as e: print(f"Error reading ROM file '{rom_path}': {e}"); return None
    except (ValueError, IndexError) as e: print(f"Error processing data for minimal map: {e}"); return None
    except Exception as e: print(f"Unexpected error in dump_minimal_map: {e}"); return None


# --- CLI wrapper ---
def main():
    parser = argparse.ArgumentParser(
        description="Dump Pokémon Red/Blue map image. Highlights special features (doors/stairs/ladders) in orange."
    )
    parser.add_argument('rom', help='Path to Pokémon Red/Blue ROM file')
    parser.add_argument('map_id', type=int, help='Map ID')
    parser.add_argument('--start', '-s', help='Start coordinate gx,gy for pathfinding')
    parser.add_argument('--end', '-e', help='End coordinate gx,gy for pathfinding')
    parser.add_argument('--output', '-o', default='map.png', help='Output image file')
    parser.add_argument('--debug', '-d', action='store_true', help='Draw coordinate grid/numbers')
    parser.add_argument('--pos', help='Optional marker coordinate gx,gy')
    parser.add_argument('--minimal', '-m', action='store_true', help='Generate minimal walkability map (with orange highlights)')
    # --- [MODIFIED] Reinstated debug-tiles argument ---
    parser.add_argument('--debug-tiles', action='store_true', help='Print the 4 tile IDs for each map coordinate')
    args = parser.parse_args()
    res = None # Pathfinding result (actions, coords)

    # --- Define Highlight Color (Only needed for Full Render) ---
    ORANGE_HIGHLIGHT_COLOR_RGBA = (255, 165, 0, 150) # Semi-transparent for full render

    # --- Load Common Data ---
    grid = None
    walkable_special_quadrants = set() # Still needed for full render's orange overlay

    try:
        print(f"Loading ROM: {args.rom}")
        rom = open(args.rom, 'rb').read()
        print(f"Loading Map ID: {args.map_id}")
        tileset_id, width, height, map_data = load_map(rom, args.map_id)
        print(f"Map Dimensions: {width}x{height} blocks ({width*2}x{height*2} quadrants)")
        print(f"Using Tileset ID: {tileset_id}")
        bank, blocks_ptr, tiles_ptr, collision_ptr, interaction_ptr = load_tileset_header(rom, tileset_id)
        print(f"Tileset Header: Bank ${bank:02X}, Blocks ${blocks_ptr:04X}, Tiles ${tiles_ptr:04X}, Collision ${collision_ptr:04X}, Interaction ${interaction_ptr:04X}")

        # Load Collision Data
        col_off = gb_to_file_offset(collision_ptr, bank)
        if col_off >= len(rom): raise ValueError("Collision pointer outside ROM.")
        collision = []
        idx = col_off
        while idx < len(rom):
            v = rom[idx]; idx += 1
            if v == 0xFF: break
            collision.append(v)
        walkable_tiles = set(collision)

        # Load Blocks Data
        blk_off = gb_to_file_offset(blocks_ptr, bank)
        if blk_off >= len(rom): raise ValueError("Blocks pointer outside ROM.")
        max_bidx = 0
        if map_data: max_bidx = max(map_data)
        block_count = max_bidx + 1
        if blk_off + block_count * 16 > len(rom):
            actual_readable_blocks = max(0, (len(rom) - blk_off) // 16)
            print(f"Warning: Block data might be truncated. Requested {block_count} blocks ({block_count*16} bytes) starting at offset {blk_off:06X}, but only space for {actual_readable_blocks} blocks.")
            block_count = actual_readable_blocks
        print(f"Loading {block_count} block definitions...")
        blocks = [rom[blk_off+i*16:blk_off+i*16+16].ljust(16, b'\x00') for i in range(block_count)]

        # --- Build Walkability Grid ---
        print("Building walkability grid...")
        grid = build_quadrant_walkability(width, height, map_data, blocks, walkable_tiles)
        grid_h, grid_w = (height*2, width*2) if grid else (0,0)
        if not grid: print("Warning: Failed to build walkability grid.")

        # --- Identify WALKABLE Special Quadrants (Needed for Full Render, optionally print debug) ---
        if not args.minimal or args.debug_tiles: # Calculate if doing full render OR if debug_tiles is enabled (for consistency)
            if not args.minimal: print("Scanning map blocks for WALKABLE special features (for full render)...")
            elif args.debug_tiles: print("Scanning map blocks for WALKABLE special features and tile IDs (full render context)...")

            # Re-add the debug printing logic here for the full render context if --debug-tiles is on
            for by in range(height):
                for bx in range(width):
                    map_idx = by*width + bx
                    if map_idx >= len(map_data): continue
                    bidx = map_data[map_idx]
                    if bidx >= len(blocks): continue
                    block_definition = blocks[bidx]
                    if len(block_definition) < 16: continue

                    for gqy in range(2):
                        for gqx in range(2):
                            gx, gy = bx * 2 + gqx, by * 2 + gqy

                            is_walkable_here = False
                            if grid and 0 <= gy < grid_h and 0 <= gx < grid_w:
                                is_walkable_here = grid[gy][gx]
                            else:
                                if args.debug_tiles: print(f"DEBUG: Skipping Coord ({gx},{gy}) - outside grid bounds ({grid_w}x{grid_h}) or grid missing.")
                                continue

                            indices = [
                                (gqy * 2 + 0) * 4 + (gqx * 2 + 0), (gqy * 2 + 0) * 4 + (gqx * 2 + 1),
                                (gqy * 2 + 1) * 4 + (gqx * 2 + 0), (gqy * 2 + 1) * 4 + (gqx * 2 + 1)
                            ]
                            tile_ids_in_quad = [block_definition[i] if i < len(block_definition) else None for i in indices]

                            if args.debug_tiles and not args.minimal: # Only print if doing full render AND debug_tiles
                                tile_str = ", ".join([f"0x{tid:02X}" if tid is not None else "N/A" for tid in tile_ids_in_quad])
                                walk_str = "Walkable" if is_walkable_here else "Blocked"
                                print(f"DEBUG: Coord ({gx:>2},{gy:>2}) Block ({bx:>2},{by:>2}) ID 0x{bidx:02X} -> Tiles: [{tile_str}] ({walk_str})")

                            all_special = True
                            is_any_special = False
                            for tile_id in tile_ids_in_quad:
                                if tile_id is None:
                                    all_special = False; break
                                if tile_id in SPECIAL_FEATURE_TILE_IDS: # Use module-level constant
                                    is_any_special = True
                                else:
                                    all_special = False; break

                            if all_special and is_walkable_here:
                                walkable_special_quadrants.add((gx, gy)) # Add to set needed for full render
                                if args.debug_tiles and not args.minimal: print(f"DEBUG: -> Adding ({gx},{gy}) to walkable special set (full render).")
                            elif all_special and not is_walkable_here:
                                if args.debug_tiles and not args.minimal: print(f"DEBUG: -> Coord ({gx},{gy}) is special but BLOCKED (full render).")
                            elif is_any_special and not all_special and args.debug_tiles and not args.minimal:
                                print(f"DEBUG: -> Coord ({gx},{gy}) had *some* special tiles but not all 4 were (full render).")


    except (FileNotFoundError, IOError) as e: print(f"Error reading ROM file '{args.rom}': {e}"); return
    except (ValueError, IndexError) as e: print(f"Error processing map/tileset/block data: {e}"); return
    except Exception as e: print(f"An unexpected error occurred during data loading: {e}"); return

    # --- Generate Image based on mode ---
    img = None
    if args.minimal:
        # --- Minimal Map Mode ---
        print(f"Generating minimal map...")
        minimal_pos = None
        if args.pos:
            try:
                coords = args.pos.split(',')
                if len(coords) == 2: minimal_pos = tuple(map(int, coords))
                else: raise ValueError("Invalid coord count")
            except Exception as e:
                print(f"Warning: Invalid format for --pos '{args.pos}'. Expected gx,gy. Error: {e}")

        # --- [MODIFIED] Pass debug_tiles flag ---
        img = dump_minimal_map(args.rom, args.map_id, pos=minimal_pos, grid=args.debug, debug=args.debug, debug_tiles=args.debug_tiles)
        if img is None: print("Failed to generate minimal map."); return

    else: # --- Full Render Mode ---
        print(f"Generating full map render... Found {len(walkable_special_quadrants)} walkable special locations.")
        try:
            # --- Load Tile Graphics ---
            tile_off = gb_to_file_offset(tiles_ptr, bank)
            if tile_off >= len(rom): raise ValueError("Tiles pointer outside ROM.")
            tiles = []
            max_tiles_possible = max(0, (len(rom) - tile_off) // 16)
            max_tile_id_used = 0
            if collision: max_tile_id_used = max(max_tile_id_used, max(collision))
            for block_def in blocks:
                 if block_def: max_tile_id_used = max(max_tile_id_used, max(block_def))
            num_tiles_to_load = min(max_tiles_possible, max(max_tile_id_used + 1, 128)) # Ensure we load at least basic tiles
            print(f"Max Tile ID used in blocks/collision: {max_tile_id_used}. Attempting to load {num_tiles_to_load} tiles.")
            for i in range(num_tiles_to_load):
                 tile_data = rom[tile_off+i*16 : tile_off+i*16+16]
                 if len(tile_data) < 16:
                     print(f"Warning: Ran out of ROM data while loading tiles. Loaded {i} tiles.")
                     break
                 tiles.append(tile_data)
            if not tiles: print("Warning: No tile graphics data loaded. Map will be blank.")

            # Walkability grid (`grid`) was already built

            img_w, img_h = width*32, height*32
            if img_w <= 0 or img_h <= 0: raise ValueError("Invalid map dimensions result in zero image size.")

            # Create Base Paletted Image
            base = Image.new('P', (img_w,img_h))
            base.putpalette([255,255,255, 192,192,192, 96,96,96, 0,0,0] + [0]*756)

            # Create overlay for special features highlight
            special_overlay = Image.new('RGBA', (img_w, img_h), (0, 0, 0, 0))
            special_draw = ImageDraw.Draw(special_overlay)

            # Render base tiles
            print("Rendering base tiles...")
            for by in range(height):
                for bx in range(width):
                    map_idx = by*width + bx
                    if map_idx >= len(map_data): continue
                    bidx = map_data[map_idx]
                    if bidx >= len(blocks): continue
                    block_definition = blocks[bidx]
                    if len(block_definition) < 16: continue
                    for i, tid in enumerate(block_definition):
                        if tid < len(tiles):
                            decoded_tile = decode_tile(tiles[tid])
                            base_tile_x, base_tile_y = bx*32 + (i%4)*8, by*32 + (i//4)*8
                            for yy in range(8):
                                for xx in range(8):
                                    px, py = base_tile_x+xx, base_tile_y+yy
                                    if 0 <= px < img_w and 0 <= py < img_h:
                                         base.putpixel((px, py), decoded_tile[yy][xx])

            # Draw highlights onto the special overlay (using the set calculated earlier)
            print(f"Drawing {len(walkable_special_quadrants)} walkable special feature highlights...")
            for gx, gy in walkable_special_quadrants:
                 highlight_x, highlight_y = gx * 16, gy * 16
                 special_draw.rectangle([highlight_x, highlight_y, highlight_x + 15, highlight_y + 15], fill=ORANGE_HIGHLIGHT_COLOR_RGBA)

            # --- Composite Layers ---
            img_rgba = base.convert('RGBA')
            img_rgba = Image.alpha_composite(img_rgba, special_overlay)

            # Overlay non-walkable areas
            walkability_overlay = Image.new('RGBA',(img_w,img_h),(0,0,0,0))
            walk_draw = ImageDraw.Draw(walkability_overlay)
            if grid:
                print("Drawing walkability overlay...")
                red_overlay_color = (255, 0, 0, 100)
                for gy in range(grid_h):
                    for gx in range(grid_w):
                        if gy < len(grid) and gx < len(grid[gy]):
                            if not grid[gy][gx]:
                                walk_draw.rectangle([gx*16, gy*16, gx*16 + 15, gy*16 + 15], fill=red_overlay_color)
            img = Image.alpha_composite(img_rgba, walkability_overlay)

            # --- Pathfinding ---
            start_coord, end_coord = None, None
            if args.start:
                 try: coords=args.start.split(','); start_coord = tuple(map(int,coords)) if len(coords)==2 else None
                 except Exception as e: print(f"Invalid start format '{args.start}': {e}")
            if args.end:
                 try: coords=args.end.split(','); end_coord = tuple(map(int,coords)) if len(coords)==2 else None
                 except Exception as e: print(f"Invalid end format '{args.end}': {e}")
            if not start_coord and end_coord and args.pos: # Use --pos as start if only --end is given
                 try: coords=args.pos.split(','); start_coord = tuple(map(int,coords)) if len(coords)==2 else None
                 except Exception as e: print(f"Invalid pos format '{args.pos}' used as start: {e}")
            if start_coord and end_coord:
                if grid:
                    print(f"Finding path from {start_coord} to {end_coord}...")
                    res = _bfs_find_path(grid, start_coord, end_coord)
                    if res:
                        actions, path_coords = res
                        print("Path Actions:", ';'.join(actions)+';')
                        print("Drawing path...")
                        pd = ImageDraw.Draw(img)
                        pts=[(x*16+8, y*16+8) for x,y in path_coords]
                        if len(pts) > 1: pd.line(pts, fill=(0, 255, 0, 200), width=5, joint='miter')
                    else: print("Path not found.")
                else: print("Cannot perform pathfinding: Walkability grid failed to generate.")
            elif args.start or args.end: print("Warning: Pathfinding requires both --start (or --pos) and --end coordinates.")

            # --- Marker ---
            if args.pos:
                try:
                    coords = args.pos.split(',')
                    if len(coords) != 2: raise ValueError("Invalid coord count")
                    px,py=map(int, coords)
                    if grid and 0 <= px < grid_w and 0 <= py < grid_h:
                        print(f"Drawing marker at {args.pos}...")
                        md=ImageDraw.Draw(img)
                        cx,cy=px*16+8, py*16+8
                        radius = 7
                        marker_fill = (0,0,255,180)
                        marker_outline = (255,255,255,220)
                        md.ellipse([(cx-radius, cy-radius),(cx+radius, cy+radius)], fill=marker_fill, outline=marker_outline, width=2)
                    else: print(f"Warning: Marker position {args.pos} is outside the grid bounds ({grid_w}x{grid_h}) or grid unavailable.")
                except Exception as e: print(f"Warning: Invalid format or value for --pos '{args.pos}'. Error: {e}")

            # --- Debug Grid and Coordinates ---
            if args.debug:
                print("Drawing debug grid and coordinates...")
                gd=ImageDraw.Draw(img)
                try: font_size = max(8, min(12, 16 // 2 - 2)); font = ImageFont.load_default(size=font_size)
                except: font = ImageFont.load_default()
                grid_line_color=(50,50,50,100)
                text_color=(200,200,255,220)
                for x in range(0, img_w, 16): gd.line([(x,0),(x,img_h-1)], fill=grid_line_color)
                for y in range(0, img_h, 16): gd.line([(0,y),(img_w-1,y)], fill=grid_line_color)
                if grid and font:
                    for gy in range(grid_h):
                        for gx in range(grid_w):
                             gd.text((gx*16+1, gy*16+0), f"{gx},{gy}", font=font, fill=text_color)

        except (ValueError, IndexError) as e: print(f"Error during full render processing: {e}"); return
        except Exception as e: print(f"Unexpected error during full render: {e}"); return

    # --- Save final image ---
    if img:
        try:
            # Full render is always RGBA because of overlays
            # Minimal map starts as RGB, needs conversion if pos/debug/grid is true
            save_mode = 'RGB'
            if not args.minimal:
                save_mode = 'RGBA'
            elif args.pos or args.debug or args.grid: # Grid lines also make it non-pure B/W/Orange
                save_mode = 'RGBA'
            # Minimal map *could* have orange, making it non-RGB if saved directly.
            # However, dump_minimal_map currently creates an RGB image.
            # If orange exists, saving as RGB loses the distinct orange.
            # Let's force RGBA for minimal map if highlights *might* exist,
            # which is always unless SPECIAL_FEATURE_TILE_IDS is empty (unlikely).
            # A pragmatic choice: always save minimal as RGB unless debug/pos/grid force RGBA.
            # If users want distinct orange preserved, saving as PNG (which supports palettes or RGB/RGBA) is needed.
            # For simplicity, let's stick to RGB for base minimal, RGBA if extras are added.

            if img.mode != save_mode:
                print(f"Converting image mode from {img.mode} to {save_mode} before saving.")
                img = img.convert(save_mode)

            img.save(args.output)
            mode_msg = "Minimal map" if args.minimal else "Map image"
            path_msg = "with path" if res and not args.minimal else ""
            print(f"Saved {mode_msg} {path_msg} (Mode: {img.mode}) to {args.output}")
        except Exception as e:
            print(f"Error saving image to {args.output}: {e}")
    else:
        print("No image generated to save.")


if __name__ == '__main__':
    main()