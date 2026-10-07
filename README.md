# ia-downloader

A self-hosted web app for downloading audio albums from the [Internet Archive](https://archive.org) into a music library, with a download queue and a built-in player.

Paste an archive.org item or collection URL, and it downloads the best audio available into `<category>/<identifier>/` under your library folder.

## Features

- **Apple Music–style interface.** A Library of finished albums with album pages and track lists, a Downloads tab for the queue, and Search. Playback has a mini-player and a full-screen Now Playing view tinted from the album art, with lock-screen controls. On wide screens the tab bar becomes a sidebar. The previous interface is still available at `/classic`.
- **Items and whole collections.** A collection URL is expanded into one queued job per item.
- **Best format wins.** Formats are ranked by quality: FLAC, ALAC, MP3, Ogg Vorbis, then AAC (`.m4a`). ALAC and AAC are told apart using archive.org's per-file format label, and extension matching ignores case.
- **Archive uploads.** If an item has no loose audio but has `.zip` or `.7z` uploads, they are downloaded and extracted. Only the best audio format inside is kept, and the archives are deleted afterwards. Extraction checks free disk space first and never writes outside the album folder.
- **A queue you can steer.**
  - Pause and resume.
  - Retry failed jobs, singly or all at once. A single retry runs next.
  - Move any waiting job to the front.
  - The queue order is saved, so restarts and redeploys keep it, and an interrupted download resumes first.
- **Auto-categorization** into Video Game, Music, Classical, Jazz, Anime, or Sound Effects. You can change the category per album.
- **Built-in player** for finished albums, with shuffle and repeat.
- **Duplicate detection.** Items already in your download history are re-downloaded with checksum verification.

## Running it

Requires Docker. Downloads use the [`internetarchive`](https://github.com/jjjake/internetarchive) CLI, and extraction uses 7-Zip; both are installed in the image.

```bash
git clone https://github.com/bosspatrone/ia-downloader.git
cd ia-downloader
cp docker-compose.example.yml docker-compose.yml   # set your library path
docker compose up -d --build
```

Then open `http://<host>:8000`.

Most items download without an account. For items that need a login, run `ia configure` on the host and mount `~/.config/internetarchive` as shown in the example compose file.

## Files it keeps

Inside the mounted library folder (`/music` in the container):

| File | Purpose |
|---|---|
| `.ia-jobs.json` | All jobs and their status |
| `.ia-queue.json` | Queue order, restored on startup |
| `.ia-history` | Identifiers already downloaded, for duplicate detection |
| `.ia-blocklist` | Permanently deleted identifiers, never downloaded again |
| `.ia-art/` | Cached cover art |

## Stack

FastAPI and uvicorn. The frontend is plain HTML, CSS and JavaScript in `static/`, with no build step. Cover art is proxied and cached in `/music/.ia-art` so the page can read its colors. No database.

## License

[MIT](LICENSE)
