"""Minimal Art-Net: only ArtDmx packets, which is all this project needs.

Packet layout (Art-Net 4): "Art-Net\\0", OpCode 0x5000 (little endian),
protocol version 14 (big endian), sequence, physical, 15-bit port-address
(SubUni low byte, Net high byte), data length (big endian, even, 2..512), data.
"""
from __future__ import annotations

import select
import socket
import struct

PORT = 6454
HEADER = b"Art-Net\x00"
OP_DMX = 0x5000
PROTOCOL_VERSION = 14


def build_artdmx(universe: int, data: bytes, sequence: int = 0, physical: int = 0) -> bytes:
    if not 0 <= universe <= 0x7FFF:
        raise ValueError("Art-Net universe (port-address) must be 0..32767")
    payload = bytes(data[:512])
    if len(payload) < 2:
        payload = payload.ljust(2, b"\x00")
    if len(payload) % 2:
        payload += b"\x00"
    return (HEADER
            + struct.pack("<H", OP_DMX)
            + struct.pack(">H", PROTOCOL_VERSION)
            + bytes([sequence & 0xFF, physical & 0xFF, universe & 0xFF, (universe >> 8) & 0x7F])
            + struct.pack(">H", len(payload))
            + payload)


def parse_artdmx(packet: bytes) -> tuple[int, bytes] | None:
    """Return (universe, dmx data) for an ArtDmx packet, None for anything else."""
    if len(packet) < 18 or packet[:8] != HEADER:
        return None
    if struct.unpack_from("<H", packet, 8)[0] != OP_DMX:
        return None
    universe = packet[14] | ((packet[15] & 0x7F) << 8)
    length = struct.unpack_from(">H", packet, 16)[0]
    return universe, bytes(packet[18:18 + length])


def parse_target(text: str, default_port: int = PORT) -> tuple[str, int]:
    """'10.55.4.7' or '127.0.0.1:6455' -> (host, port)."""
    host, _, port = text.partition(":")
    return host, int(port) if port else default_port


class PortBusyError(OSError):
    pass


class ArtNetSocket:
    """One UDP socket for sending and (optionally) receiving ArtDmx."""

    def __init__(self, bind_ip: str = "0.0.0.0", bind_port: int = PORT,
                 fallback_to_any_port: bool = False):
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
        try:
            self.sock.bind((bind_ip, bind_port))
        except OSError as exc:
            if not fallback_to_any_port:
                self.sock.close()
                raise PortBusyError(
                    f"UDP port {bind_port} is already in use (is QLC+ or another "
                    f"Art-Net program running?)") from exc
            self.sock.bind((bind_ip, 0))
        self.sock.setblocking(False)
        self.port = self.sock.getsockname()[1]
        self._sequence = 0

    def send(self, target: tuple[str, int], universe: int, data: bytes) -> None:
        self._sequence = self._sequence % 255 + 1   # 1..255; 0 would disable sequencing
        self.sock.sendto(build_artdmx(universe, data, self._sequence), target)

    def receive(self, timeout: float) -> list[tuple[int, bytes, tuple[str, int]]]:
        """Wait up to `timeout` seconds, then return every ArtDmx packet waiting."""
        packets = []
        ready, _, _ = select.select([self.sock], [], [], max(0.0, timeout))
        if not ready:
            return packets
        while True:
            try:
                raw, addr = self.sock.recvfrom(1024)
            except (BlockingIOError, InterruptedError):
                break
            except ConnectionResetError:   # ICMP "port unreachable" on some systems
                continue
            parsed = parse_artdmx(raw)
            if parsed:
                packets.append((parsed[0], parsed[1], addr))
        return packets

    def close(self) -> None:
        self.sock.close()

    def __enter__(self) -> "ArtNetSocket":
        return self

    def __exit__(self, *exc) -> None:
        self.close()
