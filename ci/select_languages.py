#!/usr/bin/env python3
"""Select native implementations affected by a set of changed paths."""

from __future__ import annotations

import argparse
import sys
from collections.abc import Iterable, Sequence


LANGUAGES = ("rust", "cpp", "go", "zig")
CROSS_LANGUAGE_PREFIXES = ("shared/", ".gitea/", "ci/")
CROSS_LANGUAGE_FILES = {"SPEC.md"}


def select_languages(
    changed_paths: Iterable[str],
    *,
    force_all: bool = False,
) -> tuple[str, ...]:
    """Return affected implementations in conformance argument order."""
    if force_all:
        return LANGUAGES

    selected: set[str] = set()
    for raw_path in changed_paths:
        path = raw_path.strip().removeprefix("./")
        if not path:
            continue
        if path in CROSS_LANGUAGE_FILES or path.startswith(CROSS_LANGUAGE_PREFIXES):
            return LANGUAGES
        for language in LANGUAGES:
            if path.startswith(f"{language}/"):
                selected.add(language)
                break
        else:
            return LANGUAGES

    return tuple(language for language in LANGUAGES if language in selected)


def format_outputs(selected_languages: Sequence[str]) -> str:
    """Format values for appending to the GitHub Actions output file."""
    selected = set(selected_languages)
    lines = [
        *(f"{language}={str(language in selected).lower()}" for language in LANGUAGES),
        f"any={str(bool(selected)).lower()}",
    ]
    return "\n".join(lines) + "\n"


def main(arguments: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--all",
        action="store_true",
        dest="force_all",
        help="select all implementations for a manual workflow run",
    )
    options = parser.parse_args(arguments)
    selected = select_languages(sys.stdin, force_all=options.force_all)
    sys.stdout.write(format_outputs(selected))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
