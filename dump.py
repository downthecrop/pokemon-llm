#!/usr/bin/env python3
import argparse
from collections import deque
from PIL import Image, ImageDraw, ImageFont

def parse_args():
    parser = argparse.ArgumentParser(
        description="Dump Pokémon Red/Blue map to image with optional walkability overlay, grid, and pathfinder"
    )
    parser.add_argument('rom', help='Path to Pokémon Red/Blue ROM file')
    parser.add_argument('map_id', type=int, help='Map ID (index into map header table)')
    parser.add_argument('--start', '-s',
                        help='Optional start coordinate as gx,gy in 16×16 grid units')
    parser.add_argument('--end', '-e',
                        help='Optional end coordinate as gx,gy in 16×16 grid units')
    parser.add_argument('--output', '-o', default='map.png', help='Output image file')
    parser.add_argument('--debug', '-d', action='store_true', help='Draw coordinate grid')
    return parser.parse_args()

def read_u8(data, offset):
    return data[offset]

def read_u16(data, offset):
    return data[offset] | (data[offset+1] << 8)

def gb_to_file_offset(ptr, bank):
    if ptr < 0x4000:
        return ptr
    return (bank * 0x4000) + (ptr - 0x4000)

# Map loading

def load_map(rom, map_id):
    ptr_table  = 0x01AE
    bank_table = 0xC23D
    ptr  = read_u16(rom, ptr_table  + map_id*2)
    bank = read_u8( rom, bank_table + map_id)
    header_off = gb_to_file_offset(ptr, bank)
    tileset_id    = read_u8(rom, header_off)
    height        = read_u8(rom, header_off+1)
    width         = read_u8(rom, header_off+2)
    map_data_ptr  = read_u16(rom, header_off+3)
    map_data_off = gb_to_file_offset(map_data_ptr, bank)
    size         = width * height
    map_data     = rom[map_data_off : map_data_off + size]
    return tileset_id, width, height, map_data

# Tileset header loading

def load_tileset_header(rom, tileset_id):
    base       = 0xC7BE
    header_off = base + tileset_id * 12
    tileset_bank  = read_u8(rom, header_off)
    blocks_ptr    = read_u16(rom, header_off + 1)
    tiles_ptr     = read_u16(rom, header_off + 3)
    collision_ptr = read_u16(rom, header_off + 5)
    return tileset_bank, blocks_ptr, tiles_ptr, collision_ptr

# Decode a single 8×8 tile

def decode_tile(tile_bytes):
    if len(tile_bytes) < 16:
        tile_bytes = tile_bytes + b'\x00' * (16 - len(tile_bytes))
    pixels = []
    for row in range(8):
        plane0 = tile_bytes[row]
        plane1 = tile_bytes[row + 8]
        row_pixels = []
        for bit in range(7, -1, -1):
            low  = (plane0 >> bit) & 1
            high = (plane1 >> bit) & 1
            row_pixels.append((high << 1) | low)
        pixels.append(row_pixels)
    return pixels

# Build 2×2 quadrant walkability grid

def build_quadrant_walkability(width, height, map_data, blocks, walkable_tiles):
    cols = width * 2
    rows = height * 2
    grid = [[False]*cols for _ in range(rows)]
    for by in range(height):
        for bx in range(width):
            bidx = map_data[by*width + bx]
            subtiles = blocks[bidx]
            for qr in range(2):
                for qc in range(2):
                    row = qr*2 + 1
                    col = qc*2
                    idx = row*4 + col
                    tid = subtiles[idx]
                    walk = (tid in walkable_tiles)
                    gx = bx*2 + qc
                    gy = by*2 + qr
                    grid[gy][gx] = walk
    return grid

# BFS pathfinder returning action string and coords

def find_path(grid, start, end):
    cols, rows = len(grid[0]), len(grid)
    sx, sy = start; ex, ey = end
    queue = deque([(sx, sy)])
    prev  = { (sx, sy): None }
    dirs  = { (1,0): 'R', (-1,0): 'L', (0,1): 'D', (0,-1): 'U' }
    while queue:
        x, y = queue.popleft()
        if (x, y) == (ex, ey): break
        for (dx, dy), action in dirs.items():
            nx, ny = x+dx, y+dy
            if 0 <= nx < cols and 0 <= ny < rows and grid[ny][nx] and (nx, ny) not in prev:
                prev[(nx, ny)] = (x, y, action)
                queue.append((nx, ny))
    if (ex, ey) not in prev:
        return None
    # Reconstruct path
    path = []
    cur  = (ex, ey)
    while prev[cur] is not None:
        px, py, action = prev[cur]
        path.append((cur[0], cur[1], action))
        cur = (px, py)
    path.reverse()
    actions = ''.join(step[2] for step in path)
    coords  = [(start[0], start[1])] + [(x,y) for x,y,_ in path]
    return actions, coords

