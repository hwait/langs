from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

from linguawiki.errors import LinguaWikiError
from linguawiki.repository_policy import GENERATED_START
from linguawiki.services import skills as skills_service
from linguawiki.services import workspace as workspace_service
from tests.conftest import SyntheticWorkspace
from tests.support.clocks import AdvancingClock


def _check(report: workspace_service.DoctorReport, name: str) -> object:
    return next(check for check in report.checks if check.name == name)


def _statuses(report: workspace_service.DoctorReport) -> dict[str, str]:
    return {check.name: check.status for check in report.checks}


def test_doctor_passes_on_a_freshly_initialized_workspace(
    synthetic_workspace: SyntheticWorkspace,
) -> None:
    report = workspace_service.doctor(synthetic_workspace.root, clock=synthetic_workspace.clock)
    statuses = _statuses(report)

    assert report.ok is True
    assert report.failures == ()
    for name in (
        "configuration",
        "lock",
        "pin_core",
        "pin_database_schema",
        "pin_skill_bundle",
        "no_core_source_copy",
        "backup_root",
        "generated_skills",
        "migration_history",
        "migration_head",
        "tables_present",
        "workspace_identity",
        "version_mirror",
        "lock_mirror",
        "installed_versions",
        "workspace_identity_match",
        "generated_files",
        "git_remote_privacy",
        "single_program",
        "privacy_candidates",
        "ignore_rules",
    ):
        assert statuses[name] == "ok", name


def test_doctor_always_states_that_sanitization_is_not_publication_safety(
    synthetic_workspace: SyntheticWorkspace,
) -> None:
    report = workspace_service.doctor(synthetic_workspace.root, clock=synthetic_workspace.clock)

    assert _statuses(report)["privacy_scope"] == "warning"
    assert any("not proof this repository is safe to publish" in item for item in report.warnings)


def test_doctor_detects_modified_generated_skills(
    synthetic_workspace: SyntheticWorkspace,
) -> None:
    skill = synthetic_workspace.root / ".agents" / "skills" / "linguawiki" / "SKILL.md"
    skill.write_text("hand written replacement", encoding="utf-8")

    report = workspace_service.doctor(synthetic_workspace.root, clock=synthetic_workspace.clock)

    assert report.ok is False
    assert _statuses(report)["generated_skills"] == "failed"
    assert "linguawiki/SKILL.md" in _check(report, "generated_skills").context["modified"]


def test_reinstalling_the_bundle_repairs_a_modified_snapshot(
    synthetic_workspace: SyntheticWorkspace,
) -> None:
    (synthetic_workspace.root / ".agents" / "skills" / "extra.md").write_text("x", encoding="utf-8")

    assert skills_service.inspect_bundle(synthetic_workspace.paths).unexpected == ("extra.md",)

    skills_service.install_bundle(synthetic_workspace.paths, clock=synthetic_workspace.clock)
    report = workspace_service.doctor(synthetic_workspace.root, clock=synthetic_workspace.clock)

    assert report.ok is True


def test_skills_check_compares_against_the_lock(synthetic_workspace: SyntheticWorkspace) -> None:
    lock = workspace_service.load_lock(synthetic_workspace.paths)
    bundle = skills_service.inspect_bundle(synthetic_workspace.paths, lock=lock)

    assert bundle.installed is True
    assert bundle.matches_lock is True
    assert bundle.matches_installed_core is True
    assert bundle.version == lock.skill_bundle.version


def test_skills_check_reports_an_absent_snapshot(synthetic_workspace: SyntheticWorkspace) -> None:
    shutil.rmtree(synthetic_workspace.paths.skills)
    lock = workspace_service.load_lock(synthetic_workspace.paths)

    bundle = skills_service.inspect_bundle(synthetic_workspace.paths, lock=lock)

    assert bundle.installed is False
    assert bundle.matches_lock is False
    assert bundle.warnings == ("the generated skill snapshot is not installed",)


def test_a_missing_generated_marker_is_reported(synthetic_workspace: SyntheticWorkspace) -> None:
    marker = synthetic_workspace.paths.skills / skills_service.GENERATED_MARKER_NAME
    marker.unlink()

    assert (
        "marker is missing" in skills_service.inspect_bundle(synthetic_workspace.paths).warnings[0]
    )


