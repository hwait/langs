"""Small Stage 0 CLI surface with a stable JSON envelope."""

from __future__ import annotations

import argparse
import os
import sys
import traceback
from collections.abc import Sequence
from typing import Never

from pydantic import ValidationError

from linguawiki import __version__
from linguawiki.clock import Clock, SystemClock
from linguawiki.contracts import ErrorEnvelope, StatusData, StatusEnvelope
from linguawiki.errors import ErrorDetail, LinguaWikiError
from linguawiki.ids import EventId


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


def _parser() -> ContractArgumentParser:
    parser = ContractArgumentParser(prog="linguawiki")
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    subcommands = parser.add_subparsers(dest="command", required=True)
    status = subcommands.add_parser("status", help="show core runtime status")
    status.add_argument("--format", choices=("human", "json"), default="human")
    return parser


def _status() -> StatusData:
    return StatusData(application_version=__version__)


def _success(command: str, data: StatusData, clock: Clock) -> StatusEnvelope:
    return StatusEnvelope(
        command=command,
        correlation_id=EventId.new(),
        generated_at=clock.now(),
        data=data,
    )


def _failure(command: str, error: LinguaWikiError, clock: Clock) -> ErrorEnvelope:
    return ErrorEnvelope(
        command=command,
        correlation_id=EventId.new(),
        generated_at=clock.now(),
        error=error.payload,
    )


def _command_name(arguments: Sequence[str], parser: ContractArgumentParser) -> str:
    subcommands = next(
        action for action in parser._actions if isinstance(action, argparse._SubParsersAction)
    )
    return next((argument for argument in arguments if argument in subcommands.choices), "unknown")


def run(argv: Sequence[str] | None = None, *, clock: Clock | None = None) -> int:
    parser = _parser()
    arguments = list(argv) if argv is not None else sys.argv[1:]
    command = _command_name(arguments, parser)
    active_clock = clock or SystemClock()
    try:
        args = parser.parse_args(arguments)
        if args.command == "status":
            envelope = _success("status", _status(), active_clock)
            if args.format == "json":
                print(envelope.model_dump_json())
            else:
                print(
                    f"LinguaWiki {envelope.data.application_version} "
                    f"(contract v{envelope.data.contract_schema_version}, Stage 0)"
                )
            return 0
        raise LinguaWikiError("unknown_command", "command is not implemented")
    except ParserExit as exc:
        if exc.message:
            output = sys.stdout if exc.status == 0 else sys.stderr
            print(exc.message, end="", file=output)
        return exc.status
    except LinguaWikiError as exc:
        print(_failure(command, exc, active_clock).model_dump_json(), file=sys.stderr)
        return 2
    except ValidationError as exc:
        details = tuple(
            ErrorDetail(field=".".join(str(part) for part in item["loc"]), reason=item["msg"])
            for item in exc.errors()
        )
        error = LinguaWikiError(
            "invalid_contract", "output contract validation failed", details=details
        )
        print(_failure(command, error, active_clock).model_dump_json(), file=sys.stderr)
        return 2
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
        return 2


def main() -> None:
    raise SystemExit(run())
