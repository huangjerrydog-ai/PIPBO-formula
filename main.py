"""Run this file in PyCharm, then open http://127.0.0.1:8775/."""
import argparse
import threading
import webbrowser
import uvicorn

if __name__=='__main__':
    parser=argparse.ArgumentParser(description='PIPBO 光敏材料研发管理系统')
    parser.add_argument('--port',type=int,default=8775)
    parser.add_argument('--no-browser',action='store_true')
    args=parser.parse_args()
    if not args.no_browser:
        threading.Timer(1.5,lambda:webbrowser.open(f'http://127.0.0.1:{args.port}/')).start()
    uvicorn.run('server:app',host='127.0.0.1',port=args.port,log_level='info')
