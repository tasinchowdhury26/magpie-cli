"""Embed catalog metadata, lyrics and artwork that were successfully retrieved."""
import re
from pathlib import Path

import requests
from mutagen.mp3 import MP3
from mutagen.id3 import (
    ID3, TIT2, TPE1, TPE2, TALB, TDRC, TCON, TRCK, TPOS,
    APIC, USLT, SYLT, COMM, error,
)


_LRC_STAMP = re.compile(r"\[(\d{1,2}):(\d{2})(?:\.(\d{1,3}))?\]")


def _parse_lrc(lrc_text):
    entries = []
    for line in (lrc_text or "").splitlines():
        stamps = list(_LRC_STAMP.finditer(line))
        lyric = _LRC_STAMP.sub("", line).strip()
        if not stamps or not lyric:
            continue
        for stamp in stamps:
            minutes, seconds, fraction = stamp.groups()
            milliseconds = int(minutes) * 60000 + int(seconds) * 1000
            if fraction:
                milliseconds += int(fraction.ljust(3, "0"))
            entries.append((lyric, milliseconds))
    return sorted(entries, key=lambda item: item[1])


def embed_everything(mp3_path: str, meta: dict, plain_lyrics=None, synced_lrc=None, instrumental=False):
    required = ("title", "artists", "album")
    missing = [field for field in required if not meta.get(field)]
    if missing:
        raise ValueError(f"Required catalog metadata is missing: {', '.join(missing)}")

    image_data = None
    art_url = meta.get("album_art_url")
    if art_url:
        try:
            response = requests.get(art_url, timeout=20)
            response.raise_for_status()
            content_type = response.headers.get("Content-Type", "image/jpeg").split(";")[0]
            if len(response.content) > 2000 and content_type.startswith("image/"):
                image_data = (response.content, content_type)
            else:
                print("  ⚠ Artwork response was not a valid image; embedding other available tags")
        except requests.RequestException as exc:
            print(f"  ⚠ Artwork fetch failed ({exc}); embedding other available tags")
    else:
        print("  ⚠ No artwork URL was returned; embedding other available tags")

    try:
        audio = MP3(mp3_path, ID3=ID3)
    except error:
        audio = MP3(mp3_path)
        audio.add_tags()

    # Replace downloader-generated tags with the catalog values selected by this workflow.
    audio.delete()
    audio = MP3(mp3_path)
    audio.add_tags()

    title = meta["title"]
    artists = meta["artists"]
    featured = meta.get("featured")
    display_title = f"{title} (feat. {featured})" if featured else title

    audio.tags.add(TIT2(encoding=3, text=display_title))
    audio.tags.add(TPE1(encoding=3, text=artists))
    audio.tags.add(TPE2(encoding=3, text=artists))
    audio.tags.add(TALB(encoding=3, text=meta["album"]))
    for key, frame in (("year", TDRC), ("genre", TCON),
                       ("track_number", TRCK), ("disc_number", TPOS)):
        if meta.get(key):
            audio.tags.add(frame(encoding=3, text=str(meta[key])))

    if image_data:
        data, content_type = image_data
        audio.tags.add(APIC(encoding=3, mime=content_type, type=3, desc="Cover", data=data))
    if plain_lyrics:
        audio.tags.add(USLT(encoding=3, lang="eng", desc="Lyrics", text=plain_lyrics))
    timed_entries = _parse_lrc(synced_lrc)
    if timed_entries:
        audio.tags.add(SYLT(encoding=3, lang="eng", format=2, type=1, desc="Lyrics", text=timed_entries))
    source = meta.get("source", "unknown")
    audio.tags.add(COMM(encoding=3, lang="eng", desc="Source", text=f"Metadata from {source}"))
    audio.save()

    lrc_path = Path(mp3_path).with_suffix(".lrc")
    if synced_lrc and timed_entries:
        lrc_path.write_text(synced_lrc.strip() + "\n", encoding="utf-8")

    # Verify core tags and every optional asset we intended to embed.
    check = MP3(mp3_path, ID3=ID3)
    tags = check.tags or {}
    if str(tags.get("TIT2", "")) != display_title:
        raise ValueError("Embedded title verification failed")
    if str(tags.get("TPE1", "")) != artists:
        raise ValueError("Embedded artist verification failed")
    if str(tags.get("TALB", "")) != meta["album"]:
        raise ValueError("Embedded album verification failed")
    if image_data and not any(isinstance(frame, APIC) and frame.type == 3 for frame in tags.values()):
        raise ValueError("Embedded artwork verification failed")
    if plain_lyrics and not any(isinstance(frame, USLT) and frame.text.strip() for frame in tags.values()):
        raise ValueError("Embedded plain lyrics verification failed")
    if timed_entries:
        if not any(isinstance(frame, SYLT) and frame.text for frame in tags.values()):
            raise ValueError("Embedded synced lyrics verification failed")
        if not lrc_path.exists() or not lrc_path.read_text(encoding="utf-8").strip():
            raise ValueError("Synced .lrc sidecar verification failed")

    print("  ✓ Core metadata tags verified")
    print(f"  {'✓' if image_data else '⚠'} Album artwork {'embedded' if image_data else 'unavailable'}")
    print(f"  {'✓' if plain_lyrics else '⚠'} Plain lyrics {'embedded' if plain_lyrics else 'unavailable'}")
    print(f"  {'✓' if timed_entries else '⚠'} Synced lyrics/LRC {'embedded' if timed_entries else 'unavailable'}")
    return {"artwork": bool(image_data), "plain_lyrics": bool(plain_lyrics), "synced_lyrics": bool(timed_entries)}
