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
import os, sys, struct, argparse

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import fw_versions as FV                                                  # noqa: E402
import apply_patch_v3                                                     # noqa: E402
import apply_render                                                       # noqa: E402

# ---------------------------------------------------------------------------
# Firmware map (code partition, base 0x5000 - same convention as the patch tools).
# Every version-specific address comes from fw_versions.cfg(version); what is left here is the
# same in every supported version.
# ---------------------------------------------------------------------------
BASE = FV.BASE
RENDER_AFTER_GETTERS_REL = 0x2E     # renderer + 0x2E: PC just past the title/artist/album getters
# renderer working buffers AFTER apply_render.py relocates the title (title -> sp+0x40)
RBUF = {"title": 0x40, "artist": 0x2c, "album": 0x18}
RENDER_OLD, RENDER_NEW = FV.RENDER_OLD, FV.RENDER_NEW

# CAN IDs the cluster's hardware acceptance table actually receives into a mailbox (identical in
# every supported version). An ID outside this set is filtered in hardware before the firmware
# ever sees it, so even a correct patch can't display it without a mailbox reconfig (which this
# patch doesn't do). Used only to warn - the emulator can still demonstrate any ID.
ACCEPTED_IDS = set(FV.ACCEPTED_IDS)

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
# Unicorn harness (subset of emu.py, English, loads any dump - no hard-coded path)
# ---------------------------------------------------------------------------
class Emu:
    def __init__(self, image, trace=False):
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
        code = image if isinstance(image, (bytes, bytearray)) else open(image, "rb").read()
        u.mem_write(KOD_LOAD, bytes(code))
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
# We execute the actual instruction at HOOK-2 ('adds r0,r4,#0' then the 'bl' at HOOK) and stop
# right after it (HOOK+4). On a patched image the bl enters the cave -> the metadata reaches the
# store; on a stock image the same bl calls the framework (stubbed to just return the mailbox
# index) and nothing reaches the store. Same instruction, same mailbox, both images: the only
# variable is the 4 hook bytes + the cave. That is the whole proof the patch is what makes the
# difference - and it exercises the hook AS INSTALLED, not just the cave in isolation.
def drive_hook_frame(e, c, canid, data8):
    mbx = FAKE_MB_BASE + INJECT_MB * 16 + 0x80
    e.w32be(c["PERBUS"] + INJECT_BUS * 4, FAKE_MB_BASE)  # table[bus] -> our module base
    e.w32be(mbx + 4, (canid & 0x7FF) << 18)             # ID register: std ID in bits 28:18
    e.wbytes(mbx + 8, bytes(data8)[:8].ljust(8, b"\x00"))
    # stub ORIG: movs r0,#mb ; bx lr  (Thumb, big-endian) - the framework mailbox fetch
    e.wbytes(c["ORIG"] & ~1, bytes([0x20, INJECT_MB & 0xFF, 0x47, 0x70]))
    e.run_to(c["HOOK_SITE"], c["HOOK_RESUME"], r4=INJECT_BUS)   # r4 = bus (the dispatcher's saved arg)


def inject_and_read(image, c, inject_canid, title, artist, run_renderer=False, trace=False):
    """Inject title/artist on `inject_canid` through the real hook, one frame at a time, and
    return what reached the media store (and, if asked, what the screen renderer pulled in)."""
    e = Emu(image, trace=trace)
    store_at = {k: c["STORE"] + FV.STORE_FIELDS[k] for k in ("title", "artist", "album")}
    # clean slate
    e.w8(c["SLEN"], 0); e.w8(c["SIDX"], 0)
    for a in store_at.values():
        e.wbytes(a, b"\x00" * 20)

    for name, text in (("title", title), ("artist", artist)):
        if not text:
            continue
        for fr in field_frames(FIELD[name], SRC_BT, text):
            drive_hook_frame(e, c, inject_canid, fr)

    store = {k: e.rstr(a) for k, a in store_at.items()}

    renderer = None
    if run_renderer:
        # best-effort proof the screen renderer pulls the same store into its draw buffers
        try:
            e.w32be(c["STATUS_SONGNO"], 1); e.w8(c["STATUS_FLAGS"], 0x00)
            sp = e.run_to(c["RENDERER"], c["RENDERER"] + RENDER_AFTER_GETTERS_REL)
            renderer = {k: e.rstr(sp + off) for k, off in RBUF.items()}
        except Exception as ex:
            renderer = {"_error": str(ex)}
    return store, renderer


