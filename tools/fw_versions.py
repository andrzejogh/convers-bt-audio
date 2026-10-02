#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
fw_versions.py - the ONE table of cluster firmware versions this patch supports, and the
fingerprint that decides which one (if any) an image is.

Every tool (apply_patch_v3.py, apply_render.py, emulate.py, check_all.py) imports this module,
so an address exists in exactly one place.

Rules this table follows
------------------------
* Every address was MEASURED on that version's image. Deltas between versions are not constant
  (reader +0xBC, PERBUS +0x65C, DLIST +0x6A8, renderer +0x92, media store +0x24, ...), so a new
  version is never derived by arithmetic - it gets its own entry, or it is refused.
* An image is accepted only if it matches EVERY anchor of EXACTLY ONE entry. Code signatures alone
  are not enough: 14.11 (CS7T-14C026-CC) has the same reader, dispatcher and renderer bytes as
  14.12, but its data tables live elsewhere - patching it with the 14.12 table would be silently
  wrong. The data anchors (PERBUS, DLIST, SRAM literals) are what refuses it.
* The SRAM scratch is not a free-memory guess. There is no unused run in .bss, and "no write seen
  in an emulator run" is not proof (firmware state machines write some structures only on certain
  screens). The scratch sits at the same place in every version: +0x22 into the stock 500-byte
  media-message buffer (the buffer the stock ISO-TP media path fills before calling the media
  dispatcher). That is the placement proven on the car with 14.12. It is SHARED memory, not free
  memory: a stock media message arriving in the middle of a 0x4B1 reassembly can spoil our partial
  title (the next 0x4B1 update repairs it), and in the emulator the cave's SBUS (written on every
  received CAN frame, at buffer +0x42/+0x43) also overwrites bytes 66-67 of a stock 0x4C1 message
  longer than 67 bytes while it is being received (e.g. a CD track list page). That second effect
  has not been observed on a car; it is kept here so nobody mistakes this placement for "free".
    14.12: buffer struct 0x400092D2, text at +0xC = 0x400092DE  ->  BUF 0x40009300
    15.07: buffer struct 0x400092F2, text at +0xC = 0x400092FE  ->  BUF 0x40009320
  (The earlier 15.07 port by @wojtkowiak used 0x40009880, chosen from the SRAM writes measured in
  the emulator. We derive the scratch differently - by the media-buffer offset proven on the car
  with 14.12 - so the two ports differ only in this address.)

