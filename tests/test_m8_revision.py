"""M8 时钟校准修订：影响窗口预览、确认落版、幂等重跑、失败保旧版与复核导出。"""

from __future__ import annotations

import csv
import io

import pytest

from app import store
from app.errors import InputError, NotFoundError
from app.modules.m6_batches import history, service as batch_service
from app.modules.m8_revision import csv_out, service

CORRECTED = "device_offsets_corrected"
BAD_TABLE = "device_offsets_bad"


# —— 影响分析：跨 15 分钟边界 ——

def test_preview_lists_crossed_samples_and_affected_windows(conn, run_main):
    """纠错表把 TH-01 偏移 20s 改成 95s，4 条采样跨过 15 分钟窗口边界。"""
    view = service.preview(conn, run_id=run_main["run_id"], offsets=CORRECTED)
    assert view["reused"] is False
    assert view["revision"]["status"] == "PREVIEWED"

    impact = view["impact"]
    assert impact["moved_samples"] == 26
    assert impact["crossed_window_samples"] == 4
    assert impact["affected_windows"] == 2

    crossed = {(m["source_row"], m["old_window"], m["new_window"]) for m in view["moved"] if m["crossed_window"]}
    # 行 49（00:15:20 采样）从 00:15 窗口退回到 00:00 窗口
    assert (49, "2026-03-01T00:15:00Z", "2026-03-01T00:00:00Z") in crossed
    # 行 1/8 退到前一日 23:45 窗口
    assert (1, "2026-03-01T00:00:00Z", "2026-02-28T23:45:00Z") in crossed

    windows = {(w["sensor_id"], w["window_start"]): w for w in view["window_changes"]}
    flipped = windows[("TH-01", "2026-03-01T00:15:00Z")]
    assert (flipped["old_observed"], flipped["new_observed"]) == (13, 11)
    assert (flipped["old_state"], flipped["new_state"]) == ("OK", "PARTIAL")
    assert flipped["state_changed"] is True


def test_preview_does_not_write_any_run_data(conn, run_main):
    """预览只登记修订审计与影响明细，不新增批次、网格或窗口行。"""
    runs_before = store.value(conn, "SELECT COUNT(*) FROM runs", (), 0)
    grid_before = store.value(conn, "SELECT COUNT(*) FROM grid", (), 0)
    service.preview(conn, run_id=run_main["run_id"], offsets=CORRECTED)
    assert store.value(conn, "SELECT COUNT(*) FROM runs", (), 0) == runs_before
    assert store.value(conn, "SELECT COUNT(*) FROM grid", (), 0) == grid_before
    assert store.value(conn, "SELECT COUNT(*) FROM revisions", (), 0) == 1
    assert store.value(conn, "SELECT COUNT(*) FROM revision_impact", (), 0) > 0


# —— 确认修订：独立 REVISED 批次，旧版原样保留 ——

def test_apply_creates_independent_revised_run_and_keeps_base(conn, run_main):
    result = service.apply(conn, run_id=run_main["run_id"], offsets=CORRECTED)
    corrected = result["run"]
    assert corrected["run_kind"] == "REVISED"
    assert corrected["base_run_id"] == run_main["run_id"]
    assert corrected["run_id"] != run_main["run_id"]
    assert corrected["balanced"] is True

    base_after = history.get(conn, run_main["run_id"])
    assert base_after["run_kind"] == "BASE"
    assert base_after["base_run_id"] is None
    # 旧批次计数一个数都没变
    assert base_after["counts"] == run_main["counts"]

    # 修订批次按新桶重算：缺口与覆盖随区间变化
    assert corrected["counts"]["accepted"] == run_main["counts"]["accepted"]
    assert corrected["counts"]["gaps"] != run_main["counts"]["gaps"]
    assert corrected["counts"]["windows"] != run_main["counts"]["windows"]
    assert result["revision"]["status"] == "APPLIED"
    assert result["revision"]["corrected_run_id"] == corrected["run_id"]


def test_apply_recomputes_gaps_fills_and_coverage(conn, run_main):
    """校准规则更新后，缺口、补齐与窗口覆盖全部按新桶重算，不沿用旧网格。"""
    result = service.apply(conn, run_id=run_main["run_id"], offsets=CORRECTED)
    new_counts = result["run"]["counts"]
    # 网格守恒在修订批次上依旧成立
    assert new_counts["expected_buckets"] == (
        new_counts["observed"] + new_counts["filled"] + new_counts["gaps"]
    )
    stored_grid = store.value(
        conn, "SELECT COUNT(*) FROM grid WHERE run_id = ?", (result["run"]["run_id"],), 0
    )
    assert stored_grid == new_counts["grid_points"]
    stored_windows = store.value(
        conn, "SELECT COUNT(*) FROM windows WHERE run_id = ?", (result["run"]["run_id"],), 0
    )
    assert stored_windows == new_counts["windows"]