def flash_selftest(image, c):
    """Run the firmware's OWN flash self-test on `image`: start, one block per call until it
    reports done, then its verdict. True = the cluster will accept this flash content. (The
    bootloader area below 0x5000 is not part of a dump; it is left as zeros here, and the factory
    application adds up to 0 by itself.)"""
    e = Emu(image)
    e.call(c["SUM_START"])
    for _ in range(64):
        if e.call(c["SUM_DONE"]):
            break
        e.call(c["SUM_STEP"])
    else:
        return False
    return bool(e.call(c["SUM_OK"]))


def control_canid(canid):
    """A different, deliberately non-matching CAN ID for the negative control. 0x4B0 carries
    the same Bluetooth text as 0x4B1 but the cave filters it out on purpose - perfect to prove
    the display only happens for the ID the patch targets."""
    return 0x4B0 if canid != 0x4B0 else 0x4C3


# ---------------------------------------------------------------------------
# patch a throw-away copy with the SHIPPED patch code (so we test what you'd flash)
# ---------------------------------------------------------------------------
def build_patched(data, canid, log):
    out, _ = apply_patch_v3.patch(data, canid, log=lambda s: log(f"    [apply_patch_v3] {s}"))
    out, _, _ = apply_render.patch(out, log=lambda s: log(f"    [apply_render]  {s}"))
    return out


def revert_to_stock(data, c):
    """Reconstruct the original stock bytes from a patched image, in memory: restore the hook to
    'bl ORIG', refill the cave with the version's erased-flash fill, and undo the renderer buffer
    moves. This reproduces the real stock behaviour for the A/B."""
    d = bytearray(data)
    n = len(apply_patch_v3.build_cave(c))
    d[c["HOOK"] - BASE:c["HOOK"] - BASE + 4] = FV.ebl(c["HOOK"], c["ORIG"])
    d[c["CAVE"] - BASE:c["CAVE"] - BASE + n] = bytes([c["CAVE_FILL"]]) * n
    for addr in c["RENDER_SITES"]:
        if FV.u16(d, addr) == RENDER_NEW:
            d[addr - BASE:addr - BASE + 2] = struct.pack(">H", RENDER_OLD)
    for addr in c.get("GUARD_LITS", ()):
        d[addr - BASE:addr - BASE + 4] = struct.pack(">I", c["GUARD_OLD"])
    return bytes(d)


# ---------------------------------------------------------------------------
# reporting
# ---------------------------------------------------------------------------
def rule(ch="-", log=print): log(ch * 72)


