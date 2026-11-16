"""修订结果导出：影响窗口与跨窗采样的可复核 CSV。

两份 CSV 都带旧值/新值对照列，值班员拿旧版批次号即可逐项复核；
修订批次本身的网格/采纳/隔离 CSV 仍走 M7 的既有导出（传 corrected run_id）。
"""

from __future__ import annotations

import csv
import io

WINDOW_COLUMNS = (
    "row_no",
    "sensor_id",
    "window_start",
    "old_observed",
    "new_observed",
    "old_expected",
    "new_expected",
    "old_coverage",
    "new_coverage",
    "old_state",
    "new_state",
    "state_changed",
)

MOVED_COLUMNS = (
    "row_no",
    "source_row",
    "sensor_id",
    "old_bucket",
    "new_bucket",
    "old_window",
    "new_window",
    "crossed_window",
    "old_offset",
    "new_offset",
)


def _write(columns: tuple[str, ...], rows: list[list]) -> str:
    buffer = io.StringIO()
    writer = csv.writer(buffer, lineterminator="\n")
    writer.writerow(columns)
    writer.writerows(rows)
    return "﻿" + buffer.getvalue()


def window_changes_csv(view: dict) -> str:
    rows = []
    for index, item in enumerate(view["window_changes"], 1):
        rows.append(
            [
                index,
                item["sensor_id"],
                item["window_start"],
                item["old_observed"],
                item["new_observed"],
                item["old_expected"],
                item["new_expected"],
                item["old_coverage"],
                item["new_coverage"],
                item["old_state"],
                item["new_state"],
                "YES" if item.get("state_changed") else "NO",
            ]
        )
    return _write(WINDOW_COLUMNS, rows)


def moved_samples_csv(view: dict) -> str:
    rows = []
    for index, item in enumerate(view["moved"], 1):
        rows.append(
            [
                index,
                item["source_row"],
                item["sensor_id"],
                item["old_bucket"],
                item["new_bucket"],
                item["old_window"],
                item["new_window"],
                "YES" if item["crossed_window"] else "NO",
                item["old_offset"],
                item["new_offset"],
            ]
        )
    return _write(MOVED_COLUMNS, rows)
