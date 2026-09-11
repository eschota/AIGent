"""Interactive media previews: thumbnails, transcoding, caching and graceful degradation."""

import asyncio
import os
import shutil

from connector import media
import subprocess
import threading
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from connector.app import create_app
from connector.config import Config, password_hash
from connector.media import MediaService

FFMPEG = shutil.which("ffmpeg")
needs_ffmpeg = pytest.mark.skipif(not FFMPEG, reason="ffmpeg is not installed on this machine")


@pytest.fixture
def web(tmp_path):
    app = create_app(tmp_path / "runtime", polling=False)
    app.state.config.values.update(admin_password=password_hash("admin-password-1"),
                                   chat_password=password_hash("chat-pass"))
    session = app.state.store.resolve(0, 0, 0, "Media", new=True)
    workspace = app.state.agent.workspace(session["id"])
    with TestClient(app) as client:
        client.post("/api/login", json={"password": "admin-password-1"})
        client.headers["X-Requested-With"] = "DeepSeekIDE"
        yield app, client, session["id"], workspace


def make_image(folder: Path, name="frame.png", size=(900, 600)):
    from PIL import Image
    path = folder / name
    Image.new("RGB", size, (40, 90, 160)).save(path)
    return path.name


def make_video(folder: Path, name="clip.mp4", extra=()):
    path = folder / name
    subprocess.run([FFMPEG, "-hide_banner", "-loglevel", "error", "-y", "-f", "lavfi",
                    "-i", "testsrc=duration=1:size=64x64:rate=10", *extra, str(path)], check=True)
    return path.name


def wait_ready(client, sid, name, variant="preview", timeout=90):
    """Poll a variant the way the interface does while a transcode is running."""
    deadline, saw_processing = time.time() + timeout, False
    while time.time() < deadline:
        response = client.get(f"/api/sessions/{sid}/media", params={"path": name, "variant": variant})
        if response.status_code == 202:
            saw_processing = True
            assert response.json()["status"] == "processing"
            time.sleep(0.2)
            continue
        return response, saw_processing
    raise AssertionError("variant never became ready")


def test_image_thumb_and_preview_are_served_and_cached(web):
    app, client, sid, workspace = web
    name = make_image(workspace)
    thumb = client.get(f"/api/sessions/{sid}/media", params={"path": name, "variant": "thumb"})
    assert thumb.status_code == 200
    assert thumb.headers["content-type"] == "image/webp"
    assert "immutable" in thumb.headers["cache-control"]
    from PIL import Image
    import io
    with Image.open(io.BytesIO(thumb.content)) as decoded:
        assert max(decoded.size) == 320
    cache = app.state.config.root / "media-cache" / sid
    assert len(list(cache.iterdir())) == 1
    again = client.get(f"/api/sessions/{sid}/media", params={"path": name, "variant": "thumb"})
    assert again.content == thumb.content
    assert len(list(cache.iterdir())) == 1, "a cached thumbnail is not produced twice"
    preview = client.get(f"/api/sessions/{sid}/media", params={"path": name, "variant": "preview"})
    assert preview.status_code == 200
    with Image.open(io.BytesIO(preview.content)) as decoded:
        assert decoded.size == (900, 600), "an image smaller than the preview box is not upscaled"
    info = client.get(f"/api/sessions/{sid}/media/info", params={"path": name}).json()
    assert (info["kind"], info["width"], info["height"], info["format"]) == ("image", 900, 600, "PNG")


def test_unsupported_and_unsafe_paths_are_refused(web):
    app, client, sid, workspace = web
    (workspace / "notes.txt").write_text("hello", encoding="utf-8")
    assert client.get(f"/api/sessions/{sid}/media", params={"path": "notes.txt"}).status_code == 415
    assert client.get(f"/api/sessions/{sid}/media/info", params={"path": "notes.txt"}).status_code == 415
    escape = client.get(f"/api/sessions/{sid}/media", params={"path": "../../config.json", "variant": "original"})
    assert escape.status_code == 400
    assert client.get(f"/api/sessions/{sid}/media", params={"path": "gone.png"}).status_code == 404
    assert client.get(f"/api/sessions/{sid}/media",
                      params={"path": make_image(workspace), "variant": "huge"}).status_code == 400


def test_capabilities_without_ffmpeg_keep_image_previews(web, monkeypatch):
    app, client, sid, workspace = web
    monkeypatch.setattr(shutil, "which", lambda name: None)
    # A real install in a standard Windows location would otherwise be discovered anyway.
    monkeypatch.setattr(media, "WINDOWS_CANDIDATES", ())
    app.state.config.values["ffmpeg_path"] = ""
    capabilities = client.get("/api/media/capabilities").json()
    assert capabilities == {"ffmpeg": False, "ffprobe": False, "path": "", "images": True,
                            "variants": ["thumb", "preview", "poster", "original"],
                            "note": capabilities["note"]}
    assert "ffmpeg" in capabilities["note"]
    name = make_image(workspace, "still.png")
    assert client.get(f"/api/sessions/{sid}/media", params={"path": name, "variant": "thumb"}).status_code == 200
    info = client.get(f"/api/sessions/{sid}/media/info", params={"path": name}).json()
    assert info["width"] == 900


