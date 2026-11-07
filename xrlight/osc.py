"""Small OSC over UDP, only for the helmet position from Unreal.
Only int and float, anything else gets ignored.
"""
from __future__ import annotations

import math
import select
import socket
import struct
import time

HELMET_ADDRESS = "/xr/helmet"

Message = tuple[str, list[float]]


def _pad(b: bytes) -> bytes:
    """pad to 4 bytes"""
    b += b"\x00"
    return b + b"\x00" * (-len(b) % 4)


def _read_string(data: bytes, pos: int) -> tuple[str, int]:
    end = data.index(b"\x00", pos)
    return data[pos:end].decode("ascii"), (end + 4) & ~3


def build_message(address: str, *args: float) -> bytes:
    tags, payload = ",", b""
    for a in args:
        if isinstance(a, int) and not isinstance(a, bool):
            tags, payload = tags + "i", payload + struct.pack(">i", a)
        else:
            tags, payload = tags + "f", payload + struct.pack(">f", float(a))
    return _pad(address.encode("ascii")) + _pad(tags.encode("ascii")) + payload


def parse_packet(data: bytes) -> list[Message]:
    """broken packet -> []"""
    try:
        if data.startswith(b"#bundle\x00"):
            out, pos = [], 16                 # "#bundle\0" + 8-byte timetag
            while pos + 4 <= len(data):
                size = struct.unpack_from(">i", data, pos)[0]
                out += parse_packet(data[pos + 4:pos + 4 + size])
                pos += 4 + size
            return out
        address, pos = _read_string(data, 0)
        tags, pos = _read_string(data, pos)
        if not address.startswith("/") or not tags.startswith(","):
            return []
        args: list[float] = []
        for t in tags[1:]:
            if t == "f":
                args.append(struct.unpack_from(">f", data, pos)[0])
            elif t == "i":
                args.append(struct.unpack_from(">i", data, pos)[0])
            else:
                return []
            pos += 4
        return [(address, args)]
    except (ValueError, struct.error, UnicodeDecodeError):
        return []


class HelmetReceiver:
    """keeps the last /xr/helmet x y z (cm), call poll() every frame"""

    def __init__(self, port: int, ip: str = "0.0.0.0", clock=time.perf_counter):
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            self.sock.bind((ip, port))
        except OSError as exc:
            self.sock.close()
            raise OSError(f"UDP port {port} is already in use (another OSC receiver?)") from exc
        self.sock.setblocking(False)
        self.port = self.sock.getsockname()[1]
        self.clock = clock
        self.position: tuple[float, float, float] | None = None
        self.received_at: float | None = None
        self.source: str = "-"
        self.count = 0

    def poll(self, timeout: float = 0.0) -> None:
        ready, _, _ = select.select([self.sock], [], [], max(0.0, timeout))
        if not ready:
            return
        while True:
            try:
                data, addr = self.sock.recvfrom(4096)
            except (BlockingIOError, InterruptedError):
                return
            except ConnectionResetError:
                continue
            for address, args in parse_packet(data):
                if address == HELMET_ADDRESS and len(args) >= 3:
                    self.position = (float(args[0]), float(args[1]), float(args[2]))
                    self.received_at = self.clock()
                    self.source = addr[0]
                    self.count += 1

    def age(self, now: float | None = None) -> float | None:
        """seconds since last message"""
        if self.received_at is None:
            return None
        return (self.clock() if now is None else now) - self.received_at

    def take_count(self) -> int:
        n, self.count = self.count, 0
        return n

    def close(self) -> None:
        self.sock.close()


class OscSender:
    def __init__(self, target: tuple[str, int]):
        self.target = target
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)

    def send(self, address: str, *args: float) -> None:
        self.sock.sendto(build_message(address, *args), self.target)

    def close(self) -> None:
        self.sock.close()


# no DMX here

def run_helmet(port: int, seconds: float | None = None, log=print) -> None:
    """just prints where the helmet is"""
    receiver = HelmetReceiver(port)
    log(f"helmet: listening for {HELMET_ADDRESS} on UDP {receiver.port} (no DMX is sent)")
    start = time.perf_counter()
    report_at = start + 1.0
    try:
        while seconds is None or time.perf_counter() - start < seconds:
            receiver.poll(max(0.0, report_at - time.perf_counter()))
            now = time.perf_counter()
            if now < report_at:
                continue
            report_at = now + 1.0
            rate, pos = receiver.take_count(), receiver.position
            if pos is None:
                log("no helmet data yet")
            else:
                log(f"x {pos[0]:7.1f}  y {pos[1]:7.1f}  z {pos[2]:6.1f} cm | {rate:3d} msg/s | "
                    f"from {receiver.source} | last {receiver.age(now) * 1000:5.0f} ms ago")
    except KeyboardInterrupt:
        pass
    finally:
        receiver.close()


def run_fake_helmet(target: tuple[str, int], radius: float = 150.0, period_s: float = 30.0,
                    rate: float = 50.0, height: float = 170.0, seconds: float | None = None,
                    log=print) -> None:
    """fake helmet walking in a circle, for testing at home"""
    sender = OscSender(target)
    log(f"fake helmet -> {target[0]}:{target[1]}: circle r {radius:g} cm, "
        f"{period_s:g} s per lap, {rate:g} msg/s")
    start = time.perf_counter()
    try:
        n = 0
        while seconds is None or time.perf_counter() - start < seconds:
            t = time.perf_counter() - start
            a = 2 * math.pi * t / period_s
            sender.send(HELMET_ADDRESS, radius * math.cos(a), radius * math.sin(a), height)
            n += 1
            time.sleep(max(0.0, start + n / rate - time.perf_counter()))
    except KeyboardInterrupt:
        pass
    finally:
        sender.close()
