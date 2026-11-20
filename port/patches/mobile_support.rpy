################################################################################

## Ren'Py Engine on-device development support (v2).
##
## Added to the launcher's game directory when packaging the Ren'Py Engine
## for Android (see port/build_android.py). It provides:
##
##  - "Launch Project" on Android runs the selected project in-process
##    through the custom bootstrap (port/patches/main.py) via a small state
##    file in shared storage.
##  - Launcher commands that the PC launcher runs through a second
##    interpreter (project scans/dumps, lint, force-recompile, gui image
##    generation, translations, ...) are routed through the same mechanism
##    and automatically return to the launcher when done.
##  - A projects directory that works with or without the "All files
##    access" permission: /storage/emulated/0/RenPyProjects when the
##    permission is granted, the app's own external directory otherwise.
##  - A starter project ("Example Game") generated on first run.
##  - A no-op text editor, so "Edit File" explains how to edit instead of
##    crashing (projects are plain files any Android editor app can edit).
##  - An overlay button that opens the "All files access" settings page
##    when the permission has not been granted yet (Android 11+).

init 100 python:
    if renpy.android:
        if not getattr(persistent, "projects_directory", None):
            persistent.projects_directory = "/storage/emulated/0/RenPyProjects"

init python:

    import json
    import os as _ms_os
    import time as _ms_time

    _MS_PRIMARY_PROJECTS = "/storage/emulated/0/RenPyProjects"

    def _ms_state_file():
        return _ms_os.path.join(_ms_os.environ.get("ANDROID_PUBLIC", "/sdcard"), "renpy_engine_state.json")

    def _ms_read_state():
        try:
            with open(_ms_state_file(), "r") as f:
                d = json.load(f)
            if isinstance(d, dict):
                return d
        except Exception:
            pass
        return {}

    def _ms_update_state(**kw):
        state = _ms_read_state()
        state.update(kw)
        try:
            tmp = _ms_state_file() + ".tmp"
            with open(tmp, "w") as f:
                json.dump(state, f)
            _ms_os.replace(tmp, _ms_state_file())
        except Exception as e:
            renpy.notify("Could not write engine state: %s" % e)

    def _ms_all_files_access():
        """True when the engine can freely use shared storage."""
        if not renpy.android:
            return True

        try:
            from jnius import autoclass

            Environment = autoclass("android.os.Environment")
            return bool(Environment.isExternalStorageManager())
        except Exception:
            pass

        # Android < 11 (or jnius unavailable): legacy storage rules apply.
        try:
            pub = _ms_os.environ.get("ANDROID_PUBLIC", "")
            if pub:
                test = _ms_os.path.join(pub, ".ms_write_test")
                with open(test, "w") as f:
                    f.write("ok")
                _ms_os.unlink(test)
                return True
        except Exception:
            pass

        return True

    def _ms_writable(d):
        try:
            if not _ms_os.path.isdir(d):
                _ms_os.makedirs(d, 0o777)
            test = _ms_os.path.join(d, ".ms_write_test")
            with open(test, "w") as f:
                f.write("ok")
            _ms_os.unlink(test)
            return True
        except Exception:
            return False

    def _ms_fallback_projects():
        return _ms_os.path.join(_ms_os.environ.get("ANDROID_PUBLIC", ""), "RenPyProjects")

    def _ms_pick_projects_directory():
        """
        Runs at every launcher start. Keeps persistent.projects_directory
        on shared storage when it is usable, falls back to the app's own
        external directory when "All files access" has not been granted
        yet, and switches back once the grant arrives (unless projects
        were already created in the fallback).
        """
        current = getattr(persistent, "projects_directory", None)

        if current and _ms_writable(current):
            return

        if _ms_writable(_MS_PRIMARY_PROJECTS):
            persistent.projects_directory = _MS_PRIMARY_PROJECTS
            return

        fb = _ms_fallback_projects()
        if _ms_writable(fb):
            persistent.projects_directory = fb

    def _ms_open_storage_settings():
        """Opens the system 'All files access' settings page."""
        if not renpy.android:
            renpy.notify("Desktop build: no storage permission needed.")
            return

        try:
            from jnius import autoclass

            Intent = autoclass("android.content.Intent")
            Settings = autoclass("android.provider.Settings")
            PythonActivity = autoclass("org.renpy.android.PythonSDLActivity")

            intent = Intent(Settings.ACTION_MANAGE_ALL_FILES_ACCESS_PERMISSION)
            PythonActivity.mActivity.startActivity(intent)
        except Exception:
            renpy.notify("Open Settings > Apps > Ren'Py Engine > Permissions.")

    _MS_STARTER_SCRIPT = '''\
define e = Character("Eileen", color="#c8ffc8")

# This is the script of your starter project, generated by the Ren'Py
# Engine port. Edit it with any Android text editor (Acode, QuickEdit,
# Markor, ...), then pick "Launch Project" in the engine again.

label start:

    scene Solid("#40305a")
    with dissolve

    "Welcome to your first Ren'Py game, running entirely on your Android device."

    e "I'm Eileen, your guide."

    menu:

        "What should we do?"

        "Write a story":

            e "Just edit game/script.rpy: every line in quotes is shown to the player."
            e "Statements run from top to bottom. scene, show, menu, label, jump - that is most of it."

        "Make a game":

            e "Ren'Py gives you screens, animations, audio, saves and more out of the box."

    e "This project lives in your projects folder on device storage."

    "Now go and create something!"
'''

    def _ms_ensure_projects_directory():
        """Creates the projects directory and the starter project."""
        if not renpy.android:
            return

        _ms_pick_projects_directory()

        base = persistent.projects_directory

        if not base:
            return

        try:
            starter = _ms_os.path.join(base, "Example Game", "game")
            script = _ms_os.path.join(starter, "script.rpy")

            if _ms_os.path.isdir(base) and not _ms_os.path.exists(script):
                _ms_os.makedirs(starter, 0o777)
                with open(script, "w") as f:
                    f.write(_MS_STARTER_SCRIPT)
        except Exception:
            pass

    _ms_ensure_projects_directory()

    # ------------------------------------------------------------------
    # Crash-proofing of command runs.
    #
    # pending_cmd is set while a headless command (dump/lint/compile/...)
    # is running. The bootstrap clears it when the command completes; if
    # we boot and it is still set, the command died with the process.
    # Count those failures and stop retrying after three, so a broken
    # project cannot turn into an endless restart loop.
    # ------------------------------------------------------------------

    def _ms_boot_command_guard():
        state = _ms_read_state()

        if state.get("pending_cmd"):
            _ms_update_state(pending_cmd=0, cmd_failures=int(state.get("cmd_failures") or 0) + 1)

    if renpy.android:
        _ms_boot_command_guard()

    def _ms_run_project(gamedir, run_args=None, run_env=None):
        """Writes the hand-off state file, then restarts into the project."""
        state = _ms_read_state()

        if run_args and (int(state.get("cmd_failures") or 0) >= 3):
            renpy.notify(_("Engine commands keep failing; scans are disabled for this session."))
            return

        new_state = {
            "run_gamedir": gamedir,
            "run_args": list(run_args) if run_args else None,
            "run_env": {str(k): str(v) for k, v in (run_env or {}).items()},
            "generation": int(_ms_time.time()),
            "pending_cmd": 1 if run_args else 0,
        }

        _ms_update_state(**new_state)

        renpy.utter_restart()

    # ------------------------------------------------------------------
    # Project.launch: the PC launcher spawns a second interpreter, which
    # does not exist on Android. Rebuild the exact command line the PC
    # would use and hand it to the bootstrap's command mode instead.
    # ------------------------------------------------------------------

    _ms_desktop_launch = project.Project.launch

    def _ms_project_launch(self, args=[], wait=False, env={}):
        if not renpy.android:
            return _ms_desktop_launch(self, args=args, wait=wait, env=env)

        state = _ms_read_state()

        if state.get("pending_cmd"):
            renpy.notify(_("A previous engine command is still pending."))
            return

        gamedir = _ms_os.path.join(self.path, "game")

        if not _ms_os.path.isdir(gamedir):
            renpy.notify("%s: not a project." % self.name)
            return

        if not args:
            # Interactive run: make sure the project has a script before
            # restarting into it.
            has_script = False

            try:
                for fn in _ms_os.listdir(gamedir):
                    if fn.endswith(".rpy") or fn.endswith(".rpyc"):
                        has_script = True
                        break
            except Exception:
                pass

            if not has_script:
                renpy.notify("%s: no script found in game/." % self.name)
                return

        # Exactly the command line the desktop launcher would spawn:
        # renpy.sh <project> <args...> --json-dump <file> [--errors-in-editor]
        run_args = [self.path] + [str(i) for i in args]

        run_args.append("--json-dump")
        run_args.append(self.get_dump_filename())

        if persistent.navigate_private:
            run_args.append("--json-dump-private")

        if persistent.navigate_library:
            run_args.append("--json-dump-common")

        run_args.append("--errors-in-editor")

        run_env = {
            "RENPY_LAUNCHER_LANGUAGE": _preferences.language or "english",
        }

        if persistent.skip_splashscreen:
            run_env["RENPY_SKIP_SPLASHSCREEN"] = "1"

        run_env.update(env or {})

        _ms_run_project(gamedir, run_args=run_args, run_env=run_env)

    project.Project.launch = _ms_project_launch

    class MobileLaunch(Action):
        """
        Replaces project.Launch() on the launcher's front page.

        On Android it boots the project in-process through the patched
        Project.launch; on desktop it keeps the original behavior.
        """

        def __call__(self):
            if not renpy.android:
                return project.Launch()()

            project.current.launch()

    def mobile_launch_action():
        if renpy.android:
            return MobileLaunch()
        return project.Launch()

    # ------------------------------------------------------------------
    # No-op text editor. All launcher "Edit File" actions funnel through
    # renpy.editor.editor (begin/open/end); on Android there is no editor
    # process to spawn, so explain where to edit instead of crashing.
    # ------------------------------------------------------------------

    class _MSEditorStub(object):

        def begin(self, *args, **kwargs):
            pass

        def end(self, *args, **kwargs):
            pass

        def open(self, fn, line=0, synchronize=False):
            try:
                rel = _ms_os.path.relpath(fn, persistent.projects_directory)
            except Exception:
                rel = fn
            renpy.notify(_("Open %s in a text editor app (Acode, QuickEdit, ...).") % rel)

    if renpy.android:
        try:
            # NOTE: never use a bare `import renpy.editor` here. Executed in
            # the store, that statement rebinds the store's `renpy` name to
            # the renpy *package* module, destroying the renpy.exports
            # binding that minstore.py sets up (renpy.has_image, renpy.music,
            # renpy.execute_default_statement, ... all disappear) and the
            # launcher dies right after the presplash. import_module()
            # touches no store names.
            import importlib as _ms_importlib

            _ms_renpy_editor = _ms_importlib.import_module("renpy.editor")
            _ms_renpy_editor.editor = _MSEditorStub()
        except Exception:
            pass


