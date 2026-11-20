#!/usr/bin/env python3
"""
Run the packaged Ren'Py Engine on a Linux desktop *as if* it were Android.

This is a development tool for the port: it catches crashes without a phone
and without a Gradle build.

    python3 port/build_android.py --stage-only        # patch + compile + dist
    python3 port/sim/simulate_device.py package       # RAPT split into a fake device
    python3 port/sim/simulate_device.py run \\
        [--fresh] [--steps port/sim/scenarios/x.json] [--seconds N] [--restarts N]

"package" uses the (patched) RAPT code itself - split_renpy(), make_tar()
with the block/keep lists, make_assets_tree() - to produce exactly the files
a device gets:

    <device>/files/          ANDROID_PRIVATE: the unpacked private tar
                             (main.py, renpy/, lib/python3.12, gui/, ...)
    <device>/app.apk         a zip with the x-prefixed assets/ tree, read by
                             the real android.apk module (ANDROID_APK)
    <device>/public/         ANDROID_PUBLIC (app's external files dir)
    <device>/old_public/     ANDROID_OLD_PUBLIC

"run" starts the Linux build of the SDK's Python on <device>/files/main.py
with the same environment variables the Java side (PythonSDLActivity) sets,
so renpy.android is True and every Android-only branch of the engine and of
the port is exercised. Only pyjnius is replaced by a small stub
(port/sim/stubs/jnius); the real android/ runtime package is used.

The shared-storage projects directory (/storage/emulated/0/RenPyProjects)
is redirected with RENPY_ENGINE_SHARED_STORAGE, which the port honours.

The app's process lifetime is simulated too: when the engine "exits" (the
Android activity would be finished) the simulator starts it again up to
--restarts times, like a user re-opening the app.
"""

import argparse
import os
import shutil
import subprocess
import sys
import zipfile

HERE = os.path.dirname(os.path.abspath(__file__))
WORK = os.environ.get("BUILD_WORK") or os.path.abspath("build-work")
RENPY_VERSION = "8.6.1"


def device_paths(device):
    return {
        "private": os.path.join(device, "files"),
        "apk": os.path.join(device, "app.apk"),
        "public": os.path.join(device, "public"),
        "old_public": os.path.join(device, "old_public"),
        "shared": os.path.join(device, "storage", "emulated", "0"),
    }


def package(device):
    dist = os.path.join(WORK, "android.dist")
    rapt = os.path.join(WORK, "rapt")

    if not os.path.isdir(dist):
        raise SystemExit("No %s - run port/build_android.py --stage-only first." % dist)

    if os.path.isdir(device):
        shutil.rmtree(device)
    os.makedirs(device)

    tmp = os.path.join(device, "dist-split")
    shutil.copytree(dist, tmp)

    sys.path.insert(0, os.path.join(rapt, "buildlib"))
    cwd = os.getcwd()
    os.chdir(rapt)

    try:
        import rapt.build as rb  # noqa
        import rapt.interface as ri  # noqa

        rb.blocklist = rb.PatternList("blocklist.txt")
        rb.keeplist = rb.PatternList("keeplist.txt")

        private, assets = rb.split_renpy(tmp)

        paths = device_paths(device)

        tarfn = os.path.join(device, "private.tar.gz")
        rb.make_tar(ri.Interface(), tarfn, [private])

        import tarfile

        os.makedirs(paths["private"])
        with tarfile.open(tarfn) as tf:
            tf.extractall(paths["private"], filter="fully_trusted")

        apk_assets = os.path.join(device, "apk-assets")
        rb.make_assets_tree(assets, apk_assets)

    finally:
        os.chdir(cwd)

    with zipfile.ZipFile(paths["apk"], "w", zipfile.ZIP_STORED) as zf:
        for dn, dirs, files in os.walk(apk_assets):
            dirs.sort()
            for fn in sorted(files):
                full = os.path.join(dn, fn)
                rel = os.path.relpath(full, apk_assets).replace(os.sep, "/")
                zf.write(full, "assets/" + rel)

    shutil.rmtree(tmp)
    shutil.rmtree(apk_assets)

    for k in ("public", "old_public", "shared"):
        os.makedirs(paths[k], exist_ok=True)

    print("device ready:", device)


