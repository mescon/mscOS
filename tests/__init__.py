"""Tests of mscOS that need no device: run with tools/test.sh (in a container with the
panel's dependencies) or `python3 -m unittest discover -s tests -t .` where aiohttp,
websockets and numpy are installed."""
import os
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BACKEND = os.path.join(ROOT, "web", "backend")
if BACKEND not in sys.path:
    sys.path.insert(0, BACKEND)
# a state directory of the test run's own, before anything of the panel is imported
STATE = tempfile.mkdtemp(prefix="mscos-test-")
os.environ.update(MSCOS_STATE=STATE, MSCOS_MODULES=os.path.join(ROOT, "modules"),
                  MSCOS_STATIC=os.path.join(ROOT, "web", "static"), MSCOS_PLUGINS_DIR=os.path.join(STATE, "plugins"))
