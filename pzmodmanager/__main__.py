"""Entry point for `python -m pzmodmanager`.

The relative import is the correct one when the package is run as a module, but
PyInstaller executes this file as a top level script with no package around it,
which makes a relative import fail before anything else can happen. The fallback
keeps both routes working. To build an executable, point PyInstaller at
run-pzmodmanager.py in the project root rather than at this file.
"""

try:
    from .cli import main
except ImportError:  # running as a plain script, or from inside a bundle
    from pzmodmanager.cli import main

if __name__ == "__main__":
    raise SystemExit(main())
