# PyInstaller build recipe.
#
#     pyinstaller pzmodmanager.spec
#
# Two things this handles that a bare command line does not:
#
#   * it builds run-pzmodmanager.py, not pzmodmanager/__main__.py, because the latter
#     uses a relative import that cannot work as a frozen top level script;
#   * it collects Textual and Rich in full. Both load data files at runtime,
#     stylesheets and terminal tables, that PyInstaller does not find by
#     following imports, so without this the executable starts and then dies the
#     moment the interface opens.

from PyInstaller.utils.hooks import collect_all, collect_submodules

datas, binaries, hiddenimports = [], [], []
for package in ("textual", "rich"):
    package_datas, package_binaries, package_hidden = collect_all(package)
    datas += package_datas
    binaries += package_binaries
    hiddenimports += package_hidden

# Several pzmodmanager modules are imported inside functions, to keep startup
# quick and Pillow optional. Collect them explicitly rather than hoping the
# bytecode scan finds every one.
hiddenimports += collect_submodules("pzmodmanager")

analysis = Analysis(
    ["run-pzmodmanager.py"],
    pathex=["."],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    runtime_hooks=[],
    excludes=[],
    noarchive=False,
)

pyz = PYZ(analysis.pure)

exe = EXE(
    pyz,
    analysis.scripts,
    analysis.binaries,
    analysis.datas,
    [],
    name="pzmodmanager",
    debug=False,
    strip=False,
    upx=False,
    console=True,
    disable_windowed_traceback=False,
)
