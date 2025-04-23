#!/usr/bin/env python3
import argparse
from collections import deque
from PIL import Image, ImageDraw, ImageFont

__all__ = ["find_path", "dump_minimal_map"]

# --- Low-level ROM helpers ---
def read_u8(data, offset):
    return data[offset]

def read_u16(data, offset):
    return data[offset] | (data[offset+1] << 8)

def gb_to_file_offset(ptr, bank):
    if ptr < 0x4000:
        return ptr
    return (bank * 0x4000) + (ptr - 0x4000)

# --- Map loading ---
def load_map(rom, map_id):
    ptr_table  = 0x01AE
    bank_table = 0xC23D
    ptr = read_u16(rom, ptr_table + map_id*2)
    bank = read_u8(rom, bank_table + map_id)
    header_off = gb_to_file_offset(ptr, bank)
    tileset_id = read_u8(rom, header_off)
    height = read_u8(rom, header_off+1)
    width = read_u8(rom, header_off+2)
    map_data_ptr = read_u16(rom, header_off+3)
    map_data_off = gb_to_file_offset(map_data_ptr, bank)
    size = width * height
    map_data = rom[map_data_off : map_data_off + size]
    return tileset_id, width, height, map_data

# --- Tileset header loading ---
def load_tileset_header(rom, tileset_id):
    base = 0xC7BE
    header_off = base + tileset_id * 12
    tileset_bank = read_u8(rom, header_off)
    blocks_ptr = read_u16(rom, header_off + 1)
    tiles_ptr = read_u16(rom, header_off + 3)
    collision_ptr = read_u16(rom, header_off + 5)
    return tileset_bank, blocks_ptr, tiles_ptr, collision_ptr

# --- Decode a single 8×8 tile ---
def decode_tile(tile_bytes):
    if len(tile_bytes) < 16:
        tile_bytes = tile_bytes + b'\x00' * (16 - len(tile_bytes))
    pixels = []
    for row in range(8):
        plane0 = tile_bytes[row]
        plane1 = tile_bytes[row + 8]
        row_pixels = []
        for bit in range(7, -1, -1):
            low = (plane0 >> bit) & 1
            high = (plane1 >> bit) & 1
            row_pixels.append((high << 1) | low)
        pixels.append(row_pixels)
    return pixels

# --- Build 2×2 quadrant walkability grid ---
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
                    idx = (qr*2+1)*4 + (qc*2)
                    tid = subtiles[idx]
                    grid[by*2+qr][bx*2+qc] = (tid in walkable_tiles)
    return grid

# --- BFS pathfinder ---
def _bfs_find_path(grid, start, end):
    cols, rows = len(grid[0]), len(grid)
    sx, sy = start; ex, ey = end
    queue = deque([(sx, sy)])
    prev = {(sx, sy): None}
    dirs = {(1,0): 'R', (-1,0): 'L', (0,1): 'D', (0,-1): 'U'}
    while queue:
        x, y = queue.popleft()
        if (x, y) == (ex, ey):
            break
        for (dx, dy), action in dirs.items():
            nx, ny = x+dx, y+dy
            if 0 <= nx < cols and 0 <= ny < rows and grid[ny][nx] and (nx, ny) not in prev:
                prev[(nx, ny)] = (x, y, action)
                queue.append((nx, ny))
    if (ex, ey) not in prev:
        return None
    path = []
    cur = (ex, ey)
    while prev[cur] is not None:
        px, py, action = prev[cur]
        path.append((cur[0], cur[1], action))
        cur = (px, py)
    path.reverse()
    actions = ''.join(step[2] for step in path)
    coords = [(sx, sy)] + [(x, y) for x, y, _ in path]
    return actions, coords