# Main execution
def main():
    args = parse_args()
    rom  = open(args.rom, 'rb').read()

    # Load map & tileset header
    tileset_id, width, height, map_data = load_map(rom, args.map_id)
    bank, blocks_ptr, tiles_ptr, collision_ptr = load_tileset_header(rom, tileset_id)

    # Load collision table
    col_off   = gb_to_file_offset(collision_ptr, bank)
    collision = []
    idx       = col_off
    while idx < len(rom):
        v = rom[idx]; idx += 1
        if v == 0xFF: break
        collision.append(v)
    walkable_tiles = set(collision)

    # Load blockset
    blk_off     = gb_to_file_offset(blocks_ptr, bank)
    block_count = max(map_data) + 1
    blocks      = []
    for i in range(block_count):
        start_off = blk_off + i*16
        blocks.append(rom[start_off:start_off+16].ljust(16, b'\x00'))

    # Load tile graphics
    tile_off = gb_to_file_offset(tiles_ptr, bank)
    tiles    = []
    for i in range(512):
        off = tile_off + i*16
        chunk = rom[off:off+16]
        if len(chunk) < 16: break
        tiles.append(chunk)

    # Build walkability grid
    grid = build_quadrant_walkability(width, height, map_data, blocks, walkable_tiles)

    # Optional pathfinding
    result = None
    if args.start and args.end:
        sx, sy = map(int, args.start.split(','))
        ex, ey = map(int, args.end.split(','))
        result = find_path(grid, (sx, sy), (ex, ey))
        if not result:
            print(f"No path from {(sx,sy)} to {(ex,ey)}")
        else:
            actions, _ = result
            print(actions)

    # Render base map
    img_w, img_h = width*32, height*32
    base_img     = Image.new('P', (img_w, img_h))
    palette      = [255,255,255,192,192,192,96,96,96,0,0,0]
    base_img.putpalette(palette + [0]*((256-len(palette)//3)*3))

    for by in range(height):
        for bx in range(width):
            bidx = map_data[by*width + bx]
            block = blocks[bidx]
            for i, tid in enumerate(block):
                if tid >= len(tiles): continue
                tile = decode_tile(tiles[tid])
                tx, ty = (i%4)*8, (i//4)*8
                for y in range(8):
                    for x in range(8):
                        base_img.putpixel((bx*32+tx+x, by*32+ty+y), tile[y][x])

    # Overlay walkability
    overlay = Image.new('RGBA', (img_w, img_h), (0,0,0,0))
    draw    = ImageDraw.Draw(overlay)
    for by in range(height):
        for bx in range(width):
            bidx = map_data[by*width + bx]
            for qr in range(2):
                for qc in range(2):
                    row, col = qr*2+1, qc*2
                    idx = row*4 + col
                    tid = blocks[bidx][idx]
                    if tid not in walkable_tiles:
                        tx, ty = bx*32+qc*16, by*32+qr*16
                        draw.rectangle([tx, ty, tx+16, ty+16], fill=(255,0,0,100))

    result_img = Image.alpha_composite(base_img.convert('RGBA'), overlay)

    # Draw path if computed
    if result and result[1]:
        _, coords = result
        pd = ImageDraw.Draw(result_img)
        pts = [(x*16+8, y*16+8) for x, y in coords]
        pd.line(pts, fill=(0,255,0,180), width=4)

    # Debug grid
    if args.debug:
        gd = ImageDraw.Draw(result_img)
        font = ImageFont.load_default()
        for x in range(0, img_w+1, 16): gd.line([(x,0),(x,img_h)], fill=(0,0,0,64))
        for y in range(0, img_h+1, 16): gd.line([(0,y),(img_w,y)], fill=(0,0,0,64))
        cols, rows = img_w//16, img_h//16
        for gy in range(rows):
            for gx in range(cols):
                gd.text((gx*16+1, gy*16+1), f"{gx},{gy}", font=font, fill=(0,0,255,255))

    result_img.save(args.output)
    print(f"Map image {'with path ' if result else ''}saved to {args.output}")

if __name__ == '__main__':
    main()
