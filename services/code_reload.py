"""Code-reload guard for long-running daemons.

The dashboard server is a launchd KeepAlive daemon: it imports the Python code
once at startup and serves stale code until manually restarted. This bit us
repeatedly (every dashboard_state.py change needed a manual `launchctl
kickstart`). The guard fingerprints the on-disk Python under the watched roots;
when it changes, the daemon exits and launchd respawns it with fresh code.

Only the fingerprint/`changed()` logic lives here (unit-tested). Wiring the
periodic check + process exit is a thin glue in the daemon's main().
"""

from __future__ import annotations

import hashlib
from pathlib import Path


class CodeReloadGuard:
    def __init__(self, roots: list[Path]) -> None:
        self.roots = [Path(r) for r in roots]
        self.baseline = self.fingerprint()

    def fingerprint(self) -> str:
        digest = hashlib.sha256()
        for root in self.roots:
            if not root.exists():
                continue
            for path in sorted(root.rglob("*.py")):
                try:
                    mtime = path.stat().st_mtime_ns
                except OSError:
                    continue
                digest.update(str(path).encode("utf-8"))
                digest.update(str(mtime).encode("utf-8"))
        return digest.hexdigest()[:16]

    def changed(self) -> bool:
        return self.fingerprint() != self.baseline
