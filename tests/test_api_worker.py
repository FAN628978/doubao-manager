"""Real Chromium + API worker end-to-end on an in-memory synthetic DOM.

Confirms REST create -> background execution -> downloaded without a live Doubao account.
"""
import asyncio
import sqlite3
import subprocess
import sys
from pathlib import Path

import httpx
from fastapi import FastAPI
from playwright.async_api import Page, APIRequestContext, async_playwright

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from test_v3 import HTML, launch_chromium
from web_api import mount_video_api


def test_api_create_worker_and_archive_real_chromium(tmp_path,monkeypatch):
    monkeypatch.setenv('DOUBAO_TEST_MODE','1')
    monkeypatch.setenv('DOUBAO_TEST_PAGE','http://localhost:7999/fixture')
    monkeypatch.setenv('DOUBAO_POLL_SECONDS','0.08')
    monkeypatch.setenv('DOUBAO_RESULT_TIMEOUT','8')
    monkeypatch.setenv('DOUBAO_LOCAL_API_KEY','integration-test-only')
    output = tmp_path/'fixture.mp4'
    p=subprocess.run(['ffmpeg','-v','error','-y','-f','lavfi','-i','color=c=green:s=320x180:d=1',
                      '-c:v','mpeg4',str(output)],capture_output=True,text=True)
    assert p.returncode==0,p.stderr
    blob=output.read_bytes()
    dbfile=tmp_path/'integration.sqlite'
    def db():
        conn=sqlite3.connect(dbfile)
        conn.row_factory=sqlite3.Row
        conn.execute('PRAGMA foreign_keys=ON')
        return conn
    with db() as c:
        c.executescript('''CREATE TABLE accounts(id TEXT PRIMARY KEY,archived INTEGER NOT NULL DEFAULT 0);
          CREATE TABLE jobs(id TEXT PRIMARY KEY,project_id TEXT,episode_id TEXT,shot_id TEXT,
          prompt TEXT,account_id TEXT,status TEXT,created_at TEXT,updated_at TEXT,
          error_message TEXT DEFAULT '',video_path TEXT);''')
        c.execute('INSERT INTO accounts(id,archived) VALUES(?,0)',('account_1',))
    def get_account(id):
        with db() as c: return c.execute('SELECT * FROM accounts WHERE id=?',(id,)).fetchone()
    def get_job(id):
        with db() as c: return c.execute('SELECT * FROM jobs WHERE id=?',(id,)).fetchone()
    def serialize(row):return dict(row)
    bridge=FastAPI()
    async def goto_mock(self,*args,**kwargs): await self.set_content(HTML)
    monkeypatch.setattr(Page,'goto',goto_mock)
    class Response:
        ok=True;status=200
        headers={'content-length':str(len(blob))}
        async def body(self):return blob
    async def get_mock(self,*args,**kwargs):return Response()
    monkeypatch.setattr(APIRequestContext,'get',get_mock)

    async def run():
        async with async_playwright() as pw:
            browser=await launch_chromium(pw)
            ctx=await browser.new_context()
            async def fake_browser(account_id):
                assert account_id=='account_1'
                return ctx
            mount_video_api(bridge,db=db,get_account=get_account,get_job=get_job,
                            browser=fake_browser,base_dir=tmp_path,downloads_dir=tmp_path/'data'/'videos',serialize=serialize)
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=bridge),base_url='http://local') as client:
                # Wrong key must be rejected; wrong result cannot be declared by normal API.
                payload={'account_id':'account_1','project_id':'测试项目','episode_id':'1','shot_id':'1',
                         'prompt':'城墙推镜','mode':'text2video','ratio':'16:9','duration':4,
                         'idempotency_key':'unique1','auto_start':True}
                assert (await client.post('/api/v1/videos/generations',json=payload)).status_code==401
                h={'X-API-Key':'integration-test-only'}
                r=await client.post('/api/v1/videos/generations',json=payload,headers=h)
                assert r.status_code==202,r.text
                jobid=r.json()['id']
                state=None
                for _ in range(90):
                    state=(await client.get(f'/api/v1/videos/generations/{jobid}',headers=h)).json()
                    if state['status'] in ('downloaded','needs_review'):break
                    await asyncio.sleep(.12)
                assert state['status']=='downloaded',state
                assert state['web']['result_source']=='web_automated'
                video=tmp_path/state['video_path']
                assert video.read_bytes()==blob
                assert state['video_file_url'].endswith('/video')
                ev=(await client.get(f'/api/v1/videos/generations/{jobid}/events',headers=h)).json()
                statuses=[e['status'] for e in ev]
                assert 'configuring' in statuses and 'submitting' in statuses
                assert 'generating' in statuses and 'downloading' in statuses
                # Duplicated request cannot create/submit a second job.
                second=await client.post('/api/v1/videos/generations',json=payload,headers=h)
                assert second.json()['id']==jobid
                with db() as c: assert c.execute('SELECT COUNT(*) FROM jobs').fetchone()[0]==1
            await browser.close()
    asyncio.run(run())