Adding a version: measure every field below on the new image, add an entry, then run
check_all.py over your firmware folder - it must report the new image as supported and pass the
emulator A/B, and every other image must keep its previous verdict.
"""
import re
import struct

BASE = 0x5000             # the application partition is mapped at 0x5000 (mem = file offset + BASE)
IMAGE_SIZE = 0xFB000      # 1 028 096 B - the application block of a Convers+ FL VBF

# ---------------------------------------------------------------------------------------------
# Code signatures - byte-identical in every supported version (the relative BL inside the
# dispatcher prologue has the same displacement in both). Checked AT the table address.
# ---------------------------------------------------------------------------------------------
READER_SIG = bytes.fromhex(        # 80 B at ORIG: the mailbox reader the hook calls
    "b470040ad502200fbc704770004248915a841c2030ff060016004b8f26015a9de0091c334083400b"
    "d00206000e00e7eb30ff060016004285ddf348885a8030ff060016004b865a9ae0091c334083400b")
DISPATCH_PRE = bytes.fromhex(      # 42 B right before HOOK: the RX dispatcher prologue
    "b5701c04491b00a0580d1c20f7fffcad6b284a1e00e132721889684922010392400843901c01d01a1c20")
DISPATCH_POST = bytes.fromhex(     # 16 B right after HOOK: the dispatcher splits by mailbox type
    "4a21016118895c092901d00d2902d006")
RENDER_PROLOG = bytes.fromhex(     # 20 B at RENDERER: push / sub sp,#0x74 / clear 3 buffers
    "b5f0b09d25002600ab18711eab10751eab00731e")
PERBUS_PAT = bytes.fromhex("fc094000fc0a00000202020202020200")   # FlexCAN A/B bases + flags
ACCEPT_TABLE = bytes.fromhex(      # CAN acceptance table: the IDs the cluster takes into a mailbox
    "04b1ffff04b3ffffffffffffffffffffffffffffffffffffffffffff0c0004c004c1ffffffff0c05"
    "ffff04c604c7ffffffffffffffffffffffffffffffff04d0ffff04d2ffff04d404d5")
ACCEPTED_IDS = (0x4B1, 0x4B3, 0x4C0, 0x4C1, 0x4C6, 0x4C7, 0x4D0, 0x4D2, 0x4D4, 0x4D5)

# Renderer: the 5 `add r0,sp,#0x64` title references, relative to RENDERER
RENDER_SITES_REL = (0x48, 0x6A, 0x86, 0x9E, 0xB6)
RENDER_OLD, RENDER_NEW = 0xA819, 0xA810      # add r0,sp,#0x64  ->  add r0,sp,#0x40
RENDER_FRAME_REL = (0x02, 0xB09D)            # sub sp,#0x74 (must stay untouched)
RENDER_EPI_REL = (-0x08, 0xB01D)             # add sp,#0x74 - epilogue SHARED with the previous function

# BT metadata store offsets (identical in every version - read by the renderer getters)
STORE_FIELDS = {"title": 0xA9, "artist": 0xBD, "album": 0xE5}

VERSIONS = {
    "14.12": dict(
        part="CS7T-14C026-CD",
        HOOK=0x236F6,          # `bl ORIG` inside the RX dispatcher (dispatcher + 0x2A)
        ORIG=0x230E4,          # mailbox reader
        CAVE=0x83240, CAVE_FILL=0x00,          # 713 B zero run (car-proven)
        PERBUS=0x7A0D4,        # per-bus FlexCAN base table
        DLIST=0x8EFF0,         # BT media field-handler list (8-byte entries)
        SABC=0x40009ABC,       # "current handler entry" the stock media dispatcher sets
        STORE=0x4000A6B4,      # BT metadata store
        BUF=0x40009300,        # cave scratch (see the module docstring)
        RENDERER=0x1CE94,      # BT Audio screen renderer
        STATUS=0x40007414,     # BT status block: +4 song number (u32), +9 flags (u8)
        ACCEPT=0x79446,        # CAN acceptance table
        DLIST_PAT=bytes.fromhex(
            "003f00000006c90d004200000006c9dd004300000006caab004400000006cb79"
            "004500000006cc57004600000006cd21003e00000006ce37003d00000006d099"
            "005100000006f8ed0000000000000000"),
        status="confirmed on cars (Mondeo MK4 FL, S-Max)",
    ),
    "15.07": dict(
        part="CS7T-14C026-CG",
        HOOK=0x237B2,
        ORIG=0x231A0,
        CAVE=0xDDE00, CAVE_FILL=0xEF,          # inside the 0xEF linker fill 0xD7D78..0xFFFF8
        PERBUS=0x7A730,
        DLIST=0x8F698,
        SABC=0x40009ADC,
        STORE=0x4000A6D8,
        BUF=0x40009320,        # = 14.12 placement, the media buffer moved by +0x20
        RENDERER=0x1CF26,
        STATUS=0x40007424,
        ACCEPT=0x79AA2,
        DLIST_PAT=bytes.fromhex(
            "003f00000006c795004200000006c865004300000006c933004400000006ca01"
            "004500000006cadf004600000006cba9003e00000006ccbf003d00000006cf21"
            "005100000006f7690000000000000000"),
        status="confirmed on a car (Mondeo MK4 FL)",
    ),
}

BANNER_RE = re.compile(rb"ROM Ver:\s*(\d\d\.\d\d)")
PART_RE = re.compile(rb"[A-Z0-9]{4}-14C026-[A-Z]{2}")


# ---------------------------------------------------------------------------------------------
def cfg(version):
    """Full configuration of a version, with the derived fields filled in."""
    c = dict(VERSIONS[version])
    c["version"] = version
    buf = c["BUF"]
    # scratch layout, identical to the original 14.12 tool: 24 B text, then state bytes
    c.update(SLEN=buf + 0x18, SIDX=buf + 0x19, SBUS=buf + 0x20, SCRATCH_SIZE=0x22)
    r = c["RENDERER"]
    c["RENDER_SITES"] = tuple(r + o for o in RENDER_SITES_REL)
    c["RENDER_FRAME"] = (r + RENDER_FRAME_REL[0], RENDER_FRAME_REL[1])
    c["RENDER_EPI"] = (r + RENDER_EPI_REL[0], RENDER_EPI_REL[1])
    c["HOOK_SITE"] = c["HOOK"] - 2          # `adds r0,r4,#0` (bus -> r0) right before the hook
    c["HOOK_RESUME"] = c["HOOK"] + 4
    c["STATUS_SONGNO"] = c["STATUS"] + 4
    c["STATUS_FLAGS"] = c["STATUS"] + 9
    c["STORE_TITLE"] = c["STORE"] + STORE_FIELDS["title"]
    return c


def ebl(src, dst):
    """Thumb BL from src to dst (big-endian halfwords), as used by every tool here."""
    o = (dst - (src + 4)) & 0x1FFFFFF
    S = (o >> 24) & 1; i1 = (o >> 23) & 1; i2 = (o >> 22) & 1
    im10 = (o >> 12) & 0x3FF; im11 = (o >> 1) & 0x7FF
    j1 = (~(i1 ^ S)) & 1; j2 = (~(i2 ^ S)) & 1
    return struct.pack('>HH', 0xF000 | (S << 10) | im10, 0xD000 | (j1 << 13) | (j2 << 11) | im11)


def at(data, addr, n):
    off = addr - BASE
    if off < 0 or off + n > len(data):
        return None
    return bytes(data[off:off + n])


def u16(data, addr):
    b = at(data, addr, 2)
    return None if b is None else struct.unpack('>H', b)[0]


def banner(data):
    m = BANNER_RE.search(bytes(data))
    return m.group(1).decode() if m else None


def part_numbers(data):
    return sorted({m.decode() for m in PART_RE.findall(bytes(data))})


# ---------------------------------------------------------------------------------------------
def probe(data, version):
    """Check every anchor of `version` against the image.

    Returns (checks, state):
      checks - list of (name, ok, detail)
      state  - {"cave": "stock"|"patched", "render": "stock"|"patched"}, or None when any
               anchor failed.
    Patch-dependent anchors (hook + cave, renderer sites) accept both the stock and the patched
    form, so the two apply scripts can run in any order and a patched dump is still recognised.
    A hook without its cave (or a cave without its hook) is a broken image and fails.
    """
    c = cfg(version)
    chk = []

    def add(name, ok, detail=""):
        chk.append((name, bool(ok), detail))

    add("size", len(data) >= c["CAVE"] - BASE + 0x200, f"{len(data)} B")
    add("reader", at(data, c["ORIG"], len(READER_SIG)) == READER_SIG, f"@{c['ORIG']:#x}")
    add("dispatcher", at(data, c["HOOK"] - len(DISPATCH_PRE), len(DISPATCH_PRE)) == DISPATCH_PRE
        and at(data, c["HOOK"] + 4, len(DISPATCH_POST)) == DISPATCH_POST, f"@{c['HOOK']:#x}")
    add("perbus", at(data, c["PERBUS"], len(PERBUS_PAT)) == PERBUS_PAT, f"@{c['PERBUS']:#x}")
    add("dlist", at(data, c["DLIST"], len(c["DLIST_PAT"])) == c["DLIST_PAT"], f"@{c['DLIST']:#x}")
    add("SABC literal", struct.pack(">I", c["SABC"]) in data, f"{c['SABC']:#x}")
    add("STORE literal", struct.pack(">I", c["STORE"]) in data, f"{c['STORE']:#x}")
    add("renderer", at(data, c["RENDERER"], len(RENDER_PROLOG)) == RENDER_PROLOG
        and u16(data, c["RENDER_EPI"][0]) == c["RENDER_EPI"][1], f"@{c['RENDERER']:#x}")

    # patch-dependent parts
    hook = at(data, c["HOOK"], 4)
    hook_state = ("stock" if hook == ebl(c["HOOK"], c["ORIG"])
                  else "patched" if hook == ebl(c["HOOK"], c["CAVE"]) else None)
    add("hook", hook_state, f"{hook.hex(' ') if hook else '-'} ({hook_state or 'unknown'})")
    sites = [u16(data, a) for a in c["RENDER_SITES"]]
    rend_state = ("stock" if all(s == RENDER_OLD for s in sites)
                  else "patched" if all(s == RENDER_NEW for s in sites) else None)
    add("render sites", rend_state, rend_state or "mixed/unknown")
    cave = at(data, c["CAVE"], 64)
    cave_state = None if cave is None else (
        "stock" if all(b == c["CAVE_FILL"] for b in cave) else "patched")
    add("cave", cave_state is not None and cave_state == (hook_state or cave_state),
        f"{cave_state} (hook {hook_state})")

    if not all(ok for _, ok, _ in chk):
        return chk, None
    return chk, {"cave": hook_state, "render": rend_state}


def identify(data):
    """-> (version or None, state or None, {version: checks}).

    A version is returned only when exactly one table entry matches all anchors."""
    reports = {}
    hits = []
    for v in VERSIONS:
        chk, state = probe(data, v)
        reports[v] = chk
        if state is not None:
            hits.append((v, state))
    if len(hits) == 1:
        return hits[0][0], hits[0][1], reports
    return None, None, reports


def explain_refusal(data, reports, label="image"):
    """Human-readable reason an image was not recognised (for the CLI tools)."""
    lines = [f"cannot recognise the firmware version of {label}."]
    b = banner(data)
    lines.append(f"  banner      : {'ROM Ver:' + b if b else 'no ROM Ver: banner'}"
                 + (f"   part numbers in image: {', '.join(part_numbers(data))}" if part_numbers(data) else ""))
    if len(data) != IMAGE_SIZE:
        lines.append(f"  size        : {len(data)} B (a Convers+ FL application partition is {IMAGE_SIZE} B)")
    for v, chk in reports.items():
        bad = [n for n, ok, _ in chk if not ok]
        lines.append(f"  vs {v:6}: " + ("all anchors match" if not bad else "mismatch: " + ", ".join(bad)))
    lines.append(f"  supported   : " + ", ".join(f"{v} ({VERSIONS[v]['part']})" for v in VERSIONS))
    lines.append("  Nothing was written. If your version is missing, open an issue with the lines above.")
    return "\n".join(lines)
