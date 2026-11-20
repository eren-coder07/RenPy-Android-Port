#!/usr/bin/env python
# Ren'Py Engine for Android - custom on-device bootstrap (v3).
#
# This file replaces the SDK's renpy.py as the root bootstrap of the
# android package (rapt's split_renpy() renames root *.py to
# private/main.py, which librenpython's start.c executes).
#
# It is derived from Ren'Py's renpy.py (MIT license, Copyright
# 2004-2026 Tom Rothamel) with an added layer that lets the Ren'Py
# launcher run user projects - and the launcher's project commands - on
# the device, where no second interpreter process can be spawned.
#
# How it works
# ------------
# The launcher (game/zz_mobile_support.rpy) hands a list of *jobs* to
# engine_submit() and calls renpy.utter_restart(). Each job is one
# "renpy.sh <project> <args...>" invocation: an interactive run of a
# project, or a headless command (json-dump scan, lint, compile,
# gui_images, translate, rmpersistent, ...).
#
# The (patched) bootstrap loop in renpy/bootstrap.py then:
#
#   path_to_gamedir()       starts the next queued job, if any: points the
#                           engine at the project's game directory and
#                           switches sys.argv to the job's command line.
#   engine_process_args()   re-parses the arguments for the job.
#   engine_session_ended()  is called whenever a run ends - normal end,
#                           quit, parse errors or an uncaught exception.
#                           It records the job's result and makes the loop
#                           continue with the next job, or with the
#                           launcher again, instead of exiting the app.
#
# All of this lives in this module's memory: the module survives
# renpy.utter_restart() (renpy.reload_all() only restores renpy.* modules),
# while a new process (the user reopening the app, or the app having been
# killed) always starts in the launcher. There is no state file that could
# go stale and boot a broken project in a loop.

from __future__ import print_function, absolute_import

import os
import sys
import time
import warnings

# Engine-session controller ##################################################

# Jobs waiting to run. Each job is a dict:
#   gamedir   the project's game directory
#   args      the command line after the program name (project path first)
#   env       extra environment variables for the run
#   label     launcher label to return to once the whole batch is done
_queue = []

# The job being run right now, or None in launcher mode.
_current = None

# Results of finished jobs, oldest first, for the launcher to inspect.
_results = []

# Launcher label to start at after the current batch, or None.
_resume_label = None

# The launcher's renpy.session, kept aside while a project runs.
_launcher_session = None

# Environment variables to put back when the current job ends: the job's
# own variables, and the SDL drivers - renpy.arguments.post_init() sets
# SDL_VIDEODRIVER=dummy for headless commands, which must not leak into
# the launcher (it could not open its window again).
_saved_env = {}
_RESTORED_ENV = ("SDL_VIDEODRIVER", "SDL_AUDIODRIVER", "RENPY_PROJECT_GAME_DIR")

# The process' original command line and parsed arguments.
_original_argv = list(sys.argv)

# Set when a job is started; used by the save/log path functions below.
_project_gamedir = None


# Added to interactive project runs (never to headless commands, so lint,
# translate and dumps don't see it): a small button that returns to the
# launcher. Ren'Py's standard GUI hides "Quit" on mobile, and a project
# without a main menu would otherwise loop forever with no way back.
_OVERLAY_RPY = u'''\
# Ren'Py Engine for Android: added to projects run from the on-device
# launcher. It is not part of your project and is not copied into it.

init 1999 python:
    # Shown everywhere, the main and game menus included.
    config.always_shown_screens.append("_renpy_engine_exit")

screen _renpy_engine_exit():
    zorder 1999

    textbutton "< Launcher":
        xpos 4
        ypos 4
        xpadding 10
        ypadding 4
        background "#00000080"
        hover_background "#000000c0"
        text_size max(14, config.screen_height // 40)
        text_color "#ffffffc0"
        text_hover_color "#ffffff"
        action Show("_renpy_engine_exit_confirm")

screen _renpy_engine_exit_confirm():
    modal True
    zorder 2000

    add "#000000b0"

    frame:
        align (0.5, 0.5)
        background "#202028f0"
        xpadding 40
        ypadding 30

        vbox:
            spacing 30

            text "Return to the Ren'Py launcher?":
                size max(16, config.screen_height // 28)
                color "#ffffff"
                xalign 0.5

            hbox:
                xalign 0.5
                spacing 80

                textbutton "Yes":
                    text_size max(16, config.screen_height // 28)
                    text_color "#ffd28c"
                    text_hover_color "#ffffff"
                    action Quit(confirm=False)

                textbutton "No":
                    text_size max(16, config.screen_height // 28)
                    text_color "#ffffff"
                    text_hover_color "#ffd28c"
                    action Hide("_renpy_engine_exit_confirm")

    key "game_menu" action Hide("_renpy_engine_exit_confirm")
'''


