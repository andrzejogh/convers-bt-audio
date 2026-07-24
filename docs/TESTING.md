# Testing with injected CAN frames

**Prove the cluster + patch work on their own — without the phone or Bluetooth module.**

The patch only ever displays what arrives on the CAN bus as **ID `0x4B1`, source byte `0x12`**. That metadata is broadcast by the module that handles Bluetooth audio, on the shared MS-CAN (infotainment) bus. Because it's a broadcast, every node on that bus — the radio/head unit **and** the Convers+ cluster — sees the same frames.

So there are two independent questions when "nothing shows from my phone":

1. **Does my patched cluster display `0x4B1` metadata at all?** → inject known frames and see.
2. **Does my Bluetooth source actually put the metadata on `0x4B1`?** → capture the bus and look.

This page covers #1. If injected frames show up but your phone's don't, the patch is fine and the problem is on the source side (#2) — your BT module / head unit isn't broadcasting `0x4B1`, or is using a different ID.

---

## What you need

- A **PCAN-USB adapter** *or* a cheap compatible clone (the common "PCAN-USB" clones sold for ~$10–15 online work with PEAK's software). 
- **[PCAN-View](https://www.peak-system.com/PCAN-View.242.0.html?&L=1)** — PEAK's free CAN monitor/transmitter for Windows.
- Access to the car's **MS-CAN** (infotainment) bus. This is the bus the cluster uses for media text; it runs at **125 kbit/s**. Where to tap it is vehicle-specific — the [microhacker forum](https://microhacker.denkdose.de/) documents the connectors for these clusters.

> ⚠️ Ignition **on / ACC, car parked**, engine off is fine. Do not do this while driving. You are only adding harmless metadata frames, but treat the CAN bus with respect.

---

## Easiest test — a single frame

Generate the frame:

```bash
python tools/make_test_frames.py --title "OK"
```

It prints something like:

```
CAN ID: 0x4B1   DLC: 8   (MS-CAN, 125 kbit/s)

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

## Fuller test — a real title (multi-frame)

A longer string is sent as a First Frame + Consecutive Frames that must go out **in order, once per burst**. The tool can transmit them for you with correct ordering and timing:

```bash
python tools/make_test_frames.py --title "PATCH TEST" --artist "CANBUS" --send --loop 30
```

(`--send` needs the PCAN adapter and PEAK's `PCANBasic` Python module, which ships with the PCAN drivers.) Without `--send` it just prints the frames so you can build a PCAN-View transmit list and trigger them in sequence.

---

## Reading the result

| Injected `0x4B1` shows on the cluster? | Meaning |
|---|---|
| **Yes** | Flash + patch are good. If your phone's metadata still doesn't show, the issue is the **Bluetooth source**: your BT module / head unit isn't broadcasting `0x4B1`. Capture the bus (below) to confirm. |
| **No** | The flash didn't take, or the firmware version differs from what the patch targets. Re-check step 2/3 in [FLASHING.md](FLASHING.md) — the scripts abort on a byte mismatch, so a clean run means the bytes matched. |

### Checking whether your phone's metadata is on the bus

With PCAN-View **recording**, play a Bluetooth track and switch songs a couple of times. Look for **`0x4B1`** frames carrying text, e.g. `10 LL 46 12 01 01 …` (a title First Frame). 

- If you **see** them but they don't display, open an issue with a short snippet of those frames.
- If you **don't** see any `0x4B1` text, your source isn't broadcasting it. It may be using a different CAN ID — a capture of what *does* appear when metadata changes could let us support it.

> Never post a full CAN log publicly — it can contain your VIN. A few `0x4B1` lines are enough.
