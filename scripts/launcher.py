# -*- coding: utf-8 -*-
"""AstrBot launcher: no console window + UTF-8 log.

Why not just run python.exe main.py?

1. A console window belongs to one process tree. Close the window and Windows
   sends CTRL_CLOSE_EVENT to every attached process -- AstrBot dies without a
   single line in its log, which looks like a mysterious crash.
2. Windows python.exe encodes stdout with the ANSI code page (GBK on zh-CN),
   so the log file ends up in a mix of encodings and shows up garbled.

Run it with pythonw.exe:

    pythonw.exe scripts\launcher.py

Set ASTRBOT_DIR to override the default (<repo>/AstrBot).
"""

import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
BASE = os.environ.get("ASTRBOT_DIR") or os.path.join(ROOT, "AstrBot")
LOG = os.path.join(ROOT, "logs", "astrbot.log")

os.chdir(BASE)
if BASE not in sys.path:
    sys.path.insert(0, BASE)

os.environ["PYTHONIOENCODING"] = "utf-8"
os.environ["PYTHONUTF8"] = "1"
os.environ["PYTHONUNBUFFERED"] = "1"

os.makedirs(os.path.dirname(LOG), exist_ok=True)
log = open(LOG, "a", encoding="utf-8", errors="replace", buffering=1)

sys.stdout = log
sys.stderr = log
sys.stdin = open(os.devnull, "r", encoding="utf-8", errors="replace")

import runpy
import time
import traceback

try:
    log.write("\n===== launcher start %s pid=%s =====\n"
              % (time.strftime("%Y-%m-%d %H:%M:%S"), os.getpid()))
    log.flush()
    runpy.run_path(os.path.join(BASE, "main.py"), run_name="__main__")
except SystemExit:
    pass
except BaseException:
    traceback.print_exc(file=log)
finally:
    try:
        log.write("\n===== launcher exit %s =====\n" % time.strftime("%Y-%m-%d %H:%M:%S"))
        log.flush()
    except Exception:
        pass
