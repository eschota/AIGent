// Always-on 3D dock for glTF/GLB models produced in a session.
//
// The look follows the Gravity House server graphics record (/api/graphics/preset): sun direction and
// colour, ambient/sky colours, ACES tone mapping, SSAO, bloom and vignette all read their values from
// that record, and quality levels 1/2/3 are the ones the server derived. This is a native three.js
// renderer, not a Unity WebGL build, so the match is a deliberate reconstruction, not a port.
//
// Everything is built with DOM calls: no markup is ever created from model or event data.
import * as THREE from '/static/vendor/three/lib/three.module.js';
import { OrbitControls } from '/static/vendor/three/addons/controls/OrbitControls.js';
import { GLTFLoader } from '/static/vendor/three/addons/loaders/GLTFLoader.js';
import { DRACOLoader } from '/static/vendor/three/addons/loaders/DRACOLoader.js';
import { KTX2Loader } from '/static/vendor/three/addons/loaders/KTX2Loader.js';
import { RoomEnvironment } from '/static/vendor/three/addons/environments/RoomEnvironment.js';
import { EffectComposer } from '/static/vendor/three/addons/postprocessing/EffectComposer.js';
import { RenderPass } from '/static/vendor/three/addons/postprocessing/RenderPass.js';
import { ShaderPass } from '/static/vendor/three/addons/postprocessing/ShaderPass.js';
import { OutputPass } from '/static/vendor/three/addons/postprocessing/OutputPass.js';
import { UnrealBloomPass } from '/static/vendor/three/addons/postprocessing/UnrealBloomPass.js';
import { SSAOPass } from '/static/vendor/three/addons/postprocessing/SSAOPass.js';
import { SMAAPass } from '/static/vendor/three/addons/postprocessing/SMAAPass.js';
import { FXAAPass } from '/static/vendor/three/addons/postprocessing/FXAAPass.js';

const VENDOR = '/static/vendor/three/addons/';
const STORE_KEY = 'aigent.viewer3d';
const MODEL_RE = /\.(glb|gltf)$/i;
const IMAGE_RE = /\.(png|jpe?g|webp|bmp|tiff?)$/i;
// Unity directional-light intensity and three.js irradiance are different scales; this gain matches the
// Gravity House reference look approximately and could not be checked against the Unity build here.
const SUN_GAIN = 3.0;

const state = {
  sid: '', models: [], current: '', sidecar: null, preset: null, quality: 2, channel: 'lit',
  post: true, grid: true, autoload: true, wire: false, loading: false, ready: false, error: '',
  thumbs: new Map(),
};
const view = {}; // three.js objects, created once

// ------------------------------------------------------------------ small helpers
const node = (tag, text, cls) => {
  const n = document.createElement(tag);
  if (text !== undefined) n.textContent = text;
  if (cls) n.className = cls;
  return n;
};
const button = (text, title, onclick, cls) => {
  const b = node('button', text, cls);
  b.type = 'button';
  if (title) b.title = title;
  b.onclick = onclick;
  return b;
};
function notify(message) {
  if (typeof window.toast === 'function') window.toast(message);
  else console.warn('[viewer3d]', message);
}
function saved() {
  try { return JSON.parse(localStorage.getItem(STORE_KEY) || '{}') || {}; } catch { return {}; }
}
function persist(patch) {
  try { localStorage.setItem(STORE_KEY, JSON.stringify({ ...saved(), ...patch })); } catch { /* private mode */ }
}
async function request(path, options = {}) {
  const headers = { 'X-Requested-With': 'DeepSeekIDE', ...(options.headers || {}) };
  if (options.body && typeof options.body !== 'string') {
    headers['Content-Type'] = 'application/json';
    options = { ...options, body: JSON.stringify(options.body) };
  }
  const response = await fetch(path, { ...options, headers });
  const data = await response.json().catch(() => ({ detail: response.statusText }));
  if (!response.ok) throw new Error(typeof data.detail === 'string' ? data.detail : 'Запрос не выполнен');
  return data;
}
function sessionId() {
  try {
    if (typeof current !== 'undefined' && current && current.id) return current.id; // eslint-disable-line no-undef
  } catch { /* app.js may not have run yet */ }
  try { return localStorage.getItem('aigent.selectedSession') || ''; } catch { return ''; }
}
const assetUrl = (sid, path) => `/api/sessions/${sid}/asset/${path.split('/').map(encodeURIComponent).join('/')}`;
const shortName = (path) => String(path).split('/').pop().replace(/^[0-9a-f]{8}-/, '');
const bytes = (size) => (size > 1048576 ? (size / 1048576).toFixed(1) + ' MB' : Math.max(1, Math.round(size / 1024)) + ' KB');
const number = (value) => (value == null ? '—' : Number(value).toLocaleString('ru-RU'));

// ------------------------------------------------------------------ shaders
const VIGNETTE_SHADER = {
  name: 'AIGentVignette',
  uniforms: {
    tDiffuse: { value: null }, uColor: { value: new THREE.Color(0.22, 0.18, 0.22) },
    uCenter: { value: new THREE.Vector2(0.5, 0.5) }, uIntensity: { value: 0.44 },
    uSmoothness: { value: 0.2 }, uAspect: { value: 1 }, uRounded: { value: 0 },
  },
  vertexShader: `varying vec2 vUv;
    void main(){ vUv = uv; gl_Position = projectionMatrix * modelViewMatrix * vec4(position, 1.0); }`,
  fragmentShader: `uniform sampler2D tDiffuse; uniform vec3 uColor; uniform vec2 uCenter;
    uniform float uIntensity; uniform float uSmoothness; uniform float uAspect; uniform float uRounded;
    varying vec2 vUv;
    void main(){
      vec4 texel = texture2D(tDiffuse, vUv);
      vec2 offset = vUv - uCenter;
      if (uRounded < 0.5) offset.x *= uAspect;
      float radius = length(offset) * 1.41421356;
      float edge = clamp(1.0 - uIntensity, 0.0, 1.0);
      float mask = smoothstep(edge, edge + max(uSmoothness, 0.001), radius);
      gl_FragColor = vec4(mix(texel.rgb, uColor, mask * uIntensity), texel.a);
    }`,
};
const CHANNEL_VERTEX = `varying vec2 vUv;
  void main(){ vUv = uv; gl_Position = projectionMatrix * modelViewMatrix * vec4(position, 1.0); }`;
