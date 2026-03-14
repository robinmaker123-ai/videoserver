# videoserver

Simple local video streaming server built with Python's standard library.

## Run

```powershell
python server.py
```

The app starts on `http://127.0.0.1:5000` by default.

To stream videos from another folder on your machine:

```powershell
$env:VIDEO_DIR="D:\Movies"
python server.py
```

## Add videos

This GitHub repo contains only the app code. It does not include your actual media files.

Place supported files in the local `videos/` folder, use the upload button in the web UI, or point `VIDEO_DIR` to your existing movie folder, then refresh the page.
You can also paste a folder path such as `D:\Movies` into the web UI and switch the library instantly without waiting for a browser upload.

Supported extensions:

- `.mp4`
- `.m4v`
- `.mov`
- `.mkv`
- `.webm`
- `.avi`

The `videos/` folder is intentionally not tracked in git, so local media files do not get uploaded to GitHub.

## Notes

- Uploaded videos are saved into the active `VIDEO_DIR`.
- The homepage can open the current library folder and switch to a different local folder path for faster imports.
- GitHub itself will not store large local movie files from this project unless you use a separate storage solution.
