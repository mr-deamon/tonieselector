from pathlib import Path
from urllib.parse import urlencode

from fastapi import Depends, FastAPI, Form, Request
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


async def _build_index_context(request: Request, db: Session, message: str | None = None, message_type: str = "info"):
    albums = (
        db.execute(select(Album).order_by(Album.name.asc()))
        .scalars()
        .all()
    )

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
async def upload(
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
