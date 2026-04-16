from __future__ import annotations

import csv
import os
import stat
from pathlib import Path

from app.csv_writer import CSVWriter
from app.member_export import MemberExportRow
from app.models import FieldResult


def test_csv_writer_uses_semicolon_and_utf8_sig(tmp_path: Path) -> None:
    writer = CSVWriter()
    rows = [
        MemberExportRow(
            user_id=10,
            first_name="Alice",
            last_name="Example",
            username="alice",
            phone_number=FieldResult.from_value("+15550000010"),
            about=FieldResult.from_value("About text"),
            birthday=FieldResult.empty(),
            photo_path=FieldResult.from_value(".runtime/exports/run/avatars/10_alice.jpg"),
            linked_channel_url=FieldResult.unavailable(),
        )
    ]

    path = writer.write(tmp_path / "members.csv", [row.to_csv_row() for row in rows])

    raw = path.read_bytes()
    assert raw.startswith(b"\xef\xbb\xbf")

    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle, delimiter=";")
        data = list(reader)

    assert reader.fieldnames == [
        "user_id",
        "first_name",
        "last_name",
        "username",
        "phone_number",
        "phone_number_status",
        "about",
        "about_status",
        "birthday",
        "birthday_status",
        "photo_path",
        "photo_path_status",
        "linked_channel_url",
        "linked_channel_url_status",
    ]
    assert data == [
        {
            "user_id": "10",
            "first_name": "Alice",
            "last_name": "Example",
            "username": "alice",
            "phone_number": "+15550000010",
            "phone_number_status": "value",
            "about": "About text",
            "about_status": "value",
            "birthday": "",
            "birthday_status": "empty",
            "photo_path": ".runtime/exports/run/avatars/10_alice.jpg",
            "photo_path_status": "value",
            "linked_channel_url": "",
            "linked_channel_url_status": "unavailable",
        }
    ]

    if os.name == "posix":
        assert stat.S_IMODE(path.stat().st_mode) == 0o600
        assert stat.S_IMODE(path.parent.stat().st_mode) == 0o700
