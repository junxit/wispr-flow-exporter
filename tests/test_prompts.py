"""The interactive setup, and the guarantee that an archive stays uncommittable."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from wispr_flow_exporter.prompts import (
    Answers,
    PromptAborted,
    collect,
    ensure_ignored,
)

DEFAULTS = Answers(
    data_dir=None,
    archive_dir="./archive",
    source="local",
    entities=None,
    audio="copy",
    max_audio_mb=512,
    include_audio_blobs=False,
    include_images=False,
    include_screen_context=False,
    recheck_days=14,
    full=False,
    strict_schema=False,
)


def _reader(answers: list[str], *, then_eof: bool = False):
    """Build an input function that replays scripted answers.

    Once the script runs out it keeps returning an empty string, which means
    "accept the default". That keeps a test about one answer from breaking
    every time a prompt is added or removed elsewhere in the flow -- which is
    exactly what happened when these were first written against a fixed count.

    Args:
        answers: What to return, in order.
        then_eof: Raise ``EOFError`` when exhausted instead, for the test that
            covers someone pressing Ctrl-D.

    Returns:
        A callable suitable for the ``reader`` parameter.
    """
    queue = list(answers)

    def read(prompt: str) -> str:
        if queue:
            return queue.pop(0)
        if then_eof:
            raise EOFError(prompt)
        return ""

    return read


def _answering(replies: dict[str, list[str]]):
    """Build an input function that answers by prompt text, not by position.

    Counting prompts made every test brittle to a question being added
    anywhere earlier in the flow, which broke them twice while this module was
    being written. Matching on a fragment of the prompt says what the test
    actually means: "when asked about X, say Y".

    Args:
        replies: Prompt fragment to the answers to give, in order. Anything
            not listed takes its default.

    Returns:
        A callable suitable for the ``reader`` parameter.
    """
    queues = {fragment: list(answers) for fragment, answers in replies.items()}

    def read(prompt: str) -> str:
        for fragment, queue in queues.items():
            if fragment in prompt and queue:
                return queue.pop(0)
        return ""

    return read


# --- accepting defaults ---------------------------------------------------


def test_pressing_enter_throughout_keeps_every_default(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """The common case: look at what it will do, then let it do it."""
    answers = collect(DEFAULTS, reader=_reader([""] * 12))

    assert answers.archive_dir == "./archive"
    assert answers.source == "local"
    assert answers.audio == "copy"
    assert answers.include_screen_context is False
    assert answers.to_argv() == ["sync", "--source", "local", "--audio", "copy"]


def test_the_equivalent_command_is_shown(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """The prompt teaches the flags rather than replacing them."""
    collect(DEFAULTS, reader=_reader([""] * 12))

    out = capsys.readouterr().out
    assert "Equivalent command:" in out
    assert "wispr-export sync --source local --audio copy" in out


def test_declining_at_the_confirmation_aborts() -> None:
    """Nothing is written until the summary has been agreed to."""
    with pytest.raises(PromptAborted, match="cancelled"):
        collect(DEFAULTS, reader=_answering({"Proceed": ["no"]}))


def test_an_interrupted_prompt_aborts_cleanly() -> None:
    """Ctrl-D at a prompt is an answer, not a traceback."""
    with pytest.raises(PromptAborted):
        collect(DEFAULTS, reader=_reader([], then_eof=True))


# --- changing defaults ----------------------------------------------------


def test_every_default_can_be_changed() -> None:
    """The point of showing a default is being able to override it."""
    answers = collect(
        DEFAULTS,
        reader=_answering(
            {
                "data directory": ["/tmp/wispr"],
                "Archive directory": ["/tmp/archive"],
                "Backend": ["both"],
                "Entities": ["meetings,notes"],
                "Re-check every record": ["yes"],
                "Meeting audio": ["skip"],
                "dictation audio blobs": ["yes"],
                "images pasted": ["yes"],
                "Include screen context": ["no"],
                "Trailing days": ["30"],
                "Fail on new columns": ["yes"],
            }
        ),
    )

    assert answers.data_dir == "/tmp/wispr"
    assert answers.archive_dir == "/tmp/archive"
    assert answers.source == "both"
    assert answers.entities == "meetings,notes"
    assert answers.full is True
    assert answers.audio == "skip"
    assert answers.include_audio_blobs is True
    assert answers.include_images is True
    assert answers.recheck_days == 30
    assert answers.strict_schema is True

    argv = answers.to_argv()
    assert "--full" in argv
    assert ["--only", "meetings,notes"] == argv[argv.index("--only") : argv.index("--only") + 2]


def test_the_audio_cap_is_only_asked_when_audio_is_copied() -> None:
    """A question that cannot matter should not be asked."""
    answers = collect(DEFAULTS, reader=_answering({"Meeting audio": ["skip"]}))
    assert answers.audio == "skip"
    assert answers.max_audio_mb == 512


def test_an_invalid_choice_is_re_asked(capsys: pytest.CaptureFixture[str]) -> None:
    """A typo must not silently select something else."""
    answers = collect(
        DEFAULTS, reader=_answering({"Backend": ["telepathy", "local"]})
    )

    assert answers.source == "local"
    assert "choose one of" in capsys.readouterr().out


def test_a_non_numeric_answer_is_re_asked(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Numbers are validated where they are entered."""
    answers = collect(
        DEFAULTS, reader=_answering({"Trailing days": ["soon", "21"]})
    )

    assert answers.recheck_days == 21
    assert "whole number" in capsys.readouterr().out