def run(device, seconds, steps, restarts, extra_env, fresh):
    import time

    paths = device_paths(device)
    sdk = os.path.join(WORK, "renpy-%s-sdk" % RENPY_VERSION)
    python = os.path.join(sdk, "lib", "py3-linux-x86_64", "python")

    if fresh:
        # A brand-new install: wipe everything except the unpacked app.
        for k in ("public", "old_public", "shared"):
            shutil.rmtree(paths[k], ignore_errors=True)
            os.makedirs(paths[k])

    env = dict(os.environ)
    env.update(
        {
            "ANDROID_PRIVATE": paths["private"],
            "ANDROID_PUBLIC": paths["public"],
            "ANDROID_OLD_PUBLIC": paths["old_public"],
            "ANDROID_APK": paths["apk"],
            "RENPY_ENGINE_SHARED_STORAGE": paths["shared"],
            "SDL_AUDIODRIVER": "dummy",
            # SDL's offscreen driver renders to a real-size EGL pbuffer
            # (Mesa llvmpipe): no X server needed, and screenshots work.
            "SDL_VIDEODRIVER": os.environ.get("SIM_VIDEODRIVER", "offscreen"),
            "PYTHONUNBUFFERED": "1",
        }
    )

    driver_log = os.path.join(paths["public"], "sim_driver.log")
    driver_rpy = os.path.join(paths["public"], "game", "zz_sim_driver.rpy")

    for fn in (driver_log, os.path.join(paths["public"], "sim_progress.json")):
        if os.path.exists(fn):
            os.unlink(fn)

    if steps:
        os.makedirs(os.path.dirname(driver_rpy), exist_ok=True)
        shutil.copy(os.path.join(HERE, "sim_driver.rpy"), driver_rpy)
        env["RENPY_ENGINE_SIM_STEPS"] = os.path.abspath(steps)
        # Projects run in-process don't search ANDROID_PUBLIC/game; the
        # bootstrap adds RENPY_SEARCHPATH in project mode.
        env["RENPY_SEARCHPATH"] = os.path.dirname(driver_rpy)
    elif os.path.exists(driver_rpy):
        os.unlink(driver_rpy)
        if os.path.exists(driver_rpy + "c"):
            os.unlink(driver_rpy + "c")

    if steps:
        import json

        with open(steps) as f:
            scenario = json.load(f)

        if isinstance(scenario, dict):
            for k, v in scenario.get("env", {}).items():
                env[k] = v.replace("$DEVICE", device)

    for kv in extra_env:
        k, _, v = kv.partition("=")
        env[k] = v

    def driver_done():
        try:
            with open(driver_log) as f:
                return "DONE all steps" in f.read()
        except Exception:
            return False

    statuses = []

    for i in range(restarts + 1):
        print("=== simulated app start #%d ===" % (i + 1), flush=True)

        p = subprocess.Popen(
            [python, "-X", "utf8", os.path.join(HERE, "stubs", "sim_boot.py"), os.path.join(paths["private"], "main.py")],
            env=env,
            cwd=paths["private"],
        )

        deadline = time.time() + seconds
        done_at = None

        while p.poll() is None and time.time() < deadline:
            time.sleep(0.25)
            if steps and done_at is None and driver_done():
                done_at = time.time()
            if done_at is not None and time.time() > done_at + 3:
                break

        if p.poll() is None:
            p.kill()
            p.wait()
            status = "done" if done_at else "timeout"
            statuses.append(status)
            print("=== app still running (%s, no crash) ===" % status, flush=True)
            break

        statuses.append(p.returncode)
        print("=== app process exited with status", p.returncode, "===", flush=True)

        if steps and driver_done():
            break

    for fn in [
        os.path.join(paths["public"], "renpy_crash.txt"),
        os.path.join(paths["public"], "traceback.txt"),
        driver_log,
    ]:
        if os.path.exists(fn):
            print("----", fn)
            with open(fn) as f:
                print(f.read())

    return statuses


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("command", choices=["package", "run"])
    ap.add_argument("--device", default=os.path.join(WORK, "sim-device"))
    ap.add_argument("--seconds", type=float, default=60)
    ap.add_argument("--steps", default=None, help="JSON list of UI steps for sim_driver.rpy")
    ap.add_argument("--fresh", action="store_true", help="wipe app data first (new install)")
    ap.add_argument("--restarts", type=int, default=0)
    ap.add_argument("--env", action="append", default=[])
    args = ap.parse_args()

    if args.command == "package":
        package(args.device)
    else:
        statuses = run(args.device, args.seconds, args.steps, args.restarts, args.env, args.fresh)
        print("statuses:", statuses)


if __name__ == "__main__":
    main()
