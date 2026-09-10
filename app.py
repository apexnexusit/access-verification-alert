"""Consent-based access-attempt selfie alerts.

Camera capture is initiated only after the visitor presses the button and the
browser grants permission. Images are relayed to the configured owner by email
and the existing Hermes Telegram home destination; they are not publicly served.
"""
from __future__ import annotations

import asyncio
import ipaddress
import os
import re
import subprocess
import tempfile
import time
from email.message import EmailMessage
from pathlib import Path
from typing import Any

import smtplib
import ssl
import yaml
from fastapi import FastAPI, File, HTTPException, Request, UploadFile
from fastapi.responses import HTMLResponse, JSONResponse

APP_DIR = Path(__file__).resolve().parent
MAX_BYTES = 5 * 1024 * 1024
RATE_SECONDS = 90
last_by_ip: dict[str, float] = {}
app = FastAPI(docs_url=None, redoc_url=None)

PAGE = """<!doctype html>
<html lang="en"><head><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Access verification</title><style>
:root{color-scheme:dark}*{box-sizing:border-box}body{margin:0;min-height:100vh;display:grid;place-items:center;background:#0a0b0d;color:#f5f7fa;font:16px system-ui,-apple-system,sans-serif}.card{width:min(92vw,440px);padding:32px;border:1px solid #30343a;border-radius:20px;background:#15171b;box-shadow:0 20px 80px #0008}h1{font-size:25px;margin:0 0 12px}p{color:#b7bdc7;line-height:1.5}button{width:100%;padding:15px;margin-top:12px;border:0;border-radius:12px;background:#fff;color:#0d0e10;font-weight:700;font-size:16px;cursor:pointer}button:disabled{opacity:.55;cursor:wait}#status{min-height:24px;margin:16px 0 0;font-size:14px}video{display:none;width:100%;border-radius:12px;margin-top:18px;transform:scaleX(-1)}.notice{font-size:12px;color:#8f98a6;margin-top:18px}
</style></head><body><main class="card"><h1>Access verification</h1><p>To continue, press the button. Your browser will ask for camera permission and, if granted, a single selfie will be sent to the access owner for review.</p><button id="capture">Verify with selfie</button><video id="video" autoplay playsinline></video><div id="status" aria-live="polite"></div><p class="notice">No photo is captured until you press the button and approve the browser camera prompt.</p></main>
<script>
const b=document.querySelector('#capture'), v=document.querySelector('#video'), s=document.querySelector('#status');
b.onclick=async()=>{b.disabled=true;s.textContent='Requesting camera permission…';let stream;try{stream=await navigator.mediaDevices.getUserMedia({video:{facingMode:'user'},audio:false});v.srcObject=stream;v.style.display='block';await new Promise(r=>setTimeout(r,700));const c=document.createElement('canvas');c.width=v.videoWidth||720;c.height=v.videoHeight||960;c.getContext('2d').drawImage(v,0,0,c.width,c.height);const blob=await new Promise(r=>c.toBlob(r,'image/jpeg',.88));const f=new FormData();f.append('photo',blob,'selfie.jpg');s.textContent='Sending verification…';const r=await fetch('/api/selfie',{method:'POST',body:f});if(!r.ok)throw new Error((await r.json()).detail||'Upload failed');s.textContent='Verification sent. Thank you.';}catch(e){s.textContent=e.name==='NotAllowedError'?'Camera permission was not granted.':`Could not send verification: ${e.message}`;}finally{if(stream)stream.getTracks().forEach(t=>t.stop());b.disabled=false;v.style.display='none';}};
</script></body></html>"""

def client_ip(request: Request) -> str:
    # The app listens only on loopback and is exposed through cloudflared, which
    # supplies this header. Do not use generic X-Forwarded-For values.
    candidate = request.headers.get('cf-connecting-ip', '').strip()
    try:
        return str(ipaddress.ip_address(candidate)) if candidate else (request.client.host if request.client else 'unknown')
    except ValueError:
        return request.client.host if request.client else 'unknown'

