/* ============================================
   SLIDE PRESENTATION APP
   Kimi K2.6 / Moonshot AI
   ============================================ */

// State
let currentSlide = 0;
const totalSlides = 5;
let isReady = true;
let isAnimating = false;
let audioCtx = null;
let bgMusicGain = null;
let isMuted = false;

// Per-slide audio config — Tech/retro/synth vibe
const slideTransitions = ['rise', 'sweep', 'chime', 'bass', 'alarm', 'chord', 'minimal'];
const slideReveals = ['blip', 'sparkle', 'tick', 'bubble', 'ping', 'snap', 'bell'];

// Per-slide script text
const slideScripts = [
  'Stripe vừa làm chấn động thế giới khi giao luôn ngân hàng doanh nghiệp cho AI quản lý. Cùng xem bản demo thực tế dưới đây!',
  'Đây không chỉ là một tài khoản thông thường, mà là trạm trung chuyển tài chính với ba quyền năng tuyệt đối. Đầu tiên, doanh nghiệp có thể ung dung trữ stablecoin để tránh bão tỷ giá. Thứ hai, bạn được phép chuyển khoản cho đối tác ở 160 quốc gia hoàn toàn miễn phí. Và cuối cùng, mức hoàn tiền 2 phần trăm khi quẹt thẻ Stripe còn ăn đứt hầu hết thẻ tín dụng hiện nay.',
  'Cộng đồng lập tức chia phe tranh cãi gay gắt về rủi ro của mô hình này. Phe ủng hộ háo hức coi đây là hạ tầng bắt buộc cho nền kinh tế tự động hóa. Phe cẩn trọng thì vẫn e dè về các khoản phí ẩn và rào cản pháp lý quốc tế. Tuy nhiên đa phần đều thừa nhận, cuộc chơi tài chính đã chính thức sang trang.',
  'Bằng cách tích hợp giao thức kết nối MCP, Treasury đã vượt xa khái niệm ngân hàng số thông thường. AI agent giờ đây không chỉ biết tư vấn, mà có thể tự đọc số dư và thanh toán hóa đơn chuẩn xác. Stripe đang từng bước trở thành hệ điều hành tài chính tối thượng của tương lai.',
  'Két sắt doanh nghiệp của bạn đã sẵn sàng mở cửa cho AI chưa? Follow Viro để không bỏ lỡ tin công nghệ mỗi ngày!'
];

// DOM Elements
const slides = document.querySelectorAll('.slide');
const progressFill = document.getElementById('progressFill');
const currentSlideEl = document.getElementById('currentSlide');
const container = document.getElementById('slideContainer');

// ============================================
// AUDIO ENGINE
// ============================================
function initAudio() {
  if (audioCtx) return;
  audioCtx = new (window.AudioContext || window.webkitAudioContext)();

  const video = document.getElementById('demoVideo');
  if (video && !video.hasAudioConnected) {
    if (window.__slideAudioPatchInstalled) {
      const source = audioCtx.createMediaElementSource(video);
      source.connect(audioCtx.destination);
      video.hasAudioConnected = true;
    }
  }
}

function playTone(type, freqStart, freqEnd, delay, fadeInTime, vol, duration, Q) {
  if (!audioCtx || isMuted) return;
  const t = audioCtx.currentTime + delay;
  const osc = audioCtx.createOscillator();
  const gain = audioCtx.createGain();
  const filter = audioCtx.createBiquadFilter();
  filter.type = 'lowpass';
  filter.frequency.value = 3000;
  if (Q) filter.Q.value = Q;
  osc.type = type;
  osc.frequency.setValueAtTime(freqStart, t);
  if (freqEnd !== freqStart) osc.frequency.exponentialRampToValueAtTime(freqEnd, t + duration);
  gain.gain.setValueAtTime(0, t);
  gain.gain.linearRampToValueAtTime(vol, t + fadeInTime);
  gain.gain.exponentialRampToValueAtTime(0.001, t + duration);
  osc.connect(filter); filter.connect(gain); gain.connect(audioCtx.destination);
  osc.start(t); osc.stop(t + duration + 0.05);
}

