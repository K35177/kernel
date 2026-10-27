#!/usr/bin/env python3
"""Opt-in real Firefox test; isolated profile, loopback only, synthetic proof.

Uses the installed esbuild/Firefox. Does not install dependencies, load a real
profile, disable browser sandboxing, or contact Telegram/production endpoints.
"""
import argparse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import shutil
import subprocess
import tempfile
import threading

ROOT = Path(__file__).resolve().parents[2]
HTML = b'''<!doctype html><script type="module">
import { ApiClient, loginDiagnostic } from '/client.js';
const report = {};
report.native_control_status = (await window.fetch('/control')).status;
try { await ({fetch:window.fetch}).fetch('/control'); report.wrong_receiver_rejected = false; }
catch(error) { report.wrong_receiver_rejected = true; report.wrong_receiver_exception = error.name === 'TypeError' ? 'TypeError' : 'OtherError'; }
const client = new ApiClient('/api', () => 'synthetic-local-browser-proof');
try { await client.initialize(); report.client_succeeded = true; }
catch(error) {
  const diagnostic = loginDiagnostic({sdkPresent:true,initDataPresent:true,telegramContextHint:true}, error);
  report.client_exception = error.name;
  report.code = diagnostic.code;
  report.stage = diagnostic.stage;
  report.endpoint = diagnostic.endpoint;
  report.status = diagnostic.httpStatus;
}
await window.fetch('/result', {method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(report)});
</script>'''


def run(expect):
    firefox = shutil.which('firefox')
    if not firefox: raise RuntimeError('Firefox not installed')
    with tempfile.TemporaryDirectory(prefix='kernel-fetch-browser.') as folder:
        folder = Path(folder)
        entry = folder/'entry.js'
        entry.write_text('export {ApiClient} from '+json.dumps(str(ROOT/'frontend/src/core/api.ts'))+';\n'
                         +'export {loginDiagnostic} from '+json.dumps(str(ROOT/'frontend/src/core/diagnostics.ts'))+';\n')
        subprocess.run([str(ROOT/'frontend/node_modules/.bin/esbuild'),str(entry),'--bundle','--format=esm',
            '--platform=browser','--outfile='+str(folder/'client.js'),'--log-level=error'],check=True)
        bundle = (folder/'client.js').read_bytes()
        finished = threading.Event(); reports = []; counters = {'auth_posts':0}
        class Handler(BaseHTTPRequestHandler):
            def log_message(self,*args): pass
            def reply(self,code,data,kind):
                self.send_response(code);self.send_header('Content-Type',kind)
                self.send_header('Content-Length',str(len(data)));self.end_headers();self.wfile.write(data)
            def do_GET(self):
                if self.path=='/': self.reply(200,HTML,'text/html')
                elif self.path=='/client.js': self.reply(200,bundle,'text/javascript')
                elif self.path=='/control': self.reply(200,b'{}','application/json')
                else: self.reply(404,b'','text/plain')
            def do_POST(self):
                raw = self.rfile.read(int(self.headers.get('Content-Length','0')))
                if self.path=='/api/auth/telegram':
                    assert json.loads(raw)=={'initData':'synthetic-local-browser-proof'}
                    counters['auth_posts']+=1;self.reply(401,b'{"detail":"synthetic rejection"}','application/json')
                elif self.path=='/result':
                    reports.append(json.loads(raw));self.reply(200,b'{}','application/json');finished.set()
                else:self.reply(404,b'','text/plain')
        server = ThreadingHTTPServer(('127.0.0.1',0),Handler)
        thread = threading.Thread(target=server.serve_forever,daemon=True);thread.start()
        profile = folder/'profile';profile.mkdir()
        (profile/'user.js').write_text('user_pref("network.captive-portal-service.enabled",false);\n'
            'user_pref("network.connectivity-service.enabled",false);\n'
            'user_pref("datareporting.healthreport.uploadEnabled",false);\n'
            'user_pref("toolkit.telemetry.enabled",false);\n')
        process = subprocess.Popen([firefox,'--headless','--no-remote','--profile',str(profile),
            f'http://127.0.0.1:{server.server_port}/'],stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
        try:
            if not finished.wait(40): raise RuntimeError('Browser report timeout')
            report = {**reports[0],**counters,'browser':'Firefox','production_contacted':False}
            print(json.dumps(report,sort_keys=True))
            expected_code = 'auth_rejected' if expect=='http401' else 'transport_or_cors_error'
            ok = report.get('native_control_status')==200 and report.get('wrong_receiver_rejected') is True \
                and report.get('code')==expected_code and report.get('stage')=='auth' \
                and report.get('endpoint')=='telegram_auth' and counters['auth_posts']==(1 if expect=='http401' else 0)
            if not ok: return 1
            print('REAL_BROWSER_FETCH_RECEIVER_'+expect.upper()+'_VERIFIED')
            return 0
        finally:
            process.terminate()
            try:process.wait(timeout=10)
            except subprocess.TimeoutExpired:process.kill();process.wait()
            server.shutdown();server.server_close();thread.join(timeout=5)


if __name__=='__main__':
    parser=argparse.ArgumentParser();parser.add_argument('--expect',choices=('http401','transport'),default='http401')
    raise SystemExit(run(parser.parse_args().expect))
