from pathlib import Path
from urllib.parse import urlencode
import shutil
import re
import logging

from fastapi import Depends, FastAPI, File, Form, Request, UploadFile
from fastapi.responses import JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from app.config import parse_figure_list, parse_figure_options, settings
from app.database import Base, engine, get_session
from app.models import Album, Series
from app.services.my_tonies import MyToniesClient
from app.services.scanner import process_inbox, sync_library

app = FastAPI(title=settings.app_name)
templates = Jinja2Templates(directory="app/templates")
app.mount("/static", StaticFiles(directory="app/static"), name="static")
app.mount("/media", StaticFiles(directory=str(settings.data_root)), name="media")
logger = logging.getLogger(__name__)


@app.on_event("startup")
def startup() -> None:
    settings.data_root.mkdir(parents=True, exist_ok=True)
    settings.db_path.parent.mkdir(parents=True, exist_ok=True)
    Base.metadata.create_all(bind=engine)
    with next(get_session()) as session:
        try:
            process_inbox(session)
            sync_library(session)
        except Exception:
            session.rollback()
            logger.exception("Startup sync failed; application continues running.")


@app.get("/")
async def index(request: Request, db: Session = Depends(get_session)):
    context = await _build_index_context(request, db)
    return templates.TemplateResponse("index.html", context)


@app.get("/upload")
async def upload_page(request: Request):
    query = urlencode({"message": "Upload page moved to /manage.", "message_type": "info"})
    return RedirectResponse(url=f"/manage?{query}", status_code=307)


@app.get("/manage")
async def manage_page(request: Request, db: Session = Depends(get_session)):
    message = request.query_params.get("message")
    message_type = request.query_params.get("message_type", "info")
    albums = (
        db.execute(
            select(Album)
            .options(selectinload(Album.series))
            .order_by(Album.series_id.asc(), Album.name.asc())
        )
        .scalars()
        .all()
    )
    grouped_albums = _group_albums_by_series(albums)
    return templates.TemplateResponse(
        "manage.html",
        {
            "request": request,
            "message": message,
            "message_type": message_type,
            "albums": albums,
            "grouped_albums": grouped_albums,
        },
    )


async def _build_index_context(request: Request, db: Session, message: str | None = None, message_type: str = "info"):
    albums = (
        db.execute(
            select(Album)
            .options(selectinload(Album.series))
            .order_by(Album.series_id.asc(), Album.name.asc())
        )
        .scalars()
        .all()
    )
    grouped_albums = _group_albums_by_series(albums)

    query_message = request.query_params.get("message")
    query_message_type = request.query_params.get("message_type", "info")
    if message is None:
        message = query_message
        message_type = query_message_type

    selected_figure_id = request.query_params.get("figure_id") or settings.default_figure_id
    figure_options = parse_figure_options(settings.figure_options)
    figure_api_error = False

    client = MyToniesClient()
    try:
        fetched_figures = await client.list_figures()
        if fetched_figures:
            figure_options = fetched_figures
    except Exception:
        figure_api_error = True

    # Apply whitelist / blacklist (mutually exclusive; whitelist takes priority)
    whitelist = parse_figure_list(settings.figure_whitelist)
    blacklist = parse_figure_list(settings.figure_blacklist)
    if whitelist:
        figure_options = [o for o in figure_options if o["id"] in whitelist]
    elif blacklist:
        figure_options = [o for o in figure_options if o["id"] not in blacklist]

    if selected_figure_id and all(option["id"] != selected_figure_id for option in figure_options):
        figure_options.insert(0, {"id": selected_figure_id, "name": selected_figure_id})

    return {
        "request": request,
        "albums": albums,
        "grouped_albums": grouped_albums,
        "default_figure_id": settings.default_figure_id,
        "selected_figure_id": selected_figure_id,
        "figure_options": figure_options,
        "figure_api_error": figure_api_error,
        "message": message,
        "message_type": message_type,
    }


