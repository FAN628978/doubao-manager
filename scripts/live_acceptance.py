#!/usr/bin/env python3
"""Windows-side acceptance for an *authorized, manually signed-in* Doubao web account.

This does not bypass login, solve challenges, cycle accounts, inspect private APIs,
or call unapproved backend endpoints. A PASS requires the running manager to archive
real video bytes. This script never resends a task with an unknown submission state.
"""
from __future__ import annotations
import argparse
import hashlib
import json
import os
import shutil
import subprocess
import sys
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlparse
from urllib.request import Request, urlopen

ROOT = Path(__file__).resolve().parents[1]
RESULTS = ROOT / 'data' / 'acceptance_reports'
TERMINAL = {'downloaded', 'needs_review', 'failed'}


def now(): return datetime.now(timezone.utc).isoformat(timespec='seconds')


def sha(b): return hashlib.sha256(b).hexdigest()


def fail(msg):
    print(f'[ERROR] {msg}', file=sys.stderr)
    return 1


def request(base, path, method='GET', body=None, content_type=None, key='', timeout=40):
    url = base + path
    data = body
    headers = {'Accept': 'application/json'}
    if key: headers['X-API-Key'] = key
    if isinstance(body, (dict, list)):
        data = json.dumps(body, ensure_ascii=False).encode('utf-8')
        headers['Content-Type'] = 'application/json'
    elif content_type:
        headers['Content-Type'] = content_type
    try:
        with urlopen(Request(url, data=data, headers=headers, method=method), timeout=timeout) as res:
            return res.read(), res.status
    except HTTPError as e:
        detail = e.read().decode('utf-8', errors='replace')[:800]
        raise RuntimeError(f'HTTP {e.code}: {detail}') from e
    except URLError as e:
        raise RuntimeError(f'无法连接到本机管理台：{e}') from e


def api(base, path, method='GET', body=None, key=''):
    raw, _ = request(base, path, method=method, body=body, key=key)
    return json.loads(raw)


def upload(base, path: Path, key):
    content = path.read_bytes()
    if len(content) > 15 * 1024 * 1024: raise ValueError(f'图片不能超过15MB: {path}')
    boundary = 'db-accept-' + uuid.uuid4().hex
    name = path.name.replace('"', '_').replace('\\', '_').replace('\r','').replace('\n','')
    body = ('--' + boundary + '\r\n'
            + f'Content-Disposition: form-data; name="file"; filename="{name}"\r\n'
            + 'Content-Type: application/octet-stream\r\n\r\n').encode('utf-8')
    body += content + ('\r\n--' + boundary + '--\r\n').encode('utf-8')
    raw, _ = request(base, '/api/v1/videos/assets', 'POST', body,
                     content_type='multipart/form-data; boundary=' + boundary, key=key)
    return json.loads(raw)['asset_id']


def check_media(blob):
    if len(blob) < 256: raise ValueError('归档文件太小')
    if blob[4:8] == b'ftyp': return '.mp4'
    if blob[:4] == b'\x1aE\xdf\xa3': return '.webm'
    raise ValueError('归档文件不是有效的 MP4/WEBM 容器')


def media_probe(path:Path):
    if not shutil.which('ffprobe'):
        return {'ffprobe': 'not_installed', 'note': '仅执行容器头检查'}
    result = subprocess.run(['ffprobe','-v','error','-select_streams','v:0','-show_entries',
                             'stream=codec_type,width,height,duration','-of','json',str(path)],
                            text=True,capture_output=True,timeout=30)
    if result.returncode: raise ValueError('ffprobe 视频流校验失败: ' + result.stderr[:300])
    info = json.loads(result.stdout)
    if not any(stream.get('codec_type') == 'video' for stream in info.get('streams', [])):
        raise ValueError('没有检测到视频流')
    return {'ffprobe':'pass', 'streams':info.get('streams', [])}


