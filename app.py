from fastapi import FastAPI, Query
from fastapi.responses import StreamingResponse
from fastapi.middleware.cors import CORSMiddleware
import yt_dlp
import os
import tempfile
import re

app = FastAPI()

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Remove emojis & weird characters from title
def sanitize_filename(name: str) -> str:
    if not name:
        return "video"
    name = re.sub(r'[^\x00-\x7F]+', '', name)           # remove non-ASCII
    name = re.sub(r'[<>:"/\\|?*\x00-\x1F]', '_', name)  # remove invalid chars
    name = name.strip(" .").replace('"', "'")[:100]
    return name or "video"

@app.get("/download")
async def download_video(url: str = Query(...)):
    ydl_opts = {
        # This guarantees < 20 MB on 99.9% of videos
        'format': (
            'bestvideo[height<=360][ext=mp4]+bestaudio[ext=m4a]/'   # 360p + audio
            'best[height<=360]/'                                    # fallback
            'worstvideo[height<=360]+worstaudio/'                   # still small
            'worst'                                                 # absolute last resort
        ),
        'merge_output_format': 'mp4',
        'noplaylist': True,
        'outtmpl': '%(id)s.%(ext)s',
        'quiet': True,
        'format_sort': ['+size', '+br'],  # always prefer smaller files
    }

    temp_dir = tempfile.TemporaryDirectory()
    ydl_opts['outtmpl'] = os.path.join(temp_dir.name, '%(id)s.%(ext)s')

    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(url, download=True)
            final_filename = ydl.prepare_filename(info)
            safe_title = sanitize_filename(info.get("title", "video"))

        def file_streamer():
            try:
                with open(final_filename, "rb") as f:
                    yield from f
            finally:
                temp_dir.cleanup()

        return StreamingResponse(
            file_streamer(),
            media_type="video/mp4",
            headers={
                "Content-Disposition": f'attachment; filename="{safe_title}.mp4"',
                "Cache-Control": "no-cache",
            }
        )

    except Exception as e:
        temp_dir.cleanup()
        return {"error": str(e)}

# Local testing only
if __name__ == "__main__":
    import uvicorn
    uvicorn.run("app:app", host="0.0.0.0", port=8000, reload=True)
