#!/usr/bin/env python3
"""Build the Ren'Py Engine ("port the engine itself") as a universal Android APK.

The result is a signed release APK containing the full Ren'Py launcher,
running on-device like the PC version: the user can create projects, edit
them with any Android editor (projects live in shared storage) and *run*
them in-process through a custom bootstrap (port/patches/main.py).

How it works (no launcher GUI involved):

1. Download the official Ren'Py SDK and RAPT for a pinned version.
2. Patch engine and RAPT (documented below):
   - rapt/buildlib/rapt/iconmaker.py : work without pygame_sdl2, find icons
     in the dist "assets/" subdir (where split_renpy() moves them).
   - SDK renpy/loader.py             : skip the packaged game APK asset layer
     when RENPY_PROJECT_GAME_DIR is set (on-device project mode).
   - SDK renpy/bootstrap.py          : command-mode hooks (engine_process_args
     / engine_command_completed) so launcher features that spawn a second
     interpreter on the PC (project scans, lint, compile, gui images,
     translations) run headless in-process and return to the launcher.
   - rapt/buildlib/rapt/build.py     : split_renpy() routes gui/, sdk-fonts/,
     tutorial/ and the_question/ into the private tar, so they unpack to
     real files on the device (the launcher reads/copies them directly).
   - rapt/keeplist.txt               : keep the .rpy sources of the template
     and tutorials (blocklist.txt would strip **.rpy).
   - rapt/templates/app-AndroidManifest.xml : requestLegacyExternalStorage.
   - launcher android/distribute/web/ios labels: notify + return on-device.
3. Patch the launcher game (front_page.rpy Launch action, mobile_support.rpy)
   and compile it to .rpyc with the SDK's own interpreter, headless.
4. Assemble the "android.dist" tree that rapt.build.build() expects and
   invoke rapt.build.build() directly, exactly like the launcher does.
   The dist must contain lib/python3.12/ (the pure-python runtime packages:
   android/, jnius/, truststore, requests, stdlib .pyc) — renpy.loader does
   `import android.apk` on every android boot and renpy.bootstrap does
   `import truststore`; without lib/python3.12 the app dies at the
   loading screen (official dists include it via the "lib/**" pattern).
5. Gradle produces a signed universal APK (all ABIs from RAPT's prototype:
   armeabi-v7a, arm64-v8a, x86_64).
"""

import json
import os
import shutil
import subprocess
import sys
import time
import urllib.request

RENPY_VERSION = "8.6.1"
BASE_URL = "https://www.renpy.org/dl/" + RENPY_VERSION

PACKAGE = "org.renpy.engine"
APP_NAME = "Ren'Py Engine"
ICON_NAME = "Ren'Py"
NUMERIC_VERSION = 80604

WORK = os.environ.get("BUILD_WORK") or os.path.abspath("build-work")
OUT = os.environ.get("BUILD_OUT") or os.path.abspath("out")
APK_NAME = f"RenPy-{RENPY_VERSION}-engine-android-universal-arm32-arm64.apk"

SDK = os.path.join(WORK, f"renpy-{RENPY_VERSION}-sdk")
RAPT = os.path.join(WORK, "rapt")
DIST = os.path.join(WORK, "android.dist")
STAGING = os.path.join(WORK, "launcher-dev")
KEYSTORE = os.environ.get("RENPY_KEYSTORE") or os.path.abspath("renpy.keystore")
STORE_PASSWORD = os.environ.get("RENPY_STORE_PASSWORD", "")
KEY_ALIAS = os.environ.get("RENPY_KEY_ALIAS", "renpyengine")
PROJECTS_DIR = "/storage/emulated/0/RenPyProjects"


def run(cmd, **kwargs):
    print("+", " ".join(str(i) for i in cmd), flush=True)
    subprocess.run([str(i) for i in cmd], check=True, **kwargs)


