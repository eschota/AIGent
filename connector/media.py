"""Interactive media for the workspace: thumbnails, browser-ready previews and probe data.

Images always work through Pillow. Video and audio use ffmpeg/ffprobe when they are installed;
without them the original file is still served, so a browser still plays an H.264 MP4 natively.
Every derived file is cached under the private root and keyed by path, mtime and size, so a
cached answer can be served immutably and a changed source produces a new key.
"""

import asyncio
import hashlib
import json
import mimetypes
import os
import shutil
from pathlib import Path

from .agent import safe_path

IMAGE_SUFFIXES = {".png", ".jpg", ".jpeg", ".webp", ".gif", ".bmp", ".tif", ".tiff"}
VIDEO_SUFFIXES = {".mp4", ".m4v", ".mov", ".webm", ".mkv", ".avi", ".ogv"}
AUDIO_SUFFIXES = {".mp3", ".wav", ".ogg", ".oga", ".opus", ".m4a", ".aac", ".flac"}
VARIANTS = ("thumb", "preview", "poster", "original")
THUMB_SIZE, PREVIEW_SIZE, POSTER_SIZE = 320, 1600, 1280
WINDOWS_CANDIDATES = (
    r"%LOCALAPPDATA%\Microsoft\WinGet\Links\{name}.exe",
    r"%LOCALAPPDATA%\Programs\ffmpeg\bin\{name}.exe",
    r"C:\ffmpeg\bin\{name}.exe",
    r"%ProgramFiles%\ffmpeg\bin\{name}.exe",
)


class UnsupportedMedia(Exception):
    """The requested file is not an image, video or audio file."""


class MediaUnavailable(Exception):
    """The derived file cannot be produced right now (no ffmpeg, timeout or a broken source)."""


# The host registry decides what `mimetypes` knows, and a Windows box usually has no entry for
# .webp — the browser then downloads a thumbnail instead of showing it. Web media types are
# fixed here so a preview renders the same on every machine.
WEB_TYPES = {".webp": "image/webp", ".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg",
             ".gif": "image/gif", ".avif": "image/avif", ".bmp": "image/bmp", ".svg": "image/svg+xml",
             ".mp4": "video/mp4", ".webm": "video/webm", ".mov": "video/quicktime", ".mkv": "video/x-matroska",
             ".mp3": "audio/mpeg", ".m4a": "audio/mp4", ".ogg": "audio/ogg", ".wav": "audio/wav"}


def guess_mime(file: Path) -> str:
    return WEB_TYPES.get(file.suffix.lower()) or mimetypes.guess_type(file.name)[0] or "application/octet-stream"


