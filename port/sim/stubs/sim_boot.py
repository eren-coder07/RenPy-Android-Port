# Simulator entry point: put the jnius stub first on sys.path, then run the
# device's main.py exactly like librenpython's start.c does (as __main__).
import os
import runpy
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
main = sys.argv[1]
sys.argv = [main]
runpy.run_path(main, run_name="__main__")
