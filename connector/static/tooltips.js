'use strict';
/* Custom tooltips: one styled layer instead of native title bubbles.
   Attach with data-tip="key" (dictionary) or data-tip-text / title, and keep multi-line text. */
(() => {
const TIPS = {
  version: 'Версия сборки AIGent. Обновления проверяются в настройках и в меню «Справка».',
  'update-banner': 'Опубликована новая версия AIGent. Скачивание идёт с публичной страницы выпусков; установка запускается только с вашего подтверждения.',
  'update-channel': 'Репозиторий GitHub, из публичных выпусков которого берётся обновление. Пустое поле отключает проверку.',
  'auto-update': 'Проверять выпуски при запуске приложения и раз в сутки. Никакие данные на сервер не отправляются.',
  'context-budget': 'Текстовый бюджет контекста в символах. 1 000 000 символов — примерно 250 тысяч токенов. При переполнении самые старые ходы исключаются из запроса, история чата сохраняется.',
  attachments: 'Файлы и изображения добавляются к следующему сообщению.',
  'auto-approve': 'Автоматически применять все правки и команды терминала этой сессии без подтверждения.'
};
const DELAY = 160, MARGIN = 8;
let layer = null, anchor = null, timer = 0, watched = new Set();
function ensureLayer() {
  if (layer) return layer;
  layer = document.createElement('div');
  layer.id = 'aigent-tooltip';
  layer.className = 'aigent-tooltip';
  layer.setAttribute('role', 'tooltip');
  layer.hidden = true;
  document.body.append(layer);
  return layer;
}
function textFor(element) {
  const key = element.dataset.tip;
  if (key && TIPS[key]) return TIPS[key];
  return element.dataset.tipText || '';
}
function place() {
  if (!anchor || !layer) return;
  const box = anchor.getBoundingClientRect(), own = layer.getBoundingClientRect();
  const above = box.top - own.height - MARGIN < 0;
  const top = above ? box.bottom + MARGIN : box.top - own.height - MARGIN;
  const left = Math.min(Math.max(MARGIN, box.left + box.width / 2 - own.width / 2),
                        window.innerWidth - own.width - MARGIN);
  layer.style.top = Math.round(top) + 'px';
  layer.style.left = Math.round(left) + 'px';
}
function show(element) {
  const text = textFor(element);
  if (!text) return;
  anchor = element;
  const node = ensureLayer();
  node.textContent = text;
  node.hidden = false;
  node.classList.toggle('multiline', text.includes('\n'));
  anchor.setAttribute('aria-describedby', node.id);
  place();
  node.classList.add('visible');
}
function hide() {
  clearTimeout(timer);
  if (anchor) anchor.removeAttribute('aria-describedby');
  anchor = null;
  if (layer) { layer.classList.remove('visible'); layer.hidden = true; }
}
function schedule(element, delay) {
  clearTimeout(timer);
  timer = setTimeout(() => { if (element.isConnected) show(element); }, delay);
}
function target(node) {
  if (!(node instanceof Element) || node === layer || node.closest('#aigent-tooltip')) return null;
  const found = node.closest('[data-tip], [data-tip-text]');
  return found && (found.dataset.tip || found.dataset.tipText) ? found : null;
}
// Native titles become custom tooltips, so every existing hint gets the same styling.
function adopt(root) {
  const scope = root && root.querySelectorAll ? root : document;
  const list = scope.querySelectorAll('[title]');
  for (const element of list) {
    const text = element.getAttribute('title');
    if (!text) continue;
    element.removeAttribute('title');
    if (!element.dataset.tipText) element.dataset.tipText = text;
  }
}
function watch(root) {
  const scope = root && root.nodeType === 1 ? root : document.body;
  if (watched.has(scope)) return;
  watched.add(scope);
  adopt(scope);
  new MutationObserver((records) => {
    for (const record of records) {
      if (record.type === 'attributes') { adopt(record.target); continue; }
      for (const node of record.addedNodes) if (node.nodeType === 1) adopt(node);
    }
  }).observe(scope, { childList: true, subtree: true, attributes: true, attributeFilter: ['title'] });
}
document.addEventListener('pointerover', (event) => {
  const found = target(event.target);
  if (!found || found === anchor) return;
  hide();
  schedule(found, DELAY);
});
document.addEventListener('pointerout', (event) => {
  const found = target(event.target);
  if (found && found !== target(event.relatedTarget)) hide();
});
document.addEventListener('focusin', (event) => {
  const found = target(event.target);
  if (found && found !== anchor && found.matches('button, a, input, select, textarea, [tabindex]')) { hide(); schedule(found, 0); }
});
document.addEventListener('focusout', hide);
document.addEventListener('keydown', (event) => { if (event.key === 'Escape') hide(); });
window.addEventListener('scroll', hide, true);
window.addEventListener('resize', hide);
window.addEventListener('blur', hide);
document.addEventListener('click', hide);
window.aigentTips = {
  register: (dictionary) => { Object.assign(TIPS, dictionary); },
  attach: (element, text) => { if (element) element.dataset.tipText = text; },
  text: (key) => TIPS[key] || '',
  adopt, watch, show, hide
};
})();
