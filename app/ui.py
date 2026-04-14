"""Interactive terminal UI for Telegram session management and exports."""

from __future__ import annotations

import asyncio
import sys
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Protocol

try:  # pragma: no cover - import availability is exercised via runtime smoke
    import questionary
    from questionary import Choice
except ImportError:  # pragma: no cover - fallback for environments without deps
    questionary = None
    Choice = None

from .dialog_search import DialogCandidate, find_dialog_candidates
from .member_export import ExportSummary, export_members
from .session_manager import (
    NoActiveSessionError,
    SessionLoginError,
    SessionManager,
    SessionPrompts,
)

MENU_EXPORT = "Export members"
MENU_CREATE_SESSION = "Create new session"
MENU_SWITCH_SESSION = "Switch active session"
MENU_EXIT = "Exit"


class PromptCancelled(RuntimeError):
    """Raised when the user cancels an interactive prompt."""


@dataclass(frozen=True, slots=True)
class PromptChoice:
    title: str
    value: Any


class PromptBackend(Protocol):
    def ask_text(
        self,
        message: str,
        *,
        default: str | None = None,
        secret: bool = False,
    ) -> str | None: ...

    def ask_select(
        self,
        message: str,
        choices: Sequence[PromptChoice],
        *,
        default: Any | None = None,
    ) -> Any | None: ...

    def ask_confirm(
        self,
        message: str,
        *,
        default: bool = False,
    ) -> bool | None: ...


class QuestionaryPromptBackend:
    def _require_questionary(self) -> Any:
        if questionary is None:
            raise RuntimeError("questionary is not installed")
        return questionary

    @staticmethod
    def _unwrap(result: Any) -> Any:
        if result is None:
            raise PromptCancelled()
        return result

    def ask_text(
        self,
        message: str,
        *,
        default: str | None = None,
        secret: bool = False,
    ) -> str | None:
        library = self._require_questionary()
        prompt = (
            library.password(message, default=default)
            if secret
            else library.text(message, default=default)
        )
        try:
            return self._unwrap(prompt.ask())
        except KeyboardInterrupt as exc:  # pragma: no cover - interactive only
            raise PromptCancelled() from exc

    def ask_select(
        self,
        message: str,
        choices: Sequence[PromptChoice],
        *,
        default: Any | None = None,
    ) -> Any | None:
        library = self._require_questionary()
        rendered_choices = [Choice(choice.title, value=choice.value) for choice in choices]
        try:
            return self._unwrap(
                library.select(message, choices=rendered_choices, default=default).ask()
            )
        except KeyboardInterrupt as exc:  # pragma: no cover - interactive only
            raise PromptCancelled() from exc

    def ask_confirm(
        self,
        message: str,
        *,
        default: bool = False,
    ) -> bool | None:
        library = self._require_questionary()
        try:
            return self._unwrap(library.confirm(message, default=default).ask())
        except KeyboardInterrupt as exc:  # pragma: no cover - interactive only
            raise PromptCancelled() from exc


class ScriptedPromptBackend:
    """Deterministic prompt backend used by tests."""

    def __init__(
        self,
        *,
        text_responses: Sequence[str | None] = (),
        select_responses: Sequence[Any | None] = (),
        confirm_responses: Sequence[bool | None] = (),
    ) -> None:
        self._text_responses = list(text_responses)
        self._select_responses = list(select_responses)
        self._confirm_responses = list(confirm_responses)
        self.text_messages: list[tuple[str, bool]] = []
        self.select_messages: list[tuple[str, tuple[str, ...]]] = []
        self.confirm_messages: list[tuple[str, bool]] = []

    def _pop(self, responses: list[Any | None]) -> Any | None:
        if not responses:
            raise AssertionError("scripted prompt backend ran out of responses")
        return responses.pop(0)

    def ask_text(
        self,
        message: str,
        *,
        default: str | None = None,
        secret: bool = False,
    ) -> str | None:
        self.text_messages.append((message, secret))
        response = self._pop(self._text_responses)
        if response is None:
            return None
        return str(response)

    def ask_select(
        self,
        message: str,
        choices: Sequence[PromptChoice],
        *,
        default: Any | None = None,
    ) -> Any | None:
        self.select_messages.append((message, tuple(choice.title for choice in choices)))
        return self._pop(self._select_responses)

    def ask_confirm(
        self,
        message: str,
        *,
        default: bool = False,
    ) -> bool | None:
        self.confirm_messages.append((message, default))
        response = self._pop(self._confirm_responses)
        if response is None:
            return None
        return bool(response)


class UIFlowPrompts(SessionPrompts):
    def __init__(self, backend: PromptBackend) -> None:
        self.backend = backend

    def request_phone(self, session_name: str) -> str:
        response = self.backend.ask_text(
            f"Phone number for session {session_name!r}:",
        )
        if response is None:
            raise PromptCancelled()
        return response

    def request_code(
        self,
        session_name: str,
        phone_number: str,
        attempt_number: int,
    ) -> str:
        response = self.backend.ask_text(
            f"Login code for {session_name!r} ({phone_number}), attempt {attempt_number}:",
        )
        if response is None:
            raise PromptCancelled()
        return response

    def request_password(self, session_name: str, attempt_number: int) -> str:
        response = self.backend.ask_text(
            f"Telegram password for {session_name!r}, attempt {attempt_number}:",
            secret=True,
        )
        if response is None:
            raise PromptCancelled()
        return response