function playNoiseWhoosh(freqStart, freqEnd, duration) {
  if (!audioCtx || isMuted) return;
  const t = audioCtx.currentTime;
  const noise = createNoiseBuffer(duration + 0.1);
  const src = audioCtx.createBufferSource();
  src.buffer = noise;
  const filter = audioCtx.createBiquadFilter();
  filter.type = 'bandpass';
  filter.frequency.setValueAtTime(freqStart, t);
  filter.frequency.exponentialRampToValueAtTime(freqEnd, t + duration);
  filter.Q.value = 0.8;
  const gain = audioCtx.createGain();
  gain.gain.setValueAtTime(0, t);
  gain.gain.linearRampToValueAtTime(0.04, t + 0.04);
  gain.gain.exponentialRampToValueAtTime(0.001, t + duration);
  src.connect(filter); filter.connect(gain); gain.connect(audioCtx.destination);
  src.start(t); src.stop(t + duration + 0.1);
}

function createNoiseBuffer(duration) {
  const sampleRate = audioCtx.sampleRate;
  const length = sampleRate * duration;
  const buffer = audioCtx.createBuffer(1, length, sampleRate);
  const data = buffer.getChannelData(0);
  for (let i = 0; i < length; i++) data[i] = (Math.random() * 2 - 1) * 0.5;
  return buffer;
}

// ============================================
// 12 TRANSITION SOUNDS
// ============================================
const transitionLib = {
  gong() { playTone('sine', 130, 260, 0, 0.6, 0.09, 0.8); playTone('triangle', 260, 520, 0.08, 0.4, 0.06, 0.6); playNoiseWhoosh(400, 1200, 0.35); },
  rise() { playTone('sine', 220, 440, 0, 0.5, 0.08, 0.6); playTone('triangle', 330, 660, 0.1, 0.35, 0.05, 0.5); },
  bass() { playTone('sine', 65, 55, 0, 0.12, 0.07, 0.7); playTone('triangle', 130, 110, 0.1, 0.08, 0.05, 0.5); playTone('sine', 65, 55, 0.35, 0.10, 0.06, 0.5); },
  chime() { playTone('sine', 880, 1320, 0, 0.6, 0.07, 0.5); playTone('sine', 1100, 880, 0.1, 0.4, 0.04, 0.4); },
  sweep() { playTone('sine', 440, 880, 0, 0.5, 0.07, 0.55); playNoiseWhoosh(600, 3000, 0.3); playTone('triangle', 660, 880, 0.12, 0.3, 0.04, 0.4); },
  boom() { playTone('sine', 80, 60, 0, 0.15, 0.09, 0.8); playTone('sine', 160, 120, 0.05, 0.12, 0.06, 0.6); playNoiseWhoosh(200, 600, 0.4); },
  alarm() { playTone('sawtooth', 370, 185, 0, 0.04, 0.07, 0.25, 2.5); playTone('sawtooth', 370, 185, 0.15, 0.04, 0.07, 0.25, 2.5); playNoiseWhoosh(1500, 3500, 0.2); },
  chord() { playTone('sine', 440, 440, 0, 0.55, 0.06, 0.7); playTone('sine', 554, 554, 0.06, 0.45, 0.05, 0.6); playTone('sine', 660, 660, 0.12, 0.4, 0.04, 0.55); playTone('sine', 880, 880, 0.2, 0.3, 0.03, 0.5); },
  ascending() { playTone('sine', 330, 440, 0, 0.5, 0.06, 0.4); playTone('sine', 440, 554, 0.15, 0.4, 0.05, 0.4); playTone('sine', 554, 660, 0.3, 0.35, 0.04, 0.4); },
  retro() { playTone('square', 262, 131, 0, 0.15, 0.03, 0.15, 2); playTone('square', 393, 262, 0.08, 0.12, 0.03, 0.15, 2); playNoiseWhoosh(2000, 500, 0.1); },
  minimal() { playTone('sine', 523, 784, 0, 0.4, 0.04, 0.3); },
  dramatic() { playTone('sine', 110, 55, 0, 0.15, 0.1, 1.0); playNoiseWhoosh(100, 400, 0.6); playTone('sine', 220, 440, 0.3, 0.5, 0.07, 0.6); }
};

