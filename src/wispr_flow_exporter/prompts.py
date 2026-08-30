"""The interactive setup shown when ``wispr-export`` is run with no arguments.

A bare invocation used to be an argparse usage error. That is the wrong answer
for a backup tool: the person running it almost always wants a sync, and the
thing they actually need to see first is what a sync is about to do -- which
directory it will read, where it will write, and which of the sensitive tiers
are switched on.

So every setting is shown with its default, each can be changed in place, and
the run is summarized and confirmed before anything is written. Defaults are
the same ones the flags and environment variables use, so the prompt teaches
the non-interactive form rather than being a separate path through the tool.

One prompt is deliberately harder to say yes to than the others. Screen context
is a bitmap and an accessibility capture of whatever application had focus while
you were dictating, so answering yes asks for a second, typed confirmation.
"""

from __future__ import annotations

import sys
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path


class PromptAborted(Exception):
    """The operator declined to proceed, or there was no terminal to ask."""


@dataclass(slots=True)
class Answers:
    """What the interactive setup collected.

    Attributes:
        data_dir: Wispr Flow application-support directory, or ``None`` for
            the platform default.
        archive_dir: Where the archive is written.
        source: Backend selection.
        entities: Entity names to archive, or ``None`` for all.
        audio: Meeting audio handling.
        max_audio_mb: Per-file audio cap.
        include_audio_blobs: Archive dictation audio blobs.
        include_images: Archive note images.
        include_screen_context: Archive screenshots and accessibility captures.
        recheck_days: Trailing days re-read for in-place edits.
        full: Ignore watermarks and re-check everything.
        strict_schema: Exit non-zero on additive schema drift.
    """

    data_dir: str | None
    archive_dir: str
    source: str
    entities: str | None
    audio: str
    max_audio_mb: int
    include_audio_blobs: bool
    include_images: bool
    include_screen_context: bool
    recheck_days: int
    full: bool
    strict_schema: bool

    def to_argv(self) -> list[str]:
        """Render the answers as the command line that would produce them.

        Printing this back is the point: the operator sees the non-interactive
        invocation for the choices they just made, so the prompt is a way to
        learn the flags rather than a substitute for them.

        Returns:
            An argument vector beginning with ``sync``.
        """
        argv = ["sync", "--source", self.source, "--audio", self.audio]
        if self.data_dir:
            argv += ["--data-dir", self.data_dir]
        if self.entities:
            argv += ["--only", self.entities]
        if self.full:
            argv.append("--full")
        if self.strict_schema:
            argv.append("--strict-schema")
        if self.include_audio_blobs:
            argv.append("--include-audio-blobs")
        if self.include_images:
            argv.append("--include-images")
        if self.include_screen_context:
            argv += ["--include-screen-context", "--i-understand"]
        return argv


def _ask(question: str, default: str, *, reader=input) -> str:
    """Ask for a value, offering a default.

    Args:
        question: The prompt text.
        default: Value used when the answer is empty.
        reader: Input function, injected for testing.

    Returns:
        The answer, or the default.

    Raises:
        PromptAborted: The input stream ended.
    """
    try:
        answer = reader(f"  {question} [{default}]: ").strip()
    except (EOFError, KeyboardInterrupt) as error:
        raise PromptAborted("cancelled") from error
    return answer or default


def _ask_bool(question: str, default: bool, *, reader=input) -> bool:
    """Ask a yes/no question.

    Args:
        question: The prompt text.
        default: Value used when the answer is empty.
        reader: Input function, injected for testing.

    Returns:
        The answer.
    """
    shown = "yes" if default else "no"
    while True:
        answer = _ask(question, shown, reader=reader).lower()
        if answer in {"y", "yes", "true", "1", "on"}:
            return True
        if answer in {"n", "no", "false", "0", "off"}:
            return False
        print("    please answer yes or no")


def _ask_choice(
    question: str, default: str, options: Sequence[str], *, reader=input
) -> str:
    """Ask for one of a fixed set of values.

    Args:
        question: The prompt text.
        default: Value used when the answer is empty.
        options: Permitted values.
        reader: Input function, injected for testing.

    Returns:
        The chosen value.
    """
    label = f"{question}  {' / '.join(options)}"
    while True:
        answer = _ask(label, default, reader=reader).lower()
        if answer in options:
            return answer
        print(f"    choose one of: {', '.join(options)}")


def _ask_int(question: str, default: int, *, reader=input) -> int:
    """Ask for a whole number.

    Args:
        question: The prompt text.
        default: Value used when the answer is empty.
        reader: Input function, injected for testing.

    Returns:
        The number.
    """
    while True:
        answer = _ask(question, str(default), reader=reader)
        try:
            return int(answer)
        except ValueError:
            print("    please enter a whole number")


