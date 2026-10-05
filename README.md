# Mgpie CLI

This CLI application fethces audio tracks and metadata, with Mega Sync, it provides a reliable audio-grabbing experience

## Features

- Using a list of tracks, it fetches the metadata of a track from the web.
- Based on the metadata, it searches Youtube and relevant platforms for the track and downloads it. 
- The fetched metadata along with the corresponding album-art are then tagged to the corresponding downloaded audio track.
- An embedded database is maintained for tracing status of each track's information and accuracy. 

## Installation

```bash
pip install -r requirements.txt

## List audio tracks 
- Edit the songs.txt file (Artist Name - Track Title)

## Run the project

```bash
python3 main.py