@app.post("/scan")
def scan(db: Session = Depends(get_session)):
    inbox_result = process_inbox(db)
    sync_result = sync_library(db)
    message = (
        "Inbox processed: "
        f"staged {inbox_result.get('staged', 0)}, "
        f"added {inbox_result.get('added', 0)}, "
        f"duplicates {inbox_result.get('duplicates', 0)}, "
        f"rejected {inbox_result.get('rejected', 0)}, "
        f"library synced {sync_result.get('synced', 0)}, "
        f"purged albums {sync_result.get('purged_albums', 0)}, "
        f"purged series {sync_result.get('purged_series', 0)}."
    )
    query = urlencode({"message": message, "message_type": "success"})
    return RedirectResponse(url=f"/?{query}", status_code=303)


@app.post("/upload")
def upload_files(
    files: list[UploadFile] = File(default=[]),
    db: Session = Depends(get_session),
):
    query = urlencode({"message": "Upload endpoint moved to /manage.", "message_type": "info"})
    return RedirectResponse(url=f"/manage?{query}", status_code=307)


@app.post("/manage")
def manage_upload_files(
    files: list[UploadFile] = File(default=[]),
    db: Session = Depends(get_session),
):
    inbox_root = settings.data_root / "inbox"
    inbox_root.mkdir(parents=True, exist_ok=True)

    saved_count = 0
    for uploaded_file in files:
        if not uploaded_file.filename:
            continue

        original_name = Path(uploaded_file.filename).name
        if not original_name:
            continue

        destination = inbox_root / original_name
        stem = destination.stem
        suffix = destination.suffix
        counter = 1
        while destination.exists():
            destination = inbox_root / f"{stem}-{counter}{suffix}"
            counter += 1

        with destination.open("wb") as output:
            shutil.copyfileobj(uploaded_file.file, output)
        saved_count += 1

    if saved_count == 0:
        query = urlencode({"message": "No files uploaded.", "message_type": "error"})
        return RedirectResponse(url=f"/manage?{query}", status_code=303)

    inbox_result = process_inbox(db)
    sync_result = sync_library(db)
    message = (
        f"Uploaded {saved_count} file(s). "
        "Inbox processed: "
        f"staged {inbox_result.get('staged', 0)}, "
        f"added {inbox_result.get('added', 0)}, "
        f"duplicates {inbox_result.get('duplicates', 0)}, "
        f"rejected {inbox_result.get('rejected', 0)}, "
        f"library synced {sync_result.get('synced', 0)}."
    )
    query = urlencode({"message": message, "message_type": "success"})
    return RedirectResponse(url=f"/manage?{query}", status_code=303)


@app.post("/albums/{album_id}/delete")
def delete_album(album_id: int, db: Session = Depends(get_session)):
    album = db.get(Album, album_id)
    if album is None:
        query = urlencode({"message": "Album not found.", "message_type": "error"})
        return RedirectResponse(url=f"/manage?{query}", status_code=303)

    deleted, error = _delete_album_files(album)
    if not deleted:
        query = urlencode({"message": error or "Delete failed.", "message_type": "error"})
        return RedirectResponse(url=f"/manage?{query}", status_code=303)

    sync_result = sync_library(db)
    message = (
        f"Deleted album '{album.name}'. "
        f"Library synced {sync_result.get('synced', 0)}, "
        f"purged albums {sync_result.get('purged_albums', 0)}, "
        f"purged series {sync_result.get('purged_series', 0)}."
    )
    query = urlencode({"message": message, "message_type": "success"})
    return RedirectResponse(url=f"/manage?{query}", status_code=303)


@app.post("/albums/delete")
def bulk_delete_albums(
    selected_album_ids: list[int] = Form(default=[]),
    db: Session = Depends(get_session),
):
    if not selected_album_ids:
        query = urlencode({"message": "No albums selected.", "message_type": "error"})
        return RedirectResponse(url=f"/manage?{query}", status_code=303)

    albums = (
        db.execute(select(Album).where(Album.id.in_(selected_album_ids)))
        .scalars()
        .all()
    )
    albums_by_id = {album.id: album for album in albums}

    deleted_count = 0
    missing_count = 0
    failed_count = 0

    for album_id in selected_album_ids:
        album = albums_by_id.get(album_id)
        if album is None:
            missing_count += 1
            continue

        deleted, _ = _delete_album_files(album)
        if deleted:
            deleted_count += 1
        else:
            failed_count += 1

    sync_result = sync_library(db)
    message = (
        f"Bulk delete: removed {deleted_count}, missing {missing_count}, failed {failed_count}. "
        f"Library synced {sync_result.get('synced', 0)}, "
        f"purged albums {sync_result.get('purged_albums', 0)}, "
        f"purged series {sync_result.get('purged_series', 0)}."
    )
    message_type = "success" if failed_count == 0 else "error"
    query = urlencode({"message": message, "message_type": message_type})
    return RedirectResponse(url=f"/manage?{query}", status_code=303)


