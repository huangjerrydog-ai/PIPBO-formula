"""Validate the actual packaged executable in an isolated data directory."""
import http.cookiejar
import json
import os
from pathlib import Path
import secrets
import socket
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request


def main():
    exe=Path(sys.argv[1]).resolve()
    with tempfile.TemporaryDirectory(prefix='pipbo-exe-smoke-') as temp:
        env=dict(os.environ,PIPBO_DATA=str(Path(temp)/'data'))
        password=secrets.token_urlsafe(24)
        def start():
            with socket.socket() as s:s.bind(('127.0.0.1',0));port=s.getsockname()[1]
            p=subprocess.Popen([str(exe),'--no-browser','--port',str(port)],env=env,cwd=temp)
            base=f'http://127.0.0.1:{port}/'
            opener=urllib.request.build_opener(urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar()))
            def request(path,data=None,raw=False):
                body=None if data is None else json.dumps(data).encode()
                req=urllib.request.Request(base+path,data=body,headers={'Content-Type':'application/json','X-Pipbo':'1'})
                with opener.open(req,timeout=3) as r:
                    content=r.read();return content if raw else json.loads(content)
            for _ in range(90):
                if p.poll() is not None:raise RuntimeError('Packaged process exited before startup')
                try:request('api/status');return p,request
                except (OSError,urllib.error.URLError):time.sleep(1)
            p.terminate();p.wait(timeout=15);raise RuntimeError('Packaged startup timeout')
        def stop(p):
            p.terminate()
            try:p.wait(timeout=15)
            except subprocess.TimeoutExpired:p.kill();p.wait(timeout=10)
        p,request=start()
        try:
            assert b'PIPBO' in request('',raw=True)
            assert b'methodsPage' in request('static/app.js',raw=True)
            request('static/style.css',raw=True)
            assert request('api/status')['setup']
            request('api/setup',{'name':'build-smoke','password':password})
            request('api/login',{'name':'build-smoke','password':password})
            assert len(request('api/materials'))>=72
            request('api/reaction-methods',dict(name='Build verification',route='N',description='',stages=[dict(name='Step',temp_min_c=0,temp_max_c=10,hold_hours=0,inputs=[])]))
            assert len(request('api/reaction-methods'))==1
            key=(Path(env['PIPBO_DATA'])/'vault.key').read_bytes()
        finally:stop(p)
        p,request=start()
        try:
            assert not request('api/status')['setup']
            request('api/login',{'name':'build-smoke','password':password})
            assert len(request('api/reaction-methods'))==1
            assert (Path(env['PIPBO_DATA'])/'vault.key').read_bytes()==key
        finally:stop(p)
    print('Packaged executable smoke test passed: assets, timezone, database, auth, methods and restart persistence.')

if __name__=='__main__':main()
