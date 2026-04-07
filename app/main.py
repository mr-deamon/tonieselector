from pathlib import Path
from urllib.parse import urlencode
import shutil

from fastapi import Depends, FastAPI, File, Form, Request, UploadFile
from fastapi.responses import RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from app.config import parse_figure_list, parse_figure_options, settings
from app.database import Base, engine, get_session
from app.models import Album
from app.services.my_tonies import MyToniesClient
from app.services.scanner import process_inbox, sync_library

app = FastAPI(title=settings.app_name)
templates = Jinja2Templates(directory="app/templates")
app.mount("/static", StaticFiles(directory="app/static"), name="static")
app.mount("/media", StaticFiles(directory=str(settings.data_root)), name="media")


@app.on_event("startup")
def startup() -> None:
    settings.data_root.mkdir(parents=True, exist_ok=True)
    settings.db_path.parent.mkdir(parents=True, exist_ok=True)
    Base.metadata.create_all(bind=engine)
    with next(get_session()) as session:
        process_inbox(session)
        sync_library(session)


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
        db.execute(select(Album).order_by(Album.name.asc()))
        .scalars()
        .all()
    )
    return templates.TemplateResponse(
        "manage.html",
        {
            "request": request,
            "message": message,
            "message_type": message_type,
            "albums": albums,
        },
    )


async def _build_index_context(request: Request, db: Session, message: str | None = None, message_type: str = "info"):
    albums = (
        db.execute(select(Album).options(selectinload(Album.series)).order_by(Album.name.asc()))
        .scalars()
        .all()
    )
    albums = sorted(
        albums,
        key=lambda album: (
            (album.series.name if album.series else album.name).casefold(),
            album.name.casefold(),
        ),
    )

    series_groups: list[dict[str, object]] = []
    grouped_albums: dict[str, list[Album]] = {}
    for album in albums:
        series_name = album.series.name if album.series else album.name
        grouped_albums.setdefault(series_name, []).append(album)
    for series_name in sorted(grouped_albums.keys(), key=str.casefold):
        series_groups.append({"name": series_name, "albums": grouped_albums[series_name]})

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
        "series_groups": series_groups,
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
        return RedirectResponse(url=f"/?{query}", status_code=303)

    deleted, error = _delete_album_files(album)
    if not deleted:
        query = urlencode({"message": error or "Delete failed.", "message_type": "error"})
        return RedirectResponse(url=f"/?{query}", status_code=303)

    sync_result = sync_library(db)
    message = (
        f"Deleted album '{album.name}'. "
        f"Library synced {sync_result.get('synced', 0)}, "
        f"purged albums {sync_result.get('purged_albums', 0)}, "
        f"purged series {sync_result.get('purged_series', 0)}."
    )
    query = urlencode({"message": message, "message_type": "success"})
    return RedirectResponse(url=f"/?{query}", status_code=303)


@app.post("/albums/delete")
def bulk_delete_albums(
    selected_album_ids: list[int] = Form(default=[]),
    db: Session = Depends(get_session),
):
    if not selected_album_ids:
        query = urlencode({"message": "No albums selected.", "message_type": "error"})
        return RedirectResponse(url=f"/?{query}", status_code=303)

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
    return RedirectResponse(url=f"/?{query}", status_code=303)


@app.post("/upload-to-tonie")
async def upload_to_tonie(
    request: Request,
    selected_album_ids: list[int] = Form(default=[]),
    figure_id: str = Form(default=""),
    db: Session = Depends(get_session),
):
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
