"""Run with Python stdlib; uses isolated SQLite/assets and a deterministic local API fixture."""
import base64
import copy
import json
import os
from pathlib import Path
import sqlite3
import sys
import tempfile
import threading
import time
import unittest
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'console'))
import batch_console as bc
import ai_bridge as bridge

PNG = base64.b64decode('iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+aZfoAAAAASUVORK5CYII=')
SCRIPT = {'title': '回归剧本', 'role_list': [], 'storyboard_list': [
    {'id':1,'scene':'学校','roles':[],'action':'开门','dialogue':'你好','duration':3},
    {'id':2,'scene':'学校','roles':[],'action':'走入','dialogue':'请进','duration':3}]}
TEXT = '```json\n' + json.dumps(SCRIPT, ensure_ascii=False) + '\n```'


class Fixture(BaseHTTPRequestHandler):
    requests = []
    def log_message(self, *args): pass
    def do_GET(self):
        self.send_response(200); self.end_headers()
        data = {'data':[{'id':'fixture'}]}
        if '/tasks/' in self.path:
            data = {'output':{'task_status':'SUCCEEDED','results':[{'url':'data:image/png;base64,'+base64.b64encode(PNG).decode()}]}}
        self.wfile.write(json.dumps(data).encode())
    def do_POST(self):
        payload = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
        self.requests.append((self.path, payload))
        if 'multimodal-generation' in self.path:
            result = {'output':{'task_id':'fixture-image'}}
        elif 'images/generations' in self.path:
            result = {'data':[{'b64_json':base64.b64encode(PNG).decode()}]}
        else:
            content = '{"ok":true,"issues":[]}' if isinstance(payload.get('messages',[{}])[0].get('content'), list) else TEXT
            messages = payload.get('messages') or (payload.get('input') or {}).get('messages') or []
            system = payload.get('system') or (messages[0].get('content') if messages else '')
            if system == bc.H3_EXPAND_SYSTEM:
                prompt = 'integrated_multimodal_description: ' + ('真实画面。'*150) + ' overall_soundscape: 门声 non_diegetic_music: 弦乐'
                content = json.dumps([{'id':1,'prompt':prompt}],ensure_ascii=False)
            if self.path.endswith('/messages'):
                result = {'content':[{'text':content}]}
            elif 'text-generation' in self.path:
                result = {'output':{'choices':[{'message':{'content':content}}]}}
            else:
                result = {'choices':[{'message':{'content':content}}]}
        self.send_response(200); self.end_headers()
        self.wfile.write(json.dumps(result).encode())


