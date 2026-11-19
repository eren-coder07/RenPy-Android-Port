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
  project in-process (no PC required). Saves and log.txt stay inside the
  project folder, like on the PC.
- **Starter project** ("Example Game") is generated on first run.
- Lint / compile / force-recompile: recompiling happens automatically when
  a project is launched (`.rpy` files are compiled on load).
- Building APKs from a project still needs a PC (Gradle/Android SDK cannot
  run on-device) - finish projects with the desktop launcher, or send the
  project folder to a PC.

## Install

1. Sideload `RenPy-8.6.1-engine-android-universal-arm32-arm64.apk`
   (Settings > Security > allow unknown apps).
2. On first run grant **All files access**
   (Settings > Apps > Ren'Py Engine > Permissions > Files and media > Allow
   management of all files). A "Grant file access" button in the launcher
   opens the right page.
3. Pick `RenPyProjects` as the projects directory when asked.

Android 6.0+ (minSdk 23), OpenGL ES 3.0.

## How it works

| Piece | What it does |
| --- | --- |
| `port/build_android.py` | Downloads the official Ren'Py SDK + RAPT, patches them, compiles the launcher, assembles the `android.dist` tree and drives `rapt.build.build()` directly (no launcher GUI). |
| `port/patches/main.py` | Replaces `renpy.py` as the bootstrap (`private/main.py`). Implements the project switch: a generation counter in `ANDROID_PUBLIC/renpy_engine_state.json` decides whether the launcher or a user project boots; project mode exports `RENPY_PROJECT_GAME_DIR`. |
| `port/patches/mobile_support.rpy` | Launcher additions: default projects directory in shared storage, starter project generation, "Launch Project" runs projects on-device (`MobileLaunch`), "Grant file access" overlay button. |
| `renpy/loader.py` patch | In project mode the packaged game asset layer (`assets/x-game/`) is skipped, so only the project's scripts are loaded. |
| `app-AndroidManifest.xml` patch | `requestLegacyExternalStorage` for Android 10. |
| `iconmaker.py` patch | Runs without pygame_sdl2 and finds icons in the dist directory. |

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

- "Build Android package" / "Build web" inside the launcher cannot run on
  the device (they need the Android SDK / Emscripten toolchain).
- Some launcher tools that spawn external processes (lint, open editor,
  force recompile) are desktop-only; projects recompile automatically on
  launch instead.
- Returning from a running game to the launcher: quit the game (or close
  the app from recents) and reopen - the launcher comes back.
- x86_64 is included for emulators; the arm32 (`armeabi-v7a`) and
  arm64 (`arm64-v8a`) libraries are the real-device targets.

## License

Ren'Py is copyright (C) Tom Rothamel, MIT licensed. This port contains
unmodified Ren'Py engine/launcher code plus the small patches documented
above (also MIT). See the Ren'Py license inside the SDK for details.
