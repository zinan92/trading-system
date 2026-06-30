import time
from pathlib import Path

from services.code_reload import CodeReloadGuard


def _write(p: Path, text: str) -> None:
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text, encoding="utf-8")


def test_guard_detects_no_change_initially(tmp_path: Path):
    root = tmp_path / "services"
    _write(root / "a.py", "x = 1\n")
    guard = CodeReloadGuard([root])
    assert guard.changed() is False


def test_guard_detects_modified_file(tmp_path: Path):
    root = tmp_path / "services"
    f = root / "a.py"
    _write(f, "x = 1\n")
    guard = CodeReloadGuard([root])
    time.sleep(0.01)
    _write(f, "x = 2\n")  # content + mtime change
    assert guard.changed() is True


def test_guard_detects_new_and_removed_files(tmp_path: Path):
    root = tmp_path / "services"
    _write(root / "a.py", "x = 1\n")
    guard = CodeReloadGuard([root])
    _write(root / "b.py", "y = 2\n")  # new file
    assert guard.changed() is True

    guard2 = CodeReloadGuard([root])
    (root / "b.py").unlink()  # removed file
    assert guard2.changed() is True


def test_guard_ignores_non_python_files(tmp_path: Path):
    root = tmp_path / "services"
    _write(root / "a.py", "x = 1\n")
    guard = CodeReloadGuard([root])
    _write(root / "notes.txt", "whatever")  # not .py
    _write(root / "data.json", "{}")
    assert guard.changed() is False
