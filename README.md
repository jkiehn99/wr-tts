# Book TTS Xiaoqiu Web

FastAPI Web UI for long-form Microsoft Xiaoqiu TTS audiobook generation.

## Features

- Upload `txt`, `md`, `epub`, `pdf`, `docx`, or `zip` containing text files.
- Split long text into safe chunks, default `1800` Chinese characters.
- Generate chunks through `/opt/data/scripts/libretts-edge.mjs` with `zh-CN-XiaoqiuNeural`.
- Merge chunks with `ffmpeg` into a downloadable MP3.
- Background task with progress polling and resumable chunk files inside each job folder.

## Run

```bash
cd /opt/data/apps/book-tts-web
chmod +x run.sh
./run.sh
```

Default URL:

```text
http://127.0.0.1:9120
```

## Environment

```bash
BOOK_TTS_WEB_HOME=/opt/data/apps/book-tts-web
XIAOQIU_TTS_SCRIPT=/opt/data/scripts/libretts-edge.mjs
XIAOQIU_TTS_VOICE=zh-CN-XiaoqiuNeural
BOOK_TTS_MAX_CHARS=1800
PORT=9120
```

## Notes

For best stability, keep chunk size between `1500` and `1800` Chinese characters. The Xiaoqiu script has been tested as stable around `1800–2000` characters and unreliable above roughly `2400` characters in this environment.
