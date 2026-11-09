import sys

__version__ = "0.1.0"

if sys.version_info[:2] < (3, 11):
    sys.exit("tmuxlog needs python 3.11 or newer")