def download(url, dest):
    if os.path.exists(dest):
        print("cached:", dest)
        return
    print("downloading:", url, flush=True)
    tmp = dest + ".part"
    urllib.request.urlretrieve(url, tmp)
    os.rename(tmp, dest)


def read(fn):
    with open(fn, "r", encoding="utf-8") as f:
        return f.read()


def write(fn, text):
    with open(fn, "w", encoding="utf-8") as f:
        f.write(text)


def patch(path, old, new, required=True):
    text = read(path)
    if old not in text:
        if required:
            raise SystemExit(f"patch anchor not found in {path}:\n{old}")
        print(f"note: anchor missing in {path} (already patched?)")
        return
    write(path, text.replace(old, new, 1))
    print("patched:", path)


def patch_all(path, old, new):
    text = read(path)
    n = text.count(old)
    if n == 0:
        raise SystemExit(f"patch anchor not found in {path}:\n{old}")
    write(path, text.replace(old, new))
    print(f"patched {n}x:", path)


def fetch_sources():
    os.makedirs(WORK, exist_ok=True)
    download(f"{BASE_URL}/renpy-{RENPY_VERSION}-sdk.tar.bz2", os.path.join(WORK, "sdk.tar.bz2"))
    download(f"{BASE_URL}/renpy-{RENPY_VERSION}-rapt.zip", os.path.join(WORK, "rapt.zip"))
    if not os.path.isdir(SDK):
        run(["tar", "xjf", os.path.join(WORK, "sdk.tar.bz2"), "-C", WORK])
    if not os.path.isdir(RAPT):
        run(["unzip", "-q", os.path.join(WORK, "rapt.zip"), "-d", WORK])


