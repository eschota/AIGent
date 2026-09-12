'use strict';
// Interactive media cards: server-side thumbnails and transcodes, a lightbox and inline playback.
// DOM only — nothing here builds markup from event data.
(function () {
  const node = (tag, text, cls) => {
    const n = document.createElement(tag);
    if (text !== undefined) n.textContent = text;
    if (cls) n.className = cls;
    return n;
  };
  const IMAGE = /\.(png|jpe?g|webp|gif|bmp|tiff?)$/i;
  const VIDEO = /\.(mp4|m4v|mov|webm|mkv|avi|ogv)$/i;
  const AUDIO = /\.(mp3|wav|ogg|oga|opus|m4a|aac|flac)$/i;
  const MODEL = /\.(glb|gltf)$/i;
  // Clips in an agent workspace are short turntables and farm frames: looping is the useful default.
  let loopVideos = true;
  try {loopVideos = localStorage.getItem('aigent.media.loop') !== '0';} catch {/* private mode */}
  const DIRECTIONS = {vision: 'Агент посмотрел', out: 'Агент отправил', in: 'Получено',
                      web: 'Вы загрузили', generated: 'Результат генерации'};
  const infoCache = new Map(), readyCache = new Map();
  const headers = {'X-Requested-With': 'DeepSeekIDE'};

  function mediaKind(path) {
    if (IMAGE.test(path)) return 'image';
    if (VIDEO.test(path)) return 'video';
    if (AUDIO.test(path)) return 'audio';
    return '';
  }
  function variantUrl(sid, path, variant) {
    return `/api/sessions/${sid}/media?path=${encodeURIComponent(path)}&variant=${variant}`;
  }
  function fileUrl(sid, path) {
    return `/api/sessions/${sid}/file?path=${encodeURIComponent(path)}`;
  }
  function shortName(path) {
    return String(path).split('/').pop().replace(/^[0-9a-f]{8}-/, '');
  }
  function duration(seconds) {
    if (!seconds && seconds !== 0) return '';
    const total = Math.round(seconds);
    return Math.floor(total / 60) + ':' + String(total % 60).padStart(2, '0');
  }
  function bytes(size) {
    if (!size && size !== 0) return '';
    return size > 1048576 ? (size / 1048576).toFixed(1) + ' MB' : Math.max(1, Math.round(size / 1024)) + ' KB';
  }
  function describe(data) {
    return [data.width && data.height ? `${data.width}×${data.height}` : '',
            duration(data.duration), bytes(data.bytes)].filter(Boolean).join(' · ');
  }
  // One request per (session, file): the card, the lightbox and the meta line share it.
  function info(sid, path) {
    const key = sid + '|' + path;
    if (!infoCache.has(key)) {
      infoCache.set(key, fetch(`/api/sessions/${sid}/media/info?path=${encodeURIComponent(path)}`, {headers})
        .then(r => r.ok ? r.json() : {}).catch(() => ({})));
    }
    return infoCache.get(key);
  }
  // A preview may still be transcoding: the server answers 202 and we poll the same immutable URL.
  function ready(sid, path, variant = 'preview', onWait) {
    const key = sid + '|' + path + '|' + variant;
    if (readyCache.has(key)) return readyCache.get(key);
    const target = variantUrl(sid, path, variant);
    const attempt = (tries) => fetch(target, {method: 'HEAD', headers}).then(response => {
      if (response.status === 202) {
        if (onWait) onWait(tries);
        return new Promise(resolve => setTimeout(resolve, 2000)).then(() => attempt(tries + 1));
      }
      if (!response.ok) throw new Error('preview unavailable');
      return target;
    });
    const promise = attempt(0).catch(() => variantUrl(sid, path, 'original'));
    readyCache.set(key, promise);
    return promise;
  }

  function isModel(path) {
    return MODEL.test(String(path || ''));
  }
  // One switch for every player on the page, remembered between sessions.
  function loopToggle() {
    const toggle = node('button');
    const sync = () => {
      toggle.textContent = loopVideos ? '⟲ Повтор включён' : '⟲ Повтор выключен';
      toggle.title = loopVideos ? 'Видео повторяется без остановки' : 'Видео играет один раз';
      toggle.classList.toggle('on', loopVideos);
    };
    toggle.type = 'button';
    toggle.onclick = () => {
      loopVideos = !loopVideos;
      try {localStorage.setItem('aigent.media.loop', loopVideos ? '1' : '0');} catch {/* private mode */}
      for (const player of document.querySelectorAll('video')) player.loop = loopVideos;
      sync();
    };
    sync();
    return toggle;
  }
  function gallery(sid) {
    return [...document.querySelectorAll('.media-card')]
      .filter(card => card.dataset.sid === String(sid) && !['audio', 'model'].includes(card.dataset.kind));
  }

  function actions(sid, path, extra) {
    const row = node('div', undefined, 'media-actions');
    const open = node('a', 'Открыть оригинал');
    open.href = variantUrl(sid, path, 'original');
    open.target = '_blank';
    open.rel = 'noreferrer noopener';
    const save = node('a', 'Скачать');
    save.href = fileUrl(sid, path);
    save.download = shortName(path);
    row.append(open, save);
    if (extra) row.append(extra);
    return row;
  }

  // ------------------------------------------------------------------ lightbox
  let overlay = null;
  function closeLightbox() {
    if (!overlay) return;
    overlay.remove();
    overlay = null;
    document.removeEventListener('keydown', onKey, true);
  }
  function onKey(event) {
    if (!overlay) return;
    if (event.key === 'Escape') {closeLightbox(); event.preventDefault();}
    else if (event.key === 'ArrowRight') step(1);
    else if (event.key === 'ArrowLeft') step(-1);
  }
  function step(delta) {
    if (!overlay) return;
    const items = gallery(overlay.dataset.sid);
    const index = items.findIndex(card => card.dataset.path === overlay.dataset.path);
    const next = items[(index + delta + items.length) % items.length];
    if (next && next.dataset.path !== overlay.dataset.path) {
      openLightbox(next.dataset.sid, next.dataset.path, next.dataset.kind);
    }
  }
  function openLightbox(sid, path, kind) {
    // A model is not an image: it belongs in the persistent 3D dock, not in the picture lightbox.
    if (isModel(path) && window.Viewer3D) return window.Viewer3D.open(sid, path);
    closeLightbox();
    kind = kind || mediaKind(path) || 'image';
    overlay = node('div', undefined, 'media-lightbox');
    overlay.dataset.sid = sid;
    overlay.dataset.path = path;
    overlay.role = 'dialog';
    overlay.tabIndex = -1;
    const stage = node('div', undefined, 'media-stage');
    const caption = node('div', undefined, 'media-lightbox-caption');
    caption.append(node('strong', shortName(path)), node('small', '', 'media-meta'));
    const close = node('button', '✕', 'media-close');
    close.type = 'button';
    close.title = 'Закрыть (Esc)';
    close.onclick = closeLightbox;
    let extraAction = null;
    if (kind === 'video') {
      const player = node('video');
      player.controls = true;
      player.playsInline = true;
      player.autoplay = true;
      player.loop = loopVideos;
      player.preload = 'metadata';
      player.poster = variantUrl(sid, path, 'poster');
      player.src = variantUrl(sid, path, 'original');
      ready(sid, path).then(src => {if (overlay && player.isConnected && player.currentTime === 0) player.src = src;});
      stage.append(player);
      extraAction = loopToggle();
    } else if (kind === 'audio') {
      const player = node('audio');
      player.controls = true;
      player.src = variantUrl(sid, path, 'original');
      stage.append(player);
    } else {
      const image = node('img');
      image.alt = shortName(path);
      image.src = variantUrl(sid, path, 'preview');
      image.onclick = () => image.classList.toggle('zoomed'); // second click: natural size
      image.title = 'Клик — масштаб 1:1';
      stage.append(image);
    }
    const navigation = node('div', undefined, 'media-nav');
    for (const [label, delta] of [['‹', -1], ['›', 1]]) {
      const button = node('button', label);
      button.type = 'button';
      button.onclick = event => {event.stopPropagation(); step(delta);};
      navigation.append(button);
    }
    overlay.append(close, navigation, stage, caption, actions(sid, path, extraAction));
    overlay.onclick = event => {if (event.target === overlay) closeLightbox();};
    document.body.append(overlay);
    document.addEventListener('keydown', onKey, true);
    overlay.focus();
    info(sid, path).then(data => {
      const meta = caption.querySelector('.media-meta');
      if (meta) meta.textContent = describe(data);
    });
    return overlay;
  }

  // ------------------------------------------------------------------ cards
  function upgradeThumb(sid, path, image) {
    const load = () => {
      const full = new Image();
      full.onload = () => {image.src = full.src; image.classList.remove('blurred');};
      full.src = variantUrl(sid, path, 'preview');
    };
    if (typeof IntersectionObserver !== 'function') {load(); return;}
    const observer = new IntersectionObserver(entries => {
      if (entries.some(entry => entry.isIntersecting)) {observer.disconnect(); load();}
    }, {rootMargin: '200px'});
    observer.observe(image);
  }

  // A generated model gets its own card: the viewer is a live WebGL dock, not a still preview.
  function modelCard(sid, path, options) {
    const card = node('figure', undefined, 'media-card model');
    card.dataset.sid = sid;
    card.dataset.path = path;
    card.dataset.kind = 'model';
    const frame = node('button', undefined, 'media-frame');
    frame.type = 'button';
    frame.title = 'Открыть в 3D-просмотре';
    frame.append(node('span', '◈', 'media-3d-mark'), node('span', path.split('.').pop().toUpperCase(), 'media-3d-format'));
    frame.onclick = () => {
      if (window.Viewer3D) window.Viewer3D.open(sid, path);
      else window.open(fileUrl(sid, path), '_blank', 'noopener');
    };
    const caption = node('figcaption');
    caption.append(node('span', shortName(path), 'media-name'), node('small', '3D-модель', 'media-meta'));
    if (options.direction) caption.append(node('span', DIRECTIONS[options.direction] || options.direction, 'media-direction'));
    if (options.caption) caption.append(node('small', options.caption, 'media-caption'));
    const open = node('button', 'Открыть в 3D');
    open.type = 'button';
    open.onclick = frame.onclick;
    card.append(frame, caption, actions(sid, path, open));
    return card;
  }

  function render(sid, path, options = {}) {
    if (isModel(path) || options.kind === 'model') return modelCard(sid, path, options);
    const kind = options.kind && ['image', 'video', 'audio'].includes(options.kind)
      ? options.kind : (mediaKind(path) || 'image');
    const card = node('figure', undefined, 'media-card ' + kind);
    card.dataset.sid = sid;
    card.dataset.path = path;
    card.dataset.kind = kind;
    const caption = node('figcaption');
    const title = node('span', shortName(path), 'media-name');
    const meta = node('small', '', 'media-meta');
    caption.append(title, meta);
    if (options.direction) caption.append(node('span', DIRECTIONS[options.direction] || options.direction, 'media-direction'));
    if (options.caption) caption.append(node('small', options.caption, 'media-caption'));

    if (kind === 'audio') {
      const player = node('audio');
      player.controls = true;
      player.preload = 'metadata';
      player.src = variantUrl(sid, path, 'original');
      card.append(player, caption, actions(sid, path));
      info(sid, path).then(data => {meta.textContent = describe(data);});
      return card;
    }

    const frame = node('button', undefined, 'media-frame');
    frame.type = 'button';
    frame.title = kind === 'video' ? 'Воспроизвести здесь' : 'Открыть во весь экран';
    const image = node('img', undefined, 'blurred');
    image.alt = shortName(path);
    image.loading = 'lazy';
    image.decoding = 'async';
    image.src = variantUrl(sid, path, 'thumb');
    image.onerror = () => {card.classList.add('no-thumb'); image.remove();};
    frame.append(image);
    if (kind === 'video') frame.append(node('span', '▶', 'media-play'));
    const badge = node('span', '', 'media-badge');
    badge.hidden = true;
    frame.append(badge);
    if (kind === 'image') upgradeThumb(sid, path, image);

    frame.onclick = () => {
      if (kind === 'image') {openLightbox(sid, path, kind); return;}
      badge.hidden = false;
      badge.textContent = 'Готовим воспроизведение…';
      ready(sid, path, 'preview', tries => {
        badge.textContent = 'Перекодирование видео… ' + (tries * 2) + ' с';
      }).then(src => {
        badge.hidden = true;
        const box = node('div', undefined, 'media-player-box');
        const player = node('video');
        player.controls = true;
        player.playsInline = true;
        player.autoplay = true;
        player.loop = loopVideos;
        player.preload = 'metadata';
        player.poster = variantUrl(sid, path, 'poster');
        player.src = src;
        player.className = 'media-player';
        const full = node('button', '⏶', 'media-fullscreen');
        full.type = 'button';
        full.title = 'На весь экран (Esc — выйти)';
        full.onclick = () => {
          if (document.fullscreenElement) {
            document.exitFullscreen();
          } else if (box.requestFullscreen) {
            box.requestFullscreen();
          } else if (box.webkitRequestFullscreen) {
            box.webkitRequestFullscreen();
          }
        };
        box.append(player, full);
        frame.replaceWith(box);
      });
    };
    let extra = null;
    if (kind === 'video') {
      extra = document.createDocumentFragment();
      const lightbox = node('button', 'Открыть в лайтбоксе');
      lightbox.type = 'button';
      lightbox.onclick = () => openLightbox(sid, path, kind);
      extra.append(lightbox, loopToggle());
    }
    card.append(frame, caption, actions(sid, path, extra));
    info(sid, path).then(data => {meta.textContent = describe(data);});
    return card;
  }

  // Consecutive media in one turn share a responsive grid: a farm run posts many frames at once.
  function grid(container) {
    const last = container.lastElementChild;
    if (last && last.classList && last.classList.contains('media-grid')) return last;
    const created = node('div', undefined, 'media-grid');
    container.append(created);
    return created;
  }

  function isMedia(path) {
    return !!mediaKind(path);
  }
  // Anything the workspace can show in place: pictures, clips, audio and now 3D models.
  function isViewable(path) {
    return isMedia(path) || isModel(path);
  }

  window.MediaUI = {render, open: openLightbox, close: closeLightbox, grid, isMedia, isModel, isViewable,
                    kind: mediaKind, url: variantUrl, info, ready,
                    get loop() {return loopVideos;}};
})();
