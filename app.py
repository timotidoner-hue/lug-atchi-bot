import shutil
import tempfile
import threading
from pathlib import Path
from urllib.parse import urlparse

import yt_dlp
from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from starlette.background import BackgroundTask

app = FastAPI()
slots = threading.BoundedSemaphore(2)  # bir vaqtda ko'pi bilan 2 ta yuklash
HOSTS = {"youtube.com", "www.youtube.com", "m.youtube.com", "music.youtube.com", "youtu.be"}


def check(url: str):
    if urlparse(url).hostname not in HOSTS:
        raise HTTPException(400, "Faqat YouTube havolalari qabul qilinadi.")


@app.get("/api/info")
def info(url: str):
    check(url)
    try:
        with yt_dlp.YoutubeDL({"quiet": True, "noplaylist": True}) as y:
            d = y.extract_info(url, download=False)
    except yt_dlp.utils.DownloadError:
        raise HTTPException(422, "Video topilmadi. Havolani tekshirib, qayta urining.")
    heights = sorted(
        {f["height"] for f in d.get("formats", []) if f.get("height") and f.get("vcodec") != "none"},
        reverse=True,
    )
    return {
        "title": d.get("title"),
        "uploader": d.get("uploader"),
        "duration": d.get("duration"),
        "thumbnail": d.get("thumbnail"),
        "heights": [h for h in heights if h <= 1080][:5],
    }


@app.get("/api/download")
def download(url: str, kind: str = "video", height: int = 720):
    check(url)
    if not slots.acquire(blocking=False):
        raise HTTPException(429, "Server band. Bir ozdan keyin qayta urining.")
    tmp = tempfile.mkdtemp()

    def cleanup():
        shutil.rmtree(tmp, ignore_errors=True)
        slots.release()

    opts = {"quiet": True, "noplaylist": True, "outtmpl": f"{tmp}/%(title).80s.%(ext)s"}
    if kind == "audio":
        opts.update(
            format="bestaudio/best",
            postprocessors=[{"key": "FFmpegExtractAudio", "preferredcodec": "mp3", "preferredquality": "192"}],
        )
    else:
        h = height
        opts.update(
            format=f"bv*[height<={h}][ext=mp4]+ba[ext=m4a]/bv*[height<={h}]+ba/b[height<={h}]",
            merge_output_format="mp4",
        )
    try:
        with yt_dlp.YoutubeDL(opts) as y:
            y.download([url])
        f = next(Path(tmp).iterdir())
    except Exception:
        cleanup()
        raise HTTPException(500, "Yuklab bo'lmadi. ffmpeg o'rnatilganini va yt-dlp yangiligini tekshiring.")
    return FileResponse(f, filename=f.name, background=BackgroundTask(cleanup))


app.mount("/", StaticFiles(directory="static", html=True), name="static")
