"""M8 HTTP 契约：预览/确认/清单/详情/CSV 下载与错误码。"""

from __future__ import annotations

import csv
import io


def test_revision_full_flow_over_http(api):
    """值班员视角：预览 → 确认 → 下载复核 CSV，旧批次与更正批次都在清单里。"""
    base = api.post(
        "/api/samples/import", json={"sample": "telemetry_raw", "offsets": "device_offsets"}
    ).json()

    preview = api.post(
        "/api/revisions/preview",
        json={"run_id": base["run_id"], "offsets": "device_offsets_corrected"},
    )
    assert preview.status_code == 200
    payload = preview.json()
    assert payload["revision"]["status"] == "PREVIEWED"
    assert payload["impact"]["affected_windows"] >= 1
    revision_id = payload["revision"]["revision_id"]

    applied = api.post(
        "/api/revisions/apply",
        json={"run_id": base["run_id"], "offsets": "device_offsets_corrected"},
    )
    assert applied.status_code == 200
    body = applied.json()
    assert body["run"]["run_kind"] == "REVISED"
    assert body["run"]["base_run_id"] == base["run_id"]
    assert body["run"]["balanced"] is True

    # 幂等：再确认一次不新增
    again = api.post(
        "/api/revisions/apply",
        json={"run_id": base["run_id"], "offsets": "device_offsets_corrected"},
    )
    assert again.json()["reused"] is True
    assert again.json()["run"]["run_id"] == body["run"]["run_id"]

    listing = api.get("/api/revisions").json()
    assert listing["applied"] == 1
    assert any(item["revision_id"] == revision_id for item in listing["revisions"])

    detail = api.get(f"/api/revisions/{revision_id}")
    assert detail.status_code == 200
    assert len(detail.json()["window_changes"]) == payload["impact"]["affected_windows"]

    windows_csv = api.get(f"/api/revisions/{revision_id}/windows.csv")
    assert windows_csv.status_code == 200
    assert windows_csv.headers["content-type"].startswith("text/csv")
    rows = list(csv.DictReader(io.StringIO(windows_csv.content.decode("utf-8-sig"))))
    assert len(rows) == payload["impact"]["affected_windows"]

    moved_csv = api.get(f"/api/revisions/{revision_id}/moved.csv")
    assert moved_csv.status_code == 200
    moved_rows = list(csv.DictReader(io.StringIO(moved_csv.content.decode("utf-8-sig"))))
    assert len(moved_rows) == payload["impact"]["moved_samples"]

    # 批次清单同时包含原始与修订两个版本
    batches = api.get("/api/batches").json()
    kinds = {item["run_id"]: item["run_kind"] for item in batches["runs"]}
    assert kinds[base["run_id"]] == "BASE"
    assert kinds[body["run"]["run_id"]] == "REVISED"


def test_revision_failure_returns_400_and_keeps_base(api):
    base = api.post(
        "/api/samples/import", json={"sample": "telemetry_raw", "offsets": "device_offsets"}
    ).json()
    response = api.post(
        "/api/revisions/apply",
        json={"run_id": base["run_id"], "offsets": "device_offsets_bad"},
    )
    assert response.status_code == 400
    assert response.json()["error"] == "OFFSET_TABLE_INVALID"

    revisions = api.get(f"/api/revisions?run_id={base['run_id']}").json()
    assert revisions["failed"] == 1
    assert revisions["revisions"][0]["status"] == "FAILED"
    # 仍然只有原始批次
    assert api.get("/api/batches").json()["count"] == 1


def test_revision_unknown_run_returns_404(api):
    response = api.post(
        "/api/revisions/preview",
        json={"run_id": "GL-NOPE", "offsets": "device_offsets_corrected"},
    )
    assert response.status_code == 404
    assert response.json()["error"] == "RUN_NOT_FOUND"


def test_revision_detail_unknown_returns_404(api):
    assert api.get("/api/revisions/RV-NOPE").status_code == 404


def test_batches_page_shows_revision_workstation(api):
    api.post(
        "/api/samples/import", json={"sample": "telemetry_raw", "offsets": "device_offsets"}
    )
    page = api.get("/batches")
    assert page.status_code == 200
    assert "时钟校准修订" in page.text
    assert "revision.js" in page.text
    assert "设备偏移表·纠错版" in page.text
