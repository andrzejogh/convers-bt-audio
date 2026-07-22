#!/usr/bin/env python3
"""
vbf_tool.py - correct BIN <-> VBF converter for Ford Convers+ (IPC update).

VBF FORMAT (verified bit-for-bit against every VBF in the project):

    [ASCII text header, ends at '}']        <- contains file_checksum
    [binary block:]
        start_addr : uint32 BIG-ENDIAN      (flash=0x30000000, main=0x00005000)
        length     : uint32 BIG-ENDIAN      (flash=0x200000,   main=0xFB000)
        payload    : `length` bytes         <- exactly the contents of the .bin
        crc16      : uint16 BIG-ENDIAN      <- CRC-16/CCITT-FALSE over payload

    file_checksum in the header = CRC32 (zlib) over the WHOLE binary block
    (i.e. 8-byte block header + payload + 2-byte CRC16).

    ORDER when building: CRC16 first, then CRC32 (because CRC32 covers the CRC16).

Usage:
    python vbf_tool.py info   <file.vbf>
    python vbf_tool.py unpack <file.vbf> <out.bin>
    python vbf_tool.py pack   <template.vbf> <in.bin> <out.vbf>
    python vbf_tool.py verify <file.vbf>
    python vbf_tool.py selftest

'pack' takes the text header and block address from <template.vbf>, inserts <in.bin>
as the payload and recomputes BOTH checksums. The template MUST be from the same
family (same sw_part_number), otherwise the cluster gets firmware not meant for it.
"""
import binascii
import os
import re
import struct
import sys

sys.stdout.reconfigure(encoding='utf-8', errors='replace')


def crc16_ccitt_false(data):
    crc = 0xFFFF
    for b in data:
        crc ^= (b << 8)
        for _ in range(8):
            crc = ((crc << 1) ^ 0x1021) & 0xFFFF if (crc & 0x8000) else (crc << 1) & 0xFFFF
    return crc


def find_header_end(data):
    """End of the text header = the closing '}' of the header { ... } block."""
    depth = 0
    for i, b in enumerate(data[:16384]):
        if b == 0x7B:
            depth += 1
        elif b == 0x7D:
            depth -= 1
            if depth == 0:
                return i + 1
    raise ValueError("text header end not found")


class Vbf:
    def __init__(self, path):
        self.path = path
        data = open(path, 'rb').read()
        self.hdr_end = find_header_end(data)
        self.header_text = data[:self.hdr_end]
        self.binary = data[self.hdr_end:]
        self.addr, self.length = struct.unpack('>II', self.binary[:8])
        self.payload = self.binary[8:8 + self.length]
        self.crc16_stored = struct.unpack('>H', self.binary[-2:])[0]
        txt = self.header_text.decode('ascii', 'replace')
        m = re.search(r'sw_part_number\s*=\s*"([^"]+)"', txt)
        self.part_number = m.group(1) if m else '?'
        m = re.search(r'file_checksum\s*=\s*(0x[0-9A-Fa-f]+)', txt)
        self.file_checksum = int(m.group(1), 16) if m else None

    def check(self):
        errs = []
        if 8 + self.length + 2 != len(self.binary):
            errs.append(f"block size: got {len(self.binary)}, expected {8 + self.length + 2}")
        c16 = crc16_ccitt_false(self.payload)
        if c16 != self.crc16_stored:
            errs.append(f"CRC16: stored 0x{self.crc16_stored:04X}, computed 0x{c16:04X}")
        c32 = binascii.crc32(self.binary) & 0xFFFFFFFF
        if c32 != self.file_checksum:
            errs.append(f"file_checksum: header 0x{self.file_checksum:08X}, computed 0x{c32:08X}")
        return errs


def build(template_vbf, payload, out_path):
    """Build a VBF from the payload, using the header and address from the template."""
    t = Vbf(template_vbf)
    if len(payload) != t.length:
        raise ValueError(
            f"payload is {len(payload)} B, but template {os.path.basename(template_vbf)} "
            f"expects {t.length} B - wrong partition/family")

    block = struct.pack('>II', t.addr, t.length) + payload
    block += struct.pack('>H', crc16_ccitt_false(payload))          # STEP 1: CRC16
    checksum = binascii.crc32(block) & 0xFFFFFFFF                    # STEP 2: CRC32 over CRC16

    txt = t.header_text.decode('ascii')

    # Preserve the template's hex-case convention (some files use lowercase hex).
    m = re.search(r'file_checksum\s*=\s*0x([0-9A-Fa-f]+)', txt)
    if not m:
        raise ValueError("template has no file_checksum field")
    digits = m.group(1)
    lower = digits.lower() == digits and digits.upper() != digits
    hexstr = f'{checksum:08x}' if lower else f'{checksum:08X}'

    new_txt, n = re.subn(r'(file_checksum\s*=\s*)0x[0-9A-Fa-f]+',
                         lambda mm: mm.group(1) + '0x' + hexstr, txt)
    if n != 1:
        raise ValueError(f"could not replace file_checksum (matches: {n})")

    out = new_txt.encode('ascii') + block
    with open(out_path, 'wb') as f:
        f.write(out)
    return out_path, checksum, len(out)