class ManualAI(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.saved = {k:getattr(bc,k) for k in ('DB_FILE','STATE_FILE','IMAGE_DIRS','_CONFIG','_VISION_ENV')}
        bc.DB_FILE = os.path.join(self.tmp.name,'console.db')
        bc.STATE_FILE = os.path.join(self.tmp.name,'legacy.json')
        bc.IMAGE_DIRS = [self.tmp.name]
        self.fixture = ThreadingHTTPServer(('127.0.0.1',0), Fixture)
        threading.Thread(target=self.fixture.serve_forever,daemon=True).start()
        url = 'http://127.0.0.1:' + str(self.fixture.server_port)
        bc._CONFIG = copy.deepcopy(bc._CONFIG_DEFAULTS)
        bc._CONFIG['llm']['local'].update(url=url,model='fixture')
        bc._CONFIG['image_gen']['local']['url'] = url
        bc._VISION_ENV = {'DASHSCOPE_BASE_URL':url+'/v1','VISION_MODEL':'fixture','DASHSCOPE_API_KEY':''}
        bc.save_state({'project':{'name':'回归项目'},'projects':{'回归项目':{'name':'回归项目'}}})
        bridge.init(bc.DB_FILE,bc._execute_ai_job,bc._finish_ai_job)
        self.http = ThreadingHTTPServer(('127.0.0.1',0), bc.Handler)
        self.http.daemon_threads = True
        threading.Thread(target=self.http.serve_forever,daemon=True).start()
        self.url = 'http://127.0.0.1:' + str(self.http.server_port)
        Fixture.requests.clear()

    def tearDown(self):
        self.pause()
        self.http.shutdown(); self.http.server_close()
        self.fixture.shutdown(); self.fixture.server_close()
        for key,value in self.saved.items(): setattr(bc,key,value)
        self.tmp.cleanup()

    def pause(self):
        bridge.STOP.set()
        deadline=time.time()+5
        while bridge.RUNNING and time.time()<deadline: time.sleep(.05)
        self.assertFalse(bridge.RUNNING)

    def post(self,path,body):
        req=urllib.request.Request(self.url+path,data=json.dumps(body).encode(),headers={'Content-Type':'application/json'})
        with urllib.request.urlopen(req) as response: return json.load(response)

    def job(self,path,body): return self.post(path,body)['ai_job']

    def until(self,fn):
        deadline=time.time()+8
        while time.time()<deadline:
            value=fn()
            if value: return value
            time.sleep(.05)
        self.fail('Timed out waiting for journal operation')

    def pending(self,jid,kind=None):
        return self.until(lambda: next((p for p in bridge.list_work()['pending'] if p['job']==jid and (not kind or p['kind']==kind)),None))

    def done(self,jid):
        result=self.until(lambda: (j if (j:=bridge.get_job(jid))['status']=='done' else None))
        self.assertEqual(result['code'],200,result)
        return result['result']

    def calls(self,jid):
        with bridge.connect() as con: return [dict(r) for r in con.execute('SELECT * FROM ai_calls WHERE job=? ORDER BY slot',(jid,))]

    def submit(self,p,text='',image=''):
        return self.post('/api/ai/submit',{'id':p['id'],'job':p['job'],'text':text,'image':image})

    def test_script_same_payload_parser_save_and_idempotence(self):
        body={'topic':'门口重逢','style':'现实','segments':2}
        api=self.job('/api/generate_script',body); a=self.done(api)
        request=json.loads(self.calls(api)[0]['payload'])
        self.assertEqual(request,Fixture.requests[-1][1])
        saved_api=bc.load_state()['project']['current_script']
        bc._CONFIG['llm']['invocation_mode']='manual'
        manual=self.job('/api/generate_script',body); p=self.pending(manual)
        self.assertEqual(request,p['request'])
        for message in request['messages']: self.assertIn(message['content'],p['text'])
        self.assertIn('max_tokens',p['text'])
        self.submit(p,TEXT); self.submit(p,TEXT)
        self.assertEqual(a,self.done(manual))
        self.assertEqual(saved_api,bc.load_state()['project']['current_script'])
        with self.assertRaises(ValueError): bridge.submit_result(p['id'],manual,'different')
        with self.assertRaises(ValueError): bridge.submit_result(p['id'],api,TEXT)
        self.assertEqual(len(self.calls(manual)),1)

    def test_image_same_payload_bytes_and_asset_qc_continuation(self):
        body={'prompt':'原有完整提示词，衣服为蓝色','filename':'fixture.png','kind':'scene','expected':{},
              '_ai_context':{'kind':'scene','key':'学校'}}
        api=self.job('/api/asset_gen',body); a=self.done(api)
        request=json.loads(self.calls(api)[0]['payload'])
        saved=copy.deepcopy(bc.load_state()['project'])
        bc._CONFIG['image_gen']['invocation_mode']='manual'
        bc._CONFIG['llm']['invocation_mode']='manual'
        manual=self.job('/api/asset_gen',body); p=self.pending(manual,'image_gen')
        self.assertEqual(request,p['request']); self.assertIn(body['prompt'],p['text'])
        self.assertEqual(p['references'],[])
        self.submit(p,image=base64.b64encode(PNG).decode())
        qc=self.pending(manual,'vision')
        self.assertEqual(json.loads(self.calls(api)[1]['payload']),qc['request'])
        self.assertTrue(qc['references'][0].startswith('data:image/png;base64,'))
        self.submit(qc,'{"ok":true,"issues":[]}')
        self.assertEqual(a,self.done(manual))
        self.assertEqual(Path(self.tmp.name,'fixture.png').read_bytes(),PNG)
        actual=bc.load_state()['project']
        for field in ('asset_imgs','asset_meta','asset_state'): self.assertEqual(saved[field],actual[field])

    def test_restart_pending_second_call_no_replay_first_result(self):
        bc._CONFIG['llm']['invocation_mode']='manual'
        jid=self.job('/api/expand_script',{'text':json.dumps(SCRIPT),'use_llm':True,'async':True})
        first=self.pending(jid)
        prompt='integrated_multimodal_description: '+('真实画面。'*150)+' overall_soundscape: 门声 non_diegetic_music: 弦乐'
        answer=json.dumps([{'id':1,'prompt':prompt}],ensure_ascii=False)
        self.submit(first,answer)
        second=self.until(lambda: next((p for p in bridge.list_work()['pending'] if p['job']==jid and p['id']!=first['id']),None))
        self.pause()
        # Mode switches never convert or discard an existing pending call.
        bc._CONFIG['llm']['invocation_mode']='api'
        bridge.init(bc.DB_FILE,bc._execute_ai_job,bc._finish_ai_job)
        restored=self.pending(jid)
        self.assertEqual(restored['id'],second['id'])
        self.assertEqual(restored['request'],second['request'])
        self.submit(restored,answer)
        result=self.done(jid)
        self.assertEqual(len(result['tasks']),2)
        self.assertEqual(len(bc.load_state()['project']['prompt_tasks']),2)
        self.assertEqual(len(self.calls(jid)),2)
        self.assertEqual(Fixture.requests,[])
        self.pause(); bridge.init(bc.DB_FILE,bc._execute_ai_job,bc._finish_ai_job)
        self.assertEqual(result,bridge.get_job(jid)['result']); self.assertEqual(len(self.calls(jid)),2)

    def test_project_switch_keeps_result_with_origin(self):
        bc._CONFIG['llm']['invocation_mode']='manual'
        jid=self.job('/api/generate_script',{'topic':'项目 A','segments':2})
        p=self.pending(jid)
        self.post('/api/project',{'name':'其他项目','fresh':True})
        self.submit(p,TEXT); self.done(jid)
        state=bc.load_state()
        self.assertEqual(state['project']['name'],'其他项目')
        self.assertNotIn('current_script',state['project'])
        self.assertEqual(state['projects']['回归项目']['current_script'],SCRIPT)

    def test_native_text_adapters_share_prepared_requests(self):
        for provider in ('claude','dashscope'):
            with self.subTest(provider=provider):
                cfg=bc._CONFIG['llm']; cfg['provider']='cloud'; cfg['provider_type']=provider
                cfg['cloud'].update(base_url=cfg['local']['url']+'/v1',model='fixture',enabled=True)
                cfg['invocation_mode']='api'
                api=self.job('/api/generate_script',{'topic':'接口回归','segments':2}); expected=self.done(api)
                cfg['invocation_mode']='manual'
                manual=self.job('/api/generate_script',{'topic':'接口回归','segments':2}); p=self.pending(manual)
                self.assertEqual(json.loads(self.calls(api)[0]['payload']),p['request'])
                self.submit(p,TEXT); self.assertEqual(expected,self.done(manual))


    def test_cloud_image_adapters_use_same_request_and_save(self):
        for provider in ('openai','dashscope'):
            with self.subTest(provider=provider):
                cfg=bc._CONFIG['image_gen']; cfg['provider']='cloud'; cfg['provider_type']=provider
                cfg['cloud'].update(base_url=cfg['local']['url']+'/v1',model='fixture',enabled=True)
                cfg['invocation_mode']='api'
                body={'prompt':'电影感场景，原有完整提示词','filename':provider+'.png'}
                api=self.job('/api/boogu_gen',body); expected=self.done(api)
                cfg['invocation_mode']='manual'
                manual=self.job('/api/boogu_gen',body); p=self.pending(manual,'image_gen')
                self.assertEqual(json.loads(self.calls(api)[0]['payload']),p['request'])
                self.submit(p,image=base64.b64encode(PNG).decode())
                self.assertEqual(expected,self.done(manual))
                self.assertEqual(Path(self.tmp.name,provider+'.png').read_bytes(),PNG)

    def test_api_expansion_keeps_original_async_contract(self):
        result=self.post('/api/expand_script',{'text':json.dumps(SCRIPT),'use_llm':True,'async':True})
        self.assertIn('task_id',result)
        self.assertTrue(result['async'])
        self.until(lambda: bc.EXPAND_JOBS[result['task_id']]['status'] in ('done','error'))


if __name__=='__main__': unittest.main(verbosity=2)
