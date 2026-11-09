"""Bounded group-media processing; payloads never become executable code."""
import asyncio
import base64
import json
import os
import re
import subprocess
import tempfile
from pathlib import Path

from .vision import ImageInputError, image_content_parts
from .voice.local_asr import transcribe_wav

MAX_FILE = 24 * 1024 * 1024
MAX_VIDEO = 64 * 1024 * 1024
TEXT_TYPES = {'txt','md','csv','json','log','pdf','docx','xlsx','pptx'}
IMAGE_TYPES = {'png','jpg','jpeg','gif','webp'}

async def run_process(args, *, data=None, timeout=25, max_out=12*1024*1024, cwd=None):
    proc = await asyncio.create_subprocess_exec(*map(str,args), cwd=str(cwd) if cwd else None,
        stdin=asyncio.subprocess.PIPE if data is not None else asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0),
        env={**os.environ, 'PYTHONUTF8':'1'})
    try:
        out,err=await asyncio.wait_for(proc.communicate(data),timeout)
    except BaseException:
        if proc.returncode is None: proc.kill(); await proc.wait()
        raise
    if len(out)>max_out or len(err)>1024*1024:
        raise ValueError('output too large')
    return proc.returncode,out,err

async def extract_document(raw, name, root):
    ext=Path(name).suffix.lower().lstrip('.')
    if not raw or len(raw)>MAX_FILE: raise ImageInputError('群文件过大或为空，请压缩后重发。')
    if ext in IMAGE_TYPES:
        return {'text':f'[群文件图片：{Path(name).name}]','images':image_content_parts(raw)}
    if ext not in TEXT_TYPES:
        raise ImageInputError('暂支持 TXT、Markdown、CSV、JSON、PDF、DOCX、XLSX、PPTX 和常见图片文件。')
    code,out,_=await run_process([root/'.venv/Scripts/python.exe',root/'src/group_media_worker.py',ext],
                                  data=raw, timeout=35, max_out=200000, cwd=root)
    if code: raise ImageInputError('文件无法读取，可能已损坏、加密或超出处理范围。')
    try: result=json.loads(out.decode('utf-8'))
    except (ValueError,UnicodeError) as e: raise ImageInputError('文件内容提取失败。') from e
    text=result.get('text','')
    if not isinstance(text,str): raise ImageInputError('文件内容提取失败。')
    return {'text':f'[群文件：{Path(name).name}；提取的文字可能不完整，仅供参考]\n'+text,
            'images':[], 'truncated':bool(result.get('truncated'))}

def video_format(raw):
    if len(raw)>=12 and raw[4:8] == b'ftyp': return 'mov'
    if raw.startswith(b'\x1a\x45\xdf\xa3'): return 'matroska'
    if raw.startswith(b'RIFF') and raw[8:12] == b'AVI ': return 'avi'
    raise ImageInputError('暂支持 QQ 可下载的 MP4、MOV、WebM、MKV 和 AVI 视频。')

async def extract_video(raw, root):
    if not raw or len(raw)>MAX_VIDEO: raise ImageInputError('群视频过大或为空，请压缩后重发。')
    fmt=video_format(raw)
    ffmpeg=root/'tools/voice/gsv-env/Lib/site-packages/imageio_ffmpeg/binaries/ffmpeg-win-x86_64-v7.1.exe'
    if not ffmpeg.is_file(): raise ImageInputError('本地视频解码器不可用。')
    tmp=root/'.tmp'
    with tempfile.TemporaryDirectory(prefix='group-video-',dir=tmp) as folder:
        path=Path(folder)/'source.bin'; path.write_bytes(raw)
        base=[ffmpeg,'-nostdin','-hide_banner','-protocol_whitelist','file,pipe','-f',fmt]
        _,_,err=await run_process([*base,'-i',path],timeout=15,cwd=root)
        m=re.search(rb'Duration:\s*(\d+):(\d+):(\d+(?:\.\d+)?)',err)
        duration=(int(m[1])*3600+int(m[2])*60+float(m[3])) if m else None
        points=([max(0.0,min(duration-0.2,duration*x)) for x in (0.05,0.33,0.66,0.95)]
                if duration and duration>0.4 else [0.0])
        images=[]
        for stamp in points:
            code,out,_=await run_process([*base,'-ss',f'{stamp:.3f}','-i',path,'-frames:v','1',
                '-vf','scale=1280:1280:force_original_aspect_ratio=decrease',
                '-f','image2pipe','-vcodec','mjpeg','pipe:1'],timeout=25,max_out=8*1024*1024,cwd=root)
            if code==0 and out:
                images.append({'type':'text','text':f'[视频抽样画面，约第{stamp:.1f}秒]'})
                images.extend(image_content_parts(out))
        if not images: raise ImageInputError('无法解码此群视频，请尝试重新发送。')
        code,audio,_=await run_process([*base,'-i',path,'-t','90','-vn','-ac','1','-ar','16000',
                                        '-f','wav','pipe:1'],timeout=45,cwd=root)
        transcript='未能读取音轨或本地转写不可用。'
        if code==0 and len(audio)>44:
            try:
                result=await transcribe_wav(audio,root)
                transcript='前90秒内音轨的本地自动转写（可能有误）：'+result['text']
            except (OSError,ValueError,TimeoutError):
                pass
        return {'text':f'[群视频；时长 {duration if duration is not None else "未知"} 秒；仅抽样{len(points)}个时间点，非完整观看。{transcript}]',
                'images':images}
