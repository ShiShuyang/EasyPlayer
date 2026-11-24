"""Collect the QML imports used by the player's video surface before DLL analysis."""
import json
from pathlib import Path
import subprocess

from PyInstaller.utils.hooks.qt import add_qt6_dependencies, pyside6_library_info

hiddenimports, binaries, datas = add_qt6_dependencies(__file__)
binaries = [(src, dst) for src, dst in binaries if Path(dst).name != "qmltooling"]

qml_file = Path(__file__).resolve().parents[2] / "flat_video.qml"
qml_root = Path(pyside6_library_info.location["QmlImportsPath"])
scanner = qml_root.parent / "qmlimportscanner.exe"
result = subprocess.run(
    [scanner, "-qmlFiles", qml_file, "-importPath", qml_root],
    check=True, capture_output=True, encoding="utf-8",
)
for module in json.loads(result.stdout):
    # QML is a built-in language module with no files on disk.
    if module["name"] == "QML" and not module.get("path"):
        continue
    directory = Path(module["path"])
    destination = str(Path(pyside6_library_info.qt_rel_dir) / "qml" / directory.relative_to(qml_root))
    if not (directory / "qmldir").is_file():
        raise RuntimeError(f"Unresolved QML module: {module['name']}")
    if module.get("plugin") and not (directory / (module["plugin"] + ".dll")).is_file():
        raise RuntimeError(f"Missing QML plugin: {module['plugin']}")
    # Nested directories are separate QML modules (e.g. QtQuick/Controls).
    for file in sorted(directory.iterdir()):
        if file.is_file():
            collection = binaries if file.suffix.lower() == ".dll" else datas
            collection.append((str(file), destination))