def is_allowed(ip: str) -> bool:
    raw = os.getenv('ALLOWED_IPS', '').strip()
    if not raw:
        return False
    try:
        candidate = ipaddress.ip_address(ip)
        return any(candidate in ipaddress.ip_network(v.strip(), strict=False) for v in raw.split(',') if v.strip())
    except ValueError:
        return False

def hermes_config() -> dict[str, Any]:
    path = Path.home() / '.hermes' / 'config.yaml'
    return yaml.safe_load(path.read_text()) or {}

def send_email(photo_path: Path, ip: str) -> None:
    cfg = hermes_config().get('email', {})
    if not all(cfg.get(k) for k in ('EMAIL_ADDRESS','EMAIL_PASSWORD','EMAIL_SMTP_HOST','EMAIL_SMTP_PORT')):
        raise RuntimeError('Email delivery is not configured')
    msg = EmailMessage()
    msg['From'] = f"Ghozo.JS <{cfg['EMAIL_ADDRESS']}>"
    # Self-delivery avoids embedding another address in app configuration.
    msg['To'] = cfg['EMAIL_ADDRESS']
    msg['Subject'] = f'Access verification selfie — {ip}'
    msg.set_content(f'A visitor completed a consent-based access verification. Source IP: {ip}. The attached photo was relayed immediately and is not retained by the service.')
    msg.add_attachment(photo_path.read_bytes(), maintype='image', subtype='jpeg', filename='access-selfie.jpg')
    with smtplib.SMTP(cfg['EMAIL_SMTP_HOST'], int(cfg['EMAIL_SMTP_PORT']), timeout=30) as smtp:
        smtp.starttls(context=ssl.create_default_context())
        smtp.login(cfg['EMAIL_ADDRESS'], cfg['EMAIL_PASSWORD'])
        smtp.send_message(msg)

def send_telegram(photo_path: Path, ip: str) -> None:
    # Uses the gateway's configured Telegram home chat without storing a bot token in this project.
    result = subprocess.run(['hermes','send','--to','telegram:1539410340', f'Access verification selfie from {ip}\nMEDIA:{photo_path}'], capture_output=True, text=True, timeout=45)
    if result.returncode != 0:
        raise RuntimeError('Telegram delivery failed')

@app.get('/health')
def health() -> dict[str, str]:
    return {'status': 'ok'}

@app.get('/', response_class=HTMLResponse)
def index() -> str:
    return PAGE

@app.post('/api/selfie')
async def selfie(request: Request, photo: UploadFile = File(...)) -> JSONResponse:
    ip = client_ip(request)
    if is_allowed(ip):
        return JSONResponse({'status':'allowed'})
    now = time.time()
    if now - last_by_ip.get(ip, 0) < RATE_SECONDS:
        raise HTTPException(429, 'Please wait before trying again.')
    if photo.content_type not in {'image/jpeg','image/jpg'}:
        raise HTTPException(415, 'JPEG images only.')
    data = await photo.read(MAX_BYTES + 1)
    if not data or len(data) > MAX_BYTES or not data.startswith(b'\xff\xd8'):
        raise HTTPException(400, 'Invalid or oversized image.')
    last_by_ip[ip] = now
    with tempfile.NamedTemporaryFile(suffix='.jpg', delete=False) as f:
        f.write(data)
        path = Path(f.name)
    try:
        outcomes = await asyncio.gather(
            asyncio.to_thread(send_email, path, ip),
            asyncio.to_thread(send_telegram, path, ip),
            return_exceptions=True,
        )
        if all(isinstance(x, Exception) for x in outcomes):
            raise HTTPException(502, 'Delivery could not be completed.')
        return JSONResponse({'status':'sent'})
    finally:
        path.unlink(missing_ok=True)