# --- the screen-context gate ----------------------------------------------


def test_screen_context_needs_a_typed_confirmation() -> None:
    """The one answer that can put someone else's screen into an archive.

    Every other prompt takes "y". This one does not.
    """
    answers = collect(
        DEFAULTS,
        reader=_answering(
            {
                "Include screen context": ["yes"],
                "I understand": ["I understand"],
            }
        ),
    )
    assert answers.include_screen_context is True
    assert "--i-understand" in answers.to_argv()


def test_a_bare_yes_does_not_enable_screen_context(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Saying yes and then not confirming leaves it off."""
    answers = collect(
        DEFAULTS,
        reader=_answering(
            {"Include screen context": ["yes"], "I understand": ["y"]}
        ),
    )

    assert answers.include_screen_context is False
    assert "screen context stays off" in capsys.readouterr().out


def test_the_screen_context_warning_names_what_it_captures(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """An informed answer needs the information before the question."""
    collect(DEFAULTS, reader=_reader([""] * 12))

    out = capsys.readouterr().out
    assert "password manager" in out
    assert "accessibility capture" in out


# --- the archive must stay uncommittable ----------------------------------


def _git(repo: Path, *args: str) -> subprocess.CompletedProcess[str]:
    """Run a git command in a repository.

    Args:
        repo: Working tree.
        *args: Arguments after ``git``.

    Returns:
        The completed process.
    """
    return subprocess.run(
        ["git", "-C", str(repo), *args], capture_output=True, text=True, check=False
    )


def test_an_unignored_archive_inside_a_repo_gets_ignored(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """One `git add -A` would otherwise publish every transcript.

    A warning printed once at the top of a long run is a control that works
    exactly until somebody scrolls, so the pattern is written rather than
    merely suggested.
    """
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q")
    archive = repo / "my-archive"
    archive.mkdir()

    ensure_ignored(archive)

    assert "/my-archive/" in (repo / ".gitignore").read_text(encoding="utf-8")
    assert _git(repo, "check-ignore", "-q", "my-archive/note.md").returncode == 0
    assert "cannot be committed" in capsys.readouterr().out


def test_an_already_ignored_archive_is_left_alone(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """The default archive/ is already covered, and must not be duplicated."""
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q")
    (repo / ".gitignore").write_text("archive/\n", encoding="utf-8")
    archive = repo / "archive"
    archive.mkdir()

    ensure_ignored(archive)

    assert (repo / ".gitignore").read_text(encoding="utf-8") == "archive/\n"
    assert capsys.readouterr().out == ""


def test_an_archive_outside_any_repository_is_fine(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Most archives will not live in a working tree at all."""
    archive = tmp_path / "elsewhere"
    archive.mkdir()

    ensure_ignored(archive)

    assert not (tmp_path / ".gitignore").exists()
    assert capsys.readouterr().out == ""


def test_the_existing_gitignore_is_appended_to_not_replaced(
    tmp_path: Path,
) -> None:
    """Rewriting someone's ignore file would be a poor way to protect them."""
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q")
    (repo / ".gitignore").write_text("*.log\n", encoding="utf-8")
    archive = repo / "vault"
    archive.mkdir()

    ensure_ignored(archive)

    body = (repo / ".gitignore").read_text(encoding="utf-8")
    assert body.startswith("*.log\n")
    assert "/vault/" in body