def collect(defaults: Answers, *, reader=input, stream=None) -> Answers:
    """Run the interactive setup and return the chosen settings.

    Args:
        defaults: The values to offer, which are the same ones the flags and
            environment variables resolve to.
        reader: Input function, injected for testing.
        stream: Where the banner is written; defaults to stdout. Resolved at
            call time rather than bound as a default argument, so redirection
            works -- a ``stream=sys.stdout`` default binds whatever stdout was
            at import.

    Returns:
        The chosen settings.

    Raises:
        PromptAborted: There is no terminal to ask on, or the operator
            declined at the confirmation.
    """
    stream = stream if stream is not None else sys.stdout
    if not sys.stdin.isatty() and reader is input:
        raise PromptAborted(
            "no terminal to prompt on; pass a subcommand, for example: "
            "wispr-export sync"
        )

    print("wispr-flow-exporter", file=stream)
    print(file=stream)
    print("  No arguments given, so here is what a sync would do.", file=stream)
    print("  Press Enter to keep a default, or type a new value.", file=stream)
    print(file=stream)

    data_dir = _ask(
        "Wispr Flow data directory", defaults.data_dir or "auto-detect", reader=reader
    )
    archive_dir = _ask("Archive directory", defaults.archive_dir, reader=reader)
    source = _ask_choice(
        "Backend", defaults.source, ("all", "local", "cloud", "mcp", "both"), reader=reader
    )
    entities = _ask("Entities to archive", defaults.entities or "all", reader=reader)
    full = _ask_bool(
        "Re-check every record, ignoring watermarks?", defaults.full, reader=reader
    )

    print(file=stream)
    audio = _ask_choice(
        "Meeting audio", defaults.audio, ("copy", "link", "skip"), reader=reader
    )
    max_audio_mb = (
        _ask_int("Skip any single recording larger than (MB)", defaults.max_audio_mb, reader=reader)
        if audio == "copy"
        else defaults.max_audio_mb
    )
    include_audio_blobs = _ask_bool(
        "Include dictation audio blobs?", defaults.include_audio_blobs, reader=reader
    )
    include_images = _ask_bool(
        "Include images pasted into notes?", defaults.include_images, reader=reader
    )

    print(file=stream)
    print("  Screen context is a screenshot and an accessibility capture of", file=stream)
    print("  whatever application had focus while you were dictating. That can", file=stream)
    print("  be a password manager, a banking session, or someone else's screen", file=stream)
    print("  on a shared call.", file=stream)
    include_screen_context = _ask_bool(
        "Include screen context?", defaults.include_screen_context, reader=reader
    )
    if include_screen_context:
        # A second, typed confirmation. The other prompts take "y"; this one
        # does not, because it is the only answer that can put someone else's
        # screen into an archive.
        typed = _ask('Type "I understand" to confirm', "no", reader=reader)
        if typed.strip().lower() != "i understand":
            print("    not confirmed; screen context stays off", file=stream)
            include_screen_context = False

    print(file=stream)
    recheck_days = _ask_int(
        "Trailing days to re-read for edits", defaults.recheck_days, reader=reader
    )
    strict_schema = _ask_bool(
        "Fail on new columns from a Wispr Flow update?",
        defaults.strict_schema,
        reader=reader,
    )

    answers = Answers(
        data_dir=None if data_dir in ("", "auto-detect") else data_dir,
        archive_dir=archive_dir,
        source=source,
        entities=None if entities.strip().lower() in ("", "all") else entities,
        audio=audio,
        max_audio_mb=max_audio_mb,
        include_audio_blobs=include_audio_blobs,
        include_images=include_images,
        include_screen_context=include_screen_context,
        recheck_days=recheck_days,
        full=full,
        strict_schema=strict_schema,
    )

    print(file=stream)
    print("  Equivalent command:", file=stream)
    print(f"    wispr-export {' '.join(answers.to_argv())}", file=stream)
    print(file=stream)
    if not _ask_bool("Proceed?", True, reader=reader):
        raise PromptAborted("cancelled")
    print(file=stream)
    return answers


def ensure_ignored(archive_dir: Path, *, stream=None) -> None:
    """Make sure the archive cannot be committed to a git repository.

    The archive holds verbatim transcripts of real conversations and, when the
    wider tiers are on, captures of the screen. If it lands inside a working
    tree that does not ignore it, one ``git add -A`` publishes all of it.

    A pattern is appended to that repository's ``.gitignore`` rather than only
    warning, because a warning printed once at the top of a long run is a
    control that works exactly until somebody scrolls.

    Args:
        archive_dir: Where the archive is written.
        stream: Where notices are written; defaults to stdout, resolved at
            call time so redirection works.
    """
    import subprocess

    stream = stream if stream is not None else sys.stdout
    try:
        top = subprocess.run(
            ["git", "-C", str(archive_dir.parent), "rev-parse", "--show-toplevel"],
            capture_output=True,
            text=True,
            check=False,
            timeout=10,
        )
    except (OSError, subprocess.SubprocessError):
        return
    if top.returncode != 0 or not top.stdout.strip():
        return
    repo = Path(top.stdout.strip())

    # Checked against a file *inside* the archive rather than the directory
    # itself. A gitignore pattern ending in "/" matches directories only, so
    # asking about a path that does not exist yet reports "not ignored" -- and
    # a first run into a fresh archive would then append a rule duplicating one
    # already there, once per run.
    check = subprocess.run(
        ["git", "-C", str(repo), "check-ignore", "-q", str(archive_dir / "index.json")],
        capture_output=True,
        check=False,
        timeout=10,
    )
    if check.returncode == 0:
        return

    try:
        relative = archive_dir.resolve().relative_to(repo.resolve())
    except ValueError:
        return

    pattern = f"/{relative.as_posix()}/"
    gitignore = repo / ".gitignore"
    existing = (
        gitignore.read_text(encoding="utf-8") if gitignore.is_file() else ""
    )
    if not existing.endswith("\n") and existing:
        existing += "\n"
    gitignore.write_text(
        existing
        + "\n# Added by wispr-export: this archive holds verbatim transcripts\n"
        + "# of real conversations and must never be committed.\n"
        + f"{pattern}\n",
        encoding="utf-8",
    )
    print(
        f"  note         : added {pattern} to {repo / '.gitignore'} so the "
        "archive cannot be committed",
        file=stream,
    )