@needs_ffmpeg
def test_capabilities_report_the_installed_tools(web):
    app, client, sid, workspace = web
    capabilities = client.get("/api/media/capabilities").json()
    assert capabilities["ffmpeg"] and capabilities["ffprobe"] and capabilities["path"]


@needs_ffmpeg
def test_video_poster_thumb_info_and_original_range(web):
    app, client, sid, workspace = web
    name = make_video(workspace)
    poster, _ = wait_ready(client, sid, name, "poster")
    assert poster.status_code == 200 and poster.headers["content-type"] == "image/jpeg"
    thumb, _ = wait_ready(client, sid, name, "thumb")
    assert thumb.status_code == 200 and len(thumb.content) < len(poster.content) + 1

    info = client.get(f"/api/sessions/{sid}/media/info", params={"path": name}).json()
    assert info["kind"] == "video" and info["codec"] == "h264"
    assert info["width"] == 64 and info["height"] == 64
    assert 0.5 < info["duration"] < 2 and info["playable"] is True

    full = client.get(f"/api/sessions/{sid}/media", params={"path": name, "variant": "original"})
    assert full.status_code == 200 and full.headers["accept-ranges"] == "bytes"
    part = client.get(f"/api/sessions/{sid}/media", params={"path": name, "variant": "original"},
                      headers={"Range": "bytes=0-99"})
    assert part.status_code == 206 and len(part.content) == 100
    assert part.headers["content-range"].endswith("/" + str(len(full.content)))


@needs_ffmpeg
def test_ready_h264_is_reused_while_other_sources_are_transcoded(web):
    app, client, sid, workspace = web
    native = make_video(workspace, "native.mp4")
    response, processing = wait_ready(client, sid, native)
    assert processing, "the first preview request answers 202 so the interface can show a spinner"
    assert response.status_code == 200 and response.headers["content-type"] == "video/mp4"
    assert len(response.content) == (workspace / native).stat().st_size, "an h264 mp4 is served as is"

    other = make_video(workspace, "legacy.mp4", extra=("-c:v", "mpeg4"))
    response, _ = wait_ready(client, sid, other)
    assert response.status_code == 200
    assert response.content != (workspace / other).read_bytes(), "a non-h264 source is transcoded"
    cache = app.state.config.root / "media-cache" / sid
    assert [p for p in cache.iterdir() if p.suffix == ".mp4"], "the transcode is cached on disk"


@needs_ffmpeg
def test_cached_variants_start_no_new_ffmpeg_process(web, monkeypatch):
    app, client, sid, workspace = web
    name = make_video(workspace)
    wait_ready(client, sid, name, "thumb")
    wait_ready(client, sid, name, "preview")
    calls = []
    original = asyncio.create_subprocess_exec

    async def counted(program, *args, **kwargs):
        calls.append(program)
        return await original(program, *args, **kwargs)

    monkeypatch.setattr(asyncio, "create_subprocess_exec", counted)
    assert client.get(f"/api/sessions/{sid}/media", params={"path": name, "variant": "thumb"}).status_code == 200
    assert client.get(f"/api/sessions/{sid}/media", params={"path": name, "variant": "preview"}).status_code == 200
    assert calls == [], "a cached variant is served without running ffmpeg again"


@needs_ffmpeg
def test_a_slow_transcode_answers_202_until_it_finishes(web, monkeypatch):
    app, client, sid, workspace = web
    name = make_video(workspace, "slow.mp4", extra=("-c:v", "mpeg4"))
    service = app.state.agent.media
    gate = threading.Event()
    original = service.video_preview

    async def slow(file, target, marker):
        await asyncio.get_running_loop().run_in_executor(None, gate.wait)
        return await original(file, target, marker)

    monkeypatch.setattr(service, "video_preview", slow)
    for _ in range(3):
        response = client.get(f"/api/sessions/{sid}/media", params={"path": name, "variant": "preview"})
        assert response.status_code == 202 and response.json() == {"status": "processing", "variant": "preview"}
    # The interface polls with HEAD, so a pending transcode costs no download.
    probe = client.head(f"/api/sessions/{sid}/media", params={"path": name, "variant": "preview"})
    assert probe.status_code == 202 and not probe.content
    gate.set()
    response, _ = wait_ready(client, sid, name)
    assert response.status_code == 200


def test_cache_eviction_keeps_the_session_cache_under_the_limit(tmp_path):
    config = Config(tmp_path)
    config.values["media_cache_mb"] = 1
    service = MediaService(config, lambda sid: tmp_path)
    folder = service.folder("s1")
    for index in range(4):
        (folder / f"file{index}.bin").write_bytes(b"0" * 400_000)
        os.utime(folder / f"file{index}.bin", (time.time() + index, time.time() + index))
    service.evict(folder)
    remaining = sorted(p.name for p in folder.iterdir())
    assert sum(p.stat().st_size for p in folder.iterdir()) <= 1024 * 1024
    assert "file3.bin" in remaining and "file0.bin" not in remaining, "the oldest entries go first"
