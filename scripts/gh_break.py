"""CLI tool to inject faults into the GitHub demo repo or reset it to pristine state.

Usage:
  python scripts/gh_break.py deps
  python scripts/gh_break.py regression
  python scripts/gh_break.py dockerfile
  python scripts/gh_break.py pyversion
  python scripts/gh_break.py secret
  python scripts/gh_break.py --reset
  python scripts/gh_break.py --init
"""

import sys
from pathlib import Path

# Add project root to path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from lazarus import chaos_gh


def main():
    if len(sys.argv) < 2:
        print(__doc__)
        sys.exit(1)

    arg = sys.argv[1].lower()
    if arg in ("--reset", "reset"):
        print("Resetting GitHub repository to pristine green state...")
        res = chaos_gh.reset()
        print(f"Success: {res['message']}")
    elif arg in ("--init", "init"):
        print("Initializing GitHub repository with pristine files...")
        res = chaos_gh.init_repo()
        print(f"Success: {res['message']}")
    elif arg in ("deps", "regression", "dockerfile", "pyversion", "secret"):
        print(f"Injecting fault '{arg}' into GitHub repo...")
        res = chaos_gh.break_repo(arg)
        print(f"Success: {res['message']}")
        print("Watch your GitHub Actions tab go red, then watch n8n and Lazarus heal it!")
    else:
        print(f"Unknown option '{arg}'. Choose from: deps, regression, dockerfile, pyversion, secret, --reset, --init")
        sys.exit(1)


if __name__ == "__main__":
    main()
