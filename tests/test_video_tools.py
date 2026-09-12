"""A model cannot watch a file: these tests prove frames and numbers really reach it."""
import json
import shutil
import subprocess
from unittest.mock import AsyncMock

import pytest

from connector.agent import Agent
from connector.config import Config
from connector.media import MediaService
from connector.store import Store
from connector.video_tools import VideoTools

ffmpeg = shutil.which("ffmpeg")
needs_ffmpeg = pytest.mark.skipif(not ffmpeg, reason="ffmpeg is not installed on this machine")


@pytest.fixture
def bundle(tmp_path):
    config = Config(tmp_path)
    store = Store(config.root / "video.db")
    agent = Agent(config, store, AsyncMock(), AsyncMock())
    media = MediaService(config, agent.workspace)
    tools = VideoTools(agent, media)
    agent.extensions.append(tools)
    session = store.resolve(70, 0, 1)
    yield config, store, agent, tools, session
    store.db.close()


def make_video(path, seconds=2, size="320x240", rate=25):
    """Two coloured halves, so scene detection has something real to find."""
    subprocess.run([ffmpeg, "-hide_banner", "-loglevel", "error", "-y",
                    "-f", "lavfi", "-i", f"color=c=navy:s={size}:d={seconds / 2}:r={rate}",
                    "-f", "lavfi", "-i", f"color=c=orange:s={size}:d={seconds / 2}:r={rate}",
                    "-filter_complex", "[0:v][1:v]concat=n=2:v=1[v]", "-map", "[v]",
                    "-pix_fmt", "yuv420p", str(path)], check=True, timeout=120)
    return path


def test_video_tools_stay_hidden_without_ffmpeg(bundle, monkeypatch):
    _, _, _, tools, session = bundle
    monkeypatch.setattr(tools.media, "tool", lambda name: "")
    assert tools.tools(session) == [], "a tool that cannot run must not be advertised"


@needs_ffmpeg
async def test_inspect_delivers_real_frames_and_measured_quality(bundle):
    _, store, agent, tools, session = bundle
    workspace = agent.workspace(session["id"])
    make_video(workspace / "clip.mp4")

    result = await agent.execute(session, "video_inspect", {"path": "clip.mp4", "frames": 4})

    assert result["frames_delivered"] is True
    assert 1 <= len(result["frames"]) <= 4
    for relative in result["frames"]:
        assert (workspace / relative).is_file(), "each delivered frame exists in the workspace"
    assert (result["width"], result["height"]) == (320, 240)
    assert result["seconds"] == pytest.approx(2, abs=0.3)
    assert result["fps"] == 25
    assert result["codec"] == "h264"
    assert result["audio"] is False
    assert "no audio track" in result["verdict"] and "below SD" in result["verdict"]
    assert agent.pending_images.get(session["id"]), "frames are queued as vision input"
    assert any(e["kind"] == "media" for e in store.events(session["id"]))


@needs_ffmpeg
async def test_a_second_inspection_reuses_the_extracted_frames(bundle):
    _, _, agent, tools, session = bundle
    workspace = agent.workspace(session["id"])
    make_video(workspace / "clip.mp4")
    file = workspace / "clip.mp4"

    first = await tools.media.keyframes(file, 4)
    second = await tools.media.keyframes(file, 4)

    assert first == second and first, "extraction is cached by content key"
    assert all(frame.is_file() for frame in first)


@needs_ffmpeg
async def test_index_writes_a_json_catalogue_of_every_video(bundle):
    _, store, agent, tools, session = bundle
    workspace = agent.workspace(session["id"])
    make_video(workspace / "one.mp4")
    (workspace / "clips").mkdir()
    make_video(workspace / "clips" / "two.mp4", seconds=1, size="640x360")
    (workspace / "notes.txt").write_text("not a video", encoding="utf-8")

    result = await agent.execute(session, "video_index", {})

    assert result["count"] == 2
    catalogue = json.loads((workspace / "videos.json").read_text(encoding="utf-8"))
    by_path = {item["path"]: item for item in catalogue["videos"]}
    assert set(by_path) == {"one.mp4", "clips/two.mp4"}
    assert by_path["clips/two.mp4"]["width"] == 640
    assert by_path["one.mp4"]["seconds"] == pytest.approx(2, abs=0.3)
    assert all(item["bytes"] > 0 for item in by_path.values())
    assert catalogue["count"] == 2 and catalogue["folder"] == "."


async def test_inspect_refuses_a_file_that_is_not_a_video(bundle):
    _, _, agent, tools, session = bundle
    (agent.workspace(session["id"]) / "note.txt").write_text("text", encoding="utf-8")
    with pytest.raises(Exception) as error:
        await tools.inspect(session, "note.txt", 4)
    assert "не видеофайл" in str(error.value) or "not" in str(error.value).lower()

    with pytest.raises(ValueError):
        await tools.inspect(session, "missing.mp4", 4)


def test_quality_names_the_actual_defects():
    verdict = MediaService.quality({
        "streams": [{"codec_type": "video", "width": 384, "height": 224,
                     "avg_frame_rate": "12/1", "bit_rate": "9000", "codec_name": "h264"}],
        "format": {"bit_rate": "9000"}})
    assert verdict["fps"] == 12
    assert "below SD" in verdict["verdict"]
    assert "choppy" in verdict["verdict"]
    assert "low bitrate" in verdict["verdict"]
    assert "no audio track" in verdict["verdict"]

    clean = MediaService.quality({
        "streams": [{"codec_type": "video", "width": 1920, "height": 1080,
                     "avg_frame_rate": "30/1", "bit_rate": "8000000", "codec_name": "h264"},
                    {"codec_type": "audio", "codec_name": "aac"}],
        "format": {"bit_rate": "8000000"}})
    assert clean["verdict"] == "no obvious technical problems"
    assert clean["audio"] is True


@needs_ffmpeg
async def test_a_clip_without_scene_cuts_still_yields_frames(bundle):
    """A smooth render has no cuts: ffmpeg writes nothing and exits non-zero, and even spacing wins."""
    _, _, agent, tools, session = bundle
    workspace = agent.workspace(session["id"])
    smooth = workspace / "smooth.mp4"
    subprocess.run([ffmpeg, "-hide_banner", "-loglevel", "error", "-y", "-f", "lavfi",
                    "-i", "gradients=s=320x240:d=3:r=25", "-pix_fmt", "yuv420p", str(smooth)],
                   check=True, timeout=120)

    frames = await tools.media.keyframes(smooth, 5)

    assert len(frames) == 5, "the even-spacing fallback fills in when scene detection finds nothing"
    assert all(frame.stat().st_size > 0 for frame in frames)
    result = await agent.execute(session, "video_inspect", {"path": "smooth.mp4", "frames": 5})
    assert result["frames_delivered"] is True and "error" not in result
