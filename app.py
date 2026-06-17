#!/usr/bin/env python3
"""FastAPI web UI for long-form Xiaoqiu TTS audiobook generation."""
from __future__ import annotations

import asyncio
import html
import json
import os
import re
import shutil
import subprocess
import time
import uuid
import urllib.error
import urllib.request
import zipfile
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from fastapi import BackgroundTasks, FastAPI, File, Form, HTTPException, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, PlainTextResponse

APP_ROOT = Path(os.environ.get("BOOK_TTS_WEB_HOME", "/opt/data/apps/book-tts-web"))
JOBS_DIR = APP_ROOT / "jobs"
UPLOADS_DIR = Path(os.environ.get("BOOK_TTS_UPLOADS_DIR", str(APP_ROOT / "runtime_uploads")))
XIAOQIU_SCRIPT = Path(os.environ.get("XIAOQIU_TTS_SCRIPT", "/opt/data/scripts/libretts-edge.mjs"))
DEFAULT_VOICE = os.environ.get("XIAOQIU_TTS_VOICE", "zh-CN-XiaoqiuNeural")
MAX_CHARS_PER_CHUNK = int(os.environ.get("BOOK_TTS_MAX_CHARS", "1800"))
PREVIEW_TTL_SECONDS = int(os.environ.get("BOOK_TTS_PREVIEW_TTL_SECONDS", str(30 * 60)))
SETTINGS_FILE = Path(os.environ.get("BOOK_TTS_SETTINGS_FILE", str(APP_ROOT / "runtime_settings.json")))
DEFAULT_TTS_SETTINGS = {
    "provider": "local",
    "proxy_type": "openai",
    "proxy_url": "",
    "proxy_api_key": "",
    "proxy_model": "tts-1",
    "proxy_voice_id": "",
    "proxy_group_id": "",
    "custom_headers": "",
    "custom_body_template": "",
    "custom_audio_field": "",
}
FALLBACK_VOICES = [
    {"name": "zh-CN-XiaoqiuNeural", "display": "晓秋 - 中文普通话", "locale": "zh-CN", "locale_label": "中文普通话", "gender": "女声"},
    {"name": "zh-CN-XiaoxiaoNeural", "display": "晓晓 - 中文普通话", "locale": "zh-CN", "locale_label": "中文普通话", "gender": "女声"},
    {"name": "zh-CN-YunxiNeural", "display": "云希 - 中文普通话", "locale": "zh-CN", "locale_label": "中文普通话", "gender": "男声"},
    {"name": "zh-CN-YunjianNeural", "display": "云健 - 中文普通话", "locale": "zh-CN", "locale_label": "中文普通话", "gender": "男声"},
    {"name": "zh-CN-XiaoyiNeural", "display": "晓伊 - 中文普通话", "locale": "zh-CN", "locale_label": "中文普通话", "gender": "女声"},
]
ALLOWED_VOICE_LOCALES = {"zh-CN", "zh-HK", "en-US", "en-GB"}
VOICE_DISPLAY_NAMES = {
    "zh-CN-XiaoqiuNeural": "晓秋",
    "zh-CN-XiaoxiaoNeural": "晓晓",
    "zh-CN-XiaoyiNeural": "晓伊",
    "zh-CN-XiaobeiNeural": "晓北",
    "zh-CN-XiaoniNeural": "晓妮",
    "zh-CN-XiaomoNeural": "晓墨",
    "zh-CN-XiaoxuanNeural": "晓萱",
    "zh-CN-XiaohanNeural": "晓涵",
    "zh-CN-YunxiNeural": "云希",
    "zh-CN-YunjianNeural": "云健",
    "zh-CN-YunyangNeural": "云扬",
    "zh-CN-YunxiaNeural": "云夏",
}

app = FastAPI(title="Book TTS Xiaoqiu", version="1.0.0")
JOBS_DIR.mkdir(parents=True, exist_ok=True)
UPLOADS_DIR.mkdir(parents=True, exist_ok=True)


@dataclass
class JobState:
    id: str
    filename: str
    status: str = "queued"
    message: str = "等待开始"
    created_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)
    total_chunks: int = 0
    done_chunks: int = 0
    chars: int = 0
    voice: str = DEFAULT_VOICE
    chunk_size: int = MAX_CHARS_PER_CHUNK
    rate: int = 0
    volume: int = 50
    tts_provider: str = "local"
    proxy_type: str = "openai"
    proxy_url: str = ""
    proxy_model: str = "tts-1"
    proxy_voice_id: str = ""
    proxy_group_id: str = ""
    output_file: str | None = None
    preview_file: str | None = None
    error: str | None = None

    @property
    def progress(self) -> float:
        if self.total_chunks <= 0:
            return 0.0
        return round(self.done_chunks / self.total_chunks, 4)


_jobs: dict[str, JobState] = {}
_locks: dict[str, asyncio.Lock] = {}


def job_dir(job_id: str) -> Path:
    path = JOBS_DIR / job_id
    path.mkdir(parents=True, exist_ok=True)
    return path