// ============================================
// 12 REVEAL SOUNDS
// ============================================
const revealLib = {
  ping() { playTone('sine', 1000, 1400, 0, 0.03, 0.06, 0.18); },
  pop() { playTone('sine', 800, 1200, 0, 0.02, 0.06, 0.15); playTone('sine', 1200, 600, 0, 0.02, 0.03, 0.12); },
  chime() { playTone('sine', 1200, 1800, 0, 0.02, 0.05, 0.3); playTone('sine', 1500, 2000, 0.06, 0.02, 0.03, 0.25); },
  click() { playTone('triangle', 2000, 800, 0, 0.01, 0.07, 0.06); },
  bubble() { const b = 400 + Math.random() * 400; playTone('sine', b, b * 2, 0, 0.02, 0.05, 0.2); playTone('sine', b * 1.3, b * 2.5, 0.05, 0.02, 0.03, 0.15); },
  woosh() { playNoiseWhoosh(800 + Math.random() * 400, 2000 + Math.random() * 1000, 0.15); },
  sparkle() { playTone('sine', 1500, 2200, 0, 0.02, 0.04, 0.2); playTone('sine', 2000, 2800, 0.04, 0.02, 0.03, 0.18); playTone('sine', 2500, 1800, 0.08, 0.02, 0.02, 0.15); },
  drop() { playTone('sine', 1400, 400, 0, 0.02, 0.06, 0.2); },
  tick() { playTone('triangle', 3000, 1500, 0, 0.01, 0.05, 0.05); },
  bell() { playTone('sine', 880, 1100, 0, 0.02, 0.05, 0.35); playTone('sine', 1100, 880, 0.08, 0.02, 0.03, 0.3); },
  blip() { playTone('square', 600, 900, 0, 0.02, 0.03, 0.08, 3); },
  snap() { playTone('triangle', 4000, 500, 0, 0.01, 0.06, 0.04); playNoiseWhoosh(3000, 1000, 0.05); }
};

// ============================================
// PLAY DISPATCHER
// ============================================
function playSlideSound(slideIndex) {
  if (!audioCtx || isMuted) return;
  const key = slideTransitions[slideIndex] || 'minimal';
  if (transitionLib[key]) transitionLib[key]();
}

function playRevealSound() {
  if (!audioCtx || isMuted) return;
  const key = slideReveals[currentSlide] || 'ping';
  if (revealLib[key]) revealLib[key]();
}

// ============================================
// BACKGROUND MUSIC
// ============================================
let bgChordIndex = 0;
let bgArpInterval = null;
let bgChordInterval = null;

const chordProgression = [
  [130.81, 164.81, 196.00, 246.94],
  [110.00, 130.81, 164.81, 196.00],
  [87.31, 130.81, 164.81, 207.65],
  [98.00, 123.47, 146.83, 174.61],
];

const arpNotes = [
  [523.25, 659.25, 783.99, 987.77],
  [440.00, 523.25, 659.25, 783.99],
  [349.23, 523.25, 659.25, 830.61],
  [392.00, 493.88, 587.33, 698.46],
];

