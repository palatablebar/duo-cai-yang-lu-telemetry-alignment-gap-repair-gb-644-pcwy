"""修订编排：预览影响窗口、确认后落 REVISED 批次，全程保留旧版。

修订键由「基批 run_key + 纠错偏移表 sha256 + 规则版本」确定性派生：
同一次修订无论重跑多少次都命中同一键，既不会新增更正版本，也不会重复计入清单。
"""

from __future__ import annotations

import time

from app import config, errors, manifest, store
from app.modules.m1_intake import service as intake_service
from app.modules.m2_timebase import service as timebase_service
from app.modules.m6_batches import history, service as batch_service

from . import impact as impact_mod

REVISION_KIND = "REVISED"


def revision_key(base_run: dict, new_table_sha: str) -> str:
    material = manifest.canonical_json(
        {
            "base_run_key": base_run["run_key"],
            "new_offset_sha256": new_table_sha,
            "rule_version": config.RULE_VERSION,
            "kind": "calibration-revision",
        }
    )
    return manifest.sha256_bytes(material.encode("utf-8"))


def failure_key(base_run: dict, offsets_key: str) -> str:
    """坏表无法解析、拿不到 sha256 时，用样例 key 派生稳定的失败审计键。"""
    material = manifest.canonical_json(
        {
            "base_run_key": base_run["run_key"],
            "new_offset_key": offsets_key,
            "rule_version": config.RULE_VERSION,
            "kind": "calibration-revision-failed",
        }
    )
    return manifest.sha256_bytes(material.encode("utf-8"))


def revision_id_for(key: str) -> str:
    return "RV-" + key[:10].upper()


def corrected_run_id_for(key: str) -> str:
    return "GL-RV-" + key[:8].upper()


def _load_base(conn, run_id: str) -> dict:
    base = history.get(conn, run_id)
    if base is None:
        raise errors.NotFoundError(
            "批次不存在", code="RUN_NOT_FOUND", detail={"run_id": run_id}
        )
    return base


def _load_corrected_table(offsets_key: str):
    if offsets_key not in config.OFFSET_SAMPLES:
        raise errors.InputError(
            "未知偏移表",
            code="OFFSET_TABLE_UNKNOWN",
            detail={"offsets": offsets_key, "choices": sorted(config.OFFSET_SAMPLES)},
        )
    try:
        return timebase_service.load_offsets(offsets_key)
    except errors.GridlabError:
        raise
    except ValueError as exc:
        raise errors.InputError(
            "纠错偏移表无法解析",
            code="OFFSET_TABLE_INVALID",
            detail={"offsets": offsets_key, "reason": str(exc)},
        ) from exc


def _derive(conn, base_run: dict, new_table) -> tuple[object, object, object]:
    """重放基批采样文件，分别跑旧表与新表两条流水线（旧表结果仅用于对比）。"""
    sample_key = _sample_key_of(base_run)
    loaded = intake_service.load_sample(sample_key)
    old_table = timebase_service.load_offsets(_offset_key_of(base_run))
    old_pipeline = batch_service._build_pipeline(loaded, old_table)
    new_pipeline = batch_service._build_pipeline(loaded, new_table)
    return loaded, old_pipeline, new_pipeline


def _sample_key_of(base_run: dict) -> str:
    for key, (file_name, _label) in config.SAMPLES.items():
        if file_name == base_run["sample_name"]:
            return key
    raise errors.NotFoundError(
        "基批样例已不在自带样例中，无法重算",
        code="SAMPLE_MISSING",
        detail={"sample": base_run["sample_name"]},
    )


def _offset_key_of(base_run: dict) -> str:
    for key, (file_name, _label) in config.OFFSET_SAMPLES.items():
        if file_name == base_run["offset_name"]:
            return key
    raise errors.NotFoundError(
        "基批偏移表已不在自带样例中，无法重算",
        code="OFFSET_TABLE_MISSING",
        detail={"offsets": base_run["offset_name"]},
    )


def _revision_payload(
    *, base_run, rev_id, rev_key, new_table, status, impact=None
) -> dict:
    return {
        "revision_id": rev_id,
        "base_run_id": base_run["run_id"],
        "corrected_run_id": None,
        "revision_key": rev_key,
        "status": status,
        "created_at": history.now_text(),
        "old_offset_name": base_run["offset_name"],
        "new_offset_name": new_table.file_name,
        "old_offset_sha": _sha_of_named_offset(base_run["offset_name"]),
        "new_offset_sha": new_table.sha256,
        "affected_windows": 0 if impact is None else impact.affected_windows,
        "moved_samples": 0 if impact is None else impact.moved_samples,
        "summary": {} if impact is None else impact.summary(),
        "error_code": None,
        "error_message": None,
    }


