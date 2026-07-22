# How it works — reverse-engineering write-up

Target: Ford Convers+ instrument cluster, MCU **NXP MAC7116** (ARM7TDMI-S, Thumb-1, **big-endian**, ARMv4T), program firmware based at `0x5000`. All addresses below are in the MCU memory map; file offset into the code partition = `address − 0x5000`.

Confirmed and tested on a **Ford Mondeo MK4 facelift (FL), 2011+** (firmware 1412-FL, partition `CS7T-14C026-CD`). The offsets in this document are from that image.

## 1. The metadata protocol

Media metadata travels over CAN as ISO-TP text frames. Application payload format:

```
[field][source][0x01][0x01][text\0]
```

| Field byte | Meaning | | Source byte | Meaning |
|---|---|---|---|---|
| `0x46` | title | | `0x12` | Bluetooth |
| `0x42` | artist | | `0x13` | USB |
| `0x3F` | album | | `0x15` | CD |
| `0x45` | genre | | | |
| `0x43` | file name | | | |

CAN IDs: `4C7`/`4C2` = USB, `4B0`/`4B1` = Bluetooth, `4C1` = CD.

ISO-TP framing: First Frame `10 LL …`, Consecutive Frame `2N …`, Single Frame `0L …` (the length `LL` includes the trailing NUL). For one track, the radio sends a sequence on `4B1`: title, artist, genre (empty SF), album, file name (empty SF), `0x44` (empty SF) — each as its own ISO-TP message, back to back.

## 2. Root cause

The cluster has **zero** code references to `4B0`, and the receive framework treats `4B1` as a dead 2-byte "signal" (it reads the ISO-TP PCI bytes as if they were signal data). The BT text is never reassembled or stored anywhere.

Crucially, the **downstream** media pipeline works fine for source `0x12` (Bluetooth). Proof: injecting `[0x46][0x12][01][01]"TEST"` on the working USB ID `4C7` makes "TEST" appear on the BT Audio screen. Everything from the media dispatcher `FUN_0x5e31c` down already handles Bluetooth — only the delivery of `4B1` text was missing.

## 3. The code cave

**Hook:** at `0x236f6`, replace `bl 0x230e4` with `bl 0x83240` (the cave). This site is **before** the mailbox is split by type (`FUN_0x236cc`), so it sees **every** received frame (type 1/2/3). The cave calls the original `0x230e4` (which returns the mailbox number `mb`), does its work, and returns `mb` — fully transparent to the caller.

**Cave** at `0x83240` (296 bytes). SRAM scratch: `BUF=0x40009300` (24 B), `SLEN=0x40009318`, `SIDX=0x40009319`, `SBUS=0x40009320`.

1. Save bus, call `0x230e4` → `mb`, save `mb`.
2. `mailbox = *(0x7a0d4 + bus*4) + mb*16 + 0x80`; data at `mailbox+8`; `CAN-ID = (*(mb+4) >> 18) & 0x7FF`.
3. Filter: **only `0x4B1`** (see §5). Anything else → original path, untouched.
4. `PCI = data[0] >> 4`: 1 = FF, 2 = CF, 0 = SF. Reassemble into `BUF`. `exp_len` (`LL`) is clamped to 22.
5. On completion: NUL-terminate, check `source == 0x12`, then walk the field dispatcher list at `0x8eff0` (entries `[00][field][00 00][handler_ptr]`), set `*(0x40009abc) = entry`, and call the handler via `bx`. The handler (`FUN_0x6cd20` title / `0x6c9dc` artist / `0x6c90c` album) writes into the metadata store selected by the source byte.

## 4. Why earlier hook sites failed

| Version | Hook | Result |
|---|---|---|
| v1 | `0x236c0` | after descriptor routing, which **drops** `4B1` → nothing |
| v2 | `0x2371c` | catches only type-2 mailboxes; `4B1` isn't type 2 → nothing |
| **v3** | **`0x236f6`** | **before** type dispatch, catches all → **works** |

A misleading dead end: `4B1 → descriptor 3` appears "routable", but only via a **non-physical logical bus 2** (base ≠ `0xFC0…`). The only physical FlexCAN buses are 0 (`0xFC094000`) and 1 (`0xFC0A0000`).

## 5. The 4B0/4B1 interleave bug

`4B0` and `4B1` carry the same text, but their frames **interleave** at the frame level. Reassembling both into one shared buffer corrupts it (e.g. "Linkin Park" → "Linkin Panki", blank fields). Fix: process **`4B1` only**, using an exact `CAN-ID == 0x4B1` compare (not a bit-0 mask). `4B1` alone carries the complete text.

