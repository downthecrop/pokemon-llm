from collections import deque
from pyAIAgent.game.rom import load_map, load_tileset_header, load_collision_data, load_block_data
from pyAIAgent.game.graphics import build_quadrant_walkability

DEFAULT_ROM = 'c.gbc'

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
    if actions is None:
        return "[PATH BLOCKED OR INVALID UNWALKABLE DESTINATION]\n"
    return actions # None if there is no valid path

def _bfs_find_path(grid, start, end):
    if not grid or not grid[0]:
        return None
    rows, cols = len(grid), len(grid[0])
    sx, sy = start
    ex, ey = end
    def oob(x, y):
        return not (0 <= x < cols and 0 <= y < rows)
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