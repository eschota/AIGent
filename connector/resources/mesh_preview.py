"""Run only in a separate Blender --background --factory-startup process."""
import argparse
import math
import sys
from pathlib import Path

import bpy
from mathutils import Vector

parser = argparse.ArgumentParser()
parser.add_argument('--input', required=True)
parser.add_argument('--output', required=True)
args = parser.parse_args(sys.argv[sys.argv.index('--')+1:])
bpy.ops.object.select_all(action='SELECT')
bpy.ops.object.delete(use_global=False)
bpy.ops.import_scene.gltf(filepath=args.input)
meshes = [o for o in bpy.context.scene.objects if o.type == 'MESH']
corners = [o.matrix_world @ Vector(c) for o in meshes for c in o.bound_box]
lo = Vector(tuple(min(v[i] for v in corners) for i in range(3)))
hi = Vector(tuple(max(v[i] for v in corners) for i in range(3)))
center = (lo+hi)/2
span = max(hi-lo)
scene = bpy.context.scene
scene.render.engine = 'CYCLES'
scene.cycles.samples = 24
scene.render.resolution_x = 512
scene.render.resolution_y = 512
scene.render.resolution_percentage = 100
scene.render.image_settings.file_format = 'PNG'
scene.world.color = (.15,.15,.15)
scene.view_settings.view_transform = 'Standard'
for position, energy in [((3,-4,5),700),((-4,-1,3),450),((0,4,5),650)]:
    bpy.ops.object.light_add(type='AREA', location=center+Vector(position)*span)
    light=bpy.context.object
    light.data.energy=energy*span*span
    light.data.shape='DISK'
    light.data.size=span*3
    light.rotation_euler=(center-light.location).to_track_quat('-Z','Y').to_euler()
bpy.ops.object.camera_add()
camera=bpy.context.object
scene.camera=camera
camera.data.type='ORTHO'
camera.data.ortho_scale=span*1.6
folder=Path(args.output)
folder.mkdir(parents=True,exist_ok=True)
for index,angle in enumerate((25,145,265)):
    radians=math.radians(angle)
    camera.location=center+Vector((math.cos(radians)*2.5,-math.sin(radians)*2.5,1.5))*span
    camera.rotation_euler=(center-camera.location).to_track_quat('-Z','Y').to_euler()
    scene.render.filepath=str(folder/f'view-{index}.png')
    bpy.ops.render.render(write_still=True)
