"""Compatibility entry point for the Meta Hackathon inference baseline."""

from __future__ import annotations

import sys
from contextlib import redirect_stderr, redirect_stdout
from datetime import datetime
from pathlib import Path

if __package__:
    from .agent.runner import main
else:  # pragma: no cover - direct script execution
    from agent.runner import main


class _TeeStream:
    """Write output to terminal and a log file simultaneously."""

    def __init__(self, *streams) -> None:
        self._streams = streams

    def write(self, data: str) -> int:
        for stream in self._streams:
            stream.write(data)
        return len(data)

    def flush(self) -> None:
        for stream in self._streams:
            stream.flush()


if __name__ == "__main__":
    import logging as _logging
    _logging.basicConfig(level=_logging.WARNING, format="[%(name)s] %(message)s")
    _logging.getLogger("server.environment").setLevel(_logging.DEBUG)

    repo_root = Path(__file__).resolve().parent
    log_dir = repo_root / "results"
    log_dir.mkdir(parents=True, exist_ok=True)

    timestamp = datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
    log_path = log_dir / f"inference_{timestamp}.log"

    original_stdout = sys.stdout
    original_stderr = sys.stderr

    with log_path.open("w", encoding="utf-8") as log_file:
        tee_stdout = _TeeStream(original_stdout, log_file)
        tee_stderr = _TeeStream(original_stderr, log_file)
        with redirect_stdout(tee_stdout), redirect_stderr(tee_stderr):
            print(f"[LOG] Writing inference output to {log_path}", flush=True)
            main()
