"""CLI de migration: python -m mervio.persistence {upgrade [rev] | downgrade [rev] | current | heads}."""
from __future__ import annotations

import sys

from . import migrate


def main(argv=None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if not args or args[0] not in ("upgrade", "downgrade", "current", "heads"):
        print(__doc__, file=sys.stderr)
        return 2
    action = args[0]
    if action == "upgrade":
        migrate.upgrade(revision=args[1] if len(args) > 1 else "head")
    elif action == "downgrade":
        migrate.downgrade(revision=args[1] if len(args) > 1 else "-1")
    elif action == "current":
        print(migrate.current_revision() or "(base vide)")
    else:
        print(migrate.head_revision())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
