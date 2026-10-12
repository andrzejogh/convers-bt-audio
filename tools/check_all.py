#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
check_all.py - run the whole patch pipeline over a folder of cluster firmware and report, per
image, whether it is supported and whether the patch provably works on it.

For every VBF / BIN found (recursively) it:
  1. unpacks VBFs and validates their checksums (vbf_tool.py);
  2. keeps only Convers+ FL application images (VBF block 0x5000 / 0xFB000); GFX, bootloaders
     and older layouts are listed as "not an FL application image";
  3. identifies the version (fw_versions.py) - an unsupported image must be REFUSED, and the
     report says which anchors failed against which version;
  4. for a supported image: builds the patch in memory exactly as apply_patch_v3.py +
     apply_render.py would, checks that NOTHING outside the hook, the cave, the five renderer
     sites and the flash self-test balancing word changed, runs both scripts again in the reverse
     order (must give the same bytes), checks that the image adds up to 0 (the firmware's flash
     self-test), repacks the VBF and re-validates its checksums;
  5. runs the emulator A/B self-test (emulate.py): stock drops the frame, patched shows it,
     a wrong CAN ID is ignored, and the firmware's own flash self-test accepts the image.

Nothing is written next to your files; with --out DIR the patched .bin/.vbf are saved there.
Identical payloads (e.g. a VBF and its already-unpacked .bin) are tested once.

Usage:
  python check_all.py <folder-or-file> [...] [--out DIR] [--json report.json]
                      [--canid 0xNNN] [--no-emulate] [--verbose]