def test_the_generated_marker_is_excluded_from_the_bundle_hash(
    synthetic_workspace: SyntheticWorkspace,
) -> None:
    marker = synthetic_workspace.paths.skills / skills_service.GENERATED_MARKER_NAME
    payload = json.loads(marker.read_text(encoding="utf-8"))
    lock = workspace_service.load_lock(synthetic_workspace.paths)

    assert payload["generated"] is True
    assert payload["skill_bundle_sha256"] == lock.skill_bundle.sha256
    assert skills_service.inspect_bundle(synthetic_workspace.paths).installed_sha256 == (
        lock.skill_bundle.sha256
    )


def test_doctor_detects_missing_ignore_rules(synthetic_workspace: SyntheticWorkspace) -> None:
    ignore = synthetic_workspace.paths.gitignore
    ignore.write_text(
        "\n".join(
            line
            for line in ignore.read_text(encoding="utf-8").splitlines()
            if line not in {"*.duckdb", "data/"}
        )
        + "\n",
        encoding="utf-8",
    )

    report = workspace_service.doctor(synthetic_workspace.root, clock=synthetic_workspace.clock)

    assert report.ok is False
    assert _statuses(report)["ignore_rules"] == "failed"
    assert "*.duckdb" in _check(report, "ignore_rules").context["missing"]
    assert workspace_service.ignore_rule_gaps(synthetic_workspace.root) != ()


def test_doctor_detects_a_missing_gitignore(synthetic_workspace: SyntheticWorkspace) -> None:
    synthetic_workspace.paths.gitignore.unlink()

    assert GENERATED_START in workspace_service.ignore_rule_gaps(synthetic_workspace.root) or (
        workspace_service.ignore_rule_gaps(synthetic_workspace.root) == ("<missing .gitignore>",)
    )


def test_doctor_detects_a_core_and_schema_mismatch(
    synthetic_workspace: SyntheticWorkspace,
) -> None:
    lock = workspace_service.load_lock(synthetic_workspace.paths)
    drifted = lock.model_copy(
        update={
            "core": lock.core.model_copy(update={"version": "0.0.1", "sha256": "a" * 64}),
            "database_schema": lock.database_schema.model_copy(update={"version": "1"}),
        }
    )
    workspace_service.write_lock(synthetic_workspace.paths, drifted)

    report = workspace_service.doctor(synthetic_workspace.root, clock=synthetic_workspace.clock)
    statuses = _statuses(report)

    assert report.ok is False
    assert statuses["pin_core"] == "failed"
    assert statuses["pin_database_schema"] == "failed"
    assert statuses["lock_mirror"] == "failed"
    assert statuses["installed_versions"] == "failed"


def test_doctor_fails_on_a_backup_root_inside_git(
    synthetic_workspace: SyntheticWorkspace, tmp_path: Path
) -> None:
    """Native databases and portable exports must never sit inside a Git repository."""

    outer = tmp_path / "outer"
    (outer / ".git").mkdir(parents=True)
    (outer / "backups").mkdir()
    configuration = synthetic_workspace.paths.config
    configuration.write_text(
        configuration.read_text(encoding="utf-8").replace(
            str(synthetic_workspace.backup_root), str(outer / "backups")
        ),
        encoding="utf-8",
    )

    report = workspace_service.doctor(synthetic_workspace.root, clock=synthetic_workspace.clock)

    assert report.ok is False
    assert _statuses(report)["backup_root"] == "failed"


def test_initialization_refuses_a_backup_root_inside_git(
    tmp_path: Path, clock: AdvancingClock
) -> None:
    outer = tmp_path / "outer"
    (outer / ".git").mkdir(parents=True)
    target = tmp_path / "PolishLinguaWiki"

    with pytest.raises(LinguaWikiError) as failure:
        workspace_service.initialize(
            workspace_service.InitOptions(
                path=target, backup_root=outer / "backups", name="Polish LinguaWiki"
            ),
            clock=clock,
        )

    assert failure.value.payload.code == "backup_root_invalid"
    assert failure.value.payload.details[0].reason == "inside a Git repository"
    assert not target.exists()


def test_doctor_warns_about_a_missing_backup_root(
    synthetic_workspace: SyntheticWorkspace,
) -> None:
    shutil.rmtree(synthetic_workspace.backup_root)

    report = workspace_service.doctor(synthetic_workspace.root, clock=synthetic_workspace.clock)

    assert _statuses(report)["backup_root_present"] == "warning"
    assert report.ok is True


