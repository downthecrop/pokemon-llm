import struct
import pathlib
from PIL import Image, ImageDraw

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

def capture(sock, filename: str = "latest.png", cell_size: int = 16) -> None:
    from pyAIAgent.utils.socket_utils import _flush_socket
    # flush any leftover bytes
    _flush_socket(sock)

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

    # build image from raw data
    img = Image.frombytes("RGBA", size, bytes(data), "raw", "ARGB")

    # draw the 16×16 grid
    draw = ImageDraw.Draw(img)
    w, h = img.size
    grid_color = (255, 0, 0, 128)  # semi-transparent red

    for x in range(0, w + 1, cell_size):
        draw.line(((x, 0), (x, h)), fill=grid_color)
    for y in range(0, h + 1, cell_size):
        draw.line(((0, y), (w, y)), fill=grid_color)

    # save
    path = pathlib.Path(filename)
    img.save(path)
