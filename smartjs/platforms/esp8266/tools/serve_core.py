#!/usr/bin/env python

#
# usage: tools/serve_core.py --iram firmware/0x00000.bin --irom firmware/0x11000.bin /tmp/esp-console.log
#
# Then you can connect with gdb. The ESP8266 SDK image provides a debugger with
# reasonable support of lx106. Example invocation:
#
# docker run -v $PWD:/cesanta -ti \
#    docker.cesanta.com:5000/esp8266-build-oss:1.3.0-cesanta \
#    xt-gdb /cesanta/smartjs/platforms/esp8266/build/smartjs.out \
#    -ex "target remote localhost:1234"
#
# If you run on OSX or windows, you have to put the IP of your host instead of
# localhost since gdb will run in a virtualmachine.

import SocketServer
import argparse
import base64
import json
import os
import sys

IRAM_BASE=0x40100000
IROM_BASE=0x40200000
ROM_BASE= 0x40000000

parser = argparse.ArgumentParser(description='Serve ESP core dump to GDB')
parser.add_argument('--port', dest='port', default=1234, type=int, help='listening port')
parser.add_argument('--iram', dest='iram', required=True, help='iram firmware section')
parser.add_argument('--iram_addr', dest='iram_addr',
                    type=lambda x: int(x,16), help='iram firmware section')
parser.add_argument('--irom', dest='irom', required=True, help='irom firmware section')
parser.add_argument('--irom_addr', dest='irom_addr',
                    type=lambda x: int(x,16), help='irom firmware section')
parser.add_argument('--rom', dest='rom', required=False, help='rom section')
parser.add_argument('--rom_addr', dest='rom_addr', default=ROM_BASE,
                    type=lambda x: int(x,16), help='rom section')
parser.add_argument('log', help='serial log containing core dump snippet')

args = parser.parse_args()

START_DELIM = '--- BEGIN CORE DUMP ---'
END_DELIM =   '---- END CORE DUMP ----'


class Core(object):
    def __init__(self, filename):
        dump = self._read(filename)
        self.mem = self._map_core(dump)
        self.mem.append(self._map_firmware(args.iram_addr, args.iram, IRAM_BASE))
        self.mem.append(self._map_firmware(args.irom_addr, args.irom, IROM_BASE))
        if args.rom_addr:
            self.mem.append(self._map_firmware(args.rom_addr, args.rom, ROM_BASE))
        self.regs = base64.decodestring(dump['REGS']['data'])

    def _search_backwards(self, f, start_offset, pattern):
        offset = start_offset
        while True:
            offset = max(0, offset - 10000)
            f.seek(offset)
            data = f.read(min(10000, start_offset))
            pos = data.rfind(pattern)
            if pos >= 0:
                return offset + pos
            elif offset == 0:
                return -1
            offset += 5000

    def _read(self, filename):
        with open(filename) as f:
            f.seek(0, os.SEEK_END)
            size = f.tell()
            end_pos = self._search_backwards(f, f.tell(), END_DELIM)
            if end_pos == -1:
                print >>sys.stderr, "Cannot find end delimiter:", END_DELIM
                os.exit(1)
            start_pos = self._search_backwards(f, end_pos, START_DELIM)
            if start_pos == -1:
                print >>sys.stderr, "Cannot find start delimiter:", START_DELIM
                os.exit(1)
            start_pos += len(START_DELIM)

            print >>sys.stderr, "Found core at %d - %d" % (start_pos, end_pos)
            f.seek(start_pos)
            core_json = f.read(end_pos - start_pos)
            print >>sys.stderr, "%s ... %s" % (core_json[:25], core_json[-25:])
            return json.loads(core_json.replace('\n', '').replace('\r', ''))

    def _map_core(self, core):
        mem = []
        for k, v in core.items():
            if not isinstance(v, dict) or k == 'REGS':
                continue
            data = base64.decodestring(v["data"])
            print >>sys.stderr, "Mapping {0} at {1:#02x}".format(k, v["addr"])
            mem.append((v["addr"], v["addr"] + len(data), data))
        return mem

    def _map_firmware(self, addr, filename, base):
        if addr is None:
            name = os.path.splitext(os.path.basename(filename))[0]
            addr = base + int(name, 16)
        with open(filename) as f:
            data = f.read()
            # ea 04 is the magic number number for new format ESP image files
            # as produced by rboot or esp-specific OTA enabled build scripts.
            # The new image format contains code relocated 16 bytes ahead.
            if ord(data[0]) == 0xea and ord(data[1]) == 0x04:
                addr += 0x10
            print >>sys.stderr, "Mapping {0} at {1:#02x}".format(filename, addr)
            return (addr, addr + len(data), data)

    def read(self, addr, size):
        for base, end, data in self.mem:
            if addr >= base and addr < end:
                return data[addr - base : addr - base + size]
        print >>sys.stderr, "Unmapped addr", hex(addr)
        return "\0" * size

