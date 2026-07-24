# Flashing walkthrough — dump → patch → pack → flash

> ⚠️ Read the disclaimer in the [README](../README.md) first. Bricking is possible. Keep verified backups. Proceed at your own risk.

This project produces a patched firmware image. **It does not read from or write to your cluster** — you use your own dump/flash workflow for that. Community methods and hardware for Convers+ clusters are documented on the [microhacker forum](https://microhacker.denkdose.de/).

> Confirmed on a **Ford Mondeo MK4 facelift (FL), 2011+** (firmware 1412-FL, partition `CS7T-14C026-CD`). Other vehicles/versions are unverified — the scripts will abort in Step 2/3 if the bytes don't match.

## Prerequisites

- Python 3.8+ (`python --version`)
- Your **original firmware image** and **EEPROM**, backed up and verified
- Your **original VBF** file (used as the packing template — it carries the correct part number, load address and CRC layout)

## Step 0 — Back up

Dump the full firmware **and** the EEPROM. Make at least two copies and verify them (compare sizes/hashes). If a flash goes wrong, these are your only way back. Do **not** skip this and do **not** upload these files anywhere — they contain Ford code and your VIN.

## Step 1 — Get the code partition

The patch operates on the program image based at `0x5000` (the `CS7T-14C026-CD` partition on the confirmed setup). If your dump is already that raw image, use it as `main.bin`. If you have a VBF, you can unpack it:

```bash
python tools/vbf_tool.py unpack original.vbf main.bin
```

## Step 2 — Apply the CAN patch

```bash
python tools/apply_patch_v3.py main.bin main_patched.bin
```

Expected output (bytes may differ only if noted):

```
HOOK @0x236f6: f7 ff fc f5 (expected bl 0x230e4 = f7 ff fc f5) OK
CAVE 296 B; bl->cave f0 5f fd a3
Saved main_patched.bin
```

If instead you see a **mismatch / assertion error**, your firmware version is different. **Stop.** The script deliberately refuses to patch an image it doesn't recognise rather than corrupt it. Open an issue with the version string and the bytes it reported.

## Step 3 — Apply the renderer patch (19-char fields)

```bash
python tools/apply_render.py main_patched.bin main_patched.bin
```

Expected:

```
guard @0x1ce96 ... OK
guard @0x1ce8c ... OK
@0x1cedc: 0xa819 -> 0xa810  (title sp+0x64 -> sp+0x40)
...(5 sites)...
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

Any change **outside** those regions means something went wrong — do not flash.

```bash
# quick diff of changed byte offsets (Git Bash / Linux / macOS)
cmp -l main.bin main_patched.bin
```

## Step 6 — Flash

Flash `main_patched.vbf` with your usual tool. Afterwards:

1. Start the car / power the cluster normally.
2. Connect a phone over Bluetooth and start playing music.
3. Open the **BT Audio** screen — the track **title** and **artist** should now be shown (up to 19 characters each). A title longer than that arrives from the Bluetooth module already truncated to 18 characters plus a `~` marker — that is normal and comes from the module, not the cluster (see the README's Limitations).

## If it goes wrong

- **Cluster won't boot / black screen:** flash your **original** firmware VBF back. This is why Step 0 exists.
- **Deep brick (no bootloader response):** recovery typically needs JTAG/BDM hardware. See the "recover bricked Convers+" threads on the microhacker forum.
- **Titles still blank after a good flash:** confirm the source is really Bluetooth (`4B1`), and that your firmware version matches the one this patch targets (Step 2 would have warned otherwise).
