from pathlib import Path

from PyInstaller.utils.hooks.qt import add_qt6_dependencies

hiddenimports, binaries, datas = add_qt6_dependencies(__file__)
# Exclude this plugin before binary analysis so its Qt DLLs are not pulled in.
binaries = [(src, dst) for src, dst in binaries if Path(src).name.lower() != "qtvirtualkeyboardplugin.dll"]
