"""3D models in a session workspace: a per-model provenance sidecar and the viewer graphics preset.

Two independent jobs live here.

*Registry* — find model files in a workspace, describe them with a pure-python glTF/GLB parser
(no external libraries: the JSON chunk of a .glb carries everything we report) and keep one
``<model>.json`` sidecar next to every model. The sidecar is the user-visible provenance record:
format, prompt, source job, seed, parameters, reference images and a history of edits. It is
written into the workspace on purpose, so it travels with the model.

*Graphics preset* — the viewer's look follows the Gravity House server graphics record
(``/gravityhouse/api/graphics``). The record is a Unity URP settings dump; we read only the keys a
three.js viewer can honour, cache the answer next to the other runtime state and fall back to the
embedded revision-24 values when the server is unreachable or web access is switched off. Quality
levels 1/2/3 are derived from that record here, so the browser never has to interpret Unity fields.
"""

import hashlib
import json
import mimetypes
import struct
import time
from pathlib import Path

import httpx

from .agent import safe_path

SCHEMA = "aigent.model/1"
VIEWABLE_SUFFIXES = {".glb", ".gltf"}
OTHER_SUFFIXES = {".fbx", ".obj", ".usdz", ".usd", ".usdc", ".ply", ".stl"}
MODEL_SUFFIXES = VIEWABLE_SUFFIXES | OTHER_SUFFIXES
REFERENCE_SUFFIXES = {".png", ".jpg", ".jpeg", ".webp", ".bmp", ".tif", ".tiff"}
SKIP_DIRECTORIES = {".git", ".local", "node_modules", "__pycache__", ".venv", "media-cache"}
MAX_SCAN_FILES = 4000
GLB_MAGIC = 0x46546C67
CHUNK_JSON = 0x4E4F534A
PATCH_FIELDS = ("prompt", "negative_prompt", "seed", "params", "tags", "source", "note", "title")

EXTRA_MIME = {".glb": "model/gltf-binary", ".gltf": "model/gltf+json", ".bin": "application/octet-stream",
              ".ktx2": "image/ktx2", ".drc": "application/octet-stream", ".fbx": "application/octet-stream",
              ".obj": "text/plain", ".usdz": "model/vnd.usdz+zip", ".basis": "application/octet-stream"}
for _suffix, _mime in EXTRA_MIME.items():
    mimetypes.add_type(_mime, _suffix)

