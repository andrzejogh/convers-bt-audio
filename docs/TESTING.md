# Testing with injected CAN frames

**Prove the cluster + patch work on their own — without the phone or Bluetooth module.**

The patch only ever displays what arrives on the CAN bus as **ID `0x4B1`, source byte `0x12`**. That metadata is broadcast by the module that handles Bluetooth audio, on the shared **MM MS-CAN** (multimedia) bus. Because it's a broadcast, every node on that bus — the radio/head unit **and** the Convers+ cluster — sees the same frames.

So there are two independent questions when "nothing shows from my phone":

1. **Does my patched cluster display `0x4B1` metadata at all?** → inject known frames and see.
2. **Does my Bluetooth source actually put the metadata on `0x4B1`?** → capture the bus and look.

This page covers #1. If injected frames show up but your phone's don't, the patch is fine and the problem is on the source side (#2) — your BT module / head unit isn't broadcasting `0x4B1`, or is using a different ID.

---

## First — the zero-hardware check: does USB metadata show?

Before reaching for a CAN adapter, answer one question: **when you play music from a USB stick, does the cluster show the track title and artist?**

USB metadata is stock functionality, and the patch feeds the Bluetooth text into the *same* media display pipeline the cluster already uses for USB. So:

- **USB titles show** → the cluster's media display works. Whatever's wrong is specific to Bluetooth: either the patch isn't active (flash) or nothing is arriving on `0x4B1` (source). Continue below.
- **USB titles don't show either** → the problem is more fundamental (wrong/faulty cluster, wrong CAN bus, or a variant this patch doesn't target). The Bluetooth patch can't work if the media display itself doesn't. Sort this out first.

If you don't have a USB source to try, the `0x4C7` injection test below is the powered equivalent.

---

## What you need

