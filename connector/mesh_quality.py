"""Heuristic slab gate for solid game props, not a universal asset-quality score."""
from collections import deque

import numpy as np
from PIL import Image, ImageDraw


def prepare_reference(source, target):
    with Image.open(source) as im:
        rgba = np.array(im.convert("RGBA"))
    if np.count_nonzero(rgba[:, :, 3] == 0) < rgba.shape[0] * rgba.shape[1] * .01:
        rgb = rgba[:, :, :3].astype(float)
        border = np.concatenate([rgb[0], rgb[-1], rgb[:, 0], rgb[:, -1]])
        bg = np.median(border, axis=0)
        candidate = np.abs(rgb - bg).sum(axis=2) < 60
        h, w = candidate.shape
        outside = np.zeros((h, w), dtype=bool)
        q = deque([(0, x) for x in range(w)] + [(h-1, x) for x in range(w)] + [(y, 0) for y in range(h)] + [(y, w-1) for y in range(h)])
        while q:
            y, x = q.popleft()
            if not (0 <= y < h and 0 <= x < w) or outside[y, x] or not candidate[y, x]:
                continue
            outside[y, x] = True
            q.extend(((y-1, x), (y+1, x), (y, x-1), (y, x+1)))
        rgba[outside, 3] = 0
    mask = rgba[:, :, 3] > 127
    fraction = float(mask.mean())
    if not .03 < fraction < .95:
        raise ValueError("Background removal did not isolate the subject; inspect the reference")
    Image.fromarray(rgba).save(target)
    return {"foreground_fraction": fraction, "has_transparency": True}


def mesh_gate(model, reference):
    import trimesh
    scene = trimesh.load(model, force="scene")
    mesh = scene.to_mesh()
    if not len(mesh.faces) or not np.isfinite(mesh.vertices).all():
        raise ValueError("Mesh is empty or contains non-finite vertices")
    extent = np.asarray(mesh.extents)
    if extent.max() <= 0:
        raise ValueError("Mesh has zero extent")
    axis = int(extent.argmin())
    thinness = float(extent.min() / extent.max())
    keep = [i for i in range(3) if i != axis]
    xy = mesh.vertices[:, keep]
    xy = (xy - xy.min(axis=0)) / np.maximum(np.ptp(xy, axis=0), 1e-8) * 255
    mask = Image.new("1", (256, 256))
    draw = ImageDraw.Draw(mask)
    for face in mesh.faces:
        draw.polygon([tuple(v) for v in xy[face]], fill=1)
    fill = float(np.array(mask).mean())
    centres = mesh.triangles_center[:, axis]
    position = (centres - centres.min()) / max(float(np.ptp(centres)), 1e-8)
    planar = float(mesh.area_faces[(position <= .06) | (position >= .94)].sum() / max(mesh.area, 1e-8))
    with Image.open(reference) as image:
        alpha = np.array(image.convert("RGBA"))[:, :, 3] > 127
    ys, xs = np.nonzero(alpha)
    ref_fill = float(alpha.sum() / ((ys.max()-ys.min()+1)*(xs.max()-xs.min()+1)))
    ratio = fill / max(ref_fill, 1e-8)
    reasons = []
    if thinness < .08:
        reasons.append("Too thin for a solid prop: possible relief/backplate")
    if fill > .85 and planar > .70:
        reasons.append("Most surface area lies on outer planes with a rectangular outline")
    if fill > .85 and ratio > 1.25:
        reasons.append("Mesh silhouette is substantially fuller than the reference")
    if fill > .85 and thinness < .12 and ratio > 1.1:
        reasons.append("Thin rectangular slab")
    return {"passed": not reasons, "profile": "solid_prop", "triangles": len(mesh.faces), "vertices": len(mesh.vertices),
            "thinness": thinness, "outline_fill": fill, "planar_mass": planar, "reference_fill": ref_fill,
            "silhouette_ratio": ratio, "extents": extent.tolist(), "reasons": reasons,
            "limitation": "Heuristic geometry gate. Compare multi-view renders to reference before accepting quality."}