def _is_interactive_terminal() -> bool:
    stdin = getattr(sys, "stdin", None)
    stdout = getattr(sys, "stdout", None)
    return bool(
        stdin is not None
        and stdout is not None
        and stdin.isatty()
        and stdout.isatty()
    )


def _mask_middle(value: str, *, keep_prefix: int = 3, keep_suffix: int = 2) -> str:
    text = value.strip()
    if len(text) <= keep_prefix + keep_suffix + 1:
        return text[:1] + "…" + text[-1:] if len(text) > 2 else "…"
    return f"{text[:keep_prefix]}…{text[-keep_suffix:]}"


def _mask_phone_number(phone_number: str | None) -> str | None:
    if phone_number is None:
        return None
    text = phone_number.strip()
    if not text:
        return None
    return _mask_middle(text, keep_prefix=5 if text.startswith("+") else 3, keep_suffix=4)


def _session_context(session) -> str:
    label = session.account_label or _mask_phone_number(session.phone_number) or "unknown account"
    return f"{session.session_name} | {label}"


def _session_choice_label(session, *, active_session_name: str | None) -> str:
    active_marker = " *" if session.session_name == active_session_name else ""
    return f"{session.session_name} | {session.account_label or _mask_phone_number(session.phone_number) or 'unknown account'}{active_marker}"


def _dialog_date_label(value: datetime | None) -> str:
    if value is None:
        return "-"
    aware_value = value if value.tzinfo is not None else value.replace(tzinfo=timezone.utc)
    return aware_value.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")


def _dialog_choice_label(candidate: DialogCandidate) -> str:
    username = f"@{candidate.username}" if candidate.username else "-"
    participants = (
        str(candidate.participants_count)
        if candidate.participants_count is not None
        else "-"
    )
    return (
        f"{candidate.title} | {candidate.entity_type} | peer {candidate.peer_id} | "
        f"{username} | participants {participants} | last { _dialog_date_label(candidate.last_message_date) }"
    )


def _short_export_status(summary: ExportSummary) -> str:
    warnings = f", {len(summary.warnings)} warnings" if summary.warnings else ""
    return f"{summary.exported_count} exported to {summary.csv_path.name}{warnings}"


def _detailed_export_status(summary: ExportSummary) -> list[str]:
    lines = [
        f"Exported {summary.exported_count} members from {summary.chat_label or 'selected chat'}",
        f"CSV: {summary.csv_path}",
        f"Avatars: {summary.avatars_dir}",
    ]
    if summary.failed_user_ids:
        lines.append(f"Failed user ids: {', '.join(str(user_id) for user_id in summary.failed_user_ids)}")
    if summary.warnings:
        lines.append("Warnings:")
        lines.extend(f"  - {warning}" for warning in summary.warnings)
    return lines


