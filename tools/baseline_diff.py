#!/usr/bin/env python3
"""
baseline_diff.py -- regression review for the smart-filename engine.

It runs build_smart_filename("", caption) over every line of your captions
export, and compares the result with a saved baseline.

  # first time (do this BEFORE changing the engine): save today's outputs
  python tools/baseline_diff.py --input captions_export.txt --update

  # after every engine change: print ONLY the lines whose output changed
  python tools/baseline_diff.py --input captions_export.txt

  # happy with the new outputs? make them the new baseline
  python tools/baseline_diff.py --input captions_export.txt --update

Options
  --baseline PATH   default tools/baseline.jsonl (a .jsonl.gz path also works)
  --repo PATH       repo root whose body/ package is imported (default: this repo)
  --show N          max changed lines printed on screen (default 40, 0 = none)
  --out FILE        write ALL changed lines to FILE (old / new side by side)
  --limit N         only the first N lines of the export

The speed line (captions/sec) covers only the engine calls.
"""
import argparse
import gzip
import importlib
import json
import os
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))


def _open(path, mode):
    if path.endswith(".gz"):
        return gzip.open(path, mode + "t", encoding="utf-8")
    return open(path, mode, encoding="utf-8")


def load_engine(repo):
    sys.path.insert(0, repo)
    import logging
    logging.disable(logging.CRITICAL)
    mod = importlib.import_module("body.Caption")
    return mod.build_smart_filename


def run_engine(build, lines):
    outs, errors = [], 0
    t0 = time.perf_counter()
    for ln in lines:
        try:
            outs.append(build("", ln))
        except Exception as e:                      # never abort the whole run
            errors += 1
            outs.append("!!ERROR %s: %s" % (type(e).__name__, e))
    return outs, time.perf_counter() - t0, errors


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--input", required=True, help="captions export, one caption per line")
    ap.add_argument("--baseline", default=os.path.join(HERE, "baseline.jsonl"))
    ap.add_argument("--repo", default=os.path.dirname(HERE))
    ap.add_argument("--update", action="store_true", help="save the current outputs as the new baseline")
    ap.add_argument("--show", type=int, default=40)
    ap.add_argument("--out", default="")
    ap.add_argument("--limit", type=int, default=0)
    a = ap.parse_args()

    with open(a.input, encoding="utf-8", errors="replace") as f:
        lines = [l.rstrip("\n") for l in f]
    if a.limit:
        lines = lines[: a.limit]

    build = load_engine(os.path.abspath(a.repo))
    outs, secs, errors = run_engine(build, lines)
    rate = len(lines) / secs if secs else 0.0
    print("engine: %d captions in %.2fs  ->  %.0f captions/sec   (errors: %d)" % (len(lines), secs, rate, errors))

    if a.update or not (os.path.exists(a.baseline) or os.path.exists(a.baseline + ".gz")):
        with _open(a.baseline, "w") as f:
            for i, (ln, out) in enumerate(zip(lines, outs)):
                f.write(json.dumps({"i": i, "out": out}, ensure_ascii=False) + "\n")
        print("baseline saved: %s (%d lines)" % (a.baseline, len(lines)))
        return 0

    base = {}
    bpath = a.baseline
    if not os.path.exists(bpath) and os.path.exists(bpath + ".gz"):
        bpath = bpath + ".gz"                 # the shipped baseline is gzipped
    with _open(bpath, "r") as f:
        for row in f:
            r = json.loads(row)
            base[r["i"]] = r["out"]
    changed = []
    for i, (ln, out) in enumerate(zip(lines, outs)):
        old = base.get(i)
        if old is not None and old != out:
            changed.append((i, ln, old, out))
    missing = len(lines) - sum(1 for i in range(len(lines)) if i in base)
    print("compared against %s: %d unchanged, %d CHANGED%s" % (
        bpath, len(lines) - len(changed) - missing, len(changed),
        (", %d not in baseline" % missing) if missing else ""))

    def block(i, ln, old, new):
        return "#%d\n  CAPTION: %s\n  OLD:     %s\n  NEW:     %s\n" % (i + 1, ln, old, new)

    for c in changed[: a.show]:
        print(block(*c))
    if len(changed) > a.show > 0:
        print("... %d more changed line(s); use --out FILE to save them all" % (len(changed) - a.show))
    if a.out:
        with open(a.out, "w", encoding="utf-8") as f:
            for c in changed:
                f.write(block(*c) + "\n")
        print("all changed lines written to %s" % a.out)
    return 0


if __name__ == "__main__":
    sys.exit(main())
