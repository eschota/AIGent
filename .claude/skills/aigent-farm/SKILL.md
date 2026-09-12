---
name: aigent-farm
description: Generate images, video and 3D on the connected AutoRig farm from any AIGent chat — endpoints, frame counts, the artifact gate that silently drops renders, and what to do when a node rejects a task.
tags: [aigent, farm, autorig, video, ltxv, image, generation, comfyui]
---

# AutoRig farm from AIGent

The farm is free: it costs no model tokens and no API money. Everything below was checked
against the live service on 2026-09-12; re-check the catalogue before trusting an older note.

## Use the tools, never a polling shell command

| Need | Tool | Notes |
|---|---|---|
| Image from a prompt | `shared_image(prompt)` | ~2-5 min queue, returns the PNG into the chat |
| Video from a workspace frame | `shared_video(path, prompt, frames, size)` | may take 20+ minutes; the tool waits |
| 3D model from a reference | `autorig_generate_model` (after `autorig_prepare_reference`) | session must have AutoRig enabled |

A `run_command` that sleeps in a poll loop is the wrong shape for this: the command has a
timeout, and the farm task outlives it. `shared_video` polls inside the tool, keeps the chat
free, survives a connector restart, and delivers the file as a chat attachment.

## Endpoints actually served

```
POST /renderfin/api-render                  {prompt}            -> image task
POST /renderfin/api-render                  {image_url, prompt} -> video task (gen_animation_by_url)
GET  /renderfin/api-render-get-task-by-url  ?url=<output_url>   -> [{status, error, workflow, render_server_name}]
POST /dev/api/scratch                       file=               -> public {url} for image_url
POST /renderfin/api-character-gen/from-image{image_url}         -> 3D job
GET  /dev/api/skills                                            -> catalogue (video is NOT listed there)
```

`status` is `Pending | Rendering | Done | Error`. The video path is real but undocumented: the
same `api-render` endpoint switches to the animation workflow as soon as `image_url` is present.

## Two animation workflows

`gen_animation_by_url.json` is the default: LTX 13B distilled, 49 frames at 384x224, about two
seconds, roughly 30 seconds of render, no audio.

`gen_animation_hq_by_url.json` is selected by passing `work_flow` (or `quality: hq` in
`shared_video`). Verified on 2026-09-12: scheduled on Raptor, 540 seconds of render, a 197 KB
mp4 of 4.04 s **with an audio track**. It runs LTX-2 19B distilled with
`ltx-2-19b-lora-camera-control-static.safetensors` — the only camera LoRA installed on the node
— plus `LTXVAddGuide(frame_idx=0, strength=0.7)` anchoring the first frame, `LTXVEmptyLatentAudio`
and `CreateVideo` for sound, and `SaveVideo`, which always writes a real artifact and therefore
never trips the gate below. It already carries `$width`, `$height` and `$long_side` placeholders;
its length is hardcoded as `length: 97` at 24 fps.

Frame-to-frame lives in the same node: a second `LTXVAddGuide` with `frame_idx: -1` anchors the
closing frame, so a clip can be driven from image A to image B. That guide is not in the deployed
template yet — adding it is a workflow edit, not an API change.

Camera motion is steered two ways: the static-lock LoRA above, and prompt language. The farm's
own idle prompts spell out "single locked-off tripod shot, no pan, no tilt, no zoom" plus a
negative list of camera moves; copy that pattern when the camera must not drift.

## Clip length and size

`RenderPrompt` accepts `frame_count` (clamped to 300) and `main_size_width/height`
(`clamp_video_dims`: 64-512, multiples of 32, default 384x224). Whether they change anything
depends on the deployed template: values reach the workflow only through the `$frames`,
`$width` and `$height` placeholders. A template without them renders its built-in length —
`gen_animation_by_url.json` hardcodes 49 frames at 384x224, about 2 seconds at 25 fps.

LTXV wants `8*k+1` frames: 49 ≈ 2 s, 121 ≈ 5 s, 241 ≈ 10 s, 297 ≈ 12 s (the API cap),
377 ≈ 15 s (only reachable by editing the template). 377 frames at 512x288 rendered in 51 s
on Raptor with 10.8 GB free VRAM.

