"""Data models for GitHub EMU enablement results."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional


@dataclass
class GitHubEnableResult:
    email: str
    status: str  # "added" | "already-member" | "invited" | "failed" | "skipped"
    user_id: Optional[str] = None
    group_id: Optional[str] = None
    invited: bool = False
    sync_triggered: bool = False
    github_username: Optional[str] = None
    message: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        return {
            "email": self.email,
            "status": self.status,
            "userId": self.user_id,
            "groupId": self.group_id,
            "invited": self.invited,
            "syncTriggered": self.sync_triggered,
            "githubUsername": self.github_username,
            "message": self.message,
        }


@dataclass
class GitHubEnableReport:
    total: int
    added: int
    already: int
    invited: int
    failed: int
    sync_triggered: int
    results: List[GitHubEnableResult] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "total": self.total,
            "added": self.added,
            "alreadyMember": self.already,
            "invited": self.invited,
            "failed": self.failed,
            "syncTriggered": self.sync_triggered,
            "results": [r.to_dict() for r in self.results],
        }
