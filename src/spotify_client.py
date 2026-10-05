import spotipy
from spotipy.oauth2 import SpotifyOAuth
import yaml
import os

def load_config():
    with open("config.yaml", "r") as f:
        return yaml.safe_load(f)

def get_spotify_client():
    config = load_config()
    sp_config = config["spotify"]

    auth_manager = SpotifyOAuth(
        client_id=sp_config["client_id"],
        client_secret=sp_config["client_secret"],
        redirect_uri=sp_config["redirect_uri"],
        scope=sp_config["scope"],
        cache_path="./cache/.spotify_cache"
    )
    return spotipy.Spotify(auth_manager=auth_manager)

def get_liked_songs(limit=50):
    sp = get_spotify_client()
    results = sp.current_user_saved_tracks(limit=limit)
    tracks = []

    for item in results["items"]:
        track = item["track"]
        if track is None:
            continue
        tracks.append({
            "name": track["name"],
            "artists": ", ".join([a["name"] for a in track["artists"]]),
            "album": track["album"]["name"],
            "album_art": track["album"]["images"][0]["url"] if track["album"]["images"] else None,
            "duration_ms": track["duration_ms"],
            "spotify_id": track["id"]
        })
    return tracks

