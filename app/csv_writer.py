"""CSV serialization for exported Telegram members."""

from __future__ import annotations

import csv
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Sequence

from .models import FieldResult

PRIVATE_DIRECTORY_MODE = 0o700
PRIVATE_FILE_MODE = 0o600


def _best_effort_chmod(path: Path, mode: int) -> None:
    try:
        path.chmod(mode)
    except OSError:
        pass

CSV_COLUMNS = [
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


@dataclass(frozen=True, slots=True)
class CsvExportRow:
    user_id: int
    first_name: str
    last_name: str
    username: str
    phone_number: FieldResult[str]
    about: FieldResult[str]
    birthday: FieldResult[str]
    photo_path: FieldResult[str]
    linked_channel_url: FieldResult[str]


def _field_value(result: FieldResult[str]) -> str:
    return "" if result.value is None else str(result.value)


def row_to_dict(row: CsvExportRow) -> dict[str, str]:
    return {
        "user_id": str(row.user_id),
        "first_name": row.first_name,
        "last_name": row.last_name,
        "username": row.username,
        "phone_number": _field_value(row.phone_number),
        "phone_number_status": row.phone_number.status,
        "about": _field_value(row.about),
        "about_status": row.about.status,
        "birthday": _field_value(row.birthday),
        "birthday_status": row.birthday.status,
        "photo_path": _field_value(row.photo_path),
        "photo_path_status": row.photo_path.status,
        "linked_channel_url": _field_value(row.linked_channel_url),
        "linked_channel_url_status": row.linked_channel_url.status,
    }


class CSVWriter:
    def __init__(
        self,
        *,
        delimiter: str = ";",
        encoding: str = "utf-8-sig",
    ) -> None:
        self.delimiter = delimiter
        self.encoding = encoding

    def write(self, path: Path, rows: Sequence[CsvExportRow] | Iterable[CsvExportRow]) -> Path:
        path.parent.mkdir(parents=True, exist_ok=True)
        if os.name == "posix":
            _best_effort_chmod(path.parent, PRIVATE_DIRECTORY_MODE)
        with path.open("w", encoding=self.encoding, newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=CSV_COLUMNS, delimiter=self.delimiter)
            writer.writeheader()
            for row in rows:
                writer.writerow(row_to_dict(row))
        if os.name == "posix":
            _best_effort_chmod(path, PRIVATE_FILE_MODE)
        return path
