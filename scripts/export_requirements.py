"""Print the project's runtime dependencies, one per line.

Used by the Dockerfile to install dependencies **before** the application source is
copied, which keeps the slow dependency layer cached across source edits.

The obvious alternative -- ``pip install .`` with only ``pyproject.toml`` present --
does not work: the build backend needs the ``app`` and ``mcp_servers`` packages in
order to build a wheel, and they have not been copied yet at that point in the build.
Reading the dependency list directly avoids both that failure and the duplication of
maintaining a separate requirements.txt.

Usage:
    python scripts/export_requirements.py [--dev] > requirements.txt
"""

from __future__ import annotations

import argparse
import pathlib
import sys
import tomllib

PYPROJECT = pathlib.Path(__file__).resolve().parents[1] / "pyproject.toml"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--dev", action="store_true", help="Include the optional 'dev' dependency group."
    )
    args = parser.parse_args()

    if not PYPROJECT.is_file():
        print(f"pyproject.toml not found at {PYPROJECT}", file=sys.stderr)
        return 1

    project = tomllib.loads(PYPROJECT.read_text(encoding="utf-8"))["project"]
    requirements: list[str] = list(project.get("dependencies", []))
    if args.dev:
        requirements += project.get("optional-dependencies", {}).get("dev", [])

    print("\n".join(requirements))
    return 0


if __name__ == "__main__":
    sys.exit(main())