class TerminalUI:
    def __init__(
        self,
        session_manager: SessionManager,
        *,
        backend: PromptBackend | None = None,
        printer: Any = print,
    ) -> None:
        self.session_manager = session_manager
        self.backend = backend or QuestionaryPromptBackend()
        self.printer = printer
        self.session_prompts = UIFlowPrompts(self.backend)
        self.last_export_summary: ExportSummary | None = None

    def _print_status(self) -> None:
        active_session = self.session_manager.get_active_session()
        if active_session is None:
            self.printer("Active session: none")
        else:
            self.printer(f"Active session: {_session_context(active_session)}")

        if self.last_export_summary is None:
            self.printer("Last export: none")
        else:
            self.printer(f"Last export: {_short_export_status(self.last_export_summary)}")

    def _ask_text(self, message: str, *, secret: bool = False) -> str:
        while True:
            response = self.backend.ask_text(message, secret=secret)
            if response is None:
                raise PromptCancelled()
            text = response.strip()
            if text:
                return text
            self.printer("Value must not be empty.")

    def _ask_positive_int(self, message: str) -> int:
        while True:
            value = self._ask_text(message)
            try:
                parsed = int(value)
            except ValueError:
                self.printer("Value must be a positive integer.")
                continue
            if parsed <= 0:
                self.printer("Value must be a positive integer.")
                continue
            return parsed

    def _ask_confirm(self, message: str, *, default: bool = False) -> bool:
        response = self.backend.ask_confirm(message, default=default)
        if response is None:
            raise PromptCancelled()
        return bool(response)

    def _ask_select(self, message: str, choices: Sequence[PromptChoice]) -> Any:
        response = self.backend.ask_select(message, choices)
        if response is None:
            raise PromptCancelled()
        return response

    async def run(self) -> int:
        if not _is_interactive_terminal():
            self.printer(
                "Interactive terminal required. Run `python3 main.py` from a TTY."
            )
            return 1

        self.printer("Telegram Members Export")
        while True:
            self.printer("")
            self._print_status()
            try:
                action = self._ask_select(
                    "Choose an action:",
                    [
                        PromptChoice(MENU_EXPORT, MENU_EXPORT),
                        PromptChoice(MENU_CREATE_SESSION, MENU_CREATE_SESSION),
                        PromptChoice(MENU_SWITCH_SESSION, MENU_SWITCH_SESSION),
                        PromptChoice(MENU_EXIT, MENU_EXIT),
                    ],
                )
            except PromptCancelled:
                self.printer("Exiting.")
                return 0
            except KeyboardInterrupt:  # pragma: no cover - interactive only
                self.printer("Exiting.")
                return 0

            if action == MENU_EXIT:
                self.printer("Exiting.")
                return 0

            try:
                if action == MENU_EXPORT:
                    await self.export_members_flow()
                elif action == MENU_CREATE_SESSION:
                    await self.create_session_flow()
                elif action == MENU_SWITCH_SESSION:
                    await self.switch_active_session_flow()
            except PromptCancelled:
                self.printer("Operation cancelled.")
            except SessionLoginError as exc:
                self.printer(f"Session login failed: {exc}")
            except NoActiveSessionError as exc:
                self.printer(str(exc))
            except Exception as exc:  # pragma: no cover - defensive runtime guard
                self.printer(f"Operation failed: {exc}")

    async def create_session_flow(self) -> None:
        session_name = self._ask_text("Session name:")
        api_id = self._ask_positive_int("API_ID:")
        api_hash = self._ask_text("API_HASH:", secret=True)

        session = await self.session_manager.create_session(
            session_name=session_name,
            api_id=api_id,
            api_hash=api_hash,
            prompts=self.session_prompts,
            mark_active=False,
        )
        self.printer(
            f"Created session {session.session_name!r} for "
            f"{session.account_label or _mask_phone_number(session.phone_number) or 'unknown account'}"
        )

        if self._ask_confirm("Mark this session as active?", default=True):
            session = self.session_manager.set_active_session(session.session_name)
            self.printer(f"Active session set to {session.session_name!r}")

    async def switch_active_session_flow(self) -> None:
        sessions = self.session_manager.list_sessions()
        if not sessions:
            self.printer("No saved sessions found.")
            return

        active_session = self.session_manager.get_active_session()
        choice = self._ask_select(
            "Select the active session:",
            [
                PromptChoice(
                    _session_choice_label(session, active_session_name=active_session.session_name if active_session else None),
                    session.session_name,
                )
                for session in sessions
            ],
        )
        session = self.session_manager.set_active_session(str(choice))
        self.printer(f"Active session set to {_session_context(session)}")

    async def _ensure_active_session(self) -> Any | None:
        active_session = self.session_manager.get_active_session()
        if active_session is not None:
            return active_session

        choice = self._ask_select(
            "No active session is selected. Choose how to continue:",
            [
                PromptChoice(MENU_CREATE_SESSION, MENU_CREATE_SESSION),
                PromptChoice(MENU_SWITCH_SESSION, MENU_SWITCH_SESSION),
                PromptChoice(MENU_EXIT, MENU_EXIT),
            ],
        )
        if choice == MENU_CREATE_SESSION:
            await self.create_session_flow()
        elif choice == MENU_SWITCH_SESSION:
            await self.switch_active_session_flow()
        else:
            return None

        return self.session_manager.get_active_session()

    async def _choose_dialog(self, query: str, candidates: Sequence[DialogCandidate]) -> DialogCandidate | None:
        if not candidates:
            self.printer(f"No dialogs matched {query!r}.")
            return None
        if len(candidates) == 1:
            return candidates[0]

        chosen = self._ask_select(
            "Multiple dialogs matched. Choose one:",
            [PromptChoice(_dialog_choice_label(candidate), candidate) for candidate in candidates],
        )
        return chosen if isinstance(chosen, DialogCandidate) else None

    async def export_members_flow(self) -> None:
        session = await self._ensure_active_session()
        if session is None:
            self.printer("Export cancelled: no active session.")
            return

        query = self._ask_text("Paste the group or supergroup title:")
        if not query.strip():
            raise ValueError("Group title must not be empty")

        async with self.session_manager.open_authorized_client(session.session_name) as client:
            candidates = await find_dialog_candidates(client, query)
            candidate = await self._choose_dialog(query, candidates)
            if candidate is None:
                return

            gateway = self.session_manager.gateway.bind_client(client)
            chat = await gateway.get_entity(candidate.peer_id)
            summary = await export_members(
                gateway,
                chat,
                runtime_dir=self.session_manager.state_store.runtime_dir,
                chat_label=candidate.title,
            )

        self.last_export_summary = summary
        self.printer("")
        for line in _detailed_export_status(summary):
            self.printer(line)


def run_app() -> int:
    if not _is_interactive_terminal():
        print("Interactive terminal required. Run `python3 main.py` from a TTY.")
        return 1

    from .state_store import StateStore

    session_manager = SessionManager(StateStore())
    ui = TerminalUI(session_manager)
    try:
        return asyncio.run(ui.run())
    except KeyboardInterrupt:  # pragma: no cover - interactive only
        return 130


def main() -> int:
    return run_app()