const CHANNEL_FRAGMENT = `uniform sampler2D tMap; uniform vec3 uMask; uniform float uScalar;
  uniform float uHasMap; uniform float uAlpha; varying vec2 vUv;
  void main(){
    float value = uScalar;
    if (uHasMap > 0.5) {
      vec4 texel = texture2D(tMap, vUv);
      value = uAlpha > 0.5 ? texel.a : dot(texel.rgb, uMask);
    }
    gl_FragColor = vec4(vec3(value), 1.0);
  }`;

// ------------------------------------------------------------------ dock DOM
const dock = node('aside', undefined, 'viewer3d-dock');
dock.id = 'viewer3d-dock';
dock.tabIndex = 0;
const header = node('div', undefined, 'viewer3d-header');
const title = node('strong', '3D');
const qualityRow = node('div', undefined, 'viewer3d-quality');
const channelSelect = node('select', undefined, 'viewer3d-channel');
const badge = node('span', '', 'viewer3d-nomap');
badge.hidden = true;
const toggles = node('div', undefined, 'viewer3d-toggles');
const stage = node('div', undefined, 'viewer3d-stage');
const overlay = node('div', '', 'viewer3d-stats');
const message = node('div', '', 'viewer3d-message');
const sidebar = node('div', undefined, 'viewer3d-list');
const provenance = node('div', undefined, 'viewer3d-provenance');
const body = node('div', undefined, 'viewer3d-body');
const grip = node('div', undefined, 'viewer3d-grip');
grip.title = 'Потяните, чтобы изменить высоту панели';

const CHANNELS = [
  { id: 'lit', label: 'Lit · как в движке' },
  { id: 'albedo', label: 'Albedo / BaseColor' },
  { id: 'roughness', label: 'Roughness' },
  { id: 'metallic', label: 'Metallic' },
  { id: 'normal', label: 'Normals' },
  { id: 'emissive', label: 'Emissive' },
  { id: 'ao', label: 'Ambient Occlusion' },
  { id: 'uv', label: 'UV checker' },
  { id: 'wire', label: 'Wireframe' },
  { id: 'vertex', label: 'Vertex colors' },
  { id: 'alpha', label: 'Alpha' },
];

function buildDock() {
  for (const level of ['1', '2', '3']) {
    qualityRow.append(button(level, `Качество ${level} (клавиша ${level})`, () => setQuality(Number(level)), 'q' + level));
  }
  CHANNELS.forEach((channel, index) => {
    const option = node('option', channel.label + (index < 9 ? `  ·  Alt+${index + 1}` : ''));
    option.value = channel.id;
    channelSelect.append(option);
  });
  channelSelect.title = 'Канал просмотра материалов (Alt+1 … Alt+9)';
  channelSelect.onchange = () => setChannel(channelSelect.value);

  toggles.append(
    button('P', 'Постобработка вкл/выкл (P)', () => setPost(!state.post), 'toggle-post'),
    button('G', 'Сетка и пол (G)', () => setGrid(!state.grid), 'toggle-grid'),
    button('W', 'Каркас поверх модели (W)', () => setWire(!state.wire), 'toggle-wire'),
    button('F', 'Вписать модель в кадр (F)', () => frameModel(), ''),
    button('R', 'Сбросить камеру (R)', () => resetCamera(), ''),
    button('⛶', 'Полный экран', () => toggleFull(), 'toggle-full'),
    button('✕', 'Свернуть панель', () => setOpen(false), 'toggle-close'),
  );
  const autoLabel = node('label', undefined, 'viewer3d-auto');
  const autoInput = node('input');
  autoInput.type = 'checkbox';
  autoInput.checked = state.autoload;
  autoInput.onchange = () => { state.autoload = autoInput.checked; persist({ autoload: state.autoload }); };
  autoLabel.append(autoInput, node('span', 'Автопоказ'));
  autoLabel.title = 'Новая модель в сессии открывается в просмотре автоматически';

  header.append(title, qualityRow, channelSelect, badge, autoLabel, toggles);
  stage.append(overlay, message);
  body.append(sidebar, stage, provenance);
  dock.append(grip, header, body);
  document.querySelector('.workgrid')?.append(dock) || document.body.append(dock);

  const stored = saved();
  state.quality = [1, 2, 3].includes(stored.quality) ? stored.quality : 2;
  state.channel = CHANNELS.some((c) => c.id === stored.channel) ? stored.channel : 'lit';
  state.autoload = stored.autoload !== false;
  state.grid = stored.grid !== false;
  state.post = stored.post !== false;
  autoInput.checked = state.autoload;
  channelSelect.value = state.channel;
  if (stored.height) document.documentElement.style.setProperty('--viewer3d-height', stored.height + 'px');
  markToggles();
  setOpen(stored.open !== false, true);
}

function markToggles() {
  for (const element of qualityRow.children) {
    element.classList.toggle('active', element.textContent === String(state.quality));
  }
  toggles.querySelector('.toggle-post')?.classList.toggle('active', state.post);
  toggles.querySelector('.toggle-grid')?.classList.toggle('active', state.grid);
  toggles.querySelector('.toggle-wire')?.classList.toggle('active', state.wire);
}