def test_doctor_detects_a_copy_of_core_source(synthetic_workspace: SyntheticWorkspace) -> None:
    (synthetic_workspace.root / "src" / "linguawiki").mkdir(parents=True)

    report = workspace_service.doctor(synthetic_workspace.root, clock=synthetic_workspace.clock)

    assert report.ok is False
    assert _statuses(report)["no_core_source_copy"] == "failed"


def test_doctor_detects_a_workspace_identity_mismatch(
    synthetic_workspace: SyntheticWorkspace,
) -> None:
    configuration = synthetic_workspace.paths.config
    configuration.write_text(
        configuration.read_text(encoding="utf-8").replace(
            synthetic_workspace.report.workspace_id, "wsp_01ARZ3NDEKTSV4RRFFQ69G5FAV"
        ),
        encoding="utf-8",
    )

    report = workspace_service.doctor(synthetic_workspace.root, clock=synthetic_workspace.clock)

    assert report.ok is False
    assert _statuses(report)["workspace_identity_match"] == "failed"


def test_doctor_reports_an_invalid_configuration_without_continuing(
    synthetic_workspace: SyntheticWorkspace,
) -> None:
    synthetic_workspace.paths.config.write_text("this is not toml =", encoding="utf-8")

    report = workspace_service.doctor(synthetic_workspace.root, clock=synthetic_workspace.clock)

    assert report.ok is False
    assert [check.name for check in report.checks] == ["configuration"]


def test_doctor_reports_a_missing_lock_without_continuing(
    synthetic_workspace: SyntheticWorkspace,
) -> None:
    synthetic_workspace.paths.lock.unlink()

    report = workspace_service.doctor(synthetic_workspace.root, clock=synthetic_workspace.clock)

    assert report.ok is False
    assert [check.name for check in report.checks] == ["configuration", "lock"]


def test_doctor_reports_a_missing_database(synthetic_workspace: SyntheticWorkspace) -> None:
    synthetic_workspace.paths.database.unlink()

    report = workspace_service.doctor(synthetic_workspace.root, clock=synthetic_workspace.clock)

    assert report.ok is False
    assert _statuses(report)["database"] == "failed"


@pytest.mark.skipif(shutil.which("git") is None, reason="git is not installed")
def test_an_unconfirmed_remote_is_a_warning_until_it_is_confirmed(
    synthetic_workspace: SyntheticWorkspace, tmp_path: Path
) -> None:
    root = synthetic_workspace.root
    subprocess.run(["git", "init", "--quiet"], cwd=root, check=True)
    subprocess.run(
        ["git", "remote", "add", "origin", str(tmp_path / "private-remote.git")],
        cwd=root,
        check=True,
    )

    warned = workspace_service.doctor(root, clock=synthetic_workspace.clock)
    assert _statuses(warned)["git_remote_privacy"] == "warning"
    assert warned.ok is True

    workspace_service.confirm_remote(root, private=True, clock=synthetic_workspace.clock)
    confirmed = workspace_service.doctor(root, clock=synthetic_workspace.clock)
    assert _statuses(confirmed)["git_remote_privacy"] == "ok"

    workspace_service.confirm_remote(root, private=False, clock=synthetic_workspace.clock)
    revoked = workspace_service.doctor(root, clock=synthetic_workspace.clock)
    assert _statuses(revoked)["git_remote_privacy"] == "warning"


@pytest.mark.skipif(shutil.which("git") is None, reason="git is not installed")
def test_a_confirmation_does_not_cover_a_remote_added_afterwards(
    synthetic_workspace: SyntheticWorkspace, tmp_path: Path
) -> None:
    """A confirmation is bound to the remotes it covered, not a permanent boolean."""

    root = synthetic_workspace.root
    subprocess.run(["git", "init", "--quiet"], cwd=root, check=True)
    subprocess.run(
        ["git", "remote", "add", "origin", str(tmp_path / "private.git")], cwd=root, check=True
    )
    workspace_service.confirm_remote(root, private=True, clock=synthetic_workspace.clock)
    assert (
        _statuses(workspace_service.doctor(root, clock=synthetic_workspace.clock))[
            "git_remote_privacy"
        ]
        == "ok"
    )

    subprocess.run(
        ["git", "remote", "add", "backup", str(tmp_path / "elsewhere.git")], cwd=root, check=True
    )

    report = workspace_service.doctor(root, clock=synthetic_workspace.clock)
    check = _check(report, "git_remote_privacy")

    assert check.status == "warning"
    assert "unconfirmed remote(s): backup" in check.message


