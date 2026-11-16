"""不变量：行账守恒、网格守恒、幂等重跑、补齐不回流。"""

from __future__ import annotations

from decimal import Decimal

from app import store
from app.modules.m6_batches import service

from expected import EDGE, GAP, MAIN


def test_row_account_conserved(conn):
    """行账守恒：原始行 = 采纳 + 重复 + 隔离，换样例也成立。"""
    for sample, expected in (("telemetry_raw", MAIN), ("gap_series", GAP), ("midnight_edge", EDGE)):
        result = service.execute_run(conn, sample=sample, offsets="device_offsets")
        counts = result["counts"]
        assert counts["raw_records"] == (
            counts["accepted"] + counts["duplicates"] + counts["quarantined"]
        ), sample
        assert counts["raw_records"] == expected["raw_records"]
        assert result["balanced"] is True


def test_grid_account_conserved(conn, run_main):
    """网格守恒：应有桶 = 实有 + 补齐 + 缺口，且等于逐传感器应有之和。"""
    counts = run_main["counts"]
    assert counts["expected_buckets"] == (
        counts["observed"] + counts["filled"] + counts["gaps"]
    )
    per_sensor = store.rows(
        conn,
        "SELECT sensor_id, COUNT(*) AS n FROM grid WHERE run_id = ? GROUP BY sensor_id",
        (run_main["run_id"],),
    )
    assert sum(row["n"] for row in per_sensor) == counts["expected_buckets"]
    assert counts["grid_points"] == counts["expected_buckets"]


def test_rerun_is_idempotent(conn, run_main):
    """幂等重跑：行数、合计、导出摘要都不变。"""
    before_runs = store.value(conn, "SELECT COUNT(*) FROM runs", (), 0)
    before_grid = store.value(conn, "SELECT COUNT(*) FROM grid", (), 0)
    before_sum = Decimal(run_main["totals"]["grid_sum"])

    again = service.execute_run(conn, sample="telemetry_raw", offsets="device_offsets")
    assert again["reused"] is True
    assert store.value(conn, "SELECT COUNT(*) FROM runs", (), 0) == before_runs
    assert store.value(conn, "SELECT COUNT(*) FROM grid", (), 0) == before_grid
    assert Decimal(again["totals"]["grid_sum"]) == before_sum
    assert again["counts"]["accepted"] == MAIN["accepted"]

    fresh = service.execute_run(conn, sample="telemetry_raw", offsets="device_offsets")
    assert fresh["counts"] == again["counts"]
    assert fresh["totals"] == again["totals"]


def test_filled_values_do_not_leak_into_accepted(conn, run_main):
    """补齐值不回流采纳合计：数值账两边都只算实有。"""
    totals = run_main["totals"]
    assert Decimal(totals["accepted_sum"]) == Decimal(totals["observed_sum"])
    assert Decimal(totals["grid_sum"]) == Decimal(totals["accepted_sum"]) + Decimal(
        totals["filled_sum"]
    )
    accepted_rows = store.value(
        conn, "SELECT COUNT(*) FROM samples WHERE run_id = ?", (run_main["run_id"],), 0
    )
    assert accepted_rows == MAIN["accepted"]
    filled_rows = store.value(
        conn,
        "SELECT COUNT(*) FROM grid WHERE run_id = ? AND state = 'FILLED'",
        (run_main["run_id"],),
        0,
    )
    assert filled_rows == MAIN["filled"]


def test_watermark_monotonic_across_replay(conn, run_main):
    """迟到只由到达顺序决定：同一文件重放得到同一批迟到标记。"""
    late_before = [
        dict(row)
        for row in store.rows(
            conn, "SELECT sensor_id, bucket_utc FROM samples WHERE run_id = ? AND late = 1",
            (run_main["run_id"],),
        )
    ]
    second = service.execute_run(conn, sample="telemetry_raw", offsets="device_offsets")
    late_after = [
        dict(row)
        for row in store.rows(
            conn, "SELECT sensor_id, bucket_utc FROM samples WHERE run_id = ? AND late = 1",
            (second["run_id"],),
        )
    ]
    assert late_before == late_after
    assert len(late_before) == MAIN["late"]


def test_revision_never_mutates_base_tables(conn, run_main):
    """修订不变量：更正批次的每一张表都写新 run_id，基批七张表逐行不变。"""
    from app.modules.m8_revision import service as revision_service

    tables = ("samples", "grid", "windows", "offsets", "quarantine", "duplicates", "reconcile", "intake")
    before = {
        name: [dict(row) for row in store.rows(conn, f"SELECT * FROM {name} WHERE run_id = ?", (run_main["run_id"],))]
        for name in tables
    }
    revision_service.apply(conn, run_id=run_main["run_id"], offsets="device_offsets_corrected")
    after = {
        name: [dict(row) for row in store.rows(conn, f"SELECT * FROM {name} WHERE run_id = ?", (run_main["run_id"],))]
        for name in tables
    }
    assert before == after


def test_revision_idempotent_under_repeated_apply(conn, run_main):
    """修订不变量：同一修订连跑三次，修订表、批次表与影响明细都不重复计入。"""
    from app.modules.m8_revision import service as revision_service

    first = revision_service.apply(conn, run_id=run_main["run_id"], offsets="device_offsets_corrected")
    for _ in range(2):
        again = revision_service.apply(conn, run_id=run_main["run_id"], offsets="device_offsets_corrected")
        assert again["revision"]["revision_id"] == first["revision"]["revision_id"]
    assert store.value(conn, "SELECT COUNT(*) FROM revisions", (), 0) == 1
    assert store.value(conn, "SELECT COUNT(*) FROM runs WHERE run_kind = 'REVISED'", (), 0) == 1
    impact_rows = store.value(conn, "SELECT COUNT(*) FROM revision_impact", (), 0)
    assert impact_rows == first["impact"]["moved_samples"] + first["impact"]["affected_windows"]
