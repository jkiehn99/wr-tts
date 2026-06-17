# 有声书生成器（Book TTS Web）

一个轻量级的网页有声书生成工具，支持把长文本、电子书和文档转换为 MP3。项目默认使用 Microsoft 小秋音色 `zh-CN-XiaoqiuNeural`，适合中文小说、文章、课程资料、长文档的批量转语音。
：**先生成试听 → 确认音色和语速 → 再生成整本 → 下载后自动清理服务器文件**。

## 功能亮点

- **试听优先**：上传文件或输入文本后，先生成一小段试听，避免一开始浪费时间生成整本。
- **整本生成**：试听满意后再点击“生成整本”，后台按分段生成并合并为 MP3。
- **格式支持**：支持 `txt`、`md`、`epub`、`pdf`、`docx`、`mobi`、`azw3`、`zip`。
- **文本直输**：不上传文件也可以直接输入文本生成试听。
- **默认小秋**：默认音色为 `晓秋 - 中文普通话`。
- **音色白名单**：只显示中文普通话、中文香港、英语美国、英语英国，避免音色列表过多。
- **TTS 代理**：支持本地 TTS，也支持 OpenAI、Google、ElevenLabs、MiniMax、自定义 JSON 代理。
- **自动清理**：整本 MP3 下载后自动删除服务器文件；只试听不生成整本的任务约 30 分钟后自动清理。
- **网页管理**：单文件 FastAPI 应用，浏览器即可上传、试听、查看进度和下载。

## 项目目录

默认部署路径：

```text
/opt/data/apps/book-tts-web
```

主要文件：

```text
app.py        # FastAPI 主程序，包含后端接口和前端页面
run.sh        # 启动脚本
scripts/      # 内置本地 TTS 后端脚本
README.md     # 项目说明
.gitignore    # Git 忽略规则
jobs/         # 运行时任务目录，不提交 Git
uploads/      # 上传文件目录，不提交 Git
settings.json # 本地 TTS 代理设置，不提交 Git
```

## 环境要求

基础环境：

- Linux / Docker 容器均可
- Python 3.10+
- `ffmpeg`
- Node.js（用于运行项目内置的 `scripts/libretts-edge.mjs`）

Python 依赖主要包括：

- `fastapi`
- `uvicorn`
- `edge_tts`
- `python-multipart`
- `pymupdf` / PDF 解析相关依赖
- `python-docx` / DOCX 解析相关依赖
- `ebooklib` / EPUB 解析相关依赖
- `mobi` / MOBI 解析相关依赖

如果你是在当前 Hermes 容器里使用，这些依赖一般已经准备好。

## 本地部署教程

进入项目目录：

```bash
cd /opt/data/apps/book-tts-web
```

赋予启动脚本执行权限：

```bash
chmod +x run.sh
```

启动服务：

```bash
./run.sh
```

默认监听：

```text
http://0.0.0.0:9120
```

本机访问：

```text
http://127.0.0.1:9120
```

也可以直接用 uvicorn 启动：

```bash
python3 -m uvicorn app:app --host 0.0.0.0 --port 9120
```

## Docker / Hermes 容器部署

如果项目运行在名为 `hermes` 的 Docker 容器内，可以在宿主机执行：

```bash
docker exec -d hermes bash -lc 'cd /opt/data/apps/book-tts-web && python3 -m uvicorn app:app --host 0.0.0.0 --port 9120'
```

进入容器调试：

```bash
docker exec -it hermes bash
cd /opt/data/apps/book-tts-web
```

查看服务进程：

```bash
ps -ef | grep 'uvicorn app:app' | grep -v grep
```

容器内健康检查：

```bash
curl http://127.0.0.1:9120/health
```

如果 Docker 没有映射 `9120:9120`，宿主机的 `127.0.0.1:9120` 访问不到。此时可以让 1Panel/OpenResty 直接反代到容器 IP：

```bash
docker inspect -f '{{range.NetworkSettings.Networks}}{{.IPAddress}}{{end}}' hermes
```

假设输出是 `172.17.0.2`，反代目标填写：

```text
http://172.17.0.2:9120
```

## 1Panel / OpenResty 反代示例

如果容器端口已经映射到宿主机：

```text
http://127.0.0.1:9120
```

如果没有映射端口，使用容器 IP：

```text
http://容器IP:9120
```

Nginx/OpenResty 反代示例：

