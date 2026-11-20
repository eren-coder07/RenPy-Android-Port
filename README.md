# Ren'Py Engine for Android (on-device development)

**Port the engine itself, so you can create real games on Android like the
Ren'Py PC version.**

This repository builds a universal Android APK (armeabi-v7a + arm64-v8a +
x86_64) that contains the full Ren'Py launcher and engine (Ren'Py 8.6.1).
On the device it behaves like the desktop SDK:

- **Create projects** with the standard launcher (GUI templates included).
- **Edit scripts** with any Android text editor (Acode, QuickEdit, Markor,
  ...). Projects live in shared storage: `/storage/emulated/0/RenPyProjects`.
- **Run projects on the device** - "Launch Project" boots the selected
  project in-process (no PC required). A small **‹ Launcher** button in the
  top-left corner of every running project takes you back (Ren'Py hides
  "Quit" on phones). Saves and log.txt stay inside the project folder.
- **Launcher tools work on-device**: Navigate Script, Check Script (Lint),
  Force Recompile, Delete Persistent, Change/Update GUI, Generate
  Translations, Extract Dialogue, Create New Project (full GUI template).
  They run in-process and the launcher comes back with the result.
- **Built-in file viewer** for "Edit File" links, lint reports and error
  reports, with an **Open in editor app** button (Acode, QuickEdit, Markor,
  ...). Projects live in `/storage/emulated/0/RenPyProjects`.
- **Errors never close the app**: a crashing or broken project shows Ren'Py's
  error screen; "Quit" returns to the launcher, which shows the traceback.
- **Starter project** ("Example Game") is generated on first run.
- Building APKs / distributions from a project still needs a PC (Gradle,
  Xcode and Emscripten cannot run on-device); those buttons say so.

## Install

1. Sideload `RenPy-8.6.1-engine-android-universal-arm32-arm64.apk`
   (Settings > Security > allow unknown apps).
2. Tap **Grant file access** (top right) and allow it: on Android 11+ this
   opens the "All files access" page, on Android 6-10 the storage permission
   dialog. Without it, projects are kept in the app's own folder
   (`Android/data/org.renpy.engine/files/RenPyProjects`); once access is
   granted the launcher switches to `/storage/emulated/0/RenPyProjects`.

Android 6.0+ (minSdk 23), OpenGL ES 3.0.

## How it works

Android cannot spawn a second interpreter process, which is how the desktop
launcher runs projects and its tools. The port runs them **in the same
process** instead:

1. The launcher (`mobile_support.rpy`) turns each `Project.launch()` into a
   *job* - the exact `renpy.sh <project> <command>` line the PC would run -
   and hands it to the bootstrap, then calls `renpy.utter_restart()`.
2. The bootstrap (`main.py`) starts the job on the next pass of the engine's
   main loop: it points the engine at the project and parses the job's
   command line.
3. However the run ends - quitting, the end of a command, parse errors, an
   uncaught exception, `sys.exit()` - the patched loop goes back to the
   launcher instead of exiting the app. The GL renderer is kept across the
   switch (no window teardown).
4. Launcher flows that need a command's result (Navigate Script, lint,
   Change GUI, translations) are resumed: the launcher restarts at the
   flow's label and the same `launch()` call returns at once, as if the
   subprocess had finished. Commands requested in the middle of flows that
   ask questions (New Project) are deferred until the front page.

The job queue lives in the bootstrap module's memory, so a fresh app start
always boots the launcher - a broken project can never cause a boot loop.

| Piece | What it does |
| --- | --- |
| `port/build_android.py` | Downloads the official Ren'Py SDK + RAPT, patches them, compiles the launcher, assembles the `android.dist` tree and drives `rapt.build.build()` directly (no launcher GUI). `--stage-only` skips Gradle. |
| `port/patches/main.py` | Replaces `renpy.py` as the bootstrap (`private/main.py`): job queue, in-process hand-off, the "‹ Launcher" button, crash dumps (`renpy_crash.txt`). |
| `port/patches/mobile_support.rpy` | Launcher additions: projects directory (with or without storage permission), starter project, on-device `Project.launch`, flow resumption, file viewer, "Grant file access" button. |
| `renpy/bootstrap.py` patch | Session hooks: start jobs, return to the launcher however a run ends, keep the renderer, dump fatal errors before Android's `System.exit`. |
| `renpy/editor.py` patch | The system editor hands files to the launcher's viewer. |
| `renpy/lint.py`, `renpy/translation/*` patches | Common files come from the APK (no common directory on disk). |
| `renpy/loader.py` patch | In project mode the packaged launcher assets (`assets/x-game/`) are skipped. |
| `gui7/code.py` patch, `launcher/` on disk | New Project reads the icon template and translated template code from disk. |
| `app-AndroidManifest.xml` patch | `requestLegacyExternalStorage` for Android 10. |
| `iconmaker.py` patch | Runs without pygame_sdl2 and finds icons in the dist directory. |

## Testing without a phone

`port/sim/` runs the packaged engine on Linux **as if it were Android**: the
real RAPT packaging (private tar + x-prefixed APK assets), the real
`android` runtime package, the Android code paths of the engine and the
port; only pyjnius is stubbed. It renders offscreen with Mesa, drives the
UI by tapping buttons by their text, and takes screenshots.

```bash
sudo apt-get install libegl1 libgles2 libegl-mesa0   # Mesa EGL/GLES
export BUILD_WORK=$PWD/build-work
sh port/sim/build_and_package.sh                     # stage + fake device
python3 port/sim/run_scenarios.py                    # all UI scenarios
```

Scenarios are JSON step lists in `port/sim/scenarios/` (see
`port/sim/sim_driver.rpy` for the step types). CI runs them before every
release build; screenshots are uploaded as a build artifact.

Signing: release keystore provided through GitHub Actions secrets
(`ANDROID_KEYSTORE_B64`, `RENPY_RELEASE_STORE_PASSWORD`,
`RENPY_RELEASE_KEY_ALIAS`); Gradle signs the APK from
`local.properties` written by the build script.

## Build from source

```bash
pip install pillow
python3 port/build_android.py   # needs JDK 21, Android SDK, ~4 GB disk
```

The APK lands in `out/RenPy-8.6.1-engine-android-universal-arm32-arm64.apk`.

## Limitations

- "Build Android package" / "Build Distributions" / "Web" / "iOS" cannot run
  on the device (they need the Android SDK, Xcode or Emscripten).
- Editing happens in another app: use "Open in editor app" from the file
  viewer, then "Launch Project" again (or "Reload" on an error screen).
- "Generate Translations" can't extract Ren'Py's own interface strings
  on-device (their sources aren't shipped); "Update Default Interface
  Translations" still works.
- x86_64 is included for emulators; the arm32 (`armeabi-v7a`) and
  arm64 (`arm64-v8a`) libraries are the real-device targets.

If the app ever closes unexpectedly, the full traceback is written to
`renpy_crash.txt` in the projects folder (and in
`Android/data/org.renpy.engine/files/`).

## License

Ren'Py is copyright (C) Tom Rothamel, MIT licensed. This port contains
unmodified Ren'Py engine/launcher code plus the small patches documented
above (also MIT). See the Ren'Py license inside the SDK for details.
