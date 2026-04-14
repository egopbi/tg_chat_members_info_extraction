from __future__ import annotations

import asyncio
from dataclasses import dataclass
from pathlib import Path

from app.avatar_store import AvatarStore, safe_filename
from app.member_export import ExportProgressSnapshot, ExportSummary, export_members


@dataclass(slots=True)
class FakeBirthday:
    day: int
    month: int
    year: int | None = None


@dataclass(slots=True)
class FakeParticipant:
    id: int
    first_name: str | None = None
    last_name: str | None = None
    username: str | None = None
    photo: object | None = None


@dataclass(slots=True)
class FakeFullUser:
    about: str | None
    birthday: object | None
    personal_channel_id: int | None


@dataclass(slots=True)
class FakeChannel:
    username: str | None


class RetryFloodError(RuntimeError):
    def __init__(self, seconds: int) -> None:
        super().__init__(f"Flood wait {seconds}")
        self.seconds = seconds


class FakeGateway:
    def __init__(self) -> None:
        self.me = FakeParticipant(id=1, first_name="Current", username="current")
        self.participants = [
            self.me,
            FakeParticipant(id=2, first_name="Alice", last_name="Example", username="alice", photo=object()),
            FakeParticipant(id=2, first_name="Alice", last_name="Example", username="alice", photo=object()),
            FakeParticipant(id=3, first_name="Bob", last_name=None, username=None, photo=None),
            FakeParticipant(id=4, first_name="Retry", last_name="User", username="retry", photo=object()),
        ]
        self.full_users = {
            2: FakeFullUser(about="About Alice", birthday=FakeBirthday(1, 2, 2000), personal_channel_id=77),
            3: FakeFullUser(about="", birthday=None, personal_channel_id=88),
        }
        self.channels = {
            77: FakeChannel(username="alice-channel"),
            88: FakeChannel(username=None),
        }
        self.download_calls = 0
        self.full_user_calls = 0
        self.retry_failures = 0
        self.participant_iteration_calls = 0

    async def get_me(self) -> FakeParticipant:
        return self.me

    def iter_participants(self, chat: object):
        self.participant_iteration_calls += 1

        async def generator():
            for index, participant in enumerate(self.participants):
                yield participant
                if self.participant_iteration_calls == 1 and index == 1:
                    raise RetryFloodError(seconds=30)

        return generator()

    async def get_full_user(self, user: FakeParticipant) -> FakeFullUser:
        self.full_user_calls += 1
        if user.id == 4:
            self.retry_failures += 1
            if self.retry_failures <= 4:
                raise RetryFloodError(seconds=30)
            raise RuntimeError("full user failed permanently")
        if user.id == 5:
            raise AttributeError("broken adapter")
        return self.full_users[user.id]

    async def get_entity(self, peer: object) -> FakeChannel:
        return self.channels[int(peer)]

    async def download_profile_photo(self, entity: object, file: Path) -> str | None:
        self.download_calls += 1
        if getattr(entity, "id", None) == 4:
            raise RetryFloodError(seconds=30)
        file.write_bytes(b"avatar-bytes")
        return str(file)


def test_safe_filename_strips_unicode_and_separators() -> None:
    assert safe_filename(42, "Мария 👋", "I/O") == "42_i_o"


def test_export_members_is_best_effort_and_summary_rich(tmp_path: Path) -> None:
    gateway = FakeGateway()
    sleep_calls: list[float] = []

    async def sleep(seconds: float) -> None:
        sleep_calls.append(seconds)

    summary: ExportSummary = asyncio.run(
        export_members(
            gateway,
            chat=object(),
            runtime_dir=tmp_path / ".runtime",
            run_id="run-001",
            sleep=sleep,
            jitter=lambda _: 0.0,
            chat_label="Test Group",
        )
    )

    assert summary.chat_label == "Test Group"
    assert summary.current_user_id == 1
    assert summary.total_seen == 7
    assert summary.exported_count == 3
    assert summary.skipped_current_account == 2
    assert summary.deduplicated_count == 2
    assert summary.failed_user_ids == (4,)
    assert summary.csv_path.exists()
    assert summary.avatars_dir == tmp_path / ".runtime" / "exports" / "run-001" / "avatars"
    assert summary.warnings
    assert len(sleep_calls) == 9
    assert sleep_calls == [30.0] * 9

    rows = {row.user_id: row for row in summary.rows}
    assert rows[2].about.status == "value"
    assert rows[2].birthday.status == "value"
    assert rows[2].linked_channel_url.status == "value"
    assert rows[2].linked_channel_url.value == "https://t.me/alice-channel"
    assert rows[2].photo_path.status == "value"
    assert rows[2].photo_path.value.endswith("2_alice_example_alice.jpg")

    assert rows[4].about.status == "error"
    assert rows[4].birthday.status == "error"
    assert rows[4].linked_channel_url.status == "error"
    assert rows[4].photo_path.status == "error"

    assert rows[3].about.status == "empty"
    assert rows[3].birthday.status == "empty"
    assert rows[3].linked_channel_url.status == "empty"
    assert rows[3].photo_path.status == "empty"

    csv_bytes = summary.csv_path.read_bytes()
    assert csv_bytes.startswith(b"\xef\xbb\xbf")


