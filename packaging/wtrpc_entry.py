"""PyInstaller entry-point stub.

``wtrpc`` is a package that uses relative imports (``wtrpc.__main__``), so
PyInstaller cannot be pointed at ``wtrpc/__main__.py`` directly -- it needs a
plain top-level script that imports the package normally.
"""

from wtrpc.__main__ import main

raise SystemExit(main())
