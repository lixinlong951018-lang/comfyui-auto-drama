"""One Windows entry point for starting and stopping this project's local services."""
import argparse
import ctypes
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import time
import urllib.request
import webbrowser

import windows_lifecycle as win

ROOT = win.ROOT
RUNTIME = ROOT / 'console' / '.windows-runtime.json'
URL = 'http://127.0.0.1:8890/'

def listeners():
    result = subprocess.run(['netstat', '-ano', '-p', 'tcp'], capture_output=True,
                            creationflags=subprocess.CREATE_NO_WINDOW)
    rows = []
    for line in result.stdout.decode(errors='replace').splitlines():
        fields = line.split()
        if len(fields) == 5 and fields[0] == 'TCP' and fields[1].endswith(':8890') and fields[3] == 'LISTENING':
            rows.append(int(fields[4]))
    return sorted(set(rows))

def healthy():
    try:
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        with opener.open(URL, timeout=2) as response: return response.status == 200
    except (OSError, urllib.error.URLError): return False

def alive(info):
    current = win.identity(info['pid'])
    return current and current['created'] == info['created'] and (win.service_of(current) or win.registered_service(current)) == info['service']

def stop_group(group):
    for info in group: win.request_stop(info)
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline and any(alive(p) for p in group): time.sleep(.2)
    forced = []
    for info in group:
        if alive(info):
            win.force_stop(info); forced.append(info['pid'])
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline and any(alive(p) for p in group): time.sleep(.1)
    remaining = [p for p in group if alive(p)]
    if remaining: raise RuntimeError('未能关闭进程：' + json.dumps(remaining, ensure_ascii=False))
    if forced: print('正常停止超时，已按确认的项目身份结束 PID：', forced)

def stop():
    stop_group(win.processes())
    remaining = win.processes()
    ports = listeners()
    if remaining or ports:
        raise RuntimeError('仍有项目进程或 8890 被占用；不会结束身份不明的进程。项目进程：'
                           + json.dumps(remaining, ensure_ascii=False) + '；监听 PID：' + str(ports))
    RUNTIME.unlink(missing_ok=True)
    return '短剧控制台已关闭'

def spawn(service):
    path = ROOT / 'console' / (service + '.py')
    env = {**os.environ, 'PYTHONIOENCODING': 'utf-8', 'PYTHONUNBUFFERED': '1',
           'BATCH_CONSOLE_CONFIG': str(ROOT / 'config.json')}
    args = [sys.executable, '-B', '-u', str(path)]
    if service == 'batch_console': args.append('8890')
    with open(ROOT / 'console' / (service + '.log'), 'ab', buffering=0) as out, \
         open(ROOT / 'console' / (service + '.err.log'), 'ab', buffering=0) as err:
        subprocess.Popen(args, cwd=ROOT / 'console', stdin=subprocess.DEVNULL,
                         stdout=out, stderr=err, env=env,
                         creationflags=subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP)

def start(browser):
    group = win.processes()
    web = [p for p in group if p['service'] == 'batch_console']
    daemon = [p for p in group if p['service'] == 'chain_daemon']
    # Old duplicate instances do not hold the new component mutexes. Remove them
    # before repairing the pair, without touching any remote ComfyUI task.
    if len(web) > 1 or len(daemon) > 1:
        stop_group(group); group = []; web = []; daemon = []
    ports = listeners()
    if ports and not set(ports).issubset({p['pid'] for p in web}):
        raise RuntimeError('8890 被其他或无法确认身份的进程占用，未操作该进程。PID：' + str(ports))
    if web and not healthy():
        stop_group(web); web = []
    if not web: spawn('batch_console')
    deadline = time.monotonic() + 20
    while not healthy() and time.monotonic() < deadline: time.sleep(.2)
    if not healthy(): raise RuntimeError('控制台启动失败，请查看 console/batch_console.err.log')
    if not daemon: spawn('chain_daemon')
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        group = win.processes()
        if len([p for p in group if p['service'] == 'batch_console']) == 1 and len([p for p in group if p['service'] == 'chain_daemon']) == 1:
            break
        time.sleep(.2)
    else: raise RuntimeError('组件启动不完整，请查看 console/chain_daemon.err.log；进程：' + str(group))
    last_open = 0
    if RUNTIME.exists():
        try: last_open = json.loads(RUNTIME.read_text())['browser_opened']
        except (ValueError, KeyError): pass
    if browser and time.time() - last_open > 5:
        webbrowser.open(URL); last_open = time.time()
    RUNTIME.write_text(json.dumps({'browser_opened': last_open}), encoding='utf-8')
    return '短剧控制台已运行：' + URL

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('action', choices=('start', 'stop', 'status'))
    parser.add_argument('--ui', action='store_true')
    parser.add_argument('--no-browser', action='store_true')
    args = parser.parse_args()
    if args.action == 'status':
        print(json.dumps({'processes': win.processes(), 'listeners': listeners()}, ensure_ascii=False)); return
    guard = win.Mutex('control', 60000)
    failed = False
    try:
        if not guard.owned: raise RuntimeError('另一个启停操作仍在进行，请稍后重试。')
        message = start(not args.no_browser) if args.action == 'start' else stop()
        print(message)
    except Exception as exc:
        failed = True
        message = str(exc)
        print(message, file=sys.stderr)
    finally: guard.close()
    # Dialogs must not hold the control lock while waiting for a user click.
    if args.ui and (failed or args.action == 'stop'):
        ctypes.windll.user32.MessageBoxW(None, message, 'Auto Drama 启停失败' if failed else 'Auto Drama', 0x10 if failed else 0x40)
    if failed: raise SystemExit(1)

if __name__ == '__main__': main()