# --- Public API ---
def find_path(rom_path, map_id, start, end):
    rom = open(rom_path, 'rb').read()
    tileset_id, width, height, map_data = load_map(rom, map_id)
    bank, blocks_ptr, tiles_ptr, collision_ptr = load_tileset_header(rom, tileset_id)
    # load collision
    col_off = gb_to_file_offset(collision_ptr, bank)
    collision = []
    idx = col_off
    while idx < len(rom):
        v = rom[idx]; idx += 1
        if v == 0xFF: break
        collision.append(v)
    walkable_tiles = set(collision)
    # load blocks
    blk_off = gb_to_file_offset(blocks_ptr, bank)
    block_count = max(map_data) + 1
    blocks = [rom[blk_off+i*16:blk_off+i*16+16].ljust(16, b'\x00') for i in range(block_count)]
    grid = build_quadrant_walkability(width, height, map_data, blocks, walkable_tiles)
    result = _bfs_find_path(grid, start, end)
    if not result:
        return None
    actions, _ = result
    return ''.join(a + ';' for a in actions)


def dump_minimal_map(rom_path, map_id, pos=None, debug=False):
    rom = open(rom_path, 'rb').read()
    tileset_id, width, height, map_data = load_map(rom, map_id)
    bank, blocks_ptr, tiles_ptr, collision_ptr = load_tileset_header(rom, tileset_id)
    # load collision
    col_off = gb_to_file_offset(collision_ptr, bank)
    collision = []
    idx = col_off
    while idx < len(rom):
        v = rom[idx]; idx += 1
        if v == 0xFF: break
        collision.append(v)
    walkable_tiles = set(collision)
    # load blocks
    blk_off = gb_to_file_offset(blocks_ptr, bank)
    block_count = max(map_data) + 1
    blocks = [rom[blk_off+i*16:blk_off+i*16+16].ljust(16, b'\x00') for i in range(block_count)]
    grid = build_quadrant_walkability(width, height, map_data, blocks, walkable_tiles)
    # minimal image
    img_w, img_h = width*2*16, height*2*16
    img = Image.new('RGB', (img_w, img_h))
    draw = ImageDraw.Draw(img)
    for y in range(len(grid)):
        for x in range(len(grid[0])):
            draw.rectangle([x*16, y*16, x*16+16, y*16+16], fill=(255,255,255) if grid[y][x] else (0,0,0))
    if pos:
        px, py = pos
        cx, cy = px*16+8, py*16+8
        draw.ellipse([(cx-6, cy-6), (cx+6, cy+6)], fill=(0,0,255), outline=(0,0,255))
    if debug:
        font = ImageFont.load_default()
        for x in range(0, img_w+1, 16): draw.line([(x,0),(x,img_h)], fill=(0,0,0,64))
        for y in range(0, img_h+1, 16): draw.line([(0,y),(img_w,y)], fill=(0,0,0,64))
        for gy in range(img_h//16):
            for gx in range(img_w//16):
                draw.text((gx*16+1, gy*16+1), f"{gx},{gy}", font=font, fill=(0,0,255))
    return img

# --- CLI wrapper ---
def main():
    parser = argparse.ArgumentParser(
        description="Dump Pokémon Red/Blue map to image with optional walkability overlay, grid, pathfinder, marker, and minimal mode"
    )
    parser.add_argument('rom', help='Path to Pokémon Red/Blue ROM file')
    parser.add_argument('map_id', type=int, help='Map ID (index into map header table)')
    parser.add_argument('--start', '-s', help='Start coordinate gx,gy')
    parser.add_argument('--end', '-e', help='End coordinate gx,gy')
    parser.add_argument('--output', '-o', default='map.png', help='Output image file')
    parser.add_argument('--debug', '-d', action='store_true', help='Draw coordinate grid')
    parser.add_argument('--pos', help='Optional marker coordinate gx,gy')
    parser.add_argument('--minimal', '-m', action='store_true', help='Monochrome minimal mode')
    args = parser.parse_args()
    res = None

    # Read ROM
    rom = open(args.rom, 'rb').read()
    # Load map & tileset
    tileset_id, width, height, map_data = load_map(rom, args.map_id)
    bank, blocks_ptr, tiles_ptr, collision_ptr = load_tileset_header(rom, tileset_id)
    # Collision
    col_off = gb_to_file_offset(collision_ptr, bank)
    collision = []
    idx = col_off
    while idx < len(rom): 
        v = rom[idx]; idx+=1
        if v == 0xFF: break
        collision.append(v)
    walkable_tiles = set(collision)
    # Blocks
    blk_off = gb_to_file_offset(blocks_ptr, bank)
    blocks = [rom[blk_off+i*16:blk_off+i*16+16].ljust(16, b'\x00') for i in range(max(map_data)+1)]
    # Build grid
    grid = build_quadrant_walkability(width, height, map_data, blocks, walkable_tiles)
    # Prepare image
    img_w, img_h = width*32, height*32
    if args.minimal:
        img = dump_minimal_map(args.rom, args.map_id, pos=tuple(map(int,args.pos.split(','))) if args.pos else None, debug=args.debug)
    else:
        # Full render
        # Load tiles
        tiles = []
        tile_off = gb_to_file_offset(tiles_ptr, bank)
        for i in range(512):
            chunk = rom[tile_off+i*16:tile_off+i*16+16]
            if len(chunk)<16: break
            tiles.append(chunk)
        base = Image.new('P', (img_w,img_h))
        base.putpalette([255,255,255,192,192,192,96,96,96,0,0,0] + [0]*744)
        for by in range(height):
            for bx in range(width):
                bidx = map_data[by*width+bx]
                for i,tid in enumerate(blocks[bidx]):
                    if tid<len(tiles):
                        tile = decode_tile(tiles[tid])
                        tx,ty=(i%4)*8,(i//4)*8
                        for yy in range(8):
                            for xx in range(8): base.putpixel((bx*32+tx+xx,by*32+ty+yy),tile[yy][xx])
        overlay = Image.new('RGBA',(img_w,img_h),(0,0,0,0))
        draw = ImageDraw.Draw(overlay)
        for by in range(height):
            for bx in range(width):
                bidx=map_data[by*width+bx]
                for qr in range(2):
                    for qc in range(2):
                        idx2=(qr*2+1)*4+qc*2
                        if blocks[bidx][idx2] not in walkable_tiles:
                            x0,y0=bx*32+qc*16,by*32+qr*16
                            draw.rectangle([x0,y0,x0+16,y0+16],fill=(255,0,0,100))
        img = Image.alpha_composite(base.convert('RGBA'),overlay)
        # Pathfinding
        res=None
        if args.end:
            sx,sy = map(int,args.start.split(',')) if args.start else map(int,args.pos.split(','))
            ex,ey=map(int,args.end.split(','))
            res=_bfs_find_path(grid,(sx,sy),(ex,ey))
            if res: print(res[0])
        if res and res[1]:
            coords=res[1]
            pd = ImageDraw.Draw(img)
            pts=[(x*16+8,y*16+8) for x,y in coords]
            pd.line(pts,fill=(0,255,0,180),width=4)
        # Marker
        if args.pos:
            px,py=map(int,args.pos.split(','))
            md=ImageDraw.Draw(img)
            cx,cy=px*16+8,py*16+8
            md.ellipse([(cx-6,cy-6),(cx+6,cy+6)],fill=(0,0,255,180),outline=(0,0,255,255),width=1)
        # Debug grid
        if args.debug:
            gd=ImageDraw.Draw(img)
            font=ImageFont.load_default()
            for x in range(0,img_w+1,16): gd.line([(x,0),(x,img_h)],fill=(0,0,0,64))
            for y in range(0,img_h+1,16): gd.line([(0,y),(img_w,y)],fill=(0,0,0,64))
            for gy in range(img_h//16):
                for gx in range(img_w//16): gd.text((gx*16+1,gy*16+1),f"{gx},{gy}",font=font,fill=(0,0,255))
    img.save(args.output)
    print(f"Map image {'with path ' if res and not args.minimal else ''}saved to {args.output}")

if __name__ == '__main__':
    main()
