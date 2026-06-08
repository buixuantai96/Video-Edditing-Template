(function () {
  const MEDIA_CLASS = 'viro-slide-media-asset';
  const LAYER_CLASS = 'viro-slide-media-layer';
  const STYLE_ID = 'viro-slide-media-runtime-style';
  const STUDIO_PREVIEW_PARAM = 'studioPreview';
  const DEFAULT_MIN_LAZY_SLIDES = 12;
  const DEFAULT_BUFFER_SLIDES = 2;

  const state = {
    applyToken: 0,
    items: [],
    slides: [],
    appliedSlides: new Set(),
    lazy: false,
    observer: null,
    scheduled: false,
    timer: 0
  };

  function ensureStyle() {
    if (document.getElementById(STYLE_ID)) return;
    const style = document.createElement('style');
    style.id = STYLE_ID;
    style.textContent = `
      .slide .viro-slide-media-layer {
        position: absolute;
        inset: 18% 9% 18% 9%;
        z-index: 0;
        border: 1px solid rgba(255, 255, 255, 0.24);
        border-radius: 18px;
        overflow: hidden;
        background: rgba(2, 6, 23, 0.72);
        box-shadow: 0 24px 80px rgba(0, 0, 0, 0.32);
        pointer-events: none;
      }
      .slide .slide-content > :not(.viro-slide-media-layer) {
        position: relative;
        z-index: 1;
      }
      .viro-slide-media-asset {
        width: 100%;
        height: 100%;
        display: block;
        object-position: center;
      }
      .viro-slide-media-fit-cover { object-fit: cover; }
      .viro-slide-media-fit-contain { object-fit: contain; background: rgba(2, 6, 23, 0.9); }
      .viro-slide-media-fit-fill { object-fit: fill; }
      .viro-slide-media-pos-top { object-position: top center; }
      .viro-slide-media-pos-bottom { object-position: bottom center; }
      .viro-slide-media-pos-left { object-position: center left; }
      .viro-slide-media-pos-right { object-position: center right; }
      .viro-slide-media-slot-filled {
        position: relative;
        overflow: hidden;
        color: transparent !important;
      }
      .viro-slide-media-slot-filled > :not(.viro-slide-media-asset) {
        display: none !important;
      }
      .viro-slide-media-slot-filled .viro-slide-media-asset {
        position: absolute;
        inset: 0;
      }
      body.viro-studio-preview-mode .theme-editor-panel,
      body.viro-studio-preview-mode #scriptPanel,
      body.viro-studio-preview-mode #audioPanel,
      body.viro-studio-preview-mode .side-controls {
        display: none !important;
      }
      body.viro-studio-preview-mode .main-wrapper {
        left: 50% !important;
        right: auto !important;
        transform: translate(-50%, -50%) !important;
        justify-content: center !important;
      }
      body.viro-studio-preview-mode .slide-container {
        cursor: default !important;
      }
    `;
    document.head.appendChild(style);
  }

  function isStudioPreviewMode() {
    const params = new URLSearchParams(window.location.search);
    return params.get(STUDIO_PREVIEW_PARAM) === '1' || params.get('studio_preview') === '1';
  }

  function applyStudioPreviewMode() {
    if (!isStudioPreviewMode()) return;
    ensureStyle();
    document.documentElement.classList.add('viro-studio-preview-mode');
    document.body?.classList?.add('viro-studio-preview-mode');
  }

  function projectNameFromPath() {
    const match = window.location.pathname.match(/\/slide\/([^/]+)/);
    return match ? decodeURIComponent(match[1]) : '';
  }

  function getRuntimeSlides() {
    const virtualSlides = window.ViroSlideVirtualization?.getSlides?.();
    if (Array.isArray(virtualSlides) && virtualSlides.length) return virtualSlides;
    return Array.from(document.querySelectorAll('.slide'));
  }

  async function loadSettings() {
    if (window.__PREVIEW_SETTINGS__ && typeof window.__PREVIEW_SETTINGS__ === 'object') {
      return window.__PREVIEW_SETTINGS__;
    }
    const project = projectNameFromPath();
    if (!project || !window.location.protocol.startsWith('http')) return {};
    try {
      const response = await fetch(`/api/preview-settings?project=${encodeURIComponent(project)}`, { cache: 'no-store' });
      if (!response.ok) return {};
      const data = await response.json();
      return data.settings || {};
    } catch {
      return {};
    }
  }

  function mediaSource(item) {
    return String(item?.url || item?.path || item?.src || '').trim();
  }

  function allowedMediaSource(src) {
    return Boolean(src) && !/^\s*(javascript|vbscript):/i.test(src);
  }

  function mediaType(item, src) {
    const explicit = String(item?.type || '').toLowerCase();
    if (explicit === 'video' || explicit === 'image') return explicit;
    return /\.(mp4|webm|mov)(\?|#|$)/i.test(src) ? 'video' : 'image';
  }

  function cleanFit(value) {
    const fit = String(value || '').toLowerCase();
    return ['cover', 'contain', 'fill'].includes(fit) ? fit : 'cover';
  }

  function cleanPosition(value) {
    const position = String(value || '').toLowerCase();
    return ['center', 'top', 'bottom', 'left', 'right'].includes(position) ? position : 'center';
  }

  function cleanSlot(value) {
    return String(value || '').trim().slice(0, 80);
  }

  function cssString(value) {
    return String(value).replace(/\\/g, '\\\\').replace(/"/g, '\\"');
  }

  function cleanSlideIndex(item, slideCount) {
    const raw = item?.slide ?? item?.index ?? item?.slideIndex ?? 0;
    const index = Number(raw);
    if (!Number.isInteger(index) || index < 0 || index >= slideCount) return null;
    return index;
  }

  function normalizeMediaItem(item, slideCount) {
    if (!item || typeof item !== 'object') return null;
    const slide = cleanSlideIndex(item, slideCount);
    const src = mediaSource(item);
    if (slide === null || !allowedMediaSource(src)) return null;
    return {
      slide,
      src,
      type: mediaType(item, src),
      fit: cleanFit(item.fit),
      position: cleanPosition(item.position),
      slot: cleanSlot(item.slot)
    };
  }

  function normalizeMediaItems(settings, slideCount) {
    const rawItems = Array.isArray(settings?.slides?.media) ? settings.slides.media : [];
    return rawItems
      .map((item) => normalizeMediaItem(item, slideCount))
      .filter(Boolean)
      .slice(0, slideCount);
  }

  function virtualizationConfig() {
    return window.__VIRO_VIRTUALIZATION__ && typeof window.__VIRO_VIRTUALIZATION__ === 'object'
      ? window.__VIRO_VIRTUALIZATION__
      : {};
  }

  function toInteger(value, fallback) {
    const parsed = Number(value);
    return Number.isFinite(parsed) ? Math.max(0, Math.round(parsed)) : fallback;
  }

  function lazyMediaEnabled(slideCount) {
    const cfg = virtualizationConfig();
    if (cfg.lazyMedia === true || cfg.mediaLazy === true) return true;
    if (cfg.lazyMedia === false || cfg.mediaLazy === false) return false;
    const minSlides = toInteger(cfg.minSlides, DEFAULT_MIN_LAZY_SLIDES);
    return Boolean(window.__RENDER_MODE__) && slideCount >= minSlides;
  }

  function bufferSlides() {
    return toInteger(virtualizationConfig().bufferSlides, DEFAULT_BUFFER_SLIDES);
  }

  function activeSlideIndex() {
    const index = state.slides.findIndex((slide) => slide.classList.contains('active'));
    return index >= 0 ? index : 0;
  }

  function visibleSlideIndexes(centerIndex = activeSlideIndex()) {
    const center = Math.max(0, Math.min(state.slides.length - 1, Number(centerIndex) || 0));
    const buffer = bufferSlides();
    const indexes = new Set();
    for (let index = Math.max(0, center - buffer); index <= Math.min(state.slides.length - 1, center + buffer); index += 1) {
      indexes.add(index);
    }
    return indexes;
  }

  function clearSlideMedia(slide) {
    slide?.querySelectorAll(`.${LAYER_CLASS}`).forEach((node) => node.remove());
    slide?.querySelectorAll('.viro-slide-media-slot-filled').forEach((slot) => {
      slot.classList.remove('viro-slide-media-slot-filled');
      slot.querySelectorAll(`.${MEDIA_CLASS}`).forEach((node) => node.remove());
    });
  }

  function clearExisting() {
    state.slides.forEach(clearSlideMedia);
    document.querySelectorAll(`.${LAYER_CLASS}`).forEach((node) => node.remove());
    document.querySelectorAll('.viro-slide-media-slot-filled').forEach((slot) => {
      slot.classList.remove('viro-slide-media-slot-filled');
      slot.querySelectorAll(`.${MEDIA_CLASS}`).forEach((node) => node.remove());
    });
    state.appliedSlides.clear();
  }

  function createMediaElement(item) {
    const element = document.createElement(item.type === 'video' ? 'video' : 'img');
    element.className = `${MEDIA_CLASS} viro-slide-media-fit-${item.fit} viro-slide-media-pos-${item.position}`;
    if (item.type === 'video') {
      element.muted = true;
      element.loop = true;
      element.playsInline = true;
      element.autoplay = true;
      element.preload = 'metadata';
    }
    element.src = item.src;
    return element;
  }

  function findSlot(slide, item) {
    if (item.slot) {
      const named = slide.querySelector(`[data-media-slot="${cssString(item.slot)}"]`);
      if (named) return named;
    }
    return slide.querySelector('[data-media-slot], .viro-source-placeholder, .webui-frame');
  }

  function applyMediaItem(slide, item) {
    if (!slide || !item?.src) return;
    const media = createMediaElement(item);
    const slot = findSlot(slide, item);
    if (slot) {
      slot.classList.add('viro-slide-media-slot-filled');
      slot.appendChild(media);
      media.play?.().catch?.(() => {});
      return;
    }
    const content = slide.querySelector('.slide-content') || slide;
    const layer = document.createElement('div');
    layer.className = LAYER_CLASS;
    layer.appendChild(media);
    content.insertBefore(layer, content.firstChild);
    media.play?.().catch?.(() => {});
  }

  function applyMediaForSlide(index) {
    if (state.appliedSlides.has(index)) return;
    const slide = state.slides[index];
    if (!slide) return;
    const items = state.items.filter((item) => item.slide === index);
    if (!items.length) return;
    items.forEach((item) => applyMediaItem(slide, item));
    state.appliedSlides.add(index);
  }

  function unloadMediaForSlide(index) {
    const slide = state.slides[index];
    if (!slide || !state.appliedSlides.has(index)) return;
    clearSlideMedia(slide);
    state.appliedSlides.delete(index);
  }

  function syncVisibleMedia(centerIndex) {
    if (!state.items.length) return;
    if (!state.lazy) {
      state.slides.forEach((_slide, index) => applyMediaForSlide(index));
      return;
    }

    const visible = visibleSlideIndexes(centerIndex);
    visible.forEach((index) => applyMediaForSlide(index));
    Array.from(state.appliedSlides).forEach((index) => {
      if (!visible.has(index)) unloadMediaForSlide(index);
    });
  }

  function scheduleMediaSync(delay) {
    if (delay) {
      window.clearTimeout(state.timer);
      state.timer = window.setTimeout(() => scheduleMediaSync(), delay);
      return;
    }
    if (state.scheduled) return;
    state.scheduled = true;
    window.requestAnimationFrame(() => {
      state.scheduled = false;
      syncVisibleMedia();
    });
  }

  function observeSlideChanges() {
    if (state.observer) state.observer.disconnect();
    state.observer = new MutationObserver(() => scheduleMediaSync());
    state.slides.forEach((slide) => {
      state.observer.observe(slide, { attributes: true, attributeFilter: ['class'] });
    });
  }

  async function applySlideMedia() {
    const token = ++state.applyToken;
    ensureStyle();
    applyStudioPreviewMode();
    const settings = await loadSettings();
    if (token !== state.applyToken) return;
    const slides = getRuntimeSlides();
    const items = normalizeMediaItems(settings, slides.length);
    state.slides = slides;
    state.items = items;
    state.lazy = lazyMediaEnabled(slides.length);
    clearExisting();
    if (!items.length) return;
    observeSlideChanges();
    syncVisibleMedia();
    setTimeout(() => syncVisibleMedia(), 350);
  }

  function ensureSlideMedia(index) {
    if (!state.items.length) return;
    const target = Math.max(0, Math.min(state.slides.length - 1, Number(index) || 0));
    visibleSlideIndexes(target).forEach((slideIndex) => applyMediaForSlide(slideIndex));
    scheduleMediaSync(650);
  }

  window.ViroSlideMediaRuntime = {
    apply: applySlideMedia,
    sync: syncVisibleMedia,
    ensure: ensureSlideMedia,
    isLazy: () => state.lazy,
    itemCount: () => state.items.length,
    loadedSlideCount: () => state.appliedSlides.size
  };
  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', () => {
      applyStudioPreviewMode();
      applySlideMedia();
      setTimeout(applyStudioPreviewMode, 150);
      setTimeout(applyStudioPreviewMode, 600);
      setTimeout(applySlideMedia, 450);
      setTimeout(applySlideMedia, 1200);
      document.addEventListener('click', () => scheduleMediaSync(120), true);
      document.addEventListener('keydown', () => scheduleMediaSync(120), true);
    });
  } else {
    applyStudioPreviewMode();
    applySlideMedia();
    setTimeout(applyStudioPreviewMode, 150);
    setTimeout(applyStudioPreviewMode, 600);
    setTimeout(applySlideMedia, 450);
    setTimeout(applySlideMedia, 1200);
    document.addEventListener('click', () => scheduleMediaSync(120), true);
    document.addEventListener('keydown', () => scheduleMediaSync(120), true);
  }
})();