## The failure that looks like a farm outage

```
render artifact quality rejected on <node>: real_output_artifact_missing:
history contains no non-temporary output artifact
```

This does not mean the render failed. `comfy_adapter.resolve_artifacts` keeps only history
entries whose `type != "temp"`, so a workflow whose `VHS_VideoCombine` has
`"save_output": false` writes the finished mp4 into ComfyUI's temp directory and the gate
throws the result away. Verified A/B on Raptor: `save_output=false` produces an artifact of
type `temp` (rejected), `save_output=true` produces type `output` (accepted). The fix belongs
in the workflow JSON on the VPS.

Until that lands the outcome is node-dependent — f15 delivered the same task that Raptor and
f5 rejected — so `shared_video` resubmits a rejected task up to three times, and each retry is
visible in the chat. Report the farm's own error text verbatim; never call a rejection
"generation is broken" without naming the node and the workflow.

## Honest reporting

`Done` plus a downloadable file is the only proof. `Pending`, `Rendering`, HTTP 200 on submit,
or an `output_url` that 404s are not results. State the task id, the node, the frame count and
the real byte size of what arrived.

## Frame to frame

`LTXVBaseSampler` conditions on a batch of images plus `optional_cond_indices`, so a second anchor
is an `ImageBatch` and indices `"0,-1"`. Deployed on 2026-09-12: `RenderPrompt.image_url_end` is
uploaded like the first frame, substituted as `$image_end`, and when it is absent the batch node and
the second loader are pruned and the indices fall back to `"0"` — the single-frame path is byte for
byte what it was. Verified live: two anchors rendered a 4.84 s transition on f5 in 150 s.

Do not add a new workflow file for this. A name no node advertises stays `Pending` forever — an
unknown workflow was still Pending after 80 s with no server assigned — so an optional branch inside
an advertised template is the only mechanism that reaches the farm.

## Judging a clip

Never call a render good because the task said Done. `video_inspect` pulls real frames out with
ffmpeg (scene cuts first, even spacing when a smooth clip has none) and hands them to vision, and
reports what ffprobe measured: resolution, fps, codec, bitrate, duration, audio. `video_index`
writes `videos.json` for the whole workspace. The default farm clip measures 512x288 at 25 fps with
no audio — that is a draft, not a deliverable, and the verdict says so.

## Sound, and long films

The farm's own audio comes from the HQ workflow: LTX-2 generates an ambience track inside the clip
(`mp4a` confirmed in a delivered file). It is atmosphere, not speech — nothing on the farm does
text-to-speech today, so narration has to arrive as an audio file.

A long film is built from beats, not from one long render. The API caps `frame_count` at 400 after
the 2026-09-12 patch, and a single LTX render stays coherent for roughly ten seconds anyway:

1. Split the script into beats of five to ten seconds and pick or generate a keyframe for each.
2. Render every beat with `shared_video`; chain them by passing the previous beat's closing frame as
   `path` and the next keyframe as the end anchor, so the cut lands on matching pixels.
3. Assemble locally with `video_assemble`: segments are padded to the largest frame, the generated
   ambience is ducked to 25 % and the narration sits on top. This costs no farm time at all.
4. Judge the result with `video_inspect`, not with the fact that the render finished.

## Worth adding to the farm

Measured gaps, in the order they would change the output most:

- Frame interpolation (RIFE or FILM). Everything renders at 25 fps and reads as choppy in motion;
  interpolation to 50 fps is cheap and needs no new model.
- Upscaling. 512x288 is the ceiling `clamp_video_dims` allows, and `video_inspect` calls that
  "below SD" for good reason. An ESRGAN-class pass after the render lifts a draft to a deliverable.
- More camera LoRAs. Only `ltx-2-19b-lora-camera-control-static` is installed, so every move has to
  be described in the prompt; push-in and orbit variants would make shots directable.
- A TTS node, which is what stands between this pipeline and a narrated film without manual steps.