def test_export_members_emits_progress_snapshots_with_expected_total(tmp_path: Path) -> None:
    gateway = FakeGateway()
    gateway.participants = [
        gateway.me,
        FakeParticipant(id=2, first_name="Alice", last_name="Example", username="alice", photo=object()),
        FakeParticipant(id=2, first_name="Alice", last_name="Example", username="alice", photo=object()),
        FakeParticipant(id=3, first_name="Bob", last_name=None, username=None, photo=None),
    ]
    gateway.participant_iteration_calls = 1

    snapshots: list[ExportProgressSnapshot] = []

    summary = asyncio.run(
        export_members(
            gateway,
            chat=object(),
            runtime_dir=tmp_path / ".runtime",
            run_id="run-progress",
            expected_total=10,
            sleep=lambda _: asyncio.sleep(0),
            jitter=lambda _: 0.0,
            progress_callback=snapshots.append,
        )
    )

    assert summary.exported_count == 2
    assert summary.skipped_current_account == 1
    assert summary.deduplicated_count == 1
    assert summary.failed_user_ids == ()
    assert [snapshot.total for snapshot in snapshots] == [10, 10, 10, 10, 10, 10]
    assert [snapshot.processed for snapshot in snapshots] == [0, 1, 2, 2, 3, 3]
    assert [snapshot.observed for snapshot in snapshots] == [0, 1, 2, 3, 4, 4]
    assert [snapshot.exported for snapshot in snapshots] == [0, 0, 1, 1, 2, 2]
    assert [snapshot.skipped for snapshot in snapshots] == [0, 1, 1, 1, 1, 1]
    assert [snapshot.deduplicated for snapshot in snapshots] == [0, 0, 0, 1, 1, 1]
    assert [snapshot.failed for snapshot in snapshots] == [0, 0, 0, 0, 0, 0]
    assert snapshots[-1].is_final is True


def test_export_members_emits_final_progress_snapshot_when_row_fails(tmp_path: Path) -> None:
    gateway = FakeGateway()
    gateway.participants = [
        gateway.me,
        FakeParticipant(id=4, first_name="Retry", last_name="User", username="retry", photo=object()),
    ]
    gateway.participant_iteration_calls = 1

    snapshots: list[ExportProgressSnapshot] = []

    summary = asyncio.run(
        export_members(
            gateway,
            chat=object(),
            runtime_dir=tmp_path / ".runtime",
            run_id="run-progress-failure",
            expected_total=2,
            sleep=lambda _: asyncio.sleep(0),
            jitter=lambda _: 0.0,
            progress_callback=snapshots.append,
        )
    )

    assert summary.exported_count == 1
    assert summary.failed_user_ids == (4,)
    assert snapshots[-1].is_final is True
    assert snapshots[-1].processed == 2
    assert snapshots[-1].observed == 2
    assert snapshots[-1].exported == 1
    assert snapshots[-1].failed == 1


def test_export_members_stops_after_four_waits_for_one_operation(tmp_path: Path) -> None:
    gateway = FakeGateway()
    gateway.participants = [gateway.me, FakeParticipant(id=4, first_name="Retry", last_name="User", username="retry", photo=None)]
    gateway.participant_iteration_calls = 1

    sleep_calls: list[float] = []

    async def sleep(seconds: float) -> None:
        sleep_calls.append(seconds)

    summary = asyncio.run(
        export_members(
            gateway,
            chat=object(),
            runtime_dir=tmp_path / ".runtime",
            run_id="run-002",
            sleep=sleep,
            jitter=lambda _: 0.0,
        )
    )

    assert summary.exported_count == 1
    assert summary.failed_user_ids == (4,)
    assert sleep_calls == [30.0, 30.0, 30.0, 30.0]
    assert summary.rows[0].about.status == "error"
    assert summary.rows[0].photo_path.status == "empty"


def test_export_members_retries_participant_enumeration_mid_stream(tmp_path: Path) -> None:
    gateway = FakeGateway()
    gateway.participants = [
        gateway.me,
        FakeParticipant(id=2, first_name="Alice", last_name="Example", username="alice", photo=object()),
        FakeParticipant(id=3, first_name="Bob", last_name=None, username=None, photo=None),
    ]

    sleep_calls: list[float] = []

    async def sleep(seconds: float) -> None:
        sleep_calls.append(seconds)

    summary = asyncio.run(
        export_members(
            gateway,
            chat=object(),
            runtime_dir=tmp_path / ".runtime",
            run_id="run-003",
            sleep=sleep,
            jitter=lambda _: 0.0,
        )
    )

    assert gateway.participant_iteration_calls == 2
    assert sleep_calls == [30.0]
    assert summary.exported_count == 2
    assert summary.failed_user_ids == ()
    assert {row.user_id for row in summary.rows} == {2, 3}
    assert summary.rows[1].photo_path.status == "empty"


def test_export_members_treats_attribute_error_as_runtime_error(tmp_path: Path) -> None:
    gateway = FakeGateway()
    gateway.participants = [
        gateway.me,
        FakeParticipant(id=5, first_name="Broken", last_name="Adapter", username="broken", photo=None),
    ]

    summary = asyncio.run(
        export_members(
            gateway,
            chat=object(),
            runtime_dir=tmp_path / ".runtime",
            run_id="run-004",
            sleep=lambda _: asyncio.sleep(0),
            jitter=lambda _: 0.0,
        )
    )

    assert summary.exported_count == 1
    assert summary.failed_user_ids == (5,)
    row = summary.rows[0]
    assert row.about.status == "error"
    assert row.birthday.status == "error"
    assert row.linked_channel_url.status == "error"
    assert row.photo_path.status == "empty"
