import argparse
import copy
import hashlib
import re
import time
import unicodedata
from datetime import datetime, timezone
from pathlib import Path

import yaml

from src.database import TrackDatabase, track_key, utc_now
from src.metadata import get_track_metadata
from src.downloader import download_track
from src.lyrics import get_lyrics
from src.tagger import embed_everything


def load_config():
    with open("config.yaml", encoding="utf-8") as f:
        return yaml.safe_load(f)


def sanitize_filename(name: str) -> str:
    name = re.sub(r'[<>:"/\\|?*]', "", name)
    return re.sub(r"\s+", " ", name).strip()


def normalize_track_id(artist: str, title: str) -> str:
    value = unicodedata.normalize("NFKD", f"{artist} - {title}").encode("ascii", "ignore").decode().lower()
    return re.sub(r"[^a-z0-9]+", " ", value).strip()


def log_step(log, message, status="INFO"):
    stamp = datetime.now().astimezone().strftime("%H:%M:%S")
    line = f"  [{status:<5}] {message}"
    print(line, flush=True)
    log.write(f"[{stamp}] {line}\n")
    log.flush()


def field(value, confidence, source):
    return {"value": value, "confidence": round(float(confidence or 0), 3),
            "source": source, "checked_at": utc_now()}


def cached_metadata(record):
    values = record.get("metadata", {})

    def get(name):
        entry = values.get(name, {})
        return entry.get("value") if isinstance(entry, dict) else entry

    title, artist, album = get("title"), get("artist"), get("album")
    if not all((title, artist, album)):
        return None
    return {
        "title": title,
        "artists": artist,
        "album": album,
        "year": get("year"),
        "genre": get("genre"),
        "track_number": get("track_number"),
        "disc_number": get("disc_number"),
        "duration_ms": get("duration_ms"),
        "album_art_url": get("album_art"),
        "source": get("metadata_source") or "database cache",
        "catalog_id": get("catalog_id"),
        "confidence": {
            "title": values.get("title", {}).get("confidence", 0),
            "artist": values.get("artist", {}).get("confidence", 0),
            "album": values.get("album", {}).get("confidence", 0),
            "album_art": values.get("album_art", {}).get("confidence", 0),
        },
    }


def store_metadata(record, meta):
    source = meta.get("source", "unknown")
    confidence = meta.get("confidence", {})
    values = {
        "title": (meta.get("title"), confidence.get("title", 0.9)),
        "artist": (meta.get("artists"), confidence.get("artist", 0.9)),
        "album": (meta.get("album"), confidence.get("album", 0.9)),
        "year": (meta.get("year"), 0.9 if meta.get("year") else 0),
        "genre": (meta.get("genre"), 0.8 if meta.get("genre") else 0),
        "track_number": (meta.get("track_number"), 0.9 if meta.get("track_number") else 0),
        "disc_number": (meta.get("disc_number"), 0.9 if meta.get("disc_number") else 0),
        "duration_ms": (meta.get("duration_ms"), 0.9 if meta.get("duration_ms") else 0),
        "album_art": (meta.get("album_art_url"), confidence.get("album_art", 0)),
        "metadata_source": (source, 1.0),
        "catalog_id": (meta.get("catalog_id"), 1.0 if meta.get("catalog_id") else 0),
    }
    record["metadata"] = {
        key: field(value, score, source if key != "metadata_source" else value)
        for key, (value, score) in values.items()
    }


def same_track(record, meta):
    old = cached_metadata(record) if record else None
    return bool(old and normalize_track_id(old["artists"], old["title"])
                == normalize_track_id(meta["artists"], meta["title"]))


def parse_args():
    parser = argparse.ArgumentParser(description="Download and tag the configured track list")
    parser.add_argument("--refresh", action="store_true",
                        help="redownload and replace audio even for completed tracks")
    parser.add_argument("--track", help="process only input rows containing this text")
    return parser.parse_args()


