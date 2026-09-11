"""Narrow AutoRig farm tools. The model never receives server credentials or a shell."""
import asyncio
import hashlib
import json
import re
import secrets
import time
from urllib.parse import urlparse

from .agent import STRING, safe_path, tool
from .providers import ProviderError

BASE = "https://autorig.online"
SKILL = """AutoRig free-3d-generation, based on the operator's skill (checked 2026-09-12).
Use view_image on the reference. One isolated object, complete silhouette, flat background.
Call autorig_prepare_reference to remove the border-connected backdrop and inspect the returned RGBA.
Then autorig_generate_model submits that reviewed reference with remove_background=false.
autorig_wait waits without repeated LLM calls, downloads the model and computes the solid-prop mesh gate.
Reject a slab/relief; do not declare quality based only on HTTP success or a turntable.
The gate measures thinness, outline_fill, planar_mass and reference silhouette ratio.
These thresholds are for solid props; thin panels and intended sheets require a separate review.
Inspect returned reference and multiple rendered views, verify part identity and topology.
Files remain in the selected session workspace. Public uploads contain only the reference explicitly passed to the tool.
Use job IDs to resume polling; do not submit duplicate jobs because a shared farm is slow.
No rigging, paid generation, production-kit replacement or DEV/Telegram broadcast is performed by these tools.

Video (verified 2026-09-12, absent from the published catalogue): POST /renderfin/api-render with
image_url instead of prompt runs the gen_animation_by_url workflow and returns an .mp4 task.
Use shared_video(path, prompt, frames, size) for it. Never poll the farm with a shell command:
a run_command times out long before a render finishes, while shared_video waits inside the tool,
keeps the chat free and survives a connector restart.
frames counts at 25 fps and must be 8*k+1 (121 ~ 5 s, 241 ~ 10 s, 297 ~ 12 s, the API cap);
size is WIDTHxHEIGHT within 64..512 in steps of 32. Both only take effect when the deployed
template carries $frames/$width/$height; otherwise the workflow renders its built-in length,
which is 49 frames at 384x224, about two seconds.

"render artifact quality rejected ... real_output_artifact_missing: history contains no
non-temporary output artifact" does not mean the render failed. The farm keeps only history
artifacts whose type is not "temp", so a VHS_VideoCombine node with save_output=false saves the
finished clip into ComfyUI's temp directory and the gate discards it. It is node-dependent in
practice, so a rejected task is resubmitted up to three times before it is reported.
Quote the farm's error text, the node and the workflow name; do not call this a farm outage.

Only Done plus a downloaded file proves a result. Pending, Rendering, a 200 on submit or an
output_url that 404s are not results.
Endpoint catalogue: https://autorig.online/dev/api/skills
"""