@app.post("/albums/group")
def group_albums_to_series(
    selected_album_ids: list[int] = Form(default=[]),
    series_name: str = Form(default=""),
    db: Session = Depends(get_session),
):
    result = _group_albums_to_series_internal(db, selected_album_ids, series_name)
    query = urlencode({"message": result["message"], "message_type": result["message_type"]})
    return RedirectResponse(url=f"/manage?{query}", status_code=303)


@app.post("/api/albums/group")
def group_albums_to_series_api(
    selected_album_ids: list[int] = Form(default=[]),
    series_name: str = Form(default=""),
    db: Session = Depends(get_session),
):
    result = _group_albums_to_series_internal(db, selected_album_ids, series_name)
    status_code = 200 if result["message_type"] == "success" else 400
    return JSONResponse(result, status_code=status_code)


@app.post("/series/rename")
def rename_series(
    old_series_slug: str = Form(default=""),
    new_series_name: str = Form(default=""),
    db: Session = Depends(get_session),
):
    result = _rename_series_internal(db, old_series_slug, new_series_name)
    query = urlencode({"message": result["message"], "message_type": result["message_type"]})
    return RedirectResponse(url=f"/manage?{query}", status_code=303)


@app.post("/api/series/rename")
def rename_series_api(
    old_series_slug: str = Form(default=""),
    new_series_name: str = Form(default=""),
    db: Session = Depends(get_session),
):
    result = _rename_series_internal(db, old_series_slug, new_series_name)
    status_code = 200 if result["message_type"] == "success" else 400
    return JSONResponse(result, status_code=status_code)


@app.post("/upload-to-tonie")
async def upload_to_tonie(
    request: Request,
    selected_album_ids: list[int] = Form(default=[]),
    figure_id: str = Form(default=""),
    db: Session = Depends(get_session),
):
    if not selected_album_ids:
        return await _render_with_message(request, db, "Select at least one story before uploading.", "error")

    albums = (
        db.execute(
            select(Album)
            .where(Album.id.in_(selected_album_ids))
            .options(selectinload(Album.series), selectinload(Album.tracks))
        )
        .scalars()
        .all()
        if selected_album_ids
        else []
    )

    total_duration = sum(album.duration_seconds for album in albums)
    if total_duration > 5400:
        return await _render_with_message(request, db, "Selected albums exceed 90 minutes.", "error")

    resolved_figure_id = figure_id or settings.default_figure_id
    if not resolved_figure_id:
        return await _render_with_message(request, db, "Figure ID is required.", "error")

    file_paths: list[Path] = []
    for album in albums:
        file_paths.extend(Path(track.path) for track in sorted(album.tracks, key=lambda item: item.track_no))

    client = MyToniesClient()
    try:
        await client.upload_album_files(resolved_figure_id, file_paths)
    except Exception as exc:
        return await _render_with_message(request, db, f"Upload failed: {exc}", "error")

    return await _render_with_message(
        request,
        db,
        f"Uploaded {len(file_paths)} files to figure {resolved_figure_id}.",
        "success",
    )


async def _render_with_message(request: Request, db: Session, message: str, message_type: str):
    context = await _build_index_context(request, db, message=message, message_type=message_type)
    return templates.TemplateResponse("index.html", context)


