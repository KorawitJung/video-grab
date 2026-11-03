# /// script
# requires-python = ">=3.10,<3.13"
# dependencies = ["yt-dlp", "faster-whisper"]
# ///
"""Download a video (or take a local file), grab frames, transcribe the audio."""
import argparse
import json
import re
import shutil
import subprocess
import sys
from pathlib import Path


def run(*cmd):
    return subprocess.run(cmd, check=True, capture_output=True, text=True, encoding="utf-8", errors="replace").stdout


def stamp(seconds):
    s = int(seconds)
    return f"{s // 3600:02d}:{s % 3600 // 60:02d}:{s % 60:02d}"


def slug(text):
    return re.sub(r'[<>:"/\\|?*\x00-\x1f\s]+', "-", text).strip("-.")[:60] or "video"


def download(url, out_root, cookies_browser):
    import yt_dlp

    opts = {
        "noplaylist": True,
        "quiet": True,
        "no_warnings": True,
        "noprogress": True,
        "format": "bv*[height<=1080]+ba/b[height<=1080]/b",
        "merge_output_format": "mp4",
    }
    if cookies_browser:
        opts["cookiesfrombrowser"] = (cookies_browser,)
    with yt_dlp.YoutubeDL(opts) as ydl:
        info = ydl.extract_info(url, download=False)
    folder = out_root / slug(f"{info.get('title') or 'video'}-{info['id']}")
    folder.mkdir(parents=True, exist_ok=True)
    opts["outtmpl"] = str(folder / "video.%(ext)s")
    with yt_dlp.YoutubeDL(opts) as ydl:
        ydl.download([url])
    video = next(p for p in folder.glob("video.*") if p.suffix not in (".part", ".ytdl"))
    meta = {k: info.get(k) for k in ("title", "uploader", "webpage_url", "upload_date")}
    return folder, video, meta


def probe(video):
    data = json.loads(run("ffprobe", "-v", "error", "-show_format", "-show_streams", "-of", "json", str(video)))
    has_audio = any(s["codec_type"] == "audio" for s in data["streams"])
    return float(data["format"]["duration"]), has_audio


def grab_frames(video, folder, duration, interval, max_frames):
    frames = folder / "frames"
    if frames.exists():
        shutil.rmtree(frames)
    frames.mkdir()
    interval = max(interval, duration / max_frames)
    run("ffmpeg", "-v", "error", "-y", "-i", str(video),
        "-vf", f"fps=1/{interval:.3f},scale='min(1280,iw)':-2",
        "-frames:v", str(max_frames), "-q:v", "3", str(frames / "frame_%04d.jpg"))
    return [(p, i * interval) for i, p in enumerate(sorted(frames.glob("*.jpg")))]


def transcribe(video, folder, model_name, language):
    import numpy as np
    from faster_whisper import WhisperModel

    # Decode with ffmpeg and pass samples, since faster-whisper's own decoder breaks on newer PyAV releases.
    pcm = folder / "audio.pcm"
    run("ffmpeg", "-v", "error", "-y", "-i", str(video), "-vn", "-ac", "1", "-ar", "16000", "-f", "f32le", str(pcm))
    audio = np.fromfile(pcm, dtype=np.float32)
    pcm.unlink()
    model = WhisperModel(model_name, device="cpu", compute_type="int8")
    segments, info = model.transcribe(audio, language=language, vad_filter=True)
    rows = [{"start": s.start, "end": s.end, "text": s.text.strip()} for s in segments]
    (folder / "transcript.json").write_text(
        json.dumps({"language": info.language, "segments": rows}, ensure_ascii=False, indent=2), encoding="utf-8")
    return info.language, rows


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("source", help="video URL or local file path")
    parser.add_argument("--out", default="video-grabs", help="parent output folder")
    parser.add_argument("--interval", type=float, default=2.0, help="seconds between frames")
    parser.add_argument("--max-frames", type=int, default=60, help="widens the interval to stay under this")
    parser.add_argument("--model", default="small", help="Whisper model: tiny, base, small, medium, large-v3")
    parser.add_argument("--language", help="language code such as th or en; auto-detected if omitted")
    parser.add_argument("--cookies-browser", help="read login cookies from this browser, such as chrome")
    args = parser.parse_args()
    sys.stdout.reconfigure(encoding="utf-8")

    out_root = Path(args.out).resolve()
    local = Path(args.source)
    if local.is_file():
        folder = out_root / slug(local.stem)
        folder.mkdir(parents=True, exist_ok=True)
        video, meta = local.resolve(), {"title": local.stem, "webpage_url": str(local.resolve())}
    else:
        print("Downloading...", flush=True)
        folder, video, meta = download(args.source, out_root, args.cookies_browser)

    duration, has_audio = probe(video)
    print(f"Grabbing frames from {stamp(duration)} of video...", flush=True)
    frames = grab_frames(video, folder, duration, args.interval, args.max_frames)

    language, rows = None, []
    if has_audio:
        print(f"Transcribing with Whisper {args.model}...", flush=True)
        language, rows = transcribe(video, folder, args.model, args.language)

    lines = [f"# {meta.get('title')}", ""]
    lines += [f"- {k}: {v}" for k, v in meta.items() if v and k != "title"]
    lines += [f"- duration: {stamp(duration)}", f"- video: {video}", f"- language: {language or 'n/a'}", ""]
    lines += ["## Frames", ""] + [f"- {stamp(t)} frames/{p.name}" for p, t in frames]
    lines += ["", "## Transcript", ""]
    if rows:
        lines += [f"[{stamp(r['start'])}] {r['text']}" for r in rows]
    else:
        lines.append("(no audio track)" if not has_audio else "(no speech detected)")
    index = folder / "index.md"
    index.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"{len(frames)} frames, {len(rows)} transcript segments, language {language or 'n/a'}")
    print(f"INDEX: {index}")


if __name__ == "__main__":
    main()
