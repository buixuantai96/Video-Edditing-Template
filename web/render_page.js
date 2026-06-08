(() => {
  const MAX_AUDIO_BYTES = 200 * 1024 * 1024;
  const MAX_TEMPLATE_ARCHIVE_BYTES = 80 * 1024 * 1024;
  const fallbackProject = window.__RENDER_PROJECT__;
  const fallbackOutputUrl = window.__RENDER_OUTPUT_URL__;
  const projects = Array.isArray(window.__PROJECTS__) && window.__PROJECTS__.length
    ? window.__PROJECTS__
    : (fallbackProject ? [{ name: fallbackProject, url: `/slide/${fallbackProject}/`, output_url: fallbackOutputUrl, video_url: fallbackOutputUrl, script_count: 0 }] : []);
  const projectMap = new Map(projects.map((project) => [project.name, project]));
  const templates = Array.isArray(window.__TEMPLATES__) ? window.__TEMPLATES__ : [];
  const templateMap = new Map(templates.map((template) => [template.name, template]));
  const voiceConnections = Array.isArray(window.__VOICE_CONNECTIONS__) ? window.__VOICE_CONNECTIONS__ : [];
  const voiceConnectionMap = new Map(voiceConnections.map((connection) => [String(connection.id || ''), connection]));
  const defaultVoiceConnectionId = String(window.__DEFAULT_VOICE_CONNECTION_ID__ || '');
  const state = {
    engine: 'elevenlabs',
    template: '',
    videoDuration: 'short',
    jobId: null,
    pollTimer: null,
    voiceJobId: null,
    voicePollTimer: null,
    voicePreviewCurrent: false,
    voicePreview: null,
    project: null,
    outputUrl: null,
    busy: false,
    canceling: false,
    uploadProject: null,
    socialReady: { youtube: false, facebook: false },
    facebookCommentTargetId: '',
    facebookActivePageId: '',
  };
  const slideComposerState = {
    project: '',
    slides: [],
    selected: 0,
    mediaFiles: [],
    busy: false,
  };
  let elevenVoiceSaveTimer = null;
  let elevenVoiceSaving = false;
  let elevenApiKeySaveTimer = null;
  let elevenApiKeySaving = false;
  let elevenProxySaveTimer = null;
  let elevenProxySaving = false;
  let facebookConfigSaving = false;
  const videoDurationOptions = {
    auto: { label: 'Mục tiêu: AI chọn theo nội dung', storyboard: 'AI chọn độ dài theo số scene.' },
    short: { label: 'Mục tiêu: ngắn ~60s', storyboard: 'Ưu tiên nhịp ngắn khoảng 60 giây.' },
    medium: { label: 'Mục tiêu: vừa ~90s', storyboard: 'Ưu tiên nhịp vừa khoảng 90 giây.' },
    long: { label: 'Mục tiêu: dài ~120s', storyboard: 'Ưu tiên nhịp dài khoảng 120 giây.' },
  };

  function $(selector) {
    return document.querySelector(selector);
  }

  function $all(selector) {
    return Array.from(document.querySelectorAll(selector));
  }

  function setButtonLabel(button, label) {
    const labelEl = button?.querySelector?.('span:last-child');
    if (labelEl) labelEl.textContent = label;
    else if (button) button.textContent = label;
  }

  function applyTheme(theme) {
    const nextTheme = theme === 'light' ? 'light' : 'dark';
    document.body.classList.toggle('theme-light', nextTheme === 'light');
    localStorage.setItem('viro-theme', nextTheme);
    $all('[data-theme-toggle]').forEach((button) => {
      const icon = button.querySelector('.btn-icon');
      const label = button.querySelector('span:last-child');
      if (icon) icon.textContent = nextTheme === 'light' ? '☀' : '☾';
      if (label) label.textContent = nextTheme === 'light' ? 'Light' : 'Dark';
    });
  }

  function toggleTheme() {
    applyTheme(document.body.classList.contains('theme-light') ? 'dark' : 'light');
  }

  function currentProject() {
    return projectMap.get(state.project) || null;
  }

  function rowForProject(projectName) {
    return $all('.project-row[data-project]').find((element) => element.dataset.project === projectName) || null;
  }

  function setStatus(message, tone = 'warn') {
    const status = $('#renderStatus');
    if (!status) return;
    status.textContent = message;
    status.hidden = !message;
    status.className = `status ${tone}`;
  }

  function setCookie(name, value, maxAgeSeconds = 31536000) {
    document.cookie = `${encodeURIComponent(name)}=${encodeURIComponent(value)}; path=/; max-age=${maxAgeSeconds}; SameSite=Lax`;
  }

  function switchLanguage(lang) {
    const nextLang = lang === 'en' ? 'en' : 'vi';
    localStorage.setItem('viro-lang', nextLang);
    setCookie('viro_lang', nextLang);
    const url = new URL(window.location.href);
    url.searchParams.set('lang', nextLang);
    window.location.href = `${url.pathname}${url.search}${url.hash}`;
  }

  function setSelectValueIfPresent(select, value) {
    if (!select) return false;
    const normalized = String(value || '');
    if (!normalized) return false;
    const hasValue = Array.from(select.options || []).some((option) => option.value === normalized);
    if (!hasValue) return false;
    select.value = normalized;
    return true;
  }

  function studioInputMode() {
    const mode = new URLSearchParams(window.location.search).get('inputMode') || 'ai';
    return ['ai', 'import', 'voiceScript'].includes(mode) ? mode : 'ai';
  }

  function studioUrlForMode(inputMode) {
    const mode = ['ai', 'import', 'voiceScript'].includes(inputMode) ? inputMode : 'ai';
    const url = new URL('/studio', window.location.origin);
    url.searchParams.set('inputMode', mode);
    if (state.template) url.searchParams.set('template', state.template);
    if (state.project) url.searchParams.set('project', state.project);
    return `${url.pathname}${url.search}`;
  }

  function syncStudioModeActions() {
    $all('.mode-tabs a[data-input-mode]').forEach((link) => {
      link.href = studioUrlForMode(link.dataset.inputMode || 'ai');
    });
    $all('[data-create-project-open]').forEach((button) => {
      if (state.template) button.dataset.template = state.template;
      button.dataset.inputMode = studioInputMode();
    });
  }

  function setVideoDuration(duration = 'short') {
    const normalized = Object.prototype.hasOwnProperty.call(videoDurationOptions, duration) ? duration : 'short';
    state.videoDuration = normalized;
    localStorage.setItem('viro-video-duration', normalized);
    $all('[data-duration]').forEach((button) => {
      button.classList.toggle('active', button.dataset.duration === normalized);
    });
    const summary = $('#videoDurationSummary');
    if (summary) summary.textContent = videoDurationOptions[normalized].label;
    const storyboardButton = $('#createStoryboard');
    if (storyboardButton) storyboardButton.dataset.duration = normalized;
  }

  function videoDurationStoryboardHint() {
    return videoDurationOptions[state.videoDuration]?.storyboard || videoDurationOptions.short.storyboard;
  }

  function currentVoiceConnectionId() {
    const select = $('#elevenCredentialSelect');
    return String(select?.value || '');
  }

  function syncVoiceCredentialStatus(config = {}) {
    const stateEl = $('#elevenApiKeyState');
    if (!stateEl) return;
    const connection = voiceConnectionMap.get(currentVoiceConnectionId());
    if (connection) {
      const provider = connection.provider === 'apikeyrotator' ? 'APIKeyRotator' : 'ElevenLabs';
      stateEl.textContent = `Sẽ dùng ${provider}: ${connection.name || connection.label || connection.id}`;
      return;
    }
    if (config.builtin_rotator_configured) {
      stateEl.textContent = `Đang dùng Viro Key Rotate (${config.registry_key_count || 0} key)`;
    }
  }

  function syncVoiceCredentialForProject(project = currentProject()) {
    const select = $('#elevenCredentialSelect');
    if (!select) return;
    const projectVoice = String(project?.voice_connection_id || '');
    if (!setSelectValueIfPresent(select, projectVoice)) {
      setSelectValueIfPresent(select, defaultVoiceConnectionId);
    }
    syncVoiceCredentialStatus();
  }

  function createProjectStatus(message = '', tone = 'warn') {
    const status = $('.create-project-status');
    if (!status) return;
    status.hidden = !message;
    status.textContent = message;
    status.className = `status ${tone} create-project-status`;
  }

  function openCreateProjectModal(trigger = null) {
    const modal = $('#createProjectModal');
    const form = $('#createProjectForm');
    if (!modal || !form) return;
    const selectedTemplate = state.template || trigger?.dataset?.template || localStorage.getItem('viro-selected-template') || '';
    const templateSelect = form.querySelector('select[name="template"]');
    if (templateSelect && selectedTemplate) templateSelect.value = selectedTemplate;
    const inputMode = trigger?.dataset?.inputMode || studioInputMode();
    const inputModeField = form.querySelector('input[name="input_mode"]');
    if (inputModeField) inputModeField.value = ['ai', 'import', 'voiceScript'].includes(inputMode) ? inputMode : 'ai';
    const languageSelect = form.querySelector('select[name="language"]');
    if (languageSelect) languageSelect.value = window.__VIRO_LANG__ === 'en' ? 'en' : 'vi';
    const voiceSelect = form.querySelector('select[name="voice_connection_id"]');
    if (voiceSelect) {
      const template = templateMap.get(selectedTemplate);
      const templateVoice = String(template?.voice_connection_id || template?.voice?.connection_id || '');
      setSelectValueIfPresent(voiceSelect, templateVoice || defaultVoiceConnectionId);
    }
    createProjectStatus('');
    modal.hidden = false;
    window.setTimeout(() => form.querySelector('input[name="name"]')?.focus(), 0);
  }

  function closeCreateProjectModal() {
    const modal = $('#createProjectModal');
    if (modal) modal.hidden = true;
  }

  function openSaveProjectTemplateModal(projectName = state.project) {
    const modal = $('#saveProjectTemplateModal');
    const form = $('#saveProjectTemplateForm');
    const name = String(projectName || state.project || '').trim();
    const project = projectMap.get(name);
    if (!modal || !form || !project) return;
    form.reset();
    form.elements.project.value = project.name;
    form.elements.project_display.value = project.name;
    form.elements.title.value = `${project.title || project.name} template`;
    const sourceTemplate = templateMap.get(project.template || '');
    if (form.elements.pack) form.elements.pack.value = sourceTemplate?.pack || 'Project snapshots';
    if (form.elements.variant) form.elements.variant.value = sourceTemplate?.variant || 'Default';
    if (form.elements.aspect_ratio) form.elements.aspect_ratio.value = sourceTemplate?.aspect_ratio || '9:16';
    if (form.elements.platforms) {
      form.elements.platforms.value = Array.isArray(sourceTemplate?.platforms) ? sourceTemplate.platforms.join(', ') : '';
    }
    if (form.elements.tags) {
      form.elements.tags.value = Array.isArray(sourceTemplate?.tags) ? sourceTemplate.tags.join(', ') : '';
    }
    setTemplateStatus(form, '');
    modal.hidden = false;
    window.setTimeout(() => form.elements.title?.focus(), 0);
  }

  function closeSaveProjectTemplateModal() {
    const modal = $('#saveProjectTemplateModal');
    if (modal) modal.hidden = true;
  }

  function setTemplateStatus(form, message = '', tone = 'warn') {
    const status = form?.querySelector?.('.template-status');
    if (!status) return;
    status.hidden = !message;
    status.textContent = message;
    status.className = `status ${tone} template-status`;
  }

  function openCreateTemplateModal() {
    const modal = $('#createTemplateModal');
    const form = $('#createTemplateForm');
    if (!modal || !form) return;
    form.reset();
    const baseSelect = form.querySelector('select[name="base_template"]');
    if (baseSelect && state.template) baseSelect.value = state.template;
    const template = templateMap.get(baseSelect?.value || state.template);
    if (form.elements.pack) form.elements.pack.value = template?.pack || 'General';
    if (form.elements.variant) form.elements.variant.value = template?.variant || 'Default';
    if (form.elements.aspect_ratio) form.elements.aspect_ratio.value = template?.aspect_ratio || '9:16';
    if (form.elements.platforms) {
      form.elements.platforms.value = Array.isArray(template?.platforms) ? template.platforms.join(', ') : '';
    }
    const voiceSelect = form.querySelector('select[name="voice_connection_id"]');
    if (voiceSelect) {
      const templateVoice = String(template?.voice_connection_id || template?.voice?.connection_id || '');
      setSelectValueIfPresent(voiceSelect, templateVoice || defaultVoiceConnectionId);
    }
    setTemplateStatus(form, '');
    modal.hidden = false;
    window.setTimeout(() => form.querySelector('input[name="title"]')?.focus(), 0);
  }

  function closeCreateTemplateModal() {
    const modal = $('#createTemplateModal');
    if (modal) modal.hidden = true;
  }

  function closeEditTemplateModal() {
    const modal = $('#editTemplateModal');
    if (modal) modal.hidden = true;
  }

  function openImportTemplateModal() {
    const modal = $('#importTemplateModal');
    const form = $('#importTemplateForm');
    if (!modal || !form) return;
    form.reset();
    const archiveName = $('#templateArchiveName');
    if (archiveName) archiveName.textContent = 'Chưa chọn file.';
    setTemplateStatus(form, '');
    modal.hidden = false;
    window.setTimeout(() => form.querySelector('input[name="archive"]')?.focus(), 0);
  }

  function closeImportTemplateModal() {
    const modal = $('#importTemplateModal');
    if (modal) modal.hidden = true;
  }

  function fileToDataUrl(file) {
    return new Promise((resolve, reject) => {
      const reader = new FileReader();
      reader.onload = () => resolve(String(reader.result || ''));
      reader.onerror = () => reject(reader.error || new Error('Không đọc được file ZIP.'));
      reader.readAsDataURL(file);
    });
  }

  async function postTemplateJson(path, payload) {
    const response = await fetch(path, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(payload),
    });
    const data = await response.json().catch(() => ({}));
    if (!response.ok) throw new Error(data.error || `HTTP ${response.status}`);
    return data;
  }

  function templateDomainPayload(formData) {
    return {
      pack: String(formData.get('pack') || '').trim(),
      variant: String(formData.get('variant') || '').trim(),
      aspect_ratio: String(formData.get('aspect_ratio') || '').trim(),
      platforms: String(formData.get('platforms') || '').trim(),
    };
  }

  async function submitImportTemplate(event) {
    event.preventDefault();
    const form = event.currentTarget;
    const submitButton = form.querySelector('button[type="submit"]');
    const formData = new FormData(form);
    const file = formData.get('archive');
    if (!(file instanceof File) || !file.size) {
      setTemplateStatus(form, 'Chọn file ZIP template trước khi import.', 'bad');
      return;
    }
    if (file.size > MAX_TEMPLATE_ARCHIVE_BYTES) {
      setTemplateStatus(form, 'File ZIP quá lớn. Giới hạn import từ UI là 80MB.', 'bad');
      return;
    }
    const payload = {
      filename: file.name,
      title: String(formData.get('title') || '').trim(),
      slug: String(formData.get('slug') || '').trim(),
      description: String(formData.get('description') || '').trim(),
      archive: '',
    };
    if (submitButton) submitButton.disabled = true;
    setTemplateStatus(form, 'Đang đọc file ZIP...', 'warn');
    try {
      payload.archive = await fileToDataUrl(file);
      setTemplateStatus(form, 'Đang import template...', 'warn');
      const data = await postTemplateJson('/api/templates/import', payload);
      const templateName = data.template?.name || data.import?.template || '';
      setTemplateStatus(form, 'Đã import template.', 'good');
      window.location.href = templateName ? `/templates?template=${encodeURIComponent(templateName)}` : '/templates';
    } catch (error) {
      setTemplateStatus(form, error.message || String(error), 'bad');
    } finally {
      if (submitButton) submitButton.disabled = false;
    }
  }

  async function submitCreateTemplate(event) {
    event.preventDefault();
    const form = event.currentTarget;
    const submitButton = form.querySelector('button[type="submit"]');
    const formData = new FormData(form);
    const payload = {
      title: String(formData.get('title') || '').trim(),
      base_template: String(formData.get('base_template') || '').trim(),
      description: String(formData.get('description') || '').trim(),
      tags: String(formData.get('tags') || '').trim(),
      ...templateDomainPayload(formData),
      voice_connection_id: String(formData.get('voice_connection_id') || '').trim(),
      script: String(formData.get('script') || '').trim(),
    };
    if (submitButton) submitButton.disabled = true;
    setTemplateStatus(form, 'Đang tạo template...', 'warn');
    try {
      const data = await postTemplateJson('/api/templates/create', payload);
      const templateName = data.template?.name || '';
      setTemplateStatus(form, 'Đã tạo template.', 'good');
      window.location.href = templateName ? `/templates?template=${encodeURIComponent(templateName)}` : '/templates';
    } catch (error) {
      setTemplateStatus(form, error.message || String(error), 'bad');
    } finally {
      if (submitButton) submitButton.disabled = false;
    }
  }

  async function openEditTemplateModal(templateName = state.template) {
    const modal = $('#editTemplateModal');
    const form = $('#editTemplateForm');
    const name = String(templateName || state.template || '').trim();
    if (!modal || !form || !name) return;
    setTemplateStatus(form, 'Đang tải template...', 'warn');
    modal.hidden = false;
    try {
      const response = await fetch(`/api/templates/edit?template=${encodeURIComponent(name)}`, { cache: 'no-store' });
      const data = await response.json().catch(() => ({}));
      if (!response.ok) throw new Error(data.error || `HTTP ${response.status}`);
      const editable = data.editable || {};
      form.elements.template.value = data.template?.name || name;
      form.elements.template_display.value = data.template?.name || name;
      form.elements.title.value = editable.title || data.template?.title || name;
      form.elements.description.value = editable.description || '';
      form.elements.tags.value = editable.tags || '';
      if (form.elements.pack) form.elements.pack.value = editable.pack || '';
      if (form.elements.variant) form.elements.variant.value = editable.variant || '';
      if (form.elements.aspect_ratio) form.elements.aspect_ratio.value = editable.aspect_ratio || '';
      if (form.elements.platforms) form.elements.platforms.value = editable.platforms || '';
      setSelectValueIfPresent(form.elements.voice_connection_id, editable.voice_connection_id || defaultVoiceConnectionId);
      form.elements.script.value = editable.script || '';
      form.elements.rules.value = editable.rules || '';
      form.elements.preview_settings.value = editable.preview_settings || '';
      setTemplateStatus(form, '');
      window.setTimeout(() => form.elements.title?.focus(), 0);
    } catch (error) {
      setTemplateStatus(form, error.message || String(error), 'bad');
    }
  }

  async function submitEditTemplate(event) {
    event.preventDefault();
    const form = event.currentTarget;
    const submitButton = form.querySelector('button[type="submit"]');
    const formData = new FormData(form);
    const payload = {
      template: String(formData.get('template') || '').trim(),
      title: String(formData.get('title') || '').trim(),
      description: String(formData.get('description') || '').trim(),
      tags: String(formData.get('tags') || '').trim(),
      ...templateDomainPayload(formData),
      voice_connection_id: String(formData.get('voice_connection_id') || '').trim(),
      script: String(formData.get('script') || ''),
      rules: String(formData.get('rules') || ''),
      preview_settings: String(formData.get('preview_settings') || ''),
    };
    if (submitButton) submitButton.disabled = true;
    setTemplateStatus(form, 'Đang lưu template...', 'warn');
    try {
      const data = await postTemplateJson('/api/templates/update', payload);
      const templateName = data.template?.name || payload.template;
      setTemplateStatus(form, 'Đã lưu template.', 'good');
      if (form.dataset.stay === 'editor') {
        const previewFrame = $('#templateEditorPreview');
        if (previewFrame) {
          const url = new URL(previewFrame.src, window.location.origin);
          url.searchParams.set('_v', String(Date.now()));
          previewFrame.src = `${url.pathname}${url.search}`;
        }
        return;
      }
      window.location.href = templateName ? `/templates?template=${encodeURIComponent(templateName)}` : '/templates';
    } catch (error) {
      setTemplateStatus(form, error.message || String(error), 'bad');
    } finally {
      if (submitButton) submitButton.disabled = false;
    }
  }

  async function submitCreateProject(event) {
    event.preventDefault();
    const form = event.currentTarget;
    const submitButton = form.querySelector('button[type="submit"]');
    const formData = new FormData(form);
    const payload = {
      name: String(formData.get('name') || '').trim(),
      template: String(formData.get('template') || '').trim(),
      language: String(formData.get('language') || window.__VIRO_LANG__ || 'vi').trim(),
      input_mode: String(formData.get('input_mode') || 'ai').trim(),
      voice_connection_id: String(formData.get('voice_connection_id') || '').trim(),
      notes: String(formData.get('notes') || '').trim(),
    };
    if (submitButton) submitButton.disabled = true;
    createProjectStatus(payload.language === 'en' ? 'Creating project...' : 'Đang tạo project...', 'warn');
    try {
      const response = await fetch('/api/projects/create', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(payload),
      });
      const data = await response.json().catch(() => ({}));
      if (!response.ok) throw new Error(data.error || `HTTP ${response.status}`);
      createProjectStatus(payload.language === 'en' ? 'Project created.' : 'Đã tạo project.', 'good');
      window.location.href = data.studio_url || `/studio?project=${encodeURIComponent(data.project?.name || '')}`;
    } catch (error) {
      createProjectStatus(error.message || String(error), 'bad');
    } finally {
      if (submitButton) submitButton.disabled = false;
    }
  }

  async function submitSaveProjectTemplate(event) {
    event.preventDefault();
    const form = event.currentTarget;
    const submitButton = form.querySelector('button[type="submit"]');
    const formData = new FormData(form);
    const payload = {
      project: String(formData.get('project') || '').trim(),
      title: String(formData.get('title') || '').trim(),
      description: String(formData.get('description') || '').trim(),
      tags: String(formData.get('tags') || '').trim(),
      ...templateDomainPayload(formData),
    };
    if (submitButton) submitButton.disabled = true;
    setTemplateStatus(form, 'Đang lưu project thành template...', 'warn');
    try {
      const data = await postTemplateJson('/api/templates/from-project', payload);
      const templateName = data.template?.name || '';
      setTemplateStatus(form, 'Đã tạo template mới.', 'good');
      window.location.href = templateName ? `/templates?template=${encodeURIComponent(templateName)}` : '/templates';
    } catch (error) {
      setTemplateStatus(form, error.message || String(error), 'bad');
    } finally {
      if (submitButton) submitButton.disabled = false;
    }
  }

  function reloadWithSourceProjects(data) {
    const nextProjects = Array.isArray(data?.projects) ? data.projects : [];
    const url = new URL(window.location.href);
    const firstProject = nextProjects[0]?.name || '';
    if (firstProject) url.searchParams.set('project', firstProject);
    else url.searchParams.delete('project');
    window.location.href = `${url.pathname}${url.search}`;
  }

  function setSourceSelectStatus(message, tone = 'warn') {
    if ($('#renderStatus')) {
      setStatus(message, tone);
    } else if ($('#uploadStatus')) {
      setUploadStatus(message, tone);
    }
  }

  async function selectSourceRootFromFinder() {
    const buttons = $all('[data-source-root-select]');
    buttons.forEach((button) => { button.disabled = true; });
    setSourceSelectStatus('Đang mở Finder để chọn source folder...', 'warn');
    try {
      const response = await fetch('/api/source-root/select', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({}),
      });
      const data = await response.json();
      if (!response.ok) throw new Error(data.error || `HTTP ${response.status}`);
      const count = Array.isArray(data.projects) ? data.projects.length : 0;
      setSourceSelectStatus(`Đã đổi source. Tìm thấy ${count} project.`, 'good');
      window.setTimeout(() => reloadWithSourceProjects(data), 250);
    } catch (error) {
      setSourceSelectStatus(error.message || String(error), 'bad');
    } finally {
      buttons.forEach((button) => { button.disabled = false; });
    }
  }

  function setRevealOutputButton(projectName = state.project, visible = false) {
    const button = $('#revealOutput');
    if (!button) return;
    button.dataset.project = projectName || '';
    button.hidden = !visible;
    button.disabled = false;
  }

  function setUploadCenterLink(projectName = state.project, visible = false) {
    const link = $('#uploadCenterLink');
    if (!link) return;
    link.href = projectName ? `/upload?project=${encodeURIComponent(projectName)}` : '/upload';
    link.hidden = !visible;
  }

  function setUploadEmpty(projectName = state.project, visible = false) {
    const empty = $('#uploadEmpty');
    if (!empty) return;
    const title = $('#uploadEmptyProject');
    if (title) title.textContent = projectName ? `${projectName} chưa có final_video.mp4` : 'Project này chưa có final_video.mp4';
    empty.hidden = !visible;
  }

  function setProjectVideoBadge(project) {
    const badge = $('#projectVideoBadge');
    if (!badge) return;
    const ready = Boolean(project?.video_url);
    badge.textContent = ready ? 'có final_video' : 'chưa render';
    badge.className = `project-video-badge ${ready ? 'ready' : 'missing'}`;
  }

  function setUploadStatus(message = '', tone = 'warn') {
    const status = $('#uploadStatus');
    if (!status) return;
    status.textContent = message;
    status.hidden = !message;
    status.className = `upload-status ${tone}`;
  }

  function setUploadResult(message = '', tone = 'warn', links = []) {
    const result = $('#uploadResult');
    if (!result) return;
    result.textContent = '';
    result.hidden = !message && !links.length;
    result.className = `upload-result ${tone}`;
    if (message) {
      const text = document.createElement('span');
      text.textContent = message;
      result.appendChild(text);
    }
    links.forEach((link) => {
      result.appendChild(document.createTextNode(' '));
      const anchor = document.createElement('a');
      anchor.href = link.href;
      anchor.target = '_blank';
      anchor.rel = 'noreferrer';
      anchor.textContent = link.label;
      result.appendChild(anchor);
    });
  }

  function flashCopyButton(button) {
    if (!button) return;
    const icon = button.querySelector('.btn-icon');
    if (!icon) return;
    if (!button.dataset.defaultIcon) button.dataset.defaultIcon = icon.textContent || '⧉';
    if (!button.dataset.defaultTitle) button.dataset.defaultTitle = button.getAttribute('title') || 'Copy';
    if (!button.dataset.defaultAriaLabel) button.dataset.defaultAriaLabel = button.getAttribute('aria-label') || button.dataset.defaultTitle;
    icon.textContent = '✓';
    button.setAttribute('title', 'Đã copy');
    button.setAttribute('aria-label', 'Đã copy');
    if (button._copyResetTimer) window.clearTimeout(button._copyResetTimer);
    button._copyResetTimer = window.setTimeout(() => {
      icon.textContent = button.dataset.defaultIcon || '⧉';
      button.setAttribute('title', button.dataset.defaultTitle || 'Copy');
      button.setAttribute('aria-label', button.dataset.defaultAriaLabel || button.dataset.defaultTitle || 'Copy');
    }, 1400);
  }

  function fallbackCopyText(text) {
    const helper = document.createElement('textarea');
    helper.value = text;
    helper.setAttribute('readonly', '');
    helper.style.position = 'fixed';
    helper.style.opacity = '0';
    helper.style.pointerEvents = 'none';
    document.body.appendChild(helper);
    helper.select();
    helper.setSelectionRange(0, helper.value.length);
    const ok = document.execCommand('copy');
    document.body.removeChild(helper);
    if (!ok) throw new Error('Clipboard copy failed.');
  }

  async function copyText(text) {
    if (navigator.clipboard?.writeText) await navigator.clipboard.writeText(text);
    else fallbackCopyText(text);
  }

  async function copyFieldValue(fieldId, label, button) {
    const field = document.getElementById(fieldId);
    const value = String(field?.value || '');
    if (!value.trim()) {
      setUploadStatus(`${label} đang trống.`, 'warn');
      return;
    }
    try {
      await copyText(value);
      flashCopyButton(button);
    } catch (error) {
      setUploadStatus(`Không copy được ${label}.`, 'bad');
    }
  }

  function hideUploadPanel() {
    const panel = $('#uploadPanel');
    if (panel) panel.hidden = true;
    state.uploadProject = null;
    state.facebookCommentTargetId = '';
    setUploadStatus('');
    setUploadResult('');
    updateFacebookCommentButton();
  }

  async function loadUploadMetadata(projectName) {
    const title = $('#uploadTitle');
    const youtubeDescription = $('#youtubeDescription');
    const facebookCaption = $('#facebookCaption');
    const facebookSourceComment = $('#facebookSourceComment');
    const privacy = $('#youtubePrivacy');
    const facebookVideoState = $('#facebookVideoState');
    if (!title || !youtubeDescription || !facebookCaption) return;
    try {
      const response = await fetch(`/api/social/upload-metadata?project=${encodeURIComponent(projectName)}`, { cache: 'no-store' });
      const data = await response.json();
      if (!response.ok) throw new Error(data.error || `HTTP ${response.status}`);
      title.value = data.title || '';
      youtubeDescription.value = data.youtubeDescription || data.description || '';
      facebookCaption.value = data.facebookCaption || '';
      if (facebookSourceComment) facebookSourceComment.value = data.facebookSourceComment || '';
      if (privacy && data.privacyStatus) privacy.value = data.privacyStatus;
      if (facebookVideoState && data.facebookVideoState) facebookVideoState.value = data.facebookVideoState === 'PUBLISHED' ? 'PUBLISHED' : 'DRAFT';
    } catch (error) {
      setUploadStatus(error.message || String(error), 'bad');
    } finally {
      updateUploadBothButton();
      updateFacebookCommentButton();
    }
  }

  function updateFacebookCommentButton() {
    const button = $('#commentFacebookSource');
    if (!button) return;
    const hasComment = Boolean(($('#facebookSourceComment')?.value || '').trim());
    button.disabled = !(state.socialReady.facebook && state.facebookCommentTargetId && hasComment);
  }

  function updateUploadBothButton() {
    const uploadBothPublic = $('#uploadBothPublic');
    if (!uploadBothPublic) return;
    const isPublic = $('#youtubePrivacy')?.value === 'public' && $('#facebookVideoState')?.value === 'PUBLISHED';
    uploadBothPublic.disabled = !(isPublic && state.socialReady.youtube && state.socialReady.facebook);
    setButtonLabel(uploadBothPublic, isPublic ? 'Upload Public YouTube + Reels' : 'Chọn Public rồi Publish');
  }

  function setAccountAvatar(avatar, thumbnail) {
    if (!avatar) return;
    avatar.hidden = !thumbnail;
    avatar.onerror = () => {
      avatar.hidden = true;
      avatar.removeAttribute('src');
    };
    if (thumbnail) avatar.src = thumbnail;
    else avatar.removeAttribute('src');
  }

  function appendAccountContent(button, account, withChevron = false) {
    const accountId = String(account.id || '');
    const avatar = document.createElement('img');
    avatar.alt = '';
    setAccountAvatar(avatar, String(account.thumbnail || ''));

    const body = document.createElement('div');
    const name = document.createElement('strong');
    const id = document.createElement('span');
    name.textContent = String(account.title || account.name || accountId);
    id.textContent = `ID: ${accountId}`;
    body.append(name, id);
    button.append(avatar, body);
    if (withChevron) {
      const chevron = document.createElement('span');
      chevron.className = 'account-chevron';
      chevron.textContent = '⌄';
      button.appendChild(chevron);
    }
  }

  function closePlatformAccountMenus(exceptPrefix = '') {
    $all('.platform-account-list.open').forEach((list) => {
      if (exceptPrefix && list.id === `${exceptPrefix}AccountList`) return;
      list.classList.remove('open');
      const trigger = list.querySelector('.platform-account-trigger');
      if (trigger) trigger.setAttribute('aria-expanded', 'false');
    });
  }

  function renderPlatformAccounts(prefix, accounts = [], activeId = '') {
    const list = $(`#${prefix}AccountList`);
    if (!list) return;
    const normalized = accounts.filter((account) => account?.id);
    list.textContent = '';
    list.classList.remove('open');
    list.hidden = !normalized.length;
    if (!normalized.length) return;

    const activeAccount = normalized.find((account) => account.id === activeId || account.active) || normalized[0];
    const activeAccountId = String(activeAccount.id || '');
    const trigger = document.createElement('button');
    trigger.type = 'button';
    trigger.className = 'platform-account platform-account-trigger active';
    trigger.dataset.accountId = activeAccountId;
    trigger.setAttribute('aria-haspopup', 'listbox');
    trigger.setAttribute('aria-expanded', 'false');
    trigger.setAttribute('title', normalized.length > 1 ? 'Bấm để chọn account khác' : 'Account đang chọn');
    appendAccountContent(trigger, activeAccount, normalized.length > 1);
    trigger.addEventListener('click', () => {
      if (normalized.length <= 1) return;
      const isOpen = list.classList.contains('open');
      closePlatformAccountMenus(prefix);
      list.classList.toggle('open', !isOpen);
      trigger.setAttribute('aria-expanded', !isOpen ? 'true' : 'false');
    });
    list.appendChild(trigger);

    if (normalized.length <= 1) return;

    const menu = document.createElement('div');
    menu.className = 'platform-account-options';
    menu.setAttribute('role', 'listbox');
    normalized.forEach((account) => {
      const accountId = String(account.id || '');
      const active = accountId === activeAccountId;
      const option = document.createElement('button');
      option.type = 'button';
      option.className = `platform-account platform-account-option ${active ? 'active' : ''}`;
      option.dataset.accountId = accountId;
      option.setAttribute('role', 'option');
      option.setAttribute('aria-selected', active ? 'true' : 'false');
      option.setAttribute('title', active ? 'Đang chọn' : 'Bấm để chọn account này');
      appendAccountContent(option, account);
      option.addEventListener('click', () => {
        closePlatformAccountMenus();
        if (!accountId || active) return;
        if (prefix === 'youtube') setYoutubeActiveChannel(accountId);
        else setFacebookActivePage(accountId);
      });
      menu.appendChild(option);
    });
    list.appendChild(menu);
  }

  function setAccountListDisabled(prefix, disabled) {
    $all(`#${prefix}AccountList button`).forEach((button) => {
      button.disabled = disabled;
    });
  }

  function accountListFallback(account = {}) {
    const accountId = String(account?.id || '');
    return accountId ? [account] : [];
  }

  function syncYoutubeChannels(youtube = {}) {
    const fallbackChannel = accountListFallback(youtube.channel);
    const channels = Array.isArray(youtube.channels) && youtube.channels.length ? youtube.channels : fallbackChannel;
    const activeChannelId = String(youtube.active_channel_id || youtube.channel?.id || channels.find((channel) => channel.active)?.id || '');
    renderPlatformAccounts('youtube', channels, activeChannelId);
  }

  function syncFacebookPages(facebook = {}) {
    const pages = Array.isArray(facebook.pages) ? facebook.pages : [];
    const activePageId = String(facebook.active_page_id || facebook.page_id || facebook.page?.id || pages.find((page) => page.active)?.id || '');
    renderPlatformAccounts('facebook', pages.length ? pages : accountListFallback(facebook.page), activePageId);
  }

  function syncFacebookConfigUi(facebook = {}) {
    const pageIdInput = $('#facebookPageId');
    const tokenInput = $('#facebookPageAccessToken');
    const configState = $('#facebookConfigState');
    const activePageId = String(facebook.active_page_id || facebook.page_id || facebook.page?.id || '').trim();
    const modalOpen = Boolean($('#facebookConfigModal') && !$('#facebookConfigModal').hidden);
    state.facebookActivePageId = activePageId;
    if (pageIdInput && document.activeElement !== pageIdInput && activePageId) {
      pageIdInput.value = activePageId;
    }
    if (tokenInput) {
      if (!modalOpen && document.activeElement !== tokenInput) tokenInput.value = '';
      tokenInput.placeholder = facebook.configured
        ? 'Đã lưu token, dán token mới nếu muốn đổi'
        : 'Dán Page access token';
    }
    if (configState) {
      configState.textContent = '';
      configState.hidden = true;
    }
  }

  function openFacebookConfigModal() {
    const modal = $('#facebookConfigModal');
    const pageIdInput = $('#facebookPageId');
    const tokenInput = $('#facebookPageAccessToken');
    if (!modal) return;
    modal.hidden = false;
    if (pageIdInput && !pageIdInput.value.trim() && state.facebookActivePageId) {
      pageIdInput.value = state.facebookActivePageId;
    }
    if (tokenInput) {
      tokenInput.value = '';
      tokenInput.focus();
    } else if (pageIdInput) {
      pageIdInput.focus();
    }
  }

  function closeFacebookConfigModal() {
    const modal = $('#facebookConfigModal');
    const tokenInput = $('#facebookPageAccessToken');
    if (tokenInput) tokenInput.value = '';
    if (modal) modal.hidden = true;
  }

  async function setYoutubeActiveChannel(channelId) {
    if (!channelId) return;
    setAccountListDisabled('youtube', true);
    setUploadStatus('Đang đổi YouTube Channel...', 'warn');
    try {
      const response = await fetch('/api/social/youtube/active-channel', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ channelId }),
      });
      const data = await response.json();
      if (!response.ok) throw new Error(data.error || `HTTP ${response.status}`);
      setUploadStatus('Đã đổi YouTube Channel.', 'good');
      await refreshSocialStatus();
    } catch (error) {
      setUploadStatus(error.message || String(error), 'bad');
    } finally {
      setAccountListDisabled('youtube', false);
    }
  }

  async function setFacebookActivePage(pageId) {
    if (!pageId) return;
    setAccountListDisabled('facebook', true);
    setUploadStatus('Đang đổi Facebook Page...', 'warn');
    try {
      const response = await fetch('/api/social/facebook/active-page', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ pageId }),
      });
      const data = await response.json();
      if (!response.ok) throw new Error(data.error || `HTTP ${response.status}`);
      setUploadStatus('Đã đổi Facebook Page.', 'good');
      await refreshSocialStatus();
    } catch (error) {
      setUploadStatus(error.message || String(error), 'bad');
    } finally {
      setAccountListDisabled('facebook', false);
    }
  }

  async function refreshSocialStatus() {
    const connectYoutube = $('#connectYoutube');
    const uploadYoutube = $('#uploadYoutube');
    const uploadFacebook = $('#uploadFacebook');
    const commentFacebookSource = $('#commentFacebookSource');
    const facebookVideoState = $('#facebookVideoState');
    if (!connectYoutube || !uploadYoutube) return;
    try {
      const response = await fetch('/api/social/status', { cache: 'no-store' });
      const data = await response.json();
      if (!response.ok) throw new Error(data.error || `HTTP ${response.status}`);
      const youtube = data.platforms?.youtube || {};
      const facebook = data.platforms?.facebook || {};
      const canYoutube = Boolean(youtube.configured && youtube.connected);
      const canFacebook = Boolean(facebook.available);
      state.socialReady = { youtube: canYoutube, facebook: canFacebook };
      syncYoutubeChannels(youtube);
      syncFacebookPages(facebook);
      syncFacebookConfigUi(facebook);
      setButtonLabel(connectYoutube, 'Thêm kênh');
      uploadYoutube.disabled = !canYoutube;
      if (uploadFacebook) {
        uploadFacebook.disabled = !canFacebook;
        setButtonLabel(uploadFacebook, canFacebook ? 'Upload Facebook Reels' : 'Cấu hình Facebook');
      }
      updateFacebookCommentButton();
      if (facebookVideoState && facebook.video_state) facebookVideoState.value = facebook.video_state === 'PUBLISHED' ? 'PUBLISHED' : 'DRAFT';
      const ready = [];
      if (canYoutube) ready.push('YouTube');
      if (canFacebook) ready.push('Facebook Reels');
      if (ready.length) {
        setUploadStatus(`${ready.join(', ')} đã sẵn sàng.`, 'good');
      } else {
        const missingYoutube = !youtube.configured;
        const missingFacebook = !facebook.configured;
        if (missingYoutube && missingFacebook) {
          setUploadStatus('Chưa cấu hình Social API. Bấm Hướng dẫn YouTube hoặc Hướng dẫn Facebook để xem cách cấu hình.', 'warn');
        } else {
          const nextSteps = [];
          if (youtube.configured && !youtube.connected) nextSteps.push('YouTube đã có config. Bấm Thêm channel để kết nối kênh.');
          if (missingYoutube) nextSteps.push(youtube.message || 'YouTube chưa cấu hình. Bấm Hướng dẫn YouTube để xem cách cấu hình.');
          if (missingFacebook) nextSteps.push(facebook.message || 'Facebook chưa cấu hình. Bấm Hướng dẫn Facebook để xem cách cấu hình.');
          setUploadStatus(nextSteps[0] || 'Chưa cấu hình Social API. Bấm hướng dẫn để xem cách cấu hình.', 'warn');
        }
      }
      updateUploadBothButton();
    } catch (error) {
      state.socialReady = { youtube: false, facebook: false };
      syncFacebookConfigUi({ configured: false });
      uploadYoutube.disabled = true;
      if (uploadFacebook) uploadFacebook.disabled = true;
      if (commentFacebookSource) commentFacebookSource.disabled = true;
      updateUploadBothButton();
      updateFacebookCommentButton();
      setUploadStatus(error.message || String(error), 'bad');
    }
  }

  async function showUploadPanel(projectName, videoUrl) {
    const panel = $('#uploadPanel');
    if (!panel) return;
    state.uploadProject = projectName;
    state.outputUrl = videoUrl || state.outputUrl;
    state.facebookCommentTargetId = '';
    panel.hidden = false;
    setUploadEmpty(projectName, false);
    setUploadResult('');
    await loadUploadMetadata(projectName);
    await refreshSocialStatus();
  }

  function setRenderState(title = '', steps = [], tone = 'running') {
    const panel = $('#renderState');
    const titleEl = $('#stateTitle');
    const list = $('#stateList');
    if (!panel || !titleEl || !list) return;
    panel.hidden = !title;
    panel.className = `render-state ${tone}`;
    titleEl.textContent = title;
    list.textContent = '';
    const lines = Array.isArray(steps) ? steps : [steps];
    lines.filter(Boolean).slice(-1).forEach((step) => {
      const item = document.createElement('li');
      item.textContent = step;
      list.appendChild(item);
    });
  }

  function formatElapsed(seconds) {
    const totalSeconds = Math.max(0, Math.floor(seconds || 0));
    const hours = Math.floor(totalSeconds / 3600);
    const minutes = Math.floor((totalSeconds % 3600) / 60);
    const remainingSeconds = totalSeconds % 60;
    if (hours) return `${hours}h${minutes}m${remainingSeconds}s`;
    if (minutes) return `${minutes}m${remainingSeconds}s`;
    return `${remainingSeconds}s`;
  }

  function elapsedForJob(job) {
    const startedAt = Number(job.started_at || job.created_at || 0);
    if (!startedAt) return '0s';
    const finishedAt = Number(job.finished_at || 0);
    const endedAt = ['done', 'failed', 'cancelled'].includes(job.status) && finishedAt ? finishedAt : Date.now() / 1000;
    return formatElapsed(endedAt - startedAt);
  }

  function renderTitle(label, job) {
    return `${label} (đã chạy ${elapsedForJob(job)})`;
  }

  function latestRawLogLine(logs = '') {
    const lines = String(logs).split(/\r?\n/).map((line) => line.trim()).filter(Boolean);
    return lines.length ? lines[lines.length - 1] : '';
  }

  function renderProgressForJob(job) {
    const logs = job.logs || '';
    const rawLine = latestRawLogLine(logs);
    let progress = {
      title: 'Bắt đầu render...',
      log: rawLine,
    };

    [
      [/Speeding up audio|Saved sped-up audio/, 'Đang xử lý audio...'],
      [/Generating Edge TTS|Generating per-slide TTS|Slide \d+ TTS/, 'Đang tạo audio...'],
      [/Whisper transcription|Transcribing audio|Aligning transcript|Splitting audio/, 'Đang tách voiceover...'],
      [/Concatenating|voiceover_concat|Timing saved/, 'Đang ghép timing...'],
      [/Mapping check|Click timeline/, 'Đang kiểm tra slide...'],
      [/Recording slides|Recording\s+\d|Raw video/, 'Đang record slide...'],
      [/Merging voiceover|Final video|Video rendered successfully|All done|Done:/, 'Sắp hoàn thành...'],
    ].forEach(([pattern, title]) => {
      if (pattern.test(logs)) progress = { title, log: rawLine };
    });

    if (job.status === 'done') return { title: 'Hoàn tất render', log: rawLine };
    if (job.status === 'failed') return { title: 'Render thất bại', log: job.error || rawLine };
    if (job.status === 'cancelled') return { title: 'Đã dừng render', log: rawLine || 'Render đã được dừng.' };
    if (job.status === 'cancelling') return { title: 'Đang dừng render...', log: rawLine || 'Đang gửi tín hiệu dừng job render.' };
    return progress;
  }

  function setEngine(engine) {
    const previousEngine = state.engine;
    state.engine = engine;
    $all('[data-engine]').forEach((button) => {
      button.classList.toggle('active', button.dataset.engine === engine);
    });
    $all('[data-pane]').forEach((pane) => {
      pane.hidden = pane.dataset.pane !== engine;
    });
    syncAdvancedSettings();
    if (previousEngine && previousEngine !== engine) {
      markVoicePreviewStale('Đã đổi nguồn giọng đọc. Bấm Preview voice để cập nhật audio.');
    }
  }

  function syncAdvancedSettings() {
    $all('[data-advanced-engine]').forEach((pane) => {
      pane.hidden = pane.dataset.advancedEngine !== state.engine;
    });
    const details = $('#advancedSettings');
    const label = $('#advancedStateLabel');
    if (details && label) label.textContent = details.open ? 'Đang mở' : 'Đang ẩn';
  }

  function syncSpeedPresets() {
    const speedInput = $('#renderSpeed');
    const value = speedInput ? Number(speedInput.value || 0) : 0;
    $all('.speed-preset[data-speed]').forEach((button) => {
      button.classList.toggle('active', Number(button.dataset.speed) === value);
    });
  }

  function currentElevenMode() {
    return document.querySelector('input[name="elevenMode"]:checked')?.value || 'tts';
  }

  function currentVoicePayload({ includeForce = false } = {}) {
    const payload = {
      project: currentProject()?.name || state.project,
      engine: state.engine,
      input_mode: studioInputMode(),
      videoDuration: state.videoDuration,
      speed: Number($('#renderSpeed')?.value || 1.1),
      size: $('#renderSize')?.value || '1080x1920',
      platform: $('#targetPlatform')?.value || 'shorts',
    };
    if (state.engine === 'elevenlabs') {
      payload.mode = currentElevenMode();
      payload.voice = $('#elevenVoice')?.value.trim() || '';
      payload.connection_id = currentVoiceConnectionId();
      if (includeForce) payload.force = Boolean($('#elevenForce')?.checked);
    } else {
      payload.voice = $('#edgeVoice')?.value.trim() || 'vi-VN-HoaiMyNeural';
      payload.edgePerSlide = Boolean($('#edgePerSlide')?.checked);
      if (includeForce) payload.force = Boolean($('#edgeForce')?.checked);
    }
    return payload;
  }

  function setVoicePreviewUi(preview = null, { stale = false, message = '' } = {}) {
    const panel = $('#voicePreviewPanel');
    const audio = $('#voicePreviewAudio');
    const label = $('#voicePreviewState');
    const hasExplicitCurrent = typeof preview?.current === 'boolean';
    const current = hasExplicitCurrent
      ? Boolean(preview.current && !preview.stale)
      : Boolean(preview?.audio_url && !stale);
    state.voicePreview = preview || null;
    state.voicePreviewCurrent = current && !stale;
    if (panel) panel.hidden = !preview?.audio_url && !message;
    if (audio && preview?.audio_url) {
      audio.src = `${preview.audio_url}${preview.audio_url.includes('?') ? '&' : '?'}_v=${Date.now()}`;
    }
    if (label) {
      if (message) label.textContent = message;
      else if (state.voicePreviewCurrent) label.textContent = `Voice preview sẵn sàng${preview?.duration ? ` · ${Number(preview.duration).toFixed(1)}s` : ''}.`;
      else if (preview?.audio_url) label.textContent = 'Voice preview đã cũ. Bấm Preview voice để tạo lại.';
      else label.textContent = 'Chưa có voice preview.';
      label.className = `config-state ${state.voicePreviewCurrent ? 'good' : 'warn'}`;
    }
  }

  function markVoicePreviewStale(message = 'Voice preview đã cũ. Bấm Preview voice để tạo lại.') {
    if (state.voicePreview) state.voicePreview.current = false;
    state.voicePreviewCurrent = false;
    setVoicePreviewUi(state.voicePreview, { stale: true, message });
  }

  function syncElevenMode() {
    const mode = currentElevenMode();
    $all('[data-eleven-mode-pane]').forEach((pane) => {
      pane.hidden = pane.dataset.elevenModePane !== mode;
    });
  }

  function syncElevenLabsApiKeyUi(configured, proxyConfigured = false, config = {}) {
    const panel = $('#elevenApiKeyPanel');
    const apiState = $('#elevenApiKeyState');
    const credentialSelect = $('#elevenCredentialSelect');
    const manualField = $('#elevenManualKeyField') || $('#elevenApiKey')?.closest?.('.field');
    if (panel) panel.hidden = false;
    if (manualField && credentialSelect) manualField.hidden = Boolean(credentialSelect.value);
    if (apiState) {
      const selectedConnection = voiceConnectionMap.get(String(credentialSelect?.value || ''));
      if (selectedConnection) {
        syncVoiceCredentialStatus(config);
        return;
      }
      if (config.builtin_rotator_configured) {
        apiState.textContent = `Đang dùng Viro Key Rotate (${config.registry_key_count || 0} key)`;
        return;
      }
      apiState.textContent = proxyConfigured
        ? 'Đang dùng APIKeyRotator proxy'
        : configured
        ? 'Đã lưu API key trong config/tts.json'
        : 'Chưa lưu API key';
    }
  }

  async function loadElevenLabsConfig() {
    const input = $('#elevenVoice');
    if (!input) return;
    try {
      const response = await fetch('/api/tts/elevenlabs/config', { cache: 'no-store' });
      const data = await response.json();
      if (!response.ok) throw new Error(data.error || `HTTP ${response.status}`);
      input.value = data.voice_id || '';
      input.placeholder = data.voice_id || 'JBFqnCBsd6RMkjVDRZzb';
      const proxyBaseInput = $('#elevenProxyBaseUrl');
      if (proxyBaseInput) proxyBaseInput.value = data.proxy_base_url || '';
      const proxyState = $('#elevenProxyKeyState');
      if (proxyState) {
        proxyState.textContent = data.proxy_key_configured
          ? 'Da luu X-Proxy-Key trong config/tts.json'
          : 'Chưa lưu X-Proxy-Key';
      }
      syncElevenLabsApiKeyUi(Boolean(data.api_key_configured), Boolean(data.proxy_key_configured && data.proxy_base_url), data);
      syncVoiceCredentialForProject();
    } catch (error) {
      setStatus(error.message || String(error), 'bad');
    }
  }

  async function saveElevenLabsVoice() {
    const input = $('#elevenVoice');
    const voiceId = input?.value.trim() || '';
    if (!voiceId) {
      return;
    }
    if (elevenVoiceSaving) return;
    elevenVoiceSaving = true;
    try {
      const response = await fetch('/api/tts/elevenlabs/config', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ voice_id: voiceId }),
      });
      const data = await response.json();
      if (!response.ok) throw new Error(data.error || `HTTP ${response.status}`);
      if (input) input.value = data.voice_id || voiceId;
      setStatus('Đã lưu ElevenLabs voice ID vào config/tts.json.', 'good');
    } catch (error) {
      setStatus(error.message || String(error), 'bad');
    } finally {
      elevenVoiceSaving = false;
    }
  }

  function scheduleElevenLabsVoiceSave(delay = 450) {
    const input = $('#elevenVoice');
    if (!input || !input.value.trim()) return;
    if (elevenVoiceSaveTimer) window.clearTimeout(elevenVoiceSaveTimer);
    elevenVoiceSaveTimer = window.setTimeout(() => {
      elevenVoiceSaveTimer = null;
      saveElevenLabsVoice();
    }, delay);
  }

  async function saveElevenLabsApiKey() {
    const input = $('#elevenApiKey');
    const apiKey = input?.value.trim() || '';
    if (!apiKey) {
      return;
    }
    if (elevenApiKeySaving) return;
    elevenApiKeySaving = true;
    const apiState = $('#elevenApiKeyState');
    if (apiState) apiState.textContent = 'Đang lưu API key...';
    try {
      const response = await fetch('/api/tts/elevenlabs/config', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ api_key: apiKey }),
      });
      const data = await response.json();
      if (!response.ok) throw new Error(data.error || `HTTP ${response.status}`);
      if (input) input.value = '';
      syncElevenLabsApiKeyUi(Boolean(data.api_key_configured), Boolean(data.proxy_key_configured && data.proxy_base_url), data);
      setStatus('Đã lưu ElevenLabs API key vào config/tts.json.', 'good');
    } catch (error) {
      setStatus(error.message || String(error), 'bad');
      if (apiState) apiState.textContent = 'Lưu API key thất bại';
    } finally {
      elevenApiKeySaving = false;
    }
  }

  function scheduleElevenLabsApiKeySave(delay = 450) {
    const input = $('#elevenApiKey');
    if (!input || !input.value.trim()) return;
    if (elevenApiKeySaveTimer) window.clearTimeout(elevenApiKeySaveTimer);
    elevenApiKeySaveTimer = window.setTimeout(() => {
      elevenApiKeySaveTimer = null;
      saveElevenLabsApiKey();
    }, delay);
  }

  async function saveElevenLabsProxy() {
    const baseInput = $('#elevenProxyBaseUrl');
    const keyInput = $('#elevenProxyKey');
    const proxyBaseUrl = baseInput?.value.trim() || '';
    const proxyKey = keyInput?.value.trim() || '';
    if (!proxyBaseUrl && !proxyKey) return;
    if (elevenProxySaving) return;
    elevenProxySaving = true;
    const proxyState = $('#elevenProxyKeyState');
    if (proxyState) proxyState.textContent = 'Đang lưu APIKeyRotator proxy...';
    try {
      const payload = {};
      if (baseInput) payload.proxy_base_url = proxyBaseUrl;
      if (proxyKey) payload.proxy_key = proxyKey;
      const response = await fetch('/api/tts/elevenlabs/config', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(payload),
      });
      const data = await response.json();
      if (!response.ok) throw new Error(data.error || `HTTP ${response.status}`);
      if (keyInput) keyInput.value = '';
      if (baseInput) baseInput.value = data.proxy_base_url || proxyBaseUrl;
      if (proxyState) {
        proxyState.textContent = data.proxy_key_configured
          ? 'Đã lưu APIKeyRotator proxy'
          : 'Chưa lưu X-Proxy-Key';
      }
      syncElevenLabsApiKeyUi(Boolean(data.api_key_configured), Boolean(data.proxy_key_configured && data.proxy_base_url), data);
      setStatus('Đã lưu APIKeyRotator proxy cho ElevenLabs.', 'good');
    } catch (error) {
      setStatus(error.message || String(error), 'bad');
      if (proxyState) proxyState.textContent = 'Luu proxy that bai';
    } finally {
      elevenProxySaving = false;
    }
  }

  function scheduleElevenLabsProxySave(delay = 450) {
    if (elevenProxySaveTimer) window.clearTimeout(elevenProxySaveTimer);
    elevenProxySaveTimer = window.setTimeout(() => {
      elevenProxySaveTimer = null;
      saveElevenLabsProxy();
    }, delay);
  }

  async function copyProjectScript(projectName, button) {
    const project = String(projectName || '').trim();
    if (!project) {
      setStatus('Không tìm thấy project để copy script.', 'bad');
      return;
    }
    if (button) button.disabled = true;
    try {
      const response = await fetch(`/api/slide-script?project=${encodeURIComponent(project)}`, { cache: 'no-store' });
      const data = await response.json();
      if (!response.ok) throw new Error(data.error || `HTTP ${response.status}`);
      const text = Array.isArray(data.lines) ? data.lines.join('\n') : '';
      if (!text.trim()) throw new Error(`${project} chưa có script-90s.txt hoặc script đang trống.`);
      await copyText(text);
      flashCopyButton(button);
      setStatus(`Đã copy script của ${project}.`, 'good');
    } catch (error) {
      setStatus(error.message || String(error), 'bad');
    } finally {
      if (button) button.disabled = false;
    }
  }

  function splitStoryboardScenes(rawText) {
    const text = String(rawText || '').replace(/\r/g, '\n').trim();
    if (!text) return [];
    const lineScenes = text.split('\n').map((line) => line.trim()).filter(Boolean);
    if (lineScenes.length > 1) return lineScenes.slice(0, 80);
    return text
      .split(/[.!?]+/)
      .map((chunk) => chunk.trim())
      .filter(Boolean)
      .slice(0, 80);
  }

  function setStoryboardStatus(message) {
    const status = $('#storyboardStatus');
    if (status) status.textContent = message || '';
  }

  function setStoryboardTab(tab = 'scenes') {
    const normalized = tab === 'media' ? 'media' : 'scenes';
    $all('[data-storyboard-tab]').forEach((button) => {
      button.classList.toggle('active', button.dataset.storyboardTab === normalized);
    });
    $all('[data-storyboard-panel]').forEach((panel) => {
      panel.hidden = panel.dataset.storyboardPanel !== normalized;
    });
  }

  function renderStoryboardScenes(lines = []) {
    const list = $('#storyboardSceneList');
    const count = $('#storyboardSceneCount');
    if (count) count.textContent = String(lines.length);
    if (!list) return;
    list.textContent = '';
    if (!lines.length) {
      const empty = document.createElement('li');
      empty.className = 'empty';
      empty.textContent = 'Chưa có scene line.';
      list.appendChild(empty);
      return;
    }
    lines.forEach((line, index) => {
      const item = document.createElement('li');
      item.className = 'storyboard-scene';
      const badge = document.createElement('span');
      badge.className = 'storyboard-scene-index';
      badge.textContent = String(index + 1);
      const body = document.createElement('div');
      const title = document.createElement('strong');
      title.textContent = `Scene ${index + 1}`;
      const copy = document.createElement('p');
      copy.textContent = line;
      body.append(title, copy);
      item.append(badge, body);
      list.appendChild(item);
    });
  }

  function mediaAssetsForProject(project) {
    if (!project) return [];
    const assets = [
      { label: 'Preview slide', hint: 'Mở preview slide nguồn.', href: project.url },
    ];
    if (project.video_url) {
      assets.push({ label: 'Final MP4', hint: 'Video đã render, sẵn sàng publish.', href: project.video_url });
    }
    if (project.has_output) {
      assets.push({ label: 'Thư mục output', hint: 'Mở output render local của project.', action: 'reveal' });
    }
    return assets;
  }

  function renderStoryboardMedia() {
    const grid = $('#storyboardMediaGrid');
    if (!grid) return;
    const project = currentProject();
    const assets = mediaAssetsForProject(project);
    grid.textContent = '';
    if (!assets.length) {
      const empty = document.createElement('div');
      empty.className = 'empty';
      empty.textContent = 'Chưa tìm thấy media của project.';
      grid.appendChild(empty);
      return;
    }
    assets.forEach((asset) => {
      const card = document.createElement('div');
      card.className = 'storyboard-media-card';
      const title = document.createElement('strong');
      title.textContent = asset.label;
      const hint = document.createElement('span');
      hint.textContent = asset.hint || '';
      const action = document.createElement(asset.href ? 'a' : 'button');
      action.className = 'secondary-btn';
      action.textContent = asset.href ? 'Mở' : 'Mở output';
      if (asset.href) {
        action.href = asset.href;
        action.target = '_blank';
        action.rel = 'noreferrer';
      } else {
        action.type = 'button';
        action.addEventListener('click', () => revealOutput(project?.name));
      }
      card.append(title, hint, action);
      grid.appendChild(card);
    });
  }

  async function fetchProjectScriptLines(projectName = state.project) {
    const project = String(projectName || '').trim();
    if (!project) throw new Error('Hãy chọn project trước.');
    const response = await fetch(`/api/slide-script?project=${encodeURIComponent(project)}`, { cache: 'no-store' });
    const data = await response.json();
    if (!response.ok) throw new Error(data.error || `HTTP ${response.status}`);
    return Array.isArray(data.lines) ? data.lines.map((line) => String(line || '').trim()).filter(Boolean) : [];
  }

  async function loadProjectScriptIntoEditor() {
    const button = $('#storyboardLoadScript');
    if (button) button.disabled = true;
    try {
      const lines = await fetchProjectScriptLines();
      if (!lines.length) throw new Error('Script của project đang chọn đang trống.');
      const editor = $('#studioInput');
      if (editor) editor.value = lines.join('\n');
      renderStoryboardScenes(lines);
      setStoryboardTab('scenes');
      setStoryboardStatus(`Đã load ${lines.length} scene từ ${state.project}.`);
      setStatus(`Đã load script từ ${state.project}.`, 'good');
    } catch (error) {
      setStoryboardStatus(error.message || String(error));
      setStatus(error.message || String(error), 'bad');
    } finally {
      if (button) button.disabled = false;
    }
  }

  async function openStoryboardModal(tab = 'scenes') {
    const modal = $('#storyboardModal');
    if (!modal) return;
    modal.hidden = false;
    setStoryboardTab(tab);
    if (tab === 'media') {
      renderStoryboardMedia();
      setStoryboardStatus('Media asset của project được liệt kê bên dưới.');
      return;
    }

    const editor = $('#studioInput');
    let lines = splitStoryboardScenes(editor?.value || '');
    if (!lines.length) {
      try {
        lines = await fetchProjectScriptLines();
        if (editor && lines.length) editor.value = lines.join('\n');
      } catch (error) {
        setStoryboardStatus(error.message || String(error));
      }
    }
    renderStoryboardScenes(lines);
    setStoryboardStatus(lines.length ? `Storyboard có ${lines.length} scene. ${videoDurationStoryboardHint()}` : `Nhập script hoặc load script của project đang chọn. ${videoDurationStoryboardHint()}`);
  }

  async function generateStoryboard() {
    const button = $('#createStoryboard');
    const project = currentProject();
    const prompt = String($('#studioInput')?.value || '').trim();
    const aiConnectionId = String($('#aiCredentialSelect')?.value || '').trim();
    if (!project) {
      setStatus('Hãy chọn hoặc tạo project trước khi tạo storyboard.', 'bad');
      return;
    }
    if (!prompt) {
      setStatus('Nhập prompt trước khi tạo storyboard.', 'bad');
      return;
    }
    if (studioInputMode() !== 'ai') {
      await openStoryboardModal('scenes');
      return;
    }
    if (!aiConnectionId) {
      const message = 'Chưa chọn OpenAI key nên chưa thể tạo storyboard bằng AI. Vào Tài khoản & Key để thêm OpenAI/API key.';
      const aiState = $('#aiStoryboardState');
      if (aiState) {
        aiState.textContent = message;
        aiState.className = 'config-state bad';
      }
      setStatus(message, 'bad');
      setSlideComposerStatus(message, 'bad');
      return;
    }
    if (button) button.disabled = true;
    setStatus('Đang gọi OpenAI để tạo storyboard...', 'warn');
    setSlideComposerStatus('Đang gọi OpenAI và fill slide...', 'warn');
    try {
      const response = await fetch('/api/storyboard/generate', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          project: project.name,
          template: state.template,
          prompt,
          language: document.documentElement.lang || 'vi',
          videoDuration: state.videoDuration,
          platform: $('#targetPlatform')?.value || 'shorts',
          ai_connection_id: aiConnectionId,
          ai_model: $('#aiStoryboardModel')?.value.trim() || 'gpt-4.1-mini',
        }),
      });
      const data = await response.json().catch(() => ({}));
      if (!response.ok) throw new Error(data.error || `HTTP ${response.status}`);
      slideComposerState.project = data.project || project.name;
      slideComposerState.slides = Array.isArray(data.slides) ? data.slides : [];
      slideComposerState.mediaFiles = Array.isArray(data.media_files) ? data.media_files : [];
      slideComposerState.selected = 0;
      const generatedLines = slideComposerState.slides.map((slide) => slide.text).filter(Boolean);
      const editor = $('#studioInput');
      if (editor && generatedLines.length) editor.value = generatedLines.join('\n');
      renderSlideComposerList();
      fillSlideEditor();
      refreshSlideComposerPreview();
      renderStoryboardScenes(generatedLines);
      await openStoryboardModal('scenes');
      markVoicePreviewStale('Storyboard mới đã tạo. Bấm Preview voice để nghe thử.');
      const storyboard = data.storyboard || {};
      const aiState = $('#aiStoryboardState');
      const aiSource = storyboard.source === 'openai_responses'
        ? `OpenAI ${storyboard.model ? `· ${storyboard.model}` : ''}`
        : 'Local fallback';
      if (aiState) {
        aiState.textContent = storyboard.source === 'openai_responses'
          ? `Đã tạo storyboard bằng ${aiSource}.`
          : `Đã tạo storyboard bằng Local fallback. ${storyboard.fallback_reason || 'Chưa chọn OpenAI key.'}`;
        aiState.className = `config-state ${storyboard.source === 'openai_responses' ? 'good' : 'warn'}`;
      }
      setSlideComposerStatus(`Đã fill ${slideComposerState.slides.length} slide từ storyboard (${aiSource}).`, 'good');
      setStatus(`Đã tạo storyboard và fill vào Slide editor (${aiSource}).`, 'good');
    } catch (error) {
      setStatus(error.message || String(error), 'bad');
      setSlideComposerStatus(error.message || String(error), 'bad');
    } finally {
      if (button) button.disabled = false;
    }
  }

  function closeStoryboardModal() {
    const modal = $('#storyboardModal');
    if (modal) modal.hidden = true;
  }

  async function saveStudioScriptToProject() {
    const button = $('#storyboardSaveScript');
    const project = state.project;
    const lines = splitStoryboardScenes($('#studioInput')?.value || '');
    if (!project) {
      setStoryboardStatus('Hãy chọn project trước.');
      return;
    }
    if (!lines.length) {
      setStoryboardStatus('Không có dòng script để lưu.');
      return;
    }
    if (button) button.disabled = true;
    try {
      const response = await fetch('/api/slide-script', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ project, lines, allowCountChange: true }),
      });
      const data = await response.json();
      if (!response.ok) throw new Error(data.error || `HTTP ${response.status}`);
      const savedLines = Array.isArray(data.lines) ? data.lines : lines;
      const current = projectMap.get(project);
      if (current) current.script_count = savedLines.length;
      renderStoryboardScenes(savedLines);
      setStoryboardStatus(`Đã lưu ${savedLines.length} scene vào ${project}.`);
      markVoicePreviewStale('Script đã thay đổi. Bấm Preview voice để cập nhật audio.');
      setStatus(`Đã lưu script vào ${project}.`, 'good');
    } catch (error) {
      setStoryboardStatus(error.message || String(error));
      setStatus(error.message || String(error), 'bad');
    } finally {
      if (button) button.disabled = false;
    }
  }

  function continueStoryboardToRender() {
    closeStoryboardModal();
    setStatus('Storyboard đã sẵn sàng. Kiểm tra voice settings rồi render audio và video.', 'good');
    $('#startRender')?.scrollIntoView({ behavior: 'smooth', block: 'center' });
  }

  function syncStudioMediaButton(project = currentProject()) {
    const button = $('#studioMediaButton');
    if (!button) return;
    const attachedCount = slideComposerState.slides.filter((slide) => slide?.media).length;
    const fileCount = slideComposerState.mediaFiles.length;
    const count = attachedCount || fileCount;
    const label = button.querySelector('span:last-child');
    const text = count ? `Gắn media (${count})` : 'Gắn media';
    if (label) label.textContent = text;
    else button.textContent = text;
  }

  function setSlideComposerStatus(message = '', tone = 'warn') {
    const status = $('#slideComposerStatus');
    if (!status) return;
    status.hidden = !message;
    status.textContent = message;
    status.className = `status ${tone} slide-composer-status`;
  }

  function currentComposerSlide() {
    return slideComposerState.slides[slideComposerState.selected] || null;
  }

  function slideMediaLabel(media) {
    if (!media) return 'Chưa gắn media.';
    return media.name || media.path || media.url || 'Media đã chọn';
  }

  function commitSlideEditorFields() {
    const slide = currentComposerSlide();
    if (!slide) return;
    slide.text = String($('#studioSlideText')?.value || '').trim();
    slide.screen_text = String($('#studioSlideScreenText')?.value || '').trim();
    slide.visual_direction = String($('#studioSlideVisualDirection')?.value || '').trim();
    slide.duration = String($('#studioSlideDuration')?.value || '').trim();
    slide.transition = String($('#studioSlideTransition')?.value || slide.transition || 'minimal').trim();
    slide.reveal = String($('#studioSlideReveal')?.value || slide.reveal || 'ping').trim();
    if (slide.media) {
      slide.media.fit = String($('#studioSlideMediaFit')?.value || slide.media.fit || 'cover').trim();
      slide.media.position = String($('#studioSlideMediaPosition')?.value || slide.media.position || 'center').trim();
    }
  }

  function goToComposerPreviewSlide(index = slideComposerState.selected) {
    const frame = $('#studioSlidePreview');
    if (!frame?.contentWindow) return;
    try {
      frame.contentWindow.goToSlide?.(index);
    } catch {}
  }

  function refreshSlideComposerPreview() {
    const frame = $('#studioSlidePreview');
    if (!frame || !slideComposerState.project) return;
    frame.src = `/slide/${encodeURIComponent(slideComposerState.project)}/?studioPreview=1&_v=${Date.now()}`;
    frame.addEventListener('load', () => goToComposerPreviewSlide(slideComposerState.selected), { once: true });
  }

  function fillSlideEditor() {
    const slide = currentComposerSlide();
    const text = $('#studioSlideText');
    const screenText = $('#studioSlideScreenText');
    const visualDirection = $('#studioSlideVisualDirection');
    const duration = $('#studioSlideDuration');
    const transition = $('#studioSlideTransition');
    const reveal = $('#studioSlideReveal');
    const fit = $('#studioSlideMediaFit');
    const position = $('#studioSlideMediaPosition');
    const mediaCurrent = $('#studioSlideMediaCurrent');
    if (text) text.value = slide?.text || '';
    if (screenText) screenText.value = slide?.screen_text || '';
    if (visualDirection) visualDirection.value = slide?.visual_direction || slide?.media_direction || '';
    if (duration) duration.value = slide?.duration || '';
    if (transition) transition.value = slide?.transition || 'minimal';
    if (reveal) reveal.value = slide?.reveal || 'ping';
    if (fit) fit.value = slide?.media?.fit || 'cover';
    if (position) position.value = slide?.media?.position || 'center';
    if (mediaCurrent) mediaCurrent.textContent = slideMediaLabel(slide?.media || null);
  }

  function renderSlideComposerList() {
    const list = $('#studioSlideList');
    if (!list) return;
    list.textContent = '';
    if (!slideComposerState.slides.length) {
      const empty = document.createElement('li');
      empty.className = 'empty';
      empty.textContent = 'Chưa có slide để sửa.';
      list.appendChild(empty);
      return;
    }
    slideComposerState.slides.forEach((slide, index) => {
      const item = document.createElement('li');
      const button = document.createElement('button');
      button.className = `secondary-btn${index === slideComposerState.selected ? ' active' : ''}`;
      button.type = 'button';
      button.dataset.slideIndex = String(index);
      const summary = String(slide.text || '').slice(0, 44);
      button.textContent = `Slide ${index + 1}${summary ? ` - ${summary}` : ''}`;
      button.addEventListener('click', () => {
        commitSlideEditorFields();
        slideComposerState.selected = index;
        renderSlideComposerList();
        fillSlideEditor();
        goToComposerPreviewSlide(index);
      });
      item.appendChild(button);
      list.appendChild(item);
    });
  }

  async function loadSlideComposer(projectName = state.project) {
    const composer = $('#slideComposer');
    if (!composer) return;
    const project = String(projectName || state.project || '').trim();
    if (!project) {
      setSlideComposerStatus('Chọn project trước khi sửa slide.', 'bad');
      return;
    }
    setSlideComposerStatus('Đang load slide...', 'warn');
    try {
      const response = await fetch(`/api/slide-composer?project=${encodeURIComponent(project)}`, { cache: 'no-store' });
      const data = await response.json().catch(() => ({}));
      if (!response.ok) throw new Error(data.error || `HTTP ${response.status}`);
      slideComposerState.project = data.project || project;
      slideComposerState.slides = Array.isArray(data.slides) ? data.slides : [];
      slideComposerState.mediaFiles = Array.isArray(data.media_files) ? data.media_files : [];
      setVoicePreviewUi(data.voice_preview || data.studio_state?.voice_preview || null);
      slideComposerState.selected = Math.min(slideComposerState.selected, Math.max(0, slideComposerState.slides.length - 1));
      composer.dataset.project = slideComposerState.project;
      renderSlideComposerList();
      fillSlideEditor();
      syncStudioMediaButton();
      refreshSlideComposerPreview();
      setSlideComposerStatus(slideComposerState.slides.length ? `Đã load ${slideComposerState.slides.length} slide.` : 'Project chưa có slide script.', slideComposerState.slides.length ? 'good' : 'warn');
    } catch (error) {
      setSlideComposerStatus(error.message || String(error), 'bad');
    }
  }

  async function saveSlideComposer() {
    if (!$('#slideComposer')) return;
    commitSlideEditorFields();
    if (!slideComposerState.project) {
      setSlideComposerStatus('Chọn project trước khi lưu slide.', 'bad');
      return;
    }
    if (!slideComposerState.slides.length) {
      setSlideComposerStatus('Không có slide để lưu.', 'bad');
      return;
    }
    const button = $('#saveSlideComposer');
    if (button) button.disabled = true;
    setSlideComposerStatus('Đang lưu slide...', 'warn');
    try {
      const response = await fetch('/api/slide-composer', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ project: slideComposerState.project, slides: slideComposerState.slides }),
      });
      const data = await response.json().catch(() => ({}));
      if (!response.ok) throw new Error(data.error || `HTTP ${response.status}`);
      slideComposerState.slides = Array.isArray(data.slides) ? data.slides : slideComposerState.slides;
      slideComposerState.mediaFiles = Array.isArray(data.media_files) ? data.media_files : slideComposerState.mediaFiles;
      setVoicePreviewUi(data.voice_preview || data.studio_state?.voice_preview || null);
      renderSlideComposerList();
      fillSlideEditor();
      syncStudioMediaButton();
      refreshSlideComposerPreview();
      markVoicePreviewStale('Đã sửa slide. Bấm Preview voice để cập nhật audio.');
      setSlideComposerStatus('Đã lưu slide editor.', 'good');
    } catch (error) {
      setSlideComposerStatus(error.message || String(error), 'bad');
    } finally {
      if (button) button.disabled = false;
    }
  }

  async function uploadSlideComposerMedia(event) {
    const input = event.currentTarget;
    const file = input.files?.[0];
    const slide = currentComposerSlide();
    if (!file || !slide) return;
    if (!slideComposerState.project) {
      setSlideComposerStatus('Chọn project trước khi upload media.', 'bad');
      return;
    }
    setSlideComposerStatus('Đang upload media...', 'warn');
    try {
      const dataUrl = await fileToDataUrl(file);
      const response = await fetch('/api/slide-media', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          project: slideComposerState.project,
          media: { name: file.name, data: dataUrl },
        }),
      });
      const data = await response.json().catch(() => ({}));
      if (!response.ok) throw new Error(data.error || `HTTP ${response.status}`);
      slide.media = {
        ...(data.media || {}),
        fit: $('#studioSlideMediaFit')?.value || 'cover',
        position: $('#studioSlideMediaPosition')?.value || 'center',
      };
      slideComposerState.mediaFiles = Array.isArray(data.files) ? data.files : slideComposerState.mediaFiles;
      fillSlideEditor();
      renderSlideComposerList();
      syncStudioMediaButton();
      setSlideComposerStatus(`Đã upload media cho Slide ${slideComposerState.selected + 1}. Đang lưu vào project...`, 'warn');
      await saveSlideComposer();
      setSlideComposerStatus(`Đã gắn media vào Slide ${slideComposerState.selected + 1}.`, 'good');
    } catch (error) {
      setSlideComposerStatus(error.message || String(error), 'bad');
    } finally {
      input.value = '';
    }
  }

  function clearSlideComposerMedia() {
    const slide = currentComposerSlide();
    if (!slide) return;
    slide.media = null;
    fillSlideEditor();
    syncStudioMediaButton();
    setSlideComposerStatus('Đã gỡ media khỏi slide hiện tại. Bấm Lưu slide để áp dụng.', 'warn');
  }

  function updateTemplateUrl(templateName) {
    const templateAwarePath = window.location.pathname.replace(/\/$/, '') || '/';
    if (!['/studio', '/templates'].includes(templateAwarePath)) return;
    const url = new URL(window.location.href);
    if (templateName) url.searchParams.set('template', templateName);
    else url.searchParams.delete('template');
    window.history.replaceState(null, '', url);
  }

  function setSelectedTemplate(templateName, updateUrl = true) {
    const fallbackTemplate = templates.find((template) => template.name === 'viro-slide-starter') || templates[0];
    const template = templateMap.get(templateName) || fallbackTemplate || null;
    if (!template) return;
    state.template = template.name;
    localStorage.setItem('viro-selected-template', template.name);

    const select = $('#templateSelect');
    if (select) select.value = template.name;

    const previewLink = $('#templatePreviewLink');
    if (previewLink) previewLink.href = template.url || `/template/${encodeURIComponent(template.name)}/`;

    const studioLink = $('#useTemplateInStudio');
    if (studioLink) studioLink.href = `/studio?template=${encodeURIComponent(template.name)}`;

    const editSelectedTemplate = $('#editSelectedTemplate');
    if (editSelectedTemplate) {
      editSelectedTemplate.dataset.template = template.name;
      editSelectedTemplate.href = template.edit_url || `/template/${encodeURIComponent(template.name)}/edit`;
    }

    const templatePreviewAction = $('#templatePreviewAction');
    if (templatePreviewAction) templatePreviewAction.href = template.url || `/template/${encodeURIComponent(template.name)}/`;
    const exportSelectedTemplate = $('#exportSelectedTemplate');
    if (exportSelectedTemplate) exportSelectedTemplate.href = `/api/templates/export?template=${encodeURIComponent(template.name)}`;

    $all('.template-card[data-template]').forEach((card) => {
      card.classList.toggle('selected', card.dataset.template === template.name);
    });
    $all('.template-select-btn[data-template]').forEach((button) => {
      button.classList.toggle('active', button.dataset.template === template.name);
    });

    syncStudioModeActions();
    if (updateUrl) updateTemplateUrl(template.name);
  }

  function updateProjectUrl(projectName) {
    const projectAwarePath = window.location.pathname.replace(/\/$/, '') || '/';
    if (!['/', '/home', '/studio', '/projects', '/platforms', '/upload'].includes(projectAwarePath)) return;
    const url = new URL(window.location.href);
    url.searchParams.set('project', projectName);
    window.history.replaceState(null, '', url);
  }

  function setSelectedProject(projectName, updateUrl = true) {
    const project = projectMap.get(projectName);
    if (!project) {
      setStatus('Chưa có dự án slide nào để render.', 'bad');
      return;
    }

    state.project = project.name;
    state.outputUrl = project.output_url || project.video_url || `/slide/${encodeURIComponent(project.name)}/output/final_video.mp4`;
    state.voicePreview = null;
    state.voicePreviewCurrent = false;
    setVoicePreviewUi(null, { stale: true, message: 'Đang đọc trạng thái voice preview...' });

    const select = $('#projectSelect');
    if (select) select.value = project.name;
    setProjectVideoBadge(project);
    syncStudioMediaButton(project);
    syncVoiceCredentialForProject(project);
    loadSlideComposer(project.name);

    const selectedName = $('#selectedName');
    if (selectedName) selectedName.textContent = project.name;

    const selectedBadge = $('#selectedBadge');
    if (selectedBadge) selectedBadge.textContent = `${project.script_count || '?'} slide`;

    const previewLink = $('#previewLink');
    if (previewLink) previewLink.href = project.url;

    const existingVideoLink = $('#existingVideoLink');
    if (existingVideoLink) {
      existingVideoLink.href = project.video_url || state.outputUrl;
      existingVideoLink.hidden = !project.video_url;
    }

    const videoLink = $('#videoLink');
    if (videoLink) {
      videoLink.href = project.video_url || state.outputUrl;
      videoLink.hidden = true;
    }
    setRevealOutputButton(project.name, !state.busy && Boolean(project.video_url));
    setUploadCenterLink(project.name, Boolean(project.video_url));

    $all('.project-row[data-project]').forEach((row) => row.classList.toggle('selected', row.dataset.project === project.name));
    $all('.select-btn').forEach((button) => {
      const active = button.dataset.project === project.name;
      button.classList.toggle('active', active);
      button.setAttribute('aria-pressed', active ? 'true' : 'false');
      setButtonLabel(button, active ? 'Đang chọn' : 'Chọn');
    });

    if (!state.busy) {
      setRenderState('', []);
      if (project.video_url) {
        setStatus('', 'warn');
        showUploadPanel(project.name, project.video_url);
      } else {
        hideUploadPanel();
        setUploadEmpty(project.name, Boolean($('#uploadPanel')));
        setStatus(window.location.pathname === '/upload' ? `${project.name} chưa có final_video.mp4. Render trong Viro Studio trước khi publish.` : '', project.has_script ? 'warn' : 'bad');
      }
    }
    syncStudioModeActions();
    if (updateUrl && window.location.pathname === '/projects') {
      setStatus(`Đã chọn project ${project.name}. Bấm Studio để mở workspace render.`, 'good');
    }
    if (updateUrl) updateProjectUrl(project.name);
  }

  function markProjectHasOutput(projectName, videoUrl) {
    const project = projectMap.get(projectName);
    if (project) {
      project.video_url = videoUrl;
      project.has_output = true;
    }

    const row = rowForProject(projectName);
    const deleteButton = row ? row.querySelector('.delete-output-btn[data-project]') : null;
    if (deleteButton) {
      deleteButton.disabled = false;
      deleteButton.classList.remove('disabled');
    }
    const slot = row ? row.querySelector('.row-video-slot') : null;
    if (slot) {
      slot.textContent = '';
      slot.classList.remove('muted');
      const link = document.createElement('a');
      link.className = 'small-link icon-btn';
      link.href = videoUrl;
      link.target = '_blank';
      link.rel = 'noreferrer';
      link.innerHTML = '<span class="btn-icon">▶</span><span>Mở</span>';
      slot.appendChild(link);
    }

    const existingVideoLink = $('#existingVideoLink');
    if (existingVideoLink && state.project === projectName) {
      existingVideoLink.href = videoUrl;
      existingVideoLink.hidden = false;
    }
    if (state.project === projectName) setRevealOutputButton(projectName, true);
    if (state.project === projectName) setUploadCenterLink(projectName, true);
    if (state.project === projectName) setProjectVideoBadge(project);
  }

  function markProjectOutputDeleted(projectName) {
    const project = projectMap.get(projectName);
    if (project) {
      project.video_url = null;
      project.has_output = false;
    }

    const row = rowForProject(projectName);
    const deleteButton = row ? row.querySelector('.delete-output-btn[data-project]') : null;
    if (deleteButton) {
      deleteButton.disabled = true;
      deleteButton.classList.add('disabled');
    }
    const slot = row ? row.querySelector('.row-video-slot') : null;
    if (slot) {
      slot.textContent = '';
      slot.classList.remove('muted');
      const disabled = document.createElement('span');
      disabled.className = 'icon-btn disabled';
      disabled.innerHTML = '<span class="btn-icon">▶</span><span>Mở</span>';
      slot.appendChild(disabled);
    }

    if (state.project === projectName) {
      const existingVideoLink = $('#existingVideoLink');
      if (existingVideoLink) existingVideoLink.hidden = true;
      const videoLink = $('#videoLink');
      if (videoLink) videoLink.hidden = true;
      setRevealOutputButton(projectName, false);
      setUploadCenterLink(projectName, false);
      setProjectVideoBadge(project);
      setUploadEmpty(projectName, Boolean($('#uploadPanel')));
      hideUploadPanel();
    }
  }

  async function revealOutput(projectName) {
    const project = projectMap.get(projectName || state.project);
    if (!project) {
      setStatus('Không tìm thấy dự án để mở output.', 'bad');
      return;
    }
    const button = $('#revealOutput');
    if (button) button.disabled = true;
    setStatus(`Đang mở vị trí final_video.mp4 của ${project.name}...`, 'warn');
    try {
      const response = await fetch('/api/output/reveal', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ project: project.name }),
      });
      const data = await response.json();
      if (!response.ok) throw new Error(data.error || `HTTP ${response.status}`);
      setStatus(`Đã mở vị trí final_video.mp4 của ${project.name}.`, 'good');
    } catch (error) {
      setStatus(error.message || String(error), 'bad');
    } finally {
      if (button) button.disabled = false;
    }
  }

  async function deleteOutput(projectName) {
    const project = projectMap.get(projectName);
    if (!project) {
      setStatus('Không tìm thấy dự án để xoá output.', 'bad');
      return;
    }
    if (!project.has_output) {
      setStatus(`${project.name} chưa có output để xoá.`, 'warn');
      return;
    }

    const confirmed = window.confirm(
      `Xoá toàn bộ thư mục output của "${project.name}"?\n\nHành động này sẽ xoá final_video.mp4 và các file render cache trong thư mục output của project.`
    );
    if (!confirmed) return;

    const buttons = [
      ...$all('.delete-output-btn[data-project]').filter((button) => button.dataset.project === project.name),
      $('#deleteSelectedOutput'),
    ].filter(Boolean);
    buttons.forEach((button) => { button.disabled = true; });
    setStatus(`Đang xoá output của ${project.name}...`, 'warn');

    try {
      const response = await fetch('/api/output/delete', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ project: project.name, confirm: true }),
      });
      const data = await response.json();
      if (!response.ok) throw new Error(data.error || `HTTP ${response.status}`);

      markProjectOutputDeleted(project.name);
      setStatus(data.deleted ? `Đã xoá output của ${project.name}.` : `${project.name} chưa có output để xoá.`, 'good');
    } catch (error) {
      setStatus(error.message || String(error), 'bad');
      setRenderState('Không thể xoá output', [error.message || String(error)], 'failed');
    } finally {
      buttons.forEach((button) => {
        const isProjectButton = button.classList.contains('delete-output-btn');
        button.disabled = isProjectButton && !project.has_output;
        if (isProjectButton) button.classList.toggle('disabled', !project.has_output);
      });
    }
  }

  function fileToBase64(file) {
    return new Promise((resolve, reject) => {
      const reader = new FileReader();
      reader.onload = () => {
        const result = String(reader.result || '');
        resolve(result.includes(',') ? result.split(',', 2)[1] : result);
      };
      reader.onerror = () => reject(reader.error || new Error('Không đọc được file audio.'));
      reader.readAsDataURL(file);
    });
  }

  async function previewVoice() {
    const button = $('#previewVoice');
    const payload = currentVoicePayload({ includeForce: true });
    if (!payload.project) {
      setStatus('Hãy chọn project trước khi Preview voice.', 'bad');
      return;
    }
    if (payload.engine === 'elevenlabs' && payload.mode === 'upload') {
      setStatus('Mode upload dùng file audio trực tiếp, không cần Preview voice API.', 'warn');
      return;
    }
    if (button) button.disabled = true;
    setVoicePreviewUi(state.voicePreview, { stale: true, message: 'Đang tạo voice preview...' });
    setRenderState('Đang preview voice', ['Đang gọi TTS và tạo timing cache.']);
    try {
      const response = await fetch('/api/voice-preview', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(payload),
      });
      const job = await response.json().catch(() => ({}));
      if (!response.ok) throw new Error(job.error || `HTTP ${response.status}`);
      state.voiceJobId = job.id;
      await pollVoicePreviewJob();
      if (state.voicePollTimer) window.clearInterval(state.voicePollTimer);
      state.voicePollTimer = window.setInterval(pollVoicePreviewJob, 1500);
    } catch (error) {
      setStatus(error.message || String(error), 'bad');
      setVoicePreviewUi(state.voicePreview, { stale: true, message: error.message || String(error) });
      setRenderState('Không thể preview voice', [error.message || String(error)], 'failed');
      if (button) button.disabled = false;
    }
  }

  async function startRender() {
    const startButton = $('#startRender');
    const stopButton = $('#stopRender');
    const videoLink = $('#videoLink');
    if (startButton) startButton.disabled = true;
    if (stopButton) {
      stopButton.hidden = true;
      stopButton.disabled = true;
    }
    if (videoLink) videoLink.hidden = true;
    setRevealOutputButton(state.project, false);
    state.busy = true;
    state.canceling = false;
    setRenderState('Đang chuẩn bị render', ['Kiểm tra project và thông số render.']);

    try {
      const project = currentProject();
      if (!project) throw new Error('Vui lòng chọn một dự án slide trước.');

      const payload = currentVoicePayload({ includeForce: true });
      payload.project = project.name;

      if (state.engine === 'elevenlabs') {
        if (payload.mode === 'upload') {
          const file = $('#elevenFile')?.files?.[0];
          if (!file) throw new Error('Vui lòng chọn file audio ElevenLabs trước.');
          if (file.size > MAX_AUDIO_BYTES) throw new Error('File audio lớn hơn 200 MB.');
          setRenderState('Đang chuẩn bị render', [`Đang nạp file audio: ${file.name}`]);
          payload.audio = {
            name: file.name,
            data: await fileToBase64(file),
          };
        } else {
          if (!state.voicePreviewCurrent) {
            throw new Error('Voice preview đã cũ hoặc chưa có. Hãy bấm Preview voice trước khi Generate video.');
          }
          setRenderState('Đang chuẩn bị render', ['ElevenLabs API: eleven_v3 gọi full script 1 lần, model khác mới tách từng slide nếu hỗ trợ context.']);
        }
      } else {
        setRenderState(
          'Đang chuẩn bị render',
          [payload.edgePerSlide ? 'Edge TTS sẽ tạo từng slide như luồng cũ.' : 'Edge TTS sẽ gọi full script 1 lần rồi split voiceover.']
        );
      }

      const response = await fetch('/api/render', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(payload),
      });
      const data = await response.json();
      if (!response.ok) throw new Error(data.error || `HTTP ${response.status}`);

      state.jobId = data.id;
      if (stopButton) {
        stopButton.hidden = false;
        stopButton.disabled = false;
      }
      const progress = renderProgressForJob(data);
      setStatus(`Đã bắt đầu job render: ${data.id}`, 'good');
      setRenderState(renderTitle(progress.title, data), [progress.log]);
      await pollJob();
      if (state.pollTimer) window.clearInterval(state.pollTimer);
      state.pollTimer = window.setInterval(pollJob, 1500);
    } catch (error) {
      state.busy = false;
      setStatus(error.message || String(error), 'bad');
      setRenderState('Không thể bắt đầu render', [error.message || String(error)], 'failed');
      if (startButton) startButton.disabled = false;
      if (stopButton) {
        stopButton.hidden = true;
        stopButton.disabled = true;
      }
    }
  }

  async function stopRender() {
    if (!state.jobId || state.canceling) return;
    const confirmed = window.confirm('Dừng job render đang chạy? Audio/video đang tạo dở có thể còn lại trong thư mục output.');
    if (!confirmed) return;
    const stopButton = $('#stopRender');
    if (stopButton) stopButton.disabled = true;
    state.canceling = true;
    setStatus('Đang dừng render...', 'warn');
    setRenderState('Đang dừng render...', ['Đang gửi tín hiệu dừng job render.']);
    try {
      const response = await fetch(`/api/jobs/${state.jobId}/cancel`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({}),
      });
      const job = await response.json();
      if (!response.ok) throw new Error(job.error || `HTTP ${response.status}`);
      const progress = renderProgressForJob(job);
      setRenderState(renderTitle(progress.title, job), [progress.log], job.status === 'cancelled' ? 'cancelled' : 'running');
    } catch (error) {
      state.canceling = false;
      if (stopButton) stopButton.disabled = false;
      setStatus(error.message || String(error), 'bad');
      setRenderState('Không thể dừng render', [error.message || String(error)], 'failed');
    }
  }

  async function pollJob() {
    if (!state.jobId) return;
    const startButton = $('#startRender');
    const stopButton = $('#stopRender');
    const videoLink = $('#videoLink');

    try {
      const response = await fetch(`/api/jobs/${state.jobId}`, { cache: 'no-store' });
      const job = await response.json();
      if (!response.ok) throw new Error(job.error || `HTTP ${response.status}`);

      const progress = renderProgressForJob(job);
      if (job.status === 'done') {
        if (state.pollTimer) window.clearInterval(state.pollTimer);
        state.pollTimer = null;
        state.busy = false;
        state.canceling = false;
        if (startButton) startButton.disabled = false;
        if (stopButton) {
          stopButton.hidden = true;
          stopButton.disabled = true;
        }
        const finalUrl = job.video_url || state.outputUrl;
        const renderUrl = job.render_url || finalUrl;
        const renderName = String(job.render_name || '').trim();
        setStatus('Render hoàn tất. final_video.mp4 đã sẵn sàng.', 'good');
        if (renderName) {
          setStatus(`Render hoàn tất. final_video.mp4 đã cập nhật, bản riêng: ${renderName}.`, 'good');
        }
        setRenderState(renderTitle(progress.title, job), [progress.log], 'done');
        if (videoLink) {
          videoLink.href = renderUrl;
          videoLink.hidden = false;
        }
        setRevealOutputButton(job.project || state.project, true);
        markProjectHasOutput(job.project || state.project, finalUrl);
        await showUploadPanel(job.project || state.project, finalUrl);
      } else if (job.status === 'failed') {
        if (state.pollTimer) window.clearInterval(state.pollTimer);
        state.pollTimer = null;
        state.busy = false;
        state.canceling = false;
        if (startButton) startButton.disabled = false;
        if (stopButton) {
          stopButton.hidden = true;
          stopButton.disabled = true;
        }
        setStatus(progress.log || 'Render thất bại.', 'bad');
        setRenderState(renderTitle(progress.title, job), [progress.log], 'failed');
      } else if (job.status === 'cancelled') {
        if (state.pollTimer) window.clearInterval(state.pollTimer);
        state.pollTimer = null;
        state.busy = false;
        state.canceling = false;
        if (startButton) startButton.disabled = false;
        if (stopButton) {
          stopButton.hidden = true;
          stopButton.disabled = true;
        }
        setStatus('Đã dừng render.', 'warn');
        setRenderState(renderTitle(progress.title, job), [progress.log], 'cancelled');
      } else {
        if (stopButton) {
          stopButton.hidden = false;
          stopButton.disabled = job.status === 'cancelling' || state.canceling;
        }
        setStatus(job.status === 'queued' ? 'Render đang chờ xử lý...' : 'Render đang chạy...', 'warn');
        setRenderState(renderTitle(progress.title, job), [progress.log]);
      }
    } catch (error) {
      if (state.pollTimer) window.clearInterval(state.pollTimer);
      state.pollTimer = null;
      state.busy = false;
      state.canceling = false;
      if (startButton) startButton.disabled = false;
      if (stopButton) {
        stopButton.hidden = true;
        stopButton.disabled = true;
      }
      setStatus(error.message || String(error), 'bad');
      setRenderState('Lỗi khi đọc trạng thái render', [error.message || String(error)], 'failed');
    }
  }

  async function pollVoicePreviewJob() {
    if (!state.voiceJobId) return;
    const button = $('#previewVoice');
    try {
      const response = await fetch(`/api/jobs/${state.voiceJobId}`, { cache: 'no-store' });
      const job = await response.json();
      if (!response.ok) throw new Error(job.error || `HTTP ${response.status}`);
      const progress = renderProgressForJob(job);
      if (job.status === 'done') {
        if (state.voicePollTimer) window.clearInterval(state.voicePollTimer);
        state.voicePollTimer = null;
        state.voiceJobId = null;
        if (button) button.disabled = false;
        const preview = job.voice_preview || {
          current: true,
          audio_url: job.audio_url,
          timing_url: job.timing_url,
          duration: job.duration,
        };
        setVoicePreviewUi(preview);
        setStatus('Voice preview đã sẵn sàng. Nghe lại trước khi Generate video.', 'good');
        setRenderState('Hoàn tất preview voice', [progress.log || 'Voice preview đã sẵn sàng.'], 'done');
      } else if (job.status === 'failed' || job.status === 'cancelled') {
        if (state.voicePollTimer) window.clearInterval(state.voicePollTimer);
        state.voicePollTimer = null;
        state.voiceJobId = null;
        if (button) button.disabled = false;
        markVoicePreviewStale(job.error || progress.log || 'Voice preview thất bại.');
        setStatus(job.error || progress.log || 'Voice preview thất bại.', 'bad');
        setRenderState('Voice preview thất bại', [job.error || progress.log], 'failed');
      } else {
        setVoicePreviewUi(state.voicePreview, { stale: true, message: progress.title });
        setRenderState(progress.title, [progress.log]);
      }
    } catch (error) {
      if (state.voicePollTimer) window.clearInterval(state.voicePollTimer);
      state.voicePollTimer = null;
      state.voiceJobId = null;
      if (button) button.disabled = false;
      setVoicePreviewUi(state.voicePreview, { stale: true, message: error.message || String(error) });
      setStatus(error.message || String(error), 'bad');
    }
  }

  $all('[data-engine]').forEach((button) => {
    button.addEventListener('click', () => setEngine(button.dataset.engine));
  });

  $all('input[name="elevenMode"]').forEach((input) => {
    input.addEventListener('change', () => {
      syncElevenMode();
      markVoicePreviewStale('Đã đổi mode voice. Bấm Preview voice để cập nhật audio.');
    });
  });
  syncElevenMode();

  const advancedSettings = $('#advancedSettings');
  if (advancedSettings) {
    advancedSettings.open = localStorage.getItem('viro-render-advanced-open-v3') === '1';
    advancedSettings.addEventListener('toggle', () => {
      localStorage.setItem('viro-render-advanced-open-v3', advancedSettings.open ? '1' : '0');
      syncAdvancedSettings();
    });
  }
  syncAdvancedSettings();
  loadElevenLabsConfig();

  const elevenCredentialSelect = $('#elevenCredentialSelect');
  if (elevenCredentialSelect) {
    elevenCredentialSelect.addEventListener('change', () => {
      syncVoiceCredentialStatus();
      markVoicePreviewStale('Đã đổi API key/connection. Bấm Preview voice để cập nhật audio.');
    });
  }

  const elevenVoiceInput = $('#elevenVoice');
  if (elevenVoiceInput) {
    elevenVoiceInput.addEventListener('paste', () => {
      window.setTimeout(() => scheduleElevenLabsVoiceSave(80), 0);
    });
    elevenVoiceInput.addEventListener('input', () => {
      scheduleElevenLabsVoiceSave(700);
      markVoicePreviewStale('Đã đổi Voice ID. Bấm Preview voice để cập nhật audio.');
    });
  }

  const elevenApiKeyInput = $('#elevenApiKey');
  if (elevenApiKeyInput) {
    elevenApiKeyInput.addEventListener('paste', () => {
      window.setTimeout(() => scheduleElevenLabsApiKeySave(80), 0);
    });
    elevenApiKeyInput.addEventListener('input', () => scheduleElevenLabsApiKeySave(700));
  }

  const elevenProxyBaseInput = $('#elevenProxyBaseUrl');
  if (elevenProxyBaseInput) {
    elevenProxyBaseInput.addEventListener('input', () => scheduleElevenLabsProxySave(700));
  }

  const elevenProxyKeyInput = $('#elevenProxyKey');
  if (elevenProxyKeyInput) {
    elevenProxyKeyInput.addEventListener('paste', () => {
      window.setTimeout(() => scheduleElevenLabsProxySave(80), 0);
    });
    elevenProxyKeyInput.addEventListener('input', () => scheduleElevenLabsProxySave(700));
  }

  $all('[data-duration]').forEach((button) => {
    button.addEventListener('click', () => setVideoDuration(button.dataset.duration));
  });

  $all('.speed-preset[data-speed]').forEach((button) => {
    button.addEventListener('click', () => {
      const speedInput = $('#renderSpeed');
      if (speedInput) speedInput.value = button.dataset.speed;
      syncSpeedPresets();
      markVoicePreviewStale('Đã đổi tốc độ đọc. Bấm Preview voice để cập nhật audio.');
    });
  });

  const speedInput = $('#renderSpeed');
  if (speedInput) {
    speedInput.addEventListener('input', () => {
      syncSpeedPresets();
      markVoicePreviewStale('Đã đổi tốc độ đọc. Bấm Preview voice để cập nhật audio.');
    });
  }

  const edgeVoiceInput = $('#edgeVoice');
  if (edgeVoiceInput) {
    edgeVoiceInput.addEventListener('input', () => markVoicePreviewStale('Đã đổi giọng Edge TTS. Bấm Preview voice để cập nhật audio.'));
  }
  const edgePerSlideInput = $('#edgePerSlide');
  if (edgePerSlideInput) {
    edgePerSlideInput.addEventListener('change', () => markVoicePreviewStale('Đã đổi cách tạo voice. Bấm Preview voice để cập nhật audio.'));
  }

  const elevenFileInput = $('#elevenFile');
  const elevenFileName = $('#elevenFileName');
  if (elevenFileInput && elevenFileName) {
    elevenFileInput.addEventListener('change', () => {
      const file = elevenFileInput.files?.[0];
      elevenFileName.textContent = file ? file.name : 'Chưa chọn file';
    });
  }

  const projectSelect = $('#projectSelect');
  if (projectSelect) {
    projectSelect.addEventListener('change', () => setSelectedProject(projectSelect.value));
  }

  const templateSelect = $('#templateSelect');
  if (templateSelect) {
    templateSelect.addEventListener('change', () => setSelectedTemplate(templateSelect.value));
  }

  const aiCredentialSelect = $('#aiCredentialSelect');
  if (aiCredentialSelect) {
    aiCredentialSelect.addEventListener('change', () => {
      const aiState = $('#aiStoryboardState');
      if (!aiState) return;
      const usingOpenAi = Boolean(aiCredentialSelect.value);
      aiState.textContent = usingOpenAi
        ? 'Sẽ gọi OpenAI để tạo structured storyboard.'
        : 'Chọn OpenAI key trước khi tạo storyboard bằng AI.';
      aiState.className = `config-state ${usingOpenAi ? 'good' : 'bad'}`;
    });
  }

  $all('.template-select-btn[data-template]').forEach((button) => {
    button.addEventListener('click', () => setSelectedTemplate(button.dataset.template));
  });

  const createStoryboardButton = $('#createStoryboard');
  if (createStoryboardButton) {
    createStoryboardButton.addEventListener('click', generateStoryboard);
  }

  const studioMediaButton = $('#studioMediaButton');
  if (studioMediaButton) {
    studioMediaButton.addEventListener('click', () => {
      if (!currentComposerSlide()) {
        openStoryboardModal('media');
        return;
      }
      $('#studioSlideMediaFile')?.click();
    });
  }
  const reloadSlideComposerButton = $('#reloadSlideComposer');
  if (reloadSlideComposerButton) reloadSlideComposerButton.addEventListener('click', () => loadSlideComposer(state.project));
  const saveSlideComposerButton = $('#saveSlideComposer');
  if (saveSlideComposerButton) saveSlideComposerButton.addEventListener('click', saveSlideComposer);
  const slideMediaFileInput = $('#studioSlideMediaFile');
  if (slideMediaFileInput) slideMediaFileInput.addEventListener('change', uploadSlideComposerMedia);
  const clearSlideMediaButton = $('#clearSlideMedia');
  if (clearSlideMediaButton) clearSlideMediaButton.addEventListener('click', clearSlideComposerMedia);
  ['#studioSlideText', '#studioSlideScreenText', '#studioSlideVisualDirection', '#studioSlideDuration', '#studioSlideTransition', '#studioSlideReveal', '#studioSlideMediaFit', '#studioSlideMediaPosition'].forEach((selector) => {
    const field = $(selector);
    if (field) field.addEventListener('change', commitSlideEditorFields);
  });

  $all('[data-storyboard-tab]').forEach((button) => {
    button.addEventListener('click', () => {
      setStoryboardTab(button.dataset.storyboardTab);
      if (button.dataset.storyboardTab === 'media') {
        renderStoryboardMedia();
        setStoryboardStatus('Media asset của project được liệt kê bên dưới.');
      }
    });
  });

  const closeStoryboardButton = $('#closeStoryboardModal');
  if (closeStoryboardButton) closeStoryboardButton.addEventListener('click', closeStoryboardModal);

  const storyboardModal = $('#storyboardModal');
  if (storyboardModal) {
    storyboardModal.addEventListener('click', (event) => {
      if (event.target === storyboardModal) closeStoryboardModal();
    });
  }

  const storyboardLoadScript = $('#storyboardLoadScript');
  if (storyboardLoadScript) storyboardLoadScript.addEventListener('click', loadProjectScriptIntoEditor);

  const storyboardSaveScript = $('#storyboardSaveScript');
  if (storyboardSaveScript) storyboardSaveScript.addEventListener('click', saveStudioScriptToProject);

  const storyboardContinue = $('#storyboardContinue');
  if (storyboardContinue) storyboardContinue.addEventListener('click', continueStoryboardToRender);

  $all('.select-btn').forEach((button) => {
    button.addEventListener('click', () => setSelectedProject(button.dataset.project));
  });

  $all('.delete-output-btn[data-project]').forEach((button) => {
    button.addEventListener('click', () => deleteOutput(button.dataset.project));
  });

  $all('.copy-script-btn[data-project]').forEach((button) => {
    button.addEventListener('click', () => copyProjectScript(button.dataset.project, button));
  });

  const deleteSelectedOutput = $('#deleteSelectedOutput');
  if (deleteSelectedOutput) {
    deleteSelectedOutput.addEventListener('click', () => deleteOutput(state.project));
  }

  const revealOutputButton = $('#revealOutput');
  if (revealOutputButton) {
    revealOutputButton.addEventListener('click', () => revealOutput(revealOutputButton.dataset.project || state.project));
  }

  const stopRenderButton = $('#stopRender');
  if (stopRenderButton) {
    stopRenderButton.addEventListener('click', stopRender);
  }

  async function uploadYoutubeVideo(project) {
    const response = await fetch('/api/social/youtube/upload', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        project,
        title: $('#uploadTitle')?.value || '',
        description: $('#youtubeDescription')?.value || '',
        privacyStatus: $('#youtubePrivacy')?.value || 'private',
      }),
    });
    const data = await response.json();
    if (!response.ok) throw new Error(data.error || `HTTP ${response.status}`);
    return data;
  }

  async function uploadFacebookReel(project, facebookVideoState = $('#facebookVideoState')?.value || 'DRAFT') {
    const response = await fetch('/api/social/facebook/upload', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        project,
        facebookCaption: $('#facebookCaption')?.value || '',
        facebookVideoState,
      }),
    });
    const data = await response.json();
    if (!response.ok) throw new Error(data.error || `HTTP ${response.status}`);
    return data;
  }

  async function commentFacebookSource(project) {
    const response = await fetch('/api/social/facebook/comment-source', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        project,
        sourceCommentTargetId: state.facebookCommentTargetId,
        facebookSourceComment: $('#facebookSourceComment')?.value || '',
      }),
    });
    const data = await response.json();
    if (!response.ok) throw new Error(data.error || `HTTP ${response.status}`);
    return data;
  }

  async function saveFacebookConfig() {
    const pageIdInput = $('#facebookPageId');
    const tokenInput = $('#facebookPageAccessToken');
    const button = $('#saveFacebookConfig');
    const pageId = pageIdInput?.value.trim() || '';
    const pageAccessToken = tokenInput?.value.trim() || '';
    if (!pageId || !pageAccessToken) {
      setUploadStatus('Nhập đủ Facebook Page ID và Page access token trước khi lưu.', 'bad');
      return;
    }
    if (facebookConfigSaving) return;
    facebookConfigSaving = true;
    if (button) button.disabled = true;
    setUploadStatus('Đang lưu cấu hình Facebook...', 'warn');
    try {
      const response = await fetch('/api/social/facebook/config', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ pageId, pageAccessToken }),
      });
      const data = await response.json();
      if (!response.ok) throw new Error(data.error || `HTTP ${response.status}`);
      if (tokenInput) tokenInput.value = '';
      if (pageIdInput) pageIdInput.value = data.active_page_id || pageId;
      await refreshSocialStatus();
      setUploadStatus('Đã lưu Facebook Page ID và Page access token vào config/social-upload.json.', 'good');
      closeFacebookConfigModal();
    } catch (error) {
      setUploadStatus(error.message || String(error), 'bad');
    } finally {
      facebookConfigSaving = false;
      if (button) button.disabled = false;
    }
  }

  function checkedValues(form, name) {
    return Array.from(form.querySelectorAll(`input[name="${name}"]:checked`)).map((input) => input.value);
  }

  function closeProjectPickers(except = null) {
    $all('[data-project-picker]').forEach((picker) => {
      if (picker === except) return;
      const menu = picker.querySelector('[data-project-picker-menu]');
      const trigger = picker.querySelector('.connection-project-picker-trigger');
      if (menu) menu.hidden = true;
      if (trigger) trigger.setAttribute('aria-expanded', 'false');
    });
  }

  function setProjectPickerOpen(picker, open) {
    if (!picker) return;
    const menu = picker.querySelector('[data-project-picker-menu]');
    const trigger = picker.querySelector('.connection-project-picker-trigger');
    if (!menu || !trigger || trigger.disabled) return;
    if (open) closeProjectPickers(picker);
    menu.hidden = !open;
    trigger.setAttribute('aria-expanded', open ? 'true' : 'false');
    if (open) {
      const search = picker.querySelector('[data-project-picker-search]');
      window.setTimeout(() => search?.focus(), 0);
    }
  }

  function updateProjectPicker(picker) {
    if (!picker) return;
    const label = picker.querySelector('[data-project-picker-label]');
    const checked = Array.from(picker.querySelectorAll('input[name="project_ids"]:checked'));
    if (!label) return;
    if (!checked.length) {
      label.textContent = 'Chưa gán project';
      label.title = '';
      return;
    }
    const names = checked.map((input) => input.value);
    label.textContent = names.length <= 2 ? names.join(', ') : `${names.length} project đã chọn`;
    label.title = names.join(', ');
  }

  function filterProjectPicker(picker) {
    if (!picker) return;
    const query = (picker.querySelector('[data-project-picker-search]')?.value || '').trim().toLowerCase();
    let visibleCount = 0;
    picker.querySelectorAll('.project-check').forEach((label) => {
      const text = label.textContent.trim().toLowerCase();
      const visible = !query || text.includes(query);
      label.hidden = !visible;
      if (visible) visibleCount += 1;
    });
    const empty = picker.querySelector('[data-project-picker-empty]');
    if (empty) empty.hidden = visibleCount !== 0;
  }

  function syncProjectPickers(root = document) {
    Array.from(root.querySelectorAll('[data-project-picker]')).forEach((picker) => {
      updateProjectPicker(picker);
      filterProjectPicker(picker);
    });
  }

  function initProjectPickers(root = document) {
    Array.from(root.querySelectorAll('[data-project-picker]')).forEach((picker) => {
      if (picker.dataset.bound === '1') {
        updateProjectPicker(picker);
        return;
      }
      picker.dataset.bound = '1';
      const trigger = picker.querySelector('.connection-project-picker-trigger');
      const search = picker.querySelector('[data-project-picker-search]');
      trigger?.addEventListener('click', () => {
        const menu = picker.querySelector('[data-project-picker-menu]');
        setProjectPickerOpen(picker, Boolean(menu?.hidden));
      });
      search?.addEventListener('input', () => filterProjectPicker(picker));
      picker.querySelectorAll('input[name="project_ids"]').forEach((input) => {
        input.addEventListener('change', () => updateProjectPicker(picker));
      });
      picker.querySelector('[data-project-picker-select-all]')?.addEventListener('click', () => {
        picker.querySelectorAll('input[name="project_ids"]').forEach((input) => {
          if (!input.closest('.project-check')?.hidden) input.checked = true;
        });
        updateProjectPicker(picker);
      });
      picker.querySelector('[data-project-picker-clear]')?.addEventListener('click', () => {
        picker.querySelectorAll('input[name="project_ids"]').forEach((input) => {
          input.checked = false;
        });
        updateProjectPicker(picker);
      });
      updateProjectPicker(picker);
    });
  }

  function setConnectionStatus(form, message, tone = 'warn') {
    const status = form?.querySelector?.('.connection-status');
    if (!status) return;
    status.hidden = !message;
    status.textContent = message || '';
    status.className = `status ${tone} connection-status`;
  }

  async function postConnectionJson(path, payload) {
    const response = await fetch(path, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(payload),
    });
    const data = await response.json().catch(() => ({}));
    if (!response.ok) throw new Error(data.error || `HTTP ${response.status}`);
    return data;
  }

  function connectionPayloadFromForm(form) {
    const formData = new FormData(form);
    return {
      id: String(formData.get('id') || '').trim(),
      name: String(formData.get('name') || '').trim(),
      provider: String(formData.get('provider') || '').trim(),
      kind: String(formData.get('kind') || '').trim(),
      account_id: String(formData.get('account_id') || '').trim(),
      endpoint_url: String(formData.get('endpoint_url') || '').trim(),
      secret_label: String(formData.get('secret_label') || '').trim(),
      secret_value: String(formData.get('secret_value') || '').trim(),
      notes: String(formData.get('notes') || '').trim(),
      project_ids: checkedValues(form, 'project_ids'),
    };
  }

  function connectionTone(data) {
    if (data?.status === 'ready') return 'good';
    return data?.ok ? 'warn' : 'bad';
  }

  function connectionHealthMessage(data, fallback = 'Đã test kết nối.') {
    if (!data) return fallback;
    const failed = Array.isArray(data.checks) ? data.checks.find((check) => check.status === 'bad') : null;
    const warning = Array.isArray(data.checks) ? data.checks.find((check) => check.status === 'warn') : null;
    if (failed) return data.message || failed.detail || fallback;
    if (warning) return data.message || warning.detail || fallback;
    return data.message || fallback;
  }

  async function testConnectionForm(form, button = null) {
    if (!form) return null;
    const payload = connectionPayloadFromForm(form);
    if (!payload.name) {
      setConnectionStatus(form, 'Nhập tên account/key trước khi test.', 'bad');
      return null;
    }
    if (button) button.disabled = true;
    setConnectionStatus(form, 'Đang test key...', 'warn');
    try {
      const data = await postConnectionJson('/api/connections/test', payload);
      setConnectionStatus(form, connectionHealthMessage(data), connectionTone(data));
      return data;
    } catch (error) {
      setConnectionStatus(form, error.message || String(error), 'bad');
      return null;
    } finally {
      if (button) button.disabled = false;
    }
  }

  async function submitConnectionCreate(event) {
    event.preventDefault();
    const form = event.currentTarget;
    const payload = connectionPayloadFromForm(form);
    const submitButton = form.querySelector('button[type="submit"]');
    if (submitButton) submitButton.disabled = true;
    setConnectionStatus(form, 'Đang test key trước khi lưu...', 'warn');
    try {
      const health = await postConnectionJson('/api/connections/test', payload);
      if (!health.ok) {
        setConnectionStatus(form, connectionHealthMessage(health), connectionTone(health));
        return;
      }
      setConnectionStatus(form, 'Test xong. Đang lưu kết nối...', connectionTone(health));
      await postConnectionJson('/api/connections', payload);
      setConnectionStatus(form, 'Đã lưu kết nối.', 'good');
      window.location.reload();
    } catch (error) {
      setConnectionStatus(form, error.message || String(error), 'bad');
    } finally {
      if (submitButton) submitButton.disabled = false;
    }
  }

  function openCreateConnectionModal() {
    const form = $('.connection-create-form');
    if (!form) return;
    form.reset();
    setConnectionStatus(form, '', 'warn');
    syncProjectPickers(form);
    form.classList.add('open');
    window.setTimeout(() => form.elements.name?.focus(), 0);
  }

  function closeCreateConnectionModal() {
    const form = $('.connection-create-form');
    if (!form) return;
    form.classList.remove('open');
    closeProjectPickers();
  }

  function openEditConnectionModal(button) {
    const card = button.closest('.connection-card');
    const modal = $('#editConnectionModal');
    const form = $('#editConnectionForm');
    if (!card || !modal || !form) return;
    form.reset();
    form.elements.id.value = card.dataset.connectionId || '';
    form.elements.name.value = card.dataset.name || '';
    form.elements.provider.value = card.dataset.provider || 'elevenlabs';
    form.elements.kind.value = card.dataset.kind || 'api_key';
    form.elements.account_id.value = card.dataset.accountId || '';
    form.elements.endpoint_url.value = card.dataset.endpointUrl || '';
    form.elements.secret_label.value = card.dataset.secretLabel || '';
    form.elements.notes.value = card.dataset.notes || '';
    let projectIds = [];
    try {
      projectIds = JSON.parse(card.dataset.projectIds || '[]');
    } catch (_error) {
      projectIds = [];
    }
    form.querySelectorAll('input[name="project_ids"]').forEach((input) => {
      input.checked = projectIds.includes(input.value);
    });
    syncProjectPickers(form);
    setConnectionStatus(form, '', 'warn');
    modal.hidden = false;
    window.setTimeout(() => form.elements.name?.focus(), 0);
  }

  function closeEditConnectionModal() {
    const modal = $('#editConnectionModal');
    if (modal) modal.hidden = true;
    closeProjectPickers();
  }

  async function submitConnectionEdit(event) {
    event.preventDefault();
    const form = event.currentTarget;
    const payload = connectionPayloadFromForm(form);
    const submitButton = form.querySelector('button[type="submit"]');
    if (submitButton) submitButton.disabled = true;
    setConnectionStatus(form, 'Đang test key trước khi lưu...', 'warn');
    try {
      const health = await postConnectionJson('/api/connections/test', payload);
      if (!health.ok) {
        setConnectionStatus(form, connectionHealthMessage(health), connectionTone(health));
        return;
      }
      setConnectionStatus(form, 'Test xong. Đang lưu thay đổi...', connectionTone(health));
      await postConnectionJson('/api/connections', payload);
      setConnectionStatus(form, 'Đã lưu thay đổi.', 'good');
      window.location.reload();
    } catch (error) {
      setConnectionStatus(form, error.message || String(error), 'bad');
    } finally {
      if (submitButton) submitButton.disabled = false;
    }
  }

  async function submitConnectionAssign(event) {
    event.preventDefault();
    const form = event.currentTarget;
    const payload = {
      id: form.dataset.connectionId || form.querySelector('input[name="id"]')?.value || '',
      project_ids: checkedValues(form, 'project_ids'),
    };
    setConnectionStatus(form, 'Đang lưu mapping...', 'warn');
    try {
      await postConnectionJson('/api/connections/assign', payload);
      setConnectionStatus(form, 'Đã lưu mapping.', 'good');
      window.location.reload();
    } catch (error) {
      setConnectionStatus(form, error.message || String(error), 'bad');
    }
  }

  async function deleteConnection(button) {
    const connectionId = button.dataset.connectionId || button.closest('[data-connection-id]')?.dataset.connectionId || '';
    if (!connectionId) return;
    const card = button.closest('.connection-card');
    const form = card?.querySelector('.connection-assign-form');
    setConnectionStatus(form, 'Đang xóa kết nối...', 'warn');
    try {
      await postConnectionJson('/api/connections/delete', { id: connectionId });
      window.location.reload();
    } catch (error) {
      setConnectionStatus(form, error.message || String(error), 'bad');
    }
  }

  async function testConnection(button) {
    const connectionId = button.dataset.connectionId || button.closest('[data-connection-id]')?.dataset.connectionId || '';
    if (!connectionId) return;
    const card = button.closest('.connection-card');
    const form = card?.querySelector('.connection-assign-form');
    button.disabled = true;
    setConnectionStatus(form, 'Đang test kết nối...', 'warn');
    try {
      const data = await postConnectionJson('/api/connections/test', { id: connectionId });
      const tone = data.status === 'ready' ? 'good' : (data.ok ? 'warn' : 'bad');
      setConnectionStatus(form, data.message || 'Đã test kết nối.', tone);
    } catch (error) {
      setConnectionStatus(form, error.message || String(error), 'bad');
    } finally {
      button.disabled = false;
    }
  }

  async function useConnectionForStudio(button) {
    const connectionId = button.dataset.connectionId || button.closest('[data-connection-id]')?.dataset.connectionId || '';
    if (!connectionId) return;
    const card = button.closest('.connection-card');
    const form = card?.querySelector('.connection-assign-form');
    button.disabled = true;
    setConnectionStatus(form, 'Đang cấu hình Studio...', 'warn');
    try {
      const data = await postConnectionJson('/api/connections/use-for-studio', { id: connectionId });
      setConnectionStatus(form, data.message || 'Đã dùng account/key này cho Studio.', 'good');
    } catch (error) {
      setConnectionStatus(form, error.message || String(error), 'bad');
    } finally {
      button.disabled = false;
    }
  }

  function filterConnections() {
    const query = ($('#connectionSearch')?.value || '').trim().toLowerCase();
    const provider = $('#connectionProviderFilter')?.value || '';
    const kind = $('#connectionKindFilter')?.value || '';
    const status = $('#connectionStatusFilter')?.value || '';
    let visibleCount = 0;
    const groups = $all('.connection-group');
    if (!groups.length) {
      $all('.connections-flat .connection-card').forEach((card) => {
        const matchesQuery = !query || (card.dataset.search || '').includes(query);
        const matchesProvider = !provider || card.dataset.provider === provider;
        const matchesKind = !kind || card.dataset.kind === kind;
        const matchesStatus = !status || card.dataset.status === status;
        const visible = matchesQuery && matchesProvider && matchesKind && matchesStatus;
        card.hidden = !visible;
        if (visible) visibleCount += 1;
      });
      const summary = $('#connectionFilterSummary');
      if (summary) summary.textContent = `${visibleCount} account/key`;
      const empty = $('#connectionFilterEmpty');
      if (empty) empty.hidden = visibleCount !== 0;
      return;
    }
    groups.forEach((group) => {
      let groupVisible = 0;
      group.querySelectorAll('.connection-card').forEach((card) => {
        const matchesQuery = !query || (card.dataset.search || '').includes(query);
        const matchesProvider = !provider || card.dataset.provider === provider;
        const matchesKind = !kind || card.dataset.kind === kind;
        const matchesStatus = !status || card.dataset.status === status;
        const visible = matchesQuery && matchesProvider && matchesKind && matchesStatus;
        card.hidden = !visible;
        if (visible) {
          groupVisible += 1;
          visibleCount += 1;
        }
      });
      group.hidden = groupVisible === 0;
      const count = group.querySelector('[data-group-count]');
      if (count) count.textContent = `${groupVisible} account`;
    });
    const summary = $('#connectionFilterSummary');
    if (summary) summary.textContent = `${visibleCount} account/key`;
    const empty = $('#connectionFilterEmpty');
    if (empty) empty.hidden = visibleCount !== 0;
  }

  const connectYoutube = $('#connectYoutube');
  if (connectYoutube) {
    connectYoutube.addEventListener('click', () => {
      const project = state.uploadProject || state.project;
      if (!project) {
        setUploadStatus('Vui lòng render hoặc chọn project trước.', 'bad');
        return;
      }
      window.open(`/api/social/youtube/connect?project=${encodeURIComponent(project)}`, '_blank', 'noopener,noreferrer');
      setUploadStatus('Đã mở tab Google OAuth. Kết nối xong quay lại tab này để upload.', 'warn');
    });
  }

  const uploadYoutube = $('#uploadYoutube');
  if (uploadYoutube) {
    uploadYoutube.addEventListener('click', async () => {
      const project = state.uploadProject || state.project;
      if (!project) {
        setUploadStatus('Vui lòng render project trước.', 'bad');
        return;
      }
      uploadYoutube.disabled = true;
      setUploadResult('');
      setUploadStatus('Đang upload lên YouTube...', 'warn');
      try {
        const data = await uploadYoutubeVideo(project);
        setUploadStatus('Upload YouTube xong. Mở Studio để kiểm tra và publish nếu cần.', 'good');
        setUploadResult(data.message || 'Uploaded.', 'good', [
          { label: 'Mở video', href: data.url },
          { label: 'Mở Studio', href: data.studio_url },
        ]);
      } catch (error) {
        setUploadStatus(error.message || String(error), 'bad');
      } finally {
        await refreshSocialStatus();
      }
    });
  }

  const uploadFacebook = $('#uploadFacebook');
  if (uploadFacebook) {
    uploadFacebook.addEventListener('click', async () => {
      const project = state.uploadProject || state.project;
      if (!project) {
        setUploadStatus('Vui lòng render project trước.', 'bad');
        return;
      }
      const facebookVideoState = $('#facebookVideoState')?.value || 'DRAFT';
      uploadFacebook.disabled = true;
      setUploadResult('');
      setUploadStatus(`Đang upload Facebook Reels (${facebookVideoState})...`, 'warn');
      try {
        const data = await uploadFacebookReel(project, facebookVideoState);
        state.facebookCommentTargetId = facebookVideoState === 'PUBLISHED' ? (data.source_comment_target_id || data.post_id || data.video_id || '') : '';
        updateFacebookCommentButton();
        setUploadStatus(data.message || 'Upload Facebook Reels xong.', 'good');
        const links = [];
        if (data.url) links.push({ label: 'Mở Reel/Post', href: data.url });
        setUploadResult(data.message || 'Uploaded.', 'good', links);
      } catch (error) {
        setUploadStatus(error.message || String(error), 'bad');
      } finally {
        await refreshSocialStatus();
      }
    });
  }

  const commentFacebookSourceButton = $('#commentFacebookSource');
  if (commentFacebookSourceButton) {
    commentFacebookSourceButton.addEventListener('click', async () => {
      const project = state.uploadProject || state.project;
      if (!project) {
        setUploadStatus('Vui lòng render project trước.', 'bad');
        return;
      }
      if (!state.facebookCommentTargetId) {
        setUploadStatus('Upload Facebook Reels ở trạng thái Publish now trước, rồi bấm Comment source.', 'bad');
        return;
      }
      if (!(($('#facebookSourceComment')?.value || '').trim())) {
        setUploadStatus('Source comment đang trống.', 'bad');
        return;
      }
      commentFacebookSourceButton.disabled = true;
      setUploadStatus('Đang comment source lên Facebook...', 'warn');
      try {
        const data = await commentFacebookSource(project);
        setUploadStatus(data.message || 'Comment source xong.', 'good');
        setUploadResult(data.message || 'Source comment posted.', 'good');
      } catch (error) {
        setUploadStatus(error.message || String(error), 'bad');
      } finally {
        updateFacebookCommentButton();
      }
    });
  }

  const saveFacebookConfigButton = $('#saveFacebookConfig');
  if (saveFacebookConfigButton) {
    saveFacebookConfigButton.addEventListener('click', saveFacebookConfig);
  }

  const openFacebookConfigButton = $('#openFacebookConfig');
  if (openFacebookConfigButton) {
    openFacebookConfigButton.addEventListener('click', openFacebookConfigModal);
  }

  ['#closeFacebookConfig', '#cancelFacebookConfig'].forEach((selector) => {
    const button = $(selector);
    if (button) button.addEventListener('click', closeFacebookConfigModal);
  });

  const facebookConfigModal = $('#facebookConfigModal');
  if (facebookConfigModal) {
    facebookConfigModal.addEventListener('click', (event) => {
      if (event.target === facebookConfigModal) closeFacebookConfigModal();
    });
  }

  const uploadBothPublic = $('#uploadBothPublic');
  if (uploadBothPublic) {
    uploadBothPublic.addEventListener('click', async () => {
      const project = state.uploadProject || state.project;
      if (!project) {
        setUploadStatus('Vui lòng render project trước.', 'bad');
        return;
      }
      if ($('#youtubePrivacy')?.value !== 'public' || $('#facebookVideoState')?.value !== 'PUBLISHED') {
        setUploadStatus('Chọn YouTube Public và Reels Publish now trước.', 'bad');
        updateUploadBothButton();
        return;
      }
      const links = [];
      uploadBothPublic.disabled = true;
      if (uploadYoutube) uploadYoutube.disabled = true;
      if (uploadFacebook) uploadFacebook.disabled = true;
      setUploadResult('');
      setUploadStatus('Đang upload Public lên YouTube...', 'warn');
      try {
        const youtubeData = await uploadYoutubeVideo(project);
        if (youtubeData.url) links.push({ label: 'Mở YouTube', href: youtubeData.url });
        if (youtubeData.studio_url) links.push({ label: 'Mở Studio', href: youtubeData.studio_url });
        setUploadStatus('YouTube xong. Đang upload Facebook Reels...', 'warn');
        const facebookData = await uploadFacebookReel(project, 'PUBLISHED');
        state.facebookCommentTargetId = facebookData.source_comment_target_id || facebookData.post_id || facebookData.video_id || '';
        updateFacebookCommentButton();
        if (facebookData.url) links.push({ label: 'Mở Reel/Post', href: facebookData.url });
        setUploadStatus('Upload Public cả YouTube và Facebook Reels xong. Nếu cần thêm nguồn, bấm Comment source riêng.', 'good');
        setUploadResult('Đã upload public cả hai nền tảng. Comment source chưa được gửi tự động.', 'good', links);
      } catch (error) {
        setUploadStatus(error.message || String(error), 'bad');
        if (links.length) setUploadResult('Một phần đã upload xong trước khi lỗi.', 'warn', links);
      } finally {
        await refreshSocialStatus();
      }
    });
  }

  ['#youtubePrivacy', '#facebookVideoState'].forEach((selector) => {
    const control = $(selector);
    if (control) control.addEventListener('change', updateUploadBothButton);
  });
  const facebookSourceComment = $('#facebookSourceComment');
  if (facebookSourceComment) facebookSourceComment.addEventListener('input', updateFacebookCommentButton);
  $all('[data-copy-target]').forEach((button) => {
    button.addEventListener('click', () => copyFieldValue(
      button.dataset.copyTarget || '',
      button.dataset.copyLabel || 'Nội dung này',
      button,
    ));
  });

  document.addEventListener('click', (event) => {
    if (!event.target.closest('.platform-account-list')) closePlatformAccountMenus();
  });

  document.addEventListener('keydown', (event) => {
    if (event.key === 'Escape') {
      closePlatformAccountMenus();
      closeFacebookConfigModal();
      closeStoryboardModal();
      closeCreateProjectModal();
      closeCreateTemplateModal();
      closeEditTemplateModal();
    }
  });

  window.addEventListener('focus', () => {
    const panel = $('#uploadPanel');
    if (panel && !panel.hidden) refreshSocialStatus();
  });

  const refreshProjects = $('#refreshProjects');
  if (refreshProjects) {
    refreshProjects.addEventListener('click', () => window.location.reload());
  }

  $all('[data-language-select]').forEach((select) => {
    select.addEventListener('change', () => switchLanguage(select.value));
  });
  $all('[data-language-toggle]').forEach((button) => {
    button.addEventListener('click', () => switchLanguage(button.dataset.languageToggle));
  });

  $all('[data-create-project-open]').forEach((button) => {
    button.addEventListener('click', () => openCreateProjectModal(button));
  });

  const createProjectForm = $('#createProjectForm');
  if (createProjectForm) createProjectForm.addEventListener('submit', submitCreateProject);

  ['#closeCreateProject', '#cancelCreateProject'].forEach((selector) => {
    const button = $(selector);
    if (button) button.addEventListener('click', closeCreateProjectModal);
  });

  const createProjectModal = $('#createProjectModal');
  if (createProjectModal) {
    createProjectModal.addEventListener('click', (event) => {
      if (event.target === createProjectModal) closeCreateProjectModal();
    });
  }

  $all('[data-create-template-open]').forEach((button) => {
    button.addEventListener('click', () => openCreateTemplateModal(button));
  });
  $all('[data-import-template-open]').forEach((button) => {
    button.addEventListener('click', openImportTemplateModal);
  });
  $all('.template-edit-btn').forEach((button) => {
    button.addEventListener('click', () => openEditTemplateModal(button.dataset.template));
  });
  $all('.save-project-template-btn').forEach((button) => {
    button.addEventListener('click', () => openSaveProjectTemplateModal(button.dataset.project));
  });

  const createTemplateForm = $('#createTemplateForm');
  if (createTemplateForm) createTemplateForm.addEventListener('submit', submitCreateTemplate);

  const importTemplateForm = $('#importTemplateForm');
  if (importTemplateForm) importTemplateForm.addEventListener('submit', submitImportTemplate);

  const templateArchiveFile = $('#templateArchiveFile');
  if (templateArchiveFile) {
    templateArchiveFile.addEventListener('change', () => {
      const archiveName = $('#templateArchiveName');
      const file = templateArchiveFile.files?.[0];
      if (archiveName) archiveName.textContent = file ? `${file.name} · ${Math.ceil(file.size / 1024)}KB` : 'Chưa chọn file.';
    });
  }

  const editTemplateForm = $('#editTemplateForm');
  if (editTemplateForm) editTemplateForm.addEventListener('submit', submitEditTemplate);
  const templateEditorForm = $('#templateEditorForm');
  if (templateEditorForm) templateEditorForm.addEventListener('submit', submitEditTemplate);
  const saveProjectTemplateForm = $('#saveProjectTemplateForm');
  if (saveProjectTemplateForm) saveProjectTemplateForm.addEventListener('submit', submitSaveProjectTemplate);

  ['#closeCreateTemplate', '#cancelCreateTemplate'].forEach((selector) => {
    const button = $(selector);
    if (button) button.addEventListener('click', closeCreateTemplateModal);
  });
  ['#closeImportTemplate', '#cancelImportTemplate'].forEach((selector) => {
    const button = $(selector);
    if (button) button.addEventListener('click', closeImportTemplateModal);
  });
  ['#closeEditTemplate', '#cancelEditTemplate'].forEach((selector) => {
    const button = $(selector);
    if (button) button.addEventListener('click', closeEditTemplateModal);
  });
  ['#closeSaveProjectTemplate', '#cancelSaveProjectTemplate'].forEach((selector) => {
    const button = $(selector);
    if (button) button.addEventListener('click', closeSaveProjectTemplateModal);
  });

  const createTemplateModal = $('#createTemplateModal');
  if (createTemplateModal) {
    createTemplateModal.addEventListener('click', (event) => {
      if (event.target === createTemplateModal) closeCreateTemplateModal();
    });
  }
  const importTemplateModal = $('#importTemplateModal');
  if (importTemplateModal) {
    importTemplateModal.addEventListener('click', (event) => {
      if (event.target === importTemplateModal) closeImportTemplateModal();
    });
  }
  const editTemplateModal = $('#editTemplateModal');
  if (editTemplateModal) {
    editTemplateModal.addEventListener('click', (event) => {
      if (event.target === editTemplateModal) closeEditTemplateModal();
    });
  }
  const saveProjectTemplateModal = $('#saveProjectTemplateModal');
  if (saveProjectTemplateModal) {
    saveProjectTemplateModal.addEventListener('click', (event) => {
      if (event.target === saveProjectTemplateModal) closeSaveProjectTemplateModal();
    });
  }

  const connectionCreateForm = $('.connection-create-form');
  if (connectionCreateForm) {
    connectionCreateForm.addEventListener('submit', submitConnectionCreate);
  }
  const openCreateConnection = $('#openCreateConnection');
  if (openCreateConnection) openCreateConnection.addEventListener('click', openCreateConnectionModal);
  const cancelCreateConnection = $('#cancelCreateConnection');
  if (cancelCreateConnection) cancelCreateConnection.addEventListener('click', closeCreateConnectionModal);
  initProjectPickers();
  const testConnectionDraft = $('#testConnectionDraft');
  if (testConnectionDraft && connectionCreateForm) {
    testConnectionDraft.addEventListener('click', () => testConnectionForm(connectionCreateForm, testConnectionDraft));
  }
  const editConnectionForm = $('#editConnectionForm');
  if (editConnectionForm) editConnectionForm.addEventListener('submit', submitConnectionEdit);
  const testEditConnection = $('#testEditConnection');
  if (testEditConnection && editConnectionForm) {
    testEditConnection.addEventListener('click', () => testConnectionForm(editConnectionForm, testEditConnection));
  }
  ['#closeEditConnection', '#cancelEditConnection'].forEach((selector) => {
    const button = $(selector);
    if (button) button.addEventListener('click', closeEditConnectionModal);
  });
  const editConnectionModal = $('#editConnectionModal');
  if (editConnectionModal) {
    editConnectionModal.addEventListener('click', (event) => {
      if (event.target === editConnectionModal) closeEditConnectionModal();
    });
  }
  $all('.connection-assign-form').forEach((form) => {
    form.addEventListener('submit', submitConnectionAssign);
  });
  $all('.connection-edit-btn').forEach((button) => {
    button.addEventListener('click', () => openEditConnectionModal(button));
  });
  $all('.connection-studio-btn').forEach((button) => {
    button.addEventListener('click', () => useConnectionForStudio(button));
  });
  $all('.connection-delete-btn').forEach((button) => {
    button.addEventListener('click', () => deleteConnection(button));
  });
  $all('.connection-test-btn').forEach((button) => {
    button.addEventListener('click', () => testConnection(button));
  });
  ['#connectionSearch', '#connectionProviderFilter', '#connectionKindFilter', '#connectionStatusFilter'].forEach((selector) => {
    const control = $(selector);
    if (control) control.addEventListener('input', filterConnections);
    if (control) control.addEventListener('change', filterConnections);
  });
  if ($('#connectionSearch')) filterConnections();
  document.addEventListener('click', (event) => {
    if (!event.target.closest('[data-project-picker]')) closeProjectPickers();
  });
  document.addEventListener('keydown', (event) => {
    if (event.key === 'Escape') {
      closeProjectPickers();
      closeCreateConnectionModal();
    }
  });

  $all('[data-source-root-select]').forEach((button) => {
    button.addEventListener('click', selectSourceRootFromFinder);
  });

  const storedTheme = localStorage.getItem('viro-theme');
  applyTheme(storedTheme || 'light');
  $all('[data-theme-toggle]').forEach((button) => {
    button.addEventListener('click', toggleTheme);
  });

  $all('.project-row[data-project]').forEach((row) => {
    row.addEventListener('click', (event) => {
      if (event.target.closest('a, button')) return;
      setSelectedProject(row.dataset.project);
    });
  });

  const startButton = $('#startRender');
  if (startButton) startButton.addEventListener('click', startRender);
  const previewVoiceButton = $('#previewVoice');
  if (previewVoiceButton) previewVoiceButton.addEventListener('click', previewVoice);

  const initialFromUrl = new URLSearchParams(window.location.search).get('project');
  const initialProject = window.__INITIAL_PROJECT__ || initialFromUrl || projects[0]?.name;
  if (initialProject) setSelectedProject(initialProject, false);
  const initialTemplateFromUrl = new URLSearchParams(window.location.search).get('template');
  const initialTemplate = initialTemplateFromUrl
    || localStorage.getItem('viro-selected-template')
    || templates.find((template) => template.name === 'viro-slide-starter')?.name
    || templates[0]?.name;
  if (initialTemplate) setSelectedTemplate(initialTemplate, false);
  const initialDuration = localStorage.getItem('viro-video-duration')
    || document.querySelector('[data-duration].active')?.dataset.duration
    || 'short';
  setVideoDuration(initialDuration);
  syncStudioModeActions();
  syncSpeedPresets();
})();
