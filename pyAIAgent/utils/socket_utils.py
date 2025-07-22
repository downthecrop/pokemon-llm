import struct

def _flush_socket(sock) -> None:
    """
    Drain any pending data from sock so that our next recv()
    only sees the fresh response to the command we send.
    """
    # Switch to non-blocking so recv() returns immediately if no data
    sock.setblocking(False)
    try:
        while True:
            data = sock.recv(4096)
            if not data:
                break
    except (BlockingIOError, OSError):
        # No more data to read
        pass
    finally:
        # Go back to blocking mode
        sock.setblocking(True)

def readrange(sock, address: str, length: str) -> bytes:
    _flush_socket(sock)
    cmd = f"READRANGE {address} {length}\n".encode('utf-8')
    sock.sendall(cmd)
    hdr = sock.recv(4)
    if len(hdr) < 4:
        raise RuntimeError("socket closed during READRANGE header")
    size = struct.unpack(">I", hdr)[0]
    data = bytearray()
    while len(data) < size:
        chunk = sock.recv(size - len(data))
        if not chunk:
            raise RuntimeError("socket closed mid-dump")
        data.extend(chunk)
    return bytes(data)


def send_command(sock, cmd: str) -> str:
    _flush_socket(sock)
    sock.sendall((cmd.strip() + "\n").encode('utf-8'))
    data = bytearray()
    while True:
        chunk = sock.recv(4096)
        if not chunk:
            raise RuntimeError("socket closed before full response")
        data.extend(chunk)
        if b"\n" in chunk:
            break
    return data.decode('utf-8').rstrip("\n")