function startBackgroundMusic() {
  if (!audioCtx || isMuted) return;
  if (bgMusicGain) return;
  bgMusicGain = audioCtx.createGain();
  bgMusicGain.gain.setValueAtTime(0, audioCtx.currentTime);
  bgMusicGain.gain.linearRampToValueAtTime(0.12, audioCtx.currentTime + 4);
  bgMusicGain.connect(audioCtx.destination);
  bgChordIndex = 0;
  playChord();
  bgChordInterval = setInterval(() => {
    bgChordIndex = (bgChordIndex + 1) % chordProgression.length;
    playChord();
  }, 8000);
  playArpNote();
  bgArpInterval = setInterval(playArpNote, 2000);
}

function playChord() {
  if (!audioCtx || !bgMusicGain || isMuted) return;
  const chord = chordProgression[bgChordIndex];
  chord.forEach((freq, i) => {
    const osc1 = audioCtx.createOscillator();
    const osc2 = audioCtx.createOscillator();
    const gain = audioCtx.createGain();
    const filter = audioCtx.createBiquadFilter();
    osc1.type = 'sine'; osc1.frequency.value = freq;
    osc2.type = 'triangle'; osc2.frequency.value = freq * 1.003;
    filter.type = 'lowpass'; filter.frequency.value = 350 + i * 50; filter.Q.value = 0.5;
    const t = audioCtx.currentTime;
    gain.gain.setValueAtTime(0, t);
    gain.gain.linearRampToValueAtTime(0.18, t + 2);
    gain.gain.setValueAtTime(0.18, t + 5);
    gain.gain.linearRampToValueAtTime(0.001, t + 8);
    osc1.connect(gain); osc2.connect(gain); gain.connect(filter); filter.connect(bgMusicGain);
    osc1.start(t + i * 0.15); osc2.start(t + i * 0.15);
    osc1.stop(t + 8.5); osc2.stop(t + 8.5);
  });
}

let arpNoteIndex = 0;
function playArpNote() {
  if (!audioCtx || !bgMusicGain || isMuted) return;
  const notes = arpNotes[bgChordIndex];
  const note = notes[arpNoteIndex % notes.length];
  arpNoteIndex++;
  const osc = audioCtx.createOscillator();
  const gain = audioCtx.createGain();
  const filter = audioCtx.createBiquadFilter();
  const delay = audioCtx.createDelay(1.0);
  const delayGain = audioCtx.createGain();
  osc.type = 'sine'; osc.frequency.value = note;
  filter.type = 'lowpass'; filter.frequency.value = 2000; filter.Q.value = 0.3;
  const t = audioCtx.currentTime;
  gain.gain.setValueAtTime(0, t);
  gain.gain.linearRampToValueAtTime(0.12, t + 0.05);
  gain.gain.exponentialRampToValueAtTime(0.03, t + 0.8);
  gain.gain.linearRampToValueAtTime(0.001, t + 2.5);
  delay.delayTime.value = 0.375; delayGain.gain.value = 0.25;
  osc.connect(filter); filter.connect(gain); gain.connect(bgMusicGain);
  gain.connect(delay); delay.connect(delayGain); delayGain.connect(bgMusicGain);
  osc.start(t); osc.stop(t + 3);
}

function stopBackgroundMusic() {
  if (bgChordInterval) { clearInterval(bgChordInterval); bgChordInterval = null; }
  if (bgArpInterval) { clearInterval(bgArpInterval); bgArpInterval = null; }
  if (bgMusicGain) {
    bgMusicGain.gain.linearRampToValueAtTime(0, audioCtx.currentTime + 1);
    setTimeout(() => { bgMusicGain = null; }, 1500);
  }
}

function toggleMute() {
  isMuted = !isMuted;
  const muteBtn = document.getElementById('muteBtn');
  if (isMuted) {
    muteBtn.innerHTML = '<i class="fa-solid fa-volume-xmark"></i>';
    stopBackgroundMusic();
  } else {
    muteBtn.innerHTML = '<i class="fa-solid fa-volume-high"></i>';
    initAudio();
    startBackgroundMusic();
  }
}

