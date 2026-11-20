"""
Minimal stand-in for pyjnius, used only by port/sim/simulate_device.py to
run the packaged Ren'Py Engine on a Linux desktop as if it were Android.

It provides just the Java classes the engine and the port touch. Any other
class raises JavaException, exactly like a missing class on a real device,
so code paths that guard jnius calls with try/except behave the same way.
"""

import os
import sys

SIM_LOG = []


class JavaException(Exception):
    pass


def _log(*args):
    msg = " ".join(str(i) for i in args)
    SIM_LOG.append(msg)
    print("[jnius-sim]", msg, file=sys.stderr)


class _PackageManager(object):
    def hasSystemFeature(self, name):
        return False


class _Activity(object):
    """The PythonSDLActivity instance (mActivity)."""

    def getPackageName(self):
        return "org.renpy.engine"

    def getPackageManager(self):
        return _PackageManager()

    def getDPI(self):
        return 320.0

    def startActivity(self, intent):
        _log("startActivity", getattr(intent, "action", intent), getattr(intent, "data", None))

    def __getattr__(self, name):
        # vibrate, openUrl, setWakeLock, armOnStop, finishOnStop,
        # finishAndRemoveTask, ...
        def method(*args, **kwargs):
            _log("activity." + name, args)

        return method


class PythonSDLActivity(object):
    mActivity = _Activity()

    @staticmethod
    def isChromebook():
        return False


class SDLActivity(object):
    mHasFocus = True


class System(object):
    @staticmethod
    def exit(code):
        _log("System.exit", code)
        sys.stdout.flush()
        sys.stderr.flush()
        os._exit(code)


class Build(object):
    MANUFACTURER = "Simulator"
    MODEL = "Linux"


class Environment(object):
    @staticmethod
    def isExternalStorageManager():
        return os.environ.get("SIM_ALL_FILES_ACCESS", "1") == "1"


class Intent(object):
    ACTION_EDIT = "android.intent.action.EDIT"
    ACTION_VIEW = "android.intent.action.VIEW"
    FLAG_GRANT_READ_URI_PERMISSION = 1
    FLAG_GRANT_WRITE_URI_PERMISSION = 2

    def __init__(self, action=None, *args):
        self.action = action
        self.data = args[0] if args else None

    def setData(self, data):
        self.data = data
        return self

    def setDataAndType(self, data, mime):
        self.data = data
        self.type = mime
        return self

    def addFlags(self, flags):
        return self

    @staticmethod
    def createChooser(intent, title):
        return intent


class File(object):
    def __init__(self, path):
        self.path = path


class RenPyFileProvider(object):
    @staticmethod
    def getUriForFile(context, authority, f):
        storage = os.environ.get("RENPY_ENGINE_SHARED_STORAGE", "/storage/emulated/0")
        if not os.path.abspath(f.path).startswith(storage):
            raise JavaException("Failed to find configured root that contains %s" % f.path)
        return "content://%s/files/%s" % (authority, os.path.relpath(f.path, storage))


class Settings(object):
    ACTION_MANAGE_ALL_FILES_ACCESS_PERMISSION = "android.settings.MANAGE_ALL_FILES_ACCESS_PERMISSION"
    ACTION_MANAGE_APP_ALL_FILES_ACCESS_PERMISSION = "android.settings.MANAGE_APP_ALL_FILES_ACCESS_PERMISSION"
    ACTION_APPLICATION_DETAILS_SETTINGS = "android.settings.APPLICATION_DETAILS_SETTINGS"


class Uri(object):
    @staticmethod
    def parse(s):
        return s

    @staticmethod
    def fromParts(scheme, ssp, fragment):
        return "%s:%s" % (scheme, ssp)


class _Version(object):
    SDK_INT = int(os.environ.get("SIM_SDK_INT", "34"))


class BuildVersion(object):
    SDK_INT = _Version.SDK_INT


_CLASSES = {
    "org.renpy.android.PythonSDLActivity": PythonSDLActivity,
    "org.libsdl.app.SDLActivity": SDLActivity,
    "java.lang.System": System,
    "android.os.Build": Build,
    "android.os.Build$VERSION": BuildVersion,
    "android.os.Environment": Environment,
    "android.content.Intent": Intent,
    "android.provider.Settings": Settings,
    "android.net.Uri": Uri,
    "java.io.File": File,
    "org.renpy.android.RenPyFileProvider": RenPyFileProvider,
}


def autoclass(name):
    if name in _CLASSES:
        return _CLASSES[name]
    raise JavaException("Class not found %r (simulator)" % name)


def cast(name, obj):
    return obj


class PythonJavaClass(object):
    pass


def java_method(*args, **kwargs):
    def deco(f):
        return f

    return deco