Exit code: 0 = every supported image passed; 1 = at least one supported image failed.
Refused (unsupported) images are expected and do not fail the run.
"""
import argparse
import hashlib
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import fw_versions as FV                                                  # noqa: E402
import apply_patch_v3                                                     # noqa: E402
import apply_render                                                       # noqa: E402
import vbf_tool                                                           # noqa: E402


def collect(paths):
    """-> list of dicts {source, kind, data, vbf, part, problem}"""
    files = []
    for p in paths:
        if os.path.isdir(p):
            for root, _, names in os.walk(p):
                files += [os.path.join(root, n) for n in sorted(names)
                          if n.lower().endswith((".vbf", ".bin"))]
        else:
            files.append(p)
    items = []
    # VBFs first: when a VBF and its unpacked .bin are both present, the VBF is the one tested
    # (it also gets the repack check) and the .bin is reported as a duplicate.
    for f in sorted(files, key=lambda s: (not s.lower().endswith(".vbf"), s.lower())):
        it = {"source": f, "kind": "vbf" if f.lower().endswith(".vbf") else "bin",
              "data": None, "vbf": None, "part": None, "problem": None}
        try:
            if it["kind"] == "vbf":
                v = vbf_tool.Vbf(f)
                it["vbf"], it["part"] = v, v.part_number
                errs = v.check()
                if errs:
                    it["problem"] = "VBF checksum/size invalid: " + "; ".join(errs)
                if (v.addr, v.length) != (FV.BASE, FV.IMAGE_SIZE):
                    it["problem"] = it["problem"] or (
                        f"not an FL application image (block {v.addr:#x}/{v.length:#x})")
                it["data"] = bytes(v.payload)
            else:
                it["data"] = open(f, "rb").read()
                if len(it["data"]) != FV.IMAGE_SIZE:
                    it["problem"] = f"not an FL application image ({len(it['data'])} B)"
        except Exception as ex:                                            # noqa: BLE001
            it["problem"] = f"unreadable: {ex}"
        items.append(it)
    return items


def allowed_ranges(c, cave_len):
    r = [(c["HOOK"], 4), (c["CAVE"], cave_len)]
    r += [(a, 2) for a in c["RENDER_SITES"]]
    r += [(c["SUM_WORD"], 4)]                       # flash self-test balancing word
    r += [(a, 4) for a in c.get("GUARD_LITS", ())]  # stack-guard band bound (scratch memory)
    return [(a - FV.BASE, a - FV.BASE + n) for a, n in r]


def check_image(it, canid, emulate, out_dir, verbose):
    data = it["data"]
    row = {"source": it["source"], "part": it["part"] or "-", "banner": FV.banner(data) or "-",
           "sha256": hashlib.sha256(data).hexdigest()[:16], "status": None, "detail": ""}
    v, state, reports = FV.identify(data)
    if v is None:
        row["status"] = "REFUSED"
        best = min(reports.items(), key=lambda kv: sum(not ok for _, ok, _ in kv[1]))
        bad = [n for n, ok, _ in best[1] if not ok]
        row["detail"] = f"closest {best[0]}: {len(bad)} anchors off ({', '.join(bad)})"
        return row
    c = FV.cfg(v)
    row["version"] = v
    logs = []
    log = logs.append
    steps = []
    try:
        if state["cave"] == "patched":
            row["status"] = "PATCHED-INPUT"
            patched = data
            steps.append("input already patched" + (
                " (first-release build: scratch in the media buffer - rebuild from the original "
                "dump for the CD track-list fix)" if state.get("old_scratch") else ""))
        else:
            p1, _ = apply_patch_v3.patch(data, canid, log=log)
            patched, _, _ = apply_render.patch(p1, log=log)
            # reverse order must give the same image
            r1, _, _ = apply_render.patch(data, log=log)
            r2, _ = apply_patch_v3.patch(r1, canid, log=log)
            if r2 != patched:
                raise AssertionError("render->patch order gives a different image than patch->render")
            steps.append("order-independent")
            # nothing outside the declared edit ranges may change
            cave_len = len(apply_patch_v3.build_cave(c, canid))
            ranges = allowed_ranges(c, cave_len)
            stray = [i for i in range(len(data)) if data[i] != patched[i]
                     and not any(a <= i < b for a, b in ranges)]
            if stray:
                raise AssertionError(f"{len(stray)} bytes changed outside hook/cave/renderer/word, "
                                     f"first @{stray[0] + FV.BASE:#x}")
            ndiff = sum(1 for i in range(len(data)) if data[i] != patched[i])
            steps.append(f"{ndiff} B changed, all inside hook/cave/renderer/balancing word")
            # applying again must be refused / a no-op
            try:
                apply_patch_v3.patch(patched, canid, log=log)
                raise AssertionError("re-applying apply_patch_v3 on a patched image was not refused")
            except apply_patch_v3.Refused:
                pass
            steps.append("re-apply refused")
            if it["vbf"] is not None and out_dir is None:
                import tempfile
                tmp = os.path.join(tempfile.mkdtemp(prefix="convers_chk_"), "t.vbf")
                vbf_tool.build(it["source"], patched, tmp)
                if vbf_tool.Vbf(tmp).check():
                    raise AssertionError("repacked VBF fails its own checksum validation")
                os.remove(tmp)
                steps.append("VBF repack OK")
            if out_dir:
                base = os.path.join(out_dir, os.path.splitext(os.path.basename(it["source"]))[0] + "_bt")
                open(base + ".bin", "wb").write(patched)
                if it["vbf"] is not None:
                    vbf_tool.build(it["source"], patched, base + ".vbf")
                    if vbf_tool.Vbf(base + ".vbf").check():
                        raise AssertionError("repacked VBF fails its own checksum validation")
                    steps.append(f"saved {os.path.basename(base)}.vbf")
            row["status"] = "OK-BUILD"
        # the firmware's flash self-test must pass on what would be flashed
        ok, text = FV.sum_report(patched, c)
        if not ok:
            raise AssertionError(text)
        steps.append(text)
        if emulate:
            import emulate as EMU
            res = EMU.selftest(patched if state["cave"] != "patched" else data, canid=canid,
                               title="ABCDEFGHIJKLMNOPQRS", artist="abcdefghijklmnopqrs", log=log)
            if not res["ok"]:
                raise AssertionError(f"emulator A/B failed ({res['reason']})")
            steps.append("emulator A/B PASS (19-char title+artist, 0x4B0 ignored)")
            row["status"] = "PASS" if row["status"] == "OK-BUILD" else row["status"] + " PASS"
        elif row["status"] == "OK-BUILD":
            row["status"] = "PASS (static)"      # built and checked, but not executed
    except (AssertionError, apply_patch_v3.Refused, apply_render.Refused) as ex:
        row["status"] = "FAIL"
        steps.append(f"FAILED: {ex}")
    row["detail"] = "; ".join(steps)
    if verbose or row["status"] == "FAIL":
        row["log"] = logs
    return row


def main():
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("paths", nargs="+", help="folders and/or .vbf/.bin files")
    ap.add_argument("--out", help="save patched .bin (and .vbf) here")
    ap.add_argument("--json", help="write the full report as JSON")
    ap.add_argument("--canid", default="0x4B1", help="CAN ID to build for (default 0x4B1)")
    ap.add_argument("--no-emulate", action="store_true", help="skip the emulator A/B")
    ap.add_argument("--verbose", action="store_true", help="print the tool logs of every image")
    a = ap.parse_args()
    canid = int(a.canid, 0)
    emulate = not a.no_emulate
    if emulate:
        try:
            import unicorn  # noqa: F401
        except ImportError:
            print("NOTE: unicorn not installed (pip install unicorn) - running without the emulator A/B.")
            emulate = False
    if a.out:
        os.makedirs(a.out, exist_ok=True)

    items = collect(a.paths)
    seen = {}
    rows = []
    for it in items:
        if it["data"] is None or it["problem"]:
            rows.append({"source": it["source"], "part": it["part"] or "-", "status": "SKIPPED",
                         "banner": FV.banner(it["data"]) if it["data"] else "-",
                         "detail": it["problem"] or "no data"})
            continue
        h = hashlib.sha256(it["data"]).hexdigest()
        if h in seen:
            rows.append({"source": it["source"], "part": it["part"] or "-", "status": "DUPLICATE",
                         "banner": FV.banner(it["data"]) or "-",
                         "detail": f"same payload as {os.path.basename(seen[h])}"})
            continue
        seen[h] = it["source"]
        rows.append(check_image(it, canid, emulate, a.out, a.verbose))

    order = {"FAIL": 0, "PASS": 1, "PASS (static)": 1, "PATCHED-INPUT PASS": 1, "PATCHED-INPUT": 1,
             "REFUSED": 2, "SKIPPED": 3, "DUPLICATE": 4}
    print(f"{'status':<19} {'version':<7} {'banner':<7} {'part':<16} source / detail")
    print("-" * 110)
    quiet = ("SKIPPED", "DUPLICATE")
    for r in sorted(rows, key=lambda r: (order.get(r["status"], 9), r["source"].lower())):
        if r["status"] in quiet and not a.verbose:
            continue
        print(f"{r['status']:<19} {r.get('version', '-'):<7} {str(r.get('banner') or '-'):<7} "
              f"{r['part']:<16} {os.path.basename(r['source'])}")
        print(f"{'':<52} {r['detail']}")
        for line in r.get("log", []):
            print(f"{'':<56}| {line}")
    why = {"SKIPPED": "not a Convers+ FL application image (GFX, bootloader, older layout) or invalid VBF",
           "DUPLICATE": "same payload as a file already tested"}
    for st in quiet:
        n = sum(r["status"] == st for r in rows)
        if n and not a.verbose:
            print(f"{st:<19} {n} files - {why[st]} (list: --verbose)")
    counts = {}
    for r in rows:
        counts[r["status"]] = counts.get(r["status"], 0) + 1
    print("-" * 110)
    print("summary: " + ", ".join(f"{k} {v}" for k, v in sorted(counts.items(), key=lambda kv: order.get(kv[0], 9))))
    print("supported versions: " + ", ".join(f"{v} {FV.VERSIONS[v]['part']} ({FV.VERSIONS[v]['status']})"
                                             for v in FV.VERSIONS))
    if not emulate:
        print("NOTE: emulator A/B was NOT run.")
    if a.json:
        json.dump(rows, open(a.json, "w", encoding="utf-8"), indent=1, ensure_ascii=False)
        print(f"report: {a.json}")
    return 1 if any(r["status"] == "FAIL" for r in rows) else 0


if __name__ == "__main__":
    sys.exit(main())
