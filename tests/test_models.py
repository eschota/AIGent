"""3D model registry: the pure-python GLB reader, provenance sidecars and the graphics preset.

The GLB used here is assembled byte by byte in the test, so the parser is checked against a real
container and not against a fixture nobody can inspect.
"""

import json
import shutil
import struct
import subprocess
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from connector.app import create_app
from connector.config import Config, password_hash
from connector.models import ModelError, ModelRegistry, gltf_stats


def glb_bytes(document: dict, binary: bytes = b"\x00" * 4) -> bytes:
    payload = json.dumps(document).encode("utf-8")
    payload += b" " * (-len(payload) % 4)
    binary += b"\x00" * (-len(binary) % 4)
    chunks = struct.pack("<II", len(payload), 0x4E4F534A) + payload
    chunks += struct.pack("<II", len(binary), 0x004E4942) + binary
    return struct.pack("<III", 0x46546C67, 2, 12 + len(chunks)) + chunks


def triangle_document(triangles: int = 1) -> dict:
    return {
        "asset": {"version": "2.0", "generator": "aigent-test"},
        "scene": 0, "scenes": [{"nodes": [0]}], "nodes": [{"mesh": 0}],
        "meshes": [{"primitives": [{"attributes": {"POSITION": 0}, "indices": 1, "material": 0}]}],
        "materials": [{"pbrMetallicRoughness": {"baseColorTexture": {"index": 0}}}],
        "textures": [{"source": 0}], "images": [{"uri": "albedo.png"}],
        "animations": [{"channels": [], "samplers": []}],
        "accessors": [
            {"bufferView": 0, "componentType": 5126, "count": 3 * triangles, "type": "VEC3",
             "min": [-1.0, 0.0, -0.5], "max": [1.0, 2.0, 0.5]},
            {"bufferView": 1, "componentType": 5123, "count": 3 * triangles, "type": "SCALAR"},
        ],
        "bufferViews": [{"buffer": 0, "byteOffset": 0, "byteLength": 36 * triangles},
                        {"buffer": 0, "byteOffset": 36 * triangles, "byteLength": 6 * triangles}],
        "buffers": [{"byteLength": 42 * triangles}],
    }


@pytest.fixture
def web(tmp_path):
    app = create_app(tmp_path / "runtime", polling=False)
    app.state.config.values.update(admin_password=password_hash("admin-password-1"),
                                   chat_password=password_hash("chat-pass"), allow_web=False)
    app.state.models3d.preset_cache = None
    session = app.state.store.resolve(0, 0, 0, "Models", new=True)
    workspace = app.state.agent.workspace(session["id"])
    with TestClient(app) as client:
        client.post("/api/login", json={"password": "admin-password-1"})
        client.headers["X-Requested-With"] = "DeepSeekIDE"
        yield app, client, session["id"], workspace


# ---------------------------------------------------------------------------- parser
def test_glb_parser_counts_geometry_and_bounds(tmp_path):
    model = tmp_path / "chair.glb"
    model.write_bytes(glb_bytes(triangle_document(triangles=4)))
    stats = gltf_stats(model)
    assert stats["meshes"] == 1 and stats["primitives"] == 1
    assert stats["triangles"] == 4, "indices count / 3 is the triangle count of a TRIANGLES primitive"
    assert stats["materials"] == 1 and stats["textures"] == 1 and stats["animations"] == 1
    assert stats["bounds"] == {"min": [-1.0, 0.0, -0.5], "max": [1.0, 2.0, 0.5], "size": [2.0, 2.0, 1.0]}
    assert stats["generator"] == "aigent-test" and stats["draco"] is False