class MediaService:
    def __init__(self, config, workspace):
        self.config, self.workspace = config, workspace
        self.jobs: dict[str, asyncio.Task] = {}
        self.errors: dict[str, str] = {}
        self.probes: dict[str, dict] = {}
        self.slots = asyncio.Semaphore(2)

    # ------------------------------------------------------------------ ffmpeg discovery
    def tool(self, name: str) -> str:
        configured = str(self.config.values.get("ffmpeg_path") or "").strip()
        if configured:
            candidate = Path(configured)
            if candidate.is_dir():
                candidate = candidate / (name + (".exe" if os.name == "nt" else ""))
            elif name not in candidate.name:
                candidate = candidate.with_name(candidate.name.replace("ffmpeg", name, 1))
            if candidate.is_file():
                return str(candidate)
        found = shutil.which(name)
        if found:
            return found
        for pattern in WINDOWS_CANDIDATES:
            expanded = os.path.expandvars(pattern.format(name=name))
            if "%" not in expanded and Path(expanded).is_file():
                return expanded
        return ""

    def capabilities(self) -> dict:
        ffmpeg, ffprobe = self.tool("ffmpeg"), self.tool("ffprobe")
        return {"ffmpeg": bool(ffmpeg), "ffprobe": bool(ffprobe), "path": ffmpeg or ffprobe,
                "images": True, "variants": list(VARIANTS),
                "note": "" if ffmpeg else "Установите ffmpeg (winget install Gyan.FFmpeg), "
                                          "чтобы получить обложки и перекодирование видео."}

    @property
    def timeout(self) -> float:
        return float(self.config.values.get("media_transcode_timeout_seconds") or 600)

    @property
    def cache_limit(self) -> int:
        return int(self.config.values.get("media_cache_mb") or 500) * 1024 * 1024

    # ------------------------------------------------------------------ paths and cache keys
    def source(self, sid: str, relative: str) -> Path:
        file = safe_path(self.workspace(sid), relative)
        if not file.is_file():
            raise FileNotFoundError("File not found")
        return file

    @staticmethod
    def category(file: Path) -> str:
        suffix = file.suffix.lower()
        for name, group in (("image", IMAGE_SUFFIXES), ("video", VIDEO_SUFFIXES), ("audio", AUDIO_SUFFIXES)):
            if suffix in group:
                return name
        raise UnsupportedMedia("Only image, video and audio files have previews")

    def folder(self, sid: str) -> Path:
        path = (self.config.root / "media-cache" / sid).resolve()
        path.mkdir(parents=True, exist_ok=True)
        return path

    @staticmethod
    def key(file: Path, variant: str) -> str:
        stat = file.stat()
        raw = f"{file}|{stat.st_mtime_ns}|{stat.st_size}|{variant}"
        return hashlib.sha256(raw.encode("utf-8", "surrogatepass")).hexdigest()

    def evict(self, folder: Path) -> None:
        files = [p for p in folder.iterdir() if p.is_file()]
        total = sum(p.stat().st_size for p in files)
        if total <= self.cache_limit:
            return
        for path in sorted(files, key=lambda p: p.stat().st_mtime):
            total -= path.stat().st_size
            path.unlink(missing_ok=True)
            if total <= self.cache_limit:
                break

    @staticmethod
    def verify_image(file: Path) -> None:
        from PIL import Image
        try:
            with Image.open(file) as decoded:
                decoded.verify()
        except Exception:
            raise UnsupportedMedia("Файл повреждён или не является поддерживаемым изображением") from None

    @staticmethod
    def ready(path: Path, variant: str) -> dict:
        return {"status": "ready", "path": path, "media_type": guess_mime(path), "variant": variant}

    # ------------------------------------------------------------------ subprocess plumbing
    async def run(self, program: str, args: list[str]) -> bytes:
        async with self.slots:
            process = await asyncio.create_subprocess_exec(
                program, *args, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
            try:
                out, err = await asyncio.wait_for(process.communicate(), self.timeout)
            except (TimeoutError, asyncio.TimeoutError):
                process.kill()
                await process.wait()
                raise MediaUnavailable(f"{Path(program).name} превысил лимит {self.timeout:.0f} с") from None
            if process.returncode:
                raise MediaUnavailable(err.decode("utf-8", "replace").strip()[-300:] or "ffmpeg failed")
            return out

    async def probe(self, file: Path) -> dict:
        """ffprobe JSON for one file; cached per content key, empty when ffprobe is absent."""
        key = self.key(file, "probe")
        if key in self.probes:
            return self.probes[key]
        program = self.tool("ffprobe")
        if not program:
            return {}
        try:
            raw = await self.run(program, ["-v", "error", "-print_format", "json",
                                           "-show_format", "-show_streams", str(file)])
            data = json.loads(raw.decode("utf-8", "replace") or "{}")
        except (MediaUnavailable, ValueError):
            data = {}
        self.probes[key] = data
        return data

    # ------------------------------------------------------------------ producers
    def image_variant(self, file: Path, target: Path, size: int) -> None:
        from PIL import Image, ImageOps
        try:
            with Image.open(file) as decoded:
                decoded.load()
                frame = ImageOps.exif_transpose(decoded)
                if frame.mode not in ("RGB", "RGBA"):
                    frame = frame.convert("RGBA" if "A" in frame.getbands() else "RGB")
                frame.thumbnail((size, size))
                temp = target.with_name("partial-" + target.name)
                frame.save(temp, "WEBP", quality=85 if size > THUMB_SIZE else 80, method=4)
                temp.replace(target)
        except UnsupportedMedia:
            raise
        except Exception:
            # A truncated or corrupted source is a normal outcome, not a server fault.
            raise UnsupportedMedia("Файл повреждён или не является поддерживаемым изображением") from None

    async def video_frame(self, file: Path, target: Path, width: int) -> None:
        program = self.tool("ffmpeg")
        if not program:
            raise MediaUnavailable("ffmpeg не найден: обложка видео недоступна")
        duration = self.duration(await self.probe(file))
        offset = min(1.0, duration * 0.1) if duration else 0.0
        temp = target.with_name("partial-" + target.name)
        await self.run(program, ["-hide_banner", "-y", "-ss", f"{offset:.3f}", "-i", str(file),
                                 "-frames:v", "1", "-vf", f"scale='min({width},iw)':-2",
                                 "-q:v", "3", str(temp)])
        temp.replace(target)

    async def video_preview(self, file: Path, target: Path, marker: Path) -> Path:
        data = await self.probe(file)
        if self.playable(file, data):
            marker.write_bytes(b"")
            return file
        program = self.tool("ffmpeg")
        if not program:
            return file
        temp = target.with_name("partial-" + target.name)
        await self.run(program, ["-hide_banner", "-y", "-i", str(file),
                                 "-vf", "scale=-2:'min(720,ih)'", "-c:v", "libx264", "-profile:v", "main",
                                 "-preset", "veryfast", "-crf", "26", "-pix_fmt", "yuv420p",
                                 "-c:a", "aac", "-b:a", "128k", "-movflags", "+faststart", str(temp)])
        temp.replace(target)
        return target

    async def keyframes(self, file: Path, count: int = 6, width: int = 640) -> list[Path]:
        """Representative frames of a video: scene changes when they exist, even spacing otherwise.

        Frames are cached next to the other derived files, so asking twice costs one extraction.
        """
        program = self.tool("ffmpeg")
        if not program:
            raise MediaUnavailable("ffmpeg is required to read frames out of a video")
        count = max(1, min(12, int(count)))
        folder = self.folder_for(file) / f"keyframes-{count}-{width}"
        existing = sorted(folder.glob("frame-*.jpg"))
        if existing:
            return existing
        folder.mkdir(parents=True, exist_ok=True)
        pattern = str(folder / "frame-%02d.jpg")
        # No -vsync/-fps_mode here: image2 output writes one file per selected frame, and the
        # flag was renamed in ffmpeg 7, so leaving it out keeps every build working.
        scene = ["-vf", f"select='gt(scene,0.25)',scale={width}:-2",
                 "-frames:v", str(count), "-q:v", "4", pattern]
        try:
            await self.run(program, ["-hide_banner", "-loglevel", "error", "-i", str(file), *scene, "-y"])
        except MediaUnavailable:
            # A smooth clip has no scene cuts at all: ffmpeg writes nothing and exits non-zero.
            pass
        frames = sorted(folder.glob("frame-*.jpg"))
        if len(frames) >= min(3, count):
            return frames[:count]
        # A steady shot has no scene cuts: fall back to evenly spaced samples.
        for frame in frames:
            frame.unlink(missing_ok=True)
        seconds = self.duration(await self.probe(file)) or 0
        rate = f"{count}/{max(seconds, 1):.3f}" if seconds else "1"
        await self.run(program, ["-hide_banner", "-loglevel", "error", "-i", str(file),
                                 "-vf", f"fps={rate},scale={width}:-2", "-frames:v", str(count),
                                 "-q:v", "4", pattern, "-y"])
        return sorted(folder.glob("frame-*.jpg"))[:count]

    def folder_for(self, file: Path) -> Path:
        """Cache directory shared by every derivative of one source file."""
        digest = hashlib.sha256(f"{file}:{file.stat().st_mtime_ns}:{file.stat().st_size}".encode()).hexdigest()[:16]
        folder = Path(self.config.root) / "media-cache" / "keyframes" / digest
        folder.mkdir(parents=True, exist_ok=True)
        return folder

    @staticmethod
    def quality(data: dict) -> dict:
        """Plain technical facts plus a blunt verdict, so nobody calls a 384x224 clip cinematic."""
        streams = data.get("streams") or []
        video = next((s for s in streams if s.get("codec_type") == "video"), {})
        audio = next((s for s in streams if s.get("codec_type") == "audio"), None)
        width, height = int(video.get("width") or 0), int(video.get("height") or 0)
        rate = video.get("avg_frame_rate") or "0/1"
        try:
            numerator, _, denominator = rate.partition("/")
            fps = round(float(numerator) / float(denominator or 1), 2)
        except (ValueError, ZeroDivisionError):
            fps = 0.0
        bitrate = int((data.get("format") or {}).get("bit_rate") or video.get("bit_rate") or 0)
        pixels = max(1, width * height)
        notes = []
        if height and height < 480:
            notes.append("below SD: acceptable as a draft, not as a deliverable")
        if fps and fps < 20:
            notes.append(f"{fps} fps looks choppy in motion")
        if bitrate and bitrate / pixels < 0.35:
            notes.append("low bitrate for this resolution; expect blocking on motion")
        if audio is None:
            notes.append("no audio track")
        return {"width": width, "height": height, "fps": fps, "codec": video.get("codec_name"),
                "bitrate": bitrate, "pixel_format": video.get("pix_fmt"), "audio": bool(audio),
                "verdict": "; ".join(notes) or "no obvious technical problems"}

    @staticmethod
    def duration(data: dict) -> float:
        try:
            return float(data.get("format", {}).get("duration") or 0)
        except (TypeError, ValueError):
            return 0.0

    @staticmethod
    def playable(file: Path, data: dict) -> bool:
        """True when the browser can stream the original: H.264 video, AAC or no audio, at most 720p."""
        if file.suffix.lower() not in (".mp4", ".m4v") or not data:
            return False
        video = [s for s in data.get("streams", []) if s.get("codec_type") == "video"]
        audio = [s for s in data.get("streams", []) if s.get("codec_type") == "audio"]
        if len(video) != 1 or video[0].get("codec_name") != "h264":
            return False
        if (video[0].get("height") or 0) > 720:
            return False
        return all(s.get("codec_name") in ("aac", "mp3") for s in audio)

    # ------------------------------------------------------------------ public entry points
    async def render(self, sid: str, relative: str, variant: str = "thumb") -> dict:
        if variant not in VARIANTS:
            raise ValueError("Unknown variant")
        file = self.source(sid, relative)
        kind = self.category(file)
        if variant == "original" and kind == "image":
            # The interface shows the file as an image, so a damaged one is refused, not streamed.
            self.verify_image(file)
        if variant == "original" or kind == "audio":
            return self.ready(file, "original")
        if kind == "image":
            if variant == "poster":
                variant = "preview"
            folder = self.folder(sid)
            target = folder / (self.key(file, variant) + ".webp")
            if target.exists():
                os.utime(target, None)
                return self.ready(target, variant)
            if file.suffix.lower() == ".gif" and variant == "preview":
                return self.ready(file, "original")  # keep the animation intact
            await asyncio.to_thread(self.image_variant, file, target,
                                    THUMB_SIZE if variant == "thumb" else PREVIEW_SIZE)
            self.evict(folder)
            return self.ready(target, variant)
        return await self.video(sid, file, variant)

    async def video(self, sid: str, file: Path, variant: str) -> dict:
        folder = self.folder(sid)
        key = self.key(file, variant)
        target = folder / (key + (".mp4" if variant == "preview" else ".jpg"))
        marker = folder / (key + ".original")
        if marker.exists():
            return self.ready(file, "original")
        if target.exists():
            os.utime(target, None)
            return self.ready(target, variant)
        task = self.jobs.get(key)
        if task is None:
            if key in self.errors:
                raise MediaUnavailable(self.errors[key])
            task = asyncio.ensure_future(self.produce(key, folder, file, target, marker, variant))
            self.jobs[key] = task
        if variant == "preview":
            if not task.done():
                return {"status": "processing", "variant": variant, "path": None,
                        "media_type": "video/mp4"}
            produced = task.result() if not task.exception() else None
            if produced is None:
                raise MediaUnavailable(self.errors.get(key, "Перекодирование не удалось"))
            return self.ready(produced, variant)
        produced = await task
        if produced is None:
            raise MediaUnavailable(self.errors.get(key, "Кадр видео недоступен"))
        return self.ready(produced, variant)

    async def produce(self, key, folder, file, target, marker, variant):
        try:
            if variant == "preview":
                produced = await self.video_preview(file, target, marker)
            else:
                await self.video_frame(file, target, POSTER_SIZE if variant == "poster" else THUMB_SIZE)
                produced = target
            self.errors.pop(key, None)
            self.evict(folder)
            return produced
        except (MediaUnavailable, OSError) as exc:
            self.errors[key] = str(exc)
            return None
        finally:
            self.jobs.pop(key, None)

    async def info(self, sid: str, relative: str) -> dict:
        file = self.source(sid, relative)
        kind = self.category(file)
        stat = file.stat()
        result = {"path": relative, "kind": kind, "bytes": stat.st_size, "mime": guess_mime(file),
                  "width": None, "height": None, "duration": None, "codec": None, "format": None,
                  "modified": stat.st_mtime}
        if kind == "image":
            from PIL import Image
            with Image.open(file) as decoded:
                result.update(width=decoded.width, height=decoded.height, format=decoded.format,
                              codec=(decoded.format or "").lower(), frames=getattr(decoded, "n_frames", 1))
            return result
        data = await self.probe(file)
        if not data:
            result["note"] = "ffprobe недоступен: длительность и разрешение неизвестны"
            return result
        streams = data.get("streams", [])
        video = next((s for s in streams if s.get("codec_type") == "video"), None)
        audio = next((s for s in streams if s.get("codec_type") == "audio"), None)
        result.update(duration=self.duration(data) or None,
                      format=(data.get("format", {}).get("format_name") or "").split(",")[0],
                      codec=(video or audio or {}).get("codec_name"),
                      audio_codec=(audio or {}).get("codec_name"),
                      playable=self.playable(file, data))
        if video:
            result.update(width=video.get("width"), height=video.get("height"))
        return result