// ------------------------------------------------------------------ open / layout
// Nothing is created while the login gate is up: no WebGL context and no request before the workspace exists.
function signedIn() {
  const workspace = document.getElementById('workspace');
  return !!workspace && !workspace.hidden;
}
let waiting = 0;
function setOpen(open, quiet) {
  dock.classList.toggle('open', !!open);
  dock.hidden = !open;
  document.body.classList.toggle('viewer3d-open', !!open);
  persist({ open: !!open });
  if (!open) {
    clearInterval(waiting);
    waiting = 0;
    return;
  }
  if (!signedIn()) {
    showMessage('Войдите в рабочее пространство, чтобы открыть модели.');
    if (!waiting) {
      waiting = setInterval(() => {
        if (!signedIn() || dock.hidden) return;
        clearInterval(waiting);
        waiting = 0;
        setOpen(true, true);
      }, 1500);
    }
    return;
  }
  mount().then(() => { refreshModels().catch(() => {}); }).catch((error) => showMessage(error.message));
  if (!quiet) dock.focus();
}
function toggleFull() {
  dock.classList.toggle('full');
  resize();
}
let dragging = 0;
grip.addEventListener('pointerdown', (event) => {
  dragging = event.clientY;
  grip.setPointerCapture(event.pointerId);
});
grip.addEventListener('pointermove', (event) => {
  if (!dragging) return;
  const height = Math.min(Math.max(dock.getBoundingClientRect().height + (dragging - event.clientY), 240), window.innerHeight - 120);
  document.documentElement.style.setProperty('--viewer3d-height', height + 'px');
  dragging = event.clientY;
  resize();
});
for (const name of ['pointerup', 'pointercancel']) {
  grip.addEventListener(name, () => {
    if (!dragging) return;
    dragging = 0;
    persist({ height: Math.round(dock.getBoundingClientRect().height) });
  });
}

function showMessage(text) {
  message.textContent = text || '';
  message.hidden = !text;
}

// ------------------------------------------------------------------ renderer, kept alive
async function loadPreset() {
  if (state.preset) return state.preset;
  try {
    state.preset = await request('/api/graphics/preset');
  } catch (error) {
    notify('Пресет графики недоступен: ' + error.message);
    throw error;
  }
  return state.preset;
}

function gradientTexture(settings) {
  // Sky gradient from the server record: ground → horizon → zenith, as an equirectangular texture,
  // so it serves both as the background and as the source of ambient reflections.
  const canvas = document.createElement('canvas');
  canvas.width = 8;
  canvas.height = 256;
  const context = canvas.getContext('2d');
  const gradient = context.createLinearGradient(0, 0, 0, 256);
  const rgb = (c) => `rgb(${Math.round(c[0] * 255)},${Math.round(c[1] * 255)},${Math.round(c[2] * 255)})`;
  gradient.addColorStop(0, rgb(settings.environment.zenith));
  gradient.addColorStop(0.48, rgb(settings.environment.horizon));
  gradient.addColorStop(0.52, rgb(settings.environment.horizon));
  gradient.addColorStop(1, rgb(settings.environment.ground));
  context.fillStyle = gradient;
  context.fillRect(0, 0, 8, 256);
  const texture = new THREE.CanvasTexture(canvas);
  texture.mapping = THREE.EquirectangularReflectionMapping;
  texture.colorSpace = THREE.SRGBColorSpace;
  texture.needsUpdate = true;
  return texture;
}

function checkerTexture() {
  const canvas = document.createElement('canvas');
  canvas.width = canvas.height = 256;
  const context = canvas.getContext('2d');
  for (let y = 0; y < 8; y++) {
    for (let x = 0; x < 8; x++) {
      context.fillStyle = (x + y) % 2 ? '#d8dde2' : '#5b6873';
      context.fillRect(x * 32, y * 32, 32, 32);
    }
  }
  context.strokeStyle = '#39e0a0';
  context.lineWidth = 2;
  context.strokeRect(1, 1, 254, 254);
  const texture = new THREE.CanvasTexture(canvas);
  texture.wrapS = texture.wrapT = THREE.RepeatWrapping;
  texture.colorSpace = THREE.SRGBColorSpace;
  return texture;
}

function sunDirection(rotation) {
  // Unity Euler degrees (x = pitch, y = yaw) applied to the light's forward axis (0,0,1) in a
  // left-handed frame; three.js is right-handed, so the Z component flips.
  const pitch = THREE.MathUtils.degToRad(rotation[0] || 0);
  const yaw = THREE.MathUtils.degToRad(rotation[1] || 0);
  return new THREE.Vector3(
    Math.cos(pitch) * Math.sin(yaw),
    -Math.sin(pitch),
    -Math.cos(pitch) * Math.cos(yaw),
  ).normalize();
}

