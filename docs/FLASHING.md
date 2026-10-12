# Flashing walkthrough — dump → patch → pack → flash

> ⚠️ Read the disclaimer in the [README](../README.md) first. Bricking is possible. Keep verified backups. Proceed at your own risk.

This project produces a patched firmware image. **It does not read from or write to your cluster** — you use your own dump/flash workflow for that. Community methods and hardware for Convers+ clusters are documented on the [microhacker forum](https://microhacker.denkdose.de/).

> Confirmed on a **Ford Mondeo MK4 facelift (FL), 2011+** (firmware 1412-FL, partition `CS7T-14C026-CD`). Firmware 15.07 (`CS7T-14C026-CG`) is also confirmed on the same car. Any other version is refused in Step 2/3 before anything is written.

## Prerequisites

- Python 3.8+ (`python --version`)
- Your **original firmware image** and **EEPROM**, backed up and verified
- Your **original VBF** file (used as the packing template — it carries the correct part number, load address and CRC layout)

## Step 0 — Back up

Dump the full firmware **and** the EEPROM. Make at least two copies and verify them (compare sizes/hashes). If a flash goes wrong, these are your only way back. Do **not** skip this and do **not** upload these files anywhere — they contain Ford code and your VIN.

## Step 1 — Get the code partition

The patch operates on the program image based at `0x5000` (the `CS7T-14C026-CD` partition on the confirmed setup, `CS7T-14C026-CG` for 15.07). If your dump is already that raw image, use it as `main.bin`. If you have a VBF, you can unpack it:

```bash
python tools/vbf_tool.py unpack original.vbf main.bin
```

## Step 2 — Apply the CAN patch

```bash
python tools/apply_patch_v3.py main.bin main_patched.bin
```

Expected output on 14.12 (15.07 prints its own addresses):

```
version       : 14.12 (CS7T-14C026-CD; confirmed on cars (Mondeo MK4 FL, S-Max))
  anchor size         : OK  1028096 B
  anchor reader       : OK  @0x230e4
  ...                   (11 anchors, all OK)
CAN ID filter : 0x4b1
HOOK @0x236f6: bl 0x230e4 -> bl 0x83240  (f0 5f fd a3)
CAVE 296 B @0x83240 (fill 0x00); SRAM scratch 0x40009300..0x40009321
flash self-test: balancing word 0x072f7020 @0xf0ffc - image adds up to 0
Saved main_patched.bin
```

The `flash self-test` line is the tool keeping the cluster's own flash check passing (one word of
unused fill at `0x0F0FFC`; without it the cluster stores DTC U3000 and a car with adaptive cruise
control reports "ACC not available" — [HOW_IT_WORKS.md §15](HOW_IT_WORKS.md)). The word's value
depends on your image. On an image that has the check switched off the line reads
`switched off in this image - no balancing needed`, and nothing is written there.

If instead you see **`REFUSED`**, your firmware version is not supported (or the file is not the code partition, or it is already patched — the message says which). **Stop.** The script deliberately refuses to patch an image it doesn't recognise rather than corrupt it. Open an issue with the lines it printed.

## Step 3 — Apply the renderer patch (19-char fields)

```bash
python tools/apply_render.py main_patched.bin main_patched.bin
```

Expected:

```
version 14.12 (CS7T-14C026-CD); renderer @ 0x1ce94
guard @0x1ce96 ... OK
guard @0x1ce8c ... OK
@0x1cedc: 0xa819 -> 0xa810  (title sp+0x64 -> sp+0x40)
...(5 sites)...
flash self-test: balancing word 0x07387044 @0xf0ffc - image adds up to 0
Saved main_patched.bin
```

The two "guard" checks confirm the shared stack frame/epilogue is exactly as expected before any byte is changed. If a guard fails, stop and file an issue.

> Want the conservative behaviour instead (no renderer change, fields capped so nothing can ever overflow)? Skip this step and edit the clamp in `apply_patch_v3.py` back to a lower value. See [HOW_IT_WORKS.md §7–8](HOW_IT_WORKS.md).

## Step 4 — Repack into a VBF

Use your **original** VBF as the template so the output keeps the correct part number, address and CRC framing:

```bash
python tools/vbf_tool.py pack original.vbf main_patched.bin main_patched.vbf
```

The tool recomputes the data checksums and validates the container. You should see `validation: OK`.

## Step 5 — Verify before flashing (recommended)

Confirm the patched image differs from your original **only** in the expected regions:

- hook: file offset `0x1e6f6` (4 bytes)
- cave: `0x7e240 … 0x7e367` (296 bytes)
- renderer: 5 single-byte sites in `0x17edd … 0x17f4b`
- flash self-test balancing word: file offset `0xebffc` (4 bytes; absent on an image with the check switched off)

Any change **outside** those regions means something went wrong — do not flash.

```bash
# quick diff of changed byte offsets (Git Bash / Linux / macOS)
cmp -l main.bin main_patched.bin
```

`python tools/emulate.py --dump main_patched.bin` runs the patch and the cluster's own flash check
in an emulator; every line, including `FLASH`, must say `PASS`.

## Step 6 — Flash

Flash `main_patched.vbf` with your usual tool. Afterwards:

1. Start the car / power the cluster normally.
2. Connect a phone over Bluetooth and start playing music.
3. Open the **BT Audio** screen — the track **title** and **artist** should now be shown. A longer title arrives from the Bluetooth module already truncated to 18 characters plus a `~` marker (the cluster ignores the `~`, so you see 18) — that is the module's doing, not the patch. See the README's Limitations.

## If it goes wrong

- **Cluster won't boot / black screen:** flash your **original** firmware VBF back. This is why Step 0 exists.
- **Deep brick (no bootloader response):** recovery typically needs JTAG/BDM hardware. See the "recover bricked Convers+" threads on the microhacker forum.
- **DTC U3000 in the cluster / "ACC not available" after flashing:** the image was patched with tools from before 2026-10-12 and fails the cluster's flash check. Run `python tools/apply_render.py main_patched.bin main_fixed.bin`, repack (Step 4) and flash again.
- **Titles still blank after a good flash:** confirm the source is really Bluetooth (`4B1`), and that your firmware version matches the one this patch targets (Step 2 would have warned otherwise).