GRAPHICS_URL = "https://autorig.online/gravityhouse/api/graphics"
# Gravity House server record, revision 24, reduced to the keys a three.js viewer can honour.
BUILTIN_REVISION = 24
BUILTIN_SETTINGS = {
    "post_enabled": True,
    "antialiasing": 1, "antialiasing_quality": 1, "taa_quality": 3,
    "bloom": {"enabled": False, "threshold": 0.9, "intensity": 1.81, "scatter": 0.02},
    "color_adjustments": {"enabled": False, "post_exposure": 0.0, "contrast": 0.0,
                          "saturation": 0.0, "hue_shift": 0.0},
    "tonemapping": {"enabled": True, "mode": 2, "aces_preset": 3},
    "vignette": {"enabled": True, "color": [0.22, 0.18, 0.22], "center": [0.5, 0.5],
                 "intensity": 0.44, "smoothness": 0.2, "rounded": False},
    "ssao": {"enabled": True, "intensity": 0.59, "radius": 0.28, "direct_lighting_strength": 1.0,
             "falloff": 72.83, "downsample": True, "blur_quality": 1},
    "sun": {"enabled": True, "intensity": 1.05, "rotation": [72.5, -35.0, 0.0],
            "color": [1.0, 0.95, 0.84], "softness": 4.0, "shadow_strength": 0.85, "shadow_bias": 0.45},
    "environment": {"background_mode": 0, "background": [0.81, 0.78, 0.70],
                    "zenith": [0.82, 0.88, 0.90], "horizon": [0.63, 0.59, 0.49],
                    "ground": [0.20, 0.22, 0.18], "exposure": 0.0, "ambient_intensity": 1.0,
                    "ambient_tint": [1.0, 1.0, 1.0], "reflections": 1.0},
    "white_balance": {"enabled": False},
}
# Where each normalised key comes from in the Unity record. Lookup is case-insensitive per segment.
FIELD_SOURCES = {
    "post_enabled": "postEnabled",
    "antialiasing": "antialiasing", "antialiasing_quality": "antialiasingQuality",
    "taa_quality": "TAA.quality",
    "bloom.enabled": "Bloom.enabled", "bloom.threshold": "Bloom.threshold",
    "bloom.intensity": "Bloom.intensity", "bloom.scatter": "Bloom.scatter",
    "color_adjustments.enabled": "ColorAdjustments.enabled",
    "color_adjustments.post_exposure": "ColorAdjustments.postExposure",
    "color_adjustments.contrast": "ColorAdjustments.contrast",
    "color_adjustments.saturation": "ColorAdjustments.saturation",
    "color_adjustments.hue_shift": "ColorAdjustments.hueShift",
    "tonemapping.enabled": "Tonemapping.enabled", "tonemapping.mode": "Tonemapping.mode",
    "tonemapping.aces_preset": "Tonemapping.acesPreset",
    "vignette.enabled": "Vignette.enabled", "vignette.color": "Vignette.color",
    "vignette.center": "Vignette.center", "vignette.intensity": "Vignette.intensity",
    "vignette.smoothness": "Vignette.smoothness", "vignette.rounded": "Vignette.rounded",
    "ssao.enabled": "SSAO.enabled", "ssao.intensity": "SSAO.Intensity", "ssao.radius": "SSAO.Radius",
    "ssao.direct_lighting_strength": "SSAO.DirectLightingStrength", "ssao.falloff": "SSAO.Falloff",
    "ssao.downsample": "SSAO.Downsample", "ssao.blur_quality": "SSAO.BlurQuality",
    "sun.enabled": "Sun.enabled", "sun.intensity": "Sun.intensity", "sun.rotation": "Sun.rotation",
    "sun.color": "Sun.color", "sun.softness": "Sun.softness",
    "sun.shadow_strength": "Sun.shadowStrength", "sun.shadow_bias": "Sun.shadowBias",
    "environment.background_mode": "Environment.backgroundMode",
    "environment.background": "Environment.background", "environment.zenith": "Environment.zenith",
    "environment.horizon": "Environment.horizon", "environment.ground": "Environment.ground",
    "environment.exposure": "Environment.exposure",
    "environment.ambient_intensity": "Environment.ambientIntensity",
    "environment.ambient_tint": "Environment.ambientTint",
    "environment.reflections": "Environment.reflections",
    "white_balance.enabled": "WhiteBalance.enabled",
}


class ModelError(Exception):
    """The requested file is not a model, or its glTF payload cannot be read."""


def asset_mime(file: Path) -> str:
    return EXTRA_MIME.get(file.suffix.lower()) or mimetypes.guess_type(file.name)[0] or "application/octet-stream"


def digest(file: Path) -> str:
    state = hashlib.sha256()
    with file.open("rb") as handle:
        while chunk := handle.read(1 << 20):
            state.update(chunk)
    return state.hexdigest()


# ---------------------------------------------------------------------------- glTF / GLB parsing
def gltf_document(file: Path) -> dict:
    """The glTF JSON of a .gltf file or of the JSON chunk of a .glb, without any external library."""
    if file.suffix.lower() == ".gltf":
        try:
            return json.loads(file.read_text(encoding="utf-8"))
        except (ValueError, UnicodeDecodeError) as exc:
            raise ModelError("This .gltf file is not valid JSON") from exc
    with file.open("rb") as handle:
        header = handle.read(12)
        if len(header) < 12:
            raise ModelError("File is too short to be a GLB")
        magic, _version, _length = struct.unpack("<III", header)
        if magic != GLB_MAGIC:
            raise ModelError("Not a GLB file: the glTF magic is missing")
        while True:
            block = handle.read(8)
            if len(block) < 8:
                raise ModelError("GLB contains no JSON chunk")
            size, kind = struct.unpack("<II", block)
            payload = handle.read(size)
            if kind == CHUNK_JSON:
                try:
                    return json.loads(payload.decode("utf-8").rstrip("\x00 "))
                except (ValueError, UnicodeDecodeError) as exc:
                    raise ModelError("The GLB JSON chunk is not valid JSON") from exc


