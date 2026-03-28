import shutil
import re
import unicodedata
from pathlib import Path

from mutagen import File as MutagenFile
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.config import settings
from app.models import Album, Series, Track

AUDIO_EXTENSIONS = {".mp3", ".m4a", ".flac", ".ogg", ".wav"}
IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png", ".webp"}


def _natural_sort_key(value: str) -> list[int | str]:
    parts = re.split(r"(\d+)", value)
    return [int(part) if part.isdigit() else part.casefold() for part in parts]


def _slugify(name: str) -> str:
    return "".join(char.lower() if char.isalnum() else "-" for char in name).strip("-")


def _normalize_text(value: str) -> str:
    return unicodedata.normalize("NFC", value).strip()


def _parse_flat_filename(file_path: Path) -> str | None:
    """Extract album name from a filename structured as 'Artist - Album - Part N'.
    The artist prefix is ignored; only the album portion is returned."""
    stem = _normalize_text(file_path.stem)
    pattern = re.compile(r"^(.*?)\s-\s(.*?)\s-\s(?:teil|track|part)\s*\d+$", flags=re.IGNORECASE)
    match = pattern.match(stem)
    if match:
        return _normalize_text(match.group(2))

    fallback_parts = [part.strip() for part in stem.split(" - ")]
    if len(fallback_parts) >= 2:
        # Take the second segment as album name (first segment is assumed to be artist)
        return _normalize_text(fallback_parts[1])

    return None


def _first_text_value(raw_value) -> str | None:
    if raw_value is None:
        return None

    if isinstance(raw_value, list):
        if not raw_value:
            return None
        candidate = raw_value[0]
        if isinstance(candidate, bytes):
            return None
        return _normalize_text(str(candidate))

    text = getattr(raw_value, "text", None)
    if isinstance(text, list) and text:
        return _normalize_text(str(text[0]))
    if text is not None:
        return _normalize_text(str(text))

    if isinstance(raw_value, bytes):
        return None

    return _normalize_text(str(raw_value))


def _read_album_name_from_metadata(audio_file: Path) -> str | None:
    """Read only the album tag from an audio file's metadata."""
    media = MutagenFile(audio_file)
    tags = getattr(media, "tags", None)
    if not tags:
        return None

    album_keys = ["\xa9alb", "TALB", "album"]

    def get_tag_value(possible_keys: list[str]) -> str | None:
        for key in possible_keys:
            try:
                found = key in tags
            except ValueError:
                continue
            if found:
                value = _first_text_value(tags.get(key))
                if value:
                    return value
        return None

    return get_tag_value(album_keys)


def _stage_flat_inbox_files(inbox_root: Path) -> int:
    staged = 0
    movable_extensions = AUDIO_EXTENSIONS | IMAGE_EXTENSIONS
    root_files = [path for path in inbox_root.iterdir() if path.is_file() and path.suffix.lower() in movable_extensions]

    audio_files = sorted(
        (path for path in root_files if path.suffix.lower() in AUDIO_EXTENSIONS),
        key=lambda path: _natural_sort_key(path.name),
    )
    image_files = sorted(
        (path for path in root_files if path.suffix.lower() in IMAGE_EXTENSIONS),
        key=lambda path: _natural_sort_key(path.name),
    )
    stem_targets: dict[str, str] = {}

    for file_path in audio_files:
        album_name = _read_album_name_from_metadata(file_path) or _parse_flat_filename(file_path)
        if album_name is None:
            continue

        target_dir = inbox_root / album_name
        target_dir.mkdir(parents=True, exist_ok=True)
        shutil.move(str(file_path), str(target_dir / file_path.name))
        stem_targets[_normalize_text(file_path.stem)] = album_name
        staged += 1

    for file_path in image_files:
        album_name = stem_targets.get(_normalize_text(file_path.stem)) or _parse_flat_filename(file_path)
        if album_name is None:
            continue

        target_dir = inbox_root / album_name
        target_dir.mkdir(parents=True, exist_ok=True)
        shutil.move(str(file_path), str(target_dir / file_path.name))
        staged += 1

    return staged


def _ensure_data_dirs() -> None:
    (settings.data_root / "inbox").mkdir(parents=True, exist_ok=True)
    (settings.data_root / "library").mkdir(parents=True, exist_ok=True)
    (settings.data_root / "processed").mkdir(parents=True, exist_ok=True)
    (settings.data_root / "rejected").mkdir(parents=True, exist_ok=True)
    (settings.data_root / "posters").mkdir(parents=True, exist_ok=True)


def _find_audio_files(folder: Path) -> list[Path]:
    return sorted(
        (path for path in folder.rglob("*") if path.suffix.lower() in AUDIO_EXTENSIONS),
        key=lambda path: _natural_sort_key(path.name),
    )