def test_gltf_json_file_and_broken_containers(tmp_path):
    text_model = tmp_path / "chair.gltf"
    text_model.write_text(json.dumps(triangle_document()), encoding="utf-8")
    assert gltf_stats(text_model)["triangles"] == 1

    without_indices = triangle_document()
    without_indices["meshes"][0]["primitives"][0].pop("indices")
    loose = tmp_path / "loose.glb"
    loose.write_bytes(glb_bytes(without_indices))
    assert gltf_stats(loose)["triangles"] == 1, "without indices the POSITION count decides"

    broken = tmp_path / "broken.glb"
    broken.write_bytes(b"NOPE" + b"\x00" * 40)
    with pytest.raises(ModelError):
        gltf_stats(broken)
    short = tmp_path / "short.glb"
    short.write_bytes(b"glTF")
    with pytest.raises(ModelError):
        gltf_stats(short)


# ---------------------------------------------------------------------------- sidecars
def test_sidecar_is_created_updated_and_keeps_history(web):
    app, client, sid, workspace = web
    (workspace / "chair.glb").write_bytes(glb_bytes(triangle_document()))
    listed = client.get(f"/api/sessions/{sid}/models").json()
    assert [item["path"] for item in listed] == ["chair.glb"]
    assert listed[0]["viewable"] is True and listed[0]["stats"]["triangles"] == 1

    sidecar = workspace / "chair.glb.json"
    assert sidecar.is_file(), "listing creates the missing sidecar next to the model"
    data = json.loads(sidecar.read_text(encoding="utf-8"))
    assert data["schema"] == "aigent.model/1" and data["format"] == "glb"
    assert data["source"]["tool"] == "unknown" and len(data["sha256"]) == 64

    updated = client.post(f"/api/sessions/{sid}/models/sidecar", json={
        "path": "chair.glb", "patch": {"prompt": "wooden chair", "seed": 42, "tags": ["prop"],
                                       "params": {"steps": 30}}}).json()
    assert updated["prompt"] == "wooden chair" and updated["seed"] == "42"
    assert updated["tags"] == ["prop"] and updated["params"] == {"steps": 30}
    assert [entry["event"] for entry in updated["history"]] == ["created", "updated"]

    refused = client.post(f"/api/sessions/{sid}/models/sidecar",
                          json={"path": "chair.glb", "patch": {"stats": {"triangles": 999}}})
    assert refused.status_code == 400 and "stats" in refused.json()["detail"]


def test_references_are_workspace_paths_and_validated(web):
    app, client, sid, workspace = web
    (workspace / "chair.glb").write_bytes(glb_bytes(triangle_document()))
    (workspace / "ref.png").write_bytes(b"\x89PNG\r\n\x1a\n" + b"0" * 32)
    (workspace / "notes.txt").write_text("not an image", encoding="utf-8")

    data = client.post(f"/api/sessions/{sid}/models/reference",
                       json={"path": "chair.glb", "image": "ref.png", "note": "оригинал"}).json()
    assert data["references"] == [{"path": "ref.png", "kind": "image", "note": "оригинал",
                                   "sha256": data["references"][0]["sha256"]}]
    again = client.post(f"/api/sessions/{sid}/models/reference",
                        json={"path": "chair.glb", "image": "ref.png", "note": "снова"}).json()
    assert len(again["references"]) == 1, "the same image is replaced, not duplicated"

    assert client.post(f"/api/sessions/{sid}/models/reference",
                       json={"path": "chair.glb", "image": "notes.txt"}).status_code == 400
    escape = client.post(f"/api/sessions/{sid}/models/reference",
                         json={"path": "chair.glb", "image": "../../config.json"})
    assert escape.status_code == 400
    missing = client.get(f"/api/sessions/{sid}/models/sidecar", params={"path": "ghost.glb"})
    assert missing.status_code == 404