def save_state(state: JobState) -> None:
    state.updated_at = time.time()
    job_dir(state.id).joinpath("state.json").write_text(
        json.dumps(asdict(state) | {"progress": state.progress}, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def load_state(job_id: str) -> JobState | None:
    if job_id in _jobs:
        return _jobs[job_id]
    path = JOBS_DIR / job_id / "state.json"
    if not path.exists():
        return None
    data = json.loads(path.read_text(encoding="utf-8"))
    data.pop("progress", None)
    state = JobState(**data)
    _jobs[job_id] = state
    return state


def text_file(job_id: str) -> Path:
    return job_dir(job_id) / "source.txt"


def write_job_text(job_id: str, text: str) -> None:
    text_file(job_id).write_text(text, encoding="utf-8")


def read_job_text(job_id: str) -> str:
    path = text_file(job_id)
    if not path.exists():
        raise RuntimeError("任务文本不存在，请重新上传")
    return path.read_text(encoding="utf-8")


def load_settings() -> dict[str, str]:
    settings = DEFAULT_TTS_SETTINGS.copy()
    if SETTINGS_FILE.exists():
        try:
            data = json.loads(SETTINGS_FILE.read_text(encoding="utf-8"))
            for key in settings:
                if isinstance(data.get(key), str):
                    settings[key] = data[key]
        except Exception:
            pass
    return settings


def save_settings(settings: dict[str, str]) -> None:
    safe = DEFAULT_TTS_SETTINGS.copy()
    for key in safe:
        value = settings.get(key, "")
        safe[key] = value.strip() if isinstance(value, str) else ""
    if safe["provider"] not in {"local", "proxy"}:
        safe["provider"] = "local"
    SETTINGS_FILE.write_text(json.dumps(safe, ensure_ascii=False, indent=2), encoding="utf-8")


def public_settings() -> dict[str, str]:
    settings = load_settings()
    return {k: v for k, v in settings.items() if k != "proxy_api_key"}


def voice_locale_label(locale: str) -> str:
    labels = {
        "zh-CN": "中文普通话",
        "zh-HK": "中文香港",
        "en-US": "英语美国",
        "en-GB": "英语英国",
    }
    return labels.get(locale, locale or "未知语言")


def voice_gender_label(gender: str) -> str:
    return {"Female": "女声", "Male": "男声"}.get(gender, gender or "未知")


def voice_display_name(name: str, locale: str) -> str:
    short = VOICE_DISPLAY_NAMES.get(name)
    if not short:
        short = name.replace(f"{locale}-", "").replace("Neural", "")
    return f"{short} - {voice_locale_label(locale)}"


def list_local_voices() -> list[dict[str, str]]:
    try:
        import edge_tts
        voices = asyncio.run(edge_tts.list_voices())
        result = []
        for item in voices:
            name = item.get("ShortName") or item.get("Name") or ""
            if not name:
                continue
            locale = item.get("Locale") or ""
            if locale not in ALLOWED_VOICE_LOCALES:
                continue
            gender = item.get("Gender") or ""
            result.append({
                "name": name,
                "display": voice_display_name(name, locale),
                "locale": locale,
                "locale_label": voice_locale_label(locale),
                "gender": voice_gender_label(gender),
            })
        names = {row["name"] for row in result}
        for voice in FALLBACK_VOICES:
            if voice["name"] not in names:
                item = voice.copy()
                item.setdefault("locale", "zh-CN")
                item.setdefault("locale_label", voice_locale_label("zh-CN"))
                item.setdefault("gender", "")
                result.append(item)

        def sort_key(row: dict[str, str]) -> tuple[int, int, str]:
            locale_order = {"zh-CN": 0, "zh-HK": 1, "en-US": 2, "en-GB": 3}
            return (0 if row["name"] == DEFAULT_VOICE else 1, locale_order.get(row.get("locale", ""), 9), row["display"])

        result.sort(key=sort_key)
        return result or FALLBACK_VOICES
    except Exception:
        return FALLBACK_VOICES


def cleanup_job(job_id: str) -> None:
    _jobs.pop(job_id, None)
    _locks.pop(job_id, None)
    path = JOBS_DIR / job_id
    if path.exists():
        shutil.rmtree(path, ignore_errors=True)


async def cleanup_preview_later(job_id: str, ttl_seconds: int = PREVIEW_TTL_SECONDS) -> None:
    await asyncio.sleep(max(1, ttl_seconds))
    state_path = JOBS_DIR / job_id / "state.json"
    if not state_path.exists():
        return
    state = load_state(job_id)
    if not state:
        return
    if state.status in {"preview_done", "failed"} and not state.output_file:
        cleanup_job(job_id)


def clean_text(text: str) -> str:
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    text = re.sub(r"[\t\u3000]+", " ", text)
    text = re.sub(r"\n{4,}", "\n\n\n", text)
    return text.strip()


def split_text(text: str, max_chars: int) -> list[str]:
    paragraphs = [p.strip() for p in re.split(r"\n\s*\n", text) if p.strip()]
    chunks: list[str] = []
    current = ""
    sentence_pattern = re.compile(r"(?<=[。！？!?；;\.])")

    def push(part: str) -> None:
        nonlocal current
        part = part.strip()
        if not part:
            return
        if not current:
            current = part
        elif len(current) + 2 + len(part) <= max_chars:
            current += "\n\n" + part
        else:
            chunks.append(current.strip())
            current = part

    for paragraph in paragraphs:
        if len(paragraph) <= max_chars:
            push(paragraph)
            continue
        sentences = [s.strip() for s in sentence_pattern.split(paragraph) if s.strip()]
        buffer = ""
        for sentence in sentences:
            if len(sentence) > max_chars:
                if buffer:
                    push(buffer)
                    buffer = ""
                for i in range(0, len(sentence), max_chars):
                    push(sentence[i : i + max_chars])
            elif not buffer:
                buffer = sentence
            elif len(buffer) + len(sentence) <= max_chars:
                buffer += sentence
            else:
                push(buffer)
                buffer = sentence
        if buffer:
            push(buffer)
    if current:
        chunks.append(current.strip())
    return chunks


def extract_epub_text(path: Path) -> str:
    try:
        from bs4 import BeautifulSoup
        from ebooklib import ITEM_DOCUMENT, epub
    except Exception as exc:
        raise HTTPException(400, f"EPUB 解析依赖缺失：{exc}") from exc
    book = epub.read_epub(str(path))
    parts: list[str] = []
    for item in book.get_items_of_type(ITEM_DOCUMENT):
        soup = BeautifulSoup(item.get_content(), "html.parser")
        text = soup.get_text("\n")
        if text.strip():
            parts.append(text)
    return "\n\n".join(parts)


def extract_pdf_text(path: Path) -> str:
    try:
        from pypdf import PdfReader
    except Exception as exc:
        raise HTTPException(400, f"PDF 解析依赖缺失：{exc}") from exc
    reader = PdfReader(str(path))
    return "\n\n".join(page.extract_text() or "" for page in reader.pages)


def extract_html_text(path: Path) -> str:
    try:
        from bs4 import BeautifulSoup
    except Exception as exc:
        raise HTTPException(400, f"HTML 解析依赖缺失：{exc}") from exc
    soup = BeautifulSoup(path.read_text(encoding="utf-8", errors="ignore"), "html.parser")
    return soup.get_text("\n")


def extract_mobi_text(path: Path) -> str:
    try:
        import mobi
    except Exception as exc:
        raise HTTPException(400, f"MOBI 解析依赖缺失：{exc}") from exc
    try:
        temp_dir, extracted = mobi.extract(str(path))
    except Exception as exc:
        raise HTTPException(400, f"MOBI 解包失败：{exc}") from exc
    try:
        extracted_path = Path(extracted)
        suffix = extracted_path.suffix.lower()
        if suffix == ".epub":
            return extract_epub_text(extracted_path)
        if suffix == ".pdf":
            return extract_pdf_text(extracted_path)
        if suffix in {".html", ".htm"}:
            return extract_html_text(extracted_path)
        raise HTTPException(400, f"MOBI 解包后格式暂不支持：{suffix or '无扩展名'}")
    finally:
        shutil.rmtree(temp_dir, ignore_errors=True)


def extract_text_from_upload(path: Path, original_name: str) -> str:
    suffix = Path(original_name).suffix.lower()
    if suffix in {".txt", ".md", ".csv", ".srt", ".vtt"}:
        return path.read_text(encoding="utf-8", errors="ignore")
    if suffix == ".epub":
        return extract_epub_text(path)
    if suffix == ".pdf":
        return extract_pdf_text(path)
    if suffix in {".mobi", ".azw3", ".azw"}:
        return extract_mobi_text(path)
    if suffix == ".docx":
        try:
            import docx
        except Exception as exc:
            raise HTTPException(400, f"DOCX 解析依赖缺失：{exc}") from exc
        doc = docx.Document(str(path))
        return "\n\n".join(p.text for p in doc.paragraphs if p.text.strip())
    if suffix == ".zip":
        texts: list[str] = []
        with zipfile.ZipFile(path) as zf:
            for name in sorted(zf.namelist()):
                if name.lower().endswith((".txt", ".md")):
                    texts.append(zf.read(name).decode("utf-8", errors="ignore"))
        if not texts:
            raise HTTPException(400, "ZIP 内没有 .txt/.md 文件")
        return "\n\n".join(texts)
    raise HTTPException(400, f"暂不支持格式：{suffix or '无扩展名'}。支持 txt/md/epub/pdf/docx/mobi/azw3/zip。")


def run_local_tts(text: str, out_file: Path, voice: str, rate: int, volume: int) -> None:
    if not XIAOQIU_SCRIPT.exists():
        raise RuntimeError(f"小秋脚本不存在：{XIAOQIU_SCRIPT}")
    cmd = [
        "node",
        str(XIAOQIU_SCRIPT),
        "--text",
        text,
        "--voice",
        voice,
        "--out",
        str(out_file),
        "--rate",
        str(rate),
        "--volume",
        str(volume),
    ]
    result = subprocess.run(cmd, capture_output=True, text=True, timeout=300)
    if result.returncode != 0 or not out_file.exists() or out_file.stat().st_size <= 0:
        raise RuntimeError((result.stderr or result.stdout or "TTS failed").strip()[:2000])


def parse_json_object(raw: str, label: str) -> dict[str, Any]:
    if not raw.strip():
        return {}
    try:
        data = json.loads(raw)
    except Exception as exc:
        raise RuntimeError(f"{label} 不是合法 JSON：{exc}") from exc
    if not isinstance(data, dict):
        raise RuntimeError(f"{label} 必须是 JSON 对象")
    return data


def render_template(value: str, variables: dict[str, str]) -> str:
    for key, replacement in variables.items():
        value = value.replace("{{" + key + "}}", replacement)
    return value


def extract_audio_from_response(data: bytes, audio_field: str) -> bytes:
    if not audio_field:
        return data
    import base64
    try:
        payload = json.loads(data.decode("utf-8"))
    except Exception as exc:
        raise RuntimeError(f"代理返回不是 JSON，无法读取字段 {audio_field}: {exc}") from exc
    value: Any = payload
    for part in audio_field.split("."):
        if isinstance(value, dict):
            value = value.get(part)
        else:
            value = None
        if value is None:
            raise RuntimeError(f"代理返回 JSON 中找不到音频字段：{audio_field}")
    if not isinstance(value, str):
        raise RuntimeError(f"音频字段 {audio_field} 不是字符串")
    if value.startswith("data:"):
        value = value.split(",", 1)[-1]
    return base64.b64decode(value)


def build_proxy_request(text: str, voice: str, settings: dict[str, str]) -> tuple[str, dict[str, str], bytes, str]:
    proxy_type = settings.get("proxy_type", "openai") or "openai"
    base_url = settings.get("proxy_url", "").strip().rstrip("/")
    if not base_url:
        raise RuntimeError("代理 TTS URL 为空，请先在设置里填写")
    if "api.elevenlabs.io" in base_url and proxy_type != "elevenlabs":
        proxy_type = "elevenlabs"
    api_key = settings.get("proxy_api_key", "").strip()
    if proxy_type == "elevenlabs" and not api_key:
        api_key = os.environ.get("ELEVENLABS_API_KEY", "").strip()
    model = settings.get("proxy_model", "tts-1").strip() or "tts-1"
    if proxy_type == "elevenlabs" and model == "tts-1":
        model = "eleven_multilingual_v2"
    voice_id = (settings.get("proxy_voice_id", "").strip() or voice)
    if proxy_type == "elevenlabs" and voice_id.startswith("zh-"):
        voice_id = "hpp4J3VqNfWAUOO0d1Us"
    group_id = settings.get("proxy_group_id", "").strip()
    headers: dict[str, str] = {"Content-Type": "application/json"}
    if proxy_type == "elevenlabs":
        if base_url == "https://api.elevenlabs.io":
            base_url = "https://api.elevenlabs.io/v1"
        url = base_url
        if "/text-to-speech/" not in url:
            url = f"{base_url}/text-to-speech/{voice_id}"
        if api_key:
            headers["xi-api-key"] = api_key
        payload = {"text": text, "model_id": model or "eleven_multilingual_v2"}
        return url, headers, json.dumps(payload).encode("utf-8"), ""
    if proxy_type == "minimax":
        url = base_url if base_url.endswith("/t2a_v2") else f"{base_url}/t2a_v2"
        if api_key:
            headers["Authorization"] = f"Bearer {api_key}"
        payload: dict[str, Any] = {
            "model": model or "speech-2.6-turbo",
            "text": text,
            "stream": False,
            "audio_setting": {"sample_rate": 32000, "bitrate": 128000, "format": "mp3"},
            "voice_setting": {"voice_id": voice_id, "speed": 1.0, "vol": 1.0, "pitch": 0},
        }
        if group_id:
            payload["group_id"] = group_id
        return url, headers, json.dumps(payload).encode("utf-8"), "data.audio"
    if proxy_type == "google":
        url = base_url if base_url.endswith("/text:synthesize") else f"{base_url}/text:synthesize"
        if api_key:
            url += ("&" if "?" in url else "?") + "key=" + api_key
        language_code = "-".join((voice_id or voice).split("-")[:2]) or "zh-CN"
        payload = {
            "input": {"text": text},
            "voice": {"languageCode": language_code, "name": voice_id or voice},
            "audioConfig": {"audioEncoding": "MP3"},
        }
        return url, headers, json.dumps(payload).encode("utf-8"), "audioContent"
    if proxy_type == "custom_json":
        variables = {"text": text, "voice": voice, "voice_id": voice_id, "model": model, "group_id": group_id}
        url = render_template(base_url, variables)
        headers.update({str(k): render_template(str(v), variables) for k, v in parse_json_object(settings.get("custom_headers", ""), "自定义请求头").items()})
        if api_key and "Authorization" not in headers:
            headers["Authorization"] = f"Bearer {api_key}"
        body_template = settings.get("custom_body_template", "").strip() or '{"model":"{{model}}","voice":"{{voice}}","input":"{{text}}"}'
        return url, headers, render_template(body_template, variables).encode("utf-8"), settings.get("custom_audio_field", "").strip()
    url = base_url if base_url.endswith("/audio/speech") else f"{base_url}/audio/speech"
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    payload = {"model": model, "voice": voice_id, "input": text, "response_format": "mp3"}
    return url, headers, json.dumps(payload).encode("utf-8"), ""


def run_proxy_tts(text: str, out_file: Path, voice: str, settings: dict[str, str]) -> None:
    url, headers, payload, audio_field = build_proxy_request(text, voice, settings)
    request = urllib.request.Request(url, data=payload, headers=headers, method="POST")
    try:
        with urllib.request.urlopen(request, timeout=300) as response:
            data = response.read()
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="ignore")
        raise RuntimeError(f"代理 TTS 失败 {exc.code}: {body[:1000]}") from exc
    if not data:
        raise RuntimeError("代理 TTS 返回空音频")
    audio = extract_audio_from_response(data, audio_field)
    out_file.parent.mkdir(parents=True, exist_ok=True)
    out_file.write_bytes(audio)


def run_tts(text: str, out_file: Path, state: JobState) -> None:
    if state.tts_provider == "proxy":
        settings = load_settings()
        settings["proxy_type"] = state.proxy_type or settings.get("proxy_type", "openai")
        settings["proxy_url"] = state.proxy_url or settings.get("proxy_url", "")
        settings["proxy_model"] = state.proxy_model or settings.get("proxy_model", "tts-1")
        settings["proxy_voice_id"] = state.proxy_voice_id or settings.get("proxy_voice_id", "")
        settings["proxy_group_id"] = state.proxy_group_id or settings.get("proxy_group_id", "")
        run_proxy_tts(text, out_file, state.voice, settings)
        return
    run_local_tts(text, out_file, state.voice, state.rate, state.volume)


def concat_audio(files: list[Path], output_file: Path) -> None:
    list_file = output_file.with_suffix(".concat.txt")
    list_file.write_text("".join(f"file '{p.as_posix()}'\n" for p in files), encoding="utf-8")
    cmd = [
        "ffmpeg",
        "-y",
        "-f",
        "concat",
        "-safe",
        "0",
        "-i",
        str(list_file),
        "-c",
        "copy",
        str(output_file),
    ]
    result = subprocess.run(cmd, capture_output=True, text=True, timeout=600)
    if result.returncode != 0 or not output_file.exists():
        raise RuntimeError((result.stderr or result.stdout or "ffmpeg concat failed")[-2000:])


async def process_preview(job_id: str, text: str, preview_chars: int) -> None:
    lock = _locks.setdefault(job_id, asyncio.Lock())
    async with lock:
        state = load_state(job_id)
        if not state:
            return
        try:
            state.status = "preview_running"
            state.message = "正在生成试听片段"
            state.chars = len(text)
            state.total_chunks = 1
            state.done_chunks = 0
            save_state(state)
            sample = text[: max(200, min(preview_chars, 1200))].strip()
            if not sample:
                raise RuntimeError("未提取到可朗读文本")
            output = job_dir(job_id) / f"{Path(state.filename).stem or 'book'}_试听_{state.voice}.mp3"
            await asyncio.to_thread(run_tts, sample, output, state)
            state.preview_file = str(output)
            state.status = "preview_done"
            state.done_chunks = 1
            state.message = "试听完成，满意后可生成整本"
            save_state(state)
            asyncio.create_task(cleanup_preview_later(job_id))
        except Exception as exc:
            state.status = "failed"
            state.error = str(exc)
            state.message = "试听失败"
            save_state(state)
            asyncio.create_task(cleanup_preview_later(job_id))


async def process_job(job_id: str, text: str) -> None:
    lock = _locks.setdefault(job_id, asyncio.Lock())
    async with lock:
        state = load_state(job_id)
        if not state:
            return
        try:
            state.status = "running"
            state.message = "正在清洗和分块"
            save_state(state)
            chunks = split_text(text, max(200, min(state.chunk_size, 2200)))
            if not chunks:
                raise RuntimeError("未提取到可朗读文本")
            state.total_chunks = len(chunks)
            state.chars = len(text)
            save_state(state)
            chunks_dir = job_dir(job_id) / "chunks"
            chunks_dir.mkdir(exist_ok=True)
            audio_files: list[Path] = []
            for index, chunk in enumerate(chunks, start=1):
                out_file = chunks_dir / f"chunk_{index:05d}.mp3"
                audio_files.append(out_file)
                if out_file.exists() and out_file.stat().st_size > 0:
                    state.done_chunks = index
                    state.message = f"跳过已完成分段 {index}/{len(chunks)}"
                    save_state(state)
                    continue
                state.message = f"正在生成分段 {index}/{len(chunks)}"
                save_state(state)
                await asyncio.to_thread(run_tts, chunk, out_file, state)
                state.done_chunks = index
                save_state(state)
            state.message = "正在合并音频"
            save_state(state)
            output = job_dir(job_id) / f"{Path(state.filename).stem or 'book'}_{state.voice}.mp3"
            await asyncio.to_thread(concat_audio, audio_files, output)
            state.output_file = str(output)
            state.status = "done"
            state.message = "完成，可下载"
            save_state(state)
        except Exception as exc:
            state.status = "failed"
            state.error = str(exc)
            state.message = "失败"
            save_state(state)


@app.get("/", response_class=HTMLResponse)
def index() -> str:
    return HTML


@app.get("/api/voices")
def voices() -> JSONResponse:
    return JSONResponse({"default": DEFAULT_VOICE, "voices": list_local_voices()})


@app.get("/api/settings")
def get_settings() -> JSONResponse:
    return JSONResponse(public_settings())


@app.post("/api/settings")
async def update_settings(payload: dict[str, Any]) -> JSONResponse:
    current = load_settings()
    for key in DEFAULT_TTS_SETTINGS:
        if key in payload and isinstance(payload[key], str):
            if key == "proxy_api_key" and not payload[key].strip():
                continue
            current[key] = payload[key]
    save_settings(current)
    return JSONResponse(public_settings())


@app.post("/api/proxy-options")
async def proxy_options(payload: dict[str, Any]) -> JSONResponse:
    settings = load_settings()
    for key in DEFAULT_TTS_SETTINGS:
        if key in payload and isinstance(payload[key], str):
            if key == "proxy_api_key" and not payload[key].strip():
                continue
            settings[key] = payload[key]
    proxy_type = settings.get("proxy_type", "openai") or "openai"
    base_url = settings.get("proxy_url", "").strip().rstrip("/")
    if "api.elevenlabs.io" in base_url:
        proxy_type = "elevenlabs"
    if proxy_type != "elevenlabs":
        return JSONResponse({"ok": True, "proxy_type": proxy_type, "models": [], "voices": []})
    if not base_url:
        base_url = "https://api.elevenlabs.io/v1"
    elif base_url == "https://api.elevenlabs.io":
        base_url = "https://api.elevenlabs.io/v1"
    api_key = settings.get("proxy_api_key", "").strip() or os.environ.get("ELEVENLABS_API_KEY", "").strip()
    headers: dict[str, str] = {}
    if api_key:
        headers["xi-api-key"] = api_key
    try:
        models: list[dict[str, str]] = []
        voices: list[dict[str, str]] = []
        errors: list[str] = []
        try:
            models_req = urllib.request.Request(f"{base_url}/models", headers=headers, method="GET")
            with urllib.request.urlopen(models_req, timeout=30) as response:
                models_raw = json.loads(response.read().decode("utf-8"))
            models = [
                {"id": item.get("model_id", ""), "name": item.get("name", item.get("model_id", ""))}
                for item in (models_raw if isinstance(models_raw, list) else [])
                if item.get("model_id")
            ]
        except urllib.error.HTTPError as exc:
            if exc.code == 401:
                errors.append("当前 Key 缺少 models_read 权限，已使用内置模型列表")
            else:
                body = exc.read().decode("utf-8", errors="ignore")
                errors.append(f"模型拉取失败 {exc.code}: {body[:300]}")
        if not models:
            models = [
                {"id": "eleven_multilingual_v2", "name": "Eleven Multilingual v2"},
                {"id": "eleven_flash_v2_5", "name": "Eleven Flash v2.5"},
                {"id": "eleven_turbo_v2_5", "name": "Eleven Turbo v2.5"},
            ]
        try:
            voices_req = urllib.request.Request(f"{base_url}/voices", headers=headers, method="GET")
            with urllib.request.urlopen(voices_req, timeout=30) as response:
                voices_raw = json.loads(response.read().decode("utf-8"))
            voices = [
                {"id": item.get("voice_id", ""), "name": item.get("name", item.get("voice_id", ""))}
                for item in voices_raw.get("voices", [])
                if item.get("voice_id")
            ]
        except urllib.error.HTTPError as exc:
            if exc.code == 401:
                errors.append("当前 Key 缺少 voices_read 权限，已使用 Bella 默认音色")
            else:
                body = exc.read().decode("utf-8", errors="ignore")
                errors.append(f"音色拉取失败 {exc.code}: {body[:300]}")
        if not voices:
            voices = [
                {"id": "hpp4J3VqNfWAUOO0d1Us", "name": "Bella"},
                {"id": "21m00Tcm4TlvDq8ikWAM", "name": "Rachel"},
                {"id": "EXAVITQu4vr4xnSDxMaL", "name": "Sarah"},
                {"id": "ErXwobaYiN019PkySvjV", "name": "Antoni"},
                {"id": "pNInz6obpgDQGcFmaJgB", "name": "Adam"},
                {"id": "TxGEqnHWrfWFTfGW9XjX", "name": "Josh"},
            ]
        return JSONResponse({"ok": True, "proxy_type": "elevenlabs", "models": models, "voices": voices, "warning": "；".join(errors)})
    except Exception as exc:
        return JSONResponse({"ok": False, "error": str(exc)[:1200]}, status_code=400)


@app.post("/api/proxy-test")
async def proxy_test(payload: dict[str, Any]) -> JSONResponse:
    settings = load_settings()
    for key in DEFAULT_TTS_SETTINGS:
        if key in payload and isinstance(payload[key], str):
            settings[key] = payload[key]
    voice = payload.get("voice") if isinstance(payload.get("voice"), str) else DEFAULT_VOICE
    text = payload.get("text") if isinstance(payload.get("text"), str) else "这是 TTS 代理测试。"
    try:
        url, headers, body, audio_field = build_proxy_request(text[:120], voice, settings)
        request = urllib.request.Request(url, data=body, headers=headers, method="POST")
        with urllib.request.urlopen(request, timeout=60) as response:
            data = response.read()
        audio = extract_audio_from_response(data, audio_field)
        return JSONResponse({"ok": True, "bytes": len(audio), "proxy_type": settings.get("proxy_type", "openai")})
    except Exception as exc:
        return JSONResponse({"ok": False, "error": str(exc)[:1200]}, status_code=400)


@app.post("/api/jobs")
async def create_job(
    background: BackgroundTasks,
    file: UploadFile | None = File(None),
    manual_text: str = Form(""),
    voice: str = Form(DEFAULT_VOICE),
    chunk_size: int = Form(MAX_CHARS_PER_CHUNK),
    rate: int = Form(0),
    volume: int = Form(50),
    preview_chars: int = Form(600),
    tts_provider: str = Form(""),
    proxy_type: str = Form(""),
    proxy_url: str = Form(""),
    proxy_model: str = Form(""),
    proxy_voice_id: str = Form(""),
    proxy_group_id: str = Form(""),
) -> JSONResponse:
    job_id = uuid.uuid4().hex[:12]
    manual_text = manual_text.strip()
    if file and file.filename:
        safe_name = re.sub(r"[^\w.\-\u4e00-\u9fff]+", "_", file.filename or "book.txt")
        upload_path = UPLOADS_DIR / f"{job_id}_{safe_name}"
        with upload_path.open("wb") as f:
            shutil.copyfileobj(file.file, f)
        text = clean_text(extract_text_from_upload(upload_path, safe_name))
    elif manual_text:
        safe_name = "文本输入.txt"
        text = clean_text(manual_text)
    else:
        raise HTTPException(400, "请上传书籍文件，或在文本框输入要生成语音的内容")
    if not text:
        raise HTTPException(400, "未提取到可朗读文本")
    settings = load_settings()
    provider = (tts_provider or settings.get("provider") or "local").strip()
    if provider not in {"local", "proxy"}:
        provider = "local"
    state = JobState(
        id=job_id,
        filename=safe_name,
        voice=voice,
        chunk_size=chunk_size,
        rate=max(-50, min(rate, 50)),
        volume=max(0, min(volume, 100)),
        tts_provider=provider,
        proxy_type=(proxy_type or settings.get("proxy_type", "openai")).strip() or "openai",
        proxy_url=(proxy_url or settings.get("proxy_url", "")).strip(),
        proxy_model=(proxy_model or settings.get("proxy_model", "tts-1")).strip() or "tts-1",
        proxy_voice_id=(proxy_voice_id or settings.get("proxy_voice_id", "")).strip(),
        proxy_group_id=(proxy_group_id or settings.get("proxy_group_id", "")).strip(),
    )
    _jobs[job_id] = state
    write_job_text(job_id, text)
    save_state(state)
    background.add_task(process_preview, job_id, text, preview_chars)
    return JSONResponse({"job_id": job_id})


@app.post("/api/jobs/{job_id}/start")
async def start_full_job(job_id: str, background: BackgroundTasks) -> JSONResponse:
    state = load_state(job_id)
    if not state:
        raise HTTPException(404, "job not found")
    if state.status in {"running", "preview_running"}:
        raise HTTPException(409, "job is already running")
    if state.status == "done":
        return JSONResponse({"job_id": job_id, "status": state.status})
    text = read_job_text(job_id)
    state.status = "queued"
    state.message = "已确认，等待生成整本"
    state.done_chunks = 0
    state.total_chunks = 0
    state.output_file = None
    state.error = None
    save_state(state)
    background.add_task(process_job, job_id, text)
    return JSONResponse({"job_id": job_id, "status": state.status})


@app.get("/api/jobs/{job_id}")
def get_job(job_id: str) -> JSONResponse:
    state = load_state(job_id)
    if not state:
        raise HTTPException(404, "job not found")
    return JSONResponse(asdict(state) | {"progress": state.progress})


@app.get("/api/jobs/{job_id}/preview")
def preview(job_id: str) -> FileResponse:
    state = load_state(job_id)
    if not state or not state.preview_file:
        raise HTTPException(404, "preview not ready")
    path = Path(state.preview_file)
    if not path.exists():
        raise HTTPException(404, "preview file missing")
    return FileResponse(path, media_type="audio/mpeg", filename=path.name)


@app.get("/api/jobs/{job_id}/download")
def download(job_id: str, background: BackgroundTasks) -> FileResponse:
    state = load_state(job_id)
    if not state or state.status != "done" or not state.output_file:
        raise HTTPException(404, "output not ready")
    path = Path(state.output_file)
    if not path.exists():
        raise HTTPException(404, "file missing")
    background.add_task(cleanup_job, job_id)
    return FileResponse(path, media_type="audio/mpeg", filename=path.name)


@app.get("/health")
def health() -> PlainTextResponse:
    return PlainTextResponse("ok")


HTML = r"""
<!doctype html>
<html lang="zh-CN">
<head>
<meta charset="utf-8" />
<meta name="viewport" content="width=device-width,initial-scale=1" />
<title>小秋有声书生成器</title>
<style>
:root{color-scheme:dark;--bg:#050507;--bg2:#121216;--card:rgba(28,28,30,.72);--panel:rgba(18,18,22,.72);--text:#f5f5f7;--muted:rgba(235,235,245,.64);--line:rgba(255,255,255,.13);--input:rgba(10,10,12,.72);--blue:#0a84ff;--green:#30d158;--shadow:0 28px 90px rgba(0,0,0,.42);--blur:saturate(180%) blur(26px)}body.light{color-scheme:light;--bg:#f5f5f7;--bg2:#fff;--card:rgba(255,255,255,.76);--panel:rgba(255,255,255,.68);--text:#1d1d1f;--muted:rgba(60,60,67,.68);--line:rgba(0,0,0,.1);--input:rgba(255,255,255,.86);--blue:#0071e3;--green:#248a3d;--shadow:0 28px 80px rgba(0,0,0,.12)}*{box-sizing:border-box}body{margin:0;min-height:100vh;background:radial-gradient(circle at 20% -10%,rgba(10,132,255,.35),transparent 32%),radial-gradient(circle at 90% 10%,rgba(94,92,230,.26),transparent 28%),linear-gradient(145deg,var(--bg),var(--bg2));color:var(--text);font-family:-apple-system,BlinkMacSystemFont,"SF Pro Display","Segoe UI",system-ui,sans-serif;transition:background .35s ease,color .25s ease}.wrap{max-width:980px;margin:0 auto;padding:34px 18px}.card{position:relative;overflow:hidden;background:var(--card);border:1px solid var(--line);border-radius:30px;padding:28px;box-shadow:var(--shadow);backdrop-filter:var(--blur);-webkit-backdrop-filter:var(--blur)}.card:before{content:"";position:absolute;inset:0 0 auto;height:1px;background:linear-gradient(90deg,transparent,rgba(255,255,255,.45),transparent);pointer-events:none}.top{display:flex;justify-content:space-between;gap:16px;align-items:flex-start;margin-bottom:22px}.actions{display:flex;gap:10px;align-items:center;flex-shrink:0}h1{margin:0 0 8px;font-size:36px;line-height:1.06;letter-spacing:-.7px;font-weight:700}.sub{color:var(--muted);font-size:15px;line-height:1.6}.grid{display:grid;grid-template-columns:1fr 1fr;gap:16px}.full{grid-column:1/-1}label{display:block;color:var(--muted);margin:10px 0 7px;font-size:13px;font-weight:650;letter-spacing:-.08px}input,select,button,textarea{box-sizing:border-box;width:100%;border-radius:18px;border:1px solid var(--line);background:var(--input);color:var(--text);padding:13px 14px;font-size:15px;outline:none;transition:border-color .2s ease,box-shadow .2s ease,background .2s ease,transform .16s ease}input:focus,select:focus,textarea:focus{border-color:var(--blue);box-shadow:0 0 0 4px color-mix(in srgb,var(--blue) 22%,transparent)}textarea{min-height:104px;font-family:ui-monospace,SFMono-Regular,Menlo,monospace;resize:vertical}button{margin-top:18px;background:var(--blue);border-color:transparent;color:#fff;font-weight:700;cursor:pointer;border-radius:999px;box-shadow:0 10px 30px color-mix(in srgb,var(--blue) 28%,transparent)}button:hover{transform:translateY(-1px)}button.secondary{background:var(--green);border-color:transparent;box-shadow:0 10px 30px color-mix(in srgb,var(--green) 25%,transparent)}button.ghost{width:auto;margin-top:0;background:rgba(120,120,128,.16);border-color:var(--line);box-shadow:none;color:var(--text);padding:10px 14px}.themeToggle{min-width:44px;height:42px;padding:0 13px;font-size:18px}button:disabled{opacity:.55;cursor:not-allowed;transform:none}.bar{height:12px;background:rgba(120,120,128,.18);border-radius:999px;overflow:hidden;border:1px solid var(--line);margin-top:20px}.fill{height:100%;width:0;background:linear-gradient(90deg,var(--blue),var(--green));border-radius:999px}pre{white-space:pre-wrap;background:var(--panel);border:1px solid var(--line);border-radius:20px;padding:16px;color:var(--muted);line-height:1.55}.download{display:none;margin-top:16px;color:var(--blue);font-weight:650}.preview{display:none;margin-top:18px;padding:16px;border:1px solid var(--line);border-radius:22px;background:var(--panel)}.preview audio{width:100%;margin-top:10px}.hint{font-size:13px;color:var(--muted);line-height:1.65}.settings{display:none;margin:0 0 18px;padding:18px;border:1px solid var(--line);border-radius:24px;background:var(--panel);backdrop-filter:var(--blur);-webkit-backdrop-filter:var(--blur)}.settings.open{display:block}.settings .row{display:grid;grid-template-columns:1fr 1fr;gap:12px}.muted{color:var(--muted);font-size:13px}@media(max-width:700px){.wrap{padding:16px 12px}.card{border-radius:24px;padding:18px}.grid,.settings .row{grid-template-columns:1fr}.top{display:block}.actions{margin-top:12px}.top button{margin:0}h1{font-size:30px}}
</style>
</head>
<body><div class="wrap"><div class="card">
<div class="top"><div><h1>小秋有声书生成器</h1>
<div class="sub">上传 txt / md / epub / pdf / docx / mobi / azw3 / zip，先试听，满意后再生成整本 MP3。</div></div><div class="actions"><button id="themeToggle" class="ghost themeToggle" type="button" title="切换浅色/深色主题">☀️</button><button id="settingsToggle" class="ghost" type="button">TTS 设置</button></div></div>
<div id="settingsBox" class="settings">
 <div class="row">
  <div><label>后端</label><select id="ttsProvider" name="tts_provider"><option value="local">本地 LibreTTS / Edge</option><option value="proxy">TTS 代理</option></select></div>
  <div><label>代理模板</label><select id="proxyType"><option value="openai">OpenAI 官方/兼容 /v1/audio/speech</option><option value="google">Google Cloud TTS text:synthesize</option><option value="elevenlabs">ElevenLabs</option><option value="minimax">MiniMax T2A</option><option value="custom_json">自定义 JSON</option></select></div>
 </div>
 <div class="row">
  <div><label>代理模型</label><select id="proxyModelSelect"><option value="tts-1">tts-1</option></select><input id="proxyModel" name="proxy_model" type="hidden" value="tts-1" /></div>
  <div><label>代理音色</label><select id="proxyVoiceSelect"><option value="">自动 / 使用默认音色</option></select><input id="proxyVoiceId" type="hidden" /></div>
 </div>
 <label>代理 URL</label><input id="proxyUrl" name="proxy_url" placeholder="切换代理模板后自动填充默认 URL，也可以手动覆盖" />
 <div class="row"><div><label>代理 API Key</label><input id="proxyApiKey" name="proxy_api_key" type="password" placeholder="请手动填写当前平台 API Key；留空则不覆盖已保存 Key" /></div><div><label>MiniMax group_id</label><input id="proxyGroupId" placeholder="MiniMax 可选 group_id" /></div></div>
 <label>自定义请求头 JSON</label><textarea id="customHeaders" placeholder='例如 {"Authorization":"Bearer {{api_key}}"}'></textarea>
 <label>自定义请求体模板 JSON</label><textarea id="customBodyTemplate" placeholder='例如 {"model":"{{model}}","voice":"{{voice}}","input":"{{text}}"}'></textarea>
 <label>自定义音频字段</label><input id="customAudioField" placeholder="如果返回 JSON+base64，例如 data.audio；直接返回 mp3 可留空" />
 <button id="saveSettings" class="ghost" type="button">保存设置</button> <button id="loadProxyOptions" class="ghost" type="button">刷新模型/音色</button> <button id="testProxy" class="ghost" type="button">测试代理</button><span id="settingsMsg" class="muted"></span>
</div>
<form id="form" class="grid">
<div class="full"><label>文本输入</label><textarea name="manual_text" placeholder="可直接输入一小段文字生成语音；如果同时上传文件，则优先使用上传文件。"></textarea></div>
<div class="full"><label>书籍文件上传</label><input name="file" type="file" /><div class="hint">支持 txt / md / epub / pdf / docx / mobi / azw3 / zip。也可以不上传文件，只使用上方文本输入。</div></div>
<div><label>音色选择</label><select id="voiceSelect" name="voice"><option value="zh-CN-XiaoqiuNeural">晓秋 - 中文普通话</option></select></div>
<div><label>每段字数</label><input name="chunk_size" type="number" min="200" max="2200" value="1800" /></div>
<div><label>语速百分比</label><input name="rate" type="number" min="-50" max="50" value="0" /></div>
<div><label>音量百分比</label><input name="volume" type="number" min="0" max="100" value="50" /></div>
<div><label>试听字数</label><input name="preview_chars" type="number" min="200" max="1200" value="600" /></div>
<div class="full"><label>说明</label><div class="hint">先生成一小段试听，确认音色、语速、音量和效果满意后，再点击生成整本。试听任务若不继续生成，约 30 分钟后自动删除；整本下载后也会自动删除服务器文件。建议每段 1500–1800 字。</div></div>
<div class="full"><button id="btn" type="submit">生成试听</button></div>
</form>
<div class="preview" id="previewBox"><b>试听片段</b><audio id="previewAudio" controls></audio><button id="startFull" class="secondary" type="button">满意，生成整本</button></div>
<div class="bar"><div id="fill" class="fill"></div></div>
<pre id="log">等待上传...</pre>
<a id="download" class="download" href="#">下载音频</a>
</div></div>
<script>
const form=document.getElementById('form'),btn=document.getElementById('btn'),log=document.getElementById('log'),fill=document.getElementById('fill'),download=document.getElementById('download'),previewBox=document.getElementById('previewBox'),previewAudio=document.getElementById('previewAudio'),startFull=document.getElementById('startFull'),voiceSelect=document.getElementById('voiceSelect'),themeToggle=document.getElementById('themeToggle'),settingsToggle=document.getElementById('settingsToggle'),settingsBox=document.getElementById('settingsBox'),ttsProvider=document.getElementById('ttsProvider'),proxyType=document.getElementById('proxyType'),proxyUrl=document.getElementById('proxyUrl'),proxyApiKey=document.getElementById('proxyApiKey'),proxyModel=document.getElementById('proxyModel'),proxyModelSelect=document.getElementById('proxyModelSelect'),proxyVoiceId=document.getElementById('proxyVoiceId'),proxyVoiceSelect=document.getElementById('proxyVoiceSelect'),proxyGroupId=document.getElementById('proxyGroupId'),customHeaders=document.getElementById('customHeaders'),customBodyTemplate=document.getElementById('customBodyTemplate'),customAudioField=document.getElementById('customAudioField'),saveSettings=document.getElementById('saveSettings'),loadProxyOptions=document.getElementById('loadProxyOptions'),testProxy=document.getElementById('testProxy'),settingsMsg=document.getElementById('settingsMsg');
let timer=null,currentJob=null,allVoices=[];
function show(obj){log.textContent=typeof obj==='string'?obj:JSON.stringify(obj,null,2)}
function resetUi(){download.style.display='none'; previewBox.style.display='none'; previewAudio.removeAttribute('src'); startFull.disabled=false; fill.style.width='0%'}
function selectedVoice(){return voiceSelect.value||'zh-CN-XiaoqiuNeural'}
function applyTheme(theme){document.body.classList.toggle('light',theme==='light'); themeToggle.textContent=theme==='light'?'🌙':'☀️'; themeToggle.title=theme==='light'?'切换深色主题':'切换浅色主题'}
const savedTheme=localStorage.getItem('bookTtsTheme')||(matchMedia('(prefers-color-scheme: light)').matches?'light':'dark'); applyTheme(savedTheme);
themeToggle.addEventListener('click',()=>{const next=document.body.classList.contains('light')?'dark':'light'; localStorage.setItem('bookTtsTheme',next); applyTheme(next)});
function settingsPayload(){syncProxyHidden(); const payload={provider:ttsProvider.value,proxy_type:proxyType.value,proxy_url:proxyUrl.value,proxy_model:proxyModel.value,proxy_voice_id:proxyVoiceId.value,proxy_group_id:proxyGroupId.value,custom_headers:customHeaders.value,custom_body_template:customBodyTemplate.value,custom_audio_field:customAudioField.value}; if(proxyApiKey.value.trim())payload.proxy_api_key=proxyApiKey.value; return payload}
function syncProxyHidden(){proxyModel.value=proxyModelSelect.value; proxyVoiceId.value=proxyVoiceSelect.value}
function renderVoices(){
 const current=voiceSelect.value;
 voiceSelect.innerHTML='';
 for(const v of allVoices){
  const o=document.createElement('option'); o.value=v.name; o.textContent=v.display||v.name; if(v.name===current)o.selected=true; voiceSelect.appendChild(o);
 }
 if(!voiceSelect.value && voiceSelect.options.length)voiceSelect.options[0].selected=true;
}
async function loadVoices(){
 try{const r=await fetch('/api/voices'); const j=await r.json(); allVoices=j.voices||[]; if(j.default){voiceSelect.value=j.default;} renderVoices();}
 catch(e){show('音色列表加载失败：'+e.message)}
}
async function loadSettings(){
 try{const r=await fetch('/api/settings'); const s=await r.json(); ttsProvider.value=s.provider||'local'; proxyType.value=s.proxy_type||'openai'; proxyUrl.value=s.proxy_url||''; proxyModel.value=s.proxy_model||'tts-1'; proxyVoiceId.value=s.proxy_voice_id||''; proxyModelSelect.innerHTML=`<option value="${proxyModel.value||'tts-1'}">${proxyModel.value||'tts-1'}</option>`; proxyModelSelect.value=proxyModel.value||'tts-1'; proxyVoiceSelect.innerHTML=`<option value="${proxyVoiceId.value||''}">${proxyVoiceId.value||'自动 / 使用默认音色'}</option>`; proxyVoiceSelect.value=proxyVoiceId.value||''; proxyGroupId.value=s.proxy_group_id||''; customHeaders.value=s.custom_headers||''; customBodyTemplate.value=s.custom_body_template||''; customAudioField.value=s.custom_audio_field||''; applyProxyPreset(); if(proxyType.value==='elevenlabs'||proxyUrl.value.includes('api.elevenlabs.io'))refreshProxyOptions();}
 catch(e){settingsMsg.textContent='设置加载失败'}
}
function setSelectStatic(select,items,current){select.innerHTML=''; for(const item of items){const o=document.createElement('option'); o.value=item.id; o.textContent=item.name||item.id; select.appendChild(o)} select.value=current||items[0]?.id||''; syncProxyHidden()}
function applyProxyPreset(force=false){const presets={openai:{url:'https://api.openai.com/v1',models:[{id:'tts-1',name:'tts-1'},{id:'tts-1-hd',name:'tts-1-hd'},{id:'gpt-4o-mini-tts',name:'gpt-4o-mini-tts'}],voices:[{id:'alloy',name:'alloy'},{id:'ash',name:'ash'},{id:'ballad',name:'ballad'},{id:'coral',name:'coral'},{id:'echo',name:'echo'},{id:'fable',name:'fable'},{id:'nova',name:'nova'},{id:'onyx',name:'onyx'},{id:'sage',name:'sage'},{id:'shimmer',name:'shimmer'}]},google:{url:'https://texttospeech.googleapis.com/v1',models:[{id:'google-tts',name:'Google Cloud TTS'}],voices:[{id:'cmn-CN-Standard-A',name:'cmn-CN-Standard-A'},{id:'cmn-CN-Wavenet-A',name:'cmn-CN-Wavenet-A'},{id:'zh-CN-Standard-A',name:'zh-CN-Standard-A'},{id:'zh-CN-Wavenet-A',name:'zh-CN-Wavenet-A'}]},elevenlabs:{url:'https://api.elevenlabs.io/v1',models:[{id:'eleven_multilingual_v2',name:'Eleven Multilingual v2'},{id:'eleven_flash_v2_5',name:'Eleven Flash v2.5'},{id:'eleven_turbo_v2_5',name:'Eleven Turbo v2.5'}],voices:[{id:'hpp4J3VqNfWAUOO0d1Us',name:'Bella'}]},minimax:{url:'https://api-uw.minimax.io/v1',models:[{id:'speech-2.6-turbo',name:'speech-2.6-turbo'},{id:'speech-02-hd',name:'speech-02-hd'},{id:'speech-01-turbo',name:'speech-01-turbo'}],voices:[{id:'male-qn-qingse',name:'male-qn-qingse'},{id:'female-shaonv',name:'female-shaonv'},{id:'female-yujie',name:'female-yujie'}]},custom_json:{url:'',models:[{id:'custom',name:'custom'}],voices:[{id:'custom',name:'custom'}]}}; const p=presets[proxyType.value]; if(!p)return; if(force||!proxyUrl.value.trim()||proxyUrl.value.includes('api.elevenlabs.io')||proxyUrl.value.includes('api.openai.com')||proxyUrl.value.includes('texttospeech.googleapis.com')||proxyUrl.value.includes('minimax.io'))proxyUrl.value=p.url; setSelectStatic(proxyModelSelect,p.models,proxyModel.value&&p.models.some(x=>x.id===proxyModel.value)?proxyModel.value:p.models[0]?.id); setSelectStatic(proxyVoiceSelect,p.voices,proxyVoiceId.value&&p.voices.some(x=>x.id===proxyVoiceId.value)?proxyVoiceId.value:p.voices[0]?.id)}
function setSelectOptions(select,items,current,emptyLabel){select.innerHTML=emptyLabel?`<option value="">${emptyLabel}</option>`:''; for(const item of items){const o=document.createElement('option'); o.value=item.id; o.textContent=item.name&&item.name!==item.id?`${item.name} · ${item.id}`:item.id; select.appendChild(o)} if(current&&[...select.options].some(o=>o.value===current))select.value=current; else if(!current&&select.options.length)select.selectedIndex=0;}
async function refreshProxyOptions(){syncProxyHidden(); applyProxyPreset(); if(proxyType.value!=='elevenlabs'&&!proxyUrl.value.includes('api.elevenlabs.io')){settingsMsg.textContent=' 当前平台暂不支持自动拉取'; return;} settingsMsg.textContent=' 正在拉取模型/音色...'; const r=await fetch('/api/proxy-options',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(settingsPayload())}); const j=await r.json().catch(()=>({ok:false,error:'响应不是 JSON'})); if(!j.ok){settingsMsg.textContent=' 拉取失败：'+(j.error||await r.text()); return;} const model=(j.models||[]).find(x=>x.id===proxyModel.value)||(j.models||[]).find(x=>x.id==='eleven_multilingual_v2')||(j.models||[])[0]; const voice=(j.voices||[]).find(x=>x.id===proxyVoiceId.value)||(j.voices||[]).find(x=>x.id==='hpp4J3VqNfWAUOO0d1Us')||(j.voices||[])[0]; setSelectOptions(proxyModelSelect,j.models||[],model&&model.id,''); setSelectOptions(proxyVoiceSelect,j.voices||[],voice&&voice.id,'自动 / 使用默认音色'); syncProxyHidden(); settingsMsg.textContent=` 已拉取 ${(j.models||[]).length} 个模型、${(j.voices||[]).length} 个音色${j.warning?'；'+j.warning:''}`;}
settingsToggle.addEventListener('click',()=>settingsBox.classList.toggle('open'));
proxyModelSelect.addEventListener('change',syncProxyHidden); proxyVoiceSelect.addEventListener('change',syncProxyHidden);
proxyType.addEventListener('change',()=>{applyProxyPreset(true); if(proxyType.value==='elevenlabs')refreshProxyOptions(); else settingsMsg.textContent=' 已填充当前模板默认 URL / 模型 / 音色，API Key 请手动填写';});
loadProxyOptions.addEventListener('click',refreshProxyOptions);
saveSettings.addEventListener('click',async()=>{
 settingsMsg.textContent=' 保存中...';
 const r=await fetch('/api/settings',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(settingsPayload())});
 settingsMsg.textContent=r.ok?' 已保存':' 保存失败：'+await r.text(); if(r.ok)proxyApiKey.value='';
});
testProxy.addEventListener('click',async()=>{
 settingsMsg.textContent=' 测试中...';
 const payload={...settingsPayload(),voice:selectedVoice(),text:'这是 TTS 代理测试。'};
 const r=await fetch('/api/proxy-test',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(payload)});
 const j=await r.json().catch(()=>({ok:false,error:'响应不是 JSON'}));
 settingsMsg.textContent=j.ok?` 测试成功：${j.bytes} bytes`:` 测试失败：${j.error||await r.text()}`;
});
async function poll(id){
 const r=await fetch('/api/jobs/'+id); const j=await r.json();
 fill.style.width=Math.round((j.progress||0)*100)+'%';
 show({状态:j.status,后端:j.tts_provider,模板:j.proxy_type,语速:j.rate,音量:j.volume,消息:j.message,进度:`${j.done_chunks}/${j.total_chunks}`,字符数:j.chars,错误:j.error||''});
 if(j.status==='preview_done'){
  clearInterval(timer); btn.disabled=false; startFull.disabled=false; previewAudio.src='/api/jobs/'+id+'/preview?t='+Date.now(); previewBox.style.display='block';
 }
 if(j.status==='done'){
  clearInterval(timer); btn.disabled=false; startFull.disabled=false; download.href='/api/jobs/'+id+'/download'; download.style.display='inline-block'; download.textContent='下载 '+(j.output_file||'音频').split('/').pop()+'（下载后服务器自动删除）';
 }
 if(j.status==='failed'){
  clearInterval(timer); btn.disabled=false; startFull.disabled=false;
 }
}
form.addEventListener('submit',async e=>{
 e.preventDefault(); btn.disabled=true; resetUi(); show('正在上传并生成试听...');
 const fd=new FormData(form); fd.set('tts_provider',ttsProvider.value); fd.set('proxy_type',proxyType.value); fd.set('proxy_url',proxyUrl.value); fd.set('proxy_model',proxyModel.value); fd.set('proxy_voice_id',proxyVoiceId.value); fd.set('proxy_group_id',proxyGroupId.value);
 const r=await fetch('/api/jobs',{method:'POST',body:fd});
 if(!r.ok){show(await r.text()); btn.disabled=false; return;}
 const {job_id}=await r.json(); currentJob=job_id; show('试听任务已创建：'+job_id); timer=setInterval(()=>poll(job_id),2000); poll(job_id);
});
startFull.addEventListener('click',async()=>{
 if(!currentJob)return; startFull.disabled=true; btn.disabled=true; show('已确认，正在启动整本生成...');
 const r=await fetch('/api/jobs/'+currentJob+'/start',{method:'POST'});
 if(!r.ok){show(await r.text()); startFull.disabled=false; btn.disabled=false; return;}
 fill.style.width='0%'; timer=setInterval(()=>poll(currentJob),2000); poll(currentJob);
});
loadVoices(); loadSettings();
</script></body></html>
"""