def patch_engine_and_rapt():
    # --- 1. iconmaker: no pygame_sdl2 in CI python, icons live in assets/ ---
    iconmaker = os.path.join(RAPT, "buildlib", "rapt", "iconmaker.py")
    patch(
        iconmaker,
        "import renpy.pygame as pygame",
        "try:\n    import renpy.pygame as pygame\nexcept Exception:  # build without pygame_sdl2\n    pygame = None",
    )
    patch(
        iconmaker,
        "        if not pygame.display.get_surface():",
        "        if (pygame is not None) and (not pygame.display.get_surface()):",
    )
    patch(
        iconmaker,
        '        src = os.path.join(self.directory, f"android-{name}-{dpi}.png")',
        '        src = os.path.join(self.directory, f"android-{name}-{dpi}.png")\n'
        '        if not os.path.exists(src):\n'
        '            alt = os.path.join(self.directory, "assets", f"android-{name}-{dpi}.png")\n'
        '            if os.path.exists(alt):\n'
        '                src = alt',
    )
    patch(
        iconmaker,
        "        for i in [os.path.join(self.directory, fn), os.path.join(plat.path(\"templates\"), fn)]:",
        "        for i in [os.path.join(self.directory, fn), os.path.join(self.directory, \"assets\", fn), os.path.join(plat.path(\"templates\"), fn)]:",
    )

    # --- 2. loader: allow skipping the packaged game layer (project mode) ---
    loader = os.path.join(SDK, "renpy", "loader.py")
    patch(
        loader,
        """    if renpy.config.renpy_base == renpy.config.basedir:
        # Read the game data from the APKs.

        apks.append(android.apk.APK(prefix="assets/x-game/"))
        game_apks.append(apks[-1])
        for i in packs:
            apks.append(android.apk.APK(apk=i, prefix="assets/game/"))
            game_apks.append(apks[-1])
            split_apks.append(apks[-1])
""",
        """    if renpy.config.renpy_base == renpy.config.basedir:
        # Read the game data from the APKs.
        #
        # Ren'Py Engine port: when RENPY_PROJECT_GAME_DIR is set (by the
        # custom bootstrap main.py), a user project from the device
        # filesystem is being run in-process instead of the packaged
        # launcher, so the packaged game assets must not be merged into
        # the file index.
        if not os.environ.get("RENPY_PROJECT_GAME_DIR"):
            apks.append(android.apk.APK(prefix="assets/x-game/"))
            game_apks.append(apks[-1])
            for i in packs:
                apks.append(android.apk.APK(apk=i, prefix="assets/game/"))
                game_apks.append(apks[-1])
                split_apks.append(apks[-1])
""",
    )

    # --- 3. bootstrap: command-mode hooks (on-device launcher features) ---
    bootstrap = os.path.join(SDK, "renpy", "bootstrap.py")
    patch(
        bootstrap,
        "                renpy.game.args = args\n",
        "                renpy.game.args = renpy.__main__.engine_process_args(args) if hasattr(renpy.__main__, \"engine_process_args\") else args\n",
    )
    patch(
        bootstrap,
        "                renpy.config.basedir = basedir\n",
        "                renpy.config.basedir = (getattr(renpy.game.args, 'basedir', '') or basedir)\n",
    )
    patch(
        bootstrap,
        """                renpy.main.main()

                exit_status = 0
""",
        """                renpy.main.main()

                exit_status = 0

                # Ren'Py Engine port: after a headless command run (json
                # dump, lint, compile, gui_images, ...) completes, hand
                # control back to the launcher instead of exiting the app.
                renpy.__main__.engine_command_completed() if hasattr(renpy.__main__, "engine_command_completed") else None
""",
    )

    # --- 4. split_renpy: ship gui template, sdk fonts and the tutorials as
    #        unpacked private files. The launcher needs them on disk: the
    #        New Project flow reads/copies config.renpy_base/gui directly,
    #        and the font searchpath root is config.renpy_base/sdk-fonts.
    #        (Everything routed to assets/ would be x-prefixed and only
    #        readable through the renpy loader.) ---
    build_py = os.path.join(RAPT, "buildlib", "rapt", "build.py")
    patch(
        build_py,
        """        if fn == "renpy":
            plat.rename(full_fn, os.path.join(private, fn))
            continue
""",
        """        if fn in ("renpy", "gui", "sdk-fonts", "tutorial", "the_question"):
            plat.rename(full_fn, os.path.join(private, fn))
            continue
""",
    )

    # --- 5. keeplist: keep the template/tutorial .rpy sources (and fonts)
    #        that blocklist.txt would strip from the private tar ---
    keeplist = os.path.join(RAPT, "keeplist.txt")
    write(keeplist, "\n".join(["", "gui/**", "tutorial/**", "the_question/**", "sdk-fonts/**", ""]))

    # --- 6. manifest: legacy external storage for Android 10 ---
    manifest = os.path.join(RAPT, "templates", "app-AndroidManifest.xml")
    patch(
        manifest,
        'android:hardwareAccelerated="true">',
        'android:hardwareAccelerated="true"\n      android:requestLegacyExternalStorage="true">',
    )

    # --- 7. launcher: Launch Project runs projects on-device ---
    front_page = os.path.join(SDK, "launcher", "game", "front_page.rpy")
    patch_all(
        front_page,
        "action project.Launch()",
        "action mobile_launch_action()",
    )

    # --- 8. launcher: packaging features that need a PC toolchain notify
    #        and return on-device instead of crashing ---
    android_rpy = os.path.join(SDK, "launcher", "game", "android.rpy")
    patch(
        android_rpy,
        """label android:

    if RAPT_PATH is None:
""",
        """label android:

    if renpy.android:
        $ renpy.notify(_("Packaging Android apps is not available on-device."))
        jump front_page

    if RAPT_PATH is None:
""",
    )
    distribute_rpy = os.path.join(SDK, "launcher", "game", "distribute.rpy")
    patch(
        distribute_rpy,
        """label distribute:

    python hide:
""",
        """label distribute:

    if renpy.android:
        $ renpy.notify(_("Building distributions is not available on-device."))
        jump front_page

    python hide:
""",
    )
    web_rpy = os.path.join(SDK, "launcher", "game", "web.rpy")
    patch(
        web_rpy,
        """label web:

    if WEB_PATH is None:
""",
        """label web:

    if renpy.android:
        $ renpy.notify(_("Packaging web apps is not available on-device."))
        jump front_page

    if WEB_PATH is None:
""",
    )


