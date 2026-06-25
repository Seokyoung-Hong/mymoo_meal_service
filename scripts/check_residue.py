"""Scan project files for disallowed identity residue."""

from __future__ import annotations

import argparse
from pathlib import Path
import sys


IGNORED_NAMES = {
    ".git",
    ".mypy_cache",
    ".pytest_cache",
    ".ruff_cache",
    ".venv",
    "__pycache__",
    "build",
    "dist",
    "node_modules",
    "venv",
}

DEFAULT_TERMS = [
    "sandol",
    "Sandol",
    "brunch",
    "ibook",
    "TIP_RESTAURANT_ID",
    "E_RESTAURANT_ID",
]

DEFAULT_PATHS = [
    "app",
    "main.py",
    "README.md",
    "docker-compose.yml",
    "Dockerfile",
    ".env.example",
]


def parse_args() -> argparse.Namespace:
    """Parse CLI arguments."""
    parser = argparse.ArgumentParser(
        description="Recursively scan target project files for disallowed terms."
    )
    parser.add_argument(
        "--terms",
        nargs="+",
        default=DEFAULT_TERMS,
        help="One or more exact terms to search for. Defaults to the plan residue terms.",
    )
    parser.add_argument(
        "--paths",
        nargs="*",
        default=DEFAULT_PATHS,
        help="Optional target-relative files or directories to scan. Defaults to production-facing files.",
    )
    return parser.parse_args()


def is_ignored(path: Path, project_root: Path) -> bool:
    """Return True when a path is inside ignored cache/generated locations."""
    try:
        relative = path.resolve().relative_to(project_root)
    except ValueError:
        return True
    return any(part in IGNORED_NAMES for part in relative.parts)


def is_binary(path: Path) -> bool:
    """Best-effort binary file detection using a small byte sample."""
    try:
        chunk = path.read_bytes()[:4096]
    except OSError:
        return True
    return b"\0" in chunk


def iter_files(paths: list[str], project_root: Path):
    """Yield files under target project paths only."""
    scanner_path = Path(__file__).resolve()
    for raw_path in paths:
        candidate = (project_root / raw_path).resolve()
        try:
            candidate.relative_to(project_root)
        except ValueError:
            print(f"Skipping outside path: {raw_path}", file=sys.stderr)
            continue

        if not candidate.exists():
            print(f"Skipping missing path: {raw_path}", file=sys.stderr)
            continue

        if candidate.is_file():
            if candidate != scanner_path and not is_ignored(candidate, project_root):
                yield candidate
            continue

        for path in candidate.rglob("*"):
            if (
                path.is_file()
                and path != scanner_path
                and not is_ignored(path, project_root)
            ):
                yield path


def scan_file(path: Path, terms: list[str], project_root: Path) -> list[str]:
    """Return formatted matches for a single text file."""
    if is_binary(path):
        return []

    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except UnicodeDecodeError:
        try:
            lines = path.read_text(encoding="utf-8-sig").splitlines()
        except UnicodeDecodeError:
            return []
    except OSError:
        return []

    matches: list[str] = []
    display_path = path.relative_to(project_root)
    for line_number, line in enumerate(lines, start=1):
        found_terms = [term for term in terms if term in line]
        if found_terms:
            matches.append(
                f"{display_path}:{line_number}: "
                f"{', '.join(found_terms)}: {line.strip()}"
            )
    return matches


def main() -> int:
    """Run the residue scanner."""
    args = parse_args()
    project_root = Path(__file__).resolve().parents[1]

    terms = args.terms or DEFAULT_TERMS

    matches: list[str] = []
    for path in iter_files(args.paths, project_root):
        matches.extend(scan_file(path, terms, project_root))

    if matches:
        print("Disallowed residue found:")
        print("\n".join(matches))
        return 1

    print("No disallowed residue found.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