async function mount() {
  if (view.renderer) return view;
  if (state.error) throw new Error(state.error);
  const settings = (await loadPreset()).settings;
  let renderer;
  try {
    // preserveDrawingBuffer keeps the last frame readable, which is what the list thumbnails capture.
    renderer = new THREE.WebGLRenderer({ antialias: true, alpha: false, preserveDrawingBuffer: true,
                                         powerPreference: 'high-performance' });
  } catch (error) {
    state.error = 'WebGL недоступен в этом браузере: просмотр 3D выключен. ' + error.message;
    showMessage(state.error);
    throw new Error(state.error);
  }
  if (!renderer.getContext()) {
    state.error = 'WebGL-контекст не создан: просмотр 3D выключен.';
    showMessage(state.error);
    throw new Error(state.error);
  }
  renderer.domElement.className = 'viewer3d-canvas';
  renderer.domElement.tabIndex = 0;
  stage.insertBefore(renderer.domElement, overlay);

  const scene = new THREE.Scene();
  const camera = new THREE.PerspectiveCamera(45, 1, 0.01, 1000);
  camera.position.set(2.2, 1.6, 2.8);
  const controls = new OrbitControls(camera, renderer.domElement);
  controls.enableDamping = true;
  controls.dampingFactor = 0.08;
  controls.screenSpacePanning = true;

  const sky = gradientTexture(settings);
  const pmrem = new THREE.PMREMGenerator(renderer);
  const flatEnvironment = pmrem.fromEquirectangular(sky).texture;
  const roomEnvironment = pmrem.fromScene(new RoomEnvironment(), 0.04).texture;

  const sun = new THREE.DirectionalLight(0xffffff, 1);
  sun.name = 'gravityhouse-sun';
  const sunTarget = new THREE.Object3D();
  scene.add(sun, sunTarget);
  sun.target = sunTarget;
  const ambient = new THREE.HemisphereLight(0xffffff, 0xffffff, 0.4);
  scene.add(ambient);

  const ground = new THREE.Mesh(new THREE.PlaneGeometry(200, 200), new THREE.ShadowMaterial({ opacity: 0.35 }));
  ground.rotation.x = -Math.PI / 2;
  ground.receiveShadow = true;
  const grid = new THREE.GridHelper(10, 20, 0x39e0a0, 0x33404a);
  grid.material.transparent = true;
  grid.material.opacity = 0.45;
  const helpers = new THREE.Group();
  helpers.add(ground, grid);
  scene.add(helpers);

  const models = new THREE.Group();
  scene.add(models);

  const draco = new DRACOLoader().setDecoderPath(VENDOR + 'libs/draco/gltf/');
  const ktx2 = new KTX2Loader().setTranscoderPath(VENDOR + 'libs/basis/').detectSupport(renderer);
  const loader = new GLTFLoader().setDRACOLoader(draco).setKTX2Loader(ktx2);

  Object.assign(view, {
    renderer, scene, camera, controls, sun, sunTarget, ambient, helpers, ground, grid, models,
    loader, draco, ktx2, sky, pmrem, flatEnvironment, roomEnvironment, checker: checkerTexture(),
    composer: null, passes: {}, clock: new THREE.Clock(), frames: 0, fps: 0, last: performance.now(),
    mixer: null,
  });
  applyPreset();
  new ResizeObserver(() => resize()).observe(stage);
  resize();
  renderer.setAnimationLoop(tick);
  state.ready = true;
  showMessage(state.models.length ? '' : 'Модели .glb/.gltf этой сессии появятся слева.');
  return view;
}

function applyPreset() {
  const { settings, quality } = state.preset;
  const level = quality[String(state.quality)];
  const { renderer, scene, sun, sunTarget, ambient } = view;
  const environment = settings.environment;

  renderer.setPixelRatio(Math.min(window.devicePixelRatio || 1, level.pixel_ratio || (window.devicePixelRatio || 1)));
  renderer.toneMapping = level.tonemapping === 'aces' ? THREE.ACESFilmicToneMapping : THREE.NoToneMapping;
  renderer.toneMappingExposure = Math.pow(2, environment.exposure || 0);
  renderer.shadowMap.enabled = !!level.shadows;
  renderer.shadowMap.type = level.shadow_type === 'pcfsoft' ? THREE.PCFSoftShadowMap : THREE.PCFShadowMap;

  scene.background = environment.background_mode === 0
    ? view.sky
    : new THREE.Color(...environment.background);
  scene.environment = level.environment === 'room' ? view.roomEnvironment : view.flatEnvironment;
  scene.environmentIntensity = environment.ambient_intensity ?? 1;

  sun.visible = !!settings.sun.enabled;
  sun.color.setRGB(...settings.sun.color);
  sun.intensity = (settings.sun.intensity || 0) * SUN_GAIN;
  const direction = sunDirection(settings.sun.rotation);
  sun.position.copy(direction).multiplyScalar(-12);
  sunTarget.position.set(0, 0, 0);
  sun.castShadow = !!level.shadows;
  if (level.shadows) {
    const size = level.shadow_map_size || 2048;
    sun.shadow.mapSize.set(size, size);
    sun.shadow.camera.near = 0.1;
    sun.shadow.camera.far = (level.shadow_distance || 40) * 1.5;
    const half = Math.max(2, (level.shadow_distance || 40) / 8);
    Object.assign(sun.shadow.camera, { left: -half, right: half, top: half, bottom: -half });
    sun.shadow.bias = -0.0005 * (settings.sun.shadow_bias ?? 0.45);
    sun.shadow.normalBias = 0.02;
    if ('intensity' in sun.shadow) sun.shadow.intensity = settings.sun.shadow_strength ?? 1;
    sun.shadow.camera.updateProjectionMatrix();
    sun.shadow.map?.dispose();
    sun.shadow.map = null;
  }
  ambient.color.setRGB(...environment.zenith);
  ambient.groundColor.setRGB(...environment.ground);
  ambient.intensity = 0.35 * (environment.ambient_intensity ?? 1);
  view.ground.visible = state.grid && !!level.shadows;
  view.grid.visible = state.grid;
  buildComposer();
  frameShadow();
}