def _accessor(document, index):
    accessors = document.get("accessors") or []
    return accessors[index] if isinstance(index, int) and 0 <= index < len(accessors) else {}


def _primitive_triangles(document, primitive) -> int:
    mode = primitive.get("mode", 4)
    if mode not in (4, 5, 6):
        return 0
    if "indices" in primitive:
        count = _accessor(document, primitive["indices"]).get("count") or 0
    else:
        count = _accessor(document, (primitive.get("attributes") or {}).get("POSITION")).get("count") or 0
    if not isinstance(count, int) or count < 3:
        return 0
    return count // 3 if mode == 4 else count - 2


def gltf_stats(file: Path) -> dict:
    """Counts and bounds taken straight from the glTF document: honest numbers, no geometry decode."""
    document = gltf_document(file)
    meshes = document.get("meshes") or []
    triangles, primitives = 0, 0
    low = [float("inf")] * 3
    high = [float("-inf")] * 3
    for mesh in meshes:
        for primitive in mesh.get("primitives") or []:
            primitives += 1
            triangles += _primitive_triangles(document, primitive)
            position = _accessor(document, (primitive.get("attributes") or {}).get("POSITION"))
            minimum, maximum = position.get("min"), position.get("max")
            if isinstance(minimum, list) and isinstance(maximum, list) and len(minimum) >= 3 and len(maximum) >= 3:
                for axis in range(3):
                    low[axis] = min(low[axis], float(minimum[axis]))
                    high[axis] = max(high[axis], float(maximum[axis]))
    bounds = None
    if all(value != float("inf") for value in low):
        bounds = {"min": [round(v, 6) for v in low], "max": [round(v, 6) for v in high],
                  "size": [round(high[i] - low[i], 6) for i in range(3)]}
    extensions = document.get("extensionsUsed") or []
    return {"meshes": len(meshes), "primitives": primitives, "triangles": triangles,
            "materials": len(document.get("materials") or []),
            "textures": len(document.get("textures") or []),
            "images": len(document.get("images") or []),
            "animations": len(document.get("animations") or []),
            "nodes": len(document.get("nodes") or []),
            "skins": len(document.get("skins") or []),
            "bounds": bounds,
            "generator": str((document.get("asset") or {}).get("generator") or "")[:200],
            "version": str((document.get("asset") or {}).get("version") or ""),
            "extensions": [str(name)[:60] for name in extensions][:20],
            "draco": "KHR_draco_mesh_compression" in extensions,
            "ktx2": "KHR_texture_basisu" in extensions}


# ---------------------------------------------------------------------------- preset helpers
def _lookup(settings, dotted: str):
    node = settings
    for segment in dotted.split("."):
        if not isinstance(node, dict):
            return None
        found = None
        for key in node:
            if isinstance(key, str) and key.lower() == segment.lower():
                found = key
                break
        if found is None:
            return None
        node = node[found]
    return node


def _as_vector(value, length, default):
    if isinstance(value, dict):
        order = ["r", "g", "b", "a"] if length == 3 and "r" in {k.lower() for k in value} else ["x", "y", "z", "w"]
        value = [_lookup(value, name) for name in order[:length]]
    if isinstance(value, (list, tuple)) and len(value) >= length:
        try:
            return [float(component) for component in value[:length]]
        except (TypeError, ValueError):
            return list(default)
    return list(default)


def normalise_settings(raw: dict) -> dict:
    """Take the viewer-relevant keys out of the Unity record; anything missing keeps the builtin value."""
    result = json.loads(json.dumps(BUILTIN_SETTINGS))
    if not isinstance(raw, dict):
        return result
    for dotted, source in FIELD_SOURCES.items():
        value = _lookup(raw, source)
        if value is None:
            continue
        parts = dotted.split(".")
        holder = result
        for part in parts[:-1]:
            holder = holder[part]
        current = holder[parts[-1]]
        if isinstance(current, bool):
            holder[parts[-1]] = bool(value)
        elif isinstance(current, list):
            holder[parts[-1]] = _as_vector(value, len(current), current)
        elif isinstance(current, int) and not isinstance(current, bool):
            try:
                holder[parts[-1]] = int(value)
            except (TypeError, ValueError):
                pass
        else:
            try:
                holder[parts[-1]] = float(value)
            except (TypeError, ValueError):
                pass
    return result


