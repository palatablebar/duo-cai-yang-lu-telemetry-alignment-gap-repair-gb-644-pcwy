"""M8 时钟校准修订入口：预览影响窗口、确认修订、修订清单与复核导出。"""

from __future__ import annotations

from fastapi import APIRouter, Query
from fastapi.responses import Response
from pydantic import BaseModel, Field

from app import store

from . import csv_out, service

router = APIRouter(tags=["m8 校准修订"])

CSV_MEDIA = "text/csv; charset=utf-8"


class RevisionBody(BaseModel):
    run_id: str = Field(max_length=64)
    offsets: str = Field(max_length=64)


def _download(body: str, filename: str) -> Response:
    return Response(
        content=body.encode("utf-8"),
        media_type=CSV_MEDIA,
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@router.post("/api/revisions/preview")
def preview_revision(body: RevisionBody):
    conn = store.session()
    try:
        return service.preview(conn, run_id=body.run_id, offsets=body.offsets)
    finally:
        conn.close()


@router.post("/api/revisions/apply")
def apply_revision(body: RevisionBody):
    conn = store.session()
    try:
        return service.apply(conn, run_id=body.run_id, offsets=body.offsets)
    finally:
        conn.close()


@router.get("/api/revisions")
def revisions_api(run_id: str | None = Query(default=None, max_length=64)):
    conn = store.session()
    try:
        return service.list_view(conn, run_id)
    finally:
        conn.close()


@router.get("/api/revisions/{revision_id}")
def revision_api(revision_id: str):
    conn = store.session()
    try:
        return service.detail_view(conn, revision_id)
    finally:
        conn.close()


@router.get("/api/revisions/{revision_id}/windows.csv")
def revision_windows_csv(revision_id: str):
    conn = store.session()
    try:
        view = service.detail_view(conn, revision_id)
    finally:
        conn.close()
    return _download(csv_out.window_changes_csv(view), f"revision-windows-{revision_id}.csv")


@router.get("/api/revisions/{revision_id}/moved.csv")
def revision_moved_csv(revision_id: str):
    conn = store.session()
    try:
        view = service.detail_view(conn, revision_id)
    finally:
        conn.close()
    return _download(csv_out.moved_samples_csv(view), f"revision-moved-{revision_id}.csv")
