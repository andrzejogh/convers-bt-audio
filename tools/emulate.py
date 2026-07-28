#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
emulate.py - test the Bluetooth-metadata patch on YOUR OWN firmware dump, in an emulator,
with NO car and NO CAN adapter.

What it does
------------
It loads your cluster firmware (main.bin) into a small ARM emulator (Unicorn), applies the
patch to a throw-away copy exactly the way you would flash it (it calls apply_patch_v3.py and
apply_render.py for you), then injects a synthetic Bluetooth metadata frame on CAN 0x4B1 -
the same ISO-TP framing the real Bluetooth module sends - and runs the actual cluster code
that handles it. Finally it reads back the cluster's media store and its screen renderer and
tells you, in plain language, whether the title/artist made it all the way to the display.

This answers the question "does the patch work on MY dump?" without touching the car. It also
pinpoints the usual reasons it "doesn't work":
  * the patch doesn't fit your firmware version  -> the anchors won't match, reported clearly
  * the flash didn't take                        -> dump a patched cluster and see 'patched'
  * your phone's metadata never shows            -> the patch is fine; your head unit likely
                                                    broadcasts on a different CAN ID (--canid)

Requirements
------------
  * Python 3
  * Unicorn CPU emulator:   pip install unicorn
  * (optional) Capstone, only for --trace:   pip install capstone
No car, no CAN adapter, no PCAN needed. Your own main.bin dump is the only input.

Usage
-----
  python emulate.py --dump main.bin
  python emulate.py --dump main.bin --title "PATCH TEST" --artist "CANBUS"
  python emulate.py --dump main.bin --canid 0x4C6         # test a non-default head-unit ID
  python emulate.py --dump my_flashed_cluster.bin         # verify an already-patched dump