def quality_presets(settings: dict) -> dict:
    """Three levels the browser applies verbatim. Mesh identities and transforms never change."""
    bloom, ssao = settings["bloom"], settings["ssao"]
    tonemapping, vignette = settings["tonemapping"], settings["vignette"]
    antialiasing = int(settings.get("antialiasing") or 0)
    post = bool(settings.get("post_enabled"))
    tonemapped = bool(tonemapping.get("enabled")) and int(tonemapping.get("mode") or 0) == 2
    return {
        "1": {"label": "Быстро", "pixel_ratio": 1.0, "msaa": 0, "antialias": False,
              "shadows": False, "shadow_map_size": 0, "shadow_type": "none", "shadow_distance": 40,
              "post": False, "ssao": False, "bloom": False, "vignette": False,
              "tonemapping": "aces" if tonemapped else "none", "environment": "flat",
              "note": "Без теней и постобработки: один проход рендера."},
        "2": {"label": "Сбалансировано", "pixel_ratio": 1.5, "msaa": 4, "antialias": True,
              "shadows": True, "shadow_map_size": 2048, "shadow_type": "pcf", "shadow_distance": 40,
              "post": post, "ssao": False, "bloom": False,
              "vignette": post and bool(vignette.get("enabled")),
              "tonemapping": "aces" if tonemapped else "none", "environment": "room",
              "note": "PCF-тени 2048, тонмаппинг и виньетка; без SSAO и bloom."},
        "3": {"label": "Максимум", "pixel_ratio": None, "msaa": 4, "antialias": True,
              "shadows": True, "shadow_map_size": 4096, "shadow_type": "pcfsoft", "shadow_distance": 40,
              "post": post, "ssao": post and bool(ssao.get("enabled")),
              "bloom": post and bool(bloom.get("enabled")),
              "vignette": post and bool(vignette.get("enabled")),
              "aa_pass": "smaa" if antialiasing >= 2 else "fxaa" if antialiasing == 1 else "none",
              "tonemapping": "aces" if tonemapped else "none", "environment": "room",
              "note": "PCFSoft-тени 4096, SSAO, bloom по записи сервера, SMAA/FXAA и OutputPass."},
    }


