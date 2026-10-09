"""Only these fixed scripts execute remotely; user messages are serialized data."""

import base64
import json
from pathlib import Path
from textwrap import indent

from .jupyter import MIME


def encoded(value):
    return base64.b64encode(json.dumps(value, ensure_ascii=False).encode()).decode()


def envelope(payload):
    return (
        f"import base64, json\nC=json.loads(base64.b64decode({encoded(payload)!r}))\n"
        + f"from IPython.display import display\ndef emit(event): display({{{MIME!r}:event}},raw=True)\n"
        + "import urllib.request\n"
        + "class NoModelRedirect(urllib.request.HTTPRedirectHandler):\n"
        + "    def redirect_request(self, *args, **kwargs): return None\n"
        + "model_http=urllib.request.build_opener(urllib.request.ProxyHandler({}),NoModelRedirect())\n"
    )


def bootstrap_script(config):
    server = Path(__file__).with_name("server.py").read_text()
    body = r"""
import os, sys, subprocess, time, urllib.request, urllib.error, signal, hashlib, fcntl, re
from pathlib import Path
root=Path('/content/spas') / C['deployment_id']
root.mkdir(parents=True,exist_ok=True,mode=0o700)
root.chmod(0o700)
guard=(root/'setup.lock').open('a')
fcntl.flock(guard,fcntl.LOCK_EX)
def terminate(process):
    if process.poll() is not None: return
    try:
        os.killpg(process.pid,signal.SIGTERM)
        process.wait(timeout=5)
    except subprocess.TimeoutExpired:
        os.killpg(process.pid,signal.SIGKILL)
        process.wait()
    except ProcessLookupError: pass
class SetupFailure(RuntimeError):
    def __init__(self, step, result):
        output=(result.stdout+'\n'+result.stderr).lower()
        reason='subprocess_failed'
        for marker,value in [('ensurepip','ensurepip_missing'),('no module named pip','pip_missing'),('no space left on device','disk_full'),('no matching distribution found','package_unavailable'),('resolutionimpossible','dependency_conflict'),('connectionerror','network_error'),('connection refused','network_error'),('temporary failure in name resolution','network_error')]:
            if marker in output:
                reason=value
                break
        self.diagnostic={'step':step,'python':sys.version.split()[0],'exit_code':result.returncode,'reason':reason}
        package=re.search(r'No matching distribution found for ([A-Za-z0-9_.<=>!+\-]+)',result.stderr)
        if package: self.diagnostic['package']=package.group(1)[:100]
        super().__init__('SPAS setup subprocess failed')
def run_child(arguments, *, step='prepare', input=None, check=True):
    process=subprocess.Popen(arguments,stdin=subprocess.PIPE if input is not None else subprocess.DEVNULL,stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True,start_new_session=True)
    try: out,err=process.communicate(input=input)
    except BaseException:
        terminate(process)
        raise
    result=subprocess.CompletedProcess(arguments,process.returncode,out,err)
    if check and process.returncode: raise SetupFailure(step,result)
    return result
python=root/'venv/bin/python'
server_file=root/'server.py'
state_file=root/'process.json'
configuration={key:C[key] for key in ('model_repo','revision','precision','max_new_tokens','max_context','port','variant')}
digest=hashlib.sha256(json.dumps(configuration,sort_keys=True).encode()).hexdigest()
def models():
    request=urllib.request.Request(f"http://127.0.0.1:{C['port']}/v1/models",headers={'Authorization':'Bearer '+C['server_key']})
    with model_http.open(request,timeout=3) as response: return json.load(response)
existing=json.loads(state_file.read_text()) if state_file.exists() else {}
reuse=False
if existing.get('digest')==digest and existing.get('pid'):
    try: reuse=any(item['id']==C['model_repo'] for item in models()['data'])
    except Exception: pass
if reuse:
    emit({'stage':'READY','snapshot_commit':existing.get('snapshot_commit'),'reused':True})
else:
    if existing.get('pid'):
        pid=existing['pid']
        try:
            command=Path(f'/proc/{pid}/cmdline').read_bytes().split(b'\x00')
            if str(server_file).encode() in command:
                os.killpg(pid,signal.SIGTERM)
                for _ in range(30):
                    if not Path(f'/proc/{pid}').exists(): break
                    time.sleep(.1)
                else: os.killpg(pid,signal.SIGKILL)
        except (FileNotFoundError,ProcessLookupError): pass
    emit({'stage':'INSTALLING'})
    phase='INSTALLING'
    # Colab can omit ensurepip. Also repair configuration left by a failed
    # previous venv creation, while retaining already installed dependencies.
    run_child([sys.executable,'-m','venv','--system-site-packages','--without-pip',str(root/'venv')],step='venv')
    pip=[sys.executable,'-m','pip','--python',str(python),'install','--disable-pip-version-check']
    requirements=['fastapi==0.143.0','uvicorn==0.54.0','transformers==4.57.3','huggingface-hub==0.36.0','accelerate==1.12.0','safetensors>=0.4,<1']
    run_child([*pip,*requirements],step='dependencies')
    has_torch=run_child([str(python),'-c','import torch'],check=False).returncode==0
    if not has_torch:
        run_child([*pip,'torch>=2.6,<3'],step='torch')
    emit({'stage':'DOWNLOADING'})
    phase='DOWNLOADING'
    download="import json,sys\nfrom pathlib import Path\nfrom huggingface_hub import snapshot_download\nc=json.load(sys.stdin)\nsnapshot=snapshot_download(c['model_repo'],revision=c['revision'],token=c.get('hf_token') or False,cache_dir=c['cache'],allow_patterns=['*.json','*.safetensors','*.model','*.txt','*.tiktoken','*.jinja'])\nprint(json.dumps({'snapshot':snapshot,'commit':Path(snapshot).name}))\n"
    downloaded=run_child([str(python),'-c',download],step='model_download',input=json.dumps({'model_repo':C['model_repo'],'revision':C['revision'],'hf_token':C.get('hf_token'),'cache':str(root/'cache')}))
    snapshot=json.loads(downloaded.stdout.strip().splitlines()[-1])
    emit({'stage':'STARTING','snapshot_commit':snapshot['commit']})
    phase='STARTING'
    server_file.write_text(C['server_source']); server_file.chmod(0o600)
    settings={**configuration,'snapshot':snapshot['snapshot'],'server_key':C['server_key']}
    config_file=root/'server.json'
    fd=os.open(config_file,os.O_WRONLY|os.O_CREAT|os.O_TRUNC,0o600)
    with os.fdopen(fd,'w') as f: json.dump(settings,f)
    log_fd=os.open(root/'server.log',os.O_WRONLY|os.O_CREAT|os.O_APPEND,0o600)
    with os.fdopen(log_fd,'ab') as output:
        process=subprocess.Popen([str(python),str(server_file),str(config_file)],stdin=subprocess.DEVNULL,stdout=output,stderr=output,start_new_session=True)
    server_process=process
    state={'pid':process.pid,'digest':digest,'snapshot_commit':snapshot['commit']}
    fd=os.open(state_file,os.O_WRONLY|os.O_CREAT|os.O_TRUNC,0o600)
    with os.fdopen(fd,'w') as f: json.dump(state,f)
    deadline=time.monotonic()+C['startup_timeout']
    while time.monotonic()<deadline:
        if process.poll() is not None: raise RuntimeError('Model server failed to start')
        try:
            if any(item['id']==C['model_repo'] for item in models()['data']):
                emit({'stage':'READY','snapshot_commit':snapshot['commit'],'reused':False})
                break
        except (urllib.error.URLError,TimeoutError,ValueError): pass
        time.sleep(2)
    else:
        terminate(process)
        raise RuntimeError('Model startup timeout')
"""
    return (
        envelope({**config, "server_source": server})
        + "phase='INSTALLING'\nserver_process=None\ntry:\n"
        + indent(body, "    ")
        + "\nexcept BaseException as error:\n"
        + "    if server_process is not None: terminate(server_process)\n"
        + "    emit({'stage':'FAILED','phase':phase,'diagnostic':getattr(error,'diagnostic',{})})\n"
        + "    raise RuntimeError('SPAS setup failed') from None\n"
        + "finally:\n    if 'guard' in globals(): guard.close()\n"
    )