def _overlay_dir():
    """
    Writes the overlay script (above) into the engine's private directory,
    and returns that directory.
    """

    d = os.path.join(os.environ.get("ANDROID_PRIVATE") or path_to_renpy_base(), "engine_overlay")
    fn = os.path.join(d, "_renpy_engine_overlay.rpy")

    try:
        with open(fn, "r") as f:
            if f.read() == _OVERLAY_RPY:
                return d
    except Exception:
        pass

    if not os.path.isdir(d):
        os.makedirs(d)

    with open(fn, "w") as f:
        f.write(_OVERLAY_RPY)

    return d


def engine_submit(jobs, resume_label=None):
    """
    Called by the launcher: queue `jobs` (see above), to be run after the
    launcher calls renpy.utter_restart(). When all of them are finished,
    the launcher is started again, at `resume_label` if given.
    """

    global _resume_label

    for j in jobs:
        _queue.append(
            {
                "gamedir": str(j["gamedir"]),
                "args": [str(i) for i in (j.get("args") or [])],
                "env": {str(k): str(v) for k, v in (j.get("env") or {}).items()},
                "key": j.get("key"),
                "interactive": bool(j.get("interactive")),
            }
        )

    _resume_label = resume_label


def engine_take_results():
    """
    Called by the launcher: returns (and forgets) the results of the jobs
    that finished since the last call.
    """

    rv = list(_results)
    del _results[:]
    return rv


# Called by the launcher with a function that shows files (the Android
# replacement of a text editor). Registered again at each launcher start.
_open_files_handler = None


def engine_set_open_files_handler(handler):
    global _open_files_handler
    _open_files_handler = handler


def engine_open_files(files):
    """
    Called by renpy.editor.SystemEditor (patched by build_android.py) with
    a list of (filename, line) pairs. Only the launcher shows them; a
    running project (--errors-in-editor) has no editor to open.
    """

    if _current is None and _open_files_handler is not None:
        _open_files_handler(files)


def engine_mode():
    """
    "launcher", or "project" while a job is running.
    """

    return "project" if _current is not None else "launcher"


def _error_files(job):
    basedir = os.path.dirname(job["gamedir"])
    return [os.path.join(d, fn) for fn in ("errors.txt", "traceback.txt") for d in (basedir, job["gamedir"])]


def _mtime(fn):
    try:
        return os.path.getmtime(fn)
    except Exception:
        return None


def _start_next_job():
    """
    Pops the next job off the queue and makes it current.
    """

    global _current, _project_gamedir, _launcher_session

    import renpy  # @UnresolvedImport

    job = _queue.pop(0)
    job["start"] = time.time()

    # Commands may redirect the standard streams (lint points sys.stdout
    # at its report file and never puts it back - on a PC the process
    # simply exits). Put them back when the job ends.
    job["streams"] = (sys.stdout, sys.stderr)
    job["error_mtimes"] = dict((fn, _mtime(fn)) for fn in _error_files(job))

    _current = job
    _project_gamedir = job["gamedir"]

    _clear_testcases()

    # The project must not see (or clobber) the launcher's session.
    if _launcher_session is None:
        _launcher_session = renpy.session
    renpy.session = {}

    _saved_env.clear()
    for k in list(_RESTORED_ENV) + list(job["env"]):
        _saved_env[k] = os.environ.get(k)

    os.environ["RENPY_PROJECT_GAME_DIR"] = job["gamedir"]

    for k, v in job["env"].items():
        os.environ[k] = v

    sys.argv = [_original_argv[0]] + list(job["args"])

    print("Ren'Py Engine: running", job["gamedir"], job["args"])


def _finish_job(status):
    """
    Records the result of the current job and returns to launcher mode.
    """

    global _current, _project_gamedir, _launcher_session

    import renpy  # @UnresolvedImport

    job = _current

    for current, original, name in zip((sys.stdout, sys.stderr), job["streams"], ("stdout", "stderr")):
        if current is not original:
            try:
                current.flush()
                current.close()
            except Exception:
                pass
            setattr(sys, name, original)

    # Ren'Py writes traceback.txt / errors.txt next to the game (the
    # logdir); only report the ones this run wrote.
    errors = [
        fn for fn in _error_files(job)
        if _mtime(fn) is not None and _mtime(fn) != job["error_mtimes"].get(fn)
    ]

    _results.append(
        {
            "gamedir": job["gamedir"],
            "args": job["args"],
            "key": job.get("key"),
            "status": status,
            "errors": errors,
            "seconds": time.time() - job["start"],
        }
    )

    _current = None
    _project_gamedir = None

    _clear_testcases()

    for k, v in _saved_env.items():
        if v is None:
            os.environ.pop(k, None)
        else:
            os.environ[k] = v
    _saved_env.clear()

    sys.argv = list(_original_argv)

    if _launcher_session is not None:
        renpy.session = _launcher_session
        _launcher_session = None


