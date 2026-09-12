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
            tool("video_index",
                 "Probe every video in the workspace and write videos.json with duration, size, codec, "
                 "fps, bitrate and the keyframes already extracted for each one.",
                 {"folder": STRING}, []),
        ]

    async def execute(self, session, name, args):
        if name == "video_inspect":
            return await self.inspect(session, str(args["path"]), int(args.get("frames") or 6))
        if name == "video_index":
            return await self.index(session, str(args.get("folder") or "."))
        raise ValueError("Unknown video tool")

    def brief(self, session):
        return ("Videos are inspectable: video_inspect pulls real frames into vision, "
                "video_index writes videos.json for the whole workspace.")

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
