"""Domain models / DTOs."""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import List, Optional


class Status(str, Enum):
    SUCCESS = "success"
    FAILED = "failed"
    SKIPPED = "skipped"
    DRY_RUN = "dry_run"


@dataclass
class FileResult:
    path: str
    size: int
    status: Status
    message: Optional[str] = None


@dataclass
class UserResult:
    user: str
    user_id: Optional[str] = None
    drive_id: Optional[str] = None
    status: Status = Status.SUCCESS
    message: Optional[str] = None
    files: List[FileResult] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "user": self.user,
            "user_id": self.user_id,
            "drive_id": self.drive_id,
            "status": self.status.value,
            "message": self.message,
            "files": [
                {
                    "path": f.path,
                    "size": f.size,
                    "status": f.status.value,
                    "message": f.message,
                }
                for f in self.files
            ],
        }


@dataclass
class BulkReport:
    total: int
    succeeded: int
    failed: int
    skipped: int
    results: List[UserResult]

    def to_dict(self) -> dict:
        return {
            "total": self.total,
            "succeeded": self.succeeded,
            "failed": self.failed,
            "skipped": self.skipped,
            "results": [r.to_dict() for r in self.results],
        }