class GDBHandler(SocketServer.BaseRequestHandler):
    def handle(self):
        core = Core(args.log)
        print >>sys.stderr, "Loaded core dump from last snippet in ", args.log

        while self.expect_packet_start():
            pkt = self.read_packet()
            if pkt == "?": # status -> trap
                self.send_str("S09")
            elif pkt == "g": # dump registers
                self.send_str(self.encode_bytes(core.regs))
            elif pkt[0] == "G": # set registers
                core.regs = self.decode_bytes(pkt[1:])
                self.send_str("OK")
            elif pkt[0] == "m": # read memory
                addr, size = pkt[1:].split(',')
                bs = core.read(int(addr, 16), int(size, 16))
                self.send_str(self.encode_bytes(bs))
            elif pkt.startswith("Hg"):
                self.send_str("OK")
            elif pkt.startswith("Hc-1"):
                # cannot continue, this is post mortem debugging
                self.send_str("E01")
            elif pkt == "qC":
                self.send_str("1")
            elif pkt == "qTStatus" or pkt == "qOffsets" or pkt.startswith("qSupported"):
                # silently ignore
                self.send_str("")
            elif pkt == "qAttached":
                self.send_str("1")
            elif pkt == "qSymbol::":
                self.send_str("OK")
            else:
                print >>sys.stderr, "Ignoring unknown command '%s'" % (pkt,)
                self.send_str("")

        print >>sys.stderr, "GDB closed the connection"

    def encode_bytes(self, bs):
        return "".join("{0:02x}".format(ord(i)) for i in bs)

    def decode_bytes(self, s):
        return s.decode('hex')

    def send_ack(self):
        self.request.sendall("+");

    def send_nack(self):
        self.request.sendall("-");

    def send_str(self, s):
        self.request.sendall("${0}#{1:02x}".format(s, self._checksum(s)))

    def _checksum(self, s):
        return sum(ord(i) for i in s) % 0x100

    def expect_packet_start(self):
        return len(self.read_until('$')) > 0

    def read_packet(self):
        pkt = self.read_until('#')
        chk = ""
        chk += self.request.recv(1)
        chk += self.request.recv(1)
        if len(chk) != 2:
            return ""
        if int(chk, 16) != self._checksum(pkt):
            print >>sys.stderr, "Bad checksum for {0}; got: {1} want: {2:02x}".format(pkt, chk, "want:", self._checksum(pkt))
            self.send_nack()
            return ""

        self.send_ack()
        return pkt

    def read_until(self, limit):
        buf = ''
        while True:
            ch = self.request.recv(1)
            if len(ch) == 0: # eof
                return ""
            if ch == limit:
                return buf
            buf += ch


class TCPServer(SocketServer.TCPServer):
    allow_reuse_address = True

server = TCPServer(('0.0.0.0', args.port), GDBHandler)
print "Waiting for gdb on", args.port
server.serve_forever()
