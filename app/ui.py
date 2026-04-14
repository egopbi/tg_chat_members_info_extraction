"""Interactive terminal UI for Telegram session management and exports."""

from __future__ import annotations

import asyncio
import logging
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

from telethon import errors

from .dialog_search import DialogCandidate, find_dialog_candidates
from .member_export import ExportProgressSnapshot, ExportSummary, export_members
from .models import SessionMeta
from .runtime_logging import configured_runtime_log_path
from .session_manager import (
    NoActiveSessionError,
    SessionLoginError,
    SessionAuthorizationError,
    SessionManager,
)

MENU_EXPORT = "Export members"
MENU_CREATE_SESSION = "Create new session"
MENU_SWITCH_SESSION = "Switch active session"
MENU_EXIT = "Exit"
logger = logging.getLogger(__name__)
_PROGRESS_BAR_WIDTH = 24


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
        prompt_kwargs: dict[str, Any] = {}
        if default is not None:
            prompt_kwargs["default"] = default
        prompt = (
            library.password(message, **prompt_kwargs)
            if secret
            else library.text(message, **prompt_kwargs)
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


@dataclass(frozen=True, slots=True)
class LoginPreparation:
    authorized: bool
    user: Any | None = None
    phone_code_hash: str | None = None


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


def _render_export_progress(snapshot: ExportProgressSnapshot) -> str:
    details = (
        f"seen {snapshot.observed} | exported {snapshot.exported} | skipped {snapshot.skipped} | "
        f"duplicates {snapshot.deduplicated} | failed {snapshot.failed}"
    )
    if snapshot.total is None:
        return f"Export progress {snapshot.processed} processed | {details}"
    total = max(snapshot.total, 0)
    if total == 0:
        bar = "-" * _PROGRESS_BAR_WIDTH
        return f"Export progress [{bar}] 0/0 | {details}"
    completed = min(snapshot.processed, total)
    filled = min(
        _PROGRESS_BAR_WIDTH,
        int((completed / total) * _PROGRESS_BAR_WIDTH),
    )
    bar = "#" * filled + "-" * (_PROGRESS_BAR_WIDTH - filled)
    return f"Export progress [{bar}] {completed}/{total} | {details}"


@dataclass(slots=True)
class LiveProgressWriter:
    stream: Any
    _last_width: int = 0

    def update(self, snapshot: ExportProgressSnapshot) -> None:
        line = _render_export_progress(snapshot)
        self._last_width = max(self._last_width, len(line))
        self.stream.write(f"\r{line.ljust(self._last_width)}")
        if snapshot.is_final:
            self.stream.write("\n")
            self._last_width = 0
        self.stream.flush()


class TerminalUI:
    def __init__(
        self,
        session_manager: SessionManager,
        *,
        backend: PromptBackend | None = None,
        printer: Any = print,
        status_stream: Any | None = None,
    ) -> None:
        self.session_manager = session_manager
        self.backend = backend or QuestionaryPromptBackend()
        self.printer = printer
        self.status_stream = status_stream or sys.stdout
        self.last_export_summary: ExportSummary | None = None

    def _run_async(self, awaitable: Any) -> Any:
        return asyncio.run(awaitable)

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
        self.printer(
            f"Log file: {configured_runtime_log_path(self.session_manager.state_store.runtime_dir)}"
        )

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

    def run(self) -> int:
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
                logger.info("Selected UI action %s", action)
                if action == MENU_EXPORT:
                    self.export_members_flow()
                elif action == MENU_CREATE_SESSION:
                    self.create_session_flow()
                elif action == MENU_SWITCH_SESSION:
                    self.switch_active_session_flow()
            except PromptCancelled:
                self.printer("Operation cancelled.")
            except SessionLoginError as exc:
                logger.exception("Session login failed")
                self.printer(f"Session login failed: {exc}")
            except NoActiveSessionError as exc:
                logger.exception("Operation failed because no active session is selected")
                self.printer(str(exc))
            except SessionAuthorizationError as exc:
                logger.exception("Operation failed because the session is not authorized")
                self.printer(str(exc))
            except Exception as exc:  # pragma: no cover - defensive runtime guard
                logger.exception("Operation failed")
                self.printer(f"Operation failed: {exc}")

    def _normalize_phone_number(self, value: str) -> str:
        phone_number = value.strip()
        return phone_number if phone_number.startswith("+") else f"+{phone_number}"

    def _describe_account(self, user: Any, phone_number: str | None = None) -> str | None:
        username = getattr(user, "username", None)
        if username:
            return f"@{username}"

        first_name = getattr(user, "first_name", None)
        last_name = getattr(user, "last_name", None)
        display_name = " ".join(part for part in [first_name, last_name] if part)
        if display_name.strip():
            return display_name.strip()

        phone = getattr(user, "phone", None) or phone_number
        if phone:
            return self._normalize_phone_number(str(phone))
        return None

    async def _prepare_login(self, session_name: str, api_id: int, api_hash: str, phone_number: str) -> LoginPreparation:
        async with self.session_manager.gateway.open_client(session_name, api_id, api_hash) as client:
            authorized = await self.session_manager.gateway.run_with_retry(
                client.is_user_authorized,
                operation_name="check Telegram authorization",
            )
            if authorized:
                return LoginPreparation(
                    authorized=True,
                    user=await self.session_manager.gateway.get_current_user(client),
                )

            sent_code = await self.session_manager.gateway.request_login_code(
                client,
                phone_number,
            )
            phone_code_hash = getattr(sent_code, "phone_code_hash", None)
            if not phone_code_hash:
                raise SessionLoginError("Telegram did not return a phone code hash")
            return LoginPreparation(authorized=False, phone_code_hash=phone_code_hash)

    async def _sign_in_with_code(
        self,
        *,
        session_name: str,
        api_id: int,
        api_hash: str,
        phone_number: str,
        code: str,
        phone_code_hash: str,
    ) -> Any:
        async with self.session_manager.gateway.open_client(session_name, api_id, api_hash) as client:
            await self.session_manager.gateway.sign_in(
                client,
                phone=phone_number,
                code=code,
                phone_code_hash=phone_code_hash,
            )
            return await self.session_manager.gateway.get_current_user(client)

    async def _sign_in_with_password(
        self,
        *,
        session_name: str,
        api_id: int,
        api_hash: str,
        password: str,
    ) -> Any:
        async with self.session_manager.gateway.open_client(session_name, api_id, api_hash) as client:
            await self.session_manager.gateway.sign_in(client, password=password)
            return await self.session_manager.gateway.get_current_user(client)

    def _save_session(self, session_name: str, api_id: int, api_hash: str, user: Any, phone_number: str) -> SessionMeta:
        try:
            existing_session = self.session_manager.state_store.load_session(session_name)
            created_at = existing_session.created_at
        except FileNotFoundError:
            created_at = datetime.now(timezone.utc)

        session = SessionMeta(
            session_name=session_name,
            api_id=api_id,
            api_hash=api_hash,
            created_at=created_at,
            updated_at=datetime.now(timezone.utc),
            account_label=self._describe_account(user, phone_number),
            phone_number=self._normalize_phone_number(phone_number),
            is_active=False,
        )
        self.session_manager.state_store.save_session(session)
        return session

    def create_session_flow(self) -> None:
        session_name = self._ask_text("Session name:")
        api_id = self._ask_positive_int("API_ID:")
        api_hash = self._ask_text("API_HASH:", secret=True)
        logger.info("Starting create-session flow for %r", session_name)

        phone_number = self._ask_text(f"Phone number for session {session_name!r}:")
        login_prelude = self._run_async(
            self._prepare_login(session_name, api_id, api_hash, phone_number)
        )
        if login_prelude.authorized:
            user = login_prelude.user
        else:
            if login_prelude.phone_code_hash is None:
                raise SessionLoginError("Telegram did not return a phone code hash")

            current_hash = login_prelude.phone_code_hash
            user = None
            for code_attempt in range(1, 4):
                code = self._ask_text(
                    f"Login code for {session_name!r} ({phone_number}), attempt {code_attempt}:"
                )
                try:
                    user = self._run_async(
                        self._sign_in_with_code(
                            session_name=session_name,
                            api_id=api_id,
                            api_hash=api_hash,
                            phone_number=phone_number,
                            code=code,
                            phone_code_hash=current_hash,
                        )
                    )
                    break
                except errors.PhoneCodeInvalidError as exc:
                    if code_attempt >= 3:
                        raise SessionLoginError("Telegram login code was rejected") from exc
                    continue
                except errors.PhoneCodeExpiredError as exc:
                    if code_attempt >= 3:
                        raise SessionLoginError("Telegram login code expired") from exc
                    login_prelude = self._run_async(
                        self._prepare_login(session_name, api_id, api_hash, phone_number)
                    )
                    if login_prelude.phone_code_hash is None:
                        raise SessionLoginError("Telegram did not return a phone code hash")
                    current_hash = login_prelude.phone_code_hash
                    continue
                except errors.SessionPasswordNeededError:
                    for password_attempt in range(1, 4):
                        password = self._ask_text(
                            f"Telegram password for {session_name!r}, attempt {password_attempt}:",
                            secret=True,
                        )
                        try:
                            user = self._run_async(
                                self._sign_in_with_password(
                                    session_name=session_name,
                                    api_id=api_id,
                                    api_hash=api_hash,
                                    password=password,
                                )
                            )
                            break
                        except errors.PasswordHashInvalidError as exc:
                            if password_attempt >= 3:
                                raise SessionLoginError("Telegram password was rejected") from exc
                            continue
                    if user is not None:
                        break

            if user is None:
                raise SessionLoginError("Telegram login code attempts were exhausted")

        session = self._save_session(session_name, api_id, api_hash, user, phone_number)
        logger.info("Created session metadata for %r", session.session_name)
        self.printer(
            f"Created session {session.session_name!r} for "
            f"{session.account_label or _mask_phone_number(session.phone_number) or 'unknown account'}"
        )

        if self._ask_confirm("Mark this session as active?", default=True):
            session = self.session_manager.set_active_session(session.session_name)
            logger.info("Marked %r as the active session from UI", session.session_name)
            self.printer(f"Active session set to {session.session_name!r}")

    def switch_active_session_flow(self) -> None:
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
        logger.info("Switched the active session to %r", session.session_name)
        self.printer(f"Active session set to {_session_context(session)}")

    def _ensure_active_session(self) -> Any | None:
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
            self.create_session_flow()
        elif choice == MENU_SWITCH_SESSION:
            self.switch_active_session_flow()
        else:
            return None

        return self.session_manager.get_active_session()

    def _choose_dialog(self, query: str, candidates: Sequence[DialogCandidate]) -> DialogCandidate | None:
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

    async def _load_dialog_candidates(
        self,
        session_name: str,
        query: str,
    ) -> list[DialogCandidate]:
        async with self.session_manager.open_authorized_client(session_name) as client:
            return await find_dialog_candidates(client, query)

    async def _export_selected_dialog(
        self,
        session_name: str,
        candidate: DialogCandidate,
        progress_callback: Any | None = None,
    ) -> ExportSummary:
        async with self.session_manager.open_authorized_client(session_name) as client:
            gateway = self.session_manager.gateway.bind_client(client)
            chat = await gateway.get_entity(candidate.peer_id)
            return await export_members(
                gateway,
                chat,
                expected_total=candidate.participants_count,
                runtime_dir=self.session_manager.state_store.runtime_dir,
                progress_callback=progress_callback,
                chat_label=candidate.title,
            )

    def export_members_flow(self) -> None:
        session = self._ensure_active_session()
        if session is None:
            self.printer("Export cancelled: no active session.")
            return

        query = self._ask_text("Paste the group or supergroup title:")
        if not query.strip():
            raise ValueError("Group title must not be empty")

        candidates = self._run_async(self._load_dialog_candidates(session.session_name, query))
        candidate = self._choose_dialog(query, candidates)
        if candidate is None:
            return

        logger.info(
            "Starting export for session=%r peer_id=%s title=%r expected_total=%s",
            session.session_name,
            candidate.peer_id,
            candidate.title,
            candidate.participants_count,
        )
        progress_writer = LiveProgressWriter(self.status_stream)
        summary = self._run_async(
            self._export_selected_dialog(
                session.session_name,
                candidate,
                progress_callback=progress_writer.update,
            )
        )

        self.last_export_summary = summary
        logger.info(
            "Completed export run_id=%s exported=%d warnings=%d",
            summary.run_id,
            summary.exported_count,
            len(summary.warnings),
        )
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
        logger.info("Starting interactive terminal UI")
        return ui.run()
    except KeyboardInterrupt:  # pragma: no cover - interactive only
        logger.info("Interactive terminal UI interrupted")
        return 130


def main() -> int:
    return run_app()
