#!/usr/bin/env python
# Ren'Py Engine for Android - custom on-device bootstrap (v2).
#
# This file replaces the SDK's renpy.py as the root bootstrap of the
# android package (rapt's split_renpy() renames root *.py to
# private/main.py, which librenpython's start.c executes).
#
# It is derived from Ren'Py's renpy.py (MIT license, Copyright
# 2004-2026 Tom Rothamel) with an added layer that lets the Ren'Py
# launcher run user projects on the device:
#
#   launcher "Launch Project"  ->  writes a state file into
#   ANDROID_PUBLIC             ->  renpy.utter_restart()
#                              ->  bootstrap loop re-runs path_to_gamedir()
#                              ->  this module points the engine at the
#                                  user project's game directory and sets
#                                  RENPY_PROJECT_GAME_DIR so the patched
#                                  engine loader skips the packaged game
#                                  asset layer.
#
# v2 adds *command mode*: launcher features that on the PC spawn a
# second interpreter (project scan dumps, lint, compile, gui image
# generation, translations, ...) instead re-run renpy in-process with
# the command's argv, then automatically boot the launcher again. The
# bootstrap.py hooks (engine_process_args / engine_command_completed)
# are installed by port/build_android.py.
#
# Each process start reads the generation counter once; the packaged
# launcher boots on the first run and whenever the generation has not
# advanced, so quitting a project (or killing the app) returns the user
# to the launcher on next start.

from __future__ import print_function, absolute_import

import json
import os
import sys
import warnings

# Functions to be customized by distributors. ################################


def _public_dir():
    return os.environ.get("ANDROID_PUBLIC") or "."


def _state_file():
    return os.path.join(_public_dir(), "renpy_engine_state.json")


def _read_state():
    try:
        with open(_state_file(), "r") as f:
            d = json.load(f)
        if isinstance(d, dict):
            return d
    except Exception:
        pass
    return {}


def _update_state(**kw):
    """Atomically merges fields into the hand-off state file."""
    state = _read_state()
    state.update(kw)
    try:
        tmp = _state_file() + ".tmp"
        with open(tmp, "w") as f:
            json.dump(state, f)
        try:
            os.chmod(tmp, 0o666)
        except Exception:
            pass
        os.replace(tmp, _state_file())
    except Exception:
        pass


# Snapshot of the generation counter at process start. A "Launch Project"
# tap writes a NEW generation from the launcher; while it is greater than
# the snapshot, the bootstrap loop boots that project instead of the
# packaged launcher.
_START_GENERATION = _read_state().get("generation") or 0

# Command line (after argv[0]) of the headless command currently being
# run, or None when not in command mode. Used by engine_process_args().
_engine_command_args = None


def _launcher_gamedir(basedir, name):
    """
    Returns the gamedir of the packaged launcher, using the stock
    renpy.py candidate logic.
    """
    candidates = [name]

    game_name = name

    while game_name:
        prefix = game_name[0]
        game_name = game_name[1:]

        if prefix == " " or prefix == "_":
            candidates.append(game_name)

    candidates.extend(["game", "data", "launcher/game"])

    for i in candidates:
        if i == "renpy":
            continue

        gamedir = os.path.join(basedir, i)

        if os.path.isdir(gamedir):
            break

    else:
        gamedir = basedir

    return gamedir


def path_to_gamedir(basedir, name):
    """
    Returns the absolute path to the directory containing the game
    scripts and assets. (This becomes config.gamedir.)

    In project mode, the project's game directory on the device
    filesystem is returned, and RENPY_PROJECT_GAME_DIR is exported for
    the patched engine loader (it must not merge the packaged game
    assets while a user project is running).
    """
    global _project_gamedir, _engine_command_args, _START_GENERATION

    state = _read_state()
    target = state.get("run_gamedir")
    generation = state.get("generation") or 0

    # A completed command run hands control back to the launcher.
    if state.get("return_to_launcher"):
        _update_state(
            return_to_launcher=0,
            pending_cmd=0,
            run_gamedir=None,
            run_args=None,
            run_env=None,
        )
        # Forget the last launch so a stray utter_restart (language
        # change, ...) stays in launcher mode.
        _START_GENERATION = max(_START_GENERATION, generation)
        _project_gamedir = None
        _engine_command_args = None
        os.environ.pop("RENPY_PROJECT_GAME_DIR", None)
        os.environ.pop("RENPY_ENGINE_COMMAND_MODE", None)
        sys.argv = [sys.argv[0]]
        return _launcher_gamedir(basedir, name)

    if (
        _project_gamedir is None
        and "ANDROID_PRIVATE" in os.environ
        and target
        and generation > _START_GENERATION
        and os.path.isdir(target)
    ):
        _project_gamedir = target
        os.environ["RENPY_PROJECT_GAME_DIR"] = target

        args = state.get("run_args") or []
        if args:
            # Command mode: json-dump / lint / compile / gui_images / ...
            _engine_command_args = [str(i) for i in args]
            os.environ["RENPY_ENGINE_COMMAND_MODE"] = "1"
            sys.argv = [sys.argv[0]] + _engine_command_args

        for k, v in (state.get("run_env") or {}).items():
            os.environ[str(k)] = str(v)

        print("Ren'Py Engine: running project", target, args if args else "(interactive)")
    elif _project_gamedir is None:
        # Launcher mode: make sure no stale project mode is active.
        os.environ.pop("RENPY_PROJECT_GAME_DIR", None)

    if _project_gamedir is not None:
        return _project_gamedir

    return _launcher_gamedir(basedir, name)