screen ms_storage_nag():

    if renpy.android and not _ms_all_files_access():

        frame:
            xpos 0.99
            ypos 0.01
            xanchor 1.0
            ypadding 8
            xpadding 12
            background "#000000b0"

            textbutton _("Grant file access"):
                action Function(_ms_open_storage_settings)
                text_size 20
                text_color "#ffd28c"

init 1000 python:
    if renpy.android:
        config.overlay_screens.append("ms_storage_nag")

        # "Open Directory" buttons on the front page would try to spawn a
        # desktop file manager. Point the user at the folder instead.
        class _MSOpenDirectory(Action):

            def __init__(self, path, absolute=False):
                self.path = path
                self.absolute = absolute

            def get_sensitive(self):
                return True

            def __call__(self):
                renpy.notify(_("Use a file manager app to open: %s") % self.path)

        OpenDirectory = _MSOpenDirectory


# Last-resort store repair. Some init block may have executed a bare
# `import renpy...` statement (any .rpy init code that does so rebinds the
# store's `renpy` name to the renpy package module, hiding renpy.exports).
# Restore the exports binding before the first script statement runs.
init 9999 python:
    try:
        import importlib as _ms_importlib

        _ms_exports = _ms_importlib.import_module("renpy.exports")

        if renpy.store.renpy is not _ms_exports:
            renpy.store.renpy = _ms_exports
    except Exception:
        pass
