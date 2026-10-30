"""Write MANIFEST.sha256 for this directory. Run once, when the claim set is final: python eval/frozen/claims/freeze.py"""
from __future__ import annotations

import hashlib
from pathlib import Path

HERE = Path(__file__).resolve().parent
MANIFEST = HERE / "MANIFEST.sha256"


def frozen_files() -> list[Path]:
    return sorted(p for p in HERE.rglob("*") if p.is_file() and p.name != "MANIFEST.sha256" and "__pycache__" not in p.parts and p.suffix != ".pyc")


def main() -> None:
    lines = [f"{hashlib.sha256(p.read_bytes()).hexdigest()}  {p.relative_to(HERE).as_posix()}" for p in frozen_files()]
    MANIFEST.write_text("\n".join(lines) + "\n", encoding="utf-8", newline="\n")
    print(f"froze {len(lines)} files")


if __name__ == "__main__":
    main()
