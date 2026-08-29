"""Standalone entry point, and the file to hand to PyInstaller.

Everything here is an absolute import, so this works whether it is run directly,
frozen into an executable, or imported. pzmodmanager/__main__.py exists for
`python -m pzmodmanager` and is not suitable for freezing.
"""

from pzmodmanager.cli import main

if __name__ == "__main__":
    raise SystemExit(main())