def cmd_info(path):
    v = Vbf(path)
    print(f"file            : {path}")
    print(f"size            : {os.path.getsize(path)} B")
    print(f"sw_part_number  : {v.part_number}")
    print(f"text header     : {v.hdr_end} B")
    print(f"block start_addr: 0x{v.addr:08X}")
    print(f"block length    : 0x{v.length:X} ({v.length} B)")
    print(f"CRC16 (payload) : 0x{v.crc16_stored:04X}")
    print(f"file_checksum   : 0x{v.file_checksum:08X}")
    errs = v.check()
    print("validation      :", "OK - consistent" if not errs else "ERRORS:")
    for e in errs:
        print("   -", e)


def cmd_verify(path):
    v = Vbf(path)
    errs = v.check()
    if not errs:
        print(f"OK  {os.path.basename(path):<45} {v.part_number}  (checksums match)")
        return 0
    print(f"BAD {os.path.basename(path):<45} {v.part_number}")
    for e in errs:
        print("   -", e)
    return 1


def cmd_unpack(vbf, out_bin):
    v = Vbf(vbf)
    with open(out_bin, 'wb') as f:
        f.write(v.payload)
    print(f"unpacked payload: {out_bin} ({len(v.payload)} B), addr=0x{v.addr:08X}, part={v.part_number}")


def cmd_pack(template, in_bin, out_vbf):
    payload = open(in_bin, 'rb').read()
    t = Vbf(template)
    path, cks, size = build(template, payload, out_vbf)
    print(f"template      : {os.path.basename(template)}  (part={t.part_number}, addr=0x{t.addr:08X})")
    print(f"payload       : {in_bin} ({len(payload)} B)")
    print(f"file_checksum : 0x{cks:08X}")
    print(f"saved         : {path} ({size} B)")
    errs = Vbf(out_vbf).check()
    print("validation    :", "OK - consistent" if not errs else f"ERRORS: {errs}")


def cmd_selftest():
    """Correctness proof: unpack and repack every known VBF; the result must be
    byte-for-byte identical to the original."""
    import glob
    here = os.path.dirname(os.path.abspath(__file__))
    files = glob.glob(os.path.join(here, '..', 'firmware_fl', '**', '*.vbf'), recursive=True)
    ok = bad = skipped = 0
    for f in sorted(set(files)):
        try:
            v = Vbf(f)
        except Exception as e:
            print(f"SKIP  {os.path.basename(f):<45} ({e})")
            skipped += 1
            continue
        if v.check():
            print(f"SKIP  {os.path.basename(f):<45} (original has bad checksums - not usable as template)")
            skipped += 1
            continue
        tmp = os.path.join(os.environ.get('TEMP', '.'), '_rt.vbf')
        build(f, v.payload, tmp)
        same = open(tmp, 'rb').read() == open(f, 'rb').read()
        os.remove(tmp)
        print(f"{'OK   ' if same else 'FAIL '} round-trip  {os.path.basename(f):<45} {v.part_number}")
        ok += same
        bad += (not same)
    print(f"\nround-trip identical: {ok}, failures: {bad}, skipped: {skipped}")
    return 1 if bad else 0


def main():
    a = sys.argv[1:]
    if not a:
        print(__doc__)
        return 1
    c = a[0]
    if c == 'info' and len(a) == 2:
        cmd_info(a[1])
    elif c == 'verify' and len(a) == 2:
        return cmd_verify(a[1])
    elif c == 'unpack' and len(a) == 3:
        cmd_unpack(a[1], a[2])
    elif c == 'pack' and len(a) == 4:
        cmd_pack(a[1], a[2], a[3])
    elif c == 'selftest':
        return cmd_selftest()
    else:
        print(__doc__)
        return 1
    return 0


if __name__ == '__main__':
    sys.exit(main())