class AutoRigTools:
    def __init__(self, agent, client):
        self.agent, self.client = agent, client

    def state(self, sid):
        rows = self.agent.store.rows("SELECT value FROM state WHERE key=?", ("autorig:"+sid,))
        return json.loads(rows[0]["value"]) if rows else {"enabled": False, "jobs": {}, "prepared": {}}

    def save(self, sid, value):
        self.agent.store.execute("INSERT OR REPLACE INTO state(key,value) VALUES (?,?)", ("autorig:"+sid, json.dumps(value)))

    def enable(self, sid, enabled):
        value = self.state(sid)
        value["enabled"] = enabled
        self.save(sid, value)
        return value

    def tools(self, session):
        if not self.state(session["id"])["enabled"]:
            return []
        return [tool("autorig_skill", "Read instructions for the connected free image/3D farm.", {}, []),
                tool("autorig_generate_image", "Create a single reference image on the free farm. Returns a persistent task ID.", {"prompt": STRING}, ["prompt"]),
                tool("autorig_prepare_reference", "Remove flat border-connected background, save RGBA, and view it before 3D generation.", {"path": STRING}, ["path"]),
                tool("autorig_generate_model", "Submit a prepared, visually inspected RGBA reference to the free Hunyuan farm. No human broadcast.", {"path": STRING}, ["path"]),
                tool("autorig_wait", "Wait for this session's existing task, download outputs and gate GLB geometry. Polling consumes no model tokens; may take 30 minutes.", {"task_id": STRING}, ["task_id"])]

    async def request(self, method, path, **kwargs):
        response = await self.client.request(method, BASE + path, timeout=60, **kwargs)
        if response.status_code >= 400:
            raise ProviderError(f"AutoRig HTTP {response.status_code}: {response.text[:400]}")
        return response.json()

    async def download(self, url, target):
        parsed = urlparse(url)
        if parsed.scheme != "https" or parsed.hostname not in {"autorig.online", "www.autorig.online"} or parsed.username:
            raise ValueError("Artifact must use the connected AutoRig HTTPS origin")
        size = 0
        async with self.client.stream("GET", url, timeout=180, follow_redirects=False) as response:
            response.raise_for_status()
            if response.is_redirect:
                raise ValueError("Unexpected artifact redirect")
            with target.open("wb") as output:
                async for chunk in response.aiter_bytes():
                    size += len(chunk)
                    if size > 150 * 1024 * 1024:
                        raise ValueError("Artifact exceeds 150 MB")
                    output.write(chunk)
        return size

    async def execute(self, session, name, args):
        sid = session["id"]
        state = self.state(sid)
        if not state["enabled"]:
            raise ValueError("Enable AutoRig for this session first")
        root = self.agent.workspace(sid)
        folder = safe_path(root, "autorig")
        folder.mkdir(exist_ok=True)
        if name == "autorig_skill":
            return {"skill": SKILL, "source": BASE + "/dev/api/skills"}
        if name == "autorig_prepare_reference":
            from .mesh_quality import prepare_reference
            source = safe_path(root, args["path"])
            target = folder / (secrets.token_hex(5) + "-reference.png")
            details = await asyncio.to_thread(prepare_reference, source, target)
            relative = target.relative_to(root).as_posix()
            state["prepared"][relative] = hashlib.sha256(target.read_bytes()).hexdigest()
            self.save(sid, state)
            return details | self.agent.queue_image(session, target)
        if name in {"autorig_generate_image", "autorig_generate_model"}:
            if sum(j.get("stage") not in {"done", "failed"} for j in state["jobs"].values()) >= 1:
                raise ValueError("A task is already pending. Use autorig_wait before submitting another.")
            if len(state["jobs"]) >= 3:
                raise ValueError("Session farm budget is three submissions. Review results before increasing scope.")
            if name == "autorig_generate_image":
                result = await self.request("POST", "/renderfin/api-render", json={"prompt": args["prompt"][:6000]})
                task_id = str(result["task_id"])
                job = {"kind": "image", "stage": "pending", "output_url": result["output_url"]}
            else:
                source = safe_path(root, args["path"])
                digest = hashlib.sha256(source.read_bytes()).hexdigest()
                if state["prepared"].get(args["path"]) != digest:
                    raise ValueError("Prepare and inspect the reference first, then submit the unchanged PNG")
                if args["path"] not in self.agent.delivered_images.get(sid, set()):
                    raise ValueError("Inspect the prepared reference in a separate model step before submitting")
                uploaded = await self.request("POST", "/dev/api/scratch", files={"file": (source.name, source.read_bytes(), "image/png")})
                result = await self.request("POST", "/renderfin/api-character-gen/from-image", json={
                    "image_url": uploaded["url"], "user_name": "AIGent " + sid, "remove_background": False})
                task_id = str(result["job_id"])
                job = {"kind": "model", "stage": result.get("stage", "pending"), "reference": args["path"], "reference_url": uploaded["url"]}
            if not re.fullmatch(r"[A-Za-z0-9_-]{1,120}", task_id):
                raise ProviderError("Invalid AutoRig task identifier")
            state["jobs"][task_id] = job
            self.save(sid, state)
            self.agent.store.event(sid, "notice", {"text": "AutoRig: задача отправлена · " + task_id})
            return {"task_id": task_id, **job, "next": "autorig_wait"}
        if name != "autorig_wait" or args["task_id"] not in state["jobs"]:
            raise ValueError("Unknown task for this session")
        task_id, job = args["task_id"], state["jobs"][args["task_id"]]
        if job.get("result"):
            return job["result"]
        deadline, last_stage = time.monotonic() + 2100, None
        while time.monotonic() < deadline:
            if not self.state(sid)["enabled"]:
                raise ValueError("AutoRig connection was disabled")
            if job["kind"] == "image":
                rows = await self.request("GET", "/renderfin/api-render-get-task-by-url", params={"url": job["output_url"]})
                status = rows[0] if rows else {}
                stage = str(status.get("status", "pending")).lower()
            else:
                status = await self.request("GET", "/renderfin/api-character-gen/" + task_id)
                stage = str(status.get("stage", "pending")).lower()
            if stage != last_stage:
                self.agent.store.event(sid, "notice", {"text": "AutoRig · " + stage, "task_id": task_id})
                last_stage = stage
            if not self.state(sid)["enabled"]:
                raise ValueError("AutoRig connection was disabled")
            job["stage"] = stage
            self.save(sid, state)
            if stage in {"error", "failed", "discarded"}:
                raise ProviderError("AutoRig task failed: " + str(status.get("last_error") or status.get("error") or status))
            if stage in {"done", "ready", "submitted"}:
                break
            await asyncio.sleep(15)
        else:
            return {"task_id": task_id, "stage": last_stage, "pending": True, "next": "Resume autorig_wait; do not duplicate the task"}
        if job["kind"] == "image":
            target = folder / (task_id + "-source.png")
            await self.download(job["output_url"], target)
            result = self.agent.queue_image(session, target)
        else:
            from .mesh_quality import mesh_gate
            if not status.get("glb_url"):
                return {"task_id": task_id, "stage": stage, "pending": True, "note": "Model is not published yet; resume waiting"}
            target = folder / (task_id + ".glb")
            await self.download(status["glb_url"], target)
            gate = await asyncio.to_thread(mesh_gate, target, safe_path(root, job["reference"]))
            result = {"task_id": task_id, "model_path": target.relative_to(root).as_posix(), "glb_url": status["glb_url"], "mesh_gate": gate}
            from .mesh_preview import render_preview
            preview = await render_preview(target, folder / (task_id + '-views'))
            if preview["available"]:
                from pathlib import Path
                result["preview"] = self.agent.queue_image(session, Path(preview["path"]))
            else:
                result["preview"] = preview
            if gate["passed"] and status.get("video_url"):
                video = folder / (task_id + ".mp4")
                await self.download(status["video_url"], video)
                result["video_path"] = video.relative_to(root).as_posix()
            (folder / (task_id + "-report.json")).write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
        job.update(stage="done", result=result)
        self.save(sid, state)
        return result
