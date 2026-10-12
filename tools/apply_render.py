#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
apply_render.py - patch the BT Audio screen renderer so that title and artist can be up to
19 characters (instead of the stock 15), WITHOUT changing the frame size (the epilogue is
SHARED with the neighbouring function - changing `sub sp` would break it).

This patch only relocates a buffer; the actual character limit is set by the clamp in
apply_patch_v3.py (currently 19, which also matches the Bluetooth module's own truncation).

The bug: the renderer loads the title via a getter into sp+0x40 (a 20-byte buffer, up to
sp+0x54), then NEEDLESSLY copies it with strcpy into the cramped sp+0x64 (16B, right next to
the saved registers at sp+0x74). The artist goes to sp+0x54 (16B, right next to the title at
sp+0x64) and at >=16 chars overflows a NUL into title[0] -> blank title.

The fix: redirect the 5 title references from sp+0x64 to sp+0x40 (title stays in the getter
buffer, 20B -> <=19 chars). The artist at sp+0x54 then has the full 32B up to sp+0x74 (saved
registers), and the title strcpy becomes strcpy(sp+0x40, sp+0x40) = a safe no-op (no write to
sp+0x64, so a long title can't overflow into the saved registers either).

  add r0,sp,#0x64  (0xA819)  ->  add r0,sp,#0x40  (0xA810)   at renderer +0x48/+0x6A/+0x86/+0x9E/+0xB6
  (14.12: 0x1cedc title strcpy, 0x1cefe empty-title branch, 0x1cf1a, 0x1cf32, 0x1cf4a draw)

The renderer address comes from fw_versions.py (the image must be a supported version) and is
cross-checked by a structural search: exactly one cluster of five `add r0,sp,#0x64` at those
spacings, guarded by `sub sp,#0x74` and the shared `add sp,#0x74` epilogue. Both must agree.

Usage: python apply_render.py <in.bin> <out.bin>   (operates on the code partition, base 0x5000)
"""
import os
import struct
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import fw_versions as FV                                                  # noqa: E402

OLD, NEW = FV.RENDER_OLD, FV.RENDER_NEW


class Refused(Exception):
    """The image is not one we can patch; nothing was written."""


def _balance(before, after, c, log):
    try:
        return FV.balance(before, after, c, lambda s: log("  " + s))
    except ValueError as e:
        raise Refused(str(e))


def find_sites(d):
    """Structural search, independent of the version table: every place with five `A819` at the
    renderer spacings plus the frame/epilogue guards. Returns a list of renderer addresses."""
    rel = [o - FV.RENDER_SITES_REL[0] for o in FV.RENDER_SITES_REL]
    fr, ep = FV.RENDER_FRAME_REL[0] - FV.RENDER_SITES_REL[0], FV.RENDER_EPI_REL[0] - FV.RENDER_SITES_REL[0]
    w = lambda o: struct.unpack('>H', d[o:o + 2])[0]                     # noqa: E731
    out = []
    i = d.find(b'\xa8\x19')
    while i != -1:
        if i % 2 == 0 and i + fr >= 0 and i + ep >= 0 and i + rel[-1] + 2 <= len(d):
            if (w(i + fr) == FV.RENDER_FRAME_REL[1] and w(i + ep) == FV.RENDER_EPI_REL[1]
                    and all(w(i + o) == OLD for o in rel)):
                out.append(i + FV.BASE - FV.RENDER_SITES_REL[0])
        i = d.find(b'\xa8\x19', i + 1)
    return out


def patch(data, log=print):
    """Relocate the title buffer in a copy of `data`. Returns (patched bytes, version, changed).
    `changed` is False when there was nothing to do (output == input). The last step balances
    the firmware's flash self-test (fw_versions.balance) - also on an already patched image."""
    v, state, reports = FV.identify(data)
    if v is None:
        raise Refused(FV.explain_refusal(data, reports))
    c = FV.cfg(v)
    log(f"  version {v} ({c['part']}); renderer @ {c['RENDERER']:#x}")
    if state["render"] == "patched":
        log("  renderer already patched (all 5 sites are add r0,sp,#0x40) - nothing to do")
        out = _balance(data, data, c, log)      # an image patched by an older tool gets balanced
        return out, v, out != bytes(data)
    hits = find_sites(data)
    if hits != [c["RENDERER"]]:
        raise Refused(f"structural renderer search found {[hex(h) for h in hits]}, "
                      f"table says {c['RENDERER']:#x} - refusing")
    d = bytearray(data)
    for addr, exp in (c["RENDER_FRAME"], c["RENDER_EPI"]):          # the frame MUST stay untouched
        got = FV.u16(d, addr)
        log(f"  guard @{addr:#x}: {got:#06x} (expected {exp:#06x}) {'OK' if got == exp else 'MISMATCH!'}")
        if got != exp:
            raise Refused("frame/epilogue differs from expected")
    for addr in c["RENDER_SITES"]:
        got = FV.u16(d, addr)
        if got != OLD:
            raise Refused(f"@{addr:#x}: {got:#06x} != {OLD:#06x} (add r0,sp,#0x64)")
        d[addr - FV.BASE:addr - FV.BASE + 2] = struct.pack('>H', NEW)
        log(f"  @{addr:#x}: {OLD:#06x} -> {NEW:#06x}  (title sp+0x64 -> sp+0x40)")
    return _balance(data, d, c, log), v, True


def main(argv=None):
    sys.stdout.reconfigure(encoding='utf-8', errors='replace')
    argv = sys.argv[1:] if argv is None else argv
    fi = argv[0] if len(argv) > 0 else 'main_patched.bin'
    fo = argv[1] if len(argv) > 1 else 'main_patched.bin'
    try:
        data = open(fi, 'rb').read()
    except OSError as e:
        print(f"cannot read {fi}: {e}"); return 2
    try:
        out, _, _ = patch(data)
    except Refused as e:
        print(f"REFUSED: {e}"); return 1
    open(fo, 'wb').write(out)
    print(f"Saved {fo}  ({len(out)} B)")
    return 0


if __name__ == '__main__':
    sys.exit(main())
