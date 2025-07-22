from PIL import Image, ImageDraw, ImageFont
import sys
from pyAIAgent.game.rom import (
    load_map,
    load_tileset_header,
    load_collision_data,
    load_block_data,
)

SPECIAL_FEATURE_TILE_IDS = {
    0x04, 0x05, 0x0C, 0x0D, 0x14, 0x15, 0x1C, 0x1D, 0x64, 0x65, 0x6C, 0x6D,
    0x66, 0x67, 0x6E, 0x6F, 0x7B, 0x5A, 0x5B, 0x5C, 0x5D, 0x30, 0x31, 0x32,
    0x33, 0x3A, 0x3B, 0x70, 0x71, 0x78, 0x79, 0x0E, 0x0F, 0x82, 0x83, 0x0A,
    0x0B, 0x1A, 0x1B,
}

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
            except Exception:
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
