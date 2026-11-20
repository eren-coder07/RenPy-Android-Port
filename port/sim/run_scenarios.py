#!/usr/bin/env python3
"""
Runs every port/sim/scenarios/*.json on a fresh simulated install and
reports failures: a missed "expect" or "click" step, an app process that
exited (a crash), or a renpy_crash.txt / traceback.txt being written.

    BUILD_WORK=build-work sh port/sim/build_and_package.sh
    BUILD_WORK=build-work python3 port/sim/run_scenarios.py [names...] [--shots DIR]

Screenshots taken by "shot" steps are copied to --shots (default:
<BUILD_WORK>/sim-shots).
"""

import glob
import os
import shutil
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
WORK = os.environ.get("BUILD_WORK") or os.path.abspath("build-work")


def main():
    argv = sys.argv[1:]
    shots = os.path.join(WORK, "sim-shots")

    if "--shots" in argv:
        i = argv.index("--shots")
        shots = os.path.abspath(argv[i + 1])
        del argv[i:i + 2]

    scenarios = sorted(glob.glob(os.path.join(HERE, "scenarios", "*.json")))

    if argv:
        scenarios = [s for s in scenarios if os.path.splitext(os.path.basename(s))[0] in argv]

    os.makedirs(shots, exist_ok=True)
    public = os.path.join(WORK, "sim-device", "public")
    failures = []

    for sc in scenarios:
        name = os.path.splitext(os.path.basename(sc))[0]
        print("### scenario", name, flush=True)

        log = os.path.join(WORK, "sim-%s.log" % name)

        with open(log, "w") as f:
            subprocess.run(
                [sys.executable, os.path.join(HERE, "simulate_device.py"), "run", "--fresh", "--seconds", "400", "--steps", sc],
                stdout=f,
                stderr=subprocess.STDOUT,
            )

        with open(log) as f:
            output = f.read()

        problems = []

        try:
            with open(os.path.join(public, "sim_driver.log")) as f:
                driver = f.read()
        except Exception:
            driver = ""

        for line in driver.splitlines():
            if "EXPECT FAILED" in line or "NOT FOUND" in line:
                problems.append(line[:300])

        if "DONE all steps" not in driver:
            problems.append("the scenario did not finish (app exited or hung)")

        if "app process exited" in output:
            problems.append("the app process exited")

        for fn in ("renpy_crash.txt", "traceback.txt"):
            path = os.path.join(public, fn)
            if os.path.exists(path):
                with open(path, errors="replace") as f:
                    problems.append("%s:\n%s" % (fn, f.read()[-1500:]))

        for png in glob.glob(os.path.join(public, "sim-*.png")):
            shutil.copy(png, os.path.join(shots, "%s-%s" % (name, os.path.basename(png)[4:])))

        if problems:
            failures.append(name)
            print("FAIL", name)
            for p in problems:
                print("   ", p)
            print("    (full log: %s)" % log)
        else:
            print("ok", name)

    print()
    print("%d scenario(s), %d failed%s" % (len(scenarios), len(failures), (": " + ", ".join(failures)) if failures else ""))
    print("screenshots:", shots)

    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