// ============================================
// SLIDE NAVIGATION
// ============================================
function init() {
  resetToStart();
  container.addEventListener('click', handleClick);
  document.addEventListener('keydown', handleKeyboard);
  let touchStartX = 0, touchStartY = 0;
  container.addEventListener('touchstart', (e) => {
    touchStartX = e.touches[0].clientX; touchStartY = e.touches[0].clientY;
  }, { passive: true });
  container.addEventListener('touchend', (e) => {
    const dx = e.changedTouches[0].clientX - touchStartX;
    const dy = e.changedTouches[0].clientY - touchStartY;
    if (Math.abs(dx) > Math.abs(dy) && Math.abs(dx) > 40) {
      if (dx < 0) nextSlide(); else prevSlide();
    }
  }, { passive: true });
  updateAudioPanel();
}

function handleClick(e) {
  initAudio();
  if (!bgMusicGain && !isMuted && currentSlide > 0) startBackgroundMusic();
  if (e.target.closest('.side-controls')) return;
  if (isReady) {
    isReady = false;
    updateProgress();
    updateAudioPanel();
    playSlideSound(0);
    const firstEl = slides[0].querySelector('.slide-element');
    if (firstEl && !firstEl.classList.contains('visible')) {
      firstEl.classList.add('visible');
      createParticleBurst(firstEl);
      triggerShimmer(firstEl.querySelector('.title-shimmer'));
    }
    playRevealSound();
    return;
  }
  const slide = slides[currentSlide];
  const mode = slide.dataset.mode;
  if (mode === 'highlight') {
    const elements = slide.querySelectorAll('.slide-element');
    const hidden = Array.from(elements).filter(el => !el.classList.contains('visible'));
    if (hidden.length > 0) { revealNextElement(); return; }
    const cards = slide.querySelectorAll('.highlightable');
    const highlighted = slide.querySelectorAll('.highlighted');
    if (highlighted.length < cards.length) {
      cards[highlighted.length].classList.add('highlighted');
      playRevealSound(); createParticleBurst(cards[highlighted.length]); return;
    }
    nextSlide(); return;
  }
  if (mode === 'traffic-light') {
    const elements = slide.querySelectorAll('.slide-element');
    const hidden = Array.from(elements).filter(el => !el.classList.contains('visible'));
    if (hidden.length > 0) { revealNextElement(); return; }
    const dots = slide.querySelectorAll('.lightable');
    const lit = slide.querySelectorAll('.lightable[class*="lit-"]');
    if (lit.length < dots.length) {
      const color = dots[lit.length].dataset.lightColor;
      dots[lit.length].classList.add('lit-' + color);
      playRevealSound(); createParticleBurst(dots[lit.length]); return;
    }
    nextSlide(); return;
  }
  const elements = slide.querySelectorAll('.slide-element');
  const hidden = Array.from(elements).filter(el => !el.classList.contains('visible'));
  if (hidden.length > 0) revealNextElement(); else nextSlide();
}

function handleKeyboard(e) {
  if (e.key === 'ArrowRight' || e.key === ' ') {
    e.preventDefault();
    handleClick({ target: container, closest: () => null });
  } else if (e.key === 'ArrowLeft') {
    e.preventDefault(); prevSlide();
  }
}

function nextSlide() {
  if (isAnimating) return;
  if (currentSlide >= totalSlides - 1) { resetToStart(); return; }
  goToSlide(currentSlide + 1);
}

function prevSlide() {
  if (isAnimating) return;
  if (currentSlide <= 0) { resetToStart(); return; }
  goToSlide(currentSlide - 1);
}

function resetToStart() {
  isAnimating = false;
  isReady = true;
  slides.forEach((slide, i) => {
    slide.classList.remove('active');
    slide.style.transform = '';
    slide.style.opacity = '';
    resetElements(i);
  });
  currentSlide = 0;
  slides[0].classList.add('active');
  slides[0].style.transform = '';
  slides[0].style.opacity = '';
  updateProgress();
  updateAudioPanel();
}