def _find_folder_poster(folder: Path) -> Path | None:
    images = sorted(path for path in folder.iterdir() if path.is_file() and path.suffix.lower() in IMAGE_EXTENSIONS)
    if not images:
        return None
    preferred = ["folder", "cover", "poster"]
    for keyword in preferred:
        for image in images:
            if keyword in image.stem.lower():
                return image
    return images[0]


def _extract_embedded_artwork(audio_file: Path, destination_stem: str) -> Path | None:
    media = MutagenFile(audio_file)
    if media is None or not getattr(media, "tags", None):
        return None

    output_path = settings.data_root / "posters" / f"{destination_stem}.jpg"

    # FLAC: pictures are stored on media.pictures, not in tags
    flac_pictures = getattr(media, "pictures", None)
    if flac_pictures:
        output_path.write_bytes(flac_pictures[0].data)
        return output_path

    tags = media.tags
    if tags is None:
        return None

    # MP4 (m4a)
    covr = tags.get("covr") if hasattr(tags, "get") else None
    if covr:
        first = covr[0] if isinstance(covr, list) else covr
        if first:
            output_path.write_bytes(bytes(first))
            return output_path

    # ID3 (mp3)
    if hasattr(tags, "getall"):
        pictures = tags.getall("APIC")
        if pictures:
            output_path.write_bytes(pictures[0].data)
            return output_path

    # Generic fallback: any tag value with a .data bytes attribute
    for tag in tags.values():
        data = getattr(tag, "data", None)
        if isinstance(data, bytes) and len(data) > 256:
            output_path.write_bytes(data)
            return output_path

    return None


def _read_duration_seconds(audio_file: Path) -> int:
    media = MutagenFile(audio_file)
    if media is None or media.info is None:
        return 0
    length = getattr(media.info, "length", 0) or 0
    return int(length)


def _upsert_album(session: Session, album_folder: Path, series_name: str, album_name: str) -> Album:
    series_slug = _slugify(series_name)
    album_slug = _slugify(album_name)

    series = session.scalar(select(Series).where(Series.slug == series_slug))
    if series is None:
        series = Series(name=series_name, slug=series_slug)
        session.add(series)
        session.flush()

    existing_album = session.scalar(
        select(Album).where(Album.series_id == series.id, Album.slug == album_slug)
    )
    if existing_album is not None:
        _refresh_album(session, existing_album, album_folder, f"{series_slug}-{album_slug}")
        return existing_album

    audio_files = _find_audio_files(album_folder)
    track_infos = [(path, _read_duration_seconds(path)) for path in audio_files]
    duration = sum(duration_seconds for _, duration_seconds in track_infos)

    poster = _find_folder_poster(album_folder)
    if poster is None and audio_files:
        poster = _extract_embedded_artwork(audio_files[0], f"{series_slug}-{album_slug}")

    album = Album(
        series_id=series.id,
        name=album_name,
        slug=album_slug,
        path=str(album_folder),
        poster_path=str(poster) if poster else None,
        duration_seconds=duration,
    )
    session.add(album)
    session.flush()

    for idx, (track_file, track_duration) in enumerate(track_infos, start=1):
        session.add(
            Track(
                album_id=album.id,
                title=track_file.stem,
                path=str(track_file),
                duration_seconds=track_duration,
                track_no=idx,
            )
        )

    return album


def _refresh_album(session: Session, album: Album, album_folder: Path, poster_stem: str) -> None:
    audio_files = _find_audio_files(album_folder)
    tracks_by_path = {track.path: track for track in album.tracks}
    seen_paths: set[str] = set()

    for idx, track_file in enumerate(audio_files, start=1):
        track_path = str(track_file)
        seen_paths.add(track_path)
        duration = _read_duration_seconds(track_file)

        existing_track = tracks_by_path.get(track_path)
        if existing_track is None:
            session.add(
                Track(
                    album_id=album.id,
                    title=track_file.stem,
                    path=track_path,
                    duration_seconds=duration,
                    track_no=idx,
                )
            )
            continue

        existing_track.title = track_file.stem
        existing_track.track_no = idx
        existing_track.duration_seconds = duration

    for track in list(album.tracks):
        if track.path not in seen_paths:
            session.delete(track)

    album.duration_seconds = sum(_read_duration_seconds(path) for path in audio_files)

    poster_missing = not album.poster_path or not Path(album.poster_path).exists()
    if poster_missing:
        poster = _find_folder_poster(album_folder)
        if poster is None and audio_files:
            poster = _extract_embedded_artwork(audio_files[0], poster_stem)
        album.poster_path = str(poster) if poster else None


