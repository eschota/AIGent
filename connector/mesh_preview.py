import asyncio
import os
from pathlib import Path
import shutil

from PIL import Image, ImageDraw


async def render_preview(model, output):
    candidates = [Path(p) for p in (shutil.which('blender'),) if p]
    if os.name == 'nt':
        candidates += sorted(Path('C:/Program Files/Blender Foundation').glob('Blender */blender.exe'), reverse=True)
    if not candidates:
        return {"available": False, "reason": "Install Blender for local multi-view mesh review"}
    output.mkdir(parents=True, exist_ok=True)
    script = Path(__file__).parent / 'resources' / 'mesh_preview.py'
    with (output/'render.log').open('wb') as log:
        options = {'creationflags': 0x08000000} if os.name == 'nt' else {}
        proc = await asyncio.create_subprocess_exec(str(candidates[0]), '--background', '--factory-startup', '--threads', '4', '--python', str(script), '--', '--input', str(model), '--output', str(output),
            cwd=output, stdout=log, stderr=log, **options)
        try:
            await asyncio.wait_for(proc.wait(), 240)
        except BaseException:
            if proc.returncode is None:
                proc.kill()
                await proc.wait()
            raise
    if proc.returncode:
        return {"available": False, "reason": "Blender preview failed; inspect render.log"}
    canvas=Image.new('RGB',(1536,550),'#22242a')
    draw=ImageDraw.Draw(canvas)
    for i in range(3):
        with Image.open(output/f'view-{i}.png') as image:
            canvas.paste(image.convert('RGB'),(i*512,30))
        draw.text((i*512+15,10),f'VIEW {i+1}',fill='white')
    path=output/'mesh-preview.png'
    canvas.save(path)
    return {"available": True, "path": str(path)}