def _delete_album_files(album: Album) -> tuple[bool, str | None]:
    album_path = Path(album.path)
    library_root = (settings.data_root / "library").resolve()
    processed_marker = settings.data_root / "processed" / album.series.name / f"{album.name}.processed"

    if album_path.exists():
        try:
            resolved_album_path = album_path.resolve()
            if library_root in resolved_album_path.parents:
                shutil.rmtree(resolved_album_path)
            else:
                return False, "Refused to delete album outside library root."
        except OSError as exc:
            return False, f"Delete failed: {exc}"

    if processed_marker.exists():
        try:
            processed_marker.unlink()
        except OSError:
            pass

    poster_path = Path(album.poster_path) if album.poster_path else None
    posters_root = (settings.data_root / "posters").resolve()
    if poster_path and poster_path.exists():
        try:
            resolved_poster_path = poster_path.resolve()
            if posters_root in resolved_poster_path.parents:
                resolved_poster_path.unlink()
        except OSError:
            pass

    return True, None


def _series_sort_key(series_name: str) -> tuple[int, int | str, str]:
    match = re.match(r"^(\d+)", series_name.strip())
    if match:
        return (0, int(match.group(1)), series_name.casefold())
    return (1, series_name.casefold(), series_name.casefold())


def _group_albums_by_series(albums: list[Album]) -> list[dict]:
    grouped: dict[str, list[Album]] = {}
    series_slugs_by_name: dict[str, str] = {}
    for album in albums:
        series_name = album.series.name if album.series else "Ungrouped"
        grouped.setdefault(series_name, []).append(album)
        if album.series and album.series.slug:
            series_slugs_by_name[series_name] = album.series.slug

    grouped_items: list[dict] = []
    for series_name in sorted(grouped.keys(), key=_series_sort_key):
        series_albums = sorted(grouped[series_name], key=lambda item: item.name.casefold())
        grouped_items.append(
            {
                "series_name": series_name,
                "series_slug": series_slugs_by_name.get(series_name, _slugify(series_name)),
                "albums": series_albums,
            }
        )
    return grouped_items


def _slugify(name: str) -> str:
    return re.sub(r"-+", "-", "".join(char.lower() if char.isalnum() else "-" for char in name)).strip("-")


def _normalize_slug(value: str) -> str:
    return re.sub(r"-+", "-", "".join(char.lower() if char.isalnum() else "-" for char in value)).strip("-")


def _group_albums_to_series_internal(db: Session, selected_album_ids: list[int], series_name: str) -> dict:
    normalized_series_name = " ".join(series_name.strip().split())
    if not selected_album_ids:
        return {
            "message": "No albums selected.",
            "message_type": "error",
            "moved_count": 0,
            "skipped_count": 0,
            "failed_count": 0,
            "target_series_name": normalized_series_name,
        }

    if not normalized_series_name:
        return {
            "message": "Series name is required.",
            "message_type": "error",
            "moved_count": 0,
            "skipped_count": 0,
            "failed_count": 0,
            "target_series_name": normalized_series_name,
        }

    series_slug = _slugify(normalized_series_name)
    existing_series = db.scalar(select(Series).where(Series.slug == series_slug))

    target_series_name = existing_series.name if existing_series else normalized_series_name
    library_root = settings.data_root / "library"
    if not existing_series and library_root.exists():
        for candidate in library_root.iterdir():
            if candidate.is_dir() and _slugify(candidate.name) == series_slug:
                target_series_name = candidate.name
                break

    albums = (
        db.execute(
            select(Album)
            .where(Album.id.in_(selected_album_ids))
            .options(selectinload(Album.series))
        )
        .scalars()
        .all()
    )
    albums_by_id = {album.id: album for album in albums}

    moved_count = 0
    skipped_count = 0
    failed_count = 0
    target_series_dir = library_root / target_series_name
    target_series_dir.mkdir(parents=True, exist_ok=True)

    for album_id in selected_album_ids:
        album = albums_by_id.get(album_id)
        if album is None:
            skipped_count += 1
            continue

        source_dir = Path(album.path)
        if not source_dir.exists() or not source_dir.is_dir():
            skipped_count += 1
            continue

        current_series_name = album.series.name if album.series else ""
        if _slugify(current_series_name) == series_slug:
            skipped_count += 1
            continue

        destination_dir = target_series_dir / source_dir.name
        if destination_dir.exists():
            failed_count += 1
            continue

        try:
            shutil.move(str(source_dir), str(destination_dir))
            old_marker = settings.data_root / "processed" / current_series_name / f"{album.name}.processed"
            new_marker_dir = settings.data_root / "processed" / target_series_name
            new_marker = new_marker_dir / f"{album.name}.processed"
            if old_marker.exists():
                new_marker_dir.mkdir(parents=True, exist_ok=True)
                shutil.move(str(old_marker), str(new_marker))
            moved_count += 1
        except Exception:
            failed_count += 1

    sync_result = sync_library(db)
    message = (
        f"Series grouping: moved {moved_count}, skipped {skipped_count}, failed {failed_count}. "
        f"Library synced {sync_result.get('synced', 0)}, "
        f"purged albums {sync_result.get('purged_albums', 0)}, "
        f"purged series {sync_result.get('purged_series', 0)}."
    )
    message_type = "success" if failed_count == 0 else "error"
    return {
        "message": message,
        "message_type": message_type,
        "moved_count": moved_count,
        "skipped_count": skipped_count,
        "failed_count": failed_count,
        "target_series_name": target_series_name,
    }