def engine_process_args(default_args):
    """
    Called by the patched bootstrap loop (renpy/bootstrap.py) instead of
    reusing the process-start argument parse.

    In command mode, sys.argv has already been switched to the command
    line by path_to_gamedir(); re-parse it with renpy's real argument
    parser so headless commands work exactly like
    `renpy.sh <project> <command>` does on the PC.
    """
    global _engine_command_args

    if _engine_command_args:
        return renpy.arguments.bootstrap()

    return default_args


def engine_command_completed():
    """
    Called by the patched bootstrap loop right after renpy.main.main()
    returns normally.

    In command mode the command's output (json dump, lint.txt, generated
    images, ...) has been written; hand control back to the launcher by
    re-raising UtterRestartException, which the bootstrap loop catches
    to boot the launcher again.
    """
    global _engine_command_args

    if os.environ.pop("RENPY_ENGINE_COMMAND_MODE", None):
        _engine_command_args = None
        sys.argv = [sys.argv[0]]
        _update_state(return_to_launcher=1, pending_cmd=0, cmd_failures=0)

        import renpy.game  # @UnresolvedImport

        raise renpy.game.UtterRestartException()


def path_to_common(renpy_base):
    """
    Returns the absolute path to the Ren'Py common directory.
    """
    path = renpy_base + "/renpy/common"

    if os.path.isdir(path):
        return path
    return None


def path_to_saves(gamedir, save_directory=None):  # type: (str, str|None) -> str
    """
    Given the path to a Ren'Py game directory and the value of config.
    save_directory, returns the absolute path to the directory where save
    files will be placed.
    """
    import renpy  # @UnresolvedImport

    # Project mode: keep saves inside the project, like on the PC.
    if _project_gamedir is not None and gamedir == _project_gamedir:
        saves = os.path.join(gamedir, "saves")
        try:
            if not os.path.isdir(saves):
                os.makedirs(saves, 0o777)
            return saves
        except Exception:
            pass

    if save_directory is None:
        save_directory = renpy.config.save_directory

    # Makes sure the permissions are right on the save directory.
    def test_writable(d):
        try:
            fn = os.path.join(d, "test.txt")
            open(fn, "w").close()
            open(fn, "r").close()
            os.unlink(fn)
            return True
        except Exception:
            return False

    # Android.
    if renpy.android:
        paths = [
            os.path.join(os.environ["ANDROID_OLD_PUBLIC"], "game/saves"),
            os.path.join(os.environ["ANDROID_PRIVATE"], "saves"),
            os.path.join(os.environ["ANDROID_PUBLIC"], "saves"),
        ]

        for rv in paths:
            if os.path.isdir(rv) and test_writable(rv):
                break
        else:
            rv = paths[-1]

        print("Saving to", rv)
        return rv

    if renpy.ios:
        from pyobjus import autoclass  # type: ignore
        from pyobjus.objc_py_types import enum  # type: ignore

        NSSearchPathDirectory = enum("NSSearchPathDirectory", NSDocumentDirectory=9)
        NSSearchPathDomainMask = enum("NSSearchPathDomainMask", NSUserDomainMask=1)

        NSFileManager = autoclass("NSFileManager")
        manager = NSFileManager.defaultManager()
        url = manager.URLsForDirectory_inDomains_(
            NSSearchPathDirectory.NSDocumentDirectory,
            NSSearchPathDomainMask.NSUserDomainMask,
        ).lastObject()

        # url.path seems to change type based on Android version, for some reason.
        try:
            rv = url.path().UTF8String()
        except Exception:
            rv = url.path.UTF8String()

        if isinstance(rv, bytes):
            rv = rv.decode("utf-8")

        print("Saving to", rv)
        return rv

    # No save directory given.
    if not save_directory:
        return os.path.join(gamedir, "saves")

    if "RENPY_PATH_TO_SAVES" in os.environ:
        return os.environ["RENPY_PATH_TO_SAVES"] + "/" + save_directory

    # Search the path above Ren'Py for a directory named "Ren'Py Data".
    # If it exists, then use that for our save directory.
    path = renpy.config.renpy_base

    while True:
        if os.path.isdir(path + "/Ren'Py Data"):
            return path + "/Ren'Py Data/" + save_directory

        newpath = os.path.dirname(path)
        if path == newpath:
            break
        path = newpath

    # Otherwise, put the saves in a platform-specific location.
    if renpy.macintosh:
        rv = "~/Library/RenPy/" + save_directory
        return os.path.expanduser(rv)

    elif renpy.windows:
        if "APPDATA" in os.environ:
            return os.environ["APPDATA"] + "/RenPy/" + save_directory
        else:
            rv = "~/RenPy/" + renpy.config.save_directory  # type: ignore
            return os.path.expanduser(rv)

    else:
        rv = "~/.renpy/" + save_directory
        return os.path.expanduser(rv)


