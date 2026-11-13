"""Run a local script/module with network audit guards and key reads forbidden.

Used for clean-environment pip bootstrap as well as application commands. Audit
guards avoid replacing the socket class while Python imports SSL/pip libraries.
"""

import os
from pathlib import Path
import runpy
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))


def no_network(event, args):
    if event in ("socket.__new__", "socket.connect", "socket.getaddrinfo", "socket.bind"):
        raise AssertionError("Offline handover test forbids network access")


sys.addaudithook(no_network)
original_getitem = os._Environ.__getitem__


def no_key(environ, key):
    if key == "API_KEY":
        raise AssertionError("Offline handover test must not read API_KEY")
    return original_getitem(environ, key)


os._Environ.__getitem__ = no_key
mode, target, *arguments = sys.argv[1:]
sys.argv = [target, *arguments]
if mode == "module":
    runpy.run_module(target, run_name="__main__")
elif mode == "script":
    runpy.run_path(target, run_name="__main__")
else:
    raise ValueError("Unknown offline command mode")
