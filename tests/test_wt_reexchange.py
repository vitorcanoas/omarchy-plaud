"""Strict -420 recovery: isolated state, scripted transport, no network."""
import contextlib
import io
import os
import pathlib
import sys
import tempfile
import time
import types
from unittest.mock import patch

os.environ['PLAUD_LINUX_HOME'] = tempfile.mkdtemp(prefix='plaud-reexchange-')
sys.path.insert(0, sys.argv[1] if len(sys.argv) > 1 else str(pathlib.Path(__file__).resolve().parent.parent))
transport = types.ModuleType('requests')
class RequestError(Exception): pass
transport.exceptions = types.SimpleNamespace(RequestException=RequestError)
queue, calls = [], []
class Reply:
    status_code = 200
    text = ''
    def __init__(self, data): self.data = data
    def json(self): return self.data

def request(method, url, **kw):
    calls.append((url, kw, api._flock_depth))
    if not queue: raise AssertionError('Unexpected extra request')
    item = queue.pop(0)
    if isinstance(item, Exception): raise item
    return Reply(item)
transport.request = request
transport.post = lambda url, **kw: request('POST', url, **kw)
transport.get = transport.put = lambda *a, **k: (_ for _ in ()).throw(AssertionError('Unexpected transport'))
sys.modules['requests'] = transport
from plaud_linux import plaud_api as api

rejected = {'status': -419}
expired = {'status': -420, 'data': None}
new = {'status': 0, 'data': {'workspace_token': 'new-wt', 'refresh_token': 'new-wrt', 'expires_in': 3600, 'refresh_expires_in': 7200}}
seed = {'ut': 'fake-ut', 'wt': 'old-wt', 'wrt': 'old-wrt', 'ws_id': 'fake-ws', 'api_domain': api.DEFAULT_API, 'wt_exp': int(time.time())+9999}
results = []
def check(name, ok):
    results.append(bool(ok))
    print(('PASS' if ok else 'FAIL')+'  '+name)

def case(replies, missing=(), save_error=False):
    queue[:] = replies
    calls.clear()
    tokens = {k:v for k,v in seed.items() if k not in missing}
    api._save_tokens(tokens)
    c = api.PlaudClient()
    error = None
    output = None
    with contextlib.redirect_stdout(io.StringIO()):
        try:
            with patch.object(api, '_save_tokens', side_effect=OSError('disk unavailable')) if save_error else contextlib.nullcontext():
                output = c._biz('POST', '/file/list', json={})
        except Exception as exc: error = type(exc).__name__
    return c, output, error, tokens

c, out, err, before = case([rejected, expired, new, {'status':0}])
check('1 -419 then -420 recovers and retries once', err is None and out == {'status':0} and len(calls)==4)
check('2 existing W3/W2 contract and locking', len(calls)==4 and [x[1]['headers'].get('Authorization') for x in calls]==['Bearer old-wt','Bearer old-wrt','Bearer fake-ut','Bearer new-wt'] and calls[1][0].endswith('/workspace/refresh/fake-ws') and calls[2][0].endswith('/workspace/token/fake-ws') and all(x[2]>0 and x[1]['json']=={} for x in calls[1:3]))
saved=api._load_tokens()
check('3 recovered credentials persisted with UT and private mode', saved.get('wt')=='new-wt' and saved.get('wrt')=='new-wrt' and saved.get('ut')=='fake-ut' and api.TOKENS_FILE.stat().st_mode & 0o777 == 0o600)
c,out,err,before=case([rejected,expired,{'status':-1}])
check('4 rejected mint preserves prior credentials and original error', err is None and out==rejected and len(calls)==3 and c.tokens==before and api._load_tokens()==before)
c,out,err,before=case([rejected,expired],missing=('ut',))
check('5 missing UT does not exchange', err is None and out==rejected and len(calls)==2 and api._load_tokens()==before)
c,out,err,before=case([rejected,{'status':-1,'data':{}}])
check('6 other refresh failures do not exchange', err is None and out==rejected and len(calls)==2)
c,out,err,before=case([rejected,new,{'status':0}])
check('7 ordinary refresh remains one refresh and retry', err is None and out=={'status':0} and len(calls)==3 and not any('/workspace/token/' in x[0] for x in calls))
c,out,err,before=case([rejected,expired,RequestError('connection failed')])
check('8 exchange network error preserves original failure', err is None and out==rejected and len(calls)==3 and c.tokens==before and api._load_tokens()==before)
bad={'status':0,'data':dict(new['data'],expires_in='bad-expiry')}
malformed_ok=[]
for reply in [bad, {'status':-1,'data':None}]:
    c,out,err,before=case([rejected,expired,reply])
    malformed_ok.append(err is None and out==rejected and c.tokens==before and api._load_tokens()==before and len(calls)==3)
check('9 malformed mint rolls back partial in-memory mutation', all(malformed_ok))
c,out,err,before=case([rejected,expired,new],save_error=True)
check('10 save failure does not report recovery or lose prior disk state', err is None and out==rejected and c.tokens==before and api._load_tokens()==before and len(calls)==3)
c,out,err,before=case([rejected,expired,new,rejected])
check('11 a second business rejection ends without another exchange', err is None and out==rejected and len(calls)==4)
checks=[]
for missing in [('ws_id',),('wrt',)]:
    c,out,err,before=case([rejected],missing=missing)
    checks.append(err is None and out==rejected and len(calls)==1 and api._load_tokens()==before)
check('12 missing workspace or refresh credential fails without auth call', all(checks))
print(f'{sum(results)} passed, {len(results)-sum(results)} failed')
sys.exit(0 if all(results) else 1)