function goToSlide(index) {
  if (index === currentSlide || isAnimating) return;
  isAnimating = true;
  initAudio();
  if (!bgMusicGain && !isMuted && index > 0) startBackgroundMusic();
  playSlideSound(index);
  const direction = index > currentSlide ? 1 : -1;
  const oldSlide = slides[currentSlide];
  const newSlide = slides[index];
  resetElements(index);
  oldSlide.classList.remove('active');
  oldSlide.style.transform = direction > 0 ? 'translateX(-60px)' : 'translateX(60px)';
  oldSlide.style.opacity = '0';
  newSlide.style.transform = direction > 0 ? 'translateX(60px)' : 'translateX(-60px)';
  newSlide.style.opacity = '0';
  requestAnimationFrame(() => {
    newSlide.classList.add('active');
    newSlide.style.transform = ''; newSlide.style.opacity = '';
    currentSlide = index;
    updateProgress();
    updateAudioPanel();
    setTimeout(() => {
      isAnimating = false;
      oldSlide.style.transform = ''; oldSlide.style.opacity = '';
      const firstEl = newSlide.querySelector('.slide-element');
      if (firstEl && !firstEl.classList.contains('visible')) {
        firstEl.classList.add('visible'); 
        playRevealSound();
        createParticleBurst(firstEl);
      }
    }, 420);
  });
}

function revealNextElement() {
  const slide = slides[currentSlide];
  const hidden = Array.from(slide.querySelectorAll('.slide-element')).filter(el => !el.classList.contains('visible'));
  if (hidden.length > 0) {
    hidden[0].classList.add('visible'); 
    playRevealSound(); 
    createParticleBurst(hidden[0]);
    
    const vid = hidden[0].querySelector('video');
    if (vid) {
      vid.play();
      if (currentSlide === 0) {
        const titleEl = slide.querySelector('.slide-element');
        if (titleEl) {
          titleEl.style.opacity = '0';
          titleEl.style.pointerEvents = 'none';
        }
      }
    }
  }
}

function resetElements(slideIndex) {
  const slide = slides[slideIndex];
  slide.querySelectorAll('.slide-element').forEach(el => {
    el.classList.remove('visible');
    el.style.opacity = '';
    el.style.pointerEvents = '';
  });
  slide.querySelectorAll('.highlighted').forEach(el => el.classList.remove('highlighted'));
  slide.querySelectorAll('.lightable').forEach(el => el.classList.remove('lit-red', 'lit-yellow', 'lit-green'));
  
  const vid = slide.querySelector('video');
  if (vid) {
    vid.pause();
    vid.currentTime = 0;
  }
}

function triggerShimmer(el) {
  if (!el) return;
  const shimmer = document.createElement('div');
  shimmer.style.cssText = `
    position: absolute; top: 0; left: 0; width: 60%; height: 100%;
    background: linear-gradient(90deg, transparent 0%, rgba(255,255,255,0.15) 40%, rgba(255,255,255,0.3) 50%, rgba(255,255,255,0.15) 60%, transparent 100%);
    pointer-events: none; z-index: 10;
    transform: translateX(-160%);
    will-change: transform;
  `;
  el.style.position = 'relative';
  el.style.overflow = 'hidden';
  el.appendChild(shimmer);
  void shimmer.offsetWidth;
  shimmer.style.transition = 'transform 2.5s ease-in-out';
  setTimeout(() => { shimmer.style.transform = 'translateX(300%)'; }, 50);
  setTimeout(() => shimmer.remove(), 3000);
}

