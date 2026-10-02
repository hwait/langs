"""CLI surface with a stable JSON envelope for people and agents."""

from __future__ import annotations

import argparse
import json
import os
import sys
import traceback
from collections.abc import Sequence
from datetime import datetime
from pathlib import Path
from typing import Any, Never

from pydantic import BaseModel, ValidationError

from linguawiki import __version__, error_model
from linguawiki import evidence as evidence_module
from linguawiki import placement as placement_module
from linguawiki import session as session_policy
from linguawiki import sources as source_policy
from linguawiki import transcripts as transcript_policy
from linguawiki.clock import Clock, SystemClock
from linguawiki.contracts import (
    ErrorEnvelope,
    GenericSuccessEnvelope,
    StatusData,
    StatusEnvelope,
)
from linguawiki.db import backup as backup_module
from linguawiki.db import migrations as migration_module
from linguawiki.errors import ErrorDetail, LinguaWikiError
from linguawiki.ids import EventId
from linguawiki.packs import stamp as stamp_module
from linguawiki.paths import require_initialized_workspace, workspace_paths
from linguawiki.services import artifacts as artifact_service
from linguawiki.services import assessment as assessment_service
from linguawiki.services import assessment_view as view_service
from linguawiki.services import authoring as authoring_service
from linguawiki.services import context as context_service
from linguawiki.services import curriculum as curriculum_service
from linguawiki.services import database as database_service
from linguawiki.services import errors as error_service
from linguawiki.services import estimates as estimate_service
from linguawiki.services import evidence as evidence_service
from linguawiki.services import knowledge as knowledge_service
from linguawiki.services import learners as learner_service
from linguawiki.services import onboarding as onboarding_service
from linguawiki.services import packs as pack_service
from linguawiki.services import privacy as privacy_service
from linguawiki.services import recordings as recording_service
from linguawiki.services import resources as resource_service
from linguawiki.services import sessions as session_service
from linguawiki.services import skills as skills_service
from linguawiki.services import sources as source_service
from linguawiki.services import speaking as speaking_service
from linguawiki.services import transcripts as transcript_service
from linguawiki.services import wiki as wiki_service
from linguawiki.services import workspace as workspace_service

FORMATS = ("human", "json")
# 0 succeeded, 1 ran but reported failures, 2 the command itself failed.
EXIT_REPORTED_FAILURE = 1
EXIT_ERROR = 2


class ParserExit(Exception):
    def __init__(self, status: int, message: str | None) -> None:
        super().__init__(message)
        self.status = status
        self.message = message


class ContractArgumentParser(argparse.ArgumentParser):
    def error(self, message: str) -> Never:
        raise LinguaWikiError("invalid_arguments", message)

    def exit(self, status: int = 0, message: str | None = None) -> Never:
        raise ParserExit(status, message)


