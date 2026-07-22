#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
apply_render_18.py - patch the BT Audio screen renderer (FUN_0x1ce94) so that title and
artist can be up to ~18 characters (instead of 15), WITHOUT changing the frame size (the
epilogue 0x1ce8c is SHARED with the neighbouring function - changing `sub sp` would break it).

The bug: the renderer loads the title via a getter into sp+0x40 (a 20-byte buffer, up to
sp+0x54), then NEEDLESSLY copies it with strcpy into the cramped sp+0x64 (16B, right next to
the saved registers at sp+0x74). The artist goes to sp+0x54 (16B, right next to the title at
sp+0x64) and at >=16 chars overflows a NUL into title[0] -> blank title.

The fix: redirect the 5 title references from sp+0x64 to sp+0x40 (title stays in the getter
buffer, 20B -> <=19 chars). The artist at sp+0x54 then has the full 32B up to sp+0x74 (saved
registers), and the title strcpy at 0x1ceda becomes strcpy(sp+0x40, sp+0x40) = a safe no-op
(no write to sp+0x64, so a long title can't overflow into the saved registers either).

  add r0,sp,#0x64  (0xA819)  ->  add r0,sp,#0x40  (0xA810)
  @ 0x1cedc (title strcpy, becomes self-copy), 0x1cefe (empty-title branch),
    0x1cf1a (0x3a320), 0x1cf32 (0x1ccea), 0x1cf4a (0x409ea draw)

Usage: python apply_render_18.py <in.bin> <out.bin>   (operates on the code partition, base 0x5000)
"""
import sys, struct

BASE = 0x5000
OLD  = 0xA819   # add r0, sp, #0x64
NEW  = 0xA810   # add r0, sp, #0x40
SITES = [0x1cedc, 0x1cefe, 0x1cf1a, 0x1cf32, 0x1cf4a]

# guard: the frame/epilogue MUST stay untouched
FRAME_SUB = (0x1ce96, 0xB09D)   # sub sp,#0x74
EPI_ADD   = (0x1ce8c, 0xB01D)   # add sp,#0x74 (shared epilogue)

def rd16(d, addr): return struct.unpack('>H', d[addr-BASE:addr-BASE+2])[0]
def wr16(d, addr, v): d[addr-BASE:addr-BASE+2] = struct.pack('>H', v)

def main():
    fi = sys.argv[1] if len(sys.argv) > 1 else 'main_patched.bin'
    fo = sys.argv[2] if len(sys.argv) > 2 else 'main_patched.bin'
    d = bytearray(open(fi, 'rb').read())

    # frame/epilogue guard
    for addr, exp in (FRAME_SUB, EPI_ADD):
        got = rd16(d, addr)
        print(f"  guard @{addr:#x}: {got:#06x} (expected {exp:#06x}) {'OK' if got==exp else 'MISMATCH!'}")
        assert got == exp, "frame/epilogue differs from expected - aborted"

    for addr in SITES:
        got = rd16(d, addr)
        assert got == OLD, f"@{addr:#x}: {got:#06x} != {OLD:#06x} (add r0,sp,#0x64)"
        wr16(d, addr, NEW)
        print(f"  @{addr:#x}: {OLD:#06x} -> {NEW:#06x}  (title sp+0x64 -> sp+0x40)")

    open(fo, 'wb').write(d)
    print(f"Saved {fo}  ({len(d)} B)")

if __name__ == '__main__':
    main()
