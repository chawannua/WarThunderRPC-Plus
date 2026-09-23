"""PyInstaller entry-point stub.

``wtrpc`` is a package that uses relative imports (``wtrpc.__main__``), so
PyInstaller cannot be pointed at ``wtrpc/__main__.py`` directly -- it needs a
plain top-level script that imports the package normally.
"""

import logging

from wtrpc.__main__ import main

# A windowed build turns an uncaught exception into a modal traceback dialog
# that sits over the game and never exits, so the watcher never restarts it.
try:
    code = main()
except SystemExit:
    raise
except BaseException:
    logging.getLogger("wtrpc").exception("Fatal error")
    code = 1
raise SystemExit(code)