## 6. Single-frame fields

Genre (`0x45`), file name (`0x43`) and `0x44` arrive as empty Single Frames. The cave handles `PCI == 0`: `L = data[0] & 0x0F`, copies the payload as an immediate complete message, dispatches, and clears that field's own store. It never touches the title (different field → different store).

## 7. The empty-title bug and the renderer

Symptom: with the CAN patch working, the title sometimes rendered blank — **only when the artist was long**. The artist itself was always fine.

The reassembler was not at fault (the title store survives even with a 30-character artist). The culprit is the **stock BT Audio renderer** `FUN_0x1ce94`, which makes two on-stack `strcpy` (`0x74f20`) copies:

```
0x1cede: strcpy(sp+0x64, title)     ; title first  → buffer at sp+0x64
0x1ceee: strcpy(sp+0x54, artist)    ; artist second → buffer at sp+0x54
```

The gap `sp+0x64 − sp+0x54 = 0x10 = 16 bytes`. `strcpy` writes `strlen+1` bytes, so an artist of **≥16 characters** (16 + NUL = 17 bytes) overflows the end of its buffer and drops a NUL onto `sp+0x64` = `title[0]`, blanking the already-written title. Exactly at 16: the 16-character artist renders fully **and** the title is empty. The original cave clamped fields to 16 characters — landing right on the threshold.

(The USB renderer `FUN_0x1cfe6` is structurally identical — same 16-byte gap. Its longer titles work only because the title is copied first and, with a short artist, its own overflow into saved registers is silently tolerated.)

## 8. The 18-character renderer patch

The frame **cannot** be enlarged: the epilogue at `0x1ce8c` (`add sp,#0x74; pop {r4-r7}; pop {r3}; bx r3`) is **shared** with the neighbouring function, so changing `sub sp` would break that function.

Clean workaround with no frame change: the renderer already loads the title via a getter (`0x6d550`) into `sp+0x40` — a 20-byte buffer running up to `sp+0x54` — and then needlessly re-copies it into the cramped `sp+0x64`. So we **redirect the 5 title references from `sp+0x64` to `sp+0x40`**:

```
add r0, sp, #0x64  (0xA819)  →  add r0, sp, #0x40  (0xA810)
@ 0x1cedc, 0x1cefe, 0x1cf1a, 0x1cf32, 0x1cf4a
```

Now the title stays in its 20-byte getter buffer (≤19 chars), the artist at `sp+0x54` has the full 32 bytes up to the saved registers (`sp+0x74`, ≤31 chars), and the title `strcpy` at `0x1ceda` becomes `strcpy(sp+0x40, sp+0x40)` — a harmless no-op that no longer writes to `sp+0x64`, so a long title can't clobber the saved registers either.

The cave's clamp is raised from 19 to **22** (text ≤18 chars). `BUF` is 24 bytes, so `exp_len = 22` leaves the NUL at `BUF[22]` safely in range.

The empty-artist → album fallback (`0x1cef4`) is stock behaviour, unchanged by the patch.

## 9. Key addresses

- Hook `0x236f6`; cave `0x83240`; original RX `0x230e4`; RX dispatcher `FUN_0x236cc`.
- FlexCAN: per-bus bases `*(0x7a0d4 + bus*4)`; `mailbox = base + mb*16 + 0x80`; ID at `+4` (`canid << 18`); data at `+8`.
- Media field dispatcher list `0x8eff0` (8-byte entries); handler-select `*(0x40009abc)`; `strcpy` `0x74f20`.
- Field handlers: title `0x6cd20`, artist `0x6c9dc`, album `0x6c90c` (copy ≤20 B, call store `0x3a320`).
- Metadata stores (base `0x4000a749`): **BT** title `0x4000a75d` / artist `0x4000a771` / album `0x4000a799`; USB title `0x4000a6d1`; CD title `0x4000a735`.
- BT Audio renderer `FUN_0x1ce94` (frame `0x74`, shared epilogue `0x1ce8c`). Renderer getters: `0x6d550` title, `0x6d568` artist, `0x6d580` album.

## 10. Verification method

Everything was validated in a [Unicorn](https://www.unicorn-engine.org/) emulator harness before flashing: reassembly (frames injected into a mailbox, cave invoked directly), and the full chain cave → store → renderer (run up to `0x1cf0a`, i.e. after the string copies; the actual pixel-drawing calls hit peripherals the harness doesn't model, and crash identically on stock, so that's an emulator artifact, not the patch). Boundary tests at 15/16/18 characters, source filtering, transparency for non-`4B1` traffic, `4B0`/`4B1` interleave, and empty-artist regression all pass.