function buildComposer() {
  const { renderer, scene, camera } = view;
  const settings = state.preset.settings;
  const level = state.preset.quality[String(state.quality)];
  if (view.composer) {
    view.composer.dispose();
    view.composer = null;
    view.passes = {};
  }
  if (!state.post || !level.post) return;
  const width = Math.max(1, stage.clientWidth), height = Math.max(1, stage.clientHeight);
  const composer = new EffectComposer(renderer);
  composer.setSize(width, height);
  composer.setPixelRatio(renderer.getPixelRatio());
  if (level.ssao) {
    const ssao = new SSAOPass(scene, camera, width, height);
    // Unity radius is in metres and falloff is a distance, so both map onto the SSAO kernel directly.
    ssao.kernelRadius = Math.max(0.05, settings.ssao.radius * 20);
    ssao.minDistance = 0.0005;
    ssao.maxDistance = Math.max(0.02, settings.ssao.radius);
    ssao.output = SSAOPass.OUTPUT.Default;
    composer.addPass(ssao);
    view.passes.ssao = ssao;
  } else {
    composer.addPass(new RenderPass(scene, camera));
  }
  if (level.bloom) {
    const bloom = new UnrealBloomPass(new THREE.Vector2(width, height),
      settings.bloom.intensity, settings.bloom.scatter, settings.bloom.threshold);
    composer.addPass(bloom);
    view.passes.bloom = bloom;
  }
  if (level.vignette) {
    const vignette = new ShaderPass(VIGNETTE_SHADER);
    vignette.uniforms.uColor.value.setRGB(...settings.vignette.color);
    vignette.uniforms.uCenter.value.set(settings.vignette.center[0], settings.vignette.center[1]);
    vignette.uniforms.uIntensity.value = settings.vignette.intensity;
    vignette.uniforms.uSmoothness.value = settings.vignette.smoothness;
    vignette.uniforms.uRounded.value = settings.vignette.rounded ? 1 : 0;
    vignette.uniforms.uAspect.value = width / Math.max(1, height);
    composer.addPass(vignette);
    view.passes.vignette = vignette;
  }
  if (level.aa_pass === 'smaa') composer.addPass(new SMAAPass());
  else if (level.aa_pass === 'fxaa') composer.addPass(new FXAAPass());
  composer.addPass(new OutputPass());
  view.composer = composer;
}

function resize() {
  if (!view.renderer) return;
  const width = Math.max(1, stage.clientWidth), height = Math.max(1, stage.clientHeight);
  view.renderer.setSize(width, height, false);
  view.camera.aspect = width / height;
  view.camera.updateProjectionMatrix();
  if (view.composer) {
    view.composer.setSize(width, height);
    if (view.passes.vignette) view.passes.vignette.uniforms.uAspect.value = width / height;
  }
}

function tick() {
  const delta = view.clock.getDelta();
  view.controls.update();
  if (view.mixer) view.mixer.update(delta);
  if (view.composer) view.composer.render(delta);
  else view.renderer.render(view.scene, view.camera);
  view.frames += 1;
  const now = performance.now();
  if (now - view.last >= 500) {
    view.fps = Math.round((view.frames * 1000) / (now - view.last));
    view.frames = 0;
    view.last = now;
    updateStats();
  }
}

function updateStats() {
  const info = view.renderer.info;
  overlay.textContent = `${view.fps} fps · ${number(info.render.triangles)} тр. · ${info.render.calls} draw call`
    + ` · тексутр ${info.memory.textures} · Q${state.quality}`;
}

// ------------------------------------------------------------------ model loading
function disposeObject(root) {
  root.traverse((child) => {
    if (child.isMesh || child.isSkinnedMesh || child.isPoints || child.isLine) {
      child.geometry?.dispose();
      const originals = child.userData.viewerOriginal ? [child.userData.viewerOriginal] : [];
      for (const material of [...toArray(child.material), ...toArray(originals)]) {
        if (!material) continue;
        for (const key of Object.keys(material)) {
          const value = material[key];
          if (value && value.isTexture) value.dispose();
        }
        material.dispose();
      }
    }
  });
}
const toArray = (value) => (Array.isArray(value) ? value : value ? [value] : []);

function clearModel() {
  if (view.mixer) {
    view.mixer.stopAllAction();
    view.mixer = null;
  }
  for (const child of [...view.models.children]) {
    view.models.remove(child);
    disposeObject(child);
  }
}

async function loadModel(path) {
  await mount();
  if (!MODEL_RE.test(path)) {
    notify('Встроенный просмотр работает для .glb и .gltf');
    return;
  }
  const sid = state.sid || sessionId();
  if (!sid) {
    showMessage('Сначала выберите чат.');
    return;
  }
  state.loading = true;
  showMessage('Загрузка модели ' + shortName(path) + '…');
  // A .gltf names its buffers and textures by relative URL, so the loader resolves them against the
  // model's own folder on the asset route.
  const folder = path.includes('/') ? path.slice(0, path.lastIndexOf('/') + 1) : '';
  view.loader.setResourcePath(`/api/sessions/${sid}/asset/${folder.split('/').map(encodeURIComponent).join('/')}`);
  try {
    const gltf = await view.loader.loadAsync(assetUrl(sid, path));
    clearModel();
    const root = gltf.scene || gltf.scenes[0];
    root.traverse((child) => {
      if (child.isMesh || child.isSkinnedMesh) {
        child.castShadow = true;
        child.receiveShadow = true;
        child.userData.viewerOriginal = child.material;
      }
    });
    view.models.add(root);
    if (gltf.animations && gltf.animations.length) {
      view.mixer = new THREE.AnimationMixer(root);
      view.mixer.clipAction(gltf.animations[0]).play();
    }
    state.current = path;
    applyChannel();
    frameModel();
    showMessage('');
    persist({ last: path });
    renderList();
    captureThumb(path);
    await loadSidecar(path);
  } catch (error) {
    showMessage('Не удалось открыть модель: ' + error.message);
    notify('Не удалось открыть модель: ' + error.message);
  } finally {
    state.loading = false;
  }
}

function modelBox() {
  const box = new THREE.Box3();
  if (!view.models.children.length) return null;
  box.setFromObject(view.models, true);
  return box.isEmpty() ? null : box;
}

