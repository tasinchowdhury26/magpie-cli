"""Find track-matched lyrics, preferring verified LRCLIB records and synced LRC."""
import re
import unicodedata
import urllib.parse
import requests


def _norm(value):
    value = unicodedata.normalize("NFKD", value or "").encode("ascii", "ignore").decode().lower()
    return re.sub(r"[^a-z0-9]+", " ", value).strip()


def _same_artist(expected, found):
    expected_tokens = [token for token in _norm(expected).split() if len(token) > 1]
    found_tokens = _norm(found).split()
    # Catalog services format collaborations differently ("A & B" vs "A, B").
    # Require the full requested artist string to be represented, but allow extra credited artists.
    return bool(expected_tokens) and all(token in found_tokens for token in expected_tokens)


def _valid_lrc(value):
    return bool(value and re.search(r"\[\d{1,2}:\d{2}(?:\.\d{1,3})?\]", value)
                and len(re.findall(r"\[\d{1,2}:\d{2}(?:\.\d{1,3})?\]", value)) >= 2)


def _record_matches(data, artist, title, duration_ms):
    if _norm(data.get("trackName") or data.get("name")) != _norm(title):
        return False
    if not _same_artist(artist, data.get("artistName", "")):
        return False
    found_duration = data.get("duration")
    if duration_ms and found_duration:
        if abs(float(found_duration) - duration_ms / 1000) > max(10, duration_ms / 1000 * 0.08):
            return False
    return True


def _lrclib_search(artist, title, album, duration_ms):
    """Try exact lookup, then indexed structured and progressively broad queries."""
    if duration_ms:
        params = {
            "artist_name": artist,
            "track_name": title,
            "album_name": album or "",
            "duration": round(duration_ms / 1000),
        }
        try:
            response = requests.get("https://lrclib.net/api/get", params=params, timeout=15)
            if response.status_code == 200:
                data = response.json()
                if _record_matches(data, artist, title, duration_ms) and _valid_lrc(data.get("syncedLyrics")):
                    return data.get("plainLyrics"), data["syncedLyrics"], "LRCLIB"
        except (requests.RequestException, ValueError, TypeError):
            pass

    searches = [
        {"track_name": title, "artist_name": artist},
        {"track_name": title},
        {"q": f"{artist} {title}"},
        {"q": title},
    ]
    seen = set()
    matches = []
    plain_matches = []
    for params in searches:
        key = tuple(sorted(params.items()))
        if key in seen:
            continue
        seen.add(key)
        try:
            response = requests.get("https://lrclib.net/api/search", params=params, timeout=15)
            if response.status_code != 200:
                continue
            records = response.json()
            if not isinstance(records, list):
                continue
            for data in records:
                if not _record_matches(data, artist, title, duration_ms):
                    continue
                if data.get("plainLyrics"):
                    plain_matches.append(data["plainLyrics"])
                synced = data.get("syncedLyrics")
                if not _valid_lrc(synced):
                    continue
                # Album editions and compilations vary. Use album as a preference, not a rejection.
                album_score = int(bool(album and _norm(album) == _norm(data.get("albumName", ""))))
                matches.append((album_score, data.get("plainLyrics"), synced))
        except (requests.RequestException, ValueError, TypeError):
            continue
    if matches:
        matches.sort(key=lambda match: match[0], reverse=True)
        return matches[0][1], matches[0][2], "LRCLIB"
    return (plain_matches[0], None, "LRCLIB") if plain_matches else (None, None, None)


def _multi_provider_lrc(artist, title, album, duration_ms):
    """Use additional public providers only after LRCLIB's metadata-aware searches."""
    try:
        import syncedlyrics
    except ImportError:
        return None

    queries = [f"{title} {artist}", f"{artist} - {title}"]
    for query in queries:
        try:
            candidate = syncedlyrics.search(query, enhanced=False)
        except Exception:
            continue
        if not _valid_lrc(candidate):
            continue
        # Do not accept provider results that explicitly identify another recording.
        tags = {key: value for key, value in re.findall(r"\[(ar|ti|al):([^\]]+)\]", candidate, re.I)}
        if tags.get("ti") and _norm(tags["ti"]) != _norm(title):
            continue
        if tags.get("ar") and not _same_artist(artist, tags["ar"]):
            continue
        if tags.get("al") and album and _norm(tags["al"]) != _norm(album):
            # A different album/edition is acceptable only if duration is unavailable;
            # otherwise LRCLIB's duration-verified search is safer.
            if duration_ms:
                continue
        return candidate
    return None


def get_lyrics(artist: str, title: str, album: str = None, duration_ms: int = None):
    """Return best available plain and synchronized lyrics, with provider provenance."""
    plain, synced, source = _lrclib_search(artist, title, album, duration_ms)
    if not synced:
        synced = _multi_provider_lrc(artist, title, album, duration_ms)
        if synced:
            source = "syncedlyrics providers"
    if not plain:
        try:
            url = "https://api.lyrics.ovh/v1/{}/{}".format(
                urllib.parse.quote(artist, safe=""), urllib.parse.quote(title, safe="")
            )
            response = requests.get(url, timeout=12)
            if response.status_code == 200:
                plain = response.json().get("lyrics")
                if plain:
                    source = source or "lyrics.ovh"
        except (requests.RequestException, ValueError, TypeError):
            pass
    if not synced:
        return plain, None, source

    if not plain:
        plain = re.sub(r"\[\d{1,2}:\d{2}(?:\.\d{1,3})?\]", "", synced)
        plain = "\n".join(line.strip() for line in plain.splitlines() if line.strip())
    print("  → Verified synced lyrics")
    return plain, synced, source