def process_inbox(session: Session) -> dict:
    _ensure_data_dirs()

    inbox_root = settings.data_root / "inbox"
    library_root = settings.data_root / "library"
    processed_root = settings.data_root / "processed"
    rejected_root = settings.data_root / "rejected"

    added = 0
    duplicates = 0
    rejected = 0
    staged = _stage_flat_inbox_files(inbox_root)

    def _process_album_dir(album_dir: Path, series_name: str, album_name: str) -> str:
        """Move album_dir into library and register it. Returns 'added', 'duplicate', or 'rejected'."""
        album_slug = _slugify(album_name)
        series_slug = _slugify(series_name)

        existing_series = session.scalar(select(Series).where(Series.slug == series_slug))
        if existing_series is not None:
            existing_album = session.scalar(
                select(Album).where(Album.series_id == existing_series.id, Album.slug == album_slug)
            )
            if existing_album is not None:
                target = rejected_root / series_name / album_name
                target.parent.mkdir(parents=True, exist_ok=True)
                if target.exists():
                    shutil.rmtree(target)
                shutil.move(str(album_dir), str(target))
                return "duplicate"

        target_dir = library_root / series_name / album_name
        target_dir.parent.mkdir(parents=True, exist_ok=True)
        if target_dir.exists():
            target = rejected_root / series_name / album_name
            target.parent.mkdir(parents=True, exist_ok=True)
            if target.exists():
                shutil.rmtree(target)
            shutil.move(str(album_dir), str(target))
            return "duplicate"

        try:
            shutil.move(str(album_dir), str(target_dir))
            _upsert_album(session, target_dir, series_name, album_name)
            processed_target = processed_root / series_name
            processed_target.mkdir(parents=True, exist_ok=True)
            (processed_target / f"{album_name}.processed").write_text("ok", encoding="utf-8")
            return "added"
        except Exception:
            reject_target = rejected_root / series_name / album_name
            reject_target.parent.mkdir(parents=True, exist_ok=True)
            if reject_target.exists():
                shutil.rmtree(reject_target)
            if album_dir.exists():
                shutil.move(str(album_dir), str(reject_target))
            return "rejected"

    for inbox_child in sorted(path for path in inbox_root.iterdir() if path.is_dir()):
        child_subdirs = [p for p in inbox_child.iterdir() if p.is_dir()]
        child_audio = [p for p in inbox_child.iterdir() if p.is_file() and p.suffix.lower() in AUDIO_EXTENSIONS]

        if child_audio and not child_subdirs:
            # Flat album dir (new single-level layout): album name = series name
            album_name = inbox_child.name
            result = _process_album_dir(inbox_child, album_name, album_name)
        else:
            # Two-level layout: inbox_child is a series dir
            series_name = inbox_child.name
            for album_dir in sorted(child_subdirs):
                album_name = album_dir.name
                result = _process_album_dir(album_dir, series_name, album_name)
                if result == "added":
                    added += 1
                elif result == "duplicate":
                    duplicates += 1
                else:
                    rejected += 1
            if not any(inbox_child.iterdir()):
                inbox_child.rmdir()
            continue

        if result == "added":
            added += 1
        elif result == "duplicate":
            duplicates += 1
        else:
            rejected += 1

    session.commit()
    return {"staged": staged, "added": added, "duplicates": duplicates, "rejected": rejected}


def sync_library(session: Session) -> dict:
    _ensure_data_dirs()
    library_root = settings.data_root / "library"
    added = 0
    purged_albums = 0
    purged_series = 0
    existing_album_paths: set[str] = set()

    for series_dir in sorted(path for path in library_root.iterdir() if path.is_dir()):
        for album_dir in sorted(path for path in series_dir.iterdir() if path.is_dir()):
            existing_album_paths.add(str(album_dir))
            series_slug = _slugify(series_dir.name)
            album_slug = _slugify(album_dir.name)

            series = session.scalar(select(Series).where(Series.slug == series_slug))
            if series is not None:
                existing = session.scalar(
                    select(Album).where(Album.series_id == series.id, Album.slug == album_slug)
                )
                if existing is not None:
                    _refresh_album(
                        session,
                        existing,
                        album_dir,
                        f"{series_slug}-{album_slug}",
                    )
                    continue

            _upsert_album(session, album_dir, series_dir.name, album_dir.name)
            added += 1

    for album in session.scalars(select(Album)).all():
        if album.path not in existing_album_paths:
            session.delete(album)
            purged_albums += 1

    for series in session.scalars(select(Series)).all():
        album_count = session.scalar(select(func.count(Album.id)).where(Album.series_id == series.id)) or 0
        if album_count == 0:
            session.delete(series)
            purged_series += 1

    session.commit()
    return {"synced": added, "purged_albums": purged_albums, "purged_series": purged_series}