@pytest.mark.skipif(shutil.which("git") is None, reason="git is not installed")
def test_a_confirmation_does_not_survive_repointing_a_remote(
    synthetic_workspace: SyntheticWorkspace, tmp_path: Path
) -> None:
    root = synthetic_workspace.root
    subprocess.run(["git", "init", "--quiet"], cwd=root, check=True)
    subprocess.run(
        ["git", "remote", "add", "origin", str(tmp_path / "private.git")], cwd=root, check=True
    )
    workspace_service.confirm_remote(root, private=True, clock=synthetic_workspace.clock)

    subprocess.run(
        ["git", "remote", "set-url", "origin", "https://example.invalid/public.git"],
        cwd=root,
        check=True,
    )

    check = _check(
        workspace_service.doctor(root, clock=synthetic_workspace.clock), "git_remote_privacy"
    )

    assert check.status == "warning"
    assert "endpoint changed since it was confirmed" in check.message
    assert "origin fetch" in check.message


@pytest.mark.skipif(shutil.which("git") is None, reason="git is not installed")
def test_a_confirmation_made_before_any_remote_existed_is_refused(
    synthetic_workspace: SyntheticWorkspace, tmp_path: Path
) -> None:
    """Confirming with no remote would record a confirmation covering nothing."""

    root = synthetic_workspace.root
    subprocess.run(["git", "init", "--quiet"], cwd=root, check=True)

    with pytest.raises(LinguaWikiError) as failure:
        workspace_service.confirm_remote(root, private=True, clock=synthetic_workspace.clock)

    assert failure.value.payload.code == "no_remote_configured"

    subprocess.run(
        ["git", "remote", "add", "origin", str(tmp_path / "private.git")], cwd=root, check=True
    )
    check = _check(
        workspace_service.doctor(root, clock=synthetic_workspace.clock), "git_remote_privacy"
    )
    assert check.status == "warning"


@pytest.mark.skipif(shutil.which("git") is None, reason="git is not installed")
def test_git_remotes_reports_names_and_urls(
    synthetic_workspace: SyntheticWorkspace, tmp_path: Path
) -> None:
    root = synthetic_workspace.root
    subprocess.run(["git", "init", "--quiet"], cwd=root, check=True)
    subprocess.run(
        ["git", "remote", "add", "origin", str(tmp_path / "private.git")], cwd=root, check=True
    )

    assert workspace_service.git_remotes(root) == {
        "origin": {
            "fetch": [str(tmp_path / "private.git")],
            "push": [str(tmp_path / "private.git")],
        }
    }


@pytest.mark.skipif(shutil.which("git") is None, reason="git is not installed")
def test_a_confirmation_covers_the_push_endpoint_too(
    synthetic_workspace: SyntheticWorkspace, tmp_path: Path
) -> None:
    """A repository can fetch privately and push somewhere public."""

    root = synthetic_workspace.root
    subprocess.run(["git", "init", "--quiet"], cwd=root, check=True)
    subprocess.run(
        ["git", "remote", "add", "origin", str(tmp_path / "private.git")], cwd=root, check=True
    )
    workspace_service.confirm_remote(root, private=True, clock=synthetic_workspace.clock)

    subprocess.run(
        ["git", "remote", "set-url", "--push", "origin", "https://example.invalid/PUBLIC.git"],
        cwd=root,
        check=True,
    )

    check = _check(
        workspace_service.doctor(root, clock=synthetic_workspace.clock), "git_remote_privacy"
    )

    assert check.status == "warning"
    assert "origin push" in check.message
    assert "example.invalid/PUBLIC.git" in check.context["remotes"]


