#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
make_test_frames.py - generate CAN 0x4B1 test frames that make the patched Convers+ cluster
display a title/artist of YOUR choosing, without needing the phone or Bluetooth module at all.

Why: the patch only shows what arrives on CAN ID 0x4B1 (source byte 0x12). Injecting known
0x4B1 frames lets you prove the cluster + patch work on their own. If the title appears, the
patch is fine and any "nothing shows from my phone" problem is on the Bluetooth-source side
(your BT module / head unit isn't putting the metadata on 0x4B1). If it does NOT appear, the
flash didn't take or the firmware version differs.

By default it just PRINTS the frames (paste them into PCAN-View's transmit list). With --send
it transmits them itself via a PEAK PCAN-USB adapter (or a compatible clone) using PCANBasic.

Usage:
  python make_test_frames.py --title "OK"                      # simplest: one Single Frame
  python make_test_frames.py --title "PATCH TEST" --artist "CANBUS"
  python make_test_frames.py --title "PATCH TEST" --send --loop 30   # also transmit (needs PCAN)

Notes:
  * ISO-TP framing: <=3 chars -> one Single Frame (easiest to send in PCAN-View, send it cyclic).
    Longer text -> a First Frame + Consecutive Frames that must be sent IN ORDER, once per burst.
  * 0x4B1 needs NO flow control - the BT module just streams the frames; so do we.
  * MS-CAN (infotainment) bus, 125 kbit/s. Ignition on, car parked. See docs/TESTING.md.
"""
import sys, argparse

CID = 0x4B1
SRC_BT = 0x12
FIELD = {"title": 0x46, "artist": 0x42, "album": 0x3F}


def field_frames(field, src, text):
    """ISO-TP frames (each a list of 8 data bytes) carrying [field][src][01][01][text]."""
    tb = text.encode("latin-1", "replace")
    if len(tb) <= 3:
        # Single Frame: [0L][46][src][01][01][text...]  (cluster NUL-terminates internally)
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


def build(args):
    seq = []  # (label, [8 bytes])
    for name in ("title", "artist", "album"):
        text = getattr(args, name)
        if text is None:
            continue
        frs = field_frames(FIELD[name], SRC_BT, text)
        for n, fr in enumerate(frs):
            kind = "SF" if len(frs) == 1 else ("FF" if n == 0 else f"CF{n}")
            seq.append((f"{name} {kind}", fr))
    if not seq:
        seq = [(f"title SF", field_frames(0x46, SRC_BT, "OK")[0])]
    return seq


def hexbytes(b):
    return " ".join(f"{x:02X}" for x in b)


def print_frames(seq):
    print(f"CAN ID: 0x{CID:03X}   DLC: 8   (MS-CAN, 125 kbit/s)\n")
    print("  #  what          data bytes")
    print("  -- ------------- -----------------------")
    for i, (label, fr) in enumerate(seq, 1):
        print(f"  {i:>2} {label:<13} {hexbytes(fr)}")
    multi = any("CF" in l or "FF" in l for l, _ in seq)
    print()
    if multi:
        print("Multi-frame: send these IN ORDER, top to bottom, once per burst (not each on its")
        print("own cyclic timer - that would interleave them). Easiest is --send, or a short")
        print("PCAN-View transmit list triggered in sequence. A <=3-char title fits one frame.")
    else:
        print("Single frame: add it to PCAN-View's transmit list and send it cyclically (e.g.")
        print("every 200 ms). Watch the BT Audio screen - the title should appear within a second.")


def send_frames(seq, bitrate, loop, gap, stmin):
    import time
    from PCANBasic import (PCANBasic, PCAN_USBBUS1, PCAN_BAUD_125K, PCAN_BAUD_250K,
                           PCAN_BAUD_500K, TPCANMsg, PCAN_MESSAGE_STANDARD, PCAN_ERROR_OK)
    baud = {"125": PCAN_BAUD_125K, "250": PCAN_BAUD_250K, "500": PCAN_BAUD_500K}[bitrate]
    p = PCANBasic()
    st = p.Initialize(PCAN_USBBUS1, baud)
    if st != PCAN_ERROR_OK:
        print("PCAN Initialize failed:", hex(st)); sys.exit(1)
    print(f"PCAN @ {bitrate}k. Sending {len(seq)} frame(s) x {loop} bursts on 0x{CID:03X}.")
    print("Watch the BT Audio screen. Ctrl+C to stop.")
    try:
        for _ in range(loop):
            for _label, fr in seq:
                m = TPCANMsg(); m.ID = CID; m.MSGTYPE = PCAN_MESSAGE_STANDARD; m.LEN = 8
                for i in range(8):
                    m.DATA[i] = fr[i]
                p.Write(PCAN_USBBUS1, m)
                time.sleep(stmin)      # ~10 ms between consecutive frames, like the real module
            time.sleep(gap)            # pause between bursts
    except KeyboardInterrupt:
        pass
    finally:
        p.Uninitialize(PCAN_USBBUS1)
        print("\nPCAN released.")


def main():
    ap = argparse.ArgumentParser(description="Generate/send CAN 0x4B1 test frames for the patched Convers+ cluster.")
    ap.add_argument("--title", help="title text (0x46). <=3 chars = one Single Frame.")
    ap.add_argument("--artist", help="artist text (0x42).")
    ap.add_argument("--album", help="album text (0x3F).")
    ap.add_argument("--send", action="store_true", help="transmit via PCAN (needs PCANBasic + PEAK/compatible adapter).")
    ap.add_argument("--bitrate", default="125", choices=["125", "250", "500"], help="MS-CAN is 125 (default).")
    ap.add_argument("--loop", type=int, default=30, help="how many bursts to send (--send).")
    ap.add_argument("--gap", type=float, default=0.3, help="seconds between bursts (--send).")
    ap.add_argument("--stmin", type=float, default=0.01, help="seconds between frames within a burst (--send).")
    a = ap.parse_args()

    seq = build(a)
    if a.send:
        send_frames(seq, a.bitrate, a.loop, a.gap, a.stmin)
    else:
        print_frames(seq)


if __name__ == "__main__":
    main()