def test_unaffected_batches_are_never_rewritten(conn, run_main):
    """整批重算只新增修订批次，未受影响的其他批次行号与合计保持不变。"""
    other = batch_service.execute_run(conn, sample="gap_series", offsets="device_offsets")
    service.apply(conn, run_id=run_main["run_id"], offsets=CORRECTED)
    other_after = history.get(conn, other["run_id"])
    assert other_after["counts"] == other["counts"]
    assert other_after["totals"] == other["totals"]
    assert other_after["run_kind"] == "BASE"


# —— 幂等：同一修订重跑不新增版本、不重复计入 ——

def test_revision_rerun_is_idempotent(conn, run_main):
    first = service.apply(conn, run_id=run_main["run_id"], offsets=CORRECTED)
    runs_before = store.value(conn, "SELECT COUNT(*) FROM runs", (), 0)
    revisions_before = store.value(conn, "SELECT COUNT(*) FROM revisions", (), 0)

    second = service.apply(conn, run_id=run_main["run_id"], offsets=CORRECTED)
    assert second["reused"] is True
    assert second["run"]["run_id"] == first["run"]["run_id"]
    assert store.value(conn, "SELECT COUNT(*) FROM runs", (), 0) == runs_before
    assert store.value(conn, "SELECT COUNT(*) FROM revisions", (), 0) == revisions_before
    assert store.value(conn, "SELECT COUNT(*) FROM revision_impact", (), 0) == (
        first["impact"]["moved_samples"] + first["impact"]["affected_windows"]
    )


def test_preview_after_apply_returns_same_revision(conn, run_main):
    applied = service.apply(conn, run_id=run_main["run_id"], offsets=CORRECTED)
    preview = service.preview(conn, run_id=run_main["run_id"], offsets=CORRECTED)
    assert preview["reused"] is True
    assert preview["revision"]["revision_id"] == applied["revision"]["revision_id"]
    assert preview["revision"]["status"] == "APPLIED"


def test_preview_then_apply_promotes_single_revision(conn, run_main):
    """先预览后确认：同一条修订记录从 PREVIEWED 晋升为 APPLIED，不新增第二条。"""
    preview = service.preview(conn, run_id=run_main["run_id"], offsets=CORRECTED)
    rev_id = preview["revision"]["revision_id"]
    applied = service.apply(conn, run_id=run_main["run_id"], offsets=CORRECTED)
    assert applied["revision"]["revision_id"] == rev_id
    assert applied["reused"] is False
    assert service.detail_view(conn, rev_id)["revision"]["status"] == "APPLIED"
    assert store.value(conn, "SELECT COUNT(*) FROM revisions", (), 0) == 1


# —— 失败保旧版 ——

def test_failed_revision_keeps_official_base_and_records_audit(conn, run_main):
    grid_before = store.value(
        conn, "SELECT COUNT(*) FROM grid WHERE run_id = ?", (run_main["run_id"],), 0
    )
    with pytest.raises(InputError) as error:
        service.apply(conn, run_id=run_main["run_id"], offsets=BAD_TABLE)
    assert error.value.code == "OFFSET_TABLE_INVALID"

    # 没有任何 REVISED 批次落库
    revised = store.rows(conn, "SELECT run_id FROM runs WHERE run_kind = 'REVISED'")
    assert revised == []
    # 旧版网格原样保留
    assert store.value(
        conn, "SELECT COUNT(*) FROM grid WHERE run_id = ?", (run_main["run_id"],), 0
    ) == grid_before

    listing = service.list_view(conn, run_main["run_id"])
    assert listing["failed"] == 1
    failed = listing["revisions"][0]
    assert failed["status"] == "FAILED"
    assert failed["error_code"] == "OFFSET_TABLE_INVALID"
    assert failed["corrected_run_id"] is None


def test_repeated_failure_writes_single_audit_row(conn, run_main):
    """同一张坏表反复失败只留一条 FAILED 审计行。"""
    for _ in range(2):
        with pytest.raises(InputError):
            service.preview(conn, run_id=run_main["run_id"], offsets=BAD_TABLE)
    listing = service.list_view(conn, run_main["run_id"])
    assert listing["count"] == 1
    assert listing["revisions"][0]["status"] == "FAILED"
    # 失败之后仍可用好表完成修订
    applied = service.apply(conn, run_id=run_main["run_id"], offsets=CORRECTED)
    assert applied["revision"]["status"] == "APPLIED"


def test_unknown_base_run_is_404(conn):
    with pytest.raises(NotFoundError) as error:
        service.preview(conn, run_id="GL-NOPE", offsets=CORRECTED)
    assert error.value.code == "RUN_NOT_FOUND"


