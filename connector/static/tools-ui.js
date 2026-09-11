'use strict';
// Shared tools: the same actions in every chat, whatever agent runs it.
// The connector executes them, so Codex and Claude sessions get the result too.
let toolCatalogue = [];

document.body.insertAdjacentHTML('beforeend', `
<dialog id="tools-dialog"><div class="section-heading"><div><span class="eyebrow">ОБЩИЕ ИНСТРУМЕНТЫ</span><h2>Инструменты чата</h2></div><button type="button" data-close-tools>✕</button></div>
<p class="muted" id="tools-scope">Работают в любом чате: DeepSeek, Codex и Claude. Результат приходит сюда же, в ленту.</p>
<div id="tools-list"></div></dialog>`);
document.querySelectorAll('[data-close-tools]').forEach(b => b.onclick = () => $('tools-dialog').close());

const toolsButton = el('button', '⚒');
toolsButton.id = 'composer-tools';
toolsButton.type = 'button';
toolsButton.title = 'Общие инструменты: генерация изображения, скиллы';
$('attach-button').after(toolsButton);

function renderTools() {
  $('tools-list').replaceChildren(...toolCatalogue.map(item => {
    const card = el('article', undefined, 'tool-card');
    const title = el('h3', item.title);
    if (item.billing) title.append(el('span', item.billing === 'free-farm' ? 'бесплатно · ферма' : item.billing, 'tool-billing'));
    card.append(title, el('p', item.description, 'muted'));
    const form = el('form');
    const inputs = {};
    for (const field of item.fields || []) {
      const label = el('label', field.label);
      const input = el('input');
      input.required = !!field.required;
      input.maxLength = 2000;
      label.append(input);
      inputs[field.name] = input;
      form.append(label);
    }
    const run = el('button', item.background ? 'Запустить в фоне' : 'Выполнить', 'primary tool-run');
    form.append(run);
    form.onsubmit = handle(async (event) => {
      event.preventDefault();
      if (!current) throw new Error('Сначала выберите или создайте чат');
      run.disabled = true;
      try {
        const args = Object.fromEntries(Object.entries(inputs).map(([k, v]) => [k, v.value]));
        const result = await api(`/api/sessions/${current.id}/tools/${item.name}`, {method: 'POST', body: {args}});
        toast(result.started ? 'Задача запущена: результат придёт в этот чат'
                             : result.path ? 'Готово: ' + result.path : 'Инструмент выполнен');
        if (result.reference) {
          const box = $('message');
          box.value = (box.value ? box.value.replace(/\s*$/, '\n\n') : '') + result.reference + '\n';
          box.dispatchEvent(new Event('input'));
        }
        $('tools-dialog').close();
      } finally {
        run.disabled = false;
      }
    });
    card.append(form);
    return card;
  }));
  if (!toolCatalogue.length) $('tools-list').append(el('p', 'Общие инструменты не подключены.', 'muted'));
}

async function openTools() {
  toolCatalogue = await api('/api/tools');
  renderTools();
  const dialog = $('tools-dialog');
  if (dialog.showModal) dialog.showModal(); else dialog.open = true;
}
toolsButton.onclick = handle(openTools);