# Returns the path to the Ren'Py base directory (containing common and
# the launcher, usually.)
def path_to_renpy_base():
    """
    Returns the absolute path to the Ren'Py base directory.
    """

    renpy_base = os.path.dirname(os.path.abspath(__file__))
    renpy_base = os.path.abspath(renpy_base)

    return renpy_base


def path_to_logdir(basedir):
    """
    Returns the absolute path to the directory where Ren'Py will write
    log and error files.
    """
    import renpy  # @UnresolvedImport

    if renpy.android:
        # Project mode: keep log.txt inside the project, like on the PC.
        if _project_gamedir is not None:
            return os.path.dirname(_project_gamedir)
        return os.environ["ANDROID_PUBLIC"]

    return basedir


def predefined_searchpath(commondir):
    import renpy  # @UnresolvedImport

    # The default gamedir, in private.
    searchpath = [renpy.config.gamedir]

    if renpy.android and _project_gamedir is None:
        # Launcher mode: the public android directory can override or add
        # game files.
        if "ANDROID_PUBLIC" in os.environ:
            android_game = os.path.join(os.environ["ANDROID_PUBLIC"], "game")

            if os.path.exists(android_game):
                searchpath.insert(0, android_game)

        # Asset packs.
        packs = [
            "ANDROID_PACK_FF1",
            "ANDROID_PACK_FF2",
            "ANDROID_PACK_FF3",
            "ANDROID_PACK_FF4",
        ]

        for i in packs:
            if i not in os.environ:
                continue

            assets = os.environ[i]

            for i in ["renpy/common", "game"]:
                dn = os.path.join(assets, i)
                if os.path.isdir(dn):
                    searchpath.append(dn)
    else:
        # Project mode: search paths from the environment, if any.
        if "RENPY_SEARCHPATH" in os.environ:
            searchpath.extend(os.environ["RENPY_SEARCHPATH"].split("::"))

    if commondir and os.path.isdir(commondir):
        searchpath.append(commondir)

    if renpy.android or renpy.ios:
        print("Mobile search paths:", " ".join(searchpath))

    return searchpath


##############################################################################


android = "ANDROID_PRIVATE" in os.environ

# Resolved on first bootstrap-loop iteration; also used by the save/log
# path functions above.
_project_gamedir = None


def main():

    renpy_base = path_to_renpy_base()

    sys.path.append(renpy_base)

    # Ignore warnings.
    warnings.simplefilter("ignore", DeprecationWarning)

    # Start Ren'Py proper.
    try:
        import renpy.bootstrap
    except ImportError:
        print("Could not import renpy.bootstrap. Please ensure you decompressed Ren'Py", file=sys.stderr)
        print("correctly, preserving the directory structure.", file=sys.stderr)
        raise

    # Set renpy.__main__ to this module.
    renpy.__main__ = sys.modules[__name__]  # type: ignore

    try:
        renpy.bootstrap.bootstrap(renpy_base)
    except SystemExit:
        raise
    except BaseException:
        # Crash insurance: dump the full traceback where it can be
        # retrieved from the device, then let the app die normally
        # (the Android presplash goes away with it).
        import traceback

        tb = traceback.format_exc()
        print(tb, file=sys.stderr)
        for d in [
            os.environ.get("ANDROID_PUBLIC", ""),
            "/storage/emulated/0/RenPyProjects",
        ]:
            try:
                with open(os.path.join(d, "renpy_crash.txt"), "w") as f:
                    f.write(tb)
            except Exception:
                pass
        raise


if __name__ == "__main__":
    main()