@pytest.mark.skipif(shutil.which("git") is None, reason="git is not installed")
def test_every_push_destination_is_covered_not_just_the_last(
    synthetic_workspace: SyntheticWorkspace, tmp_path: Path
) -> None:
    """Git allows several pushurl values; keeping only one hid the others."""

    root = synthetic_workspace.root
    private = str(tmp_path / "private.git")
    subprocess.run(["git", "init", "--quiet"], cwd=root, check=True)
    subprocess.run(["git", "remote", "add", "origin", private], cwd=root, check=True)
    workspace_service.confirm_remote(root, private=True, clock=synthetic_workspace.clock)

    for url in ("https://example.invalid/PUBLIC.git", private):
        subprocess.run(
            ["git", "config", "--add", "remote.origin.pushurl", url], cwd=root, check=True
        )

    remotes = workspace_service.git_remotes(root)
    check = _check(
        workspace_service.doctor(root, clock=synthetic_workspace.clock), "git_remote_privacy"
    )

    assert remotes["origin"]["push"] == ["https://example.invalid/PUBLIC.git", private]
    assert check.status == "warning"
    assert "origin push" in check.message
    assert "example.invalid/PUBLIC.git" in check.context["remotes"]


@pytest.mark.skipif(shutil.which("git") is None, reason="git is not installed")
def test_confirming_every_push_destination_clears_the_warning(
    synthetic_workspace: SyntheticWorkspace, tmp_path: Path
) -> None:
    root = synthetic_workspace.root
    subprocess.run(["git", "init", "--quiet"], cwd=root, check=True)
    subprocess.run(
        ["git", "remote", "add", "origin", str(tmp_path / "private.git")], cwd=root, check=True
    )
    subprocess.run(
        ["git", "config", "--add", "remote.origin.pushurl", str(tmp_path / "mirror.git")],
        cwd=root,
        check=True,
    )

    workspace_service.confirm_remote(root, private=True, clock=synthetic_workspace.clock)

    assert (
        _statuses(workspace_service.doctor(root, clock=synthetic_workspace.clock))[
            "git_remote_privacy"
        ]
        == "ok"
    )


@pytest.mark.skipif(shutil.which("git") is None, reason="git is not installed")
def test_remote_credentials_are_never_persisted(
    synthetic_workspace: SyntheticWorkspace,
) -> None:
    """The confirmation stores sanitized displays and fingerprints, not raw URLs."""

    root = synthetic_workspace.root
    secret = "s3cret-token"
    subprocess.run(["git", "init", "--quiet"], cwd=root, check=True)
    subprocess.run(
        ["git", "remote", "add", "origin", f"https://learner:{secret}@example.invalid/w.git"],
        cwd=root,
        check=True,
    )

    workspace_service.confirm_remote(root, private=True, clock=synthetic_workspace.clock)
    report = workspace_service.doctor(root, clock=synthetic_workspace.clock)
    stored = (synthetic_workspace.paths.database).read_bytes().decode("latin-1")

    assert _statuses(report)["git_remote_privacy"] == "ok"
    assert secret not in stored
    assert secret not in _check(report, "git_remote_privacy").context.get("remotes", "")


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        ("https://user:token@example.invalid/w.git", "https://example.invalid/w.git"),
        ("https://example.invalid/w.git", "https://example.invalid/w.git"),
        ("ssh://git@example.invalid:2222/w.git", "ssh://example.invalid:2222/w.git"),
        ("git@example.invalid:learner/w.git", "example.invalid:learner/w.git"),
        ("/local/path/w.git", "/local/path/w.git"),
    ],
)
def test_remote_urls_are_sanitized_before_they_are_stored(url: str, expected: str) -> None:
    assert workspace_service.sanitize_remote_url(url) == expected
    assert workspace_service.remote_fingerprint(url) != workspace_service.remote_fingerprint(
        f"{url}x"
    )