```nginx
location / {
    proxy_pass http://127.0.0.1:9120;
    proxy_set_header Host $host;
    proxy_set_header X-Real-IP $remote_addr;
    proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
    proxy_set_header X-Forwarded-Proto $scheme;
}
```

如果使用容器 IP，把 `127.0.0.1` 换成实际容器 IP。

## 使用流程

1. 打开网页。
2. 上传电子书/文档，或直接在文本框输入内容。
3. 选择音色，默认是 `晓秋 - 中文普通话`。
4. 设置每段字数和语速。
5. 点击 `生成试听`。
6. 播放试听音频，确认音色和语速是否合适。
7. 满意后点击 `满意，生成整本`。
8. 等待后台分段生成并合并 MP3。
9. 点击下载音频。
10. 下载后服务器端任务目录和音频文件会自动删除。

## 音色说明

当前音色列表使用白名单，只显示：

- 中文普通话：例如 `晓秋 - 中文普通话`
- 中文香港：例如 `HiuMaan - 中文香港`
- 英语美国：例如 `Jenny - 英语美国`
- 英语英国：例如 `Sonia - 英语英国`

这样可以避免 Edge TTS 返回几百个多语言音色，导致页面难以选择。

## TTS 代理设置

页面右上角点击 `TTS 设置`，可以切换：

- 本地 LibreTTS / Edge
- TTS 代理

支持的代理模板：

- OpenAI 官方/兼容：`/v1/audio/speech`
- Google Cloud TTS：`/v1/text:synthesize`
- ElevenLabs
- MiniMax T2A
- 自定义 JSON

代理配置项包括：

- 代理 URL
- API Key
- 模型名
- voice_id
- MiniMax group_id
- 自定义请求头 JSON
- 自定义请求体模板 JSON
- 自定义音频字段

API Key 保存在本地 `settings.json`，接口不会回显。不要把 `settings.json` 提交到 Git 仓库。

## 环境变量

```bash
BOOK_TTS_WEB_HOME=/opt/data/apps/book-tts-web
XIAOQIU_TTS_SCRIPT=/opt/data/apps/book-tts-web/scripts/libretts-edge.mjs
XIAOQIU_TTS_VOICE=zh-CN-XiaoqiuNeural
BOOK_TTS_MAX_CHARS=1800
BOOK_TTS_PREVIEW_TTL_SECONDS=1800
PORT=9120
```

说明：

- `BOOK_TTS_WEB_HOME`：项目运行目录。
- `XIAOQIU_TTS_SCRIPT`：本地小秋 TTS 脚本路径；默认使用项目内置 `scripts/libretts-edge.mjs`，通常不用设置。
- `XIAOQIU_TTS_VOICE`：默认音色。
- `BOOK_TTS_MAX_CHARS`：默认每段字符数。
- `BOOK_TTS_PREVIEW_TTL_SECONDS`：试听任务过期清理时间，默认 1800 秒。
- `PORT`：服务端口，默认 9120。

## 稳定性建议

小秋长文本 TTS 建议每段保持在 `1500–1800` 个中文字符。

当前环境实测：

- `1800` 字左右较稳定。
- `2000` 字左右通常可用。
- `2400` 字以上容易失败。

如果生成失败，优先把每段字数调低到 `1500`。

## Git 提交注意事项

以下内容不应提交到 Git：

- `jobs/`
- `uploads/`
- `settings.json`
- `*.mp3`
- `*.concat.txt`
- `__pycache__/`

这些文件可能包含上传原文、生成音频、任务状态或 API Key。

## 常见问题

### 页面能打开，但上传 `.mobi` 报不支持

确认运行的是最新版服务，重启：

```bash
cd /opt/data/apps/book-tts-web
python3 -m uvicorn app:app --host 0.0.0.0 --port 9120
```

### 域名打不开，但容器内能访问

大概率是 Docker 没有映射 `9120` 端口。可以：

- 重建容器并加 `-p 9120:9120`
- 或让 OpenResty 直接反代到容器 IP 的 `9120`

### 音色列表太多

当前版本已经改成白名单，只显示中文普通话、中文香港、英语美国、英语英国。

### 生成很慢

长书会按分段逐段生成，属于正常现象。可以先用试听确认效果，再生成整本。

### 下载后文件找不到

这是正常行为。整本 MP3 下载后，服务器会自动删除任务目录和音频文件，避免占用磁盘。