def prepare_launcher_project():
    """Stage the launcher as a compilable renpy project with our patches."""
    if os.path.isdir(STAGING):
        shutil.rmtree(STAGING)
    os.makedirs(STAGING)
    shutil.copytree(
        os.path.join(SDK, "launcher", "game"),
        os.path.join(STAGING, "game"),
        ignore=shutil.ignore_patterns("__pycache__", "cache"),
    )  # noqa: E501
    shutil.copy(
        os.path.join(os.path.dirname(os.path.abspath(__file__)), "patches", "mobile_support.rpy"),
        os.path.join(STAGING, "game", "zz_mobile_support.rpy"),
    )
    # Bundle the GUI template fonts (gui7 copies fonts relative to the
    # launcher's game directory).
    shutil.copytree(
        os.path.join(SDK, "sdk-fonts"),
        os.path.join(STAGING, "game", "sdk-fonts"),
    )


def compile_launcher():
    env = dict(os.environ)
    env["SDL_VIDEODRIVER"] = "dummy"
    env["SDL_AUDIODRIVER"] = "dummy"
    renpy_sh = os.path.join(SDK, "renpy.sh")
    run([renpy_sh, STAGING, "compile"], env=env)


def make_icons(dist):
    """Generate the icon PNG set expected by rapt.iconmaker."""
    from PIL import Image, ImageDraw, ImageFont

    def background(size):
        img = Image.new("RGB", (size, size))
        top = (40, 22, 82)
        bottom = (232, 93, 61)
        for y in range(size):
            t = y / max(size - 1, 1)
            img.paste(
                tuple(int(top[i] + (bottom[i] - top[i]) * t) for i in range(3)),
                (0, y, size, y + 1),
            )
        return img

    def glyph(size, color, alpha=255):
        layer = Image.new("RGBA", (size, size), (0, 0, 0, 0))
        d = ImageDraw.Draw(layer)
        w = size
        # Play triangle, slightly right of center (optical centering).
        cx, cy = w * 0.54, w * 0.5
        r = w * 0.26
        d.polygon(
            [(cx - r * 0.8, cy - r), (cx - r * 0.8, cy + r), (cx + r * 1.1, cy)],
            fill=color + (alpha,),
        )
        return layer

    def foreground(size):
        return glyph(size, (255, 255, 255))

    def icon(size):
        img = background(size).convert("RGBA")
        img.alpha_composite(glyph(size, (255, 255, 255)))
        return img

    dpis = [
        ("mdpi", 1),
        ("hdpi", 1.5),
        ("xhdpi", 2),
        ("xxhdpi", 3),
        ("xxxhdpi", 4),
    ]
    os.makedirs(dist, exist_ok=True)
    for dpi, scale in dpis:
        icon_img = icon(int(48 * scale))
        icon_img.save(os.path.join(dist, f"android-icon-{dpi}.png"))
        icon_img.save(os.path.join(dist, f"android-icon_round-{dpi}.png"))
        background(int(108 * scale)).save(os.path.join(dist, f"android-icon_background-{dpi}.png"))
        foreground(int(108 * scale)).save(os.path.join(dist, f"android-icon_foreground-{dpi}.png"))
    print("icons written to", dist)