def preview(conn, *, run_id: str, offsets: str) -> dict:
    """预览修订影响：不落任何批次数据；失败只登记 FAILED 审计行。"""
    base_run = _load_base(conn, run_id)
    fail_id = revision_id_for(failure_key(base_run, offsets))
    try:
        new_table = _load_corrected_table(offsets)
        rev_key = revision_key(base_run, new_table.sha256)
        rev_id = revision_id_for(rev_key)

        existing = history.find_revision_by_key(conn, rev_key)
        if existing is not None:
            return {
                "reused": True,
                "revision": existing,
                "impact": existing["summary"],
                "window_changes": _stored_window_changes(conn, existing),
                "moved": _stored_moved(conn, existing),
            }

        loaded, old_pipeline, new_pipeline = _derive(conn, base_run, new_table)
        result = impact_mod.compare_pipelines(old_pipeline, new_pipeline)

        record = _revision_payload(
            base_run=base_run,
            rev_id=rev_id,
            rev_key=rev_key,
            new_table=new_table,
            status="PREVIEWED",
            impact=result,
        )
        with store.transaction(conn):
            history.save_revision(conn, record)
            _save_impact_detail(conn, rev_id, result)
        saved = history.get_revision(conn, rev_id)
        return {
            "reused": False,
            "revision": saved,
            "impact": result.summary(),
            "window_changes": [item.as_dict() for item in result.window_changes],
            "moved": [item.__dict__ for item in result.moved],
        }
    except errors.GridlabError as exc:
        _record_failure(conn, base_run, fail_id, failure_key(base_run, offsets), offsets, exc.code, str(exc))
        raise


def apply(conn, *, run_id: str, offsets: str) -> dict:
    """确认修订：落独立 REVISED 批次；同一修订重跑只复用，不新增版本。"""
    started_at = time.monotonic()
    base_run = _load_base(conn, run_id)
    fail_id = revision_id_for(failure_key(base_run, offsets))
    try:
        new_table = _load_corrected_table(offsets)
    except errors.GridlabError as exc:
        _record_failure(conn, base_run, fail_id, failure_key(base_run, offsets), offsets, exc.code, str(exc))
        raise
    rev_key = revision_key(base_run, new_table.sha256)
    rev_id = revision_id_for(rev_key)

    existing = history.find_revision_by_key(conn, rev_key)
    already_applied = existing is not None and existing["status"] == "APPLIED"
    if already_applied:
        corrected = history.get(conn, existing["corrected_run_id"])
        return {
            "reused": True,
            "revision": existing,
            "run": batch_service._result(
                conn, corrected, reused=True, lines=batch_service._lines_of(conn, corrected["run_id"])
            ),
            "impact": existing["summary"],
            "window_changes": _stored_window_changes(conn, existing),
            "moved": _stored_moved(conn, existing),
        }

    try:
        loaded, old_pipeline, new_pipeline = _derive(conn, base_run, new_table)
        result = impact_mod.compare_pipelines(old_pipeline, new_pipeline)
    except errors.GridlabError as exc:
        _record_failure(conn, base_run, fail_id, failure_key(base_run, offsets), offsets, exc.code, str(exc))
        raise

    corrected_key = _corrected_run_key(base_run, new_table, loaded)
    # 同输入同参数的批次全局只有一个 run_key：链式修订回到旧表时直接复用既有批次，
    # 不强行插第二份，也不与 runs.run_key 唯一约束冲突。
    corrected_existing = history.find_by_key(conn, corrected_key)
    corrected_id = (
        corrected_existing["run_id"] if corrected_existing else corrected_run_id_for(rev_key)
    )

    # 与批次落库共用同一个事务：任何一步失败整体回滚，绝不留半份更正结果。
    with store.transaction(conn):
        if corrected_existing is None:
            params = batch_service._params()
            _persist_revised_run(
                conn,
                run_id=corrected_id,
                loaded=loaded,
                key=corrected_key,
                params=params,
                pipeline=new_pipeline,
                base_run_id=base_run["run_id"],
                started_at=started_at,
            )
        if existing is None:
            history.save_revision(
                conn,
                _revision_payload(
                    base_run=base_run,
                    rev_id=rev_id,
                    rev_key=rev_key,
                    new_table=new_table,
                    status="APPLIED",
                    impact=result,
                )
                | {"corrected_run_id": corrected_id, "applied_at": history.now_text()},
            )
            _save_impact_detail(conn, rev_id, result)
        else:
            history.mark_revision_applied(conn, rev_id, corrected_id, result.summary())
            _replace_impact_detail(conn, rev_id, result)

    revision = history.get_revision(conn, rev_id)
    corrected = history.get(conn, corrected_id)
    return {
        "reused": False,
        "revision": revision,
        "run": batch_service._result(
            conn, corrected, reused=False, lines=batch_service._lines_of(conn, corrected_id)
        ),
        "impact": result.summary(),
        "window_changes": [item.as_dict() for item in result.window_changes],
        "moved": [item.__dict__ for item in result.moved],
    }


def _corrected_run_key(base_run: dict, new_table, loaded) -> str:
    inputs = [
        {"name": loaded.file_name, "sha256": loaded.sha256, "bytes": loaded.size},
        {"name": new_table.file_name, "sha256": new_table.sha256, "bytes": new_table.size},
    ]
    return manifest.run_key(inputs, config.RULE_VERSION, batch_service._params())


def _persist_revised_run(conn, **kwargs) -> dict:
    """在调用方已开启的事务里写 REVISED 批次，不再另开事务。"""
    return batch_service._write_pipeline(conn, run_kind=REVISION_KIND, **kwargs)