function createParticleBurst(element) {
  // Wait slightly longer (100ms) to ensure flexbox layouts have fully settled
  setTimeout(() => {
    // Focus the burst specifically on the icon rather than the whole block container
    let targetEl = element.querySelector('.icon-badge, .card-icon, i') || element;
    
    const rect = targetEl.getBoundingClientRect();
    const containerRect = container.getBoundingClientRect();
    if (!rect || !containerRect) return;

    // Detect CSS scale transform on container (used during auto_render recording)
    const scaleX = containerRect.width / container.offsetWidth || 1;
    const scaleY = containerRect.height / container.offsetHeight || 1;

    const cx = (rect.left - containerRect.left + rect.width / 2) / scaleX;
    const cy = (rect.top - containerRect.top + rect.height / 2) / scaleY;
    
    const particleCount = 30; // Increased for a much more vibrant burst
    const colors = [
      '#FF3F8E', '#04C2C9', '#2E5BFF', '#FF9F00', 
      '#00E676', '#D500F9', '#FFEA00', '#FF1744',
      '#00e5ff', '#84ffff', '#1de9b6', '#a7ffeb'
    ];

    for (let i = 0; i < particleCount; i++) {
      const particle = document.createElement('div');
      particle.className = 'particle';
      
      const angle = (Math.PI * 2 * i) / particleCount + (Math.random() * 0.4 - 0.2);
      const distance = 50 + Math.random() * 90;
      const dx = Math.cos(angle) * distance;
      const dy = Math.sin(angle) * distance;
      const size = 3 + Math.random() * 5;
      const color = colors[Math.floor(Math.random() * colors.length)];
      const duration = 0.7 + Math.random() * 0.5;
      
      particle.style.cssText = `
        position: absolute;
        width: ${size}px;
        height: ${size}px;
        border-radius: 50%;
        background: ${color};
        left: ${cx}px;
        top: ${cy}px;
        pointer-events: none;
        z-index: 2000;
        opacity: 1;
        box-shadow: 0 0 15px ${color}, 0 0 5px ${color};
        transition: transform ${duration}s cubic-bezier(0.22, 1, 0.36, 1), opacity ${duration}s ease-out;
      `;
      
      container.appendChild(particle);
      
      // Force reflow để trình duyệt vẽ trạng thái ban đầu (quan trọng cho headless render)
      void particle.offsetWidth;
      
      // Thay requestAnimationFrame bằng setTimeout 50ms để đảm bảo headless browser 
      // có đủ thời gian vẽ frame đầu tiên trước khi chuyển state
      setTimeout(() => {
        particle.style.transform = `translate(${dx}px, ${dy}px) scale(0)`;
        particle.style.opacity = '0';
      }, 50);
      
      setTimeout(() => particle.remove(), duration * 1000 + 100);
    }
  }, 100);
}

function updateProgress() {
  if (isReady) {
    progressFill.style.width = '0%';
    currentSlideEl.textContent = '0';
  } else {
    progressFill.style.width = ((currentSlide + 1) / totalSlides) * 100 + '%';
    currentSlideEl.textContent = currentSlide + 1;
  }
}

function sleep(ms) { return new Promise(r => setTimeout(r, ms)); }

async function autoPlay() {
  initAudio(); startBackgroundMusic();
  for (let i = 0; i < totalSlides; i++) {
    if (i > 0) { goToSlide(i); await sleep(800); }
    const els = slides[i].querySelectorAll('.slide-element');
    for (let j = 0; j < els.length; j++) {
      els[j].classList.add('visible'); playRevealSound(); createParticleBurst(els[j]); await sleep(600);
    }
    await sleep(Math.floor(90000 / totalSlides) - els.length * 600);
  }
}

// ============================================
// AUDIO PANEL CONTROLLER
// ============================================
function updateAudioPanel() {
  const badge = document.getElementById('audioPanelSlide');
  const tSel = document.getElementById('transitionSelect');
  const rSel = document.getElementById('revealSelect');
  if (isReady) {
    if (badge) badge.textContent = '⏸ Ready';
  } else {
    if (badge) badge.textContent = 'Slide ' + (currentSlide + 1);
  }
  if (tSel) tSel.value = slideTransitions[currentSlide] || 'minimal';
  if (rSel) rSel.value = slideReveals[currentSlide] || 'ping';
  updateScriptPanel();
}