def test_listing_marks_formats_without_a_viewer_and_survives_a_rebuild(web):
    app, client, sid, workspace = web
    (workspace / "chair.glb").write_bytes(glb_bytes(triangle_document()))
    (workspace / "rig.fbx").write_bytes(b"Kaydara FBX Binary" + b"\x00" * 40)
    (workspace / "notes.txt").write_text("ignored", encoding="utf-8")
    listed = {item["path"]: item for item in client.get(f"/api/sessions/{sid}/models").json()}
    assert set(listed) == {"chair.glb", "rig.fbx"}
    assert listed["rig.fbx"]["viewable"] is False and "glb" in listed["rig.fbx"]["note"]

    client.post(f"/api/sessions/{sid}/models/sidecar",
                json={"path": "chair.glb", "patch": {"prompt": "keep me"}})
    (workspace / "chair.glb").write_bytes(glb_bytes(triangle_document(triangles=7)))
    data = client.get(f"/api/sessions/{sid}/models/sidecar", params={"path": "chair.glb"}).json()
    assert data["prompt"] == "keep me", "regenerating the model keeps the provenance already recorded"
    assert data["stats"]["triangles"] == 7
    assert "file_changed" in [entry["event"] for entry in data["history"]]


def test_upload_of_a_glb_registers_its_source(web):
    app, client, sid, workspace = web
    response = client.post(f"/api/sessions/{sid}/files",
                           files={"file": ("hero.glb", glb_bytes(triangle_document()), "model/gltf-binary")},
                           data={"kind": "document", "caption": "farm result"})
    assert response.status_code == 200
    stored = response.json()["path"]
    data = client.get(f"/api/sessions/{sid}/models/sidecar", params={"path": stored}).json()
    assert data["source"]["tool"] == "upload" and data["prompt"] == "farm result"


# ---------------------------------------------------------------------------- asset route
def test_asset_route_serves_model_media_types_and_refuses_traversal(web):
    app, client, sid, workspace = web
    (workspace / "chair.glb").write_bytes(glb_bytes(triangle_document()))
    (workspace / "scene.gltf").write_text(json.dumps(triangle_document()), encoding="utf-8")
    (workspace / "buffer.bin").write_bytes(b"\x00" * 16)
    assert client.get(f"/api/sessions/{sid}/asset/chair.glb").headers["content-type"] == "model/gltf-binary"
    assert client.get(f"/api/sessions/{sid}/asset/scene.gltf").headers["content-type"] == "model/gltf+json"
    binary = client.get(f"/api/sessions/{sid}/asset/buffer.bin")
    assert binary.status_code == 200 and binary.content == b"\x00" * 16
    # The client normalises a literal "../", so the traversal is sent percent-encoded.
    escape = client.get(f"/api/sessions/{sid}/asset/%2e%2e%2f%2e%2e%2fconfig.json")
    assert escape.status_code == 400 and "workspace" in escape.json()["detail"]
    assert client.get(f"/api/sessions/{sid}/asset/nowhere.glb").status_code == 404


# ---------------------------------------------------------------------------- graphics preset
def test_preset_falls_back_to_the_builtin_record_and_derives_quality_levels(web):
    app, client, sid, workspace = web
    preset = client.get("/api/graphics/preset").json()
    assert preset["source"] == "builtin" and preset["revision"] == 24
    settings = preset["settings"]
    assert settings["tonemapping"] == {"enabled": True, "mode": 2, "aces_preset": 3}
    assert settings["ssao"]["enabled"] is True and settings["bloom"]["enabled"] is False
    assert settings["sun"]["rotation"] == [72.5, -35.0, 0.0]

    levels = preset["quality"]
    assert set(levels) == {"1", "2", "3"}
    assert levels["1"] == levels["1"] | {"shadows": False, "post": False, "pixel_ratio": 1.0}
    assert levels["2"]["shadow_map_size"] == 2048 and levels["2"]["shadow_type"] == "pcf"
    assert levels["2"]["ssao"] is False and levels["2"]["vignette"] is True
    assert levels["3"]["shadow_map_size"] == 4096 and levels["3"]["shadow_type"] == "pcfsoft"
    assert levels["3"]["ssao"] is True, "SSAO is enabled in the server record"
    assert levels["3"]["bloom"] is False, "bloom is disabled in the server record, so level 3 skips it"
    assert levels["3"]["pixel_ratio"] is None and levels["3"]["aa_pass"] == "fxaa"
    for level in levels.values():
        assert level["shadow_distance"] == 40


