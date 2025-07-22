#!/usr/bin/env python3
import argparse
import sys
from PIL import Image, ImageDraw, ImageFont
from pyAIAgent.game.rom import (
    load_map,
    load_tileset_header,
    load_collision_data,
    load_block_data,
    load_tile_graphics,
)
from pyAIAgent.game.graphics import (
    build_quadrant_walkability,
    calculate_walkable_special_quadrants,
    decode_tile,
    dump_minimal_map,
)
from pyAIAgent.navigation import _bfs_find_path

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
        except ValueError:
            print(f"Warning: Invalid --pos format '{args.pos}'.", file=sys.stderr)

    # Parse --crop into a tuple if present
    crop_tuple = None
    if args.crop:
        try:
            crop_tuple = tuple(map(int, args.crop.split(',')))
        except ValueError:
            print(f"Warning: Invalid --crop format '{args.crop}'.", file=sys.stderr)

    if args.minimal:
        print("Generating minimal map...", file=sys.stderr)

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
        print("Generating full map render...", file=sys.stderr)
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
                except Exception:
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
            except ValueError:
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
                except ValueError:
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