def _rename_series_internal(db: Session, old_series_slug: str, new_series_name: str) -> dict:
    normalized_new_name = " ".join(new_series_name.strip().split())
    if not old_series_slug:
        return {"message": "Series identifier is required.", "message_type": "error"}

    if not normalized_new_name:
        return {"message": "New series name is required.", "message_type": "error"}

    series = db.scalar(select(Series).where(Series.slug == old_series_slug))
    if series is None:
        normalized_old_slug = _normalize_slug(old_series_slug)
        all_series = db.scalars(select(Series)).all()
        series = next(
            (
                candidate
                for candidate in all_series
                if _normalize_slug(candidate.slug) == normalized_old_slug
                or _normalize_slug(candidate.name) == normalized_old_slug
            ),
            None,
        )

    if series is None:
        return {"message": "Series not found.", "message_type": "error"}

    new_series_slug = _slugify(normalized_new_name)
    if not new_series_slug:
        return {"message": "Invalid series name.", "message_type": "error"}

    conflicting_series = db.scalar(select(Series).where(Series.slug == new_series_slug))
    if conflicting_series is not None and conflicting_series.id != series.id:
        has_albums = db.scalar(select(Album.id).where(Album.series_id == conflicting_series.id).limit(1)) is not None
        if has_albums:
            return {"message": "Target series already exists.", "message_type": "error"}
        db.delete(conflicting_series)
        db.flush()

    old_series_name = series.name
    library_root = settings.data_root / "library"
    processed_root = settings.data_root / "processed"
    old_library_dir = library_root / old_series_name
    new_library_dir = library_root / normalized_new_name
    old_processed_dir = processed_root / old_series_name
    new_processed_dir = processed_root / normalized_new_name

    try:
        if old_series_name != normalized_new_name:
            if old_library_dir.exists():
                if new_library_dir.exists() and old_library_dir.resolve() != new_library_dir.resolve():
                    return {"message": "Cannot rename: target library folder already exists.", "message_type": "error"}
                shutil.move(str(old_library_dir), str(new_library_dir))

            if old_processed_dir.exists():
                if new_processed_dir.exists() and old_processed_dir.resolve() != new_processed_dir.resolve():
                    return {"message": "Cannot rename: target processed folder already exists.", "message_type": "error"}
                shutil.move(str(old_processed_dir), str(new_processed_dir))
    except Exception as exc:
        db.rollback()
        return {"message": f"Series rename failed: {exc}", "message_type": "error"}

    sync_result = sync_library(db)
    message = (
        f"Renamed series '{old_series_name}' to '{normalized_new_name}'. "
        f"Library synced {sync_result.get('synced', 0)}, "
        f"purged albums {sync_result.get('purged_albums', 0)}, "
        f"purged series {sync_result.get('purged_series', 0)}."
    )
    return {
        "message": message,
        "message_type": "success",
        "old_series_name": old_series_name,
        "new_series_name": normalized_new_name,
        "new_series_slug": new_series_slug,
    }
