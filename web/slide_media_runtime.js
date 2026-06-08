(function () {
  const MEDIA_CLASS = 'viro-slide-media-asset';
  const LAYER_CLASS = 'viro-slide-media-layer';
  const STYLE_ID = 'viro-slide-media-runtime-style';
  const STUDIO_PREVIEW_PARAM = 'studioPreview';

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

  function clearExisting() {
    document.querySelectorAll(`.${LAYER_CLASS}`).forEach((node) => node.remove());
    document.querySelectorAll('.viro-slide-media-slot-filled').forEach((slot) => {
      slot.classList.remove('viro-slide-media-slot-filled');
      slot.querySelectorAll(`.${MEDIA_CLASS}`).forEach((node) => node.remove());
    });
  }

  function createMediaElement(item, src) {
    const type = mediaType(item, src);
    const element = document.createElement(type === 'video' ? 'video' : 'img');
    element.className = `${MEDIA_CLASS} viro-slide-media-fit-${cleanFit(item.fit)} viro-slide-media-pos-${cleanPosition(item.position)}`;
    if (type === 'video') {
      element.muted = true;
      element.loop = true;
      element.playsInline = true;
      element.autoplay = true;
      element.preload = 'metadata';
    }
    element.src = src;
    return element;
  }

  function findSlot(slide, item) {
    const slot = String(item?.slot || '').trim();
    if (slot) {
      const named = slide.querySelector(`[data-media-slot="${CSS.escape(slot)}"]`);
      if (named) return named;
    }
    return slide.querySelector('[data-media-slot], .viro-source-placeholder, .webui-frame');
  }

  function applyMediaItem(slide, item) {
    const src = mediaSource(item);
    if (!slide || !src) return;
    const media = createMediaElement(item, src);
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

  async function applySlideMedia() {
    ensureStyle();
    applyStudioPreviewMode();
    const settings = await loadSettings();
    const items = Array.isArray(settings?.slides?.media) ? settings.slides.media : [];
    clearExisting();
    if (!items.length) return;
    const slides = Array.from(document.querySelectorAll('.slide'));
    items.forEach((item) => {
      const index = Number(item?.slide ?? item?.index ?? item?.slideIndex ?? 0);
      if (!Number.isFinite(index)) return;
      applyMediaItem(slides[index], item);
    });
  }

  window.ViroSlideMediaRuntime = { apply: applySlideMedia };
  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', () => {
      applyStudioPreviewMode();
      applySlideMedia();
      setTimeout(applyStudioPreviewMode, 150);
      setTimeout(applyStudioPreviewMode, 600);
      setTimeout(applySlideMedia, 450);
      setTimeout(applySlideMedia, 1200);
    });
  } else {
    applyStudioPreviewMode();
    applySlideMedia();
    setTimeout(applyStudioPreviewMode, 150);
    setTimeout(applyStudioPreviewMode, 600);
    setTimeout(applySlideMedia, 450);
    setTimeout(applySlideMedia, 1200);
  }
})();
