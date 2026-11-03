"""Offline Studio readiness, cookie import, late chat video and delivery regressions."""
import json, sys, tempfile, zipfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT))
import engine as module
from engine import MuseEngine, MuseGenerationError, ESSENTIAL_COOKIES
from store import ESSENTIAL_COOKIES as STORE_COOKIES

class Clock:
    def __init__(self): self.now=0
    def time(self): return self.now
    monotonic=time
    def sleep(self,n): self.now+=n

with tempfile.TemporaryDirectory() as tmp:
    cfg=SimpleNamespace(profile_dir=tmp,login_wait=2,chat_timeout=90)
    e=MuseEngine(cfg)
    page=SimpleNamespace(js=lambda s:'Connecting...',send=lambda *a:None)
    e._open_page=lambda:page;e._apply_cookies=lambda *a:None
    e.renew_session_http=lambda *a,**k:{}
    e._wait_ws_ready=lambda *a,**k:False
    try:e.ensure_page({'hatch_sess':'fixture'})
    except MuseGenerationError as exc:assert 'Connecting' in str(exc)
    else:raise AssertionError('Disconnected composer must not be accepted')
    assert e.page is None
    e.page=page
    try:e._send('Do not submit')
    except MuseGenerationError as exc:assert 'chưa được gửi' in str(exc)
    else:raise AssertionError('Send must recheck readiness')

    for delay in (15,30,60):
        e=MuseEngine(cfg);clock=Clock();result={};submissions=[]
        e.page=SimpleNamespace(js=lambda s:'https://muse.ai/thread/new_video')
        e.ensure_page=lambda *a,**k:None;e.reset_thread=lambda **k:None
        e._send=lambda p:submissions.append(p) or 'clicked'
        e._poll_chat=lambda:(0,'',False) if clock.now==0 else (1,'Your video is ready.',False)
        e.attachments=lambda:[] if clock.now<delay else [{'hasVideo':True,'vSrc':'blob:new','src':'blob:new'}]
        e.retrieve_attachment=lambda a,k:{'filename':'new.mp4','kind':k}
        with patch.object(module,'time',clock):
            text=''.join(e.chat_stream({},'Make video',on_result=result.update))
        assert clock.now>=delay and result['media'][0]['filename']=='new.mp4'
        assert len(submissions)==1 and text=='Your video is ready.'

    e=MuseEngine(cfg);clock=Clock();e.page=SimpleNamespace(js=lambda s:'Still sending')
    e.ensure_page=lambda *a,**k:None;e.reset_thread=lambda **k:None;e._send=lambda p:'clicked'
    e._poll_chat=lambda:(0,'',True)
    with patch.object(module,'time',clock):
        try:list(e.chat_stream({},'Hello'))
        except MuseGenerationError as exc:assert 'Delivery not confirmed' in str(exc) and clock.now<32
        else:raise AssertionError('Undelivered chat must fail promptly')
assert ESSENTIAL_COOKIES==STORE_COOKIES==('hatch_sess',)
with zipfile.ZipFile(ROOT/'muse2api-extension.zip') as z:
    assert "const ESSENTIAL = ['hatch_sess']" in z.read('muse2api-extension/popup.js').decode()
    assert json.loads(z.read('muse2api-extension/manifest.json'))['version']=='1.0.2'
print('PASS: disconnected-page cleanup/send guard; chat late video 15/30/60s; unconfirmed delivery; optional cookies; packaged extension')