function frameModel() {
  const box = modelBox();
  if (!box) return;
  const size = box.getSize(new THREE.Vector3());
  const center = box.getCenter(new THREE.Vector3());
  const radius = Math.max(size.length() / 2, 0.001);
  const distance = (radius / Math.sin(THREE.MathUtils.degToRad(view.camera.fov) / 2)) * 1.25;
  view.camera.near = Math.max(distance / 5000, 0.001);
  view.camera.far = distance * 40;
  view.camera.updateProjectionMatrix();
  const direction = new THREE.Vector3(0.75, 0.5, 1).normalize();
  view.camera.position.copy(center).addScaledVector(direction, distance);
  view.controls.target.copy(center);
  view.controls.update();
  view.ground.position.y = box.min.y;
  const grid = Math.max(1, Math.ceil(radius * 4));
  view.grid.scale.setScalar(grid / 10);
  view.grid.position.y = box.min.y + 0.001;
  frameShadow();
}

function frameShadow() {
  const box = modelBox();
  if (!box || !view.sun.castShadow) return;
  const radius = Math.max(box.getSize(new THREE.Vector3()).length() / 2, 0.5);
  const half = Math.min(radius * 1.6, (state.preset.quality[String(state.quality)].shadow_distance || 40) / 2);
  Object.assign(view.sun.shadow.camera, { left: -half, right: half, top: half, bottom: -half });
  view.sun.shadow.camera.updateProjectionMatrix();
  const center = box.getCenter(new THREE.Vector3());
  view.sunTarget.position.copy(center);
  view.sun.position.copy(center).addScaledVector(sunDirection(state.preset.settings.sun.rotation), -Math.max(12, radius * 6));
}

function resetCamera() {
  view.controls.reset?.();
  frameModel();
}

function captureThumb(path) {
  requestAnimationFrame(() => {
    try {
      const source = view.renderer.domElement;
      const canvas = document.createElement('canvas');
      canvas.width = canvas.height = 128;
      const size = Math.min(source.width, source.height);
      canvas.getContext('2d').drawImage(source, (source.width - size) / 2, (source.height - size) / 2,
        size, size, 0, 0, 128, 128);
      state.thumbs.set(path, canvas.toDataURL('image/png'));
      renderList();
    } catch { /* a tainted or zero-sized canvas simply has no thumbnail */ }
  });
}

// ------------------------------------------------------------------ channels
function channelMaterial(original, mesh, channel) {
  const white = new THREE.Color(1, 1, 1);
  const shaded = (map, mask, scalar, alpha) => {
    if (mesh.isSkinnedMesh) {
      // A raw ShaderMaterial cannot skin, so an animated mesh shows the flat scalar value instead.
      return new THREE.MeshBasicMaterial({ color: new THREE.Color(scalar, scalar, scalar), toneMapped: false });
    }
    return new THREE.ShaderMaterial({
      vertexShader: CHANNEL_VERTEX, fragmentShader: CHANNEL_FRAGMENT,
      uniforms: {
        tMap: { value: map || null }, uMask: { value: mask }, uScalar: { value: scalar },
        uHasMap: { value: map ? 1 : 0 }, uAlpha: { value: alpha ? 1 : 0 },
      },
      side: original.side,
    });
  };
  switch (channel) {
    case 'albedo':
      return new THREE.MeshBasicMaterial({
        map: original.map || null, color: original.color ? original.color.clone() : white,
        transparent: original.transparent, alphaTest: original.alphaTest, side: original.side,
        vertexColors: !!original.vertexColors, toneMapped: false,
      });
    case 'roughness':
      return shaded(original.roughnessMap, new THREE.Vector3(0, 1, 0), original.roughness ?? 1);
    case 'metallic':
      return shaded(original.metalnessMap, new THREE.Vector3(0, 0, 1), original.metalness ?? 0);
    case 'ao':
      return shaded(original.aoMap, new THREE.Vector3(1, 0, 0), 1);
    case 'alpha':
      return shaded(original.map, new THREE.Vector3(0, 0, 0), original.opacity ?? 1, true);
    case 'normal':
      return new THREE.MeshNormalMaterial({ side: original.side });
    case 'emissive':
      return new THREE.MeshBasicMaterial({
        map: original.emissiveMap || null,
        color: original.emissive ? original.emissive.clone() : new THREE.Color(0, 0, 0),
        side: original.side, toneMapped: false,
      });
    case 'uv':
      return new THREE.MeshBasicMaterial({ map: view.checker, side: original.side, toneMapped: false });
    case 'wire':
      return new THREE.MeshBasicMaterial({ color: 0x8fe3c0, wireframe: true, toneMapped: false });
    case 'vertex': {
      const coloured = !!mesh.geometry?.getAttribute('color');
      return new THREE.MeshBasicMaterial({
        vertexColors: coloured, color: coloured ? white : new THREE.Color(0.45, 0.45, 0.45),
        side: original.side, toneMapped: false,
      });
    }
    default:
      return null;
  }
}

const CHANNEL_MAPS = { albedo: 'map', roughness: 'roughnessMap', metallic: 'metalnessMap', ao: 'aoMap', emissive: 'emissiveMap' };

function applyChannel() {
  if (!view.models) return;
  let withMap = 0, total = 0;
  view.models.traverse((child) => {
    if (!(child.isMesh || child.isSkinnedMesh)) return;
    const originals = toArray(child.userData.viewerOriginal);
    if (!originals.length) return;
    if (child.userData.viewerSwapped) {
      for (const material of toArray(child.material)) material?.dispose?.();
      child.userData.viewerSwapped = false;
    }
    if (state.channel === 'lit') {
      child.material = Array.isArray(child.userData.viewerOriginal) ? originals : originals[0];
    } else {
      const built = originals.map((original) => channelMaterial(original, child, state.channel) || original);
      child.material = Array.isArray(child.userData.viewerOriginal) ? built : built[0];
      child.userData.viewerSwapped = true;
    }
    const slot = CHANNEL_MAPS[state.channel];
    if (slot) {
      total += originals.length;
      withMap += originals.filter((original) => original && original[slot]).length;
    }
    // The wireframe overlay is independent of the channel: it just switches the material flag.
    for (const material of toArray(child.material)) {
      if (material && 'wireframe' in material && state.channel !== 'wire') material.wireframe = state.wire;
    }
  });
  const slot = CHANNEL_MAPS[state.channel];
  if (slot && total && !withMap) {
    badge.textContent = 'нет карты · показано скалярное значение материала';
    badge.hidden = false;
  } else if (slot && withMap < total) {
    badge.textContent = `карта есть у ${withMap} из ${total} материалов`;
    badge.hidden = false;
  } else {
    badge.hidden = true;
  }
  channelSelect.value = state.channel;
}