def _add_format(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--format", choices=FORMATS, default="human")


def _add_workspace(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--workspace", default=".", help="learner workspace root")
    _add_format(parser)


def _workspace_parser(subcommands: Any) -> None:
    workspace = subcommands.add_parser("workspace", help="create and diagnose learner workspaces")
    actions = workspace.add_subparsers(dest="action", required=True)
    initialize = actions.add_parser("init", help="render an independent learner workspace")
    initialize.add_argument("path")
    initialize.add_argument("--backup-root", required=True, help="verified backup root outside Git")
    initialize.add_argument("--name")
    initialize.add_argument("--timezone", default="UTC")
    initialize.add_argument(
        "--history",
        default="git-wiki",
        help="learner history policy; the frozen contract supports git-wiki",
    )
    initialize.add_argument("--git-init", action="store_true")
    initialize.add_argument(
        "--uv-lock",
        action="store_true",
        help="resolve the workspace uv.lock with uv",
    )
    initialize.add_argument(
        "--find-links",
        help="extra artifact directory uv may resolve the pinned core from",
    )
    _add_format(initialize)
    for name, help_text in (
        ("status", "show workspace identity, pins, and database state"),
        ("doctor", "run every workspace diagnostic"),
        ("privacy-check", "list private files that could reach Git"),
    ):
        _add_workspace(actions.add_parser(name, help=help_text))
    lock_dependencies = actions.add_parser(
        "lock-dependencies", help="resolve the workspace uv.lock with uv"
    )
    lock_dependencies.add_argument(
        "--find-links",
        help="extra artifact directory uv may resolve the pinned core from",
    )
    _add_workspace(lock_dependencies)
    confirm = actions.add_parser("confirm-remote", help="record a Git remote privacy decision")
    confirm.add_argument("--private", dest="private", action="store_true", default=None)
    confirm.add_argument("--not-private", dest="private", action="store_false")
    _add_workspace(confirm)


def _db_parser(subcommands: Any) -> None:
    database = subcommands.add_parser("db", help="learner database lifecycle")
    actions = database.add_subparsers(dest="action", required=True)
    for name, help_text in (
        ("init", "create and migrate the learner database"),
        ("status", "show applied and pending migrations"),
        ("check", "run integrity and drift checks"),
    ):
        _add_workspace(actions.add_parser(name, help=help_text))
    migrate = actions.add_parser("migrate", help="apply pending migrations")
    migrate.add_argument("--dry-run", action="store_true")
    _add_workspace(migrate)
    backup = actions.add_parser("backup", help="create a verified native and portable backup")
    backup.add_argument("--reason", default="manual")
    layers = backup.add_mutually_exclusive_group()
    layers.add_argument("--native-only", action="store_true")
    layers.add_argument("--portable-only", action="store_true")
    _add_workspace(backup)
    export = actions.add_parser("export-portable", help="write a standalone portable export")
    export.add_argument("--to", required=True)
    _add_workspace(export)
    restore = actions.add_parser("restore", help="restore a backup into a new path")
    restore.add_argument("--from", dest="source", required=True)
    restore.add_argument("--to", dest="target", required=True)
    restore.add_argument("--kind", choices=("auto", "native", "portable"), default="auto")
    _add_workspace(restore)


def _skills_parser(subcommands: Any) -> None:
    skills = subcommands.add_parser("skills", help="generated Codex skill snapshot")
    actions = skills.add_subparsers(dest="action", required=True)
    for name, help_text in (
        ("install", "regenerate the committed skill snapshot"),
        ("check", "verify the snapshot against the pinned bundle"),
    ):
        _add_workspace(actions.add_parser(name, help=help_text))


def _add_track_selector(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--track", help="learning track; the only active track by default")


def _add_input(parser: argparse.ArgumentParser, *, required: bool = True) -> None:
    """Structured payloads arrive through a file or stdin, never as a shell argument.

    Learner text and pack drafts are arbitrary Unicode; passing them as argv leaves them
    in shell history and breaks on quoting.
    """

    parser.add_argument(
        "--input",
        dest="input_path",
        required=required,
        help="JSON payload file, or - to read stdin",
    )


def _read_input(path: str | None) -> Any:
    if path is None:
        raise LinguaWikiError("invalid_arguments", "this command needs --input")
    text = sys.stdin.read() if path == "-" else Path(path).expanduser().read_text(encoding="utf-8")
    try:
        return json.loads(text)
    except json.JSONDecodeError as exc:
        raise LinguaWikiError(
            "invalid_input",
            "the --input payload is not valid JSON",
            details=(ErrorDetail(field="input", reason=str(exc)),),
        ) from exc


def _pack_parser(subcommands: Any) -> None:
    pack = subcommands.add_parser("pack", help="validate, install, measure, and author packs")
    actions = pack.add_subparsers(dest="action", required=True)
    scaffold = actions.add_parser("scaffold", help="create the declared pack structure")
    scaffold.add_argument("path", help="new, empty directory for the pack")
    scaffold.add_argument("--pack-key", required=True)
    scaffold.add_argument("--name", required=True, help="display name")
    scaffold.add_argument("--language", required=True, help="BCP-47 target language tag")
    scaffold.add_argument("--framework", required=True, help="proficiency framework identifier")
    scaffold.add_argument("--framework-name", required=True)
    scaffold.add_argument("--framework-version", required=True)
    scaffold.add_argument(
        "--level",
        action="append",
        default=[],
        required=True,
        help="framework level, in order, repeated",
    )
    scaffold.add_argument(
        "--band", action="append", default=[], required=True, help="level this pack will cover"
    )
    scaffold.add_argument(
        "--theme", action="append", default=[], required=True, help="practical theme"
    )
    scaffold.add_argument("--support", action="append", default=[], help="support language tag")
    scaffold.add_argument("--maintainer", action="append", default=[])
    scaffold.add_argument("--license", dest="license_name", default="CC-BY-4.0")
    _add_format(scaffold)
    validate = actions.add_parser("validate", help="fully validate a pack directory")
    validate.add_argument("pack", help="pack directory, or the key of a bundled pack")
    _add_format(validate)
    coverage = actions.add_parser("coverage", help="measure coverage and maturity gates")
    coverage.add_argument("pack")
    _add_format(coverage)
    publish = actions.add_parser("publish", help="stamp checksums after the maturity gate passes")
    publish.add_argument("pack")
    publish.add_argument("--maturity", help="maturity to publish as; the manifest's by default")
    _add_format(publish)
    stamp = actions.add_parser("stamp", help="recompute the content hashes a pack declares")
    stamp.add_argument("pack")
    stamp.add_argument("--check", action="store_true", help="fail instead of rewriting")
    _add_format(stamp)
    install = actions.add_parser("install", help="install a pack into the learner database")
    install.add_argument("pack")
    install.add_argument("--dry-run", action="store_true")
    _add_workspace(install)
    update = actions.add_parser("update", help="update an installed pack to another version")
    update.add_argument("pack")
    update.add_argument("--apply", action="store_true", help="apply after reviewing the preview")
    _add_workspace(update)
    diff = actions.add_parser("diff", help="preview what installing a pack would change")
    diff.add_argument("pack")
    _add_workspace(diff)
    _add_workspace(actions.add_parser("list", help="list installed packs"))
    _pack_author_parser(actions)
    _pack_template_parser(actions)


def _pack_author_parser(actions: Any) -> None:
    author = actions.add_parser("author", help="draft, review, and promote pack content")
    author_actions = author.add_subparsers(dest="author_action", required=True)
    draft = author_actions.add_parser(
        "generate-draft", help="open a bounded drafting batch; it never approves output"
    )
    draft.add_argument("--template-key", required=True)
    draft.add_argument("--template-version", type=int, required=True)
    draft.add_argument("--count", type=int, required=True, help="items the batch may hold")
    draft.add_argument("--pack")
    draft.add_argument("--provider")
    draft.add_argument("--model")
    draft.add_argument("--model-version")
    draft.add_argument("--privacy", choices=("public", "private", "synthetic"), default="public")
    _add_workspace(draft)
    imported = author_actions.add_parser("import", help="record drafted or imported items")
    _add_input(imported)
    imported.add_argument("--batch", help="generation batch these items came from")
    imported.add_argument("--pack")
    imported.add_argument(
        "--origin",
        default="human-authored",
        help="origin class for a non-batch import",
    )
    imported.add_argument("--rights", default="pack content, not cleared for redistribution")
    imported.add_argument("--privacy", choices=("public", "private", "synthetic"), default="public")
    _add_workspace(imported)
    queue = author_actions.add_parser("review-queue", help="unfinished content, worst first")
    queue.add_argument("--pack")
    queue.add_argument("--limit", type=int, default=authoring_service.DEFAULT_QUEUE_LIMIT)
    _add_workspace(queue)
    review = author_actions.add_parser("review", help="record one review axis")
    review.add_argument("--content", required=True)
    review.add_argument("--axis", required=True)
    review.add_argument("--state", required=True)
    review.add_argument("--reviewer-kind", required=True)
    review.add_argument("--reviewer")
    review.add_argument("--method")
    review.add_argument("--evidence")
    review.add_argument("--inspection", choices=("accepted", "defective"))
    review.add_argument("--finding")
    _add_workspace(review)
    approve = author_actions.add_parser("approve", help="promote content through its gate")
    approve.add_argument("--content", required=True)
    approve.add_argument(
        "--lifecycle",
        default="approved-personal",
        choices=("approved-personal", "verified", "publication-ready"),
    )
    _add_workspace(approve)
    reject = author_actions.add_parser("reject", help="reject content, keeping its reviews")
    reject.add_argument("--content", required=True)
    reject.add_argument("--reason", required=True)
    _add_workspace(reject)
    invalidate = author_actions.add_parser(
        "invalidate", help="mark content and its dependents needs-review"
    )
    invalidate.add_argument("--content", action="append", default=[])
    invalidate.add_argument("--batch")
    invalidate.add_argument("--reason", required=True)
    _add_workspace(invalidate)


def _pack_template_parser(actions: Any) -> None:
    template = actions.add_parser("template", help="manage generation-template sampling")
    template_actions = template.add_subparsers(dest="template_action", required=True)
    validate = template_actions.add_parser(
        "validate", help="register or re-validate a versioned template"
    )
    _add_input(validate, required=False)
    validate.add_argument("--template-key")
    validate.add_argument("--template-version", type=int)
    _add_workspace(validate)
    stabilize = template_actions.add_parser(
        "stabilize", help="mark a template stable after three clean inspected runs"
    )
    stabilize.add_argument("--template-key", required=True)
    stabilize.add_argument("--template-version", type=int, required=True)
    _add_workspace(stabilize)
    quarantine = template_actions.add_parser(
        "quarantine", help="quarantine a template and everything it produced"
    )
    quarantine.add_argument("--template-key", required=True)
    quarantine.add_argument("--template-version", type=int, required=True)
    quarantine.add_argument("--reason", required=True)
    _add_workspace(quarantine)


def _user_parser(subcommands: Any) -> None:
    user = subcommands.add_parser("user", help="learner profiles")
    actions = user.add_subparsers(dest="action", required=True)
    create = actions.add_parser("create", help="create a learner")
    create.add_argument("--name", required=True)
    create.add_argument("--timezone", required=True, help="IANA timezone of the learner")
    create.add_argument("--native", action="append", default=[], help="native language tag")
    create.add_argument("--support", action="append", default=[], help="support language tag")
    _add_workspace(create)
    update = actions.add_parser("update", help="change a learner profile")
    update.add_argument("--user")
    update.add_argument("--name")
    update.add_argument("--timezone")
    update.add_argument("--native", action="append")
    update.add_argument("--support", action="append")
    update.add_argument("--status", choices=("active", "archived"))
    _add_workspace(update)
    show = actions.add_parser("show", help="show one learner")
    show.add_argument("--user")
    _add_workspace(show)
    _add_workspace(actions.add_parser("list", help="list learners"))


def _track_parser(subcommands: Any) -> None:
    track = subcommands.add_parser("track", help="learning tracks")
    actions = track.add_subparsers(dest="action", required=True)
    create = actions.add_parser("create", help="bind a learner to one target language")
    create.add_argument("--target-language", required=True)
    create.add_argument("--framework", required=True, help="a framework the pack declares")
    create.add_argument("--user")
    create.add_argument("--pack")
    create.add_argument("--region")
    create.add_argument("--script")
    create.add_argument("--declared-level")
    create.add_argument("--target-level")
    create.add_argument("--goal")
    _add_input(create, required=False)
    _add_workspace(create)
    update = actions.add_parser("update", help="change a track's goal, target, or preferences")
    _add_track_selector(update)
    update.add_argument("--goal")
    update.add_argument("--target-level")
    update.add_argument("--declared-level")
    _add_input(update, required=False)
    _add_workspace(update)
    show = actions.add_parser("show", help="show one track")
    _add_track_selector(show)
    _add_workspace(show)
    _add_workspace(actions.add_parser("list", help="list tracks"))
    for name, help_text in (
        ("activate", "make a track active"),
        ("pause", "pause a track"),
        ("archive", "archive a track"),
    ):
        entry = actions.add_parser(name, help=help_text)
        _add_track_selector(entry)
        _add_workspace(entry)


def _onboard_parser(subcommands: Any) -> None:
    onboard = subcommands.add_parser("onboard", help="resumable language onboarding")
    actions = onboard.add_subparsers(dest="action", required=True)
    start = actions.add_parser("start", help="open onboarding and seed provisional estimates")
    _add_track_selector(start)
    start.add_argument("--mode", choices=("declared-level", "placement"), default="declared-level")
    start.add_argument("--declared-level")
    start.add_argument("--idempotency-key")
    _add_workspace(start)
    record = actions.add_parser("record", help="record one self-report answer")
    record.add_argument("--key", required=True)
    _add_input(record)
    record.add_argument("--onboarding")
    _add_track_selector(record)
    _add_workspace(record)
    status = actions.add_parser("status", help="show onboarding state and the next step")
    status.add_argument("--onboarding")
    _add_track_selector(status)
    _add_workspace(status)
    finalize = actions.add_parser(
        "finalize", help="prepare resources and build the calibration queue"
    )
    finalize.add_argument("--onboarding")
    _add_track_selector(finalize)
    finalize.add_argument("--weeks", type=int, default=resource_service.DEFAULT_WEEKS)
    finalize.add_argument("--item-budget", type=int, default=resource_service.DEFAULT_ITEM_BUDGET)
    finalize.add_argument(
        "--calibration-sample", type=int, default=onboarding_service.CALIBRATION_SAMPLE
    )
    finalize.add_argument("--no-calibration", action="store_true")
    _add_workspace(finalize)
    abandon = actions.add_parser("abandon", help="abandon onboarding, keeping its answers")
    abandon.add_argument("--onboarding")
    _add_track_selector(abandon)
    _add_workspace(abandon)


def _resources_parser(subcommands: Any) -> None:
    resources = subcommands.add_parser("resources", help="bounded resource preparation")
    actions = resources.add_subparsers(dest="action", required=True)
    for name, help_text in (
        ("plan", "show the plan without writing it"),
        ("prepare", "apply the plan and import reference items as unseen"),
    ):
        entry = actions.add_parser(name, help=help_text)
        _add_track_selector(entry)
        entry.add_argument(
            "--mode", choices=("declared-level", "placement"), default="declared-level"
        )
        entry.add_argument(
            "--level", action="append", default=[], help="framework level to prepare"
        )
        entry.add_argument("--weeks", type=int, default=resource_service.DEFAULT_WEEKS)
        entry.add_argument("--item-budget", type=int, default=resource_service.DEFAULT_ITEM_BUDGET)
        if name == "prepare":
            entry.add_argument("--dry-run", action="store_true")
        _add_workspace(entry)
    status = actions.add_parser("status", help="show the applied plan")
    _add_track_selector(status)
    _add_workspace(status)


def _curriculum_parser(subcommands: Any) -> None:
    curriculum = subcommands.add_parser("curriculum", help="prior-course import and audit")
    actions = curriculum.add_subparsers(dest="action", required=True)
    imported = actions.add_parser("import", help="store a permissible course outline")
    _add_input(imported)
    _add_track_selector(imported)
    _add_workspace(imported)
    show = actions.add_parser("show", help="show an imported outline and its gaps")
    show.add_argument("--curriculum")
    _add_track_selector(show)
    _add_workspace(show)
    position = actions.add_parser("position", help="record which units the learner claims")
    position.add_argument("--completed", action="append", default=[], help="unit code")
    position.add_argument("--current", action="append", default=[], help="unit code")
    position.add_argument("--curriculum")
    _add_track_selector(position)
    _add_workspace(position)
    audit_start = actions.add_parser("audit-start", help="select a risk-weighted audit sample")
    audit_start.add_argument("--curriculum")
    audit_start.add_argument(
        "--sample-size", type=int, default=curriculum_service.DEFAULT_AUDIT_SAMPLE
    )
    audit_start.add_argument("--idempotency-key")
    _add_track_selector(audit_start)
    _add_workspace(audit_start)
    audit_record = actions.add_parser("audit-record", help="record a batch of audit results")
    _add_input(audit_record)
    audit_record.add_argument("--audit")
    _add_track_selector(audit_record)
    _add_workspace(audit_record)
    audit_finalize = actions.add_parser(
        "audit-finalize", help="turn audit misses into an evidence-gap queue"
    )
    audit_finalize.add_argument("--audit")
    audit_finalize.add_argument("--stop-reason", default="completed")
    _add_track_selector(audit_finalize)
    _add_workspace(audit_finalize)
    audit_report = actions.add_parser("audit-report", help="show an audit's state")
    audit_report.add_argument("--audit")
    _add_track_selector(audit_report)
    _add_workspace(audit_report)


def _client_parser(subcommands: Any) -> None:
    client = subcommands.add_parser("client", help="the local browser client")
    actions = client.add_subparsers(dest="action", required=True)
    serve = actions.add_parser("serve", help="serve the client on loopback until interrupted")
    serve.add_argument(
        "--port",
        type=int,
        default=0,
        help="port to bind; 0 asks the operating system for a free one",
    )
    serve.add_argument(
        "--no-open",
        action="store_true",
        help="print the launch URL instead of opening a browser",
    )
    serve.add_argument(
        "--run",
        help="open this run in the page; without it the page offers the newest resumable run",
    )
    _add_workspace(serve)


def _assessment_parser(subcommands: Any) -> None:
    assessment = subcommands.add_parser("assessment", help="calibration and placement runs")
    actions = assessment.add_subparsers(dest="action", required=True)
    start = actions.add_parser("start", help="open a bounded calibration or placement run")
    _add_track_selector(start)
    start.add_argument(
        "--run-type", choices=("pilot-calibration", "placement"), default="pilot-calibration"
    )
    start.add_argument("--dimension", action="append", default=[])
    start.add_argument("--modality", action="append", default=[])
    start.add_argument(
        "--scoring",
        choices=placement_module.SCORING_CONDITIONS,
        default=placement_module.DEFAULT_SCORING,
        help="machine: serve only tasks the server can score without a judge",
    )
    start.add_argument("--idempotency-key")
    _add_workspace(start)
    nxt = actions.add_parser("next", help="serve the next task")
    nxt.add_argument("--run")
    _add_track_selector(nxt)
    # Serving is a mutation: it spends an item's exposure. Without a key a retry after a
    # lost response consumed a second task and burned a second item.
    nxt.add_argument("--idempotency-key")
    _add_workspace(nxt)
    record = actions.add_parser("record", help="score one served task")
    record.add_argument("--run")
    _add_track_selector(record)
    record.add_argument("--content", required=True)
    # Optional now: a machine-scorable task is scored from the key the run snapshotted,
    # and a supplied score is the compatibility path rather than the default one.
    record.add_argument("--score", type=float)
    _add_input(record, required=False)
    # The learner's whole answer, scored in memory. It reaches the database only through
    # the retention rule, which is why it is separate from --excerpt: an excerpt is a
    # caller's own choice of what to keep, and passing both would be two accounts of one
    # answer. Long answers go in --response-file, because argv is bounded text.
    record.add_argument("--response")
    record.add_argument(
        "--response-file",
        help="file holding the learner's response, or - to read stdin",
    )
    record.add_argument(
        "--response-visibility", choices=("withheld", "excerpt", "full"), default=None
    )
    record.add_argument("--excerpt")
    record.add_argument(
        "--assessor-kind",
        choices=("deterministic", "ai", "learner", "human"),
        default="deterministic",
    )
    record.add_argument("--assessor")
    record.add_argument("--confidence", choices=("low", "medium", "high"), default="medium")
    # The recording a judge listened to. A spoken task answered by a recording takes a
    # verdict only from a judge who names the one the learner submitted.
    record.add_argument("--audio-artifact")
    # The submission a judge was handed, and the claim it was handed under. A verdict
    # naming a submission is revalidated against it when it lands, and held rather than
    # refused if the run has been paused since.
    record.add_argument("--submission")
    record.add_argument("--claim")
    record.add_argument(
        "--rubric",
        help="JSON file holding the per-criterion rubric scores, or - to read stdin",
    )
    record.add_argument("--idempotency-key")
    _add_workspace(record)
    pending = actions.add_parser(
        "pending", help="recordings in a run waiting for a judge, with how to reach each"
    )
    pending.add_argument("--run")
    _add_track_selector(pending)
    _add_workspace(pending)
    for name, help_text, status in (
        ("pause", "pause a run so it can resume later", "paused"),
        ("resume", "resume a paused run", "in-progress"),
        ("abandon", "abandon a run", "abandoned"),
    ):
        entry = actions.add_parser(name, help=help_text)
        entry.add_argument("--run")
        entry.set_defaults(run_status=status)
        _add_track_selector(entry)
        _add_workspace(entry)
    finalize = actions.add_parser("finalize", help="close a run and write its estimates")
    finalize.add_argument("--run")
    _add_track_selector(finalize)
    finalize.add_argument("--reason", default="completed")
    finalize.add_argument("--idempotency-key")
    _add_workspace(finalize)
    report = actions.add_parser("report", help="show a run's per-dimension estimates")
    report.add_argument("--run")
    _add_track_selector(report)
    _add_workspace(report)
    # The read model the browser client draws from, exposed here too: the server and the
    # CLI have to be answerable to the same cases, and a question only one of them can be
    # asked cannot be compared.
    screen = actions.add_parser("screen", help="everything a client needs to draw the run")
    screen.add_argument("--run")
    _add_track_selector(screen)
    _add_workspace(screen)


def _knowledge_parser(subcommands: Any) -> None:
    knowledge = subcommands.add_parser("knowledge", help="the language-agnostic knowledge graph")
    actions = knowledge.add_subparsers(dest="action", required=True)
    get = actions.add_parser("get", help="one item with its edges, examples, and stage")
    get.add_argument("item", help="content ID, stable key, or exact alias")
    _add_track_selector(get)
    _add_workspace(get)
    search = actions.add_parser("search", help="find items by text, kind, tag, level, or relation")
    search.add_argument("--query")
    search.add_argument("--kind")
    search.add_argument("--tag")
    search.add_argument("--level")
    search.add_argument("--relation")
    search.add_argument("--related-to")
    search.add_argument("--limit", type=int)
    _add_track_selector(search)
    _add_workspace(search)
    upsert = actions.add_parser("upsert", help="create or replace one learner-authored item")
    _add_input(upsert)
    _add_track_selector(upsert)
    _add_workspace(upsert)
    link = actions.add_parser("link", help="add one typed edge between items")
    link.add_argument("--source", required=True)
    link.add_argument("--relation", required=True)
    link.add_argument("--target")
    link.add_argument("--target-ref")
    _add_track_selector(link)
    _add_workspace(link)
    merge = actions.add_parser("merge", help="fold a duplicate learner item into another")
    merge.add_argument("--source", required=True)
    merge.add_argument("--into", required=True)
    # A merge moves evidence, errors, and follow-ups and is not reversible, so the dry
    # run is the default and applying it has to be asked for.
    merge.add_argument("--apply", action="store_true", help="apply the merge, not a dry run")
    _add_track_selector(merge)
    _add_workspace(merge)


def _evidence_parser(subcommands: Any) -> None:
    evidence = subcommands.add_parser(
        "evidence", help="attempts, atomic evidence, and mastery recomputation"
    )
    actions = evidence.add_subparsers(dest="action", required=True)
    record = actions.add_parser("record", help="record one attempt and the evidence it justifies")
    # Not required: a bank task is authoritative about what it demanded, so naming one
    # supplies both. Passing a value the bank contradicts is refused rather than obeyed.
    record.add_argument("--task-type", choices=evidence_module.TASK_TYPES)
    record.add_argument("--modality", choices=evidence_module.MODALITIES)
    record.add_argument("--score", type=float, required=True)
    record.add_argument("--target", help="knowledge item the attempt was about")
    record.add_argument("--dimension", help="skill dimension the attempt bears on")
    record.add_argument(
        "--claim",
        action="append",
        default=[],
        choices=evidence_module.CLAIMS,
        help="what the attempt proves; defaults to the weakest claim the task supports",
    )
    record.add_argument("--help-level", choices=evidence_module.HELP_LEVELS, default="none")
    record.add_argument(
        "--correction-mode", choices=evidence_module.CORRECTION_MODES, default="none"
    )
    record.add_argument(
        "--retrieval", choices=evidence_module.RETRIEVAL_CLASSES, default="immediate"
    )
    record.add_argument("--delay-hours", type=float)
    record.add_argument("--latency-ms", type=int)
    record.add_argument("--difficulty", type=float)
    record.add_argument("--context", help="the setting; diversity is counted in these")
    # The learner's own words are a payload, not an argument: argv leaves them in shell
    # history, and what is retained depends on consent.
    _add_input(record, required=False)
    record.add_argument("--response-visibility", choices=evidence_service.RESPONSE_VISIBILITIES)
    record.add_argument(
        "--assessor-kind", choices=evidence_module.ASSESSOR_KINDS, default="deterministic"
    )
    record.add_argument("--assessor")
    record.add_argument("--confidence", choices=evidence_module.CONFIDENCE_LEVELS, default="medium")
    record.add_argument(
        "--origin",
        choices=evidence_service.SUPPORTED_ORIGINS,
        default="import",
        help="live-session attempts belong to the session engine",
    )
    record.add_argument("--assessment-run")
    record.add_argument("--task", help="assessment bank task the attempt answered")
    record.add_argument("--idempotency-key")
    _add_track_selector(record)
    _add_workspace(record)
    observe = actions.add_parser("observe", help="record one qualitative observation")
    observe.add_argument(
        "--category", required=True, choices=evidence_service.OBSERVATION_CATEGORIES
    )
    observe.add_argument("--note", required=True)
    observe.add_argument("--salience", choices=("low", "medium", "high"), default="medium")
    observe.add_argument("--attempt")
    _add_track_selector(observe)
    _add_workspace(observe)
    listing = actions.add_parser("list", help="the evidence behind an item or a dimension")
    listing.add_argument("--item")
    listing.add_argument("--dimension")
    listing.add_argument("--limit", type=int, default=50)
    _add_track_selector(listing)
    _add_workspace(listing)
    recompute = actions.add_parser(
        "recompute", help="recompute stages and estimates from raw evidence"
    )
    recompute.add_argument("--item")
    recompute.add_argument("--dry-run", action="store_true")
    recompute.add_argument("--no-dimensions", dest="dimensions", action="store_false", default=True)
    _add_track_selector(recompute)
    _add_workspace(recompute)


def _errors_parser(subcommands: Any) -> None:
    errors = subcommands.add_parser("errors", help="recurring errors and the follow-up queue")
    actions = errors.add_subparsers(dest="action", required=True)
    record = actions.add_parser("record", help="record one occurrence of a recurring error")
    record.add_argument("--category", required=True)
    record.add_argument("--signature", required=True, help="the form; normalized for identity")
    record.add_argument("--description", required=True)
    record.add_argument("--target")
    record.add_argument("--learner-form")
    record.add_argument("--corrected-form")
    record.add_argument("--explanation")
    record.add_argument(
        "--meaning-impact", choices=("none", "minor", "major", "breakdown"), default="minor"
    )
    record.add_argument(
        "--classification",
        choices=("learner-error", "transcription-artifact", "uncertain"),
        default="learner-error",
    )
    record.add_argument("--confidence", choices=("low", "medium", "high"), default="medium")
    record.add_argument("--severity", choices=("low", "medium", "high"), default="medium")
    record.add_argument("--attempt")
    # The two remedies for an uncertain match. Exactly one, never both.
    record.add_argument("--attach-to", help="file this against an existing error pattern")
    record.add_argument("--distinct", action="store_true", help="record it as a new pattern")
    _add_track_selector(record)
    _add_workspace(record)
    show = actions.add_parser("show", help="one error with its history and what it still needs")
    show.add_argument("error")
    _add_track_selector(show)
    _add_workspace(show)
    listing = actions.add_parser("list", help="the track's errors, worst first")
    listing.add_argument("--status", choices=error_model.STATUSES)
    listing.add_argument("--live", dest="live_only", action="store_true")
    listing.add_argument("--limit", type=int, default=50)
    _add_track_selector(listing)
    _add_workspace(listing)
    followup = actions.add_parser("followup", help="queue one follow-up")
    followup.add_argument("--kind", required=True, choices=error_service.FOLLOWUP_KINDS)
    followup.add_argument("--action", dest="followup_action", required=True)
    followup.add_argument("--target")
    followup.add_argument("--error")
    followup.add_argument("--attempt")
    followup.add_argument("--priority", type=int, default=0)
    _add_track_selector(followup)
    _add_workspace(followup)
    queue = actions.add_parser("queue", help="the follow-up queue")
    queue.add_argument("--status", choices=error_service.FOLLOWUP_STATUSES, default="open")
    queue.add_argument("--limit", type=int, default=50)
    _add_track_selector(queue)
    _add_workspace(queue)


def _estimate_parser(subcommands: Any) -> None:
    estimate = subcommands.add_parser(
        "estimate", help="the multidimensional profile and its history"
    )
    actions = estimate.add_subparsers(dest="action", required=True)
    show = actions.add_parser("show", help="one estimate per dimension, never one global level")
    show.add_argument(
        "--summary",
        action="store_true",
        help="also offer a single summary level, labelled as a summary",
    )
    _add_track_selector(show)
    _add_workspace(show)
    history = actions.add_parser("history", help="the immutable snapshots behind a dimension")
    history.add_argument("--dimension")
    history.add_argument("--limit", type=int, default=50)
    _add_track_selector(history)
    _add_workspace(history)


def _context_parser(subcommands: Any) -> None:
    context = subcommands.add_parser("context", help="bounded, privacy-aware context bundles")
    actions = context.add_subparsers(dest="action", required=True)
    for scope in context_service.SCOPES:
        entry = actions.add_parser(scope, help=f"context for {scope} work")
        entry.set_defaults(scope=scope)
        if scope == "concept":
            entry.add_argument("--item", required=True)
        entry.add_argument("--max-records", type=int)
        entry.add_argument("--max-tokens", type=int)
        entry.add_argument(
            "--include-responses",
            action="store_true",
            help="include the learner's own words; needs transcript consent",
        )
        _add_track_selector(entry)
        _add_workspace(entry)


def _plan_parser(subcommands: Any) -> None:
    plan = subcommands.add_parser("plan", help="plan one lesson and explain every block")
    actions = plan.add_subparsers(dest="action", required=True)
    create = actions.add_parser("create", help="choose the blocks for one session")
    create.add_argument("--minutes", type=int, required=True)
    create.add_argument(
        "--mode",
        choices=sorted(session_policy.MODES),
        default=session_policy.DEFAULT_MODE,
        help="an explicit mode is honoured unless it is impossible, which is then explained",
    )
    create.add_argument("--energy", choices=session_policy.ENERGY_LEVELS, default="normal")
    create.add_argument("--intent", help="what the learner said they want from this session")
    create.add_argument(
        "--correction-mode",
        choices=session_policy.CORRECTION_MODES,
        help="defaults to the track's declared preference",
    )
    create.add_argument("--idempotency-key")
    _add_track_selector(create)
    _add_workspace(create)
    show = actions.add_parser("show", help="the plan, its ranking, and what it is holding")
    show.add_argument("--session")
    _add_track_selector(show)
    _add_workspace(show)


def _session_parser(subcommands: Any) -> None:
    session = subcommands.add_parser(
        "session", help="run, stage, close, and recover learning sessions"
    )
    actions = session.add_subparsers(dest="action", required=True)
    for name, help_text in (
        ("start", "begin a planned session"),
        ("status", "what the session is and what it is holding"),
        ("resume", "pick a session back up after an interruption"),
    ):
        entry = actions.add_parser(name, help=help_text)
        entry.add_argument("--session")
        _add_track_selector(entry)
        _add_workspace(entry)
    log_action = actions.add_parser(
        "log", help="store one bounded batch of observations durably, crediting nothing"
    )
    log_action.add_argument("--session")
    # The batch is a payload, not a set of flags: it carries the learner's own words, and
    # argv leaves those in shell history.
    _add_input(log_action)
    _add_track_selector(log_action)
    _add_workspace(log_action)
    staged_action = actions.add_parser("staged", help="the provisional events not yet credited")
    staged_action.add_argument("--session")
    staged_action.add_argument("--limit", type=int, default=100)
    _add_track_selector(staged_action)
    _add_workspace(staged_action)
    for name, help_text in (
        ("close", "finalize a session and materialize its staged work exactly once"),
        ("partial-close", "finalize a shortened session, crediting only what happened"),
    ):
        entry = actions.add_parser(name, help=help_text)
        entry.add_argument("--session")
        if name == "close":
            entry.add_argument(
                "--outcome", choices=session_policy.CLOSE_OUTCOMES, default="completed"
            )
        entry.add_argument("--actual-minutes", type=int)
        entry.add_argument("--fatigue", choices=("low", "medium", "high"))
        entry.add_argument("--summary")
        entry.add_argument(
            "--discard-block",
            action="append",
            default=[],
            help="exclude a reviewed block's staged events from this close",
        )
        entry.add_argument("--idempotency-key")
        _add_track_selector(entry)
        _add_workspace(entry)
    abandon = actions.add_parser("abandon", help="abandon a session, keeping its staged work")
    abandon.add_argument("--session")
    abandon.add_argument("--reason")
    _add_track_selector(abandon)
    _add_workspace(abandon)
    recover = actions.add_parser(
        "recover", help="move staged work from a finished session into an open one"
    )
    recover.add_argument("--from", dest="source", required=True)
    recover.add_argument("--into", dest="target")
    recover.add_argument("--event", action="append", default=[])
    _add_track_selector(recover)
    _add_workspace(recover)
    ingest = actions.add_parser(
        "ingest-package", help="validate and stage an externally produced session"
    )
    _add_input(ingest)
    ingest.add_argument("--session")
    ingest.add_argument("--producer")
    _add_track_selector(ingest)
    _add_workspace(ingest)


def _wiki_parser(subcommands: Any) -> None:
    wiki = subcommands.add_parser("wiki", help="generated projections of the learner database")
    actions = wiki.add_subparsers(dest="action", required=True)
    build = actions.add_parser("build", help="render a generated view and record its hash")
    build.add_argument("--view", choices=wiki_service.DASHBOARD_VIEWS, default="dashboard")
    _add_track_selector(build)
    _add_workspace(build)


def _source_parser(subcommands: Any) -> None:
    source = subcommands.add_parser("source", help="catalogue material and record what it taught")
    actions = source.add_subparsers(dest="action", required=True)
    add = actions.add_parser("add", help="catalogue one work, with its rights")
    add.add_argument("--kind", choices=source_policy.SOURCE_KINDS, required=True)
    add.add_argument("--title", required=True)
    add.add_argument("--creator")
    add.add_argument("--uri", dest="canonical_uri")
    add.add_argument(
        "--rights",
        choices=source_policy.RIGHTS_CLASSES,
        default="metadata-only",
        help="what may be stored from the work itself",
    )
    add.add_argument("--rights-note")
    add.add_argument("--language")
    add.add_argument("--level")
    add.add_argument("--has-audio", action="store_true")
    add.add_argument("--has-transcript", action="store_true")
    add.add_argument("--notes")
    # Units carry excerpts, which are text from the work: a payload, never argv.
    _add_input(add, required=False)
    _add_track_selector(add)
    _add_workspace(add)
    listing = actions.add_parser("list", help="the catalogue and its progress")
    listing.add_argument("--status", choices=source_policy.SOURCE_STATUSES)
    listing.add_argument("--kind", choices=source_policy.SOURCE_KINDS)
    listing.add_argument("--limit", type=int, default=50)
    _add_track_selector(listing)
    _add_workspace(listing)
    show = actions.add_parser("show", help="one source, its units, and its progress")
    show.add_argument("--source", required=True)
    _add_track_selector(show)
    _add_workspace(show)
    comprehension = actions.add_parser(
        "comprehension", help="record how much was understood, and with what help"
    )
    comprehension.add_argument("--source", required=True)
    comprehension.add_argument("--unit")
    comprehension.add_argument("--aid", choices=source_policy.COMPREHENSION_AIDS, default="unaided")
    comprehension.add_argument("--band", choices=source_policy.COMPREHENSION_BANDS, required=True)
    comprehension.add_argument("--mode", choices=source_policy.STUDY_MODES, default="intensive")
    comprehension.add_argument("--replays", type=int, default=0)
    comprehension.add_argument("--lookups", type=int, default=0)
    comprehension.add_argument("--minutes", type=int)
    comprehension.add_argument("--note")
    _add_track_selector(comprehension)
    _add_workspace(comprehension)
    position = actions.add_parser("position", help="record where the learner is in a source")
    position.add_argument("--source", required=True)
    position.add_argument("--unit")
    position.add_argument("--mode", choices=source_policy.STUDY_MODES)
    position.add_argument("--minutes", type=int)
    _add_track_selector(position)
    _add_workspace(position)
    complete = actions.add_parser("complete-unit", help="mark one unit worked through")
    complete.add_argument("--source", required=True)
    complete.add_argument("--unit", required=True)
    complete.add_argument("--minutes", type=int)
    _add_track_selector(complete)
    _add_workspace(complete)
    link = actions.add_parser("link", help="link a knowledge item to the unit it came from")
    link.add_argument("--source", required=True)
    link.add_argument("--unit", required=True)
    link.add_argument("--target", required=True)
    link.add_argument(
        "--target-kind",
        choices=("knowledge-item", "example", "error-pattern"),
        default="knowledge-item",
    )
    link.add_argument(
        "--relation",
        choices=("encountered-in", "extracted-from", "illustrated-by", "practised-in"),
        default="extracted-from",
    )
    _add_track_selector(link)
    _add_workspace(link)
    status_action = actions.add_parser("status", help="move a source through its lifecycle")
    status_action.add_argument("--source", required=True)
    status_action.add_argument("--status", choices=source_policy.SOURCE_STATUSES, required=True)
    _add_track_selector(status_action)
    _add_workspace(status_action)


def _artifact_parser(subcommands: Any) -> None:
    artifact = subcommands.add_parser(
        "artifact", help="register, verify, and purge recordings and files"
    )
    actions = artifact.add_subparsers(dest="action", required=True)
    register = actions.add_parser("register", help="record a file that stays outside Git")
    register.add_argument("--path", dest="relative_path", required=True)
    register.add_argument("--kind", choices=("audio", "transcript", "other"), default="audio")
    register.add_argument(
        "--origin",
        choices=("learner-recording", "provider-export", "source-download", "other"),
        default="learner-recording",
    )
    register.add_argument("--rights", choices=source_policy.RIGHTS_CLASSES, default="full-local")
    register.add_argument("--source")
    register.add_argument("--media-type")
    register.add_argument(
        "--not-retained",
        dest="retained",
        action="store_false",
        help="record that the file existed and was not kept -- the file is then deleted",
    )
    _add_track_selector(register)
    _add_workspace(register)
    verify = actions.add_parser("verify", help="check registered files against their checksums")
    _add_track_selector(verify)
    _add_workspace(verify)
    listing = actions.add_parser("list", help="what is registered, including what was purged")
    listing.add_argument("--kind", choices=("audio", "transcript", "other"))
    listing.add_argument("--limit", type=int, default=50)
    _add_track_selector(listing)
    _add_workspace(listing)
    clip = actions.add_parser(
        "clip", help="register a selected excerpt of a recording, which outlives the whole"
    )
    clip.add_argument("--path", dest="relative_path", required=True)
    clip.add_argument("--of", dest="clip_of", required=True, help="the recording it came from")
    # Required: a clip with no window is the whole recording wearing a label that earns
    # it longer retention than a whole recording gets.
    clip.add_argument("--from-ms", dest="clip_starts_at_ms", type=int, required=True)
    clip.add_argument("--to-ms", dest="clip_ends_at_ms", type=int, required=True)
    clip.add_argument("--media-type")
    _add_track_selector(clip)
    _add_workspace(clip)
    sweep = actions.add_parser(
        "sweep", help="apply this track's audio retention policy to what it still holds"
    )
    sweep.add_argument(
        "--dry-run", action="store_true", help="report what would go without deleting it"
    )
    _add_track_selector(sweep)
    _add_workspace(sweep)
    purge = actions.add_parser("purge", help="delete a file and settle what depended on it")
    purge.add_argument("--artifact", required=True)
    purge.add_argument(
        "--reason", choices=transcript_policy.PURGE_REASONS, default="learner-request"
    )
    purge.add_argument(
        "--dry-run",
        action="store_true",
        help="report the consequences without deleting anything",
    )
    _add_track_selector(purge)
    _add_workspace(purge)


def _transcript_parser(subcommands: Any) -> None:
    transcript = subcommands.add_parser(
        "transcript", help="what was said, in layers that stay apart"
    )
    actions = transcript.add_subparsers(dest="action", required=True)
    imported = actions.add_parser("import", help="store the transcript of a session package")
    _add_input(imported)
    imported.add_argument("--ingestion")
    _add_track_selector(imported)
    _add_workspace(imported)
    show = actions.add_parser("show", help="a transcript with its layers and disagreements")
    show.add_argument("--session")
    show.add_argument("--ingestion")
    show.add_argument("--utterance")
    show.add_argument("--limit", type=int, default=200)
    _add_track_selector(show)
    _add_workspace(show)
    for name, help_text in (
        ("normalize", "tidy an utterance without changing which words were heard"),
        ("review", "record what a person heard when they listened again"),
    ):
        entry = actions.add_parser(name, help=help_text)
        entry.add_argument("--utterance", required=True)
        # The text is the learner's own words: a payload, never argv.
        _add_input(entry)
        entry.add_argument("--reviewer")
        entry.add_argument("--reason")
        if name == "review":
            entry.add_argument("--confidence", choices=("low", "medium", "high"), default="medium")
            entry.add_argument(
                "--reviewer-kind",
                choices=("deterministic", "ai", "learner", "human"),
                default="human",
            )
        else:
            entry.add_argument(
                "--reviewer-kind",
                choices=("deterministic", "ai", "learner", "human"),
                default="deterministic",
            )
        _add_track_selector(entry)
        _add_workspace(entry)
    interpret = actions.add_parser(
        "interpret", help="say whether an utterance was a mistake or a mishearing"
    )
    interpret.add_argument("--utterance", required=True)
    interpret.add_argument(
        "--classification", choices=transcript_policy.CLASSIFICATIONS, required=True
    )
    interpret.add_argument("--category", help="the error category a learner-error is filed under")
    interpret.add_argument("--signature", help="the recurring form, if not the line itself")
    interpret.add_argument("--attach-to", help="file against this existing pattern")
    interpret.add_argument("--distinct", action="store_true", help="insist this is a new pattern")
    interpret.add_argument(
        "--despite-low-confidence",
        action="store_true",
        help="blame the learner although the transcriber was unsure; needs a human or "
        "learner reviewer who listened, and --override-reason",
    )
    interpret.add_argument("--override-reason", help="why the transcriber's doubt was overruled")
    interpret.add_argument("--confidence", choices=("low", "medium", "high"), default="medium")
    interpret.add_argument(
        "--reviewer-kind", choices=("deterministic", "ai", "learner", "human"), default="ai"
    )
    interpret.add_argument("--reviewer")
    _add_input(interpret, required=False)
    _add_track_selector(interpret)
    _add_workspace(interpret)
    pronunciation = actions.add_parser(
        "pronunciation", help="record how something sounded, where sound supports it"
    )
    pronunciation.add_argument(
        "--dimension", choices=transcript_policy.PRONUNCIATION_DIMENSIONS, required=True
    )
    pronunciation.add_argument(
        "--status", choices=transcript_policy.PRONUNCIATION_STATUSES, required=True
    )
    pronunciation.add_argument(
        "--basis", choices=transcript_policy.EVIDENCE_BASES, default="transcript"
    )
    pronunciation.add_argument("--utterance")
    pronunciation.add_argument("--audio")
    pronunciation.add_argument("--target")
    pronunciation.add_argument("--note")
    pronunciation.add_argument(
        "--reviewer-kind", choices=("deterministic", "ai", "learner", "human"), default="ai"
    )
    pronunciation.add_argument("--reviewer")
    _add_track_selector(pronunciation)
    _add_workspace(pronunciation)


def _speaking_parser(subcommands: Any) -> None:
    speaking = subcommands.add_parser("speaking", help="make, check, and take in a spoken session")
    actions = speaking.add_subparsers(dest="action", required=True)
    package = actions.add_parser("package", help="write an empty but valid package to fill in")
    package.add_argument("--external-session-id", required=True)
    package.add_argument("--language", dest="target_language", required=True)
    package.add_argument("--started-at", required=True, help="ISO 8601, UTC")
    package.add_argument("--minutes", type=int, default=20)
    package.add_argument("--utterances", type=int, default=4)
    package.add_argument("--session")
    package.add_argument("--out", help="write the package here instead of to the envelope")
    _add_track_selector(package)
    _add_workspace(package)
    validate = actions.add_parser("validate", help="say what ingesting this would do")
    _add_input(validate)
    validate.add_argument("--session", help="the session ingestion would attach it to")
    validate.add_argument("--adapter", choices=speaking_service.ADAPTERS, default="lingua")
    validate.add_argument("--external-session-id")
    validate.add_argument("--language", dest="target_language")
    validate.add_argument("--started-at")
    _add_track_selector(validate)
    _add_workspace(validate)
    ingest = actions.add_parser("ingest", help="stage the events and store the transcript")
    _add_input(ingest)
    ingest.add_argument("--adapter", choices=speaking_service.ADAPTERS, default="lingua")
    ingest.add_argument("--external-session-id")
    ingest.add_argument("--language", dest="target_language")
    ingest.add_argument("--started-at")
    ingest.add_argument("--session")
    ingest.add_argument("--producer")
    _add_track_selector(ingest)
    _add_workspace(ingest)


def _privacy_parser(subcommands: Any) -> None:
    privacy = subcommands.add_parser("privacy", help="what is private and what could escape")
    actions = privacy.add_subparsers(dest="action", required=True)
    audit = actions.add_parser(
        "audit", help="paths, committed pages, retention state, and purge consequences"
    )
    _add_track_selector(audit)
    _add_workspace(audit)


def _parser() -> ContractArgumentParser:
    parser = ContractArgumentParser(prog="linguawiki")
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    subcommands = parser.add_subparsers(dest="group", required=True)
    status = subcommands.add_parser("status", help="show core runtime status")
    _add_format(status)
    _workspace_parser(subcommands)
    _db_parser(subcommands)
    _skills_parser(subcommands)
    _pack_parser(subcommands)
    _user_parser(subcommands)
    _track_parser(subcommands)
    _onboard_parser(subcommands)
    _resources_parser(subcommands)
    _curriculum_parser(subcommands)
    _assessment_parser(subcommands)
    _client_parser(subcommands)
    _knowledge_parser(subcommands)
    _evidence_parser(subcommands)
    _errors_parser(subcommands)
    _estimate_parser(subcommands)
    _context_parser(subcommands)
    _plan_parser(subcommands)
    _session_parser(subcommands)
    _source_parser(subcommands)
    _artifact_parser(subcommands)
    _transcript_parser(subcommands)
    _speaking_parser(subcommands)
    _privacy_parser(subcommands)
    _wiki_parser(subcommands)
    return parser


def _status() -> StatusData:
    return StatusData(
        application_version=__version__,
        database_schema_version=migration_module.head_version(),
    )


def _success(command: str, data: StatusData, clock: Clock) -> StatusEnvelope:
    return StatusEnvelope(
        command=command,
        correlation_id=EventId.new(),
        generated_at=clock.now(),
        data=data,
    )


def _envelope(
    command: str, data: BaseModel, clock: Clock, warnings: Sequence[str] = ()
) -> GenericSuccessEnvelope:
    return GenericSuccessEnvelope(
        command=command,
        correlation_id=EventId.new(),
        generated_at=clock.now(),
        warnings=tuple(warnings),
        data=data.model_dump(mode="json"),
    )


def _failure(command: str, error: LinguaWikiError, clock: Clock) -> ErrorEnvelope:
    return ErrorEnvelope(
        command=command,
        correlation_id=EventId.new(),
        generated_at=clock.now(),
        error=error.payload,
    )


def _subparser_actions(parser: argparse.ArgumentParser) -> argparse._SubParsersAction[Any] | None:
    return next(
        (action for action in parser._actions if isinstance(action, argparse._SubParsersAction)),
        None,
    )


def _command_name(arguments: Sequence[str], parser: ContractArgumentParser) -> str:
    """Name the command from registered subcommands, including nested groups."""

    parts: list[str] = []
    current: argparse.ArgumentParser = parser
    remaining = list(arguments)
    while (action := _subparser_actions(current)) is not None:
        match = next((argument for argument in remaining if argument in action.choices), None)
        if match is None:
            break
        parts.append(match)
        remaining = remaining[remaining.index(match) + 1 :]
        current = action.choices[match]
    return ".".join(parts) if parts else "unknown"


def _print(envelope: BaseModel, human: str, output_format: str) -> None:
    if output_format == "json":
        print(envelope.model_dump_json())
    else:
        print(human)


def _run_status(args: argparse.Namespace, clock: Clock) -> int:
    envelope = _success("status", _status(), clock)
    _print(
        envelope,
        f"LinguaWiki {envelope.data.application_version} "
        f"(contract v{envelope.data.contract_schema_version}, "
        f"schema v{envelope.data.database_schema_version}, Stage {envelope.data.stage})",
        args.format,
    )
    return 0


def _run_workspace(args: argparse.Namespace, clock: Clock, command: str) -> int:
    if args.action == "init":
        init_report = workspace_service.initialize(
            workspace_service.InitOptions(
                path=args.path,
                backup_root=args.backup_root,
                name=args.name,
                timezone=args.timezone,
                history_policy=args.history,
                git_init=args.git_init,
                uv_lock=args.uv_lock,
                find_links=args.find_links,
            ),
            clock=clock,
        )
        verb = "initialized" if init_report.created else "already initialized"
        _print(
            _envelope(command, init_report, clock, init_report.warnings),
            f"{verb} {init_report.name} at {init_report.workspace} (workspace "
            f"{init_report.workspace_id}, schema v{init_report.database_schema_version})",
            args.format,
        )
        return 0
    if args.action == "status":
        status_report = workspace_service.status(args.workspace, clock=clock)
        _print(
            _envelope(command, status_report, clock, status_report.warnings),
            f"{status_report.name} ({status_report.workspace_id}) "
            f"core {status_report.core.version}, schema "
            f"v{status_report.applied_schema_version}/{status_report.packaged_schema_version}, "
            f"{status_report.users} user(s), {status_report.tracks} active track(s)",
            args.format,
        )
        return 0
    if args.action == "doctor":
        doctor = workspace_service.doctor(args.workspace, clock=clock)
        failures = doctor.failures
        lines = [f"{check.status:>7}  {check.name}: {check.message}" for check in doctor.checks]
        lines.append("doctor passed" if doctor.ok else f"doctor found {len(failures)} failure(s)")
        _print(_envelope(command, doctor, clock, doctor.warnings), "\n".join(lines), args.format)
        return 0 if doctor.ok else EXIT_REPORTED_FAILURE
    if args.action == "lock-dependencies":
        locked = workspace_service.lock_dependencies(args.workspace, find_links=args.find_links)
        _print(
            _envelope(command, locked, clock, locked.warnings),
            f"resolved dependency lock for {locked.name}",
            args.format,
        )
        return 0
    if args.action == "privacy-check":
        privacy = workspace_service.privacy_check(args.workspace)
        lines = [f"{item.path}: {item.reason}" for item in privacy.violations]
        lines.extend(f"missing ignore rule: {rule}" for rule in privacy.missing_ignore_rules)
        lines.append(
            f"checked {privacy.candidates_checked} candidate(s) from {privacy.source}: "
            + ("safe" if privacy.ok else "unsafe")
        )
        _print(_envelope(command, privacy, clock, privacy.warnings), "\n".join(lines), args.format)
        return 0 if privacy.ok else EXIT_REPORTED_FAILURE
    if args.private is None:
        raise LinguaWikiError("invalid_arguments", "choose --private or --not-private explicitly")
    confirmed = workspace_service.confirm_remote(args.workspace, private=args.private, clock=clock)
    _print(
        _envelope(command, confirmed, clock, confirmed.warnings),
        f"recorded remote privacy confirmation for {confirmed.workspace_id}",
        args.format,
    )
    return 0


def _run_db(args: argparse.Namespace, clock: Clock, command: str) -> int:
    if args.action == "restore":
        paths = workspace_paths(args.workspace)
        restore_report = backup_module.restore(
            args.source,
            args.target,
            clock=clock,
            active_database=paths.database if paths.config.is_file() else None,
            kind=args.kind,
        )
        _print(
            _envelope(command, restore_report, clock),
            f"restored {restore_report.source_kind} backup into {restore_report.target} "
            f"({restore_report.total_rows} row(s), schema "
            f"v{restore_report.database_schema_version})",
            args.format,
        )
        return 0
    paths = require_initialized_workspace(args.workspace)
    configuration = workspace_service.load_configuration(paths)
    if args.action == "init":
        migration = database_service.initialize(paths, clock=clock)
        _print(
            _envelope(command, migration, clock),
            f"database at {migration.database} is at schema v{migration.applied_schema_version} "
            f"({len(migration.applied)} migration(s) applied)",
            args.format,
        )
        return 0
    if args.action == "status":
        db_status = database_service.status(paths, clock=clock)
        _print(
            _envelope(command, db_status, clock),
            f"schema v{db_status.applied_schema_version}/{db_status.packaged_schema_version}, "
            f"{len(db_status.pending)} pending, writer lock "
            f"{'held' if db_status.writer_lock_held else 'free'}",
            args.format,
        )
        return 0
    if args.action == "migrate":
        backup_root = backup_module.resolve_backup_root(
            configuration.backup_root, workspace_root=paths.root
        )
        migration = database_service.migrate(
            paths, backup_root=backup_root, clock=clock, dry_run=args.dry_run
        )
        prefix = "would apply" if migration.dry_run else "applied"
        _print(
            _envelope(command, migration, clock),
            f"{prefix} {len(migration.applied)} migration(s); schema "
            f"v{migration.applied_schema_version}",
            args.format,
        )
        return 0
    if args.action == "check":
        lock = workspace_service.load_lock(paths) if paths.lock.is_file() else None
        integrity = database_service.check(paths, lock=lock, clock=clock)
        lines = [f"{check.status:>7}  {check.name}: {check.message}" for check in integrity.checks]
        lines.append("database check passed" if integrity.ok else "database check failed")
        _print(
            _envelope(command, integrity, clock, integrity.warnings), "\n".join(lines), args.format
        )
        return 0 if integrity.ok else EXIT_REPORTED_FAILURE
    if args.action == "backup":
        backup_root = backup_module.resolve_backup_root(
            configuration.backup_root, workspace_root=paths.root
        )
        backup_report = database_service.backup(
            paths,
            backup_root=backup_root,
            clock=clock,
            reason=args.reason,
            native=not args.portable_only,
            portable=not args.native_only,
        )
        _print(
            _envelope(command, backup_report, clock, backup_report.skipped_layers),
            f"verified backup at {backup_report.directory} ({backup_report.total_rows} row(s))",
            args.format,
        )
        return 0
    report_export = database_service.export_portable(paths, target=args.to, clock=clock)
    _print(
        _envelope(command, report_export, clock),
        f"portable export at {report_export.directory} ({report_export.total_rows} row(s))",
        args.format,
    )
    return 0


def _run_skills(args: argparse.Namespace, clock: Clock, command: str) -> int:
    paths = require_initialized_workspace(args.workspace)
    lock = workspace_service.load_lock(paths) if paths.lock.is_file() else None
    if args.action == "install":
        installed = skills_service.install_bundle(paths, clock=clock)
        _print(
            _envelope(command, installed, clock, installed.warnings),
            f"installed {installed.file_count} generated skill file(s) at {installed.root}",
            args.format,
        )
        return 0
    bundle = skills_service.inspect_bundle(paths, lock=lock)
    matches = bundle.matches_installed_core and bundle.matches_lock is not False
    _print(
        _envelope(command, bundle, clock, bundle.warnings),
        f"{bundle.file_count} generated skill file(s); "
        + ("snapshot matches the pinned bundle" if matches else "snapshot differs"),
        args.format,
    )
    return 0 if matches else EXIT_REPORTED_FAILURE


def _pack_workspace(args: argparse.Namespace) -> Any:
    return require_initialized_workspace(args.workspace)


def _run_pack(args: argparse.Namespace, clock: Clock, command: str) -> int:
    if args.action == "scaffold":
        scaffolded = pack_service.scaffold(
            args.path,
            pack_key=args.pack_key,
            name=args.name,
            language=args.language,
            framework_id=args.framework,
            framework_name=args.framework_name,
            framework_version=args.framework_version,
            levels=args.level,
            bands=args.band,
            themes=args.theme,
            support_languages=args.support,
            maintainers=args.maintainer or ("pack author",),
            license_name=args.license_name,
        )
        _print(
            _envelope(command, scaffolded, clock, scaffolded.warnings),
            f"scaffolded {scaffolded.pack_key} {scaffolded.version} ({scaffolded.maturity}) "
            f"at {scaffolded.pack}: {len(scaffolded.files)} file(s)",
            args.format,
        )
        return 0
    if args.action == "validate":
        report = pack_service.validate(args.pack)
        _print(
            _envelope(command, report, clock, report.warnings),
            f"{report.pack_key} {report.version} ({report.maturity}) validates: "
            + ", ".join(f"{count} {kind}" for kind, count in sorted(report.counts.items())),
            args.format,
        )
        return 0 if report.ok else EXIT_REPORTED_FAILURE
    if args.action == "coverage":
        coverage = pack_service.coverage(args.pack)
        lines = [
            f"{coverage.pack_key} {coverage.version}: declared {coverage.declared_maturity}, "
            f"highest supported {coverage.highest_supported_maturity}",
            "onboarding modes: " + (", ".join(coverage.supported_onboarding_modes) or "none"),
        ]
        lines.extend(f"gap  {gap}" for gap in coverage.gaps)
        lines.extend(f"expectation  {failure}" for failure in coverage.expectation_failures)
        _print(_envelope(command, coverage, clock), "\n".join(lines), args.format)
        return 0 if coverage.ok else EXIT_REPORTED_FAILURE
    if args.action == "publish":
        published = pack_service.publish(args.pack, maturity=args.maturity)
        _print(
            _envelope(command, published, clock, published.warnings),
            f"published {published.pack_key} {published.version} as {published.maturity} "
            f"at {published.content_address}",
            args.format,
        )
        return 0
    if args.action == "stamp":
        stamped = stamp_module.stamp_pack(args.pack, write=not args.check)
        payload = GenericSuccessEnvelope(
            command=command,
            correlation_id=EventId.new(),
            generated_at=clock.now(),
            data={
                "pack": stamped.pack,
                "pack_key": stamped.pack_key,
                "items": len(stamped.items),
                "restamped": [item.stable_key for item in stamped.stale],
                "written": list(stamped.written),
                "check": args.check,
            },
        )
        _print(
            payload,
            f"{stamped.pack_key}: {len(stamped.items)} item hash(es) checked, "
            f"{len(stamped.written)} file(s) rewritten",
            args.format,
        )
        return 0
    paths = _pack_workspace(args)
    if args.action == "list":
        summaries = pack_service.listing(paths, clock=clock)
        listing = GenericSuccessEnvelope(
            command=command,
            correlation_id=EventId.new(),
            generated_at=clock.now(),
            data={"packs": [entry.model_dump(mode="json") for entry in summaries]},
        )
        _print(
            listing,
            "\n".join(
                f"{entry.pack_key} {entry.version} ({entry.maturity}) {entry.language_tag}"
                for entry in summaries
            )
            or "no pack is installed",
            args.format,
        )
        return 0
    if args.action == "diff":
        difference = pack_service.diff(paths, args.pack, clock=clock)
        _print(
            _envelope(command, difference, clock),
            f"{difference.pack_key}: {len(difference.added)} added, "
            f"{len(difference.changed)} changed, {len(difference.removed)} removed, "
            f"{difference.unchanged} unchanged",
            args.format,
        )
        return 0
    if args.action == "install":
        installed = pack_service.install(
            paths, args.pack, clock=clock, dry_run=args.dry_run, command=command
        )
        verb = (
            "would install"
            if installed.dry_run
            else ("installed" if installed.created else "reinstalled")
        )
        _print(
            _envelope(command, installed, clock, installed.warnings),
            f"{verb} {installed.pack_key} {installed.version} ({installed.maturity})",
            args.format,
        )
        return 0
    updated = pack_service.install(
        paths,
        args.pack,
        clock=clock,
        allow_update=True,
        dry_run=not args.apply,
        command=command,
    )
    prefix = "would update" if updated.dry_run else "updated"
    _print(
        _envelope(command, updated, clock, updated.warnings),
        f"{prefix} {updated.pack_key} from {updated.updated_from} to {updated.version}",
        args.format,
    )
    return 0


def _run_pack_author(args: argparse.Namespace, clock: Clock, command: str) -> int:
    paths = _pack_workspace(args)
    action = args.author_action
    if action == "generate-draft":
        batch = authoring_service.generate_draft(
            paths,
            template_key=args.template_key,
            version=args.template_version,
            item_count=args.count,
            pack_key=args.pack,
            provider=args.provider,
            model=args.model,
            model_version=args.model_version,
            privacy_class=args.privacy,
            clock=clock,
            command=command,
        )
        _print(
            _envelope(command, batch, clock, batch.warnings),
            f"batch {batch.batch_id} open under {batch.sampling_policy}: "
            f"{batch.required_sample} of up to {args.count} item(s) must be inspected",
            args.format,
        )
        return 0
    if action == "import":
        payload = _read_input(args.input_path)
        items = payload["items"] if isinstance(payload, dict) else payload
        result = authoring_service.import_items(
            paths,
            items=items,
            batch_id=args.batch,
            pack_key=args.pack,
            origin_class=args.origin,
            rights=args.rights,
            privacy=args.privacy,
            clock=clock,
            command=command,
        )
        if isinstance(result, tuple):
            envelope = GenericSuccessEnvelope(
                command=command,
                correlation_id=EventId.new(),
                generated_at=clock.now(),
                data={"items": [entry.model_dump(mode="json") for entry in result]},
            )
            _print(envelope, f"recorded {len(result)} draft item(s)", args.format)
            return 0
        _print(
            _envelope(command, result, clock, result.warnings),
            f"batch {result.batch_id} holds {result.item_count} draft item(s); "
            f"{result.required_sample} to inspect",
            args.format,
        )
        return 0
    if action == "review-queue":
        queue = authoring_service.review_queue(
            paths, pack_key=args.pack, limit=args.limit, clock=clock
        )
        lines = [
            f"tier {entry.risk_tier} {entry.stable_key} ({entry.lifecycle}"
            + (", quarantined" if entry.quarantined else "")
            + f") centrality {entry.dependency_centrality}; to reach "
            f"{entry.promotion_target}: "
            + (", ".join(entry.gate_problems) or "nothing outstanding")
            for entry in queue.items
        ]
        lines.append(f"{queue.returned} of {queue.total} unfinished item(s)")
        _print(_envelope(command, queue, clock, queue.warnings), "\n".join(lines), args.format)
        return 0
    if action == "review":
        outcome = authoring_service.review(
            paths,
            content_id=args.content,
            axis=args.axis,
            state=args.state,
            reviewer_kind=args.reviewer_kind,
            reviewer=args.reviewer,
            method=args.method,
            evidence_reference=args.evidence,
            inspection=args.inspection,
            finding=args.finding,
            clock=clock,
            command=command,
        )
        if isinstance(outcome, authoring_service.InvalidationReport):
            _print(
                _envelope(command, outcome, clock, outcome.warnings),
                f"quarantined {len(outcome.roots)} batch item(s) and invalidated "
                f"{len(outcome.invalidated)} dependent(s): {outcome.reason}",
                args.format,
            )
            return EXIT_REPORTED_FAILURE
        _print(
            _envelope(command, outcome, clock, outcome.warnings),
            f"{outcome.stable_key}: {args.axis} -> {args.state}; "
            + (", ".join(outcome.gate_problems) or "gate satisfied"),
            args.format,
        )
        return 0
    if action == "approve":
        approved = authoring_service.approve(
            paths, content_id=args.content, lifecycle=args.lifecycle, clock=clock, command=command
        )
        _print(
            _envelope(command, approved, clock, approved.warnings),
            f"{approved.stable_key} is now {approved.lifecycle}",
            args.format,
        )
        return 0
    if action == "reject":
        rejected = authoring_service.reject(
            paths, content_id=args.content, reason=args.reason, clock=clock, command=command
        )
        _print(
            _envelope(command, rejected, clock, rejected.warnings),
            f"{rejected.stable_key} rejected: {args.reason}",
            args.format,
        )
        return 0
    invalidated = authoring_service.invalidate(
        paths,
        reason=args.reason,
        content_ids=args.content,
        batch_id=args.batch,
        clock=clock,
        command=command,
    )
    _print(
        _envelope(command, invalidated, clock, invalidated.warnings),
        f"invalidated {len(invalidated.roots)} root(s) and {len(invalidated.invalidated)} "
        f"dependent(s)",
        args.format,
    )
    return 0


def _run_pack_template(args: argparse.Namespace, clock: Clock, command: str) -> int:
    paths = _pack_workspace(args)
    if args.template_action == "validate":
        report = authoring_service.validate_template(
            paths,
            payload=None if args.input_path is None else _read_input(args.input_path),
            template_key=args.template_key,
            version=args.template_version,
            clock=clock,
            command=command,
        )
        _print(
            _envelope(command, report, clock, report.warnings),
            f"{report.template_key} v{report.version} is {report.maturity}; next batch uses "
            f"{report.sampling_policy}",
            args.format,
        )
        return 0
    if args.template_action == "stabilize":
        report = authoring_service.stabilize_template(
            paths,
            template_key=args.template_key,
            version=args.template_version,
            clock=clock,
            command=command,
        )
        _print(
            _envelope(command, report, clock, report.warnings),
            f"{report.template_key} v{report.version} is stable after "
            f"{report.inspected_runs} inspected run(s)",
            args.format,
        )
        return 0
    quarantined = authoring_service.quarantine_template(
        paths,
        template_key=args.template_key,
        version=args.template_version,
        reason=args.reason,
        clock=clock,
        command=command,
    )
    _print(
        _envelope(command, quarantined, clock, quarantined.warnings),
        f"quarantined {len(quarantined.quarantined_batches)} batch(es), "
        f"{len(quarantined.roots)} item(s), {len(quarantined.invalidated)} dependent(s)",
        args.format,
    )
    return EXIT_REPORTED_FAILURE


def _run_user(args: argparse.Namespace, clock: Clock, command: str) -> int:
    paths = _pack_workspace(args)
    if args.action == "create":
        user = learner_service.create_user(
            paths,
            display_name=args.name,
            timezone=args.timezone,
            native_languages=args.native,
            support_languages=args.support,
            clock=clock,
            command=command,
        )
        _print(
            _envelope(command, user, clock),
            f"created {user.display_name} ({user.user_id}) in {user.timezone}",
            args.format,
        )
        return 0
    if args.action == "update":
        user = learner_service.update_user(
            paths,
            user=args.user,
            display_name=args.name,
            timezone=args.timezone,
            native_languages=args.native,
            support_languages=args.support,
            status=args.status,
            clock=clock,
            command=command,
        )
        _print(
            _envelope(command, user, clock),
            f"updated {user.display_name} ({user.status})",
            args.format,
        )
        return 0
    if args.action == "show":
        user = learner_service.show_user(paths, user=args.user, clock=clock)
        _print(
            _envelope(command, user, clock),
            f"{user.display_name} ({user.user_id}) {user.timezone}; native "
            f"{list(user.native_languages)}, support {list(user.support_languages)}, "
            f"{len(user.tracks)} track(s)",
            args.format,
        )
        return 0
    users = learner_service.list_users(paths, clock=clock)
    envelope = GenericSuccessEnvelope(
        command=command,
        correlation_id=EventId.new(),
        generated_at=clock.now(),
        data={"users": [entry.model_dump(mode="json") for entry in users]},
    )
    _print(
        envelope,
        "\n".join(f"{entry.user_id} {entry.display_name} ({entry.status})" for entry in users)
        or "no learner exists yet",
        args.format,
    )
    return 0


def _track_preferences(path: str | None) -> learner_service.TrackPreferences | None:
    if path is None:
        return None
    return learner_service.TrackPreferences.model_validate(_read_input(path))


def _run_track(args: argparse.Namespace, clock: Clock, command: str) -> int:
    paths = _pack_workspace(args)
    if args.action == "create":
        track = learner_service.create_track(
            paths,
            target_language=args.target_language,
            framework=args.framework,
            user=args.user,
            pack_key=args.pack,
            region=args.region,
            script=args.script,
            declared_level=args.declared_level,
            target_level=args.target_level,
            goal=args.goal,
            preferences=_track_preferences(args.input_path),
            clock=clock,
            command=command,
        )
        _print(
            _envelope(command, track, clock, track.warnings),
            f"created {track.target_language} track {track.track_id} in "
            f"{track.proficiency_framework}, declared {track.declared_level}",
            args.format,
        )
        return 0
    if args.action == "update":
        track = learner_service.update_track(
            paths,
            track=args.track,
            goal=args.goal,
            target_level=args.target_level,
            declared_level=args.declared_level,
            preferences=_track_preferences(args.input_path),
            clock=clock,
            command=command,
        )
        _print(
            _envelope(command, track, clock, track.warnings),
            f"updated {track.track_id}: goal {track.goal}, target {track.target_level}",
            args.format,
        )
        return 0
    if args.action == "show":
        track = learner_service.show_track(paths, track=args.track, clock=clock)
        _print(
            _envelope(command, track, clock, track.warnings),
            f"{track.track_id} {track.target_language} ({track.status}) "
            f"{track.proficiency_framework} declared {track.declared_level} "
            f"target {track.target_level}; pack {track.pack_key} {track.pack_version}",
            args.format,
        )
        return 0
    if args.action == "list":
        tracks = learner_service.list_tracks(paths, clock=clock)
        envelope = GenericSuccessEnvelope(
            command=command,
            correlation_id=EventId.new(),
            generated_at=clock.now(),
            data={"tracks": [entry.model_dump(mode="json") for entry in tracks]},
        )
        _print(
            envelope,
            "\n".join(
                f"{entry.track_id} {entry.target_language} ({entry.status}) "
                f"declared {entry.declared_level}"
                for entry in tracks
            )
            or "no track exists yet",
            args.format,
        )
        return 0
    status = {"activate": "active", "pause": "paused", "archive": "archived"}[args.action]
    track = learner_service.set_track_status(
        paths, status=status, track=args.track, clock=clock, command=command
    )
    _print(
        _envelope(command, track, clock, track.warnings),
        f"{track.track_id} is now {track.status}",
        args.format,
    )
    return 0


def _onboard_lines(report: onboarding_service.OnboardingReport) -> str:
    lines = [
        f"{report.onboarding_id} {report.mode} ({report.status}); declared "
        f"{report.declared_level}, {report.calibration_label} against "
        f"{report.pack_key} {report.pack_version} ({report.pack_maturity})",
        f"seeded estimates: {list(report.seeded_estimates)}",
        f"calibration queue: {len(report.calibration_queue)} target(s)",
    ]
    if report.resource_plan is not None:
        lines.append(
            f"plan: {report.resource_plan.plan_label}, "
            f"{report.resource_plan.imported_items} unseen reference item(s)"
        )
    if report.unsupported_dimensions:
        lines.append(f"unsupported dimensions: {list(report.unsupported_dimensions)}")
    if report.next_step:
        lines.append(f"next: {report.next_step}")
    return "\n".join(lines)


def _run_onboard(args: argparse.Namespace, clock: Clock, command: str) -> int:
    paths = _pack_workspace(args)
    if args.action == "start":
        report = onboarding_service.start(
            paths,
            track=args.track,
            mode=args.mode,
            declared_level=args.declared_level,
            idempotency_key=args.idempotency_key,
            clock=clock,
            command=command,
        )
    elif args.action == "record":
        report = onboarding_service.record_answer(
            paths,
            key=args.key,
            value=_read_input(args.input_path),
            onboarding=args.onboarding,
            track=args.track,
            clock=clock,
            command=command,
        )
    elif args.action == "status":
        report = onboarding_service.status(
            paths, onboarding=args.onboarding, track=args.track, clock=clock
        )
    elif args.action == "abandon":
        report = onboarding_service.abandon(
            paths, onboarding=args.onboarding, track=args.track, clock=clock, command=command
        )
    else:
        report = onboarding_service.finalize(
            paths,
            onboarding=args.onboarding,
            track=args.track,
            weeks=args.weeks,
            item_budget=args.item_budget,
            calibration_sample=args.calibration_sample,
            open_calibration=not args.no_calibration,
            clock=clock,
            command=command,
        )
    _print(_envelope(command, report, clock, report.warnings), _onboard_lines(report), args.format)
    return 0


def _plan_lines(report: resource_service.ResourcePlanReport) -> str:
    lines = [
        f"{report.plan_label} for {report.track_id} from {report.pack_key} "
        f"{report.pack_version} ({report.pack_maturity})",
        f"bundles: {list(report.bundles)}; levels {list(report.level_codes)}",
        "counts: "
        + (
            ", ".join(f"{count} {kind}" for kind, count in sorted(report.counts.items()))
            or "nothing to import"
        ),
        f"skipped {len(report.skipped)}, proposed {len(report.proposed_sources)} external "
        f"source(s)",
    ]
    if report.unsupported_dimensions:
        lines.append(f"untestable dimensions: {list(report.unsupported_dimensions)}")
    if report.missing_modalities:
        lines.append(f"modalities without a recommendation: {list(report.missing_modalities)}")
    return "\n".join(lines)


def _run_resources(args: argparse.Namespace, clock: Clock, command: str) -> int:
    paths = _pack_workspace(args)
    if args.action == "plan":
        report = resource_service.plan(
            paths,
            track=args.track,
            onboarding_mode=args.mode,
            level_codes=args.level or None,
            weeks=args.weeks,
            item_budget=args.item_budget,
            clock=clock,
        )
    elif args.action == "prepare":
        report = resource_service.prepare(
            paths,
            track=args.track,
            onboarding_mode=args.mode,
            level_codes=args.level or None,
            weeks=args.weeks,
            item_budget=args.item_budget,
            dry_run=args.dry_run,
            clock=clock,
            command=command,
        )
    else:
        report = resource_service.show(paths, track=args.track, clock=clock)
    _print(_envelope(command, report, clock, report.warnings), _plan_lines(report), args.format)
    return 0


def _run_curriculum(args: argparse.Namespace, clock: Clock, command: str) -> int:
    paths = _pack_workspace(args)
    if args.action == "import":
        report = curriculum_service.import_curriculum(
            paths, _read_input(args.input_path), track=args.track, clock=clock, command=command
        )
    elif args.action == "position":
        report = curriculum_service.position(
            paths,
            completed=args.completed,
            current=args.current,
            curriculum=args.curriculum,
            track=args.track,
            clock=clock,
            command=command,
        )
    elif args.action == "show":
        report = curriculum_service.show(
            paths, curriculum=args.curriculum, track=args.track, clock=clock
        )
    else:
        return _run_curriculum_audit(args, clock, command)
    _print(
        _envelope(command, report, clock, report.warnings),
        f"{report.title} {report.version} ({report.rights_status}): {len(report.units)} unit(s), "
        f"{report.mapped_objectives} of {report.total_objectives} objective(s) mapped, "
        f"{len(report.unmapped_objectives)} explicit gap(s), "
        f"{report.encountered_items} self-reported item(s)",
        args.format,
    )
    return 0


def _run_curriculum_audit(args: argparse.Namespace, clock: Clock, command: str) -> int:
    paths = _pack_workspace(args)
    if args.action == "audit-start":
        report = curriculum_service.audit_start(
            paths,
            curriculum=args.curriculum,
            track=args.track,
            sample_size=args.sample_size,
            idempotency_key=args.idempotency_key,
            clock=clock,
            command=command,
        )
    elif args.action == "audit-record":
        payload = _read_input(args.input_path)
        results = payload["results"] if isinstance(payload, dict) else payload
        report = curriculum_service.audit_record(
            paths,
            results=results,
            audit=args.audit,
            track=args.track,
            clock=clock,
            command=command,
        )
    elif args.action == "audit-finalize":
        report = curriculum_service.audit_finalize(
            paths,
            audit=args.audit,
            track=args.track,
            stop_reason=args.stop_reason,
            clock=clock,
            command=command,
        )
    else:
        report = curriculum_service.audit_report(
            paths, audit=args.audit, track=args.track, clock=clock
        )
    _print(
        _envelope(command, report, clock, report.warnings),
        f"audit {report.audit_id} ({report.status}): {report.recorded} of {report.sample_size} "
        f"probed, {len(report.confirmed_gaps)} gap(s), {len(report.untested_targets)} untested, "
        f"{len(report.queued_calibration)} queued",
        args.format,
    )
    return 0


def _screen_lines(screen: view_service.RunScreen) -> str:
    lines = [
        f"{screen.run_id} {screen.calibration_label} ({screen.status}) against "
        f"{screen.pack_key} {screen.pack_version}; "
        f"{screen.tasks_recorded} of {screen.tasks_served} served task(s) scored"
    ]
    lines.extend(
        f"  {dimension.dimension} ({dimension.dimension_kind}): {dimension.status}, "
        f"{dimension.tasks_used}/{dimension.maximum_tasks} task(s), "
        f"{dimension.estimated_level or 'no estimate'} ({dimension.confidence})"
        for dimension in screen.dimensions
    )
    # The tasks by name, not a count: an operator comparing this against the browser needs
    # to know which task is waiting, not how many are.
    lines.extend(
        f"  awaiting {task.content_id} in {task.dimension}: answer by {task.answer_with}"
        + (" with audio" if task.plays_audio else "")
        for task in screen.outstanding
    )
    if not screen.outstanding:
        lines.append("  nothing is awaiting an answer")
    return "\n".join(lines)


def _pending_lines(report: recording_service.PendingReport) -> str:
    lines = [
        f"{report.run_id}: {len(report.pending)} recording(s) waiting for a judge; recording "
        f"{'offered' if report.recording.offered else 'not offered'}, kept under "
        f"{report.recording.retention_policy}"
    ]
    for entry in report.pending:
        where = entry.audio_path if entry.judgeable else f"cannot be judged: {entry.problem}"
        lines.append(
            f"  {entry.task.content_id} ({entry.task.dimension}, {entry.task.task_type}) "
            f"recording {entry.submission.artifact_id}: {where}"
        )
    return "\n".join(lines)


def _assessment_rubric(args: argparse.Namespace) -> Any:
    """The rubric a judge scored against: `--rubric`, or the older `--input`, not both."""

    if args.rubric is not None and args.input_path is not None:
        raise LinguaWikiError(
            "invalid_arguments",
            "pass the rubric once, with --rubric; --input is the older spelling of the same "
            "payload",
            details=(ErrorDetail(field="rubric", reason="also passed as --input"),),
        )
    reference = args.rubric if args.rubric is not None else args.input_path
    if reference is None:
        return None
    rubric = _read_input(reference)
    if not isinstance(rubric, dict):
        raise LinguaWikiError(
            "invalid_input",
            "a rubric is a JSON object of criterion scores",
            details=(ErrorDetail(field="rubric", reason=type(rubric).__name__),),
        )
    return rubric


def _assessment_lines(report: assessment_service.AssessmentRunReport) -> str:
    lines = [
        f"{report.run_id} {report.calibration_label} ({report.status}) against "
        f"{report.pack_key} {report.pack_version} ({report.pack_maturity}); "
        f"{report.tasks_recorded} of {report.tasks_served} served task(s) scored"
    ]
    lines.extend(
        f"  {entry.dimension}: {entry.status} n={entry.tasks_used}/"
        f"{entry.minimum_tasks}-{entry.maximum_tasks} confidence={entry.confidence} "
        f"estimate={entry.estimated_level} [{entry.credible_low}..{entry.credible_high}]"
        + (f" stop={entry.stop_reason}" if entry.stop_reason else "")
        for entry in report.dimensions
    )
    if report.untested_dimensions:
        lines.append(f"not tested: {list(report.untested_dimensions)}")
    return "\n".join(lines)


#: Bound on a response file, generous for any written answer and small enough that a
#: mistyped path is refused rather than read into memory.
RESPONSE_FILE_LIMIT = 1_000_000


def _read_response_file(reference: str) -> str:
    """Read the learner's answer from a file, reporting what went wrong rather than raising.

    Reading a file can fail, and "cannot be read" is an answer a caller can act on: a
    mistyped path, a directory, a permission, and a file that is not UTF-8 are all things
    the operator fixes, and every one of them reached the envelope as `internal_error`,
    which names nothing. The size bound is part of the same refusal: without it a mistyped
    path to a large file is an out-of-memory crash instead.
    """

    if reference == "-":
        try:
            return sys.stdin.read()
        except (OSError, UnicodeDecodeError) as exc:
            raise LinguaWikiError(
                "response_unreadable",
                f"the response could not be read from stdin: {exc}",
                details=(ErrorDetail(field="response_file", reason=type(exc).__name__),),
            ) from exc
    path = Path(reference).expanduser()
    try:
        size = path.stat().st_size
    except OSError as exc:
        raise LinguaWikiError(
            "response_unreadable",
            f"the response file {path} could not be read: {exc.strerror or exc}",
            details=(ErrorDetail(field="response_file", reason=type(exc).__name__),),
        ) from exc
    if size > RESPONSE_FILE_LIMIT:
        raise LinguaWikiError(
            "response_unreadable",
            f"the response file {path} is {size} bytes, past the {RESPONSE_FILE_LIMIT} "
            "a single answer may be; check the path is the one you meant",
            details=(ErrorDetail(field="response_file", reason="too large"),),
        )
    try:
        return path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        raise LinguaWikiError(
            "response_unreadable",
            f"the response file {path} could not be read: {exc}",
            details=(ErrorDetail(field="response_file", reason=type(exc).__name__),),
        ) from exc


def _assessment_response(args: argparse.Namespace) -> str | None:
    """The learner's answer, from argv or from a file, with one refusal for both.

    `--response` is argv and therefore bounded text; a long written answer goes through
    `--response-file`. Two routes to one value means one place decides what is acceptable,
    so blank text is refused the same way whichever route it arrived by -- a rule enforced
    on the flag a stage happened to add, and not on the other, is not a rule.
    """

    if args.response is not None and args.response_file is not None:
        raise LinguaWikiError(
            "invalid_arguments",
            "pass the learner's response either inline or in a file, not both",
            details=(ErrorDetail(field="response", reason="two responses supplied"),),
        )
    response: str
    if args.response is not None:
        response = str(args.response)
    elif args.response_file is not None:
        response = _read_response_file(args.response_file)
    else:
        return None
    if not response.strip():
        raise LinguaWikiError(
            "invalid_arguments",
            "an empty response is a skip, not a wrong answer; skip the task instead",
            details=(ErrorDetail(field="response", reason="blank"),),
        )
    return response


def _run_assessment(args: argparse.Namespace, clock: Clock, command: str) -> int:
    paths = _pack_workspace(args)
    if args.action == "next":
        outcome = assessment_service.next_task(
            paths,
            run=args.run,
            track=args.track,
            clock=clock,
            command=command,
            idempotency_key=args.idempotency_key,
        )
        if isinstance(outcome, assessment_service.NextTaskReport):
            _print(
                _envelope(command, outcome, clock, outcome.warnings),
                f"[{outcome.dimension} #{outcome.sequence}] {outcome.task_type} "
                f"({outcome.modality}, {outcome.level_code}, difficulty {outcome.difficulty}, "
                f"family {outcome.content_family}, selected for {outcome.selection_reason})\n"
                f"{outcome.prompt}",
                args.format,
            )
            return 0
        _print(
            _envelope(command, outcome, clock, outcome.warnings),
            _assessment_lines(outcome),
            args.format,
        )
        return 0
    if args.action == "start":
        report = assessment_service.start(
            paths,
            track=args.track,
            run_type=args.run_type,
            dimensions=args.dimension or None,
            modalities=args.modality or None,
            scoring=args.scoring,
            idempotency_key=args.idempotency_key,
            clock=clock,
            command=command,
        )
    elif args.action == "record":
        report = assessment_service.record(
            paths,
            content_id=args.content,
            score=args.score,
            response=_assessment_response(args),
            response_visibility=args.response_visibility,
            run=args.run,
            track=args.track,
            rubric=_assessment_rubric(args),
            response_excerpt=args.excerpt,
            assessor_kind=args.assessor_kind,
            assessor=args.assessor,
            confidence=args.confidence,
            audio_artifact=args.audio_artifact,
            submission=args.submission,
            claim=args.claim,
            idempotency_key=args.idempotency_key,
            clock=clock,
            command=command,
        )
    elif args.action == "pending":
        waiting = recording_service.pending(paths, run=args.run, track=args.track, clock=clock)
        _print(
            _envelope(command, waiting, clock, waiting.warnings),
            _pending_lines(waiting),
            args.format,
        )
        return 0
    elif args.action in ("pause", "resume", "abandon"):
        report = assessment_service.set_status(
            paths,
            status=args.run_status,
            run=args.run,
            track=args.track,
            clock=clock,
            command=command,
        )
    elif args.action == "finalize":
        report = assessment_service.finalize(
            paths,
            run=args.run,
            track=args.track,
            reason=args.reason,
            idempotency_key=args.idempotency_key,
            clock=clock,
            command=command,
        )
    elif args.action == "screen":
        screen = view_service.run_screen(paths, run=args.run, track=args.track, clock=clock)
        _print(
            _envelope(command, screen, clock, screen.warnings),
            _screen_lines(screen),
            args.format,
        )
        return 0
    else:
        report = assessment_service.report(paths, run=args.run, track=args.track, clock=clock)
    _print(
        _envelope(command, report, clock, report.warnings), _assessment_lines(report), args.format
    )
    return 0


def _run_client(args: argparse.Namespace, clock: Clock, command: str) -> int:
    """Bind, print where it is listening, and serve in the foreground.

    The URL is printed whether or not a browser is opened, because it carries the launch
    token for this start and there is nowhere else to get it: it is never written to disk.
    """

    from linguawiki.client import server as client_server

    paths = _pack_workspace(args)
    client = client_server.build_server(paths, port=args.port, clock=clock, run=args.run)
    # On stderr, so `--format json` output on stdout stays a single parseable document.
    recovery = client.recovery
    if recovery is not None and recovery.examined:
        print(
            f"recovered {recovery.examined} unresolved recording(s): "
            f"{len(recovery.registered)} registered, {len(recovery.refused)} refused",
            file=sys.stderr,
        )
    for finding in () if recovery is None else recovery.findings:
        print(f"warning: {finding}", file=sys.stderr)
    print(f"LinguaWiki client listening on {client.origin}", file=sys.stderr)
    print(f"open {client.launch_url}", file=sys.stderr)
    if not args.no_open:
        import webbrowser

        webbrowser.open(client.launch_url)
    try:
        client.serve_forever()
    except KeyboardInterrupt:
        print("stopping", file=sys.stderr)
    finally:
        client.close()
    return 0


def _run_knowledge(args: argparse.Namespace, clock: Clock, command: str) -> int:
    paths = _pack_workspace(args)
    if args.action == "get":
        item = knowledge_service.get(paths, item=args.item, track=args.track, clock=clock)
        lines = [
            f"{item.stable_key} ({item.kind}, {item.owner}"
            + (f" {item.pack_key}" if item.pack_key else "")
            + f"): {item.title}",
            f"  {item.level_min or 'no level'} | lifecycle {item.lifecycle} | risk tier "
            f"{item.risk_tier} | {len(item.aliases)} alias(es), {len(item.relations)} edge(s), "
            f"{len(item.examples)} example(s)",
        ]
        if item.state is not None:
            lines.append(
                f"  stage {item.state.stage} (gates gave {item.state.gated_stage}, evidence "
                f"allows {item.state.evidence_ceiling}) confidence {item.state.confidence:.2f} "
                f"from {item.state.positive_evidence}+/{item.state.negative_evidence}-"
            )
            lines.extend(f"    {entry}" for entry in item.state.explanation)
        _print(_envelope(command, item, clock, item.warnings), "\n".join(lines), args.format)
        return 0
    if args.action == "search":
        found = knowledge_service.search(
            paths,
            query=args.query,
            kind=args.kind,
            tag=args.tag,
            level=args.level,
            relation=args.relation,
            related_to=args.related_to,
            track=args.track,
            limit=args.limit,
            clock=clock,
        )
        lines = [
            f"{found.total_matched} item(s) matched; {len(found.hits)} shown (limit {found.limit})"
        ]
        lines.extend(
            f"  {hit.stable_key} [{hit.kind}] {hit.title} "
            f"({hit.level_min or 'no level'}, {hit.owner}, stage {hit.stage or 'unseen'})"
            for hit in found.hits
        )
        _print(_envelope(command, found, clock, found.warnings), "\n".join(lines), args.format)
        return 0
    if args.action == "upsert":
        payload = _read_input(args.input_path)
        if not isinstance(payload, dict):
            raise LinguaWikiError("invalid_input", "a knowledge upsert payload is an object")
        written = knowledge_service.upsert(
            paths,
            stable_key=str(payload["stable_key"]),
            kind=str(payload["kind"]),
            title=str(payload["title"]),
            body=str(payload["body"]),
            summary=payload.get("summary"),
            level=payload.get("level"),
            level_max=payload.get("level_max"),
            aliases=payload.get("aliases") or (),
            themes=payload.get("themes") or (),
            features=payload.get("features") or (),
            track=args.track,
            clock=clock,
            command=command,
        )
        _print(
            _envelope(command, written, clock, written.warnings),
            f"{'created' if written.created else 'replaced'} {written.stable_key} "
            f"({written.kind}) as {written.content_id} at lifecycle {written.lifecycle}",
            args.format,
        )
        return 0
    if args.action == "link":
        linked = knowledge_service.link(
            paths,
            source=args.source,
            relation_type=args.relation,
            target=args.target,
            target_ref=args.target_ref,
            track=args.track,
            clock=clock,
            command=command,
        )
        _print(
            _envelope(command, linked, clock, linked.warnings),
            f"{'added' if linked.created else 'already present'}: {linked.source_content_id} "
            f"--{linked.relation_type}--> "
            f"{linked.target_content_id or linked.target_ref}",
            args.format,
        )
        return 0
    merged = knowledge_service.merge(
        paths,
        source=args.source,
        into=args.into,
        track=args.track,
        dry_run=not args.apply,
        clock=clock,
        command=command,
    )
    lines = [
        f"{'would merge' if merged.dry_run else 'merged'} {merged.source_content_id} into "
        f"{merged.target_content_id} (stages {merged.source_stage} -> {merged.target_stage})"
    ]
    lines.extend(
        f"  {move.table}: {move.moved} row(s) move"
        + (f", {move.already_present} already on the target" if move.already_present else "")
        for move in merged.moves
        if move.moved or move.already_present
    )
    if merged.errors_remapped or merged.errors_folded:
        lines.append(
            f"  error patterns: {merged.errors_remapped} re-derived, "
            f"{merged.errors_folded} folded into the same error on the target"
        )
    _print(_envelope(command, merged, clock, merged.warnings), "\n".join(lines), args.format)
    return 0


def _run_evidence(args: argparse.Namespace, clock: Clock, command: str) -> int:
    paths = _pack_workspace(args)
    if args.action == "record":
        payload = None if args.input_path is None else _read_input(args.input_path)
        response = None
        if payload is not None:
            if not isinstance(payload, dict) or "response" not in payload:
                raise LinguaWikiError(
                    "invalid_input", "the --input payload is an object with a 'response' field"
                )
            response = str(payload["response"])
        attempt = evidence_service.record(
            paths,
            task_type=args.task_type,
            modality=args.modality,
            score=args.score,
            target=args.target,
            dimension=args.dimension,
            claims=args.claim or None,
            help_level=args.help_level,
            correction_mode=args.correction_mode,
            retrieval=args.retrieval,
            delay_hours=args.delay_hours,
            latency_ms=args.latency_ms,
            difficulty=args.difficulty,
            context=args.context,
            response=response,
            response_visibility=args.response_visibility,
            assessor_kind=args.assessor_kind,
            assessor=args.assessor,
            confidence=args.confidence,
            origin=args.origin,
            assessment_run=args.assessment_run,
            task=args.task,
            idempotency_key=args.idempotency_key,
            track=args.track,
            clock=clock,
            command=command,
        )
        lines = [
            f"{attempt.attempt_id} {attempt.outcome} ({attempt.task_type}/{attempt.modality}, "
            f"help {attempt.help_level}, {attempt.retrieval}, context {attempt.context_key})",
            "  evidence: "
            + ", ".join(
                f"{entry.claim} {entry.polarity} strength {entry.strength:.2f} ({entry.novelty})"
                for entry in attempt.evidence
            ),
        ]
        if attempt.stage_after is not None:
            lines.append(
                f"  {attempt.target_title}: stage {attempt.stage_before or 'unseen'} -> "
                f"{attempt.stage_after}"
            )
            lines.extend(f"    {entry}" for entry in attempt.stage_explanation)
        if attempt.errors_reactivated:
            lines.append(f"  reactivated: {list(attempt.errors_reactivated)}")
        if attempt.errors_supported:
            lines.append(f"  counter-evidence for: {list(attempt.errors_supported)}")
        lines.append(f"  response retained as: {attempt.response_visibility}")
        _print(_envelope(command, attempt, clock, attempt.warnings), "\n".join(lines), args.format)
        return 0
    if args.action == "observe":
        observation = evidence_service.observations(
            paths,
            category=args.category,
            note=args.note,
            salience=args.salience,
            attempt=args.attempt,
            track=args.track,
            clock=clock,
            command=command,
        )
        _print(
            _envelope(command, observation, clock, observation.warnings),
            f"{observation.observation_id} {observation.category} ({observation.salience}): "
            f"{observation.note}",
            args.format,
        )
        return 0
    if args.action == "list":
        listed = evidence_service.listing(
            paths,
            track=args.track,
            item=args.item,
            dimension=args.dimension,
            limit=args.limit,
            clock=clock,
        )
        lines = [f"{listed.total} evidence row(s); {len(listed.entries)} shown"]
        lines.extend(
            f"  {entry.occurred_at} {entry.claim} {entry.polarity} "
            f"strength {entry.strength:.2f} [{entry.context_key}, {entry.novelty}, "
            f"{entry.retrieval}, help {entry.help_level}]"
            for entry in listed.entries
        )
        _print(_envelope(command, listed, clock, listed.warnings), "\n".join(lines), args.format)
        return 0
    recomputed = evidence_service.recompute(
        paths,
        track=args.track,
        item=args.item,
        dimensions=args.dimensions,
        dry_run=args.dry_run,
        clock=clock,
        command=command,
    )
    _print(
        _envelope(command, recomputed, clock, recomputed.warnings),
        _recompute_lines(recomputed),
        args.format,
    )
    return 0


def _recompute_lines(report: evidence_service.RecomputeReport) -> str:
    lines = [
        f"{'would recompute' if report.dry_run else 'recomputed'} "
        f"{report.items_considered} item(s) under {report.aggregation_version}; "
        f"{report.items_changed} stage(s) change"
    ]
    lines.extend(
        f"  {change.stable_key}: {change.stage_before or 'unseen'} -> {change.stage_after} "
        f"(gates {change.gated_stage}, evidence allows {change.evidence_ceiling}, "
        f"confidence {change.confidence:.2f})"
        + (f" regressed: {list(change.regressed_claims)}" if change.regressed_claims else "")
        for change in report.changes
    )
    if report.estimates is not None:
        lines.append(
            f"  estimates: {len(report.estimates.changes)} changed, "
            f"{len(report.estimates.unchanged)} unchanged"
        )
        lines.extend(
            f"    {change.dimension}: "
            f"{(change.previous.level_code if change.previous else None) or 'none'} -> "
            f"{change.current.level_code or 'none'} "
            f"[{change.current.level_low}..{change.current.level_high}] "
            f"{change.current.estimate_status}, {change.reason}"
            for change in report.estimates.changes
        )
    return "\n".join(lines)


def _error_lines(report: error_service.ErrorReport) -> str:
    lines = [
        f"{report.error_id} {report.category}/{report.signature} ({report.status}): "
        f"{report.description}",
        f"  {report.occurrence_count} occurrence(s), {report.success_count} qualifying "
        f"success(es), severity {report.severity}"
        + (f", target {report.target_title}" if report.target_title else ""),
    ]
    if report.status_reason:
        lines.append(f"  reason: {report.status_reason}")
    if report.outstanding:
        lines.append(f"  still needs: {', '.join(report.outstanding)}")
    return "\n".join(lines)


def _run_errors(args: argparse.Namespace, clock: Clock, command: str) -> int:
    paths = _pack_workspace(args)
    if args.action == "record":
        recorded = error_service.record(
            paths,
            category=args.category,
            signature=args.signature,
            description=args.description,
            target=args.target,
            learner_form=args.learner_form,
            corrected_form=args.corrected_form,
            explanation=args.explanation,
            meaning_impact=args.meaning_impact,
            classification=args.classification,
            confidence=args.confidence,
            severity=args.severity,
            attempt=args.attempt,
            attach_to=args.attach_to,
            distinct=args.distinct,
            track=args.track,
            clock=clock,
            command=command,
        )
        _print(
            _envelope(command, recorded, clock, recorded.warnings),
            _error_lines(recorded),
            args.format,
        )
        return 0
    if args.action == "show":
        shown = error_service.show(paths, error=args.error, track=args.track, clock=clock)
        lines = [_error_lines(shown)]
        lines.extend(
            f"  occurrence {entry.observed_at} ({entry.classification}, impact "
            f"{entry.meaning_impact})"
            for entry in shown.occurrences
        )
        lines.extend(
            f"  counter-evidence {entry.qualification} in {entry.context_key}"
            for entry in shown.counter_evidence
        )
        _print(_envelope(command, shown, clock, shown.warnings), "\n".join(lines), args.format)
        return 0
    if args.action == "list":
        listed = error_service.listing(
            paths,
            track=args.track,
            status=args.status,
            live_only=args.live_only,
            limit=args.limit,
            clock=clock,
        )
        lines = [f"{listed.total} error(s), {listed.live} still live"]
        lines.extend(_error_lines(entry) for entry in listed.entries)
        _print(_envelope(command, listed, clock, listed.warnings), "\n".join(lines), args.format)
        return 0
    if args.action == "followup":
        queued = error_service.add_followup(
            paths,
            kind=args.kind,
            action=args.followup_action,
            target=args.target,
            error=args.error,
            attempt=args.attempt,
            priority=args.priority,
            track=args.track,
            clock=clock,
            command=command,
        )
        _print(
            _envelope(command, queued, clock, queued.warnings),
            f"{queued.followup_id} {queued.kind} ({queued.status}, priority "
            f"{queued.priority}): {queued.action}",
            args.format,
        )
        return 0
    queue = error_service.list_followups(
        paths, track=args.track, status=args.status, limit=args.limit, clock=clock
    )
    lines = [f"{queue.total} follow-up(s); {len(queue.entries)} shown"]
    lines.extend(
        f"  {entry.followup_id} {entry.kind} (priority {entry.priority}): {entry.action}"
        for entry in queue.entries
    )
    _print(_envelope(command, queue, clock, queue.warnings), "\n".join(lines), args.format)
    return 0


def _run_estimate(args: argparse.Namespace, clock: Clock, command: str) -> int:
    paths = _pack_workspace(args)
    if args.action == "show":
        shown = estimate_service.report(paths, track=args.track, summary=args.summary, clock=clock)
        lines = [f"{shown.track_id} in {shown.framework_id} ({shown.calculation_version})"]
        lines.extend(
            f"  {entry.dimension}: {entry.level_code or '--'} "
            f"[{entry.level_low or '--'}..{entry.level_high or '--'}] "
            f"{entry.estimate_status}, confidence {entry.confidence_label}, basis "
            f"{entry.basis}, {entry.evidence_count} observation(s)"
            for entry in shown.estimates
        )
        if shown.summary_level is not None:
            lines.append(f"  summary: {shown.summary_level} -- {shown.summary_label}")
        _print(_envelope(command, shown, clock, shown.warnings), "\n".join(lines), args.format)
        return 0
    history = estimate_service.history(
        paths, track=args.track, dimension=args.dimension, limit=args.limit, clock=clock
    )
    lines = [f"{history.total} snapshot(s); {len(history.snapshots)} shown"]
    for snapshot in history.snapshots:
        lines.append(
            f"  {snapshot.recorded_at} {snapshot.dimension}: {snapshot.level_code or '--'} "
            f"[{snapshot.level_low or '--'}..{snapshot.level_high or '--'}] "
            f"{snapshot.estimate_status} ({snapshot.basis}, {snapshot.evidence_count} "
            f"observation(s)) -- {snapshot.reason}"
        )
        lines.extend(
            f"      {factor.name} ({factor.weight:g}): {factor.detail}"
            for factor in snapshot.factors
        )
    _print(_envelope(command, history, clock, history.warnings), "\n".join(lines), args.format)
    return 0


def _run_context(args: argparse.Namespace, clock: Clock, command: str) -> int:
    paths = _pack_workspace(args)
    bundle = context_service.build(
        paths,
        scope=args.scope,
        track=args.track,
        item=getattr(args, "item", None),
        max_records=args.max_records,
        max_tokens=args.max_tokens,
        include_responses=args.include_responses,
        clock=clock,
    )
    lines = [
        f"{bundle.scope} bundle for {bundle.track_id}: {bundle.records} record(s), "
        f"~{bundle.estimated_tokens}/{bundle.token_limit} token(s)"
        + (" (bounded)" if bundle.bounded else ""),
        f"  sections: {', '.join(bundle.sections) or 'none'}",
        f"  learner responses included: {bundle.responses_included}",
    ]
    lines.extend(
        f"  omitted {omission.section}: {omission.included} of {omission.available} "
        f"({omission.reason})"
        for omission in bundle.omissions
    )
    _print(_envelope(command, bundle, clock, bundle.warnings), "\n".join(lines), args.format)
    return 0


def _session_lines(report: session_service.SessionReport) -> str:
    """A session as a person reads it: the plan, then what it is holding."""

    lines = [
        f"{report.session_id} {report.status} ({report.mode} mode, {report.energy} energy, "
        f"{report.correction_mode} correction)",
        f"  {report.planned_minutes} of {report.requested_minutes} minute(s) planned; "
        f"novelty {report.novel_targets}/{report.novel_target_cap}",
    ]
    for block in report.blocks:
        lines.append(
            f"  {block.sequence}. {block.role:8} {block.block_type:18} "
            f"{block.planned_minutes:3}m  {block.status:9} {block.objective}"
        )
        lines.extend(f"       - {reason}" for reason in block.rationale)
        if block.targets:
            lines.append(
                "       targets: "
                + ", ".join(
                    f"{target.title}{' (new)' if target.novel else ''}" for target in block.targets
                )
            )
    for omission in report.omissions:
        lines.append(f"  omitted {omission.block_type}: {omission.reason}")
    if report.batches or report.staged_events:
        lines.append(
            f"  staged: {report.staged_events} event(s) across {report.batches} batch(es)"
            + (
                f", last sequence {report.last_batch_sequence}"
                if report.last_batch_sequence
                else ""
            )
        )
    close = report.finalization
    if close is not None:
        lines.append(
            f"  closed {close.outcome}: {close.attempts_written} attempt(s), "
            f"{close.evidence_written} evidence row(s), {close.errors_written} error "
            f"occurrence(s), {close.followups_written} follow-up(s)"
        )
    if report.resume_from is not None:
        point = report.resume_from
        lines.append(
            f"  resume at block {point.sequence} ({point.block_type})"
            + (f", activity {point.activity_kind}" if point.activity_kind else "")
        )
    if report.next_actions:
        lines.append(f"  next: {', '.join(report.next_actions)}")
    return "\n".join(lines)


def _close_lines(report: session_service.CloseReport) -> str:
    lines = [
        f"{report.session_id} {report.outcome}"
        + (" (replayed: the original result)" if report.replayed else ""),
        f"  consumed {report.staged_consumed} staged event(s)"
        + (f", discarded {report.staged_discarded}" if report.staged_discarded else ""),
        f"  wrote {report.attempts_written} attempt(s), {report.evidence_written} evidence "
        f"row(s), {report.errors_written} error occurrence(s), "
        f"{report.followups_written} follow-up(s), {report.observations_written} note(s)",
    ]
    if report.comprehension_written:
        lines.append(
            f"  recorded {report.comprehension_written} comprehension observation(s) on "
            f"{', '.join(report.sources_worked)}"
        )
    for change in report.stage_changes:
        lines.append(
            f"  {change.title}: stage {change.stage_before or 'unseen'} -> {change.stage_after}"
        )
    if report.errors_touched:
        lines.append(f"  errors touched: {list(report.errors_touched)}")
    if report.dimensions_recomputed:
        lines.append(f"  estimates recomputed: {list(report.dimensions_recomputed)}")
    lines.append(f"  policy versions: {report.calculation_versions}")
    return "\n".join(lines)


def _run_plan(args: argparse.Namespace, clock: Clock, command: str) -> int:
    paths = _pack_workspace(args)
    if args.action == "create":
        report = session_service.create(
            paths,
            minutes=args.minutes,
            mode=args.mode,
            energy=args.energy,
            intent=args.intent,
            correction_mode=args.correction_mode,
            track=args.track,
            idempotency_key=args.idempotency_key,
            clock=clock,
            command=command,
        )
    else:
        report = session_service.show(paths, session=args.session, track=args.track, clock=clock)
    _print(_envelope(command, report, clock, report.warnings), _session_lines(report), args.format)
    return 0


def _run_session(args: argparse.Namespace, clock: Clock, command: str) -> int:
    paths = _pack_workspace(args)
    if args.action in ("start", "status", "resume"):
        if args.action == "start":
            report = session_service.start(
                paths, session=args.session, track=args.track, clock=clock, command=command
            )
        elif args.action == "resume":
            report = session_service.resume(
                paths, session=args.session, track=args.track, clock=clock, command=command
            )
        else:
            report = session_service.show(
                paths, session=args.session, track=args.track, clock=clock
            )
        _print(
            _envelope(command, report, clock, report.warnings),
            _session_lines(report),
            args.format,
        )
        return 0
    if args.action == "log":
        payload = _read_input(args.input_path)
        if not isinstance(payload, dict):
            raise LinguaWikiError(
                "invalid_input",
                "a batch is a lingua.session.events.v1 object with a sequence, an "
                "idempotency key, and an ordered event array",
                details=(ErrorDetail(field="input", reason="payload is not an object"),),
            )
        batch = session_service.log(
            paths,
            batch=payload,
            session=args.session,
            track=args.track,
            clock=clock,
            command=command,
        )
        lines = [
            f"batch {batch.sequence} of {batch.session_id}: {batch.event_count} event(s) "
            + ("already stored" if batch.duplicate else "staged"),
            f"  content hash {batch.content_hash}",
            f"  {batch.staged_events} staged event(s) in this batch; nothing is credited "
            "until the session closes",
        ]
        _print(_envelope(command, batch, clock, batch.warnings), "\n".join(lines), args.format)
        return 0
    if args.action == "staged":
        events = session_service.staged(
            paths,
            session=args.session,
            track=args.track,
            limit=args.limit,
            clock=clock,
        )
        listing = session_service.StagedListing(events=events, total=len(events))
        lines = [f"{len(events)} staged event(s)"]
        lines.extend(
            f"  {event.batch_sequence}.{event.sequence} {event.kind:26} {event.status:13} "
            f"{event.evidence_basis:10} {event.summary}"
            for event in events
        )
        _print(_envelope(command, listing, clock, ()), "\n".join(lines), args.format)
        return 0
    if args.action in ("close", "partial-close"):
        outcome = getattr(args, "outcome", "partial")
        closed = session_service.close(
            paths,
            outcome=outcome,
            session=args.session,
            track=args.track,
            actual_minutes=args.actual_minutes,
            fatigue=args.fatigue,
            summary=args.summary,
            discard_blocks=args.discard_block,
            idempotency_key=args.idempotency_key,
            clock=clock,
            command=command,
        )
        _print(
            _envelope(command, closed, clock, closed.warnings),
            _close_lines(closed),
            args.format,
        )
        return 0
    if args.action == "abandon":
        report = session_service.abandon(
            paths,
            session=args.session,
            track=args.track,
            reason=args.reason,
            clock=clock,
            command=command,
        )
        _print(
            _envelope(command, report, clock, report.warnings),
            _session_lines(report),
            args.format,
        )
        return 0
    if args.action == "recover":
        recovery = session_service.recover(
            paths,
            source=args.source,
            target=args.target,
            events=args.event,
            track=args.track,
            clock=clock,
            command=command,
        )
        lines = [
            f"recovered {recovery.recovered} staged event(s) from "
            f"{recovery.source_session_id} into {recovery.target_session_id}"
            + (f" as batch {recovery.batch_id}" if recovery.batch_id else ""),
        ]
        if recovery.skipped:
            lines.append(f"  {recovery.skipped} staged event(s) were left where they were")
        _print(
            _envelope(command, recovery, clock, recovery.warnings),
            "\n".join(lines),
            args.format,
        )
        return 0
    if args.action == "ingest-package":
        payload = _read_input(args.input_path)
        if not isinstance(payload, dict):
            raise LinguaWikiError(
                "invalid_input",
                "a session package is a lingua.session.v1 object",
                details=(ErrorDetail(field="input", reason="payload is not an object"),),
            )
        # Through `speaking.ingest`, not `sessions.ingest_package`. Staging a package's
        # events without storing its transcript is half an ingestion: the utterances the
        # events are *about* never arrive, so an acoustic claim has no utterance to name
        # and the close refuses work this command accepted.
        ingested = speaking_service.ingest(
            paths,
            package=payload,
            session=args.session,
            track=args.track,
            producer=args.producer,
            file_sha256=_file_digest(args.input_path),
            clock=clock,
            command=command,
        )
        lines = [
            f"{ingested.package_id} -> {ingested.session_id}: "
            + (
                "already ingested, nothing staged again"
                if ingested.duplicate
                else f"{ingested.staged_events} event(s) staged"
            ),
            f"  {ingested.imported_utterances} utterance(s) imported, "
            f"{ingested.skipped_utterances} already present",
            f"  retention: {ingested.retention_policy}; audio "
            + ("available" if ingested.audio_available else "not retained"),
        ]
        if ingested.skipped_events:
            lines.append(f"  {ingested.skipped_events} event(s) were not staged")
        _print(
            _envelope(command, ingested, clock, ingested.warnings),
            "\n".join(lines),
            args.format,
        )
        return 0
    raise LinguaWikiError("unknown_command", "command is not implemented")


def _file_digest(path: str | None) -> str | None:
    """The bytes as they arrived, for provenance alongside the canonical content hash."""

    if path is None or path == "-":
        return None
    import hashlib

    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def _run_wiki(args: argparse.Namespace, clock: Clock, command: str) -> int:
    paths = _pack_workspace(args)
    report = wiki_service.build(
        paths, view=args.view, track=args.track, clock=clock, command=command
    )
    lines = [
        f"{report.view} for {report.track_id}: {', '.join(report.files)}",
        f"  projection {report.projection_version}, hash {report.content_hash}",
        f"  source event {report.source_event_id or 'none'}"
        + (" (the projection was stale)" if report.stale_before else ""),
    ]
    _print(_envelope(command, report, clock, report.warnings), "\n".join(lines), args.format)
    return 0


def _parse_timestamp(value: str, *, field: str) -> datetime:
    """Read a wall-clock argument, refusing one without a zone.

    A session that happened "at 19:00" happened at 19:00 somewhere. Accepting a naive
    timestamp would silently record it as UTC, and every delay and chronology derived from
    it would be wrong by the learner's own offset.
    """

    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as failure:
        raise LinguaWikiError(
            "invalid_timestamp",
            f"--{field} is not an ISO 8601 timestamp: {value}",
            details=(ErrorDetail(field=field, reason=value),),
        ) from failure
    if parsed.tzinfo is None:
        raise LinguaWikiError(
            "naive_timestamp",
            f"--{field} has no time zone, and a session that happened at {value} happened "
            "at that time somewhere; add an offset or a trailing Z",
            details=(ErrorDetail(field=field, reason=value),),
        )
    return parsed


def _source_lines(report: Any) -> str:
    progress = report.progress
    lines = [
        f"{report.source_id}: {report.title}" + (f" -- {report.creator}" if report.creator else ""),
        f"  {report.kind}, {report.status}, rights {report.rights}",
    ]
    total = report.total_units if report.total_units is not None else "?"
    if progress is None:
        lines.append(f"  not started; {total} unit(s) catalogued")
    else:
        lines.append(
            f"  {progress.status}: {progress.completed_units} of {total} unit(s), "
            f"{progress.minutes_spent} minute(s)"
        )
        if progress.coverage is not None:
            lines.append(f"  coverage {progress.coverage:.0%}")
        lines.append(
            f"  understood {progress.unaided_band or 'unmeasured'} unaided, "
            f"{progress.aided_band or 'unmeasured'} with help"
        )
    for unit in report.units[:20]:
        mark = "x" if unit.completed else " "
        lines.append(f"  [{mark}] {unit.sequence}. {unit.label}")
    return "\n".join(lines)


def _run_source(args: argparse.Namespace, clock: Clock, command: str) -> int:
    paths = _pack_workspace(args)
    if args.action == "add":
        payload = _read_input(args.input_path) if args.input_path else {}
        if not isinstance(payload, dict):
            raise LinguaWikiError(
                "invalid_input",
                "the payload for `source add` is an object whose `units` array carries the "
                "units, because a unit may hold an excerpt and argv is not a place for text",
                details=(ErrorDetail(field="input", reason="payload is not an object"),),
            )
        report = source_service.add(
            paths,
            kind=args.kind,
            title=args.title,
            creator=args.creator,
            canonical_uri=args.canonical_uri,
            rights=args.rights,
            rights_note=args.rights_note,
            language=args.language,
            level_code=args.level,
            has_audio=args.has_audio,
            has_transcript=args.has_transcript,
            notes=args.notes,
            units=payload.get("units", []),
            track=args.track,
            clock=clock,
            command=command,
        )
        _print(
            _envelope(command, report, clock, report.warnings), _source_lines(report), args.format
        )
        return 0
    if args.action == "list":
        listing = source_service.listing(
            paths,
            status=args.status,
            kind=args.kind,
            limit=args.limit,
            track=args.track,
            clock=clock,
        )
        lines = [f"{listing.total} source(s)"]
        for entry in listing.sources:
            lines.append(
                f"  {entry.source_id}  {entry.kind:9} {entry.status:10} {entry.title}"
                f" -- {entry.progress.status if entry.progress else 'not started'}"
            )
        _print(_envelope(command, listing, clock, listing.warnings), "\n".join(lines), args.format)
        return 0
    if args.action == "show":
        report = source_service.show(paths, source=args.source, track=args.track, clock=clock)
        _print(
            _envelope(command, report, clock, report.warnings), _source_lines(report), args.format
        )
        return 0
    if args.action == "comprehension":
        observed = source_service.record_comprehension(
            paths,
            source=args.source,
            unit=args.unit,
            aid=args.aid,
            band=args.band,
            mode=args.mode,
            replays=args.replays,
            lookups=args.lookups,
            minutes=args.minutes,
            note=args.note,
            track=args.track,
            clock=clock,
            command=command,
        )
        lines = [
            f"{observed.aid} comprehension of {observed.band} recorded "
            f"({observed.mode}, reading {observed.sequence} of this unit)",
            f"  unaided {observed.progress.unaided_band or 'unmeasured'}, "
            f"aided {observed.progress.aided_band or 'unmeasured'}",
        ]
        _print(
            _envelope(command, observed, clock, observed.warnings), "\n".join(lines), args.format
        )
        return 0
    if args.action == "position":
        report = source_service.position(
            paths,
            source=args.source,
            unit=args.unit,
            mode=args.mode,
            minutes=args.minutes,
            track=args.track,
            clock=clock,
            command=command,
        )
        _print(
            _envelope(command, report, clock, report.warnings), _source_lines(report), args.format
        )
        return 0
    if args.action == "complete-unit":
        report = source_service.complete_unit(
            paths,
            source=args.source,
            unit=args.unit,
            minutes=args.minutes,
            track=args.track,
            clock=clock,
            command=command,
        )
        _print(
            _envelope(command, report, clock, report.warnings), _source_lines(report), args.format
        )
        return 0
    if args.action == "link":
        report = source_service.link_item(
            paths,
            source=args.source,
            unit=args.unit,
            target=args.target,
            target_kind=args.target_kind,
            relation=args.relation,
            track=args.track,
            clock=clock,
            command=command,
        )
        _print(
            _envelope(command, report, clock, report.warnings), _source_lines(report), args.format
        )
        return 0
    report = source_service.set_status(
        paths,
        source=args.source,
        status=args.status,
        track=args.track,
        clock=clock,
        command=command,
    )
    _print(_envelope(command, report, clock, report.warnings), _source_lines(report), args.format)
    return 0


def _run_artifact(args: argparse.Namespace, clock: Clock, command: str) -> int:
    paths = _pack_workspace(args)
    if args.action == "register":
        report = artifact_service.register(
            paths,
            relative_path=args.relative_path,
            kind=args.kind,
            origin=args.origin,
            rights=args.rights,
            source=args.source,
            media_type=args.media_type,
            retained=args.retained,
            track=args.track,
            clock=clock,
            command=command,
        )
        lines = [
            f"{report.artifact_id}: {report.relative_path}",
            f"  {report.kind}, {report.origin}, {report.byte_size or 0} byte(s)",
            f"  sha256 {report.sha256}",
        ]
        _print(_envelope(command, report, clock, report.warnings), "\n".join(lines), args.format)
        return 0
    if args.action == "verify":
        verified = artifact_service.verify(paths, track=args.track, clock=clock)
        # Independent counts, not nested ones. `altered` includes a tombstoned recording
        # whose file has come back, which is by definition not among the present files, so
        # "0 present, 1 of them altered" was a summary contradicting itself.
        lines = [
            f"{verified.checked} artifact(s) checked",
            f"  {verified.present} intact where the record says, "
            f"{len(verified.altered)} not what the record says",
            f"  {len(verified.missing)} missing without a tombstone, {len(verified.purged)} purged",
            f"  {len(verified.escaped)} path(s) leading outside the workspace, "
            f"{len(verified.unreadable)} unreadable",
        ]
        # Named, not counted. A count in a warning tells an operator something is wrong and
        # not which file to go and look at.
        lines.extend(f"  altered: {entry}" for entry in verified.altered)
        lines.extend(f"  missing: {entry}" for entry in verified.missing)
        lines.extend(f"  escaped: {entry}" for entry in verified.escaped)
        lines.extend(f"  unreadable: {entry}" for entry in verified.unreadable)
        _print(
            _envelope(command, verified, clock, verified.warnings), "\n".join(lines), args.format
        )
        return 0 if verified.ok else EXIT_REPORTED_FAILURE
    if args.action == "list":
        listing = artifact_service.listing(
            paths, kind=args.kind, limit=args.limit, track=args.track, clock=clock
        )
        lines = [f"{listing.total} artifact(s)"]
        for entry in listing.artifacts:
            state = f"purged ({entry.purge_reason})" if entry.purged_at else "held"
            lines.append(f"  {entry.artifact_id}  {entry.kind:10} {state:28} {entry.relative_path}")
        _print(_envelope(command, listing, clock, listing.warnings), "\n".join(lines), args.format)
        return 0
    if args.action == "clip":
        clipped = artifact_service.register(
            paths,
            relative_path=args.relative_path,
            kind="audio",
            origin="learner-recording",
            media_type=args.media_type,
            clip_of=args.clip_of,
            clip_starts_at_ms=args.clip_starts_at_ms,
            clip_ends_at_ms=args.clip_ends_at_ms,
            track=args.track,
            clock=clock,
            command=command,
        )
        window = (
            f" ({clipped.clip_starts_at_ms}-{clipped.clip_ends_at_ms}ms)"
            if clipped.clip_starts_at_ms is not None
            else ""
        )
        lines = [
            f"{clipped.artifact_id}: {clipped.relative_path}{window}",
            f"  a selected clip of {clipped.clip_of_artifact_id}",
            "  a clip is kept past the retention window while a pronunciation target it "
            "supports is unfinished; the whole recording is not",
        ]
        _print(_envelope(command, clipped, clock, clipped.warnings), "\n".join(lines), args.format)
        return 0
    if args.action == "sweep":
        swept = artifact_service.sweep(
            paths, dry_run=args.dry_run, track=args.track, clock=clock, command=command
        )
        verb = "would be" if swept.dry_run else "was"
        lines = [
            f"retention policy {swept.policy}"
            + (f" over {swept.retention_days} day(s)" if swept.retention_days else ""),
            f"  {swept.considered} recording(s) considered; {len(swept.purged)} {verb} purged",
            f"  {len(swept.invalidated_observations)} acoustic claim(s) {verb} invalidated",
        ]
        if swept.unclipped_recordings:
            lines.append(
                f"  {len(swept.unclipped_recordings)} whole recording(s) that evidence "
                "rests on are going: clip what matters first with `artifact clip`"
            )
        _print(_envelope(command, swept, clock, swept.warnings), "\n".join(lines), args.format)
        return 0
    purged = artifact_service.purge(
        paths,
        artifact=args.artifact,
        reason=args.reason,
        dry_run=args.dry_run,
        track=args.track,
        clock=clock,
        command=command,
    )
    verb = "would be" if args.dry_run else "was"
    lines = [
        f"{purged.artifact_id} ({purged.relative_path}) {verb} purged: {purged.reason}",
        f"  {len(purged.invalidated_observations)} acoustic claim(s) {verb} invalidated",
        f"  {purged.surviving_language_evidence} piece(s) of language evidence survive: what "
        "the learner said was established by the transcript, which is still here",
    ]
    _print(_envelope(command, purged, clock, purged.warnings), "\n".join(lines), args.format)
    return 0


def _transcript_text(payload: Any) -> str:
    if isinstance(payload, str):
        return payload
    if isinstance(payload, dict) and isinstance(payload.get("text"), str):
        return str(payload["text"])
    raise LinguaWikiError(
        "invalid_input",
        "the payload is the revised line: a JSON string, or an object with a `text` field "
        "and optionally the `original` it revises",
        details=(ErrorDetail(field="input", reason="no text"),),
    )


def _run_transcript(args: argparse.Namespace, clock: Clock, command: str) -> int:
    paths = _pack_workspace(args)
    if args.action == "import":
        payload = _read_input(args.input_path)
        if not isinstance(payload, dict):
            raise LinguaWikiError(
                "invalid_input",
                "a transcript import takes a lingua.session.v1 package object",
                details=(ErrorDetail(field="input", reason="payload is not an object"),),
            )
        report = transcript_service.import_package(
            paths,
            package=payload,
            ingestion_id=args.ingestion,
            track=args.track,
            clock=clock,
            command=command,
        )
        lines = [
            f"{report.imported} utterance(s) imported, {report.skipped} already present",
            f"  the learner's words are kept as {report.retention_policy}",
        ]
        _print(_envelope(command, report, clock, report.warnings), "\n".join(lines), args.format)
        return 0
    if args.action == "show":
        shown = transcript_service.show(
            paths,
            session=args.session,
            ingestion=args.ingestion,
            utterance=args.utterance,
            limit=args.limit,
            track=args.track,
            clock=clock,
        )
        lines = [f"{shown.total} utterance(s), words kept as {shown.retention_policy}"]
        for utterance in shown.utterances:
            lines.append(
                f"  {utterance.external_id} [{utterance.speaker}] best layer "
                f"{utterance.best_layer}"
                + (
                    f", disagreement: {', '.join(utterance.disagreement)}"
                    if utterance.disagreement
                    else ""
                )
            )
            if utterance.raw_text is not None:
                lines.append(f"     raw: {utterance.raw_text}")
            for revision in utterance.revisions:
                lines.append(f"     {revision.layer} ({revision.kind}): {revision.text or '--'}")
            for claim in utterance.pronunciation:
                state = claim.invalidation_reason or "standing"
                lines.append(f"     {claim.dimension} {claim.status} from {claim.basis}: {state}")
        _print(_envelope(command, shown, clock, shown.warnings), "\n".join(lines), args.format)
        return 0
    if args.action in ("normalize", "review"):
        payload = _read_input(args.input_path)
        text = _transcript_text(payload)
        original = payload.get("original") if isinstance(payload, dict) else None
        call = (
            transcript_service.normalize
            if args.action == "normalize"
            else transcript_service.review
        )
        extra: dict[str, Any] = {"confidence": args.confidence} if args.action == "review" else {}
        revision = call(
            paths,
            utterance=args.utterance,
            text=text,
            original=original,
            reviewer_kind=args.reviewer_kind,
            reviewer=args.reviewer,
            reason=args.reason,
            track=args.track,
            clock=clock,
            command=command,
            **extra,
        )
        lines = [
            f"{revision.revision_id}: {revision.layer} layer, recorded as a {revision.kind} "
            f"of the {revision.derived_from} layer"
        ]
        _print(_envelope(command, revision, clock), "\n".join(lines), args.format)
        return 0
    if args.action == "interpret":
        payload = _read_input(args.input_path) if args.input_path else {}
        if not isinstance(payload, dict):
            raise LinguaWikiError(
                "invalid_input",
                "the payload holds the meaning, the corrected form, and the explanation, "
                "because all three are language text rather than flags",
                details=(ErrorDetail(field="input", reason="payload is not an object"),),
            )
        interpreted = transcript_service.interpret(
            paths,
            utterance=args.utterance,
            classification=args.classification,
            meaning=payload.get("meaning"),
            corrected_form=payload.get("corrected_form"),
            explanation=payload.get("explanation"),
            category=args.category,
            signature=payload.get("signature"),
            attach_to=args.attach_to,
            distinct=args.distinct,
            despite_low_confidence=args.despite_low_confidence,
            override_reason=args.override_reason,
            confidence=args.confidence,
            reviewer_kind=args.reviewer_kind,
            reviewer=args.reviewer,
            track=args.track,
            clock=clock,
            command=command,
        )
        lines = [
            f"{interpreted.interpretation_id}: {interpreted.classification}",
            f"  counts against the learner, as {interpreted.error_id}"
            if interpreted.counts_against_the_learner
            else "  recorded, and counted against nobody",
        ]
        if interpreted.overrode_low_confidence:
            lines.append(
                f"  the transcriber's own uncertainty was overruled: {interpreted.override_reason}"
            )
        _print(_envelope(command, interpreted, clock), "\n".join(lines), args.format)
        return 0
    claim = transcript_service.record_pronunciation(
        paths,
        dimension=args.dimension,
        status=args.status,
        basis=args.basis,
        utterance=args.utterance,
        audio=args.audio,
        target=args.target,
        note=args.note,
        reviewer_kind=args.reviewer_kind,
        reviewer=args.reviewer,
        track=args.track,
        clock=clock,
        command=command,
    )
    lines = [f"{claim.observation_id}: {claim.dimension} {claim.status} from a {claim.basis} basis"]
    _print(_envelope(command, claim, clock), "\n".join(lines), args.format)
    return 0


def _adapted_package(args: argparse.Namespace) -> dict[str, Any]:
    payload = _read_input(args.input_path)
    if not isinstance(payload, dict):
        raise LinguaWikiError(
            "invalid_input",
            "a speaking payload is an object: either a lingua.session.v1 package or an "
            "export for the adapter named by --adapter",
            details=(ErrorDetail(field="input", reason="payload is not an object"),),
        )
    started = None
    if getattr(args, "started_at", None):
        started = _parse_timestamp(args.started_at, field="started-at")
    return speaking_service.adapt(
        payload,
        adapter=args.adapter,
        external_session_id=args.external_session_id,
        target_language=args.target_language,
        started_at=started,
        session=getattr(args, "session", None),
        track=args.track,
    )


def _run_speaking(args: argparse.Namespace, clock: Clock, command: str) -> int:
    paths = _pack_workspace(args)
    if args.action == "package":
        package = speaking_service.scaffold(
            external_session_id=args.external_session_id,
            target_language=args.target_language,
            started_at=_parse_timestamp(args.started_at, field="started-at"),
            minutes=args.minutes,
            utterances=args.utterances,
            session=args.session,
            track=args.track,
        )
        if args.out:
            Path(args.out).expanduser().write_text(
                json.dumps(package, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
            )
        envelope = GenericSuccessEnvelope(
            command=command,
            correlation_id=EventId.new(),
            generated_at=clock.now(),
            warnings=(
                "the utterance stubs are placeholders: replace the text and the times with "
                "what was actually said, then run `speaking validate` before ingesting",
            ),
            data=package,
        )
        human = (
            f"wrote a package for {args.external_session_id} to {args.out}"
            if args.out
            else json.dumps(package, indent=2, ensure_ascii=False)
        )
        _print(envelope, human, args.format)
        return 0
    if args.action == "validate":
        report = speaking_service.validate(
            paths,
            package=_adapted_package(args),
            session=args.session,
            track=args.track,
            clock=clock,
        )
        lines = [
            f"{report.package_id or 'this package'}: "
            + ("valid" if report.valid else "not ingestable"),
            f"  layers {', '.join(report.layers) or 'none'}; {report.utterances} utterance(s), "
            f"{report.events} event(s), {report.artifacts} artifact(s)",
            f"  words would be kept as {report.retention_policy}",
        ]
        lines.extend(f"  problem: {problem}" for problem in report.problems)
        _print(_envelope(command, report, clock, report.warnings), "\n".join(lines), args.format)
        return 0 if report.valid else EXIT_REPORTED_FAILURE
    ingested = speaking_service.ingest(
        paths,
        package=_adapted_package(args),
        session=args.session,
        track=args.track,
        producer=args.producer,
        clock=clock,
        command=command,
    )
    lines = [
        f"{ingested.ingestion_id}: {ingested.staged_events} event(s) staged, "
        f"{ingested.imported_utterances} utterance(s) imported",
        f"  session {ingested.session_id}"
        + (" (already ingested, so nothing was staged again)" if ingested.duplicate else ""),
        "  nothing is credited until the session closes",
    ]
    _print(_envelope(command, ingested, clock, ingested.warnings), "\n".join(lines), args.format)
    return 0


def _run_privacy(args: argparse.Namespace, clock: Clock, command: str) -> int:
    paths = _pack_workspace(args)
    report = privacy_service.audit(paths, track=args.track, clock=clock)
    retention = report.retention
    lines = [
        "privacy audit: " + ("nothing private is escaping" if report.ok else "ACTION NEEDED"),
        f"  {report.paths.candidates_checked} file(s) checked for Git, "
        f"{len(report.paths.violations)} violation(s)",
        f"  {len(report.content_leaks)} committed page(s) hold private content",
        f"  {len(report.log_leaks)} audit log entr(ies) hold private content",
        f"  words kept as {retention.transcript_policy}; "
        f"{retention.utterances_with_words} of {retention.utterances_held} utterance(s) "
        "hold their words",
        f"  {retention.artifacts_held} recording(s) held, {retention.artifacts_purged} purged; "
        f"{retention.claims_resting_on_audio} claim(s) rest on audio",
        f"  audio retention: {retention.audio_retention_policy}"
        + (
            f" over {retention.audio_retention_days} day(s)"
            if retention.audio_retention_days
            else ""
        ),
    ]
    if retention.unkept_files_still_present:
        lines.append(
            f"  {len(retention.unkept_files_still_present)} recording(s) are recorded as "
            "removed and their files are still here: "
            + ", ".join(retention.unkept_files_still_present)
        )
    lines.extend(f"  path: {entry.path} -- {entry.reason}" for entry in report.paths.violations)
    lines.extend(f"  leak: {entry.path} -- {entry.reason}" for entry in report.content_leaks)
    lines.extend(f"  log:  {entry.path} -- {entry.reason}" for entry in report.log_leaks)
    if report.purge_consequences:
        lines.append("  purging would cost:")
        lines.extend(
            f"    {entry.artifact_id} ({entry.relative_path}): "
            f"{entry.invalidated_claims} acoustic claim(s) invalidated, "
            f"{entry.surviving_language_evidence} piece(s) of language evidence untouched"
            for entry in report.purge_consequences
        )
    _print(_envelope(command, report, clock, report.warnings), "\n".join(lines), args.format)
    return 0 if report.ok else EXIT_REPORTED_FAILURE


def _dispatch(args: argparse.Namespace, clock: Clock, command: str) -> int:
    if args.group == "status":
        return _run_status(args, clock)
    if args.group == "workspace":
        return _run_workspace(args, clock, command)
    if args.group == "db":
        return _run_db(args, clock, command)
    if args.group == "skills":
        return _run_skills(args, clock, command)
    if args.group == "pack":
        if args.action == "author":
            return _run_pack_author(args, clock, command)
        if args.action == "template":
            return _run_pack_template(args, clock, command)
        return _run_pack(args, clock, command)
    if args.group == "user":
        return _run_user(args, clock, command)
    if args.group == "track":
        return _run_track(args, clock, command)
    if args.group == "onboard":
        return _run_onboard(args, clock, command)
    if args.group == "resources":
        return _run_resources(args, clock, command)
    if args.group == "curriculum":
        return _run_curriculum(args, clock, command)
    if args.group == "assessment":
        return _run_assessment(args, clock, command)
    if args.group == "knowledge":
        return _run_knowledge(args, clock, command)
    if args.group == "evidence":
        return _run_evidence(args, clock, command)
    if args.group == "errors":
        return _run_errors(args, clock, command)
    if args.group == "estimate":
        return _run_estimate(args, clock, command)
    if args.group == "context":
        return _run_context(args, clock, command)
    if args.group == "plan":
        return _run_plan(args, clock, command)
    if args.group == "session":
        return _run_session(args, clock, command)
    if args.group == "source":
        return _run_source(args, clock, command)
    if args.group == "artifact":
        return _run_artifact(args, clock, command)
    if args.group == "transcript":
        return _run_transcript(args, clock, command)
    if args.group == "speaking":
        return _run_speaking(args, clock, command)
    if args.group == "privacy":
        return _run_privacy(args, clock, command)
    if args.group == "client":
        return _run_client(args, clock, command)
    if args.group == "wiki":
        return _run_wiki(args, clock, command)
    raise LinguaWikiError("unknown_command", "command is not implemented")


def run(argv: Sequence[str] | None = None, *, clock: Clock | None = None) -> int:
    parser = _parser()
    arguments = list(argv) if argv is not None else sys.argv[1:]
    command = _command_name(arguments, parser)
    active_clock = clock or SystemClock()
    try:
        args = parser.parse_args(arguments)
        return _dispatch(args, active_clock, command)
    except ParserExit as exc:
        if exc.message:
            output = sys.stdout if exc.status == 0 else sys.stderr
            print(exc.message, end="", file=output)
        return exc.status
    except LinguaWikiError as exc:
        print(_failure(command, exc, active_clock).model_dump_json(), file=sys.stderr)
        return EXIT_ERROR
    except ValidationError as exc:
        details = tuple(
            ErrorDetail(field=".".join(str(part) for part in item["loc"]), reason=item["msg"])
            for item in exc.errors()
        )
        error = LinguaWikiError(
            # Not "output": a payload arriving through `--input` fails here too, and
            # telling a caller their *output* contract failed sends them looking in the
            # wrong place entirely.
            "invalid_contract",
            "contract validation failed",
            details=details,
        )
        print(_failure(command, error, active_clock).model_dump_json(), file=sys.stderr)
        return EXIT_ERROR
    except Exception as exc:
        error = LinguaWikiError(
            "internal_error",
            "an unexpected internal error occurred",
            retryable=False,
            details=(ErrorDetail(reason=type(exc).__name__),),
        )
        print(_failure(command, error, active_clock).model_dump_json(), file=sys.stderr)
        if os.environ.get("LINGUAWIKI_DEBUG") == "1":
            traceback.print_exc(file=sys.stderr)
        return EXIT_ERROR


def main() -> None:
    raise SystemExit(run())


__all__ = ["ContractArgumentParser", "main", "run"]
