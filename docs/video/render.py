"""Renders explainer.html to app/static/banksview-explainer.mp4 (+ poster). Needs Playwright with Chromium and ffmpeg."""
import pathlib, shutil, subprocess, sys, tempfile
from playwright.sync_api import sync_playwright

here = pathlib.Path(__file__).parent
out = here.parent.parent / "app" / "static"
tmp = pathlib.Path(tempfile.mkdtemp())
with sync_playwright() as p:
    b = p.chromium.launch()
    ctx = b.new_context(viewport={"width": 1280, "height": 720}, record_video_dir=str(tmp), record_video_size={"width": 1280, "height": 720})
    pg = ctx.new_page()
    pg.goto((here / "explainer.html").as_uri())
    pg.wait_for_function("window.__t > 31.5", timeout=60000)
    pg.screenshot(path=str(tmp / "poster.png"))
    pg.wait_for_function("window.__done === true", timeout=90000)
    ctx.close(); b.close()
webm = next(tmp.glob("*.webm"))
subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-i", str(webm), "-vf", "fps=30,format=yuv420p", "-c:v", "libx264", "-crf", "23", "-preset", "slow",
                "-movflags", "+faststart", "-an", str(out / "banksview-explainer.mp4")], check=True)
subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-i", str(tmp / "poster.png"), "-q:v", "4", str(out / "explainer-poster.jpg")], check=True)
print("ok", (out / "banksview-explainer.mp4").stat().st_size)