- A **PCAN-USB adapter** *or* a cheap compatible clone (the common "PCAN-USB" clones sold for ~$10–15 online work with PEAK's software).
- **[PCAN-View](https://www.peak-system.com/PCAN-View.242.0.html?&L=1)** — PEAK's free CAN monitor/transmitter for Windows. Installing it also installs the PCAN driver.
- A tap into the car's **MM MS-CAN** (multimedia) bus at **125 kbit/s** — see [Wiring into the bus](#wiring-into-the-bus) just below; the easy way is the OBD-II port.

**Software — you only need what your chosen method uses:**

| Method | Needs |
|---|---|
| **Copy the frame bytes below into PCAN-View** (simplest) | Just PCAN-View. **No Python, no scripts.** |
| **Run `make_test_frames.py` to print frames for custom text** | Python 3 (standard library only — nothing to install). |
| **Run `make_test_frames.py --send` to transmit automatically** | Python 3 **plus** PEAK's [PCAN-Basic API](https://www.peak-system.com/PCAN-Basic.239.0.html?&L=1): copy its `PCANBasic.py` into the `tools/` folder next to the script (the PCAN driver, installed with PCAN-View, provides the matching `PCANBasic.dll`). |

For the quick tests below, the exact bytes are printed right here — so the zero-Python PCAN-View route is all you need.

> ⚠️ Ignition **on / ACC, car parked**, engine off is fine. Do not do this while driving. You are only adding harmless metadata frames, but treat the CAN bus with respect.

---

## Wiring into the bus

The Bluetooth/USB metadata rides on the **MM MS-CAN** (the multimedia CAN), at **125 kbit/s** — the bus the radio/nav (ACM) and the Convers+ cluster (IPC) share. Note this is a *different* bus from the regular MS-CAN, so use the right pins.

The easiest and safest tap is the **OBD-II (DLC) port** — no need to open the cluster:

| Signal | OBD-II pin |
|---|:--:|
| **MM MS-CAN CAN-H** (metadata) | **pin 1** |
| **MM MS-CAN CAN-L** (metadata) | **pin 8** |
| **Ground — mandatory** | **pin 4** (chassis) or **pin 5** (signal) |

Connect **all three**: adapter CAN-H → pin 1, CAN-L → pin 8, and — this part is **mandatory** — the adapter's **GND → pin 4 or 5**. A USB-CAN adapter (CANable, PCAN clone, etc.) running off a laptop has no shared reference with the car otherwise, and CAN will be unreliable or simply won't work without that common ground.

Set your adapter to **125 kbit/s**. Do **not** add a termination resistor — the bus is already terminated. Ignition on / ACC, car parked.

(For reference, the other buses on the OBD-II port: regular MS-CAN = pins 3/11 @ 125k, HS-CAN = pins 6/14 @ 500k — but the metadata is on MM MS-CAN, pins 1/8.)

Pinout from the MK4-Wiki CAN-bus overview by Go4IT: <https://mk4-wiki.denkdose.de/artikel/can-bus/mk4_can-bus_schema>

---

## Easiest test — a single frame

Generate the frame:

```bash
python tools/make_test_frames.py --title "OK"
```

It prints something like:

```
CAN ID: 0x4B1   DLC: 8   (MM MS-CAN, 125 kbit/s)

  #  what          data bytes
  -- ------------- -----------------------
   1 title SF      06 46 12 01 01 4F 4B 00
```

In **PCAN-View** (connected at **125 kbit/s**):

1. Go to the **Transmit** tab → **New Message**.
2. ID `4B1`, Length `8`, Data `06 46 12 01 01 4F 4B 00`.
3. Set it to **Cyclic**, period e.g. `200 ms`, and start it.
4. Open the **BT Audio** screen on the cluster.

**Expected:** the title **`OK`** appears within a second. That means the flash took and the patch is working — full stop.

---

## Baseline test — the USB media path (works even on *stock* firmware)

There's a second, deeper test worth knowing. Send the **same data bytes on ID `0x4C7`** (the
factory USB media path) instead of `0x4B1`:

```bash
python tools/make_test_frames.py --title "OK" --id 0x4C7
```
→ `4C7  06 46 12 01 01 4F 4B 00`, sent cyclically in PCAN-View.

Because the source byte is still `0x12`, the cluster routes it into the **BT** store, so the
title shows on the **BT Audio** screen — and this works **even on unpatched firmware**, because
it reuses the media pipeline the cluster already runs for USB. (This is actually the experiment
that proved the whole approach was possible in the first place.)

It's a great baseline: it tells you whether the cluster's BT-screen display path works *at all*,
independently of both the patch and your Bluetooth source. Interpretation:

- **`0x4C7` test shows, `0x4B1` test doesn't** → the display path is fine, but the patch isn't
  active — the flash didn't take or the firmware version differs.
- **Both show** → everything cluster-side is good; a "nothing from my phone" issue is purely the
  Bluetooth source not broadcasting `0x4B1`.
- **Neither shows** → you're likely not on the right CAN bus / bitrate, or this is a cluster
  variant the patch doesn't target.

> Keep `0x4C7` tests to a short (≤ 3-char) title: a single frame needs no flow control, but a
> longer message on `0x4C7` does, and this tool doesn't send it.

## Fuller test — a real title (multi-frame)

A longer string is sent as a First Frame + Consecutive Frames that must go out **in order, once per burst**. The tool can transmit them for you with correct ordering and timing:

```bash
python tools/make_test_frames.py --title "PATCH TEST" --artist "CANBUS" --send --loop 30
```

(`--send` needs the extra `PCANBasic.py` copied into `tools/` — see the software table above; without it the script prints a reminder of exactly what to install.) Without `--send` it just prints the frames so you can build a PCAN-View transmit list and trigger them in sequence — no Python API required.

---

## Reading the result

| Injected `0x4B1` shows on the cluster? | Meaning |
|---|---|
| **Yes** | Flash + patch are good. If your phone's metadata still doesn't show, the issue is the **Bluetooth source**: your BT module / head unit isn't broadcasting `0x4B1`. Capture the bus (below) to confirm. |
| **No** | The flash didn't take, or the firmware version differs from what the patch targets. Re-check step 2/3 in [FLASHING.md](FLASHING.md) — the scripts abort on a byte mismatch, so a clean run means the bytes matched. |

### Checking whether your phone's metadata is on the bus

With PCAN-View **recording**, play a Bluetooth track and switch songs a couple of times. Look for **`0x4B1`** frames carrying text, e.g. `10 LL 46 12 01 01 …` (a title First Frame). 

- If you **see** them but they don't display, open an issue with a short snippet of those frames.
- If you **don't** see any `0x4B1` text, your source isn't broadcasting it. It may be using a different CAN ID — see the next section.

> Never post a full CAN log publicly — it can contain your VIN. A few `0x4B1` lines are enough.

---

## If your head unit uses a different CAN ID (experimental)

Different head units broadcast Bluetooth metadata on **different CAN IDs**. On the forum thread about this, a Blaupunkt **MCA** was found using IDs like **`4C3`, `4C6`, `4B0`** instead of the `4B1` this patch defaults to (there's no negotiation — each unit uses a fixed ID baked into its firmware):
<https://microhacker.denkdose.de/viewtopic.php?t=127>

**1. Find your ID.** Capture the bus (recording in PCAN-View), play Bluetooth and skip a couple of tracks, and look across the `0x4Bx`/`0x4Cx` range for a frame whose text changes with the track. A title frame looks like:

```
<ID>  10 LL 46 12 01 01 <first chars…>     (46 = title, 12 = Bluetooth source)
```

The `46 12 01 01` header is the tell-tale. Note the **ID** that frame arrives on.

**2. Build the patch for that ID** with `--canid`:

```bash
python tools/apply_patch_v3.py main.bin main_patched.bin --canid 0x4C6
python tools/apply_render.py  main_patched.bin main_patched.bin
```

then repack and flash as usual (see [FLASHING.md](FLASHING.md)).

### What "experimental" means here

- ✅ **Verified in the emulator:** the retargeted cave correctly reassembles and displays a frame on the new ID (e.g. `0x4C6`), and ignores others. Static analysis also confirms the patch's hook sits in the mailbox dispatch **before** any ID-based routing, so any ID the cluster accepts reaches it.
- ⚠️ **It only works for IDs the cluster already receives.** The cluster's CAN acceptance table lists: **`4B1, 4B3, 4C0, 4C1, 4C6, 4C7, 4D0, 4D2, 4D4, 4D5`**. `4C6` is on it — good, since it's one of the MCA's Bluetooth IDs. **`4C3` and `4B0` are *not*** — a head unit using those would need an extra FlexCAN mailbox reconfiguration, which this patch does not do.
- ⚠️ **Not yet confirmed on real hardware** for a non-default ID. If you try it, please report back with your car, head unit, ID and result — success or failure both help.
- The format must also match (`[field][0x12][01][01][text]`). If your capture shows a different layout, open an issue with a snippet.

> Never post a full CAN log publicly — it can contain your VIN. A few frames of the relevant ID are enough.