# ---------------------------------------------------------------------------- registry
class ModelRegistry:
    def __init__(self, config, workspace, client: httpx.AsyncClient | None = None):
        self.config, self.workspace, self.client = config, workspace, client
        self.stats_cache: dict[tuple, dict] = {}
        self.preset_cache: dict | None = None

    # -------------------------------------------------------------- paths
    def model_file(self, sid: str, relative: str) -> Path:
        file = safe_path(self.workspace(sid), relative)
        if file.suffix.lower() not in MODEL_SUFFIXES:
            raise ModelError("This file is not a 3D model")
        if not file.is_file():
            raise ModelError("Model not found")
        return file

    @staticmethod
    def sidecar_path(file: Path) -> Path:
        """``chair.glb`` keeps its provenance in ``chair.glb.json`` — one sidecar per model file."""
        return file.with_name(file.name + ".json")

    def scan(self, sid: str) -> list[Path]:
        root = self.workspace(sid)
        found: list[Path] = []
        if not root.is_dir():
            return found
        stack = [root]
        while stack and len(found) < MAX_SCAN_FILES:
            folder = stack.pop()
            try:
                entries = sorted(folder.iterdir())
            except OSError:
                continue
            for entry in entries:
                if entry.is_symlink():
                    continue
                if entry.is_dir():
                    if entry.name not in SKIP_DIRECTORIES and not entry.name.startswith("."):
                        stack.append(entry)
                elif entry.suffix.lower() in MODEL_SUFFIXES:
                    found.append(entry)
        return sorted(found, key=lambda p: p.stat().st_mtime, reverse=True)

    def stats(self, file: Path) -> dict:
        """Parsed counts, cached by path, mtime and size so a repeated listing re-reads nothing."""
        if file.suffix.lower() not in VIEWABLE_SUFFIXES:
            return {}
        stat = file.stat()
        key = (str(file), stat.st_mtime_ns, stat.st_size)
        if key not in self.stats_cache:
            try:
                self.stats_cache[key] = gltf_stats(file)
            except (ModelError, OSError, struct.error) as exc:
                self.stats_cache[key] = {"error": str(exc)}
        return self.stats_cache[key]

    # -------------------------------------------------------------- sidecar
    def blank(self, file: Path, root: Path, source: dict | None = None) -> dict:
        stat = file.stat()
        now = time.time()
        return {"schema": SCHEMA, "file": file.relative_to(root).as_posix(),
                "format": file.suffix.lower().lstrip("."), "created": now, "updated": now,
                "size": stat.st_size, "sha256": digest(file),
                "source": {"tool": "unknown", "provider": "", "job_id": "", "url": ""} | (source or {}),
                "prompt": "", "negative_prompt": "", "seed": None, "params": {},
                "references": [], "history": [], "stats": self.stats(file), "tags": []}

    def read_sidecar(self, file: Path, root: Path) -> dict | None:
        path = self.sidecar_path(file)
        if not path.is_file():
            return None
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (ValueError, UnicodeDecodeError, OSError):
            return None
        return data if isinstance(data, dict) else None

    def write_sidecar(self, file: Path, data: dict) -> dict:
        path = self.sidecar_path(file)
        temp = path.with_name(path.name + ".tmp")
        temp.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
        temp.replace(path)
        return data

    def note(self, data: dict, event: str, detail: str = "") -> dict:
        history = data.setdefault("history", [])
        history.append({"ts": time.time(), "event": event, "detail": detail[:400]})
        del history[:-200]
        data["updated"] = time.time()
        return data

    def sidecar(self, sid: str, relative: str, source: dict | None = None, create: bool = True) -> dict:
        """Read the sidecar next to a model, creating it on first sight."""
        root = self.workspace(sid)
        file = self.model_file(sid, relative)
        data = self.read_sidecar(file, root)
        if data is None:
            if not create:
                return {}
            data = self.note(self.blank(file, root, source), "created",
                             "Sidecar created by the model registry")
            return self.write_sidecar(file, data)
        stat = file.stat()
        changed = data.get("size") != stat.st_size
        if changed:  # the file was regenerated under the same name
            data["size"], data["sha256"] = stat.st_size, digest(file)
            data["stats"] = self.stats(file)
            self.note(data, "file_changed", "Model bytes changed on disk; stats and hash refreshed")
        elif not data.get("stats"):
            data["stats"] = self.stats(file)
            changed = True
        data.setdefault("schema", SCHEMA)
        data.setdefault("file", file.relative_to(root).as_posix())
        data.setdefault("format", file.suffix.lower().lstrip("."))
        for key, empty in (("references", list), ("history", list), ("tags", list), ("params", dict)):
            if not isinstance(data.get(key), empty):
                data[key] = empty()
        if changed:
            self.write_sidecar(file, data)
        return data

    def update_sidecar(self, sid: str, relative: str, patch: dict) -> dict:
        if not isinstance(patch, dict):
            raise ModelError("Patch must be an object")
        unknown = [key for key in patch if key not in PATCH_FIELDS]
        if unknown:
            raise ModelError("Unknown sidecar fields: " + ", ".join(sorted(unknown)))
        data = self.sidecar(sid, relative)
        file = self.model_file(sid, relative)
        applied = []
        for key, value in patch.items():
            if key == "tags":
                if not isinstance(value, list):
                    raise ModelError("tags must be a list of strings")
                data["tags"] = [str(tag)[:60] for tag in value][:40]
            elif key in ("params", "source"):
                if not isinstance(value, dict):
                    raise ModelError(key + " must be an object")
                data[key] = (data.get(key) or {}) | value
            elif key == "seed":
                data["seed"] = None if value is None else str(value)[:80]
            else:
                data[key] = str(value)[:20000]
            applied.append(key)
        self.note(data, "updated", "Fields: " + ", ".join(sorted(applied)))
        return self.write_sidecar(file, data)

    def add_reference(self, sid: str, relative: str, image: str, note: str = "") -> dict:
        """Attach a reference image already present in the workspace; nothing is copied."""
        root = self.workspace(sid)
        picture = safe_path(root, image)
        if not picture.is_file():
            raise ModelError("Reference image not found in this workspace")
        if picture.suffix.lower() not in REFERENCE_SUFFIXES:
            raise ModelError("A reference must be an image file")
        data = self.sidecar(sid, relative)
        file = self.model_file(sid, relative)
        entry = {"path": picture.relative_to(root).as_posix(), "kind": "image",
                 "note": str(note)[:500], "sha256": digest(picture)}
        references = [item for item in data.get("references") or []
                      if isinstance(item, dict) and item.get("path") != entry["path"]]
        references.append(entry)
        data["references"] = references[:40]
        self.note(data, "reference_added", entry["path"])
        return self.write_sidecar(file, data)

    def register(self, sid: str, relative: str, meta: dict | None = None) -> dict:
        """Called by a generator once a model lands in the workspace (upload, farm job, autorig)."""
        meta = meta or {}
        source = meta.get("source") or {}
        data = self.sidecar(sid, relative, source=source)
        patch = {key: meta[key] for key in PATCH_FIELDS if key in meta}
        if source:
            patch["source"] = source
        if patch:
            return self.update_sidecar(sid, relative, patch)
        return data

    # -------------------------------------------------------------- listing
    def list(self, sid: str) -> list[dict]:
        root = self.workspace(sid)
        items = []
        for file in self.scan(sid):
            suffix = file.suffix.lower()
            viewable = suffix in VIEWABLE_SUFFIXES
            relative = file.relative_to(root).as_posix()
            stat = file.stat()
            try:
                data = self.sidecar(sid, relative)
                has_sidecar = True
            except (ModelError, OSError, ValueError):
                data, has_sidecar = {}, False
            items.append({
                "path": relative, "name": file.name, "format": suffix.lstrip("."),
                "size": stat.st_size, "modified": stat.st_mtime, "viewable": viewable,
                "sidecar": has_sidecar, "sidecar_path": self.sidecar_path(file).relative_to(root).as_posix(),
                "prompt": data.get("prompt", ""), "tags": data.get("tags", []),
                "source": data.get("source", {}), "references": len(data.get("references") or []),
                "stats": data.get("stats") or {},
                "note": "" if viewable else f"Формат .{suffix.lstrip('.')} показан в списке; "
                                            "встроенный просмотр работает для .glb и .gltf.",
            })
        return items

    # -------------------------------------------------------------- graphics preset
    @property
    def preset_url(self) -> str:
        return str(self.config.values.get("graphics_preset_url") or GRAPHICS_URL)

    @property
    def preset_file(self) -> Path:
        return self.config.root / "graphics-preset.json"

    def preset_from(self, source: str, revision, raw: dict) -> dict:
        settings = normalise_settings(raw)
        return {"source": source, "revision": revision, "url": self.preset_url,
                "settings": settings, "quality": quality_presets(settings),
                "note": "Значения графики берутся из записи сервера Gravity House; "
                        "оффлайн используется встроенная запись ревизии 24."}

    async def preset(self, refresh: bool = False) -> dict:
        if self.preset_cache is not None and not refresh:
            return self.preset_cache
        if self.config.values.get("allow_web", True) and self.client is not None:
            try:
                response = await self.client.get(self.preset_url, timeout=10)
                response.raise_for_status()
                payload = response.json()
                if isinstance(payload, dict) and isinstance(payload.get("settings"), dict):
                    record = {"revision": payload.get("revision"), "settings": payload["settings"],
                              "fetched": time.time()}
                    try:
                        self.preset_file.write_text(json.dumps(record, indent=2, ensure_ascii=False),
                                                    encoding="utf-8")
                    except OSError:
                        pass
                    self.preset_cache = self.preset_from("server", record["revision"], record["settings"])
                    return self.preset_cache
            except (httpx.HTTPError, ValueError, TypeError):
                pass
        if self.preset_file.is_file():
            try:
                record = json.loads(self.preset_file.read_text(encoding="utf-8"))
                if isinstance(record.get("settings"), dict):
                    self.preset_cache = self.preset_from("cache", record.get("revision"), record["settings"])
                    return self.preset_cache
            except (ValueError, UnicodeDecodeError, OSError):
                pass
        self.preset_cache = self.preset_from("builtin", BUILTIN_REVISION, {})
        return self.preset_cache
