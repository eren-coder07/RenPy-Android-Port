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
   - SDK renpy/editor.py             : SystemEditor hands files to the
     launcher's built-in viewer on Android (no editor process).
   - SDK renpy/lint.py               : treat APK-loaded common files as common.
   - SDK renpy/translation/*.py      : no common directory on disk on Android.
   - SDK renpy/loader.py             : skip the packaged game APK asset layer
     when RENPY_PROJECT_GAME_DIR is set (on-device project mode).
   - SDK renpy/bootstrap.py          : engine-session hooks (engine_process_args
     / engine_session_ended, see port/patches/main.py) so projects and the
     launcher features that spawn a second interpreter on the PC (project
     scans, lint, compile, gui images, translations) run in-process and
     always return to the launcher - never exit the app.
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
NUMERIC_VERSION = 80605

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

    # The patches below edit the SDK and RAPT in place, so always start
    # from freshly extracted copies (a second run on a patched tree would
    # fail on the patch anchors). RAPT's gradle project/ and bin/ are
    # kept, so incremental Gradle builds still work.
    if os.path.isdir(SDK):
        shutil.rmtree(SDK)
    run(["tar", "xjf", os.path.join(WORK, "sdk.tar.bz2"), "-C", WORK])

    keep = {}
    if os.path.isdir(RAPT):
        for d in ("project", "bin"):
            src = os.path.join(RAPT, d)
            if os.path.isdir(src):
                keep[d] = os.path.join(WORK, "rapt-keep-" + d)
                if os.path.isdir(keep[d]):
                    shutil.rmtree(keep[d])
                os.rename(src, keep[d])
        shutil.rmtree(RAPT)
    run(["unzip", "-q", os.path.join(WORK, "rapt.zip"), "-d", WORK])
    for d, tmp in keep.items():
        os.rename(tmp, os.path.join(RAPT, d))


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

    # --- 3. bootstrap: engine-session hooks (port/patches/main.py). The
    #        launcher runs projects and project commands in-process; the
    #        bootstrap loop must start queued jobs, re-parse their command
    #        lines, and - whenever a run ends (quit, end of a command,
    #        parse errors, uncaught exception) - go back to the launcher
    #        instead of exiting the app. NOTE: renpy.main.main() never
    #        returns normally (commands and quitting both raise
    #        QuitException), so the hook must run after the except
    #        clauses, not after main(). ---
    bootstrap = os.path.join(SDK, "renpy", "bootstrap.py")
    patch(
        bootstrap,
        "                renpy.game.args = args\n",
        "                renpy.game.args = renpy.__main__.engine_process_args(args) if hasattr(renpy.__main__, \"engine_process_args\") else args\n",
    )
    patch(
        bootstrap,
        "                renpy.config.basedir = basedir\n",
        "                renpy.config.basedir = os.path.abspath(getattr(renpy.game.args, \"basedir\", None) or basedir)\n",
    )
    patch(
        bootstrap,
        """            except renpy.game.UtterRestartException:
                # On an UtterRestart, reload Ren'Py.
                renpy.reload_all()
""",
        """            except renpy.game.UtterRestartException:
                # On an UtterRestart, reload Ren'Py.
                getattr(renpy.__main__, "engine_reload", renpy.reload_all)()
""",
    )
    patch(
        bootstrap,
        """                if e.relaunch:
                    relaunch()
""",
        """                if e.relaunch:
                    if renpy.android:
                        # No second process on Android: restart in-process.
                        getattr(renpy.__main__, "engine_reload", renpy.reload_all)()
                        exit_status = None
                    else:
                        relaunch()
""",
    )
    patch(
        bootstrap,
        """            except Exception as e:
                # If exception was raised outside of Ren'Py execution context,
                # or context was unable to handle it, report it here.
                renpy.error.report_exception(e, editor=True)
""",
        """            except SystemExit as e:
                # Ren'Py Engine port: a project (or an invalid command line,
                # via argparse) called sys.exit(). Only the launcher may
                # end the app.
                if getattr(renpy.__main__, "engine_mode", lambda: "launcher")() != "project":
                    raise

                exit_status = e.code if isinstance(e.code, int) else 1

            except Exception as e:
                # If exception was raised outside of Ren'Py execution context,
                # or context was unable to handle it, report it here.
                renpy.error.report_exception(e, editor=True)

            # Ren'Py Engine port: a project run or project command ended -
            # continue with the next queued job or the launcher instead of
            # exiting the app.
            if (exit_status is not None) and hasattr(renpy.__main__, "engine_session_ended"):
                if renpy.__main__.engine_session_ended(exit_status):
                    # (engine_reload: reload_all() that survives runs that
                    # never opened the display.)
                    renpy.__main__.engine_reload()
                    exit_status = None
""",
    )

    patch(
        bootstrap,
        """        if renpy.android:
            from jnius import autoclass  # type: ignore

            import android

            android.activity.finishAndRemoveTask()
""",
        """        if renpy.android:
            # Ren'Py Engine port: System.exit() below ends the process at
            # once; dump an exception that is on its way out first.
            if hasattr(renpy.__main__, "engine_before_exit"):
                renpy.__main__.engine_before_exit()

            from jnius import autoclass  # type: ignore

            import android

            android.activity.finishAndRemoveTask()
""",
    )

    # --- 3b. editor: there is no editor process to spawn on Android. The
    #         SystemEditor hands the files to the bootstrap, which forwards
    #         them to the launcher's built-in file viewer. ---
    editor_py = os.path.join(SDK, "renpy", "editor.py")
    patch(
        editor_py,
        """class SystemEditor(Editor):
    def open(self, filename, line=None, **kwargs):
        try:
""",
        """class SystemEditor(Editor):
    # Ren'Py Engine port: on Android, collect the files of a transaction
    # and let the launcher show them (port/patches/main.py).
    def begin(self, new_window=False, **kwargs):
        self.android_files = []

    def end(self, **kwargs):
        files = getattr(self, "android_files", [])
        self.android_files = []

        hook = getattr(renpy.__main__, "engine_open_files", None)

        if files and (hook is not None):
            hook(files)

    def open(self, filename, line=None, **kwargs):
        if renpy.android:
            if not hasattr(self, "android_files"):
                self.android_files = []

            self.android_files.append((filename, line))
            return

        try:
""",
    )

    # --- 3c. lint: on Android the common files are read from the APK, and
    #         their nodes are named "00db.rpy" instead of
    #         "renpy/common/00db.rpy", so lint reported problems in Ren'Py's
    #         own files. Project files are always "game/...". ---
    lint_py = os.path.join(SDK, "renpy", "lint.py")
    patch(
        lint_py,
        """    if filename.startswith("common/") or filename.startswith("renpy/common/"):
        return True
""",
        """    if filename.startswith("common/") or filename.startswith("renpy/common/"):
        return True
    elif renpy.android and not filename.startswith("/") and not filename.startswith("game/"):
        return True
""",
    )

    # --- 3d. translations: config.commondir is None on Android (the common
    #         files are in the APK), and these normpath(None) calls crashed
    #         the translate / dialogue commands (Generate Translations,
    #         Extract Dialogue, and the string step of New Project). ---
    generation_py = os.path.join(SDK, "renpy", "translation", "generation.py")
    patch(
        generation_py,
        """    commondir = os.path.normpath(renpy.config.commondir)  # type: ignore
    gamedir = os.path.normpath(renpy.config.gamedir)

    if filename.startswith(commondir):
""",
        """    commondir = os.path.normpath(renpy.config.commondir) if renpy.config.commondir else None  # type: ignore
    gamedir = os.path.normpath(renpy.config.gamedir)

    if commondir and filename.startswith(commondir):
""",
    )
    dialogue_py = os.path.join(SDK, "renpy", "translation", "dialogue.py")
    patch(
        dialogue_py,
        """        commondir = os.path.normpath(renpy.config.commondir)  # type: ignore

        if filename.startswith(commondir):
            return
""",
        """        commondir = os.path.normpath(renpy.config.commondir) if renpy.config.commondir else None  # type: ignore

        if commondir and filename.startswith(commondir):
            return
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
        """        if fn in ("renpy", "gui", "sdk-fonts", "tutorial", "the_question", "launcher"):
            plat.rename(full_fn, os.path.join(private, fn))
            continue
""",
    )

    # --- 5. keeplist: keep the template/tutorial .rpy sources (and fonts)
    #        that blocklist.txt would strip from the private tar ---
    keeplist = os.path.join(RAPT, "keeplist.txt")
    write(keeplist, "\n".join(["", "gui/**", "tutorial/**", "the_question/**", "sdk-fonts/**", "launcher/**", ""]))

    # --- 5b. New Project: the template's translated code is looked up in
    #         the launcher's game directory, which on Android is inside the
    #         APK. Fall back to the on-disk copy shipped by make_dist(). ---
    gui7_code = os.path.join(SDK, "launcher", "game", "gui7", "code.py")
    patch(
        gui7_code,
        """        src = os.path.join(renpy.config.gamedir, "tl", language, fn + "m")

        if not os.path.exists(src):
""",
        """        src = os.path.join(renpy.config.gamedir, "tl", language, fn + "m")

        if not os.path.exists(src):
            # Ren'Py Engine port: on-disk copy (the launcher game is in the APK).
            src = os.path.join(renpy.config.renpy_base, "launcher", "game", "tl", language, fn + "m")

        if not os.path.exists(src):
""",
    )

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

    # Launcher files that New Project reads from disk (relative to
    # renpy_base) rather than through the loader: the icon template and
    # the translated gui template code. The launcher game itself is in the
    # APK, so ship copies as real files (routed to the private tar).
    launcher_disk = os.path.join(DIST, "launcher", "game")
    os.makedirs(os.path.join(launcher_disk, "gui7"))
    # project.json marks the directory "hidden" in the project list.
    shutil.copy(os.path.join(SDK, "launcher", "project.json"), os.path.join(DIST, "launcher", "project.json"))
    shutil.copy(os.path.join(STAGING, "game", "gui7", "icon.png"), os.path.join(launcher_disk, "gui7", "icon.png"))
    tl = os.path.join(STAGING, "game", "tl")
    for lang in sorted(os.listdir(tl)):
        if not os.path.isdir(os.path.join(tl, lang)):
            continue
        for fn in os.listdir(os.path.join(tl, lang)):
            if fn.endswith(".rpym"):
                os.makedirs(os.path.join(launcher_disk, "tl", lang), exist_ok=True)
                shutil.copy(os.path.join(tl, lang, fn), os.path.join(launcher_disk, "tl", lang, fn))

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
    # --stage-only: patch, compile the launcher and assemble android.dist,
    # but skip Gradle (used by port/sim/simulate_device.py).
    stage_only = "--stage-only" in sys.argv[1:]

    fetch_sources()
    patch_engine_and_rapt()
    prepare_launcher_project()
    compile_launcher()
    make_dist()

    if not stage_only:
        build_apk()

    print("DONE")


if __name__ == "__main__":
    main()
