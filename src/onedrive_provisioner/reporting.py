"""CSV / JSON report writers."""
from __future__ import annotations

import csv
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import List

from .models import BulkReport


def write_reports(report: BulkReport, output_dir: str | Path, formats: List[str]) -> List[Path]:
    out_dir = Path(output_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    written: list[Path] = []

    if "json" in formats:
        p = out_dir / f"onedrive-report-{ts}.json"
        p.write_text(json.dumps(report.to_dict(), indent=2), encoding="utf-8")
        written.append(p)

    if "csv" in formats:
        p = out_dir / f"onedrive-report-{ts}.csv"
        with p.open("w", newline="", encoding="utf-8") as fh:
            w = csv.writer(fh)
            w.writerow([
                "user", "user_id", "drive_id", "user_status", "user_message",
                "file_path", "file_size", "file_status", "file_message",
            ])
            for r in report.results:
                if not r.files:
                    w.writerow([r.user, r.user_id or "", r.drive_id or "",
                                r.status.value, r.message or "", "", "", "", ""])
                for f in r.files:
                    w.writerow([
                        r.user, r.user_id or "", r.drive_id or "",
                        r.status.value, r.message or "",
                        f.path, f.size, f.status.value, f.message or "",
                    ])
        written.append(p)

    return written
