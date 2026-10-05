"""Resolve track metadata from Apple's public catalog before audio is fetched."""
import re
import unicodedata
from typing import Dict, Optional, Tuple
from urllib.parse import urlencode

import requests


def _norm(value: str) -> str:
    value = unicodedata.normalize("NFKD", value or "").encode("ascii", "ignore").decode().lower()
    return re.sub(r"[^a-z0-9]+", " ", value).strip()


def parse_track_query(raw_line: str) -> Tuple[Optional[str], str]:
    """Input list convention: `Artist;Other Artist Title`, or `Artist - Title`."""
    line = raw_line.strip()
    if " - " in line:
        artist, title = line.split(" - ", 1)
        return artist.strip(), title.strip()
    # Legacy list rows often contain semicolon-separated artists followed by an unmarked
    # title. Search the complete row; catalog results are accepted only when the track title
    # is an exact suffix of the row, avoiding an invented artist/title boundary.
    return None, line


def get_itunes_metadata(query: str) -> Optional[Dict]:
    artist, title = parse_track_query(query)
    term = " ".join(part for part in (artist, title) if part)
    try:
        response = requests.get(
            "https://itunes.apple.com/search?" + urlencode({"term": term, "entity": "song", "limit": 50}),
            timeout=20,
        )
        response.raise_for_status()
        results = response.json().get("results", [])
    except (requests.RequestException, ValueError) as exc:
        print(f"  → Metadata catalog lookup failed: {exc}")
        return None

    wanted_title = _norm(title)
    wanted_artist = _norm(artist or "")
    ranked = []
    for result in results:
        got_title = _norm(result.get("trackName", ""))
        got_artist = _norm(result.get("artistName", ""))
        # Penalize extras and version mismatches; require title and artist corroboration.
        if not artist:
            row_tokens = _norm(query).split()
            title_tokens = got_title.split()
            title_match = bool(title_tokens) and row_tokens[-len(title_tokens):] == title_tokens
            prefix = row_tokens[:-len(title_tokens)] if title_match else []
            artist_tokens = [token for token in got_artist.split() if len(token) > 2]
            artist_match = title_match and bool(artist_tokens) and all(token in prefix for token in artist_tokens)
        else:
            title_match = got_title == wanted_title or (wanted_title and wanted_title in got_title)
            artist_match = any(token in got_artist for token in wanted_artist.split() if len(token) > 2)
        if not title_match or not artist_match:
            continue
        score = (135 if not artist and title_match and artist_match else
                 (100 if got_title == wanted_title else 65) +
                 (35 if wanted_artist and got_artist == wanted_artist else 20 if artist_match else 0))
        if result.get("artworkUrl100"):
            score += 5
        ranked.append((score, result))
    if not ranked:
        return None
    ranked.sort(key=lambda item: item[0], reverse=True)
    score, result = ranked[0]
    if score < 100:
        return None
    art = result.get("artworkUrl100", "").replace("100x100bb", "1200x1200bb").replace("100x100", "1200x1200")
    return {
        "title": result.get("trackName"), "artists": result.get("artistName"),
        "album": result.get("collectionName"),
        "year": str(result.get("releaseDate", ""))[:4] or None,
        "genre": result.get("primaryGenreName"), "track_number": result.get("trackNumber"),
        "disc_number": result.get("discNumber"), "duration_ms": result.get("trackTimeMillis"),
        "album_art_url": art, "source": "iTunes", "catalog_id": result.get("trackId"),
        "confidence": {
            "title": 1.0 if got_title == wanted_title else 0.9,
            "artist": 1.0 if wanted_artist and got_artist == wanted_artist else 0.95 if not artist else 0.9,
            "album": 1.0 if result.get("collectionName") else 0.0,
            "album_art": 0.95 if art else 0.0,
        },
    }


def get_track_metadata(raw_line: str) -> Dict:
    meta = get_itunes_metadata(raw_line)
    if not meta or not all(meta.get(field) for field in ("title", "artists", "album")):
        raise ValueError(f"No confident title/artist/album catalog match for: {raw_line}")
    return meta
