# videoserver

Simple local video streaming server built with Python's standard library.

## Run

```powershell
python server.py
```

The app starts on `http://127.0.0.1:5000` by default.

## Add videos

Place supported files in the `videos/` folder and refresh the page.

Supported extensions:

- `.mp4`
- `.m4v`
- `.mov`
- `.mkv`
- `.webm`
- `.avi`

The `videos/` folder is intentionally not tracked in git, so local media files do not get uploaded to GitHub.