@pytest.mark.skipif(shutil.which("git") is None, reason="git is not installed")
def test_doctor_fails_when_git_cannot_list_candidates(
    synthetic_workspace: SyntheticWorkspace, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A force-added database must never be reported safe because git errored."""

    root = synthetic_workspace.root
    subprocess.run(["git", "init", "--quiet"], cwd=root, check=True)
    subprocess.run(["git", "add", "-f", "data/linguawiki.duckdb"], cwd=root, check=True)
    failing = tmp_path / "bin"
    failing.mkdir()
    (failing / "git").write_text("#!/bin/sh\nprintf 'fatal: broken\\n' >&2\nexit 128\n")
    (failing / "git").chmod(0o755)
    monkeypatch.setenv("PATH", str(failing))

    report = workspace_service.privacy_check(root)
    doctor = workspace_service.doctor(root, clock=synthetic_workspace.clock)

    assert report.ok is False
    assert report.source == "unavailable"
    assert report.candidates_checked == 0
    assert doctor.ok is False
    assert _statuses(doctor)["privacy_inspection"] == "failed"


@pytest.mark.skipif(shutil.which("git") is None, reason="git is not installed")
def test_privacy_check_inspects_an_ancestor_git_repository(
    tmp_path: Path, backup_root: Path, clock: AdvancingClock
) -> None:
    """A workspace nested in another repository is still tracked by that repository."""

    parent = tmp_path / "parent"
    parent.mkdir()
    subprocess.run(["git", "init", "--quiet"], cwd=parent, check=True)
    nested = parent / "PolishLinguaWiki"
    workspace_service.initialize(
        workspace_service.InitOptions(
            path=nested, backup_root=backup_root, name="Polish LinguaWiki"
        ),
        clock=clock,
    )
    subprocess.run(
        ["git", "add", "-f", "PolishLinguaWiki/data/linguawiki.duckdb"], cwd=parent, check=True
    )

    report = workspace_service.privacy_check(nested)
    doctor = workspace_service.doctor(nested, clock=clock)

    assert report.source == "git"
    assert report.git_root == str(parent.resolve())
    assert report.ok is False
    assert {item.path for item in report.violations} == {"data/linguawiki.duckdb"}
    assert doctor.ok is False
    assert _statuses(doctor)["privacy_candidates"] == "failed"
    assert _statuses(doctor)["workspace_nesting"] == "warning"


@pytest.mark.skipif(shutil.which("git") is None, reason="git is not installed")
def test_a_nested_workspace_with_nothing_force_added_is_clean(
    tmp_path: Path, backup_root: Path, clock: AdvancingClock
) -> None:
    """The nesting itself is a warning, not a violation: .gitignore still applies."""

    parent = tmp_path / "parent"
    parent.mkdir()
    subprocess.run(["git", "init", "--quiet"], cwd=parent, check=True)
    nested = parent / "PolishLinguaWiki"
    workspace_service.initialize(
        workspace_service.InitOptions(
            path=nested, backup_root=backup_root, name="Polish LinguaWiki"
        ),
        clock=clock,
    )

    report = workspace_service.privacy_check(nested)

    assert report.source == "git"
    assert report.violations == ()
    assert "wiki/index.md" in report.warnings or report.ok is True
    doctor = workspace_service.doctor(nested, clock=clock)
    assert doctor.ok is True
    assert _statuses(doctor)["workspace_nesting"] == "warning"


def test_doctor_reports_an_unrecognized_database_instead_of_crashing(
    synthetic_workspace: SyntheticWorkspace,
) -> None:
    """Doctor used to raise a raw catalog error on a database it did not create."""

    import duckdb

    synthetic_workspace.paths.database.unlink()
    connection = duckdb.connect(str(synthetic_workspace.paths.database))
    connection.execute("CREATE TABLE somebody_elses_data(id INTEGER)")
    connection.close()

    report = workspace_service.doctor(synthetic_workspace.root, clock=synthetic_workspace.clock)

    assert report.ok is False
    assert _statuses(report)["database_recognized"] == "failed"
    assert "somebody_elses_data" in _check(report, "database_recognized").context["tables"]


@pytest.mark.skipif(shutil.which("git") is None, reason="git is not installed")
def test_confirm_remote_refuses_a_damaged_database(
    synthetic_workspace: SyntheticWorkspace, tmp_path: Path
) -> None:
    """Enforcement lives at the writer boundary, so no command can forget it."""

    from linguawiki.db.connection import open_writer

    root = synthetic_workspace.root
    subprocess.run(["git", "init", "--quiet"], cwd=root, check=True)
    subprocess.run(
        ["git", "remote", "add", "origin", str(tmp_path / "private.git")], cwd=root, check=True
    )
    with (
        open_writer(
            synthetic_workspace.paths, command="test", clock=synthetic_workspace.clock
        ) as database,
        database.transaction() as transaction,
    ):
        transaction.execute("CREATE TABLE hand_made(id VARCHAR)")

    with pytest.raises(LinguaWikiError) as failure:
        workspace_service.confirm_remote(
            synthetic_workspace.root, private=True, clock=synthetic_workspace.clock
        )

    # An extra table has a valid history, so it is ours-but-damaged, not foreign.
    assert failure.value.payload.code == "database_damaged"


def test_confirm_remote_requires_an_initialized_workspace(tmp_path: Path) -> None:
    with pytest.raises(LinguaWikiError) as failure:
        workspace_service.confirm_remote(tmp_path, private=True)

    assert failure.value.payload.code == "workspace_not_initialized"


@pytest.mark.skipif(shutil.which("git") is None, reason="git is not installed")
def test_privacy_check_rejects_force_added_private_artifacts(
    synthetic_workspace: SyntheticWorkspace,
) -> None:
    root = synthetic_workspace.root
    subprocess.run(["git", "init", "--quiet"], cwd=root, check=True)
    (root / "wiki" / "clip.wav").write_bytes(b"fake audio")
    (root / "wiki" / "transcripts" / "raw").mkdir(parents=True)
    (root / "wiki" / "transcripts" / "raw" / "session.md").write_text("raw", encoding="utf-8")
    (root / "wiki" / "copy.duckdb").write_bytes(b"fake database")
    subprocess.run(
        [
            "git",
            "add",
            "-f",
            "wiki/clip.wav",
            "wiki/transcripts/raw/session.md",
            "wiki/copy.duckdb",
        ],
        cwd=root,
        check=True,
    )

    report = workspace_service.privacy_check(root)
    doctor = workspace_service.doctor(root, clock=synthetic_workspace.clock)

    assert report.source == "git"
    assert report.ok is False
    assert {item.path for item in report.violations} == {
        "wiki/clip.wav",
        "wiki/transcripts/raw/session.md",
        "wiki/copy.duckdb",
    }
    assert doctor.ok is False
    assert _statuses(doctor)["privacy_candidates"] == "failed"


@pytest.mark.skipif(shutil.which("git") is None, reason="git is not installed")
def test_privacy_check_rejects_unexpected_top_level_entries(
    synthetic_workspace: SyntheticWorkspace,
) -> None:
    """An untracked top-level file is unreviewed content, not a safe Git candidate."""

    root = synthetic_workspace.root
    subprocess.run(["git", "init", "--quiet"], cwd=root, check=True)
    (root / "learner-notes.txt").write_text("private thoughts", encoding="utf-8")
    (root / "scratch").mkdir()
    (root / "scratch" / "todo.md").write_text("todo", encoding="utf-8")

    report = workspace_service.privacy_check(root)
    doctor = workspace_service.doctor(root, clock=synthetic_workspace.clock)

    assert report.ok is False
    assert {item.path for item in report.violations} == {
        "learner-notes.txt",
        "scratch/todo.md",
    }
    assert {item.reason for item in report.violations} == {"outside the Git-safe top level"}
    assert doctor.ok is False
    assert _statuses(doctor)["privacy_candidates"] == "failed"


@pytest.mark.skipif(shutil.which("git") is None, reason="git is not installed")
def test_every_generated_file_is_an_acceptable_git_candidate(
    synthetic_workspace: SyntheticWorkspace,
) -> None:
    root = synthetic_workspace.root
    subprocess.run(["git", "init", "--quiet"], cwd=root, check=True)

    report = workspace_service.privacy_check(root)

    assert report.ok is True
    assert report.candidates_checked > 0


@pytest.mark.skipif(shutil.which("git") is None, reason="git is not installed")
def test_private_artifacts_are_not_git_candidates_by_default(
    synthetic_workspace: SyntheticWorkspace,
) -> None:
    root = synthetic_workspace.root
    subprocess.run(["git", "init", "--quiet"], cwd=root, check=True)
    (root / "artifacts" / "recording.wav").write_bytes(b"fake audio")
    (root / "imports" / "package.json").write_text("{}", encoding="utf-8")
    (root / "drafts" / "draft.md").write_text("draft", encoding="utf-8")
    (root / "exports" / "anki" / "deck.tsv").write_text("a\tb\n", encoding="utf-8")

    report = workspace_service.privacy_check(root)
    candidates = set(report.violations)

    assert report.ok is True
    assert candidates == set()
    tracked = subprocess.run(
        ["git", "ls-files", "--others", "--exclude-standard"],
        cwd=root,
        capture_output=True,
        text=True,
        check=True,
    ).stdout.splitlines()
    assert "data/linguawiki.duckdb" not in tracked
    assert "artifacts/recording.wav" not in tracked
    assert "drafts/draft.md" not in tracked
    assert "wiki/index.md" in tracked