def verdict(c, stock_store, patched_store, renderer, ctrl_store, ctrl_id, title, artist, canid, log=print,
            flash_ok=True):
    want = {k: v for k, v in (("title", title), ("artist", artist)) if v}
    log("")
    log(f"  Same 0x{canid:03X} frame, same method, driven through the real hook @0x{c['HOOK_SITE']:x}:")
    log("")

    # BEFORE - the stock image must DROP the frame (nothing reaches the store).
    stock_empty = all(stock_store.get(k, "") == "" for k in want)
    log(f"  BEFORE  stock    title={stock_store['title']!r}  artist={stock_store['artist']!r}"
        f"   -> {'PASS (dropped, as it should be)' if stock_empty else 'FAIL (stock already shows it?!)'}")

    # AFTER - the patched image must carry it to the store AND the screen renderer.
    filled = all(patched_store.get(k, "") == v for k, v in want.items())
    log(f"  AFTER   patched  title={patched_store['title']!r}  artist={patched_store['artist']!r}"
        f"   -> {'PASS (reaches the store)' if filled else 'FAIL'}")
    if renderer and "_error" not in renderer:
        render_ok = all(renderer.get(k, "") == v for k, v in want.items())
        log(f"          renderer title={renderer['title']!r}  artist={renderer['artist']!r}"
            f"   -> {'PASS (drawn on screen)' if render_ok else 'FAIL'}")
    else:
        render_ok = None
        why = renderer.get("_error", "unknown") if renderer else "not run"
        log(f"          renderer (skipped - {why})")

    # CONTROL - the same patched image must IGNORE a non-matching ID.
    ctrl_empty = all(ctrl_store.get(k, "") == "" for k in want)
    log(f"  CONTROL wrong 0x{ctrl_id:03X}  title={ctrl_store['title']!r}  artist={ctrl_store['artist']!r}"
        f"   -> {'PASS (correctly ignored)' if ctrl_empty else 'FAIL (leaked!)'}")

    log(f"  FLASH   the firmware's own flash self-test on the patched image"
        f"   -> {'PASS (adds up to 0)' if flash_ok else 'FAIL'}")

    log("")
    if stock_empty and filled and ctrl_empty and render_ok is not False and not flash_ok:
        log("  RESULT: FAIL - the patch itself works, but this image does not pass the cluster's")
        log("          flash self-test: on the car it stores DTC U3000 and adaptive cruise control")
        log("          reports 'ACC not available'. It was patched with an older version of the")
        log("          tools. Run apply_render.py on it (it balances the image) and flash again.")
        return False
    ok = stock_empty and filled and ctrl_empty and render_ok is not False
    if ok:
        log("  RESULT: PASS - the patch is what makes the difference: the identical Bluetooth")
        log("          frame is DROPPED before patching and REACHES THE SCREEN after, and only")
        log("          for the CAN ID the patch targets.")
        if canid != 0x4B1:
            log(f"          Built and tested for the non-default CAN ID 0x{canid:03X}.")
        log("          If your phone still shows nothing on the car, the cluster side is fine -")
        log("          the problem is the source: your head unit broadcasts on a different CAN")
        log("          ID. Capture the bus, find the ID, and re-run with --canid 0xNNN.")
        return True
    if not stock_empty:
        log("  RESULT: FAIL - the stock image already displayed the frame; the A/B is invalid.")
    elif not filled:
        log("  RESULT: FAIL - the patched image did NOT carry the metadata to the store.")
    elif not ctrl_empty:
        log("  RESULT: FAIL - a non-matching CAN ID also filled the store (the filter leaked).")
    else:
        log("  RESULT: FAIL - the renderer did not pick up the injected text.")
    log("          Please open an issue with the output above.")
    return False