function updateScriptPanel() {
  const sBadge = document.getElementById('scriptSlideBadge');
  const sText = document.getElementById('scriptText');
  if (!sBadge || !sText) return;
  if (isReady) {
    sBadge.textContent = '⏸ Ready';
    sText.textContent = 'Bấm vào slide để bắt đầu...';
  } else {
    sBadge.textContent = 'Slide ' + (currentSlide + 1);
    sText.textContent = slideScripts[currentSlide] || '';
  }
}

function setSlideTransition(idx, value) {
  slideTransitions[idx] = value;
  initAudio();
  if (!isMuted) playSlideSound(idx);
}

function setSlideReveal(idx, value) {
  slideReveals[idx] = value;
  initAudio();
  if (!isMuted) playRevealSound();
}

function audioPanelPrev() { if (currentSlide > 0) goToSlide(currentSlide - 1); }
function audioPanelNext() { if (currentSlide < totalSlides - 1) goToSlide(currentSlide + 1); }

function testTransition() {
  initAudio();
  if (!bgMusicGain && !isMuted) startBackgroundMusic();
  playSlideSound(currentSlide);
}

function testReveal() {
  initAudio();
  if (!bgMusicGain && !isMuted) startBackgroundMusic();
  playRevealSound();
}

// BGM Presets
const bgmPresets = {
  ambient: { chords: [[130.81, 164.81, 196, 246.94], [110, 130.81, 164.81, 196], [87.31, 130.81, 164.81, 207.65], [98, 123.47, 146.83, 174.61]], arps: [[523.25, 659.25, 783.99, 987.77], [440, 523.25, 659.25, 783.99], [349.23, 523.25, 659.25, 830.61], [392, 493.88, 587.33, 698.46]] },
  cinematic: { chords: [[146.83, 174.61, 220, 277.18], [116.54, 146.83, 174.61, 220], [98, 116.54, 146.83, 174.61], [110, 138.59, 164.81, 220]], arps: [[293.66, 349.23, 440, 554.37], [233.08, 293.66, 349.23, 440], [196, 233.08, 293.66, 349.23], [220, 277.18, 329.63, 440]] },
  lofi: { chords: [[130.81, 155.56, 196, 233.08], [110, 138.59, 164.81, 207.65], [87.31, 110, 130.81, 164.81], [98, 123.47, 155.56, 185]], arps: [[523.25, 622.25, 783.99, 932.33], [440, 554.37, 659.25, 783.99], [349.23, 440, 523.25, 659.25], [392, 493.88, 622.25, 739.99]] },
  piano: { chords: [[130.81, 164.81, 196, 261.63], [146.83, 174.61, 220, 293.66], [87.31, 110, 130.81, 174.61], [98, 123.47, 146.83, 196]], arps: [[261.63, 329.63, 392, 523.25], [293.66, 349.23, 440, 587.33], [174.61, 220, 261.63, 349.23], [196, 246.94, 293.66, 392]] },
  dark: { chords: [[65.41, 82.41, 98, 123.47], [73.42, 87.31, 110, 130.81], [55, 69.3, 82.41, 103.83], [61.74, 77.78, 92.5, 116.54]], arps: [[261.63, 311.13, 392, 466.16], [293.66, 349.23, 440, 523.25], [220, 277.18, 329.63, 415.3], [246.94, 311.13, 369.99, 466.16]] }
};

function switchBGM(preset) {
  if (preset === 'none') { stopBackgroundMusic(); return; }
  const p = bgmPresets[preset];
  if (!p) return;
  for (let i = 0; i < 4; i++) {
    chordProgression[i] = p.chords[i];
    arpNotes[i] = p.arps[i];
  }
  stopBackgroundMusic();
  setTimeout(() => { if (!isMuted) startBackgroundMusic(); }, 1200);
}

// Initialize
document.addEventListener('DOMContentLoaded', init);
