"""修订影响分析：对比旧批次与纠错重算结果，定位跨窗口采样与覆盖度变化。

只做纯计算，输入是两条流水线产物（m6._build_pipeline 的结果），不碰数据库，
因此同一份输入无论预览多少次都得到同一份影响清单。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime


def _window_of(moment: datetime) -> datetime:
    from app.modules.m2_timebase import contract

    return contract.align_window(moment, 15)


def _text(moment: datetime) -> str:
    return moment.strftime("%Y-%m-%dT%H:%M:%SZ")


@dataclass(frozen=True)
class MovedSample:
    source_row: int
    sensor_id: str
    old_bucket: str
    new_bucket: str
    old_window: str
    new_window: str
    crossed_window: bool
    old_offset: int
    new_offset: int


@dataclass
class WindowChange:
    sensor_id: str
    window_start: str
    old_observed: int
    new_observed: int
    old_expected: int
    new_expected: int
    old_coverage: str
    new_coverage: str
    old_state: str
    new_state: str

    @property
    def changed(self) -> bool:
        return (
            self.old_observed != self.new_observed
            or self.old_expected != self.new_expected
            or self.old_coverage != self.new_coverage
            or self.old_state != self.new_state
        )

    def as_dict(self) -> dict:
        return {
            "sensor_id": self.sensor_id,
            "window_start": self.window_start,
            "old_observed": self.old_observed,
            "new_observed": self.new_observed,
            "old_expected": self.old_expected,
            "new_expected": self.new_expected,
            "old_coverage": self.old_coverage,
            "new_coverage": self.new_coverage,
            "old_state": self.old_state,
            "new_state": self.new_state,
            "state_changed": self.old_state != self.new_state,
        }


@dataclass
class Impact:
    moved: list[MovedSample] = field(default_factory=list)
    window_changes: list[WindowChange] = field(default_factory=list)
    old_counts: dict = field(default_factory=dict)
    new_counts: dict = field(default_factory=dict)
    old_totals: dict = field(default_factory=dict)
    new_totals: dict = field(default_factory=dict)

    @property
    def moved_samples(self) -> int:
        return len(self.moved)

    @property
    def crossed_count(self) -> int:
        return sum(1 for item in self.moved if item.crossed_window)

    @property
    def affected_windows(self) -> int:
        return len(self.window_changes)

    def summary(self) -> dict:
        return {
            "moved_samples": self.moved_samples,
            "crossed_window_samples": self.crossed_count,
            "affected_windows": self.affected_windows,
            "state_changed_windows": sum(1 for item in self.window_changes if item.old_state != item.new_state),
            "old_counts": self.old_counts,
            "new_counts": self.new_counts,
            "old_totals": self.old_totals,
            "new_totals": self.new_totals,
        }

    def as_dict(self) -> dict:
        payload = self.summary()
        payload["moved"] = [item.__dict__ for item in self.moved]
        payload["window_changes"] = [item.as_dict() for item in self.window_changes]
        return payload


def _sample_index(pipeline) -> dict[tuple[int, str], object]:
    """(source_row, sensor_id) -> 采纳行；采纳行在两条流水线里同源同行号。"""
    return {(row.source_row, row.sensor_id): row for row in pipeline["aligned"].kept}


def _window_index(pipeline) -> dict[tuple[str, datetime], object]:
    analysis = pipeline["analysis"]
    if analysis is None:
        return {}
    index = {}
    for item in analysis.windows:
        index[(item.sensor_id, item.start)] = item
    return index


def compare_pipelines(old_pipeline, new_pipeline) -> Impact:
    """对比同一采样文件在旧/新偏移表下的两条流水线，给出迁移采样与窗口变化。"""
    impact = Impact(
        old_counts=dict(old_pipeline["counts"]),
        new_counts=dict(new_pipeline["counts"]),
        old_totals=dict(old_pipeline["totals"]),
        new_totals=dict(new_pipeline["totals"]),
    )

    old_rows = _sample_index(old_pipeline)
    new_rows = _sample_index(new_pipeline)
    for key in sorted(set(old_rows) & set(new_rows)):
        before, after = old_rows[key], new_rows[key]
        if before.bucket == after.bucket:
            continue
        old_window = _window_of(before.bucket)
        new_window = _window_of(after.bucket)
        impact.moved.append(
            MovedSample(
                source_row=before.source_row,
                sensor_id=before.sensor_id,
                old_bucket=_text(before.bucket),
                new_bucket=_text(after.bucket),
                old_window=_text(old_window),
                new_window=_text(new_window),
                crossed_window=old_window != new_window,
                old_offset=before.offset,
                new_offset=after.offset,
            )
        )

    old_windows = _window_index(old_pipeline)
    new_windows = _window_index(new_pipeline)
    for key in sorted(set(old_windows) | set(new_windows)):
        before = old_windows.get(key)
        after = new_windows.get(key)
        change = WindowChange(
            sensor_id=key[0],
            window_start=_text(key[1]),
            old_observed=0 if before is None else before.observed,
            new_observed=0 if after is None else after.observed,
            old_expected=0 if before is None else before.expected,
            new_expected=0 if after is None else after.expected,
            old_coverage="0.000" if before is None else str(before.coverage),
            new_coverage="0.000" if after is None else str(after.coverage),
            old_state="MISSING" if before is None else before.state,
            new_state="MISSING" if after is None else after.state,
        )
        # 两侧都没有实有桶的窗口只是分析区间扫过的空窗，不算受影响窗口。
        if change.old_observed == 0 and change.new_observed == 0:
            continue
        if change.changed:
            impact.window_changes.append(change)
    return impact