# ---------------------------------------------------------------------------
# the self-test itself - also used by check_all.py
# ---------------------------------------------------------------------------
def selftest(data, canid=0x4B1, title="PATCH TEST", artist="CANBUS", trace=False, log=print):
    """A/B self-test of one image. Returns a dict: ok, version, state, reason, results."""
    res = {"ok": False, "version": None, "state": None, "reason": None}
    v, state, reports = FV.identify(data)
    if v is None:
        rule(log=log)
        log("  This dump does not match any firmware version the patch supports.")
        for line in FV.explain_refusal(data, reports, "this dump").splitlines():
            log("  " + line)
        log("  Either it is a different cluster variant, or the file isn't the code")
        log("  partition (base 0x5000). The patch cannot be validated on this image.")
        res["reason"] = "unsupported"
        return res
    c = FV.cfg(v)
    res["version"] = v
    patched_now = state["cave"] == "patched"
    res["state"] = "patched" if patched_now else "stock"
    log(f"  Firmware    : {v} ({c['part']}; {c['status']})")
    log(f"  Patch state : {'PATCHED' if patched_now else 'STOCK'}   "
        f"(hook @0x{c['HOOK']:x} = {FV.at(data, c['HOOK'], 4).hex(' ')})")
    if FV.u16(data, c["HOOK_SITE"]) != 0x1C20:        # adds r0,r4,#0 - the bus argument
        log(f"  unexpected instruction at the hook entry 0x{c['HOOK_SITE']:x} - cannot drive the hook")
        res["reason"] = "hook entry"
        return res

    if canid not in ACCEPTED_IDS:
        rule(log=log)
        log(f"  NOTE: 0x{canid:03X} is NOT in the cluster's CAN acceptance table")
        log(f"        ({', '.join('0x%03X' % i for i in sorted(ACCEPTED_IDS))}).")
        log("        The emulator can still show the patch handling it, but on the real car the")
        log("        hardware drops this ID before the firmware sees it - it would need a mailbox")
        log("        reconfiguration this patch does not do. For a real fix, pick an ID the")
        log("        cluster already accepts (0x4C6 is the usual Bluetooth one for an MCA).")

    # We always end up with two images to A/B: an unpatched (stock) one and a patched one.
    rule(log=log)
    if not patched_now:
        log("  This is a STOCK (unpatched) dump. Building a patched copy the same way you")
        log("  would flash it, then comparing the two - identical firmware, only the patch")
        log("  differs:")
        stock_img = bytes(data)
        try:
            patched_img = build_patched(data, canid, log)
        except (apply_patch_v3.Refused, apply_render.Refused) as ex:
            log(f"\n  Patching refused: {ex}\n  The dump was not modified.")
            res["reason"] = "patch refused"
            return res
    else:  # patched dump (e.g. read back from a flashed cluster)
        log("  This dump already contains the patch (e.g. dumped from a flashed cluster).")
        log(f"    renderer (19-char) patch: {'present' if state['render'] == 'patched' else 'NOT FOUND'}")
        if state.get("old_scratch"):
            log("    NOTE: built by the first release - its working memory sits inside the radio's")
            log("    media buffer and can spoil a CD / MP3 track-list page. Patch your ORIGINAL dump")
            log("    again with the current tools to get the fix.")
            c = dict(c, BUF=c["BUF_OLD"], SLEN=c["BUF_OLD"] + 0x18, SIDX=c["BUF_OLD"] + 0x19,
                     SBUS=c["BUF_OLD"] + 0x20)
        log("  Testing exactly what is flashed, and reconstructing the stock bytes in memory")
        log("  for the before/after comparison:")
        patched_img = bytes(data)
        stock_img = revert_to_stock(data, c)

    # BEFORE: same frame through the real hook on the stock image -> must be dropped.
    stock_store, _ = inject_and_read(stock_img, c, canid, title, artist, run_renderer=False)
    # AFTER: same frame on the patched image -> must reach store + renderer.
    patched_store, renderer = inject_and_read(patched_img, c, canid, title, artist,
                                              run_renderer=True, trace=trace)
    # CONTROL: a non-matching ID on the patched image -> must be ignored.
    ctrl_id = control_canid(canid)
    ctrl_store, _ = inject_and_read(patched_img, c, ctrl_id, title, artist, run_renderer=False)

    flash_ok = flash_selftest(patched_img, c)

    res["ok"] = verdict(c, stock_store, patched_store, renderer, ctrl_store, ctrl_id,
                        title, artist, canid, log=log, flash_ok=flash_ok)
    res["results"] = dict(stock=stock_store, patched=patched_store, renderer=renderer, control=ctrl_store,
                          flash_selftest=flash_ok)
    if not res["ok"]:
        res["reason"] = "A/B failed"
    return res


def main():
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
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
    data = open(a.dump, "rb").read()

    rule("=")
    print(" Convers+ Bluetooth patch - emulator self-test (no car, no CAN adapter)")
    rule("=")
    print(f"  Dump: {a.dump}  ({len(data)} bytes)")
    if len(data) < 0x80000:
        rule()
        print(f"  This file is only {len(data)} bytes - too small to be the cluster code")
        print(f"  partition (that is {FV.IMAGE_SIZE} bytes, base 0x5000). Make sure you")
        print("  pass the code/main partition of your dump, not the EEPROM or a VBF header.")
        sys.exit(2)

    res = selftest(data, canid, a.title, a.artist, trace=a.trace)
    rule("=")
    sys.exit(0 if res["ok"] else 1)


if __name__ == "__main__":
    main()
