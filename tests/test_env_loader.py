"""Guardrail spec for scripts/with_env.sh.

The load-bearing assertion is test_names_mode_never_leaks_a_value: the script's
entire purpose is that secrets reach a child process and nothing else. If that
test can be made to fail, credentials are leaking into logs and transcripts.

These assertions may be added to. They may never be weakened or deleted.
"""
import os
import stat
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
SCRIPT = ROOT / "scripts" / "with_env.sh"
FIXTURE = ROOT / "fixtures" / "env" / "sample.env"

# Every value in the fixture. None of these may ever appear in the script's output.
SECRET_VALUES = [
    "plainvalue123",
    "quoted-value-456",
    "single-value-789",
    "exported-value-abc",
    "key=with=equals",
    "p@ssw0rd!",
    "spaced-value-def",
]

EXPECTED_NAMES = {
    "SAMPLE_PLAIN", "SAMPLE_QUOTED", "SAMPLE_SINGLE", "SAMPLE_EXPORTED",
    "SAMPLE_WITH_EQUALS", "SAMPLE_SPECIAL", "SAMPLE_SPACED",
}


def run(*args, expect_ok=True):
    proc = subprocess.run(
        ["bash", str(SCRIPT), "--env-file", str(FIXTURE), *args],
        capture_output=True, text=True,
    )
    if expect_ok:
        assert proc.returncode == 0, f"rc={proc.returncode} stderr={proc.stderr}"
    return proc


@pytest.fixture(autouse=True)
def _fixture_perms():
    FIXTURE.chmod(0o600)
    yield


class TestNamesMode:
    def test_lists_every_variable_name(self):
        names = set(run("--names").stdout.split())
        assert names == EXPECTED_NAMES

    def test_names_mode_never_leaks_a_value(self):
        """The whole point of the script. Do not weaken this."""
        proc = run("--names")
        combined = proc.stdout + proc.stderr
        for secret in SECRET_VALUES:
            assert secret not in combined, f"value {secret!r} leaked into output"

    def test_comments_and_blank_lines_are_skipped(self):
        assert "#" not in run("--names").stdout


class TestChildProcess:
    def test_value_reaches_the_child(self):
        proc = run("--", "printenv", "SAMPLE_PLAIN")
        assert proc.stdout.strip() == "plainvalue123"

    def test_double_quotes_are_stripped(self):
        assert run("--", "printenv", "SAMPLE_QUOTED").stdout.strip() == "quoted-value-456"

    def test_single_quotes_are_stripped(self):
        assert run("--", "printenv", "SAMPLE_SINGLE").stdout.strip() == "single-value-789"

    def test_export_prefix_is_handled(self):
        assert run("--", "printenv", "SAMPLE_EXPORTED").stdout.strip() == "exported-value-abc"

    def test_value_containing_equals_is_preserved(self):
        assert run("--", "printenv", "SAMPLE_WITH_EQUALS").stdout.strip() == "key=with=equals"

    def test_surrounding_whitespace_is_trimmed(self):
        assert run("--", "printenv", "SAMPLE_SPACED").stdout.strip() == "spaced-value-def"

    def test_does_not_leak_into_the_calling_environment(self):
        run("--", "true")
        assert "SAMPLE_PLAIN" not in os.environ


class TestInjection:
    def test_backticks_in_a_value_are_not_executed(self):
        """A pasted secret containing shell metacharacters must be inert.

        This is why the script parses line by line instead of sourcing the file.
        """
        proc = run("--", "printenv", "SAMPLE_SPECIAL")
        # The payload must survive byte-for-byte: backticks not executed as a
        # command substitution, and $backtick not expanded as a variable.
        assert proc.stdout.strip() == "p@ssw0rd!$backtick`echo pwned`"


class TestRefusals:
    def test_refuses_a_group_readable_file(self, tmp_path):
        loose = tmp_path / "loose.env"
        loose.write_text("A=b\n")
        loose.chmod(0o644)
        proc = subprocess.run(
            ["bash", str(SCRIPT), "--env-file", str(loose), "--names"],
            capture_output=True, text=True,
        )
        assert proc.returncode != 0
        assert "chmod 600" in proc.stderr

    def test_refuses_a_missing_file(self, tmp_path):
        proc = subprocess.run(
            ["bash", str(SCRIPT), "--env-file", str(tmp_path / "nope.env"), "--names"],
            capture_output=True, text=True,
        )
        assert proc.returncode != 0

    def test_refuses_when_no_command_given(self):
        proc = run(expect_ok=False)
        assert proc.returncode != 0


class TestRepoHygiene:
    def test_dotenv_is_gitignored(self):
        proc = subprocess.run(
            ["git", "check-ignore", "-q", ".env"], cwd=ROOT, capture_output=True
        )
        assert proc.returncode == 0, ".env is NOT gitignored"

    def test_dotenv_variants_are_gitignored(self):
        for name in [".env.local", ".env.production", ".env.azure"]:
            proc = subprocess.run(
                ["git", "check-ignore", "-q", name], cwd=ROOT, capture_output=True
            )
            assert proc.returncode == 0, f"{name} is NOT gitignored"

    def test_dotenv_is_not_tracked_by_git(self):
        proc = subprocess.run(
            ["git", "ls-files", "--error-unmatch", ".env"], cwd=ROOT, capture_output=True
        )
        assert proc.returncode != 0, ".env is TRACKED by git"

    def test_fixture_is_not_world_readable(self):
        assert not (FIXTURE.stat().st_mode & (stat.S_IRGRP | stat.S_IROTH))
