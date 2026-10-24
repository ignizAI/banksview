"""Builds the narration + soft music bed for the explainer and muxes it into app/static/banksview-explainer.mp4.
Needs: piper-tts and an English voice model (en-us-lessac-medium), numpy, ffmpeg.  Usage: python narrate.py MODEL.onnx"""
import subprocess, sys, wave, pathlib, tempfile
import numpy as np
from piper import PiperVoice

model = sys.argv[1]
here = pathlib.Path(__file__).parent
static = here.parent.parent / "app" / "static"
video = static / "banksview-explainer.mp4"
tmp = pathlib.Path(tempfile.mkdtemp())
T = [0.4, 6.4, 12.9, 19.4, 26.4, 33.4, 39.9]          # narration start per scene (scene starts + 0.4s)
LINES = [
    "Reading bank statements by hand is slow, and easy to get wrong.",
    "With BanksView, sign in with your email, upload the PDF, and add its password.",
    "Thirteen checks run first, so a bad file is caught before you pay anything.",
    "Then every transaction is read and reconciled, and one hundred and forty one metrics are computed.",
    "You get a risk score, flags, a dashboard, and an Excel report with eight sheets.",
    "Fifty rupees per finished report. Nothing is charged if the upload fails.",
    "BanksView. Bank statement analysis for lending teams.",
]
voice = PiperVoice.load(model)
dur = float(subprocess.check_output(["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", str(video)]))
sr = voice.config.sample_rate
total = int((dur + 0.5) * sr)
mix = np.zeros(total, dtype=np.float32)
for i, (t, line) in enumerate(zip(T, LINES)):
    wav = tmp / f"l{i}.wav"
    with wave.open(str(wav), "wb") as w:
        voice.synthesize_wav(line, w)
    with wave.open(str(wav)) as w:
        a = np.frombuffer(w.readframes(w.getnframes()), dtype=np.int16).astype(np.float32) / 32768
    start = int(t * sr)
    mix[start:start + len(a)] += a[: total - start]
    nxt = (T[i + 1] if i + 1 < len(T) else dur)
    print(f"line {i}: {len(a)/sr:.1f}s in a {nxt - t:.1f}s slot")
    assert len(a) / sr < nxt - t - 0.2, "narration longer than its scene"
mix *= 0.9 / max(1e-6, np.abs(mix).max())
# music bed: warm chord pad (A major add9) with slow swell and light chimes on scene changes, kept well under the voice
t = np.arange(total) / sr
pad = sum(np.sin(2 * np.pi * f * t) * a for f, a in [(110, .5), (220, .35), (277.18, .25), (329.63, .25), (415.3, .12), (493.88, .1)]).astype(np.float32)
pad *= (0.75 + 0.25 * np.sin(2 * np.pi * t / 8))
for s in [0, 6, 12.5, 19, 26, 33, 39.5]:
    k = int(s * sr); n = int(1.6 * sr)
    if k + n < total:
        tt = np.arange(n) / sr
        pad[k:k + n] += (np.sin(2 * np.pi * 880 * tt) * np.exp(-3 * tt) * .5 + np.sin(2 * np.pi * 1318.5 * tt) * np.exp(-4 * tt) * .25).astype(np.float32)
env = np.minimum(1, np.minimum(t / 2, (dur - t) / 2)).clip(0, 1)
pad = pad / np.abs(pad).max() * env
out = mix + pad * 0.09
out = np.clip(out, -1, 1)
final = tmp / "mix.wav"
with wave.open(str(final), "wb") as w:
    w.setnchannels(1); w.setsampwidth(2); w.setframerate(sr); w.writeframes((out * 32767).astype(np.int16).tobytes())
silent = tmp / "silent.mp4"
subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-i", str(video), "-an", "-c:v", "copy", str(silent)], check=True)
subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-i", str(silent), "-i", str(final), "-c:v", "copy", "-c:a", "aac", "-b:a", "128k", "-ar", "44100",
                "-af", "loudnorm=I=-18:TP=-2", "-shortest", "-movflags", "+faststart", str(video)], check=True)
print("muxed", video.stat().st_size)