def main():
    args = parse_args()
    config = load_config()
    songs_file = config["paths"]["songs_file"]
    output_dir = Path(config["download"]["output_dir"])
    max_songs = config["download"].get("max_songs")
    delay = config["download"].get("delay_between_songs", 3)
    skip_completed = config["download"].get("skip_existing", True)
    log_file = config["paths"].get("log_file", "download_log.txt")
    db_path = config["paths"].get("database_file", "./database.json")
    database = TrackDatabase(db_path)  # Loaded once; every track lookup is a dict lookup.
    instrumental_tracks = {
        normalize_track_id(*entry.split(" - ", 1))
        for entry in config.get("lyrics", {}).get("instrumental_tracks", [])
        if " - " in entry
    }
    output_dir.mkdir(parents=True, exist_ok=True)

    with open(songs_file, encoding="utf-8") as f:
        lines = [line.strip() for line in f if line.strip() and not line.startswith("#")]
    if args.track:
        lines = [line for line in lines if args.track.casefold() in line.casefold()]
    songs = lines if max_songs is None else lines[:max_songs]
    total = len(songs)
    print("╭──────────────────────────────────────────────────────────────╮")
    print(f"│ MAGPIE  ·  {total} tracks queued  ·  database: {db_path}")
    print("╰──────────────────────────────────────────────────────────────╯")

    attempted = completed = failed = skipped = asset_gaps = 0
    interrupted = False
    with open(log_file, "a", encoding="utf-8") as log:
        log.write(f"\n{'=' * 76}\nRun started: {utc_now()}\n")
        log.flush()
        for i, raw_line in enumerate(songs, 1):
            attempted = i
            previous = database.get(raw_line)  # O(1) lookup; no per-track file/database scan.
            prior_record = copy.deepcopy(previous) if previous else None

            # Skip a fully complete item only when its recorded audio is still at the configured path.
            if skip_completed and not args.refresh and prior_record and prior_record.get("status") == "complete":
                saved_audio = prior_record.get("audio", {})
                old_audio_path = Path(saved_audio["path"]) if saved_audio.get("path") else None
                same_file_size = (old_audio_path and old_audio_path.is_file()
                                  and old_audio_path.stat().st_size == saved_audio.get("file_size_bytes"))
                if (same_file_size and old_audio_path.parent.resolve() == output_dir.resolve()):
                    print(f"\n┌─ [{i:02d}/{total:02d}] {raw_line}")
                    log.write(f"\nTRACK {i}/{total}: {raw_line}\n")
                    log_step(log, f"Already complete; using database record {track_key(raw_line)}", "SKIP")
                    print("└" + "─" * 68, flush=True)
                    skipped += 1
                    continue

            record = prior_record or {
                "track_id": track_key(raw_line),
                "input": raw_line,
                "created_at": utc_now(),
                "attempts": 0,
                "metadata": {},
                "audio": {},
                "lyrics": {},
                "artwork": {},
                "issues": [],
            }
            record["input"] = raw_line
            record["status"] = "processing"
            record["attempts"] = int(record.get("attempts", 0)) + 1
            record["last_attempt"] = utc_now()
            record["issues"] = []
            database.put(raw_line, record)
            database.save()  # Persist the in-progress state before long network operations.

            print(f"\n┌─ [{i:02d}/{total:02d}] {raw_line}")
            log.write(f"\n{'-' * 76}\nTRACK {i}/{total}: {raw_line}\n")
            log.flush()
            track_failed = False
            try:
                log_step(log, "Searching web catalog for title, artist, album and artwork…")
                try:
                    meta = get_track_metadata(raw_line)
                    store_metadata(record, meta)
                    log_step(log, f"Match: {meta['artists']} — {meta['title']}  ·  {meta['album']}", "OK")
                except Exception as exc:
                    meta = cached_metadata(record)
                    if not meta:
                        raise
                    log_step(log, f"Catalog unavailable ({exc}); reusing saved metadata", "WARN")

                query = f"{meta['artists']} - {meta['title']}"
                old_audio = (prior_record or {}).get("audio", {})
                old_path = Path(old_audio.get("path", "")) if old_audio.get("path") else None
                old_confidence = float(old_audio.get("confidence", 0) or 0)
                can_reuse_audio = (
                    not args.refresh and old_path and old_path.is_file()
                    and old_path.parent.resolve() == output_dir.resolve()
                    and old_confidence >= 0.78 and same_track(prior_record, meta)
                )
                if can_reuse_audio:
                    mp3_path = str(old_path)
                    source_info = old_audio.get("source_info", {})
                    log_step(log, f"Reusing previously verified audio (confidence {old_confidence:.0%})", "OK")
                else:
                    log_step(log, "Searching audio-only results; filtering video/talk formats and checking duration…")
                    mp3_path, source_info = download_track(
                        query, str(output_dir), expected_duration_ms=meta.get("duration_ms")
                    )
                    if not mp3_path:
                        raise RuntimeError("No suitable audio result passed title/artist/duration checks")
                    source_info = source_info or {}
                    log_step(log, f"Audio fetched: {source_info.get('title', 'unknown')} · "
                             f"{source_info.get('channel') or source_info.get('uploader') or 'unknown source'}", "OK")

                source_info = source_info or {}
                audio_confidence = float(source_info.get("_magpie_match_confidence", old_confidence or 0.8))
                record["audio"] = {
                    "path": str(Path(mp3_path)),
                    "source": "YouTube",
                    "source_id": source_info.get("id") or old_audio.get("source_id"),
                    "source_title": source_info.get("title") or old_audio.get("source_title"),
                    "source_channel": source_info.get("channel") or source_info.get("uploader") or old_audio.get("source_channel"),
                    "source_info": {
                        "id": source_info.get("id") or old_audio.get("source_id"),
                        "title": source_info.get("title") or old_audio.get("source_title"),
                        "channel": source_info.get("channel") or source_info.get("uploader") or old_audio.get("source_channel"),
                        "title_match_confidence": source_info.get("_magpie_title_match_confidence",
                                                                  old_audio.get("match_evidence", {}).get("title", 0)),
                        "artist_match_confidence": source_info.get("_magpie_artist_match_confidence",
                                                                   old_audio.get("match_evidence", {}).get("artist", 0)),
                        "duration_confidence": source_info.get("_magpie_duration_confidence",
                                                               old_audio.get("match_evidence", {}).get("duration", 0)),
                    },
                    "duration_seconds": source_info.get("duration") or old_audio.get("duration_seconds"),
                    "duration_gap_seconds": source_info.get("_magpie_duration_gap_seconds", old_audio.get("duration_gap_seconds")),
                    "confidence": round(audio_confidence, 3),
                    "match_evidence": {
                        "title": source_info.get("_magpie_title_match_confidence",
                                                 old_audio.get("match_evidence", {}).get("title", 0)),
                        "artist": source_info.get("_magpie_artist_match_confidence",
                                                  old_audio.get("match_evidence", {}).get("artist", 0)),
                        "duration": source_info.get("_magpie_duration_confidence",
                                                    old_audio.get("match_evidence", {}).get("duration", 0)),
                    },
                    "checked_at": utc_now(),
                }

                log_step(log, "Looking up plain and synchronized lyrics…")
                try:
                    plain, synced, lyrics_source = get_lyrics(
                        meta["artists"], meta["title"], meta.get("album"), meta.get("duration_ms")
                    )
                except Exception as exc:
                    plain, synced, lyrics_source = None, None, None
                    log_step(log, f"Lyrics lookup failed: {exc}", "WARN")

                instrumental = normalize_track_id(meta["artists"], meta["title"]) in instrumental_tracks
                if instrumental:
                    plain, synced = "Instrumental", None
                lyrics_confidence = (1.0 if instrumental else 0.96 if lyrics_source == "LRCLIB" and synced
                                     else 0.78 if synced else 0.7 if plain else 0)
                record["lyrics"] = {
                    "plain_available": bool(plain),
                    "synced_available": bool(synced),
                    "instrumental": instrumental,
                    "source": lyrics_source or ("config:instrumental_tracks" if instrumental else None),
                    "confidence": round(lyrics_confidence, 3),
                    "sidecar_path": str((output_dir / sanitize_filename(
                        f"{meta['artists']} - {meta['title']}.lrc"))) if synced else None,
                    "synced_content_sha256": hashlib.sha256(synced.encode("utf-8")).hexdigest() if synced else None,
                    "checked_at": utc_now(),
                }
                if instrumental:
                    log_step(log, "Confirmed instrumental; no lyric text or timed LRC is expected", "INFO")
                elif plain and synced:
                    log_step(log, f"Plain and synced lyrics found ({lyrics_source})", "OK")
                elif plain:
                    log_step(log, f"Plain lyrics found ({lyrics_source}); no synced LRC found", "WARN")
                else:
                    log_step(log, "Lyrics not found; continuing with the available tags", "WARN")

                target = output_dir / sanitize_filename(f"{meta['artists']} - {meta['title']}.mp3")
                source_path = Path(mp3_path)
                log_step(log, "Embedding catalog metadata, artwork and available lyrics…")
                embed_result = embed_everything(str(source_path), meta, plain, synced, instrumental=instrumental)
                source_lrc = source_path.with_suffix(".lrc")
                target_lrc = target.with_suffix(".lrc")
                if source_path.resolve() != target.resolve():
                    # Only replace an existing library copy after the temporary download
                    # has been tagged and read-back verification has succeeded.
                    source_path.replace(target)
                    if source_lrc.exists():
                        source_lrc.replace(target_lrc)
                if not embed_result["synced_lyrics"] and target_lrc.exists():
                    target_lrc.unlink()
                art_confidence = float(meta.get("confidence", {}).get("album_art", 0.0) or 0.0)
                record["artwork"] = {
                    "url": meta.get("album_art_url"),
                    "embedded": bool(embed_result["artwork"]),
                    "confidence": round(art_confidence if embed_result["artwork"] else 0, 3),
                    "source": meta.get("source"),
                    "checked_at": utc_now(),
                }
                record["audio"]["path"] = str(target)
                record["audio"]["file_size_bytes"] = target.stat().st_size

                issues = []
                metadata_confidence = meta.get("confidence", {})
                for key, value in (("title", metadata_confidence.get("title", 0)),
                                   ("artist", metadata_confidence.get("artist", 0)),
                                   ("album", metadata_confidence.get("album", 0))):
                    if float(value or 0) < 0.9:
                        issues.append(f"{key}_confidence_below_threshold")
                if not embed_result["artwork"]:
                    issues.append("album_art_missing")
                if not plain and not instrumental:
                    issues.append("lyrics_missing")
                if not synced and not instrumental:
                    issues.append("synced_lyrics_missing")
                if audio_confidence < 0.78:
                    issues.append("audio_match_confidence_below_threshold")
                record["issues"] = issues
                record["status"] = "complete" if not issues else "complete_with_gaps"
                record["last_error"] = None
                record["completed_at"] = utc_now()
                completed += 1
                asset_gaps += len(issues)
                log_step(log, f"Database updated · {record['status']} · {target.name}", "DONE")
            except KeyboardInterrupt:
                interrupted = True
                record["status"] = "incomplete"
                record["last_error"] = "Interrupted by user"
                record["issues"] = ["interrupted"]
                track_failed = True
                log_step(log, "Interrupted by user; remaining tracks were not attempted", "WARN")
            except Exception as exc:
                record["status"] = "incomplete"
                record["last_error"] = str(exc)
                record.setdefault("issues", []).append("processing_failed")
                track_failed = True
                log_step(log, f"Track could not be completed: {exc}", "FAIL")
            finally:
                record["updated_at"] = utc_now()
                database.put(raw_line, record)
                database.save()
                print("└" + "─" * 68, flush=True)

            if track_failed:
                failed += 1
                if isinstance(record.get("last_error"), str) and record["last_error"] == "Interrupted by user":
                    break
            if i < total and delay > 0:
                try:
                    time.sleep(delay)
                except KeyboardInterrupt:
                    interrupted = True
                    log_step(log, "Interrupted by user; remaining tracks were not attempted", "WARN")
                    break

        log.write(
            f"\nRUN SUMMARY: attempted={attempted}, completed={completed}, skipped={skipped}, "
            f"failed={failed}, quality_gaps={asset_gaps}, interrupted={interrupted}\n{'=' * 76}\n"
        )

    print("\n╭──────────────────────── RUN SUMMARY ─────────────────────────╮")
    print(f"│ Tracks attempted : {attempted:<42}│")
    print(f"│ Completed        : {completed:<42}│")
    print(f"│ Already complete : {skipped:<42}│")
    print(f"│ Failed           : {failed:<42}│")
    print(f"│ Not attempted    : {total - attempted:<42}│")
    print(f"│ Quality gaps     : {asset_gaps:<42}│")
    print(f"│ Interrupted      : {str(interrupted):<42}│")
    print(f"│ Database         : {db_path:<42}│")
    print(f"│ Detailed log     : {log_file:<42}│")
    print("╰──────────────────────────────────────────────────────────────╯")


if __name__ == "__main__":
    main()