def inference_script(port, server_key, payload):
    return (
        envelope({"port": port, "server_key": server_key, "payload": payload})
        + r"""
import urllib.request, urllib.error
request=urllib.request.Request(f"http://127.0.0.1:{C['port']}/v1/chat/completions",data=json.dumps(C['payload']).encode(),headers={'Authorization':'Bearer '+C['server_key'],'Content-Type':'application/json'})
try:
    with model_http.open(request,timeout=600) as response:
        if C['payload'].get('stream'):
            for raw in response:
                line=raw.decode().strip()
                if line.startswith('data:'):
                    value=line[5:].strip()
                    if value=='[DONE]': emit({'type':'transport_done'})
                    elif value: emit({'type':'chunk','data':json.loads(value)})
        else: emit({'type':'response','data':json.load(response)})
except urllib.error.HTTPError as error:
    emit({'type':'http_error','status':error.code,'retry_after':error.headers.get('Retry-After')})
"""
    )


def status_script(port, server_key):
    return (
        envelope({"port": port, "server_key": server_key})
        + r"""
import urllib.request, urllib.error
try:
    request=urllib.request.Request(f"http://127.0.0.1:{C['port']}/v1/models",headers={'Authorization':'Bearer '+C['server_key']})
    with model_http.open(request,timeout=5) as response:
        emit({'type':'models','data':json.load(response)})
except (urllib.error.URLError,TimeoutError):
    emit({'type':'offline'})
"""
    )


def stop_script(deployment_id):
    return (
        envelope({"deployment_id": deployment_id})
        + r"""
import os,signal,time
from pathlib import Path
root=Path('/content/spas')/C['deployment_id']
state=root/'process.json'
if state.exists():
    pid=json.loads(state.read_text()).get('pid')
    if pid:
        try:
            if str(root/'server.py').encode() in Path(f'/proc/{pid}/cmdline').read_bytes().split(b'\x00'):
                os.killpg(pid,signal.SIGTERM)
                for _ in range(30):
                    if not Path(f'/proc/{pid}').exists(): break
                    time.sleep(.1)
                else: os.killpg(pid,signal.SIGKILL)
        except (FileNotFoundError,ProcessLookupError): pass
    state.unlink(missing_ok=True)
emit({'stage':'STOPPED'})
"""
    )
