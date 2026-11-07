# quick check that OSC from Unreal reaches the Mac: prints every message on UDP 9000
# usage: python tests/script_head.py [port]
import socket
import struct
import sys

port = int(sys.argv[1]) if len(sys.argv) > 1 else 9000
s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
s.bind(("0.0.0.0", port))
print(f"listening on {port}…")
pad = lambda n: (n + 4) & ~3

try:
    while True:
        data, addr = s.recvfrom(2048)
        try:
            end = data.index(b"\0"); address = data[:end].decode(errors="replace"); i = pad(end)
            if i >= len(data) or data[i:i+1] != b",":
                print(address, "(no arguments)", data); continue
            t_end = data.index(b"\0", i); tags = data[i+1:t_end].decode(); j = pad(t_end); vals = []
            for t in tags:
                if t == "f": vals.append(round(struct.unpack(">f", data[j:j+4])[0], 1)); j += 4
                elif t == "d": vals.append(round(struct.unpack(">d", data[j:j+8])[0], 1)); j += 8
                elif t == "i": vals.append(struct.unpack(">i", data[j:j+4])[0]); j += 4
            print(address, vals, "from", addr[0])
        except Exception as e:
            print("could not parse:", e, data)
except KeyboardInterrupt:
    pass
