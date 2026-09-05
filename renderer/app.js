/**
 * Biedronka Promo Hunter – App Logic
 *
 * The Python side indexes leaflets in the background; searching only ever
 * touches the local FTS5 index, so results come back instantly and refresh
 * on their own as more pages finish OCR.
 */

(function () {
  'use strict';

  // === DOM ===
  const sectionSearch = document.getElementById('section-search');
  const sectionResults = document.getElementById('section-results');
  const sectionSettings = document.getElementById('section-settings');

  const searchInput = document.getElementById('search-input');
  const searchBtn = document.getElementById('search-btn');
  const chipsBox = document.getElementById('keyword-chips');

  const resultsCount = document.getElementById('results-count');
  const resultsKeyword = document.getElementById('results-keyword');
  const galleryGrid = document.getElementById('gallery-grid');
  const noResults = document.getElementById('no-results');
  const newSearchBtn = document.getElementById('new-search-btn');
  const discordBtn = document.getElementById('discord-btn');
  const folderBtn = document.getElementById('folder-btn');

  const indexStrip = document.getElementById('index-strip');
  const indexFill = document.getElementById('index-fill');
  const indexText = document.getElementById('index-text');
  const indexCount = document.getElementById('index-count');
  const indexDot = document.getElementById('index-dot');

  const lightbox = document.getElementById('lightbox');
  const lightboxImg = document.getElementById('lightbox-img');
  const lightboxWrapper = document.getElementById('lightbox-img-wrapper');
  const lightboxInfo = document.getElementById('lightbox-info');
  const lightboxClose = document.getElementById('lightbox-close');
  const lightboxPrev = document.getElementById('lightbox-prev');
  const lightboxNext = document.getElementById('lightbox-next');

  const navLinks = document.querySelectorAll('.nav-link');

  // === State ===
  let keywords = [];              // saved shopping list
  let hitsByKeyword = {};         // keyword -> hits from the last search
  let activeKeyword = '';
  let shownHits = [];             // hits currently in the gallery
  let lightboxIndex = -1;
  let indexing = false;
  let thumbnailWidth = 300;
  let backendLabel = '';

  // === Navigation ===
  function showSection(sectionId) {
    [sectionSearch, sectionResults, sectionSettings].forEach((s) => {
      if (s) s.classList.remove('active');
    });
    navLinks.forEach((link) => {
      link.classList.toggle('active', link.dataset.section === sectionId);
    });
    const target = document.getElementById('section-' + sectionId);
    if (target) target.classList.add('active');
  }

  navLinks.forEach((link) => {
    link.addEventListener('click', (e) => {
      e.preventDefault();
      showSection(link.dataset.section);
    });
  });

  // === Keyword list ===
  function renderChips() {
    chipsBox.innerHTML = '';
    keywords.forEach((keyword) => {
      const hits = hitsByKeyword[keyword];
      const chip = document.createElement('span');
      chip.className = 'chip' + (keyword === activeKeyword ? ' active' : '');

      const label = document.createElement('span');
      label.textContent = keyword;
      chip.appendChild(label);

      const count = document.createElement('span');
      count.className = 'chip-count';
      count.textContent = hits ? hits.length : '…';
      chip.appendChild(count);

      const remove = document.createElement('button');
      remove.className = 'chip-remove';
      remove.textContent = '×';
      remove.title = 'Usuń hasło';
      remove.addEventListener('click', (e) => {
        e.stopPropagation();
        keywords = keywords.filter((k) => k !== keyword);
        delete hitsByKeyword[keyword];
        if (activeKeyword === keyword) activeKeyword = keywords[0] || '';
        persistKeywords();
        renderChips();
        renderResults();
      });
      chip.appendChild(remove);

      chip.addEventListener('click', () => {
        activeKeyword = keyword;
        renderChips();
        renderResults();
        showSection('results');
      });

      chipsBox.appendChild(chip);
    });
  }

  function persistKeywords() {
    window.api.loadConfig().then((config) => {
      window.api.saveConfig({ ...config, keywords });
    });
  }

  function addKeyword(raw) {
    const keyword = (raw || '').trim().toLowerCase();
    if (!keyword) return;
    if (!keywords.includes(keyword)) keywords.push(keyword);
    activeKeyword = keyword;
    searchInput.value = '';
    persistKeywords();
    renderChips();
    window.api.search(keyword);
    showSection('results');
  }

  searchBtn.addEventListener('click', () => addKeyword(searchInput.value));
  searchInput.addEventListener('keydown', (e) => {
    if (e.key === 'Enter') addKeyword(searchInput.value);
  });

  newSearchBtn.addEventListener('click', () => {
    showSection('search');
    searchInput.focus();
  });

  folderBtn.addEventListener('click', () => window.api.openFolder());

  discordBtn.addEventListener('click', () => {
    if (!shownHits.length) return;
    discordBtn.disabled = true;
    window.api.sendDiscord(activeKeyword, shownHits);
  });

  // === Results ===
  // The remote URL goes in the query string, not the host: hostnames are
  // lowercased by the URL parser and the CDN paths are case sensitive.
  function thumbUrl(imageUrl) {
    return `page-thumb://img/?u=${encodeURIComponent(imageUrl)}&w=${thumbnailWidth}`;
  }

  function renderResults() {
    const hits = hitsByKeyword[activeKeyword] || [];
    shownHits = hits;

    resultsKeyword.textContent = activeKeyword;
    resultsCount.textContent = hits.length;
    galleryGrid.innerHTML = '';
    noResults.style.display = hits.length ? 'none' : '';
    discordBtn.disabled = !hits.length;

    hits.forEach((hit, index) => {
      const card = document.createElement('div');
      card.className = 'gallery-card';
      card.style.animationDelay = `${Math.min(index * 0.04, 0.5)}s`;

      const img = document.createElement('img');
      img.src = thumbUrl(hit.image_url);
      img.loading = 'lazy';
      img.alt = `${hit.leaflet_name}, strona ${hit.page_number}`;
      card.appendChild(img);

      const badge = document.createElement('div');
      badge.className = 'card-badge';
      badge.textContent = index + 1;
      card.appendChild(badge);

      const caption = document.createElement('div');
      caption.className = 'card-hits';
      caption.textContent = `${hit.leaflet_name} · s.${hit.page_number}`;
      card.appendChild(caption);

      card.addEventListener('click', () => openLightbox(index));
      galleryGrid.appendChild(card);
    });
  }

  // === Lightbox ===
  function openLightbox(index) {
    if (index < 0 || index >= shownHits.length) return;
    lightboxIndex = index;
    const hit = shownHits[index];

    lightboxImg.src = hit.image_url;
    lightboxInfo.textContent = `${hit.leaflet_name} — strona ${hit.page_number}`;
    lightbox.classList.add('active');

    lightboxImg.onload = () => drawHitBoxes(hit);
    if (lightboxImg.complete) drawHitBoxes(hit);
  }

  // Boxes are stored in source-image pixels, so scale them to the rendered size.
  function drawHitBoxes(hit) {
    lightboxWrapper.querySelectorAll('.hit-box').forEach((el) => el.remove());
    if (!hit.boxes || !hit.boxes.length) return;
    if (!lightboxImg.naturalWidth) return;

    const scaleX = lightboxImg.clientWidth / lightboxImg.naturalWidth;
    const scaleY = lightboxImg.clientHeight / lightboxImg.naturalHeight;
    const offsetX = lightboxImg.offsetLeft;
    const offsetY = lightboxImg.offsetTop;

    hit.boxes.forEach((box) => {
      const [x0, y0, x1, y1] = box[2];
      const el = document.createElement('div');
      el.className = 'hit-box';
      el.style.left = `${offsetX + x0 * scaleX}px`;
      el.style.top = `${offsetY + y0 * scaleY}px`;
      el.style.width = `${(x1 - x0) * scaleX}px`;
      el.style.height = `${(y1 - y0) * scaleY}px`;
      el.title = box[0];
      lightboxWrapper.appendChild(el);
    });
  }

  function closeLightbox() {
    lightbox.classList.remove('active');
    lightboxWrapper.querySelectorAll('.hit-box').forEach((el) => el.remove());
  }

  lightboxClose.addEventListener('click', closeLightbox);
  lightbox.querySelector('.lightbox-backdrop').addEventListener('click', closeLightbox);
  lightboxPrev.addEventListener('click', () => openLightbox(lightboxIndex - 1));
  lightboxNext.addEventListener('click', () => openLightbox(lightboxIndex + 1));

  document.addEventListener('keydown', (e) => {
    if (!lightbox.classList.contains('active')) return;
    if (e.key === 'Escape') closeLightbox();
    if (e.key === 'ArrowLeft') openLightbox(lightboxIndex - 1);
    if (e.key === 'ArrowRight') openLightbox(lightboxIndex + 1);
  });

  window.addEventListener('resize', () => {
    if (lightbox.classList.contains('active') && lightboxIndex >= 0) {
      drawHitBoxes(shownHits[lightboxIndex]);
    }
  });

  // === Indexing strip ===
  function setIndexState(text, { percent, count, state } = {}) {
    indexText.textContent = text;
    if (percent !== undefined) indexFill.style.width = `${percent}%`;
    if (count !== undefined) indexCount.textContent = count;
    indexDot.className = 'index-strip-dot' + (state ? ' ' + state : '');
  }

  function showBackend(backend) {
    if (backend) backendLabel = backend;
  }

  // Refresh open results as new pages land in the index.
  let refreshTimer = null;
  function scheduleRefresh() {
    if (refreshTimer || !keywords.length) return;
    refreshTimer = setTimeout(() => {
      refreshTimer = null;
      window.api.searchMany(keywords);
    }, 2500);
  }

  // === Engine events ===
  window.api.onSearchEvent((evt) => {
    switch (evt.type) {
      case 'ready':
        setIndexState(
          evt.indexed
            ? `Indeks gotowy: ${evt.indexed} stron. Sprawdzam nowe gazetki...`
            : 'Buduję indeks od zera...',
          { state: '' }
        );
        cacheInfo.textContent = `Indeks: ${evt.indexed} stron`;
        if (keywords.length) window.api.searchMany(keywords);
        break;

      case 'status':
        setIndexState(evt.message);
        break;

      case 'progress': {
        indexing = true;
        const percent = evt.total ? (evt.current / evt.total) * 100 : 0;
        const where = evt.leaflet ? `${evt.leaflet} · s.${evt.page}` : 'indeksowanie';
        setIndexState(`OCR: ${where}`, {
          percent,
          count: `${evt.current} / ${evt.total}`,
        });
        scheduleRefresh();
        break;
      }

      case 'index-status':
        cacheInfo.textContent = `Indeks: ${evt.indexed} stron`;
        if (evt.backend) showBackend(evt.backend);
        break;

      case 'engine':
        showBackend(evt.backend);
        break;

      case 'cache-cleared':
        hitsByKeyword = {};
        renderChips();
        renderResults();
        cacheInfo.textContent = 'Indeks: 0 stron';
        cacheStatus.style.display = '';
        cacheStatus.textContent =
          `Usunięto ${evt.pages} stron z indeksu i ${evt.files} plików. ` +
          'Indeksowanie ruszy przy następnym starcie albo po kliknięciu „Indeksuj teraz".';
        cacheClearBtn.disabled = false;
        setIndexState('Indeks pusty.', { percent: 0, count: '', state: 'done' });
        indexStrip.classList.remove('hidden');
        break;

      case 'index-done':
        indexing = false;
        window.api.indexStatus();
        setIndexState(
          evt.indexed
            ? `Indeks aktualny — dodano ${evt.indexed} stron, razem ${evt.total}.`
            : `Indeks aktualny — ${evt.total} stron${backendLabel ? ` (${backendLabel})` : ''}.`,
          { percent: 100, state: 'done' }
        );
        if (keywords.length) window.api.searchMany(keywords);
        setTimeout(() => indexStrip.classList.add('hidden'), 6000);
        break;

      case 'results':
        hitsByKeyword[evt.keyword] = evt.hits;
        if (!activeKeyword) activeKeyword = evt.keyword;
        renderChips();
        if (evt.keyword === activeKeyword) renderResults();
        break;

      case 'discord-done':
        discordBtn.disabled = false;
        setIndexState(`Wysłano na Discorda: ${evt.sent} stron.`, { state: 'done' });
        indexStrip.classList.remove('hidden');
        break;

      case 'error':
        indexStrip.classList.remove('hidden');
        setIndexState(evt.message, { state: 'error' });
        discordBtn.disabled = false;
        cacheClearBtn.disabled = false;
        cacheStatus.style.display = '';
        cacheStatus.textContent = evt.message;
        break;

      case 'process-ended':
        indexing = false;
        if (evt.code !== 0 && evt.code !== null) {
          setIndexState(`Indekser zakończył się (kod ${evt.code}).`, { state: 'error' });
        }
        break;

      default:
        break;
    }
  });

  // === Settings ===
  const webhookInput = document.getElementById('settings-webhook');
  const webhookSave = document.getElementById('settings-webhook-save');
  const webhookStatus = document.getElementById('webhook-status');
  const discordToggle = document.getElementById('settings-discord-toggle');
  const thumbRange = document.getElementById('settings-thumb-quality');
  const thumbLabel = document.getElementById('thumb-quality-label');
  const cacheInfo = document.getElementById('cache-info');
  const cacheStatus = document.getElementById('cache-status');
  const cacheClearBtn = document.getElementById('cache-clear-btn');
  const reindexBtn = document.getElementById('reindex-btn');

  reindexBtn.addEventListener('click', () => {
    cacheStatus.style.display = '';
    cacheStatus.textContent = 'Startuję indeksowanie...';
    indexStrip.classList.remove('hidden');
    window.api.reindex();
  });

  cacheClearBtn.addEventListener('click', () => {
    const ok = window.confirm(
      'Usunąć cały indeks OCR i zapisane strony?\n\n' +
      'Wszystkie gazetki zostaną zeskanowane od nowa (~4 min). Tej operacji nie da się cofnąć.'
    );
    if (!ok) return;
    cacheClearBtn.disabled = true;
    cacheStatus.style.display = '';
    cacheStatus.textContent = indexing
      ? 'Zatrzymuję indeksowanie, potem czyszczę...'
      : 'Czyszczę...';
    window.api.clearCache();
  });

  window.api.loadConfig().then((config) => {
    webhookInput.value = config.discordWebhookUrl || '';
    discordToggle.checked = !!config.discordEnabled;
    keywords = Array.isArray(config.keywords) ? config.keywords : [];
    activeKeyword = keywords[0] || '';
    renderChips();
    window.api.startEngine();
  });

  webhookSave.addEventListener('click', async () => {
    const config = await window.api.loadConfig();
    await window.api.saveConfig({ ...config, discordWebhookUrl: webhookInput.value.trim() });
    webhookStatus.style.display = '';
    webhookStatus.textContent = 'Zapisano. Zrestartuj aplikację, żeby indekser zobaczył nowy webhook.';
  });

  discordToggle.addEventListener('change', async () => {
    const config = await window.api.loadConfig();
    await window.api.saveConfig({ ...config, discordEnabled: discordToggle.checked });
  });

  if (thumbRange) {
    thumbRange.addEventListener('input', () => {
      thumbnailWidth = parseInt(thumbRange.value, 10) || 300;
      thumbLabel.textContent = `${thumbnailWidth} px`;
    });
    thumbRange.addEventListener('change', () => renderResults());
  }

  // === Window controls ===
  document.getElementById('btn-minimize').addEventListener('click', () => window.api.minimizeWindow());
  document.getElementById('btn-maximize').addEventListener('click', () => window.api.maximizeWindow());
  document.getElementById('btn-close').addEventListener('click', () => window.api.closeWindow());
})();