def save_report(report):
    RESULTS.mkdir(parents=True, exist_ok=True)
    name = (report.get('job_id') or 'not_created').replace('/','_').replace('\\','_')
    dest = RESULTS / (name + '.json')
    dest.write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8')
    return dest


def arguments(argv=None):
    p = argparse.ArgumentParser(description='已登录 Windows 豆包网页版视频生成实机验收工具（需要人工确认）')
    p.add_argument('--base-url',default='http://127.0.0.1:8765',help='本机管理台地址')
    g=p.add_mutually_exclusive_group(required=True)
    g.add_argument('--account-id',help='开始一次新的实机生成，需要预先手动登录')
    g.add_argument('--job-id',help='仅恢复查询已有任务，不重新发送生成请求')
    p.add_argument('--prompt',default='电影级3D国漫，雨后的古风街道，镜头缓慢推进。')
    p.add_argument('--prompt-file',type=Path)
    p.add_argument('--mode',choices=['text2video','image2video','first_last_frame','reference_images'],default='text2video')
    p.add_argument('--duration',type=int,choices=[4,10,15],default=4)
    p.add_argument('--ratio',choices=['16:9','9:16','1:1'],default='16:9')
    p.add_argument('--first-frame',type=Path)
    p.add_argument('--last-frame',type=Path)
    p.add_argument('--reference',type=Path,action='append',default=[])
    p.add_argument('--project',default='Windows 实机验收')
    p.add_argument('--episode',default='EP01')
    p.add_argument('--shot',default='SHOT01')
    p.add_argument('--idempotency-key',help='重启恢复时重复调用不会新建任务；另可直接用 --job-id')
    p.add_argument('--timeout',type=int,default=1200)
    p.add_argument('--poll',type=float,default=5)
    p.add_argument('--yes',action='store_true',help='确认使用选定账号的生成额度（建议手动确认）')
    return p.parse_args(argv)