// ------------------------------------------------------------------ controls
function setQuality(level) {
  if (![1, 2, 3].includes(level)) return;
  state.quality = level;
  persist({ quality: level });
  markToggles();
  if (view.renderer) {
    applyPreset();
    resize();
  }
}
function setChannel(channel) {
  state.channel = channel;
  persist({ channel });
  applyChannel();
}
function setPost(enabled) {
  state.post = enabled;
  persist({ post: enabled });
  markToggles();
  if (view.renderer) {
    buildComposer();
    resize();
  }
}
function setGrid(enabled) {
  state.grid = enabled;
  persist({ grid: enabled });
  markToggles();
  if (view.grid) {
    view.grid.visible = enabled;
    view.ground.visible = enabled && view.sun.castShadow;
  }
}
function setWire(enabled) {
  state.wire = enabled;
  markToggles();
  applyChannel();
}

// Keyboard shortcuts stay inside the panel: the composer and the code editor keep their own keys.
dock.addEventListener('keydown', (event) => {
  if (event.ctrlKey || event.metaKey) return;
  const target = event.target;
  if (target && (target.tagName === 'INPUT' || target.tagName === 'TEXTAREA' || target.tagName === 'SELECT')) return;
  if (event.altKey) {
    const index = Number(event.key) - 1;
    if (index >= 0 && index < CHANNELS.length) {
      setChannel(CHANNELS[index].id);
      event.preventDefault();
    }
    return;
  }
  const key = event.key.toLowerCase();
  if (['1', '2', '3'].includes(key)) setQuality(Number(key));
  else if (key === 'p') setPost(!state.post);
  else if (key === 'g') setGrid(!state.grid);
  else if (key === 'w') setWire(!state.wire);
  else if (key === 'f') frameModel();
  else if (key === 'r') resetCamera();
  else return;
  event.preventDefault();
});

// ------------------------------------------------------------------ model list and provenance
function renderList() {
  sidebar.replaceChildren();
  const heading = node('div', undefined, 'viewer3d-list-head');
  heading.append(node('span', 'Модели сессии'), button('↻', 'Обновить список', () => refreshModels().catch((e) => notify(e.message))));
  sidebar.append(heading);
  if (!state.models.length) {
    sidebar.append(node('p', 'Моделей пока нет. .glb и .gltf появятся здесь автоматически.', 'muted'));
    return;
  }
  for (const item of state.models) {
    const row = node('button', undefined, 'viewer3d-item' + (item.path === state.current ? ' selected' : ''));
    row.type = 'button';
    row.title = item.path + (item.viewable ? '' : '\n' + item.note);
    const thumb = node('span', undefined, 'viewer3d-thumb');
    const picture = state.thumbs.get(item.path);
    if (picture) {
      const image = node('img');
      image.alt = '';
      image.src = picture;
      thumb.append(image);
    } else {
      thumb.textContent = item.format.toUpperCase();
    }
    const meta = node('span', undefined, 'viewer3d-item-meta');
    meta.append(node('strong', shortName(item.name)));
    const stats = item.stats || {};
    meta.append(node('small', [bytes(item.size), stats.triangles ? number(stats.triangles) + ' тр.' : '',
      stats.materials ? stats.materials + ' мат.' : '', item.references ? item.references + ' реф.' : '']
      .filter(Boolean).join(' · ')));
    if (!item.viewable) meta.append(node('small', 'просмотр недоступен', 'viewer3d-warn'));
    row.append(thumb, meta);
    row.onclick = () => {
      if (!item.viewable) {
        notify(item.note);
        return;
      }
      loadModel(item.path);
    };
    sidebar.append(row);
  }
}

async function refreshModels() {
  const sid = sessionId();
  if (!sid) return;
  if (sid !== state.sid) {
    state.sid = sid;
    state.current = '';
    state.thumbs.clear();
    if (view.models) clearModel();
  }
  state.models = await request(`/api/sessions/${sid}/models`);
  renderList();
  if (!state.current && state.models.length && state.autoload) {
    const first = state.models.find((item) => item.viewable);
    if (first) await loadModel(first.path);
  }
}

async function loadSidecar(path) {
  try {
    state.sidecar = await request(`/api/sessions/${state.sid}/models/sidecar?path=${encodeURIComponent(path)}`);
  } catch (error) {
    state.sidecar = null;
    notify('Паспорт модели недоступен: ' + error.message);
  }
  renderProvenance();
}

function field(label, value) {
  const row = node('div', undefined, 'viewer3d-field');
  row.append(node('span', label), node('strong', value === '' || value == null ? '—' : String(value)));
  return row;
}

