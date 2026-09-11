"""Check the repository state immediately before creating or pushing a release tag."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

from check_agent_spec_placeholders import validation_errors

REPO_ROOT = Path(__file__).resolve().parents[1]


def git(*args: str, check: bool = True) -> str:
    result = subprocess.run(
        ["git", *args],
        cwd=REPO_ROOT,
        check=check,
        capture_output=True,
        text=True,
    )
    return result.stdout.strip()


def main() -> int:
    tag = sys.argv[1] if len(sys.argv) > 1 else "v0.1.0"
    errors: list[str] = []

    dirty = git("status", "--porcelain")
    if dirty:
        errors.append("the working tree is not clean")

    head = git("rev-parse", "HEAD")
    result = subprocess.run(
        ["git", "rev-parse", f"{tag}^{{commit}}"],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
    )
    if result.returncode:
        errors.append(f"tag {tag!r} does not exist")
    elif result.stdout.strip() != head:
        errors.append(f"tag {tag!r} does not point at HEAD")

    errors.extend(validation_errors())

    if errors:
        print("Not ready to publish:")
        for error in errors:
            print(f"  - {error}")
        return 1

    print(f"Ready to publish: clean tree, portable placeholders, {tag} -> HEAD")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