The dump is never modified: patching happens on a temporary copy.
"""
import os, sys, struct, argparse, shutil, subprocess, tempfile

# ---------------------------------------------------------------------------
# Firmware map (code partition, base 0x5000 - same convention as the patch tools)
# ---------------------------------------------------------------------------
BASE = 0x5000
HOOK = 0x236f6          # patched: bl -> cave;  stock: bl 0x230e4
HOOK_SITE = 0x236f4     # 'adds r0,r4,#0' right before the hooked bl - our execution entry
HOOK_RESUME = 0x236fa   # instruction right after the hooked bl - where we stop
ORIG = 0x230e4          # framework RX helper the cave tail-calls to fetch the mailbox
CAVE = 0x83240          # the patch's code cave
CAVE_MAX = 320          # generous upper bound on cave length (for revert / emptiness checks)
PERBUS = 0x7a0d4        # table[bus] -> FlexCAN module base;  mailbox = base + mb*16 + 0x80

# cave scratch / ISO-TP reassembly state (SRAM)
SLEN = 0x40009318       # expected length
SIDX = 0x40009319       # bytes gathered so far
SBUS = 0x40009320       # +0 bus, +1 mailbox index

# media store the field handlers fill (SRAM) - what the BT Audio screen reads
STORE = {"title": 0x4000a75d, "artist": 0x4000a771, "album": 0x4000a799}

# BT Audio screen renderer + the status it gates on
RENDERER = 0x1ce94
RENDER_AFTER_GETTERS = 0x1cec2      # PC just past the title/artist/album getters
STATUS_SONGNO = 0x40007418
STATUS_FLAGS = 0x4000741d
# renderer working buffers AFTER apply_render.py relocates the title (title -> sp+0x40)
RBUF = {"title": 0x40, "artist": 0x2c, "album": 0x18}

# apply_render.py rewrite sites: 0xA819 (add r0,sp,#0x64) -> 0xA810 (#0x40)
RENDER_SITES = [0x1cedc, 0x1cefe, 0x1cf1a, 0x1cf32, 0x1cf4a]
RENDER_OLD, RENDER_NEW = 0xA819, 0xA810

# CAN IDs the cluster's hardware acceptance table (@0x79446) actually receives into a mailbox.
# From docs/HOW_IT_WORKS.md section 13. An ID outside this set is filtered in hardware before the
# firmware ever sees it, so even a correct patch can't display it without a mailbox reconfig
# (which this patch doesn't do). Used only to warn - the emulator can still demonstrate any ID.
ACCEPTED_IDS = {0x4B1, 0x4B3, 0x4C0, 0x4C1, 0x4C6, 0x4C7, 0x4D0, 0x4D2, 0x4D4, 0x4D5}

# CAN payload fields
FIELD = {"title": 0x46, "artist": 0x42}
SRC_BT = 0x12

# emulator memory regions (same as the dev harness emu.py)
CODE_BASE, CODE_SIZE = 0x00000000, 0x00100000
SRAM_BASE, SRAM_SIZE = 0x40000000, 0x00010000
STACK_BASE, STACK_SIZE = 0x50000000, 0x00010000
PERIPH_BASE, PERIPH_SIZE = 0xFC000000, 0x00100000
KOD_LOAD = 0x5000
SENTINEL = 0x0000FFF0

FAKE_MB_BASE = 0x4000c000   # our stand-in FlexCAN module base (free SRAM)
INJECT_BUS, INJECT_MB = 0, 1


# ---------------------------------------------------------------------------
# ISO-TP framing - identical to make_test_frames.py (kept local so this script
# is self-contained even if run on its own)
# ---------------------------------------------------------------------------
def field_frames(field, src, text):
    """ISO-TP frames (each a list of 8 data bytes) carrying [field][src][01][01][text]."""
    tb = text.encode("latin-1", "replace")
    if len(tb) <= 3:
        payload = bytes([field, src, 0x01, 0x01]) + tb
        return [list((bytes([len(payload)]) + payload).ljust(8, b"\x00"))]
    payload = bytes([field, src, 0x01, 0x01]) + tb + b"\x00"
    L = len(payload)
    frames = [list((bytes([0x10 | ((L >> 8) & 0x0F), L & 0xFF]) + payload[:6]).ljust(8, b"\x00"))]
    i, seq = 6, 1
    while i < L:
        frames.append(list((bytes([0x20 | (seq & 0x0F)]) + payload[i:i + 7]).ljust(8, b"\x00")))
        i += 7
        seq = (seq + 1) & 0x0F
    return frames


# ---------------------------------------------------------------------------
# tiny helpers shared with the patch tools
# ---------------------------------------------------------------------------
def ebl(src, dst):
    """Encode a Thumb BL from src to dst (big-endian), same as the patch scripts."""
    o = (dst - (src + 4)) & 0x1FFFFFF
    S = (o >> 24) & 1; i1 = (o >> 23) & 1; i2 = (o >> 22) & 1
    im10 = (o >> 12) & 0x3FF; im11 = (o >> 1) & 0x7FF
    j1 = (~(i1 ^ S)) & 1; j2 = (~(i2 ^ S)) & 1
    return struct.pack(">HH", 0xF000 | (S << 10) | im10, 0xD000 | (j1 << 13) | (j2 << 11) | im11)


# ---------------------------------------------------------------------------
# Unicorn harness (subset of emu.py, English, loads any dump - no hard-coded path)
# ---------------------------------------------------------------------------
class Emu:
    def __init__(self, image_path, trace=False):
        from unicorn import (Uc, UC_ARCH_ARM, UC_MODE_THUMB, UC_MODE_BIG_ENDIAN,
                             UC_HOOK_CODE, UC_HOOK_MEM_UNMAPPED)
        from unicorn.arm_const import UC_ARM_REG_SP, UC_ARM_REG_LR, UC_ARM_REG_PC
        self._c = dict(UC_HOOK_CODE=UC_HOOK_CODE, SP=UC_ARM_REG_SP,
                       LR=UC_ARM_REG_LR, PC=UC_ARM_REG_PC)
        self.uc = Uc(UC_ARCH_ARM, UC_MODE_THUMB | UC_MODE_BIG_ENDIAN)
        u = self.uc
        u.mem_map(CODE_BASE, CODE_SIZE)
        u.mem_map(SRAM_BASE, SRAM_SIZE)
        u.mem_map(STACK_BASE, STACK_SIZE)
        u.mem_map(PERIPH_BASE, PERIPH_SIZE)
        code = open(image_path, "rb").read()
        u.mem_write(KOD_LOAD, code)
        self.fault = None
        u.hook_add(UC_HOOK_MEM_UNMAPPED, self._on_unmapped)
        if trace:
            u.hook_add(UC_HOOK_CODE, self._trace)

    def _on_unmapped(self, u, access, address, size, value, _):
        pc = u.reg_read(self._c["PC"])
        self.fault = (address, pc)
        return False

    def _trace(self, u, address, size, _):
        from capstone import Cs, CS_ARCH_ARM, CS_MODE_THUMB, CS_MODE_BIG_ENDIAN
        if not hasattr(self, "_md"):
            self._md = Cs(CS_ARCH_ARM, CS_MODE_THUMB | CS_MODE_BIG_ENDIAN)
        for ins in self._md.disasm(bytes(u.mem_read(address, size)), address):
            print(f"    {ins.address:08x}: {ins.mnemonic:8s} {ins.op_str}")

    # memory helpers
    def w8(self, a, v): self.uc.mem_write(a, bytes([v & 0xFF]))
    def wbytes(self, a, b): self.uc.mem_write(a, bytes(b))
    def w32be(self, a, v): self.uc.mem_write(a, struct.pack(">I", v & 0xFFFFFFFF))
    def rstr(self, a, maxn=64):
        b = bytes(self.uc.mem_read(a, maxn))
        z = b.find(b"\x00")
        return b[:z if z >= 0 else maxn].decode("latin-1", "replace")

    def call(self, addr, r0=0, count=200_000):
        from unicorn import UcError
        from unicorn.arm_const import (UC_ARM_REG_R0, UC_ARM_REG_SP, UC_ARM_REG_LR)
        u = self.uc
        u.reg_write(UC_ARM_REG_SP, STACK_BASE + STACK_SIZE - 0x100)
        u.reg_write(UC_ARM_REG_LR, SENTINEL | 1)
        u.reg_write(UC_ARM_REG_R0, r0)
        self.fault = None
        try:
            u.emu_start(addr | 1, SENTINEL, 1_000_000, count)
        except UcError as e:
            if self.fault:
                raise RuntimeError(f"unmapped memory @0x{self.fault[0]:08x} "
                                   f"from PC=0x{self.fault[1]:08x}") from e
            raise
        return u.reg_read(UC_ARM_REG_R0)

    def run_to(self, start, stop, count=500_000, **regs):
        from unicorn.arm_const import (UC_ARM_REG_SP, UC_ARM_REG_LR, UC_ARM_REG_R0,
                                       UC_ARM_REG_R1, UC_ARM_REG_R2, UC_ARM_REG_R3,
                                       UC_ARM_REG_R4, UC_ARM_REG_R5, UC_ARM_REG_R6, UC_ARM_REG_R7)
        u = self.uc
        u.reg_write(UC_ARM_REG_SP, STACK_BASE + STACK_SIZE - 0x100)
        u.reg_write(UC_ARM_REG_LR, SENTINEL | 1)
        rmap = [UC_ARM_REG_R0, UC_ARM_REG_R1, UC_ARM_REG_R2, UC_ARM_REG_R3,
                UC_ARM_REG_R4, UC_ARM_REG_R5, UC_ARM_REG_R6, UC_ARM_REG_R7]
        for i in range(8):
            u.reg_write(rmap[i], regs.get(f"r{i}", 0))
        stopset = {stop & ~1, SENTINEL}
        def _stop(uu, address, size, _):
            if (address & ~1) in stopset:
                uu.emu_stop()
        h = u.hook_add(self._c["UC_HOOK_CODE"], _stop)
        self.fault = None
        try:
            u.emu_start(start | 1, SENTINEL, 1_000_000, count)
        finally:
            u.hook_del(h)
        return u.reg_read(UC_ARM_REG_SP)


# ---------------------------------------------------------------------------
# injection: run the REAL hook site with a stand-in FlexCAN mailbox
# ---------------------------------------------------------------------------
# We execute the actual instruction at 0x236f4 ('adds r0,r4,#0' then 'bl' at 0x236f6) and stop
# right after it (0x236fa). On a patched image the bl enters the cave -> the metadata reaches the
# store; on a stock image the same bl calls the framework (stubbed to just return the mailbox
# index) and nothing reaches the store. Same instruction, same mailbox, both images: the only
# variable is the 4 hook bytes + the cave. That is the whole proof the patch is what makes the
# difference - and it exercises the hook AS INSTALLED, not just the cave in isolation.
def drive_hook_frame(e, canid, data8):
    mbx = FAKE_MB_BASE + INJECT_MB * 16 + 0x80
    e.w32be(PERBUS + INJECT_BUS * 4, FAKE_MB_BASE)      # table[bus] -> our module base
    e.w32be(mbx + 4, (canid & 0x7FF) << 18)             # ID register: std ID in bits 28:18
    e.wbytes(mbx + 8, bytes(data8)[:8].ljust(8, b"\x00"))
    # stub ORIG (0x230e4): movs r0,#mb ; bx lr  (Thumb, big-endian) - the framework mailbox fetch
    e.wbytes(ORIG & ~1, bytes([0x20, INJECT_MB & 0xFF, 0x47, 0x70]))
    e.run_to(HOOK_SITE, HOOK_RESUME, r4=INJECT_BUS)     # r4 = bus (the dispatcher's saved arg)


def inject_and_read(image_path, inject_canid, title, artist, run_renderer=False, trace=False):
    """Inject title/artist on `inject_canid` through the real hook, one frame at a time, and
    return what reached the media store (and, if asked, what the screen renderer pulled in)."""
    e = Emu(image_path, trace=trace)
    # clean slate
    e.w8(SLEN, 0); e.w8(SIDX, 0)
    for a in STORE.values():
        e.wbytes(a, b"\x00" * 40)

    for name, text in (("title", title), ("artist", artist)):
        if not text:
            continue
        for fr in field_frames(FIELD[name], SRC_BT, text):
            drive_hook_frame(e, inject_canid, fr)

    store = {k: e.rstr(a) for k, a in STORE.items()}

    renderer = None
    if run_renderer:
        # best-effort proof the screen renderer pulls the same store into its draw buffers
        try:
            e.w32be(STATUS_SONGNO, 1); e.w8(STATUS_FLAGS, 0x00)
            sp = e.run_to(RENDERER, RENDER_AFTER_GETTERS)
            renderer = {k: e.rstr(sp + off) for k, off in RBUF.items()}
        except Exception as ex:
            renderer = {"_error": str(ex)}
    return store, renderer


def control_canid(canid):
    """A different, deliberately non-matching CAN ID for the negative control. 0x4B0 carries
    the same Bluetooth text as 0x4B1 but the cave filters it out on purpose - perfect to prove
    the display only happens for the ID the patch targets."""
    return 0x4B0 if canid != 0x4B0 else 0x4C3


# ---------------------------------------------------------------------------
# patch-state detection (read-only, on the raw bytes)
# ---------------------------------------------------------------------------
def detect_state(data):
    ho = HOOK - BASE
    hook_bytes = bytes(data[ho:ho + 4])
    stock_hook = ebl(HOOK, ORIG)
    patched_hook = ebl(HOOK, CAVE)
    cave_present = any(data[CAVE - BASE:CAVE - BASE + 64])
    if hook_bytes == patched_hook and cave_present:
        return "patched", hook_bytes
    if hook_bytes == stock_hook and not cave_present:
        return "stock", hook_bytes
    return "unknown", hook_bytes


def renderer_is_patched(data):
    for addr in RENDER_SITES:
        if struct.unpack(">H", data[addr - BASE:addr - BASE + 2])[0] != RENDER_NEW:
            return False
    return True


# ---------------------------------------------------------------------------
# patch a throw-away copy via the SHIPPED scripts (so we test what you'd flash)
# ---------------------------------------------------------------------------
def build_patched(dump_path, canid, workdir):
    here = os.path.dirname(os.path.abspath(__file__))
    apply_patch = os.path.join(here, "apply_patch_v3.py")
    apply_render = os.path.join(here, "apply_render.py")
    out = os.path.join(workdir, "patched.bin")
    logs = []
    cmd = [sys.executable, apply_patch, dump_path, out]
    if canid != 0x4B1:
        cmd += ["--canid", hex(canid)]
    r = subprocess.run(cmd, capture_output=True, text=True)
    logs.append(("apply_patch_v3.py", r))
    if r.returncode != 0:
        return None, logs
    r2 = subprocess.run([sys.executable, apply_render, out, out], capture_output=True, text=True)
    logs.append(("apply_render.py", r2))
    if r2.returncode != 0:
        return None, logs
    return out, logs


def revert_to_stock(data):
    """Reconstruct the original stock bytes from a patched image, in memory: restore the hook to
    'bl 0x230e4', blank the cave, and undo the renderer buffer moves. The cave region is all
    zeros on genuine stock firmware, so this reproduces the real stock behaviour for the A/B."""
    d = bytearray(data)
    d[HOOK - BASE:HOOK - BASE + 4] = ebl(HOOK, ORIG)
    d[CAVE - BASE:CAVE - BASE + CAVE_MAX] = b"\x00" * CAVE_MAX
    for addr in RENDER_SITES:
        if struct.unpack(">H", d[addr - BASE:addr - BASE + 2])[0] == RENDER_NEW:
            d[addr - BASE:addr - BASE + 2] = struct.pack(">H", RENDER_OLD)
    return bytes(d)


# ---------------------------------------------------------------------------
# reporting
# ---------------------------------------------------------------------------
def rule(ch="-"): print(ch * 72)


def verdict(stock_store, patched_store, renderer, ctrl_store, ctrl_id, title, artist, canid):
    want = {k: v for k, v in (("title", title), ("artist", artist)) if v}
    print()
    print(f"  Same 0x{canid:03X} frame, same method, driven through the real hook @0x{HOOK_SITE:x}:")
    print()

    # BEFORE - the stock image must DROP the frame (nothing reaches the store).
    stock_empty = all(stock_store.get(k, "") == "" for k in want)
    print(f"  BEFORE  stock    title={stock_store['title']!r}  artist={stock_store['artist']!r}"
          f"   -> {'PASS (dropped, as it should be)' if stock_empty else 'FAIL (stock already shows it?!)'}")

    # AFTER - the patched image must carry it to the store AND the screen renderer.
    filled = all(patched_store.get(k, "") == v for k, v in want.items())
    print(f"  AFTER   patched  title={patched_store['title']!r}  artist={patched_store['artist']!r}"
          f"   -> {'PASS (reaches the store)' if filled else 'FAIL'}")
    if renderer and "_error" not in renderer:
        render_ok = all(renderer.get(k, "") == v for k, v in want.items())
        print(f"          renderer title={renderer['title']!r}  artist={renderer['artist']!r}"
              f"   -> {'PASS (drawn on screen)' if render_ok else 'FAIL'}")
    else:
        render_ok = None
        why = renderer.get("_error", "unknown") if renderer else "not run"
        print(f"          renderer (skipped - {why})")

    # CONTROL - the same patched image must IGNORE a non-matching ID.
    ctrl_empty = all(ctrl_store.get(k, "") == "" for k in want)
    print(f"  CONTROL wrong 0x{ctrl_id:03X}  title={ctrl_store['title']!r}  artist={ctrl_store['artist']!r}"
          f"   -> {'PASS (correctly ignored)' if ctrl_empty else 'FAIL (leaked!)'}")

    print()
    ok = stock_empty and filled and ctrl_empty and render_ok is not False
    if ok:
        print("  RESULT: PASS - the patch is what makes the difference: the identical Bluetooth")
        print("          frame is DROPPED before patching and REACHES THE SCREEN after, and only")
        print("          for the CAN ID the patch targets.")
        if canid != 0x4B1:
            print(f"          Built and tested for the non-default CAN ID 0x{canid:03X}.")
        print("          If your phone still shows nothing on the car, the cluster side is fine -")
        print("          the problem is the source: your head unit broadcasts on a different CAN")
        print("          ID. Capture the bus, find the ID, and re-run with --canid 0xNNN.")
        return True
    if not stock_empty:
        print("  RESULT: FAIL - the stock image already displayed the frame; the A/B is invalid.")
    elif not filled:
        print("  RESULT: FAIL - the patched image did NOT carry the metadata to the store.")
    elif not ctrl_empty:
        print("  RESULT: FAIL - a non-matching CAN ID also filled the store (the filter leaked).")
    else:
        print("  RESULT: FAIL - the renderer did not pick up the injected text.")
    print("          Please open an issue with the output above.")
    return False


def main():
    ap = argparse.ArgumentParser(
        description="Emulate the Bluetooth-metadata patch on your own firmware dump - no car needed.")
    ap.add_argument("--dump", required=True, help="your cluster firmware dump (main.bin, code partition).")
    ap.add_argument("--title", default="PATCH TEST", help="title text to inject (default 'PATCH TEST').")
    ap.add_argument("--artist", default="CANBUS", help="artist text to inject (default 'CANBUS').")
    ap.add_argument("--canid", default="0x4B1",
                    help="CAN ID to build/inject on (default 0x4B1; e.g. 0x4C6 for some head units).")
    ap.add_argument("--trace", action="store_true", help="disassemble every executed instruction (needs capstone).")
    a = ap.parse_args()

    canid = int(a.canid, 0)
    if not (0 <= canid <= 0x7FF):
        print("CAN ID out of 11-bit range."); sys.exit(2)

    try:
        import unicorn  # noqa: F401
    except ImportError:
        print("This tool needs the Unicorn CPU emulator. Install it with:\n")
        print("    pip install unicorn\n")
        print("(and 'pip install capstone' too if you want --trace). Nothing else is required.")
        sys.exit(2)

    if not os.path.isfile(a.dump):
        print(f"Dump not found: {a.dump}"); sys.exit(2)
    data = bytearray(open(a.dump, "rb").read())

    rule("=")
    print(" Convers+ Bluetooth patch - emulator self-test (no car, no CAN adapter)")
    rule("=")
    print(f"  Dump: {a.dump}  ({len(data)} bytes)")

    need = CAVE - BASE + 320   # must at least reach past the cave region
    if len(data) < need:
        rule()
        print(f"  This file is only {len(data)} bytes - too small to be the cluster code")
        print(f"  partition (that is ~{CODE_SIZE - KOD_LOAD} bytes, base 0x5000). Make sure you")
        print("  pass the code/main partition of your dump, not the EEPROM or a VBF header.")
        sys.exit(2)

    state, hook_bytes = detect_state(data)
    print(f"  Patch state: {state.upper()}   (hook @0x{HOOK:x} = {hook_bytes.hex(' ')})")

    if state == "unknown":
        rule()
        print("  This dump does not match the firmware version the patch targets.")
        print(f"    hook @0x{HOOK:x} is {hook_bytes.hex(' ')}")
        print(f"    expected (stock)   bl 0x230e4 = {ebl(HOOK, ORIG).hex(' ')}")
        print(f"    expected (patched) bl ->cave  = {ebl(HOOK, CAVE).hex(' ')}")
        print("  Either it is a different cluster variant, or the file isn't the code")
        print("  partition (base 0x5000). The patch cannot be validated on this image.")
        print("  Please open an issue with your car model, cluster part number and this output.")
        sys.exit(1)

    if canid not in ACCEPTED_IDS:
        rule()
        print(f"  NOTE: 0x{canid:03X} is NOT in the cluster's CAN acceptance table")
        print(f"        ({', '.join('0x%03X' % i for i in sorted(ACCEPTED_IDS))}).")
        print("        The emulator can still show the patch handling it, but on the real car the")
        print("        hardware drops this ID before the firmware sees it - it would need a mailbox")
        print("        reconfiguration this patch does not do. For a real fix, pick an ID the")
        print("        cluster already accepts (0x4C6 is the usual Bluetooth one for an MCA).")

    workdir = tempfile.mkdtemp(prefix="convers_emu_")
    try:
        # We always end up with two images to A/B: an unpatched (stock) one and a patched one.
        if state == "stock":
            rule()
            print("  This is a STOCK (unpatched) dump. Building a patched copy the same way you")
            print("  would flash it, then comparing the two - identical firmware, only the patch")
            print("  differs:")
            stock_img = a.dump
            patched_img, logs = build_patched(a.dump, canid, workdir)
            for name, r in logs:
                for line in (r.stdout or "").splitlines():
                    print(f"    [{name}] {line}")
                if r.returncode != 0:
                    print(f"    [{name}] FAILED (exit {r.returncode})")
                    for line in (r.stderr or "").splitlines():
                        print(f"    [{name}] {line}")
            if patched_img is None:
                print("\n  Patching failed - see the messages above. The dump was not modified.")
                sys.exit(1)
        else:  # patched dump (e.g. read back from a flashed cluster)
            rule()
            print("  This dump already contains the patch (e.g. dumped from a flashed cluster).")
            print(f"    renderer (19-char) patch: {'present' if renderer_is_patched(data) else 'NOT FOUND'}")
            print("  Testing exactly what is flashed, and reconstructing the stock bytes in memory")
            print("  for the before/after comparison:")
            patched_img = a.dump
            stock_img = os.path.join(workdir, "reverted_stock.bin")
            open(stock_img, "wb").write(revert_to_stock(data))

        # BEFORE: same frame through the real hook on the stock image -> must be dropped.
        stock_store, _ = inject_and_read(stock_img, canid, a.title, a.artist, run_renderer=False)
        # AFTER: same frame on the patched image -> must reach store + renderer.
        patched_store, renderer = inject_and_read(patched_img, canid, a.title, a.artist,
                                                  run_renderer=True, trace=a.trace)
        # CONTROL: a non-matching ID on the patched image -> must be ignored.
        ctrl_id = control_canid(canid)
        ctrl_store, _ = inject_and_read(patched_img, ctrl_id, a.title, a.artist, run_renderer=False)

        ok = verdict(stock_store, patched_store, renderer, ctrl_store, ctrl_id,
                     a.title, a.artist, canid)
        rule("=")
        sys.exit(0 if ok else 1)
    finally:
        shutil.rmtree(workdir, ignore_errors=True)


if __name__ == "__main__":
    main()
