import sys

if sys.version_info < (3, 11):
    raise SystemExit("tmuxlog requires Python 3.11+")

__version__ = "0.1.0"
