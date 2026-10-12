#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
4B1 patch v3 - hook the `bl <mailbox reader>` inside the CAN RX dispatcher, BEFORE the mailbox
is split by type. Fires for EVERY received frame (type 1/2/3). The cave calls the original
reader (to get mb), reads the FlexCAN mailbox, filters CAN-ID==0x4B1 (or --canid), reassembles
ISO-TP -> media store, and returns mb (transparent to the dispatcher).

Usage: python apply_patch_v3.py <in.bin> <out.bin> [--canid 0xNNN] [--version 14.12|15.07]

The firmware version is recognised from the image bytes (see fw_versions.py); an image that
does not match exactly one supported version is refused before anything is written.
--version only restricts the check to that entry - it never makes an unknown image acceptable.

The last step balances the firmware's flash self-test (fw_versions.balance): one word of linker
fill is set so that the patched image adds up to 0, as the factory image does. Without it the
cluster stores DTC U3000 and a car with adaptive cruise control reports "ACC not available".

--canid retargets the filtered CAN ID (default 0x4B1). EXPERIMENTAL - some head units
broadcast Bluetooth metadata on a different ID; see docs/TESTING.md. It only works for IDs
the cluster already accepts into a mailbox; other IDs need a mailbox reconfiguration.
"""
import os
import struct
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import fw_versions as FV                                                  # noqa: E402

CANID_DEFAULT = 0x4B1
# CAN ID the cave filters on. Default 0x4B1 (Bluetooth on the car this was built on).
# --canid retargets it - EXPERIMENTAL. It only works for IDs the cluster already receives into a
# mailbox (its acceptance table holds: 4B1, 4B3, 4C0, 4C1, 4C6, 4C7, 4D0, 4D2, 4D4, 4D5 - the same
# in every supported version). Our hook sits in the mailbox dispatch BEFORE any ID routing, so any
# accepted ID reaches the cave; an ID outside that list is hardware-filtered before it ever arrives
# and would also need a mailbox reconfiguration (not done here). Verified in the emulator for 0x4C6.


def hw(v): return struct.pack('>H', v & 0xFFFF)


def program(c, canid):
    """The cave, as a list of Thumb instructions. Identical for every version - only the
    addresses in `c` (fw_versions.cfg) change. Do not edit without re-testing on a car: the
    default 14.12 build must stay byte-identical to the image confirmed on the car."""
    ORIG, PERBUS, DLIST, SABC = c["ORIG"], c["PERBUS"], c["DLIST"], c["SABC"]
    BUF, SLEN, SIDX, SBUS = c["BUF"], c["SLEN"], c["SIDX"], c["SBUS"]
    P = []
    def I(*a): P.append(a)
    def L(n): P.append(('L', n))

    I('push',[4,5,6,7])
    I('ldrl',3,('c',SBUS)); I('strb_i',0,3,0)           # save bus
    I('bl',ORIG)                                          # r0 = mb
    I('ldrl',3,('c',SBUS)); I('strb_i',0,3,1)           # save mb
    # mailbox = PERBUS[bus] + mb*16 + 0x80
    I('ldrb_i',0,3,0); I('ldrl',2,('c',PERBUS)); I('lsls_i',1,0,2); I('ldr_r',4,2,1)
    I('ldrb_i',1,3,1); I('lsls_i',1,1,4); I('adds_r',4,4,1); I('adds_i8',4,0x80)
    # CAN-ID == 0x4B1 ? (4B1 only - 4B0 carries the same text; handling both = interleave + buffer corruption)
    I('ldr_iw',0,4,1); I('lsrs_i',0,0,18)
    I('ldrl',1,('c',0x7FF)); I('ands',0,1)
    I('ldrl',1,('c',canid)); I('cmp_r',0,1); I('bcc',1,'L_orig')
    I('adds_i3',6,4,0); I('adds_i8',6,8)                 # r6 = data (mb+8)
    I('ldrb_i',0,6,0); I('lsrs_i',2,0,4)
    I('cmp_i',2,1); I('bcc',0,'L_ff')
    I('cmp_i',2,2); I('bcc',0,'L_cf')
    I('cmp_i',2,0); I('bcc',0,'L_sf')      # Single Frame (short/empty field)
    I('b','L_orig')
    L('L_sf')                               # SF: whole payload in one frame -> immediately complete
    I('ldrb_i',1,6,0); I('movs_i',0,0x0F); I('ands',1,0)   # L = d0 & 0x0F
    I('ldrl',3,('c',SLEN)); I('strb_i',1,3,0)              # exp_len = L
    I('ldrl',5,('c',BUF)); I('movs_i',2,0)                 # i=0
    L('L_sfl')
    I('cmp_r',2,1); I('bcc',2,'L_sfd')                     # i >= L -> done
    I('adds_r',0,6,2); I('ldrb_i',0,0,1); I('strb_r',0,5,2)  # BUF[i] = d[1+i]
    I('adds_i3',2,2,1); I('b','L_sfl')
    L('L_sfd')
    I('ldrl',3,('c',SIDX)); I('strb_i',1,3,0); I('b','L_check')  # idx = L (complete)
    L('L_ff')
    # Clamp 23: text <=19 chars. This is the architecture's natural ceiling (the stock field
    # handler caps at 19, the title store is 20B, the renderer buffers are 20B). It also matches
    # the Bluetooth module's own limit: it truncates every field to 18 chars + a '~' marker (19
    # total) at the source, so nothing longer ever arrives. Pairs with apply_render.py (title ->
    # sp+0x40 20B, artist -> sp+0x54). BUF is 24B (BUF..BUF+0x17), exp_len=23 -> null@BUF[23]
    # = last byte of BUF (no collision with SLEN). See docs/HOW_IT_WORKS.md.
    I('ldrb_i',1,6,1); I('cmp_i',1,23); I('bcc',9,'L_ffok'); I('movs_i',1,23)
    L('L_ffok')
    I('ldrl',3,('c',SLEN)); I('strb_i',1,3,0); I('ldrl',5,('c',BUF)); I('movs_i',2,0)
    L('L_ffl')
    I('adds_r',0,6,2); I('ldrb_i',0,0,2); I('strb_r',0,5,2)
    I('adds_i3',2,2,1); I('cmp_i',2,6); I('bcc',3,'L_ffl')
    I('ldrl',3,('c',SIDX)); I('movs_i',0,6); I('strb_i',0,3,0); I('b','L_check')
    L('L_cf')
    I('ldrl',3,('c',SIDX)); I('ldrb_i',2,3,0); I('cmp_i',2,0); I('bcc',0,'L_orig')
    I('ldrl',5,('c',BUF)); I('movs_i',1,0)
    L('L_cfl')
    I('adds_r',0,2,1); I('cmp_i',0,23); I('bcc',2,'L_cfd')
    I('adds_r',0,6,1); I('ldrb_i',0,0,1); I('adds_r',3,5,2); I('strb_r',0,3,1)
    I('adds_i3',1,1,1); I('cmp_i',1,7); I('bcc',3,'L_cfl')
    L('L_cfd')
    I('adds_r',2,2,1); I('ldrl',3,('c',SIDX)); I('strb_i',2,3,0)
    L('L_check')
    I('ldrl',3,('c',SLEN)); I('ldrb_i',0,3,0); I('ldrl',3,('c',SIDX)); I('ldrb_i',2,3,0)
    I('cmp_i',0,5); I('bcc',3,'L_orig'); I('cmp_r',2,0); I('bcc',3,'L_orig')
    I('ldrl',5,('c',BUF)); I('movs_i',1,0); I('strb_r',1,5,0)   # NUL-terminate at BUF[len]
    I('ldrb_i',1,5,1); I('cmp_i',1,0x12); I('bcc',1,'L_rst')    # source byte 0x12 = Bluetooth
    # walk the BT field-handler list like the stock media dispatcher: {flag, field, -, handler}
    I('adds_i3',1,5,0); I('ldrl',2,('c',DLIST)); I('ldrl',4,('c',SABC))
    L('L_dl')
    I('ldr_iw',3,2,1); I('cmp_i',3,0); I('bcc',0,'L_rst')
    I('ldrb_i',5,2,1); I('ldrb_i',0,1,0); I('cmp_r',5,0); I('bcc',1,'L_dn')
    I('ldrb_i',5,2,0); I('cmp_i',5,0); I('bcc',1,'L_dn')
    I('str_iw',2,4,0); I('ldrl',0,('lbit','L_ret')); I('mov_hi',14,0); I('bx',3)
    L('L_ret'); I('b','L_rst')
    L('L_dn'); I('adds_i8',2,8); I('b','L_dl')
    L('L_rst')
    I('ldrl',3,('c',SIDX)); I('movs_i',0,0); I('strb_i',0,3,0)
    L('L_orig')
    I('ldrl',3,('c',SBUS)); I('ldrb_i',0,3,1)            # r0 = mb (return value for the dispatcher)
    I('pop',[4,5,6,7])
    return P


def enc(m, a, addr, lab, lit):
    if m=='push': return hw(0xB400|0x0100|sum(1<<r for r in a[0]))     # +lr
    if m=='pop':  return hw(0xBC00|0x0100|sum(1<<r for r in a[0]))     # +pc
    if m=='movs_i': return hw(0x2000|(a[0]<<8)|(a[1]&0xFF))
    if m=='cmp_i':  return hw(0x2800|(a[0]<<8)|(a[1]&0xFF))
    if m=='adds_i8':return hw(0x3000|(a[0]<<8)|(a[1]&0xFF))
    if m=='adds_i3':return hw(0x1C00|(a[2]<<6)|(a[1]<<3)|a[0])
    if m=='adds_r': return hw(0x1800|(a[2]<<6)|(a[1]<<3)|a[0])
    if m=='lsls_i': return hw(0x0000|(a[2]<<6)|(a[1]<<3)|a[0])
    if m=='lsrs_i': return hw(0x0800|((a[2]&0x1F)<<6)|(a[1]<<3)|a[0])
    if m=='ands':   return hw(0x4000|(a[1]<<3)|a[0])
    if m=='cmp_r':  return hw(0x4280|(a[1]<<3)|a[0])
    if m=='mov_hi': return hw(0x4600|((a[0]>>3)<<7)|((a[1]&0xF)<<3)|(a[0]&7))
    if m=='ldrb_i': return hw(0x7800|(a[2]<<6)|(a[1]<<3)|a[0])
    if m=='strb_i': return hw(0x7000|(a[2]<<6)|(a[1]<<3)|a[0])
    if m=='strb_r': return hw(0x5400|(a[2]<<6)|(a[1]<<3)|a[0])
    if m=='ldr_r':  return hw(0x5800|(a[2]<<6)|(a[1]<<3)|a[0])
    if m=='ldr_iw': return hw(0x6800|(a[2]<<6)|(a[1]<<3)|a[0])
    if m=='str_iw': return hw(0x6000|(a[2]<<6)|(a[1]<<3)|a[0])
    if m=='bx':     return hw(0x4700|(a[0]<<3))
    if m=='ldrl':
        la=lit[a[1]]; off=la-((addr+4)&~3)
        assert 0<=off<=0x3FC and off%4==0, f"ldrl {off:#x}@{addr:#x}"
        return hw(0x4800|(a[0]<<8)|(off>>2))
    if m=='b':
        off=lab[a[0]]-(addr+4); assert -2048<=off<=2046
        return hw(0xE000|((off>>1)&0x7FF))
    if m=='bcc':
        off=lab[a[1]]-(addr+4); assert -256<=off<=254, f"bcc {off}@{addr:#x}"
        return hw(0xD000|(a[0]<<8)|((off>>1)&0xFF))
    if m=='bl': return FV.ebl(addr,a[0])
    raise ValueError(m)


def build_cave(c, canid=CANID_DEFAULT):
    """Assemble the cave for version config `c` (fw_versions.cfg) at c["CAVE"] -> bytes."""
    assert 0 <= canid <= 0x7FF, "CAN ID out of 11-bit range"
    P = program(c, canid)
    addr = c["CAVE"]; lab = {}
    for op in P:
        if op[0] == 'L': lab[op[1]] = addr
        else: addr += 4 if op[0] == 'bl' else 2
    pool = addr + (addr & 2); lits = []
    for op in P:
        if op[0] == 'ldrl' and op[2] not in lits: lits.append(op[2])
    lit = {}; a = pool
    for k in lits: lit[k] = a; a += 4
    total = a - c["CAVE"]
    out = bytearray(); addr = c["CAVE"]
    for op in P:
        if op[0] == 'L': continue
        out += enc(op[0], op[1:], addr, lab, lit); addr += 4 if op[0] == 'bl' else 2
    while addr < pool: out += hw(0x46C0); addr += 2          # nop - align the literal pool
    for k in lits: out += struct.pack('>I', k[1] if k[0] == 'c' else (lab[k[1]] | 1)); addr += 4
    assert len(out) == total
    return bytes(out)


class Refused(Exception):
    """The image is not one we can patch; nothing was written."""


def patch(data, canid=CANID_DEFAULT, version=None, log=print):
    """Install hook + cave into a copy of `data`. Returns (patched bytes, version).
    Raises Refused (with the reason) instead of writing anything questionable."""
    v, state, reports = FV.identify(data)
    if version is not None and version not in FV.VERSIONS:
        raise Refused(f"unknown version '{version}' - known: {', '.join(FV.VERSIONS)}")
    if v is None or (version is not None and v != version):
        if version is not None and v is not None:
            raise Refused(f"--version {version} given, but the image matches {v}")
        raise Refused(FV.explain_refusal(data, reports))
    c = FV.cfg(v)
    log(f"version       : {v} ({c['part']}; {c['status']})")
    b = FV.banner(data)
    if b and b != v:
        log(f"  WARNING: the image banner says ROM Ver:{b} but the anchors match {v}")
    for name, ok, detail in reports[v]:
        log(f"  anchor {name:<13}: {'OK' if ok else 'MISMATCH'}  {detail}")
    if state["cave"] == "patched":
        raise Refused("this image already contains the hook and cave - it is already patched.")
    cave = build_cave(c, canid)
    co = c["CAVE"] - FV.BASE
    if not all(x == c["CAVE_FILL"] for x in data[co:co + len(cave)]):
        raise Refused(f"cave window {c['CAVE']:#x}+{len(cave)} is not free "
                      f"(expected every byte == 0x{c['CAVE_FILL']:02X})")
    log(f"CAN ID filter : {canid:#05x}" + ("" if canid == CANID_DEFAULT else "  (EXPERIMENTAL - non-default ID)"))
    log(f"HOOK @{c['HOOK']:#x}: bl {c['ORIG']:#x} -> bl {c['CAVE']:#x}  ({FV.ebl(c['HOOK'], c['CAVE']).hex(' ')})")
    log(f"CAVE {len(cave)} B @{c['CAVE']:#x} (fill 0x{c['CAVE_FILL']:02X}); "
        f"SRAM scratch {c['BUF']:#x}..{c['BUF'] + c['SCRATCH_SIZE'] - 1:#x}")
    d = bytearray(data)
    ho = c["HOOK"] - FV.BASE
    d[ho:ho + 4] = FV.ebl(c["HOOK"], c["CAVE"])
    d[co:co + len(cave)] = cave
    if "GUARD_LITS" in c:
        # the scratch lives below the stack-guard band: move the band's lower bound above it
        old, new = struct.pack(">I", c["GUARD_OLD"]), struct.pack(">I", c["GUARD_NEW"])
        n = bytes(data[:co]).count(old)
        if n != len(c["GUARD_LITS"]):
            raise Refused(f"stack-guard bound {c['GUARD_OLD']:#x} occurs {n} times in the firmware, "
                          f"expected {len(c['GUARD_LITS'])}")
        if not (c["GUARD_OLD"] <= c["BUF"] and c["BUF"] + c["SCRATCH_SIZE"] <= c["GUARD_NEW"]):
            raise Refused("scratch is not inside the memory taken from the stack-guard band")
        for a in c["GUARD_LITS"]:
            d[a - FV.BASE:a - FV.BASE + 4] = new
        log(f"stack-guard band bound {c['GUARD_OLD']:#x} -> {c['GUARD_NEW']:#x} "
            f"(3 literals) - the scratch gets memory of its own")
    try:
        out = FV.balance(data, d, c, log)       # keep the firmware's flash self-test passing
    except ValueError as e:
        raise Refused(str(e))
    return out, v


def parse_args(argv):
    canid, version, files = CANID_DEFAULT, None, []
    i = 0
    while i < len(argv):
        t = argv[i]
        if t == '--canid':
            canid = int(argv[i + 1], 0); i += 2; continue
        if t == '--version':
            version = argv[i + 1]; i += 2; continue
        files.append(t); i += 1
    if not 0 <= canid <= 0x7FF:
        raise SystemExit("CAN ID out of 11-bit range")
    fi = files[0] if len(files) > 0 else 'main.bin'
    fo = files[1] if len(files) > 1 else 'main_patched.bin'
    return fi, fo, canid, version


def main(argv=None):
    sys.stdout.reconfigure(encoding='utf-8', errors='replace')
    fi, fo, canid, version = parse_args(sys.argv[1:] if argv is None else argv)
    try:
        data = open(fi, 'rb').read()
    except OSError as e:
        print(f"cannot read {fi}: {e}"); return 2
    try:
        out, _ = patch(data, canid, version)
    except Refused as e:
        print(f"REFUSED: {e}"); return 1
    open(fo, 'wb').write(out)
    print("Saved", fo)
    return 0


if __name__ == '__main__':
    sys.exit(main())