def main(argv=None):
    a = arguments(argv)
    parsed = urlparse(a.base_url)
    if parsed.scheme != 'http' or parsed.hostname not in ('127.0.0.1', 'localhost'):
        return fail('出于隐私安全，只接受本机 localhost/127.0.0.1 的 HTTP 管理台')
    base=a.base_url.rstrip('/')
    key=os.getenv('DOUBAO_LOCAL_API_KEY','')
    report={'started_at':now(),'mode':a.mode,'provider':'doubao_web',
            'live_website_verified':False,'result_source':None, 'job_id':a.job_id,
            'phases':[],'status':'not_started'}
    try:
        health=api(base,'/api/health',key=key)
        if not health.get('ok'): raise RuntimeError('健康检查未通过')
        if a.job_id:
            jobid = a.job_id
            print('[RESUME] 仅查询已存在任务，不重新提交。')
        else:
            accounts=api(base,'/api/accounts',key=key)
            if not any(x['id']==a.account_id for x in accounts): raise ValueError('account_id 不存在或已归档')
            prompt=a.prompt_file.read_text(encoding='utf-8').strip() if a.prompt_file else a.prompt.strip()
            if not prompt: raise ValueError('提示词为空')
            assets=[]
            if a.mode == 'image2video':
                if not a.first_frame: raise ValueError('图生视频必须提供 --first-frame')
                assets=[a.first_frame]
            elif a.mode == 'first_last_frame':
                if not a.first_frame or not a.last_frame: raise ValueError('首尾帧必须提供两张图片')
                assets=[a.first_frame,a.last_frame]
            elif a.mode == 'reference_images':
                if not 1 <= len(a.reference) <= 9: raise ValueError('参考图数量必须是1～9张')
                assets=list(a.reference)
            if not all(x.is_file() for x in assets): raise ValueError('部分图片路径不存在')
            print(f'[INFO] 账号={a.account_id} 模式={a.mode} 时长={a.duration} 画幅={a.ratio} 图片={len(assets)}')
            print('[WARNING] 即将操作豆包网页版，可能消耗账号正常生成额度；失败不会自动更换账号。')
            if not a.yes and input('已确认账号登录及额度并允许提交？输入 YES 继续：').strip() != 'YES':
                print('已取消，未创建任务');return 2
            payload={'account_id':a.account_id, 'project_id':a.project,
                     'episode_id':a.episode,'shot_id':a.shot, 'prompt':prompt,
                     'mode':a.mode,'duration':a.duration,'ratio':a.ratio,
                     'idempotency_key':a.idempotency_key or 'live-accept-'+uuid.uuid4().hex,
                     'auto_start':False}
            for f in assets:
                print('[ASSET] 上传:',f.name)
                asset=upload(base,f,key)
                if a.mode=='image2video': payload['first_frame_asset_id']=asset
                elif a.mode=='first_last_frame':
                    if f==a.first_frame and 'first_frame_asset_id' not in payload: payload['first_frame_asset_id']=asset
                    else: payload['last_frame_asset_id']=asset
                else: payload.setdefault('reference_asset_ids',[]).append(asset)
            report['idempotency_key'] = payload['idempotency_key']
            report['prompt_sha256'] = sha(prompt.encode('utf-8'))
            # Write a receipt even before the request, to facilitate safe manual investigation.
            report['status']='about_to_create'
            save_report(report)
            created=api(base,'/api/v1/videos/generations','POST',payload,key=key)
            jobid=created['id'];report['job_id']=jobid;report['status']=created['status']
            print('[CREATED]',jobid)
            save_report(report)
            if created['status']=='queued':
                api(base,'/api/v1/videos/generations/'+quote(jobid)+'/start','POST',{},key=key)
        deadline=time.monotonic()+max(1,a.timeout)
        previous=None
        while time.monotonic()<deadline:
            job=api(base,'/api/v1/videos/generations/'+quote(jobid),key=key)
            status=job['status']
            if status!=previous:
                print(f'[{now()}] {jobid}: {status}')
                report['phases'].append({'time':now(),'state':status,'code':job['web'].get('error_code','')})
                previous=status;report['status']=status
                save_report(report)
            if status in TERMINAL: break
            time.sleep(max(0.2,a.poll))
        else:
            report['status']='pending_timeout'
            raise RuntimeError('验收脚本等待超时；任务可能仍在运行。请用 --job-id 查询，切勿重新建单')
        if report['status']!='downloaded':
            print('[NEEDS_REVIEW]',job.get('error_message') or job['web'].get('error_code'))
            print('请人工处理/查看日志；不能自动重试生成。')
            return 3
        source=job['web'].get('result_source') or 'unknown'
        blob,_=request(base,'/api/jobs/'+quote(jobid)+'/video',key=key,timeout=120)
        extension=check_media(blob)
        RESULTS.mkdir(parents=True,exist_ok=True)
        video=RESULTS/(jobid+extension)
        video.write_bytes(blob)
        probe=media_probe(video)
        report.update(status='downloaded',result_source=source,media_sha256=sha(blob),
                      media_bytes=len(blob),media_path=str(video),probe=probe,
                      live_website_verified=(source=='web_automated'))
        print('[VIDEO]',video,'bytes=',len(blob),'sha256=',sha(blob))
        print('[RESULT]','LIVE_AUTO_PASS' if report['live_website_verified'] else 'MANUAL_FILE_VERIFIED')
        return 0
    except (ValueError,RuntimeError,OSError,KeyboardInterrupt) as exc:
        report['exception']=str(exc)[:500]
        if report['status'] not in ('pending_timeout','needs_review'):
            report['status']='error_or_interrupted'
        print('[ERROR]',exc,file=sys.stderr)
        return 1
    finally:
        report['ended_at']=now()
        print('[REPORT]',save_report(report))


if __name__=='__main__':
    sys.exit(main())