def _write_crash_dump(text):
    """
    Writes `text` to renpy_crash.txt in the app's public directory and in
    the projects directory, where it can be read without a PC.
    """

    for d in [
        os.environ.get("ANDROID_PUBLIC", ""),
        os.path.join(shared_storage(), "RenPyProjects"),
    ]:
        try:
            if d and os.path.isdir(d):
                with open(os.path.join(d, "renpy_crash.txt"), "w") as f:
                    f.write(text)
        except Exception:
            pass


def engine_before_exit():
    """
    Called by the patched bootstrap right before Android's System.exit(),
    which ends the process without unwinding: an exception that is on its
    way out would never reach main()'s handler, so dump it here.
    """

    import traceback

    et, ev, tb = sys.exc_info()

    if et is None or issubclass(et, SystemExit):
        return

    text = "".join(traceback.format_exception(et, ev, tb))
    print(text, file=sys.stderr)
    _write_crash_dump(text)


class _NoInterface(object):
    """Stands in for a display interface that was never created."""

    def kill_textures(self):
        pass

    def kill_surfaces(self):
        pass


def engine_reload():
    """
    renpy.reload_all() for switching between the launcher and projects.

    - The renderer is kept: reload_all() would otherwise quit it, closing
      the window - on Android the window belongs to the activity, and
      re-creating it is the fragile part. The next run's set_mode() then
      re-initializes the same renderer at its own screen size (the path
      Ren'Py uses for display resets), since _keep_renderer is only set
      during the reload.
    - It also survives runs that never opened the display: a headless
      command has no renderer, and a project that failed while loading its
      script has no interface either.
    """

    import renpy  # @UnresolvedImport

    session = renpy.session
    had_keep = "_keep_renderer" in session
    old_keep = session.get("_keep_renderer")

    no_interface = renpy.display.interface is None

    session["_keep_renderer"] = True

    if no_interface:
        renpy.display.interface = _NoInterface()

    try:
        renpy.reload_all()
    finally:
        if had_keep:
            session["_keep_renderer"] = old_keep
        else:
            session.pop("_keep_renderer", None)

        if isinstance(renpy.display.interface, _NoInterface):
            renpy.display.interface = None


def _clear_testcases():
    """
    Ren'Py's testcase registry survives reloads (it expects the same game
    to be reloaded); the launcher and each project have their own.
    """

    try:
        import renpy.test.testexecution  # @UnresolvedImport

        renpy.test.testexecution.testcases.clear()
    except Exception:
        pass


def engine_session_ended(exit_status):
    """
    Called by the patched bootstrap loop each time a run of the engine ends
    without an utter restart (a quit, the end of a command, parse errors,
    an uncaught exception). Returns True if the loop should go on - back to
    the launcher or on to the next job - or False to let the app exit.
    """

    global _resume_label, _current

    if _current is None and not _queue:
        # The launcher itself quit: exit the app like the stock engine.
        return False

    if _current is not None:
        try:
            _finish_job(exit_status)
        except Exception:
            # Never let bookkeeping take the app down: go back to the
            # launcher regardless.
            import traceback

            traceback.print_exc()
            _current = None

    if not _queue:
        import renpy  # @UnresolvedImport

        if _resume_label:
            renpy.session["launcher_start_label"] = _resume_label
        _resume_label = None

    return True


def engine_process_args(default_args):
    """
    Called by the patched bootstrap loop in place of reusing the arguments
    parsed at process start. While a job runs, sys.argv holds its command
    line; parse it with renpy's real argument parser, exactly like
    `renpy.sh <project> <command>` on a PC.
    """

    if _current is None:
        return default_args

    # NOTE: this module has no global `renpy`; import it here.
    import renpy.arguments  # @UnresolvedImport

    try:
        return renpy.arguments.bootstrap()
    except SystemExit as e:
        # argparse exits on bad arguments - never let that kill the app.
        raise Exception("Invalid engine command line %r (%s)" % (sys.argv[1:], e))


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

    The bootstrap loop calls this once per run. If jobs are queued and none
    is running, the next one starts here and its project's game directory
    is returned. RENPY_PROJECT_GAME_DIR is exported for the patched engine
    loader (it must not merge the packaged launcher assets).
    """

    if _current is None and _queue:
        _start_next_job()

    if _current is not None:
        return _current["gamedir"]

    return _launcher_gamedir(basedir, name)


def shared_storage():
    """
    The root of the device's shared storage (overridable for the desktop
    simulator in port/sim).
    """

    return os.environ.get("RENPY_ENGINE_SHARED_STORAGE", "/storage/emulated/0")


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

        # Interactive run: the "< Launcher" button.
        if _current is not None and _current.get("interactive"):
            try:
                searchpath.append(_overlay_dir())
            except Exception as e:
                print("Ren'Py Engine: could not add the launcher button:", e)

    if commondir and os.path.isdir(commondir):
        searchpath.append(commondir)

    if renpy.android or renpy.ios:
        print("Mobile search paths:", " ".join(searchpath))

    return searchpath


##############################################################################


android = "ANDROID_PRIVATE" in os.environ


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
        _write_crash_dump(tb)
        raise


if __name__ == "__main__":
    main()
