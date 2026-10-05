import yt_dlp
import re
import unicodedata
import uuid
from pathlib import Path

def to_title_case(text):
    small_words = {'a', 'an', 'the', 'and', 'but', 'or', 'for', 'nor', 'on', 'at', 'to', 'from', 'by', 'of', 'in', 'with'}
    words = text.strip().split()
    if not words:
        return text
    result = []
    for i, word in enumerate(words):
        lower = word.lower()
        if i == 0 or i == len(words) - 1 or lower not in small_words:
            result.append(word.capitalize())
        else:
            result.append(lower)
    return " ".join(result)

def clean_title(title):
    patterns = [
        r'\(Official Video\)', r'\(Official Music Video\)', r'\(Official Audio\)',
        r'\(Lyrics\)', r'\(Lyric Video\)', r'\[Official Video\]',
        r'\[Official Music Video\]', r'\[Official Audio\]',
        r'ft\..*', r'feat\..*', r'Feat\..*', r'\|.*',
    ]
    for p in patterns:
        title = re.sub(p, '', title, flags=re.IGNORECASE)
    return title.strip(" -–|")

def sanitize_filename(name: str) -> str:
    # Remove characters that break filesystems
    return re.sub(r'[<>:"/\\|?*]', '', name).strip()

def download_track(query, output_dir="./library", expected_duration_ms=None):
    Path(output_dir).mkdir(parents=True, exist_ok=True)
    run_id = uuid.uuid4().hex

    search_attempts = [f"{query} official audio", f"{query} topic", f"{query} audio", query]

    ydl_opts_base = {
        "format": "bestaudio/best",
        "format_sort": ["abr", "asr", "filesize"],
        "outtmpl": f"{output_dir}/temp_{run_id}_%(id)s.%(ext)s",
        "postprocessors": [
            {
                "key": "FFmpegExtractAudio",
                "preferredcodec": "mp3",
                "preferredquality": "320",
            },
            {
                "key": "FFmpegMetadata",
                "add_metadata": True,
            },
        ],
        "quiet": True,
        "no_warnings": True,
        "noplaylist": True,
        "socket_timeout": 30,
        "progress_hooks": [
            lambda status: print(
                f"    ↳ Download progress: {status.get('_percent_str', '').strip()}",
                end="\r" if status.get("status") == "downloading" else "\n",
                flush=True,
            ) if status.get("status") in ("downloading", "finished") else None
        ],
    }

    for search in search_attempts:
        ydl_opts = ydl_opts_base.copy()

        try:
            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                info = ydl.extract_info(f"ytsearch10:{search}", download=False)

                entries = [entry for entry in (info.get("entries") or []) if entry]
                if not entries:
                    continue

                def norm(value):
                    value = unicodedata.normalize("NFKD", value or "").encode("ascii", "ignore").decode().lower()
                    return set(re.findall(r"[a-z0-9]+", value))

                artist, title = query.split(" - ", 1) if " - " in query else ("", query)
                expected_title = norm(title)
                expected_artist = norm(artist)
                ranked = []
                for candidate in entries:
                    duration = candidate.get("duration") or 0
                    if duration <= 0 or duration > 900:
                        continue
                    if expected_duration_ms:
                        expected_seconds = expected_duration_ms / 1000
                        # Allow small edits/rounding, but reject likely alternate recordings.
                        if abs(duration - expected_seconds) > max(8, expected_seconds * 0.06):
                            continue
                    candidate_title = candidate.get("title") or ""
                    title_lower = candidate_title.lower()
                    candidate_channel = candidate.get("channel") or candidate.get("uploader") or ""
                    title_excludes = (
                        "official video", "music video", "reaction", "karaoke", "interview",
                        "podcast", "commentary", "review", "discussion", "side talk", "sidetalk",
                        "talk show", "spoken intro", "behind the scenes", "explained", "teaser",
                        "trailer", "fan made", "fanmade",
                    )
                    channel_excludes = ("podcast", "reaction", "interview", "commentary")
                    if (any(term in title_lower for term in title_excludes)
                            or any(term in candidate_channel.lower() for term in channel_excludes)):
                        continue
                    title_words = norm(candidate_title)
                    title_tokens = {word for word in expected_title if len(word) > 1}
                    artist_tokens = {word for word in expected_artist if len(word) > 1}
                    title_overlap = len(title_tokens & title_words) / max(len(title_tokens), 1)
                    candidate_words = norm(candidate_title + " " + candidate_channel)
                    artist_overlap = len(artist_tokens & candidate_words) / max(len(artist_tokens), 1)
                    if title_overlap >= 0.80 and (not artist_tokens or artist_overlap >= 0.34):
                        channel_text = candidate_channel.lower()
                        source_bonus = 2 if " - topic" in channel_text or "official" in channel_text or "vevo" in channel_text else 0
                        expected_seconds = expected_duration_ms / 1000 if expected_duration_ms else None
                        duration_gap = abs(duration - expected_seconds) if expected_seconds else None
                        duration_confidence = (
                            max(0.0, 1.0 - duration_gap / max(8.0, expected_seconds * 0.06))
                            if duration_gap is not None else 0.65
                        )
                        confidence = min(1.0, 0.65 * title_overlap + 0.25 * artist_overlap + 0.10 * duration_confidence)
                        ranked.append((title_overlap * 4 + artist_overlap + source_bonus,
                                       candidate, confidence, duration_gap, title_overlap,
                                       artist_overlap, duration_confidence))
                if not ranked:
                    continue
                ranked.sort(key=lambda item: item[0], reverse=True)
                _, entry, confidence, duration_gap, title_overlap, artist_overlap, duration_confidence = ranked[0]
                entry["_magpie_match_confidence"] = round(confidence, 4)
                entry["_magpie_duration_gap_seconds"] = round(duration_gap, 3) if duration_gap is not None else None
                entry["_magpie_title_match_confidence"] = round(title_overlap, 4)
                entry["_magpie_artist_match_confidence"] = round(artist_overlap, 4)
                entry["_magpie_duration_confidence"] = round(duration_confidence, 4)

                # Actual download
                ydl.download([entry["webpage_url"]])

                temp_files = list(Path(output_dir).glob(f"temp_{run_id}_*.mp3"))
                if not temp_files:
                    continue

                temp_path = str(max(temp_files, key=lambda path: path.stat().st_mtime))

                # Return the fresh download; the caller applies the catalog filename only
                # after all metadata/tagging work has completed.
                return temp_path, entry

        except Exception as e:
            # print(f"  → Search attempt failed: {e}")  # uncomment for debug
            continue

    print("  → Could not find a clean version")
    return None, None
