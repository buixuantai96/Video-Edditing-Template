# Viro Template

Local-first tooling for turning short scripts, articles, and ideas into
vertical video projects. Viro Template ships with a browser-based studio,
HTML/CSS/JS video templates, TTS integrations, render helpers, and publishing
utilities for YouTube and Facebook workflows.

> This repository is currently prepared in an open-source style, but no
> `LICENSE` file is included yet. Add a license before publishing it as a
> public open-source project.

## Features

- Browser studio for creating projects from reusable video templates.
- AI script, article-to-video, and manual script entry modes.
- HTML/CSS/JS template runtime for 9:16 slide-style videos.
- Edge TTS draft rendering.
- ElevenLabs upload and API-based voiceover rendering.
- Local render pipeline that exports `output/final_video.mp4`.
- Template management, project management, and preview pages.
- YouTube and Facebook publishing helpers with local configuration.
- Secret-safe defaults: real config files are ignored, examples are committed.

## Requirements

- Python 3.12 or newer.
- FFmpeg available on `PATH` for video/audio rendering.
- Playwright browser dependencies for preview and render capture.
- Windows PowerShell or a POSIX shell, depending on your platform.

Python dependencies are listed in `requirements.txt`:

```txt
edge-tts
elevenlabs
playwright
faster-whisper
```

## Quick Start

### Windows

```powershell
powershell -ExecutionPolicy Bypass -File .\setup_and_run_windows.ps1
```

### macOS / Linux

```bash
./setup_and_run.sh
```

After setup, open:

```text
http://localhost:8765
```

The main studio pages are:

- `http://localhost:8765/studio?inputMode=ai`
- `http://localhost:8765/templates`
- `http://localhost:8765/projects`
- `http://localhost:8765/upload`

## Manual Setup

If you do not want to use the setup script:

```bash
python -m venv .venv
python -m pip install -r requirements.txt
python -m playwright install chromium
python web_server.py --host 127.0.0.1 --port 8765
```

On Windows, use:

```powershell
py -3.12 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
.\.venv\Scripts\python.exe -m playwright install chromium
.\.venv\Scripts\python.exe web_server.py --host 127.0.0.1 --port 8765
```

## Project Workflow

1. Open Studio.
2. Choose a template.
3. Create or select a project.
4. Add script or source content.
5. Preview storyboard and media.
6. Render audio and video.
7. Open or publish the generated MP4.

Generated projects live in:

```text
slide/<project-name>/
```

Generated video output is ignored by Git:

```text
slide/<project-name>/output/final_video.mp4
```

## Rendering

Render from the browser UI, or use scripts directly.

Edge TTS:

```bash
python render_edgetts.py slide/<project-name> --speed 1.1 --voice vi-VN-HoaiMyNeural
```

ElevenLabs audio upload:

```bash
python render_elevenlabs.py slide/<project-name> path/to/voiceover.mp3 --speed 1.1
```

ElevenLabs API TTS:

```bash
python render_elevenlabs_tts.py slide/<project-name> --speed 1.1
```

Validate a project layout:

```bash
python validate_slide.py slide/<project-name>
```

## Repository Structure

```text
config/          Example config files only.
slide/           Local generated projects. Kept empty in Git.
social_upload/   YouTube and Facebook upload helpers.
template/        Reusable video templates.
tests/           Unit tests.
tts/             TTS engine integrations.
web/             Browser assets and Studio JavaScript.
web_server.py    Local web app and API server.
```

## Configuration

Copy example config files before adding local credentials:

```bash
cp config/tts.example.json config/tts.json
cp config/social-upload.example.json config/social-upload.json
```

On Windows PowerShell:

```powershell
Copy-Item config\tts.example.json config\tts.json
Copy-Item config\social-upload.example.json config\social-upload.json
```

The real config files are intentionally ignored by Git:

- `config/tts.json`
- `config/social-upload.json`
- `config/connections.json`
- `.env`

Never commit API keys, OAuth tokens, page access tokens, generated voice audio,
or rendered video output.

## Testing

Syntax checks:

```bash
python -m py_compile web_server.py auto_render.py generate_tts.py render_edgetts.py render_elevenlabs.py render_elevenlabs_tts.py validate_slide.py
node --check web/render_page.js
```

Unit tests, after installing `pytest`:

```bash
python -m pip install pytest
python -m pytest
```

## Security

- Keep credentials in ignored local config files or environment variables.
- Review staged files before pushing:

```bash
git status --short
git diff --cached --name-only
```

- Rotate any credential that was ever committed or shared accidentally.
- Do not upload generated output that contains private source material unless it
  is intended for public release.

## Contributing

1. Create a branch from `main`.
2. Keep changes focused and scoped.
3. Run syntax checks and relevant tests.
4. Do not commit secrets, generated output, cache files, or local artifacts.
5. Open a pull request with a concise summary and verification notes.

## License

No license has been declared yet. Add a `LICENSE` file before distributing this
repository as public open-source software.

