import asyncio
import io
from pathlib import Path
from unittest.mock import AsyncMock

import pytest
from PIL import Image, ImageDraw

from connector.agent import Agent
from connector.autorig import AutoRigTools
from connector.computer import ComputerTools
from connector.config import Config
from connector.providers import DeepSeek, ProviderError
from connector.store import Store
from connector.vision import image_content


@pytest.fixture
def agent(tmp_path):
    store = Store(tmp_path / 'test.db')
    agent = Agent(Config(tmp_path), store, AsyncMock(), AsyncMock())
    yield agent
    store.db.close()


def test_image_content_uses_pixels_and_not_filename(tmp_path):
    file = tmp_path / 'misnamed.jpg'
    Image.new('RGB', (900, 600), 'blue').save(file, format='PNG')
    result = image_content(file, detail='low')
    assert '512 x 341' in result[0]['text']
    assert result[1]['image_url']['url'].startswith('data:image/png;base64,')
    file.write_text('not an image')
    with pytest.raises(OSError):
        image_content(file)


async def test_nonvision_model_fails_before_spending_tokens(agent):
    agent.config.values.update(model='deepseek-v4-pro', deepseek_key='test-key')
    client = AsyncMock()
    with pytest.raises(ProviderError, match='Flash'):
        await DeepSeek(agent.config, client).complete([{'role':'user','content':[{'type':'image_url','image_url':{'url':'data:image/png;base64,test'}}]}], [], AsyncMock())
    client.stream.assert_not_called()


def test_prepare_reference_and_slab_gate(tmp_path):
    trimesh = pytest.importorskip('trimesh')
    from connector.mesh_quality import mesh_gate, prepare_reference
    source, rgba = tmp_path/'source.png', tmp_path/'rgba.png'
    image = Image.new('RGB',(300,300),'magenta')
    ImageDraw.Draw(image).ellipse((40,40,260,260),fill='gray')
    image.save(source)
    assert prepare_reference(source, rgba)['has_transparency']
    with Image.open(rgba) as im:
        assert im.getpixel((0,0))[3] == 0 and im.getpixel((150,150))[3] == 255
    slab = tmp_path/'slab.glb'
    trimesh.creation.box(extents=[1,1,.01]).export(slab)
    assert not mesh_gate(slab,rgba)['passed']
    solid = tmp_path/'solid.glb'
    trimesh.creation.icosphere().export(solid)
    assert mesh_gate(solid,rgba)['passed']


async def test_autorig_is_opt_in_and_prepared_reference_must_be_seen(agent):
    session = agent.store.resolve(0,0,1)
    api = AsyncMock()
    farm = AutoRigTools(agent,api)
    assert not farm.tools(session)
    with pytest.raises(ValueError,match='Enable'):
        await farm.execute(session,'autorig_generate_model',{'path':'reference.png'})
    farm.enable(session['id'],True)
    with pytest.raises(ValueError,match='Artifact'):
        await farm.download('http://127.0.0.1/private', Path('unused'))
    api.request.assert_not_called()


class FakeDesktop:
    def __init__(self):
        self.color = 'blue'
        self.actions = []

    def info(self, hwnd):
        return {'id':hwnd,'pid':1,'title':'Fixture','rect':[0,0,100,100]}

    def capture(self, hwnd):
        image=Image.new('RGB',(100,100),self.color)
        output=io.BytesIO()
        image.save(output,format='PNG')
        return self.info(hwnd),image.size,output.getvalue()

    def action(self, frame, args):
        self.actions.append(args)


@pytest.mark.parametrize('change',['revoke','pixels','none'])
async def test_computer_action_requires_review_and_current_frame(agent,change):
    session=agent.store.resolve(0,0,1)
    desktop=FakeDesktop()
    computer=ComputerTools(agent,desktop)
    computer.bind(session['id'],123)
    view=await computer.execute(session,'computer_view',{})
    task=asyncio.create_task(computer.execute(session,'computer_action',{'screenshot_id':view['screenshot_id'],'action':'click','x':10,'y':10}))
    await asyncio.sleep(.01)
    assert not desktop.actions and len(agent.approvals)==1
    if change=='revoke':
        computer.bind(session['id'],None)
    if change=='pixels':
        desktop.color='red'
    agent.decide(next(iter(agent.approvals)),True,admin=True)
    if change!='none':
        with pytest.raises(ValueError):
            await task
        assert not desktop.actions
    else:
        assert (await task)['performed']=='click'
        assert len(desktop.actions)==1
        with pytest.raises(ValueError,match='stale'):
            await computer.execute(session,'computer_action',{'screenshot_id':view['screenshot_id'],'action':'click','x':10,'y':10})
