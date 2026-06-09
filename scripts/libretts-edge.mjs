#!/usr/bin/env node
import fs from 'fs';
import crypto from 'crypto';
import path from 'path';

const args = process.argv.slice(2);
let text = '';
let voice = 'zh-CN-XiaoqiuNeural';
let out = '/opt/data/audio_cache/libretts_xiaoqiu.mp3';
let rate = 0;
let pitch = 0;
for (let i = 0; i < args.length; i++) {
  if (args[i] === '--text') text = args[++i] || '';
  else if (args[i] === '--voice') voice = args[++i] || voice;
  else if (args[i] === '--out') out = args[++i] || out;
  else if (args[i] === '--rate') rate = Number(args[++i] || 0);
  else if (args[i] === '--pitch') pitch = Number(args[++i] || 0);
}
if (!text) {
  console.error('Usage: libretts-edge.mjs --text 文本 [--voice zh-CN-XiaoqiuNeural] [--out /path/file.mp3] [--rate 0] [--pitch 0]');
  process.exit(2);
}
let clientId = crypto.randomUUID().replace(/-/g, '');
function formatDate() { return new Date().toUTCString().replace(/GMT/, '').trim().toLowerCase() + ' GMT'; }
function generateSignature(urlStr) {
  const encodedUrl = encodeURIComponent(urlStr.split('://')[1]);
  const uuidStr = crypto.randomUUID().replace(/-/g, '');
  const formattedDate = formatDate();
  const bytesToSign = `MSTranslatorAndroidApp${encodedUrl}${formattedDate}${uuidStr}`.toLowerCase();
  const key = Buffer.from('oik6PdDdMnOXemTbwvMn9de/h9lFnfBaCWbGMMZqqoSaQaqUOqjVGm5NqsmjcBI1x+sS9ugjB55HEJWRiFXYFw==', 'base64');
  const signatureBase64 = crypto.createHmac('sha256', key).update(bytesToSign).digest('base64');
  return `MSTranslatorAndroidApp::${signatureBase64}::${formattedDate}::${uuidStr}`;
}
async function getEndpoint() {
  const endpointUrl = 'https://dev.microsofttranslator.com/apps/endpoint?api-version=1.0';
  const response = await fetch(endpointUrl, { method: 'POST', headers: {
    'Accept-Language': 'zh-Hans', 'X-ClientVersion': '4.0.530a 5fe1dc6c', 'X-UserId': '0f04d16a175c411e',
    'X-HomeGeographicRegion': 'zh-Hans-CN', 'X-ClientTraceId': clientId, 'X-MT-Signature': generateSignature(endpointUrl),
    'User-Agent': 'okhttp/4.5.0', 'Content-Type': 'application/json; charset=utf-8', 'Accept-Encoding': 'gzip'
  }});
  if (!response.ok) throw new Error(`getEndpoint failed ${response.status}: ${await response.text()}`);
  return await response.json();
}
function escapeXml(s) { return s.replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;').replace(/'/g,'&apos;'); }
function ssml(text, voice, rate, pitch) { return `<speak xmlns="http://www.w3.org/2001/10/synthesis" xmlns:mstts="http://www.w3.org/2001/mstts" version="1.0" xml:lang="zh-CN"><voice name="${voice}"><mstts:express-as style="general" styledegree="1.0" role="default"><prosody rate="${rate}%" pitch="${pitch}%" volume="50">${escapeXml(text)}</prosody></mstts:express-as></voice></speak>`; }
const endpoint = await getEndpoint();
const response = await fetch(`https://${endpoint.r}.tts.speech.microsoft.com/cognitiveservices/v1`, { method:'POST', headers: {
  'Authorization': endpoint.t, 'Content-Type':'application/ssml+xml', 'X-Microsoft-OutputFormat':'audio-24khz-48kbitrate-mono-mp3',
  'User-Agent':'okhttp/4.5.0', 'Origin':'https://azure.microsoft.com', 'Referer':'https://azure.microsoft.com/'
}, body: ssml(text, voice, rate, pitch) });
const buf = Buffer.from(await response.arrayBuffer());
if (!response.ok) { console.error(buf.toString('utf8')); process.exit(1); }
fs.mkdirSync(path.dirname(out), { recursive: true });
fs.writeFileSync(out, buf);
console.log(JSON.stringify({ path: out, voice, bytes: buf.length, sha256: crypto.createHash('sha256').update(buf).digest('hex') }, null, 2));