def test_unknown_offset_table_is_input_error(conn, run_main):
    with pytest.raises(InputError) as error:
        service.apply(conn, run_id=run_main["run_id"], offsets="nope")
    assert error.value.code == "OFFSET_TABLE_UNKNOWN"


# —— 复核导出 ——

def test_revision_csv_exports_old_new_side_by_side(conn, run_main):
    view = service.preview(conn, run_id=run_main["run_id"], offsets=CORRECTED)
    windows_csv = csv_out.window_changes_csv(view)
    moved_csv = csv_out.moved_samples_csv(view)
    assert windows_csv.startswith("﻿")
    assert moved_csv.startswith("﻿")

    window_rows = list(csv.DictReader(io.StringIO(windows_csv.lstrip("﻿"))))
    moved_rows = list(csv.DictReader(io.StringIO(moved_csv.lstrip("﻿"))))
    assert len(window_rows) == view["impact"]["affected_windows"]
    assert len(moved_rows) == view["impact"]["moved_samples"]
    row49 = next(item for item in moved_rows if item["source_row"] == "49")
    assert row49["crossed_window"] == "YES"
    assert row49["old_offset"] == "20" and row49["new_offset"] == "95"
    flipped = next(item for item in window_rows if item["window_start"] == "2026-03-01T00:15:00Z")
    assert flipped["old_state"] == "OK" and flipped["new_state"] == "PARTIAL"


def test_revised_run_remains_exportable_through_m7(conn, run_main):
    """更正批次与旧批次都能用 M7 既有导出追回，证据链不断。"""
    from app.modules.m7_export import service as export_service

    result = service.apply(conn, run_id=run_main["run_id"], offsets=CORRECTED)
    corrected_id = result["run"]["run_id"]
    corrected_grid = export_service.grid_csv(conn, corrected_id)
    base_grid = export_service.grid_csv(conn, run_main["run_id"])
    assert len(corrected_grid.splitlines()) != len(base_grid.splitlines())
    assert str(result["run"]["counts"]["grid_points"] + 1) == str(len(corrected_grid.splitlines()))
    # 旧版仍可追回
    assert len(base_grid.splitlines()) == run_main["counts"]["grid_points"] + 1


def test_manifest_carries_revision_lineage(conn, run_main):
    result = service.apply(conn, run_id=run_main["run_id"], offsets=CORRECTED)
    manifest = history.manifest_of(conn, result["run"]["run_id"])
    assert manifest["run_kind"] == "REVISED"
    assert manifest["base_run_id"] == run_main["run_id"]
    base_manifest = history.manifest_of(conn, run_main["run_id"])
    assert base_manifest["run_kind"] == "BASE"
    assert base_manifest["base_run_id"] is None


def test_revision_of_empty_sample_has_zero_impact(conn):
    """无采纳行的空态批次也能修订：影响为零，不报错，照样落独立更正批次。"""
    empty_run = batch_service.execute_run(conn, sample="header_only", offsets="device_offsets")
    preview = service.preview(conn, run_id=empty_run["run_id"], offsets=CORRECTED)
    assert preview["impact"]["moved_samples"] == 0
    assert preview["impact"]["affected_windows"] == 0
    applied = service.apply(conn, run_id=empty_run["run_id"], offsets=CORRECTED)
    assert applied["run"]["balanced"] is True
    assert applied["run"]["run_kind"] == "REVISED"


def test_distinct_bases_get_distinct_revisions(conn, run_main):
    """两个基批各自修订，修订号与更正批次互不串号。"""
    other = batch_service.execute_run(conn, sample="midnight_edge", offsets="device_offsets")
    first = service.apply(conn, run_id=run_main["run_id"], offsets=CORRECTED)
    second = service.apply(conn, run_id=other["run_id"], offsets=CORRECTED)
    assert first["revision"]["revision_id"] != second["revision"]["revision_id"]
    assert first["run"]["run_id"] != second["run"]["run_id"]
    assert second["run"]["base_run_id"] == other["run_id"]
    listing = service.list_view(conn)
    assert listing["applied"] == 2


def test_chain_revision_back_to_original_table_reuses_base_run(conn, run_main):
    """对修订批次再用原表修订：结果与基批同 run_key，直接复用，不造重复批次。"""
    first = service.apply(conn, run_id=run_main["run_id"], offsets=CORRECTED)
    chain = service.apply(
        conn, run_id=first["run"]["run_id"], offsets="device_offsets"
    )
    # 命中既有 BASE 批次，runs 表没有新增行
    assert chain["run"]["run_id"] == run_main["run_id"]
    assert store.value(conn, "SELECT COUNT(*) FROM runs", (), 0) == 2
    # 链式修订自身仍是一条独立审计记录
    assert chain["revision"]["base_run_id"] == first["run"]["run_id"]
    # 重放链式修订幂等
    assert service.apply(
        conn, run_id=first["run"]["run_id"], offsets="device_offsets"
    )["reused"] is True