function renderProvenance() {
  provenance.replaceChildren();
  const data = state.sidecar;
  provenance.append(node('div', 'Паспорт модели', 'viewer3d-list-head'));
  if (!data) {
    provenance.append(node('p', 'Откройте модель, чтобы увидеть её .json с происхождением.', 'muted'));
    return;
  }
  const stats = data.stats || {};
  const source = data.source || {};
  provenance.append(
    field('Файл', data.file), field('Формат', (data.format || '').toUpperCase()),
    field('Размер', bytes(data.size)), field('SHA-256', String(data.sha256 || '').slice(0, 16) + '…'),
    field('Инструмент', source.tool || 'неизвестно'),
    field('Задача', source.job_id || ''), field('Seed', data.seed),
    field('Треугольники', number(stats.triangles)), field('Меши', number(stats.meshes)),
    field('Материалы', number(stats.materials)), field('Текстуры', number(stats.textures)),
    field('Анимации', number(stats.animations)),
    field('Габариты', stats.bounds ? stats.bounds.size.map((v) => v.toFixed(3)).join(' × ') : ''),
    field('Сжатие', [stats.draco ? 'DRACO' : '', stats.ktx2 ? 'KTX2' : ''].filter(Boolean).join(' + ') || 'нет'),
  );

  const promptLabel = node('label', 'Промпт');
  const prompt = node('textarea');
  prompt.rows = 3;
  prompt.value = data.prompt || '';
  promptLabel.append(prompt);
  const tagsLabel = node('label', 'Теги через запятую');
  const tags = node('input');
  tags.value = (data.tags || []).join(', ');
  tagsLabel.append(tags);
  const save = button('Сохранить паспорт', 'Записать изменения в <модель>.json', async () => {
    try {
      state.sidecar = await request(`/api/sessions/${state.sid}/models/sidecar`, {
        method: 'POST',
        body: {
          path: state.current,
          patch: { prompt: prompt.value, tags: tags.value.split(',').map((t) => t.trim()).filter(Boolean) },
        },
      });
      notify('Паспорт модели сохранён');
      renderProvenance();
    } catch (error) { notify(error.message); }
  }, 'primary');
  const copy = button('Скопировать промпт', 'Скопировать промпт в буфер обмена', async () => {
    try {
      await navigator.clipboard.writeText(prompt.value);
      notify('Промпт скопирован');
    } catch { notify('Буфер обмена недоступен в этом окне'); }
  });
  provenance.append(promptLabel, tagsLabel, save, copy);

  const references = node('div', undefined, 'viewer3d-references');
  references.append(node('div', 'Референсы', 'viewer3d-list-head'));
  for (const reference of data.references || []) {
    const card = node('button', undefined, 'viewer3d-reference');
    card.type = 'button';
    card.title = reference.path + (reference.note ? '\n' + reference.note : '');
    const image = node('img');
    image.alt = reference.path;
    image.loading = 'lazy';
    image.src = `/api/sessions/${state.sid}/media?path=${encodeURIComponent(reference.path)}&variant=thumb`;
    card.append(image, node('small', shortName(reference.path)));
    card.onclick = () => window.MediaUI?.open(state.sid, reference.path);
    references.append(card);
  }
  if (!(data.references || []).length) references.append(node('p', 'Исходных изображений пока нет.', 'muted'));
  references.append(button('＋ Добавить референс', 'Выбрать изображение из рабочей папки', addReference));
  provenance.append(references);

  const history = node('details', undefined, 'viewer3d-history');
  history.append(node('summary', 'История изменений · ' + (data.history || []).length));
  for (const entry of [...(data.history || [])].reverse()) {
    history.append(node('div', `${new Date(entry.ts * 1000).toLocaleString()} · ${entry.event} · ${entry.detail || ''}`));
  }
  provenance.append(history);
  const raw = node('details');
  raw.append(node('summary', 'JSON паспорта'), node('pre', JSON.stringify(data, null, 2)));
  provenance.append(raw);
}

async function addReference() {
  if (!state.current) return;
  let files = [];
  try {
    files = await request(`/api/sessions/${state.sid}/files`);
  } catch (error) {
    notify(error.message);
    return;
  }
  const images = files.filter((file) => IMAGE_RE.test(file.path));
  if (!images.length) {
    notify('В рабочей папке нет изображений для референса');
    return;
  }
  const dialog = node('dialog', undefined, 'viewer3d-picker');
  dialog.append(node('h3', 'Выберите исходное изображение'));
  const list = node('div', undefined, 'viewer3d-picker-list');
  for (const file of images.slice(0, 200)) {
    const pick = node('button', undefined, '');
    pick.type = 'button';
    const image = node('img');
    image.alt = file.path;
    image.loading = 'lazy';
    image.src = `/api/sessions/${state.sid}/media?path=${encodeURIComponent(file.path)}&variant=thumb`;
    pick.append(image, node('small', shortName(file.path)));
    pick.onclick = async () => {
      dialog.close();
      try {
        state.sidecar = await request(`/api/sessions/${state.sid}/models/reference`, {
          method: 'POST', body: { path: state.current, image: file.path, note: '' },
        });
        renderProvenance();
        notify('Референс добавлен в паспорт модели');
      } catch (error) { notify(error.message); }
    };
    list.append(pick);
  }
  dialog.append(list, button('Отмена', '', () => dialog.close()));
  dialog.addEventListener('close', () => dialog.remove());
  document.body.append(dialog);
  dialog.showModal();
}

// ------------------------------------------------------------------ wiring
buildDock();
renderList();
renderProvenance();

// New models announce themselves through the event stream; app.js re-dispatches every event.
window.addEventListener('aigent:event', (event) => {
  const detail = event.detail || {};
  const payload = (detail.event || {}).payload || {};
  const kind = (detail.event || {}).kind;
  if (kind !== 'media' || !payload.path || !MODEL_RE.test(payload.path)) return;
  refreshModels().then(() => {
    if (state.autoload && !dock.hidden) loadModel(payload.path);
  }).catch(() => {});
});
setInterval(() => {
  if (!dock.hidden && sessionId()) refreshModels().catch(() => {});
}, 20000);

window.Viewer3D = {
  open(sid, path) {
    if (sid && sid !== state.sid) state.sid = sid;
    setOpen(true);
    return loadModel(path);
  },
  show() { setOpen(true); },
  hide() { setOpen(false); },
  refresh() { return refreshModels(); },
  isModel(path) { return MODEL_RE.test(String(path || '')); },
  state,
};
window.dispatchEvent(new CustomEvent('aigent:viewer3d-ready'));
