## Simulator UI driver (port/sim only - never packaged into the APK).
##
## simulate_device.py copies this file into ANDROID_PUBLIC/game/, a
## directory the Android launcher really adds to its search path, so it is
## loaded on top of the packaged launcher. It replays a list of steps from
## the JSON file named by RENPY_ENGINE_SIM_STEPS, clicking buttons by their
## text the way a finger would. Progress is kept in a file, so the replay
## continues across renpy.utter_restart() and across app restarts.
##
## Step forms:
##   ["click", "text"]      click the button whose text contains "text"
##   ["wait", seconds]
##   ["shot", "name"]       save a screenshot to ANDROID_PUBLIC/sim-<name>.png
##   ["expect", "text"]     fail unless "text" is on screen (text or a button)
##   ["py", "code"]         exec code in the store
##   ["key", "keysym"]      press a key (e.g. "K_ESCAPE", "K_RETURN")
##   ["tap"]                tap the middle of the screen
##   ["type", "text"]       type text into the focused input

init 999 python:
    import os as _sim_os
    import json as _sim_json
    import time as _sim_time

    _sim_steps_fn = _sim_os.environ.get("RENPY_ENGINE_SIM_STEPS")
    _sim_progress_fn = _sim_os.path.join(_sim_os.environ.get("ANDROID_PUBLIC", "."), "sim_progress.json")
    _sim_log_fn = _sim_os.path.join(_sim_os.environ.get("ANDROID_PUBLIC", "."), "sim_driver.log")

    def _sim_log(*args):
        msg = " ".join(str(i) for i in args)
        with open(_sim_log_fn, "a") as f:
            f.write("%.1f %s\n" % (_sim_time.time(), msg))
        print("[sim-driver]", msg)

    def _sim_load_steps():
        try:
            with open(_sim_steps_fn) as f:
                d = _sim_json.load(f)
            # Either a list of steps, or {"env": {...}, "steps": [...]}.
            if hasattr(d, "keys"):  # (the store's dict is a revertable type)
                d = d.get("steps", [])
            return d
        except Exception:
            return []

    def _sim_get_progress():
        try:
            with open(_sim_progress_fn) as f:
                return int(_sim_json.load(f)["step"])
        except Exception:
            return 0

    def _sim_set_progress(n):
        with open(_sim_progress_fn, "w") as f:
            _sim_json.dump({"step": n}, f)

    _sim_state = {"next_time": _sim_time.time() + 2.0, "attempts": 0}

    def _sim_screen_text():
        try:
            return renpy.display.tts.root._tts_all(False)
        except Exception:
            return ""

    def _sim_focus_texts():
        rv = []
        for f in renpy.display.focus.focus_list:
            try:
                rv.append(f.widget._tts_all(False))
            except Exception:
                pass
        return rv

    def _sim_click(pattern):
        import renpy.test.testfocus as tf
        import renpy.test.testmouse as tm

        f = tf.find_focus(pattern, False)

        if f is None or f.x is None:
            return False

        x = int(f.x + f.w / 2)
        y = int(f.y + f.h / 2)
        tm.click_mouse(1, x, y)
        return True

    def _sim_periodic():
        if not _sim_steps_fn:
            return

        now = _sim_time.time()

        if now < _sim_state["next_time"]:
            return

        steps = _sim_load_steps()
        n = _sim_get_progress()

        if n >= len(steps):
            if not _sim_state.get("done"):
                _sim_state["done"] = True
                _sim_log("DONE all steps")
            return

        step = steps[n]
        kind = step[0]
        arg = step[1] if len(step) > 1 else None

        _sim_state["next_time"] = now + 0.7

        def advance():
            _sim_state["attempts"] = 0
            _sim_set_progress(n + 1)

        if kind == "wait":
            _sim_log("step", n, "wait", arg)
            advance()
            _sim_state["next_time"] = now + float(arg)

        elif kind == "shot":
            fn = _sim_os.path.join(_sim_os.environ.get("ANDROID_PUBLIC", "."), "sim-%s.png" % arg)
            # Read the last frame back from the GL renderer.
            renpy.screenshot(fn)
            _sim_log("step", n, "shot", fn)
            advance()

        elif kind == "expect":
            if arg.casefold() in (_sim_screen_text() + "\n".join(_sim_focus_texts())).casefold():
                _sim_log("step", n, "expect OK", repr(arg))
                advance()
            else:
                _sim_state["attempts"] += 1
                if _sim_state["attempts"] > 15:
                    _sim_log("step", n, "EXPECT FAILED", repr(arg), "screen:", repr(_sim_screen_text()[:400]))
                    advance()

        elif kind == "click":
            # Advance first: the click may restart the interpreter.
            if any(arg.casefold() in t.casefold() for t in _sim_focus_texts()):
                _sim_log("step", n, "click", repr(arg))
                advance()
                _sim_click(arg)
                _sim_state["next_time"] = now + 1.5
            else:
                _sim_state["attempts"] += 1
                if _sim_state["attempts"] > 15:
                    _sim_log("step", n, "CLICK TARGET NOT FOUND", repr(arg), "focusables:", repr(_sim_focus_texts()[:40]))
                    advance()

        elif kind == "key":
            import renpy.pygame as pg
            import renpy.test.testmouse as tm
            _sim_log("step", n, "key", arg)
            advance()
            key = getattr(pg, arg)
            pg.event.post(pg.event.Event(pg.KEYDOWN, key=key, mod=0, unicode="", test=True, repeat=False, scancode=0))
            pg.event.post(pg.event.Event(pg.KEYUP, key=key, mod=0, unicode="", test=True, repeat=False, scancode=0))

        elif kind == "tap":
            import renpy.test.testmouse as tm
            _sim_log("step", n, "tap")
            advance()
            tm.click_mouse(1, config.screen_width // 2, config.screen_height // 2)

        elif kind == "type":
            import renpy.pygame as pg
            _sim_log("step", n, "type", repr(arg))
            advance()
            pg.event.post(pg.event.Event(pg.TEXTINPUT, text=arg, test=True))

        elif kind == "py":
            _sim_log("step", n, "py", arg)
            advance()
            exec(arg, store.__dict__)

    config.periodic_callbacks.append(_sim_periodic)
