(function () {
  const API_NAME = 'ViroSlideVirtualization';
  const DEFAULT_MIN_SLIDES = 12;
  const DEFAULT_BUFFER_SLIDES = 2;

  const state = {
    initialized: false,
    enabled: false,
    container: null,
    slides: [],
    placeholders: [],
    observers: [],
    scheduled: false,
    syncTimer: 0
  };

  function config() {
    return window.__VIRO_VIRTUALIZATION__ && typeof window.__VIRO_VIRTUALIZATION__ === 'object'
      ? window.__VIRO_VIRTUALIZATION__
      : {};
  }

  function toInteger(value, fallback) {
    const parsed = Number(value);
    return Number.isFinite(parsed) ? Math.max(0, Math.round(parsed)) : fallback;
  }

  function bufferSlides() {
    return toInteger(config().bufferSlides, DEFAULT_BUFFER_SLIDES);
  }

  function minSlides() {
    return toInteger(config().minSlides, DEFAULT_MIN_SLIDES);
  }

  function getContainer() {
    return document.getElementById('slideContainer') || document.querySelector('.slide-container') || document.body;
  }

  function shouldEnable(count) {
    const explicit = config().enabled;
    if (explicit === true) return true;
    if (explicit === false) return false;
    return Boolean(window.__RENDER_MODE__) && count >= minSlides();
  }

  function countRevealUnits(slide) {
    if (!slide) return 0;
    let count = slide.querySelectorAll('.slide-element').length;
    if (slide.dataset.mode === 'highlight') {
      count += slide.querySelectorAll('.highlightable').length;
    }
    if (slide.dataset.mode === 'traffic-light') {
      count += slide.querySelectorAll('.lightable').length;
    }
    return count;
  }

  function getSlides() {
    if (state.slides.length) return state.slides;
    const container = getContainer();
    return Array.from(container.querySelectorAll('.slide'));
  }

  function getRevealUnits() {
    return getSlides().map(countRevealUnits);
  }

  function pauseSlideMedia(slide) {
    slide?.querySelectorAll('video, audio').forEach((media) => {
      try { media.pause(); } catch {}
    });
  }

  function placeholder(index) {
    if (!state.placeholders[index]) {
      state.placeholders[index] = document.createComment(`viro-slide-placeholder:${index}`);
    }
    return state.placeholders[index];
  }

  function referenceNodeAfter(index) {
    for (let i = index + 1; i < state.slides.length; i += 1) {
      if (state.slides[i]?.parentNode) return state.slides[i];
      if (state.placeholders[i]?.parentNode) return state.placeholders[i];
    }
    return null;
  }

  function mountSlide(index) {
    const slide = state.slides[index];
    if (!slide || slide.parentNode) return;
    const marker = placeholder(index);
    if (marker.parentNode) {
      marker.parentNode.replaceChild(slide, marker);
    } else {
      state.container.insertBefore(slide, referenceNodeAfter(index));
    }
    slide.dataset.viroVirtualized = 'mounted';
  }

  function unmountSlide(index) {
    const slide = state.slides[index];
    if (!slide || !slide.parentNode || slide.classList.contains('active')) return;
    pauseSlideMedia(slide);
    slide.dataset.viroVirtualized = 'detached';
    slide.parentNode.replaceChild(placeholder(index), slide);
  }

  function activeIndex() {
    const index = state.slides.findIndex((slide) => slide.classList.contains('active'));
    return index >= 0 ? index : 0;
  }

  function isNear(center, index, buffer) {
    return Math.abs(index - center) <= buffer;
  }

  function sync(centerIndex) {
    if (!state.initialized) initialize();
    if (!state.enabled || !state.slides.length) return;
    const center = Number.isFinite(centerIndex) ? Math.max(0, Math.min(state.slides.length - 1, centerIndex)) : activeIndex();
    const buffer = bufferSlides();
    state.slides.forEach((_slide, index) => {
      if (isNear(center, index, buffer)) {
        mountSlide(index);
      } else {
        unmountSlide(index);
      }
    });
  }

  function scheduleSync(delay) {
    if (!state.enabled) return;
    if (delay) {
      window.clearTimeout(state.syncTimer);
      state.syncTimer = window.setTimeout(() => scheduleSync(), delay);
      return;
    }
    if (state.scheduled) return;
    state.scheduled = true;
    window.requestAnimationFrame(() => {
      state.scheduled = false;
      sync();
    });
  }

  function ensure(index) {
    if (!state.initialized) initialize();
    if (!state.enabled || !state.slides.length) return;
    const target = Math.max(0, Math.min(state.slides.length - 1, Number(index) || 0));
    const current = activeIndex();
    const buffer = bufferSlides();
    state.slides.forEach((_slide, slideIndex) => {
      if (isNear(target, slideIndex, buffer) || slideIndex === current) {
        mountSlide(slideIndex);
      }
    });
    scheduleSync(650);
  }

  function observeSlides() {
    state.observers.forEach((observer) => observer.disconnect());
    state.observers = [];
    const observer = new MutationObserver(() => scheduleSync());
    state.slides.forEach((slide) => {
      observer.observe(slide, { attributes: true, attributeFilter: ['class'] });
    });
    state.observers.push(observer);
  }

  function wrapNavigation() {
    const wrap = (name, getIndex) => {
      const original = window[name];
      if (typeof original !== 'function' || original.__viroVirtualized) return;
      const wrapped = function (...args) {
        const index = getIndex(args);
        if (Number.isFinite(index)) ensure(index);
        const result = original.apply(this, args);
        scheduleSync(700);
        return result;
      };
      wrapped.__viroVirtualized = true;
      window[name] = wrapped;
    };

    wrap('goToSlide', (args) => Number(args[0]));
    wrap('setActiveRuntimeSlide', (args) => Number(args[0]));
  }

  function calculateSceneTimings(rawLayers, fps) {
    const frameRate = fps == null ? null : Number(fps);
    if (frameRate != null && (!Number.isFinite(frameRate) || frameRate <= 0)) {
      throw new Error('fps must be a positive number');
    }
    let currentSeconds = 0;
    let currentFrame = 0;
    return (Array.isArray(rawLayers) ? rawLayers : []).map((rawLayer, index) => {
      const layer = { ...(rawLayer || {}) };
      const secondDuration = layer.duration ?? layer.duration_seconds ?? layer.durationSeconds;
      const frameDuration = layer.duration_in_frames ?? layer.durationInFrames;
      let durationSeconds;

      if (secondDuration != null) {
        durationSeconds = Number(secondDuration);
      } else if (frameDuration != null && frameRate) {
        durationSeconds = Number(frameDuration) / frameRate;
      } else {
        throw new Error(`layer ${index} is missing duration`);
      }
      if (!Number.isFinite(durationSeconds) || durationSeconds <= 0) {
        throw new Error(`layer ${index} must have a positive duration`);
      }

      if (frameRate) {
        const durationInFrames = Math.max(1, Math.round(durationSeconds * frameRate));
        const startFrame = currentFrame;
        currentFrame += durationInFrames;
        currentSeconds = currentFrame / frameRate;
        layer.start_frame = startFrame;
        layer.startFrame = startFrame;
        layer.duration_in_frames = durationInFrames;
        layer.durationInFrames = durationInFrames;
        layer.start = Number((startFrame / frameRate).toFixed(3));
        layer.duration = Number((durationInFrames / frameRate).toFixed(3));
      } else {
        layer.start = Number(currentSeconds.toFixed(3));
        layer.duration = Number(durationSeconds.toFixed(3));
        currentSeconds += durationSeconds;
      }
      return layer;
    });
  }

  function initialize() {
    if (state.initialized) return;
    state.container = getContainer();
    state.slides = Array.from(state.container.querySelectorAll('.slide'));
    state.placeholders = new Array(state.slides.length);
    state.enabled = shouldEnable(state.slides.length);
    state.initialized = true;
    wrapNavigation();
    if (!state.enabled) return;
    observeSlides();
    sync();
    document.addEventListener('click', () => scheduleSync(120), true);
    document.addEventListener('keydown', () => scheduleSync(120), true);
    window.setInterval(() => scheduleSync(), 1500);
  }

  window[API_NAME] = {
    initialize,
    sync,
    ensure,
    getRevealUnits,
    calculateSceneTimings,
    isEnabled: () => state.enabled,
    mountedSlideCount: () => state.slides.filter((slide) => Boolean(slide.parentNode)).length
  };

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', () => {
      window.setTimeout(initialize, 0);
      window.setTimeout(() => scheduleSync(), 350);
      window.setTimeout(() => scheduleSync(), 1200);
    });
  } else {
    window.setTimeout(initialize, 0);
    window.setTimeout(() => scheduleSync(), 350);
    window.setTimeout(() => scheduleSync(), 1200);
  }
})();
