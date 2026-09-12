"""Look inside a video: real frames, real numbers.

A model cannot watch a file. These tools pull representative frames out with ffmpeg, hand them
to the vision pipeline, and report what ffprobe actually measured, so a clip is judged on its
content and its technical quality instead of on the fact that a render finished.
"""

import json
import time
from pathlib import Path

from .agent import STRING, safe_path, tool
from .media import VIDEO_SUFFIXES, MediaUnavailable, UnsupportedMedia

INDEX_NAME = "videos.json"
MAX_INDEX = 60
MAX_SEGMENTS = 24
VOICE_LEVEL = 1.0      # the narration stays at its own level
AMBIENCE_LEVEL = 0.25  # the clip's own audio drops under the voice instead of fighting it


class VideoTools:
    def __init__(self, agent, media):
        self.agent, self.media = agent, media

    # ------------------------------------------------------------------ extension protocol
    def tools(self, session):
        if not self.media.tool("ffmpeg"):
            return []
        return [
            tool("video_inspect",
                 "Watch a workspace video: extracts representative frames (scene cuts, else evenly "
                 "spaced), delivers them as vision input and reports resolution, fps, bitrate, codec, "
                 "duration and whether there is audio. Use it before judging a rendered clip.",
                 {"path": STRING, "frames": {"type": "integer", "minimum": 1, "maximum": 12}}, ["path"]),
            tool("video_assemble",
                 "Join rendered segments into one film and lay a voice track over them. Segments are "
                 "concatenated in the given order with an optional crossfade; the generated ambience is "
                 "kept but ducked under the narration. This runs locally with ffmpeg, not on the farm.",
                 {"clips": {"type": "array", "items": STRING}, "output": STRING, "voice": STRING,
                  "crossfade": {"type": "number", "minimum": 0, "maximum": 2}},
                 ["clips", "output"]),
            tool("video_index",
                 "Probe every video in the workspace and write videos.json with duration, size, codec, "
                 "fps, bitrate and the keyframes already extracted for each one.",
                 {"folder": STRING}, []),
        ]

    async def execute(self, session, name, args):
        if name == "video_inspect":
            return await self.inspect(session, str(args["path"]), int(args.get("frames") or 6))
        if name == "video_assemble":
            return await self.assemble(session, list(args.get("clips") or []), str(args["output"]),
                                       str(args.get("voice") or ""), float(args.get("crossfade") or 0))
        if name == "video_index":
            return await self.index(session, str(args.get("folder") or "."))
        raise ValueError("Unknown video tool")

    def brief(self, session):
        return ("Videos are inspectable and editable locally: video_inspect pulls real frames into "
                "vision, video_index writes videos.json, video_assemble joins segments and lays a "
                "voice over them with ffmpeg (no farm time).")

    # ------------------------------------------------------------------ work
    async def describe(self, session, file: Path):
        data = await self.media.probe(file)
        quality = self.media.quality(data)
        return {"bytes": file.stat().st_size,
                "seconds": round(self.media.duration(data), 2),
                **quality}

    async def inspect(self, session, path, frames):
        sid = session["id"]
        root = self.agent.workspace(sid)
        file = safe_path(root, path)
        if not file.is_file():
            raise ValueError("Файл не найден: " + path)
        if file.suffix.lower() not in VIDEO_SUFFIXES:
            raise UnsupportedMedia("Это не видеофайл: " + path)
        report = await self.describe(session, file)
        try:
            extracted = await self.media.keyframes(file, frames)
        except MediaUnavailable as exc:
            return report | {"frames": [], "error": str(exc)}
        folder = safe_path(root, "frames")
        folder.mkdir(parents=True, exist_ok=True)
        delivered, failures = [], []
        for index, frame in enumerate(extracted, 1):
            target = folder / f"{file.stem[:40]}-{index:02d}.jpg"
            target.write_bytes(frame.read_bytes())
            relative = target.relative_to(root).as_posix()
            try:
                self.agent.queue_image(session, target)
                delivered.append(relative)
            except ValueError as exc:  # a non-vision model still gets the files
                failures.append(str(exc))
        self.agent.store.event(sid, "notice", {
            "text": f"Видео {path}: {report['width']}x{report['height']}, {report['seconds']} с, "
                    f"кадров извлечено {len(delivered)}"})
        result = report | {"path": path, "frames": delivered, "frames_delivered": bool(delivered),
                           "note": "Кадры приложены как изображения: опиши, что на них происходит, "
                                   "и оцени качество по числам выше."}
        if failures:
            result["vision"] = failures[0]
        return result

    async def assemble(self, session, clips, output, voice, crossfade):
        """Concatenate segments and mix narration over the generated ambience."""
        sid = session["id"]
        root = self.agent.workspace(sid)
        program = self.media.tool("ffmpeg")
        if not program:
            raise MediaUnavailable("ffmpeg is required to assemble video")
        if not clips:
            raise ValueError("Укажите хотя бы один сегмент")
        if len(clips) > MAX_SEGMENTS:
            raise ValueError(f"За один раз склеивается не больше {MAX_SEGMENTS} сегментов")
        files = []
        for clip in clips:
            file = safe_path(root, str(clip))
            if not file.is_file():
                raise ValueError("Сегмент не найден: " + str(clip))
            files.append(file)
        target = safe_path(root, output)
        target.parent.mkdir(parents=True, exist_ok=True)
        voice_file = safe_path(root, voice) if voice else None
        if voice_file and not voice_file.is_file():
            raise ValueError("Файл озвучки не найден: " + voice)

        probes = [await self.media.probe(file) for file in files]
        have_audio = [any(s.get("codec_type") == "audio" for s in (data.get("streams") or []))
                      for data in probes]
        width = max((self.media.quality(data)["width"] for data in probes), default=0) or 512
        height = max((self.media.quality(data)["height"] for data in probes), default=0) or 288
        seconds = sum(self.media.duration(data) for data in probes)

        args = ["-hide_banner", "-loglevel", "error"]
        for file in files:
            args += ["-i", str(file)]
        if voice_file:
            args += ["-i", str(voice_file)]

        steps, video_labels, audio_labels = [], [], []
        for index in range(len(files)):
            steps.append(f"[{index}:v]scale={width}:{height}:force_original_aspect_ratio=decrease,"
                         f"pad={width}:{height}:(ow-iw)/2:(oh-ih)/2,setsar=1,fps=25[v{index}]")
            video_labels.append(f"[v{index}]")
            if have_audio[index]:
                audio_labels.append(f"[{index}:a]")
            else:
                steps.append(f"anullsrc=channel_layout=stereo:sample_rate=48000,"
                             f"atrim=duration={max(self.media.duration(probes[index]), 0.04):.3f}[a{index}]")
                audio_labels.append(f"[a{index}]")
        pairs = "".join(v + a for v, a in zip(video_labels, audio_labels, strict=True))
        steps.append(f"{pairs}concat=n={len(files)}:v=1:a=1[cv][ca]")
        if voice_file:
            steps.append(f"[ca]volume={AMBIENCE_LEVEL}[bed]")
            steps.append(f"[{len(files)}:a]volume={VOICE_LEVEL}[voice]")
            steps.append("[bed][voice]amix=inputs=2:duration=first:dropout_transition=0:normalize=0[mix]")
            audio_out = "[mix]"
        else:
            audio_out = "[ca]"
        args += ["-filter_complex", ";".join(steps), "-map", "[cv]", "-map", audio_out,
                 "-c:v", "libx264", "-pix_fmt", "yuv420p", "-crf", "20", "-preset", "medium",
                 "-c:a", "aac", "-b:a", "160k", "-movflags", "+faststart", str(target), "-y"]
        await self.media.run(program, args)

        report = await self.describe(session, target)
        relative = target.relative_to(root).as_posix()
        self.agent.store.event(sid, "media", {"path": relative, "kind": "video", "direction": "generated",
                                              "prompt": f"assembled from {len(files)} segment(s)"})
        return {"path": relative, "segments": len(files), "voice": bool(voice_file),
                "expected_seconds": round(seconds, 2), **report,
                "note": "Смонтировано локально ffmpeg; время фермы не потрачено."}

    async def index(self, session, folder):
        sid = session["id"]
        root = self.agent.workspace(sid)
        base = safe_path(root, folder)
        if not base.is_dir():
            raise ValueError("Папка не найдена: " + folder)
        videos = sorted((p for p in base.rglob("*") if p.suffix.lower() in VIDEO_SUFFIXES and p.is_file()),
                        key=lambda p: p.stat().st_mtime, reverse=True)[:MAX_INDEX]
        entries = []
        for file in videos:
            try:
                report = await self.describe(session, file)
            except (MediaUnavailable, OSError) as exc:
                entries.append({"path": file.relative_to(root).as_posix(), "error": str(exc)[:200]})
                continue
            entries.append({"path": file.relative_to(root).as_posix(),
                            "modified": round(file.stat().st_mtime, 3), **report})
        payload = {"generated": round(time.time(), 3), "folder": folder, "count": len(entries),
                   "videos": entries}
        target = safe_path(root, INDEX_NAME)
        target.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        self.agent.store.event(sid, "notice", {"text": f"{INDEX_NAME}: описано видеофайлов — {len(entries)}"})
        return {"index": INDEX_NAME, "count": len(entries),
                "videos": [{k: entry.get(k) for k in ("path", "seconds", "width", "height", "verdict")}
                           for entry in entries[:12]]}