def _record_failure(conn, base_run, rev_id, rev_key, offsets_key, code, message) -> None:
    """修订失败：只留 FAILED 审计行，不写任何批次/网格数据，旧版保持正式。

    同一基批同一坏表反复失败只留一条审计行（revision_key 唯一约束兜底）。
    """
    if history.get_revision(conn, rev_id) is not None:
        return
    file_name = config.OFFSET_SAMPLES.get(offsets_key, (offsets_key, ""))[0]
    payload = {
        "revision_id": rev_id,
        "base_run_id": base_run["run_id"],
        "corrected_run_id": None,
        "revision_key": rev_key,
        "status": "FAILED",
        "created_at": history.now_text(),
        "old_offset_name": base_run["offset_name"],
        "new_offset_name": file_name,
        "old_offset_sha": _sha_of_named_offset(base_run["offset_name"]),
        "new_offset_sha": "",
        "affected_windows": 0,
        "moved_samples": 0,
        "summary": {},
        "error_code": code,
        "error_message": message,
    }
    with store.transaction(conn):
        history.save_revision(conn, payload)


def _sha_of_named_offset(file_name: str) -> str:
    for key, (candidate, _label) in config.OFFSET_SAMPLES.items():
        if candidate == file_name:
            return timebase_service.load_offsets(key).sha256
    return ""


def _save_impact_detail(conn, revision_id: str, result) -> None:
    for item in result.moved:
        conn.execute(
            "INSERT INTO revision_impact(revision_id, kind, sensor_id, source_row,"
            " old_bucket, new_bucket, old_window, new_window, crossed,"
            " old_offset, new_offset) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (
                revision_id,
                "MOVED",
                item.sensor_id,
                item.source_row,
                item.old_bucket,
                item.new_bucket,
                item.old_window,
                item.new_window,
                1 if item.crossed_window else 0,
                item.old_offset,
                item.new_offset,
            ),
        )
    for item in result.window_changes:
        conn.execute(
            "INSERT INTO revision_impact(revision_id, kind, sensor_id, window_start,"
            " old_observed, new_observed, old_expected, new_expected,"
            " old_coverage, new_coverage, old_state, new_state, crossed)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                revision_id,
                "WINDOW",
                item.sensor_id,
                item.window_start,
                item.old_observed,
                item.new_observed,
                item.old_expected,
                item.new_expected,
                item.old_coverage,
                item.new_coverage,
                item.old_state,
                item.new_state,
                1 if item.old_state != item.new_state else 0,
            ),
        )


def _replace_impact_detail(conn, revision_id: str, result) -> None:
    conn.execute("DELETE FROM revision_impact WHERE revision_id = ?", (revision_id,))
    _save_impact_detail(conn, revision_id, result)


def _stored_window_changes(conn, revision: dict) -> list[dict]:
    rows = store.rows(
        conn,
        "SELECT * FROM revision_impact WHERE revision_id = ? AND kind = 'WINDOW'"
        " ORDER BY sensor_id, window_start",
        (revision["revision_id"],),
    )
    return [
        {
            "sensor_id": row["sensor_id"],
            "window_start": row["window_start"],
            "old_observed": row["old_observed"],
            "new_observed": row["new_observed"],
            "old_expected": row["old_expected"],
            "new_expected": row["new_expected"],
            "old_coverage": row["old_coverage"],
            "new_coverage": row["new_coverage"],
            "old_state": row["old_state"],
            "new_state": row["new_state"],
            "state_changed": bool(row["crossed"]),
        }
        for row in rows
    ]


def _stored_moved(conn, revision: dict) -> list[dict]:
    rows = store.rows(
        conn,
        "SELECT * FROM revision_impact WHERE revision_id = ? AND kind = 'MOVED'"
        " ORDER BY sensor_id, source_row",
        (revision["revision_id"],),
    )
    return [
        {
            "source_row": row["source_row"],
            "sensor_id": row["sensor_id"],
            "old_bucket": row["old_bucket"],
            "new_bucket": row["new_bucket"],
            "old_window": row["old_window"],
            "new_window": row["new_window"],
            "crossed_window": bool(row["crossed"]),
            "old_offset": row["old_offset"],
            "new_offset": row["new_offset"],
        }
        for row in rows
    ]


def list_view(conn, base_run_id: str | None = None) -> dict:
    revisions = history.list_revisions(conn, base_run_id)
    return {
        "revisions": revisions,
        "count": len(revisions),
        "applied": sum(1 for item in revisions if item["status"] == "APPLIED"),
        "failed": sum(1 for item in revisions if item["status"] == "FAILED"),
    }


def detail_view(conn, revision_id: str) -> dict:
    revision = history.get_revision(conn, revision_id)
    if revision is None:
        raise errors.NotFoundError(
            "修订不存在", code="REVISION_NOT_FOUND", detail={"revision_id": revision_id}
        )
    return {
        "revision": revision,
        "impact": revision["summary"],
        "window_changes": _stored_window_changes(conn, revision),
        "moved": _stored_moved(conn, revision),
    }