def make_dist():
    if os.path.isdir(DIST):
        shutil.rmtree(DIST)
    os.makedirs(DIST)

    # Compiled launcher game.
    shutil.copytree(os.path.join(STAGING, "game"), os.path.join(DIST, "game"))

    # GUI template, SDK fonts, tutorial and The Question: needed as REAL
    # files on the device (the launcher reads/copies them directly), so
    # the patched split_renpy() routes them into the private tar, which
    # unpacks to renpy_base/ on the device.
    for d in ["gui", "sdk-fonts", "tutorial", "the_question"]:
        shutil.copytree(
            os.path.join(SDK, d),
            os.path.join(DIST, d),
            ignore=shutil.ignore_patterns("__pycache__", "cache"),
        )

    # Engine code (renpy/common is split into assets by rapt.build).
    shutil.copytree(
        os.path.join(SDK, "renpy"),
        os.path.join(DIST, "renpy"),
        ignore=shutil.ignore_patterns("__pycache__"),
    )

    # Pure-python runtime packages. split_renpy() moves lib/ into private/,
    # which unpacks to ANDROID_PRIVATE/lib/python3.12 — a path the embedded
    # runtime puts on sys.path. loader.py imports android.apk and
    # bootstrap.py imports truststore from here on every boot.
    shutil.copytree(
        os.path.join(SDK, "lib", "python3.12"),
        os.path.join(DIST, "lib", "python3.12"),
    )

    # Custom bootstrap; split_renpy() renames root *.py to private/main.py.
    shutil.copy(
        os.path.join(os.path.dirname(os.path.abspath(__file__)), "patches", "main.py"),
        os.path.join(DIST, "main.py"),
    )

    # rapt configuration (read by rapt.configure.Configuration).
    config = {
        "package": PACKAGE,
        "name": APP_NAME,
        "icon_name": ICON_NAME,
        "version": RENPY_VERSION,
        "numeric_version": NUMERIC_VERSION,
        "orientation": "sensorLandscape",
        "permissions": ["VIBRATE", "INTERNET", "MANAGE_EXTERNAL_STORAGE"],
        "include_pil": False,
        "include_sqlite": False,
        "layout": None,
        "source": False,
        "expansion": False,
        "google_play_key": None,
        "google_play_salt": None,
        "store": "none",
        "update_icons": True,
        "update_always": True,
        "heap_size": None,
        "update_keystores": False,
    }
    with open(os.path.join(DIST, "android.json"), "w") as f:
        json.dump(config, f, indent=4, sort_keys=True)

    make_icons(DIST)


def build_apk():
    sys.path.insert(0, os.path.join(RAPT, "buildlib"))
    os.chdir(RAPT)

    import rapt.build as rapt_build  # noqa: E402
    import rapt.interface as rapt_interface  # noqa: E402

    # Materialize the gradle project first, then seed the signing and SDK
    # paths (copy_project preserves local.properties across updates).
    rapt_build.copy_project(False)
    gradlew = os.path.join(RAPT, "project", "gradlew")
    if os.path.exists(gradlew):
        os.chmod(gradlew, 0o755)
    write(os.path.join(RAPT, "sdk.txt"), os.environ.get("ANDROID_HOME", os.environ.get("ANDROID_SDK_ROOT", "")) + "\n")
    write(
        os.path.join(RAPT, "project", "local.properties"),
        "\n".join(
            [
                f"sdk.dir={os.environ.get('ANDROID_HOME', os.environ.get('ANDROID_SDK_ROOT', ''))}",
                f"key.store={KEYSTORE}",
                f"key.store.password={STORE_PASSWORD}",
                f"key.alias={KEY_ALIAS}",
                f"key.alias.password={STORE_PASSWORD}",
                "",
            ]
        ),
    )

    iface = rapt_interface.Interface()
    rapt_build.build(
        iface,
        DIST,
        STAGING,
        bundle=False,
        install=False,
        launch=False,
        permissions=[],
        version=RENPY_VERSION,
    )

    os.makedirs(OUT, exist_ok=True)
    src = os.path.join(RAPT, "bin", f"{PACKAGE}-{RENPY_VERSION}-release.apk")
    dest = os.path.join(OUT, APK_NAME)
    shutil.copy(src, dest)
    print("APK:", dest, os.path.getsize(dest), "bytes")


def main():
    fetch_sources()
    patch_engine_and_rapt()
    prepare_launcher_project()
    compile_launcher()
    make_dist()
    build_apk()
    print("DONE")


if __name__ == "__main__":
    main()
