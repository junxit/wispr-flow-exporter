"""Command-line entry point for ``wispr-export``.

Every handler is a ``cmd_*(args) -> int`` returning a process exit code, and
``main`` does nothing but dispatch. Exit codes are part of the contract:

    0    success (may still carry additive schema-drift warnings)
    1    runtime failure, or one or more entities failed
    2    argparse usage error (reserved by argparse itself)
    3    additive schema drift, only under --strict-schema
    4    breaking schema drift, or a policy blocked data that should exist
    5    source unreachable
    130  interrupted; progress was saved

Code 3 is opt-in on purpose. Wispr Flow ships roughly twenty migrations a
month, so a non-zero exit on every new column would train the operator to
ignore the exit code -- and then to ignore code 4, which actually matters.
"""

from __future__ import annotations

import argparse

SOURCE_CHOICES = ("auto", "local", "cloud", "both")
AUDIO_CHOICES = ("copy", "link", "skip")
ENTITIES = (
    "meetings",
    "notes",
    "todos",
    "dictionary",
    "calendar",
    "dictation",
    "account",
    "tables",
)


def cmd_doctor(args: argparse.Namespace) -> int:
    """Report on the source, schema, policy and archive without writing.

    Args:
        args: Parsed arguments.

    Returns:
        Process exit code.
    """
    raise NotImplementedError("doctor lands in build step 5")


def cmd_sync(args: argparse.Namespace) -> int:
    """Archive new and changed data.

    Args:
        args: Parsed arguments.

    Returns:
        Process exit code.
    """
    raise NotImplementedError("sync lands in build step 9")


def main(argv: list[str] | None = None) -> int:
    """Entry point for the ``wispr-export`` command.

    Args:
        argv: Argument vector; defaults to ``sys.argv[1:]``.

    Returns:
        Process exit code.
    """
    parser = argparse.ArgumentParser(
        prog="wispr-export",
        description="Maintain a local archive of all Wispr Flow data.",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    def add_source(target: argparse.ArgumentParser) -> None:
        target.add_argument(
            "--source",
            choices=SOURCE_CHOICES,
            default=None,
            help="backend to use (default: auto — local, plus cloud when authorized)",
        )
        target.add_argument(
            "--data-dir",
            metavar="PATH",
            default=None,
            help="Wispr Flow application-support directory",
        )
        target.add_argument(
            "--db",
            metavar="PATH",
            default=None,
            help="flow.sqlite to read (a backup copy is opened immutable)",
        )

    doctor = sub.add_parser("doctor", help="validate source, schema, policy and archive")
    add_source(doctor)
    doctor.set_defaults(func=cmd_doctor)

    sync = sub.add_parser("sync", help="archive new and changed data")
    add_source(sync)
    sync.add_argument("-v", "--verbose", action="store_true", help="per-record output")
    sync.set_defaults(func=cmd_sync)

    args = parser.parse_args(argv)
    return int(args.func(args))


if __name__ == "__main__":
    raise SystemExit(main())