def test_a_cached_record_is_used_when_the_server_is_unreachable(tmp_path):
    config = Config(tmp_path)
    config.values["allow_web"] = False
    (tmp_path / "graphics-preset.json").write_text(json.dumps({
        "revision": 31,
        "settings": {"postEnabled": True, "Bloom": {"enabled": True, "intensity": 3.0},
                     "Sun": {"rotation": {"x": 10, "y": -20, "z": 0}},
                     "Tonemapping": {"enabled": True, "mode": 0}},
    }), encoding="utf-8")
    registry = ModelRegistry(config, lambda sid: tmp_path, None)
    import asyncio
    preset = asyncio.run(registry.preset())
    assert preset["source"] == "cache" and preset["revision"] == 31
    assert preset["settings"]["bloom"] == {"enabled": True, "threshold": 0.9, "intensity": 3.0, "scatter": 0.02}
    assert preset["settings"]["sun"]["rotation"] == [10.0, -20.0, 0.0]
    assert preset["quality"]["3"]["bloom"] is True
    assert preset["quality"]["3"]["tonemapping"] == "none", "mode 0 is not ACES"
    assert preset["settings"]["ssao"]["intensity"] == 0.59, "keys the record omits keep the builtin value"


# ---------------------------------------------------------------------------- viewer module
@pytest.mark.skipif(not shutil.which("node"), reason="node is not installed on this machine")
def test_viewer_module_and_vendored_addons_parse_as_es_modules():
    """`node --check <file>` silently passes on a file with `import`, so the module mode is fed stdin."""
    static = Path(__file__).resolve().parents[1] / "connector" / "static"
    files = [static / "viewer3d.js", static / "vendor" / "three" / "lib" / "three.module.js",
             static / "vendor" / "three" / "addons" / "loaders" / "GLTFLoader.js",
             static / "vendor" / "three" / "addons" / "postprocessing" / "EffectComposer.js"]
    for file in files:
        assert file.is_file(), f"{file} is missing from the vendored set"
        result = subprocess.run(["node", "--input-type=module", "--check"],
                                stdin=file.open("rb"), capture_output=True)
        assert result.returncode == 0, f"{file.name}: {result.stderr.decode('utf-8', 'replace')[:400]}"
    viewer = (static / "viewer3d.js").read_text(encoding="utf-8")
    assert "'three'" not in viewer and "/static/vendor/three/" in viewer, \
        "the page has no import map, so every specifier must be a real path"


def test_every_viewer_import_points_at_a_vendored_file():
    """No import map is allowed by the page CSP, so each specifier must resolve to a served file."""
    import re
    static = Path(__file__).resolve().parents[1] / "connector" / "static"
    viewer = (static / "viewer3d.js").read_text(encoding="utf-8")
    specifiers = re.findall(r"from '(/static/[^']+)'", viewer)
    assert len(specifiers) >= 12, "the viewer imports three.js and its addons by absolute path"
    for specifier in specifiers:
        assert (static / specifier.removeprefix("/static/")).is_file(), specifier
    # The addons themselves were rewritten away from the bare 'three' specifier when vendored.
    statement = re.compile(r"^\s*(?:import|export)\s[^;]*?from\s*'([^']+)'", re.M | re.S)
    for module in (static / "vendor" / "three" / "addons").rglob("*.js"):
        for specifier in statement.findall(module.read_text(encoding="utf-8")):
            if specifier.startswith("."):
                assert (module.parent / specifier).resolve().is_file(), f"{module.name} -> {specifier}"
            else:
                raise AssertionError(f"{module.name} still imports the bare specifier {specifier!r}")
    for decoder in ("libs/draco/gltf/draco_decoder.wasm", "libs/basis/basis_transcoder.wasm"):
        assert (static / "vendor" / "three" / "addons" / decoder).is_file(), decoder
