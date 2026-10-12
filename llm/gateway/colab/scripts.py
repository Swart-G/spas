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
    nimble_setup = Path(__file__).with_name("nimble_setup.py").read_text()
    nimble_runtime = Path(__file__).with_name("nimble_runtime.py").read_text()
    decisions = Path(__file__).parent.parent.joinpath("core/decisions.py").read_text()
    # The standalone runtime shares request validation; response sanitization stays local.
    decisions = decisions.split("\ndef validate_result(", 1)[0]
    decisions = decisions.replace("from .errors import GatewayError\n", "").replace(
        "import math\n", ""
    )
    download_source = Path(__file__).with_name("download.py").read_text()
    body = r"""
import os, sys, subprocess, time, urllib.request, urllib.error, signal, hashlib, fcntl, re
from pathlib import Path
root=Path('/content/spas') / C['deployment_id']
root.mkdir(parents=True,exist_ok=True,mode=0o700)
root.chmod(0o700)
(root/'download.py').write_text(C['download_source'])
emit({'type':'progress','step':'prepare','status':'running','vm_started_at':time.time()-float(Path('/proc/uptime').read_text().split()[0])})
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
        for marker,value in [('download failed: stalled','download_stalled'),('out of memory','out_of_memory'),('cannot allocate memory','out_of_memory'),('no module named','missing_dependency'),('ensurepip','ensurepip_missing'),('no module named pip','pip_missing'),('no space left on device','disk_full'),('no matching distribution found','package_unavailable'),('resolutionimpossible','dependency_conflict'),('connectionerror','network_error'),('connection refused','network_error'),('temporary failure in name resolution','network_error')]:
            if marker in output:
                reason=value
                break
        self.diagnostic={'step':step,'python':sys.version.split()[0],'exit_code':result.returncode,'reason':reason}
        package=re.search(r'No matching distribution found for ([A-Za-z0-9_.<=>!+\-]+)',result.stderr)
        if package: self.diagnostic['package']=package.group(1)[:100]
        super().__init__('SPAS setup subprocess failed')
def run_child(arguments, *, step='prepare', input=None, check=True):
    import threading, queue
    emit({'type':'progress','step':step,'status':'running'})
    process=subprocess.Popen(arguments,stdin=subprocess.PIPE if input is not None else subprocess.DEVNULL,stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True,start_new_session=True)
    messages=queue.Queue()
    output,errors=[],[]
    def read(stream, lines, structured):
        for line in stream:
            if structured and line.startswith('SPAS_PROGRESS '):
                try:
                    event=json.loads(line[len('SPAS_PROGRESS '):])
                    if isinstance(event,dict): messages.put(event)
                except ValueError: pass
            else: lines.append(line)
        stream.close()
    threads=[threading.Thread(target=read,args=(process.stdout,output,True),daemon=True),threading.Thread(target=read,args=(process.stderr,errors,False),daemon=True)]
    for thread in threads: thread.start()
    try:
        if input is not None:
            process.stdin.write(input)
            process.stdin.close()
        while any(thread.is_alive() for thread in threads):
            try: emit(messages.get(timeout=.2))
            except queue.Empty: pass
        for thread in threads: thread.join()
        while not messages.empty(): emit(messages.get_nowait())
        process.wait()
    except BaseException:
        terminate(process)
        for thread in threads: thread.join(timeout=5)
        raise
    result=subprocess.CompletedProcess(arguments,process.returncode,''.join(output),''.join(errors))
    if check and process.returncode:
        diagnostic=result.stderr
        for secret in (C.get('hf_token'),C.get('server_key')):
            if secret: diagnostic=diagnostic.replace(secret,'[REDACTED]')
        fd=os.open(root/'setup-error.log',os.O_WRONLY|os.O_CREAT|os.O_TRUNC,0o600)
        with os.fdopen(fd,'w') as log: log.write(diagnostic)

    emit({'type':'progress','step':step,'status':'failed' if check and process.returncode else 'done'})
    if check and process.returncode: raise SetupFailure(step,result)
    return result
python=root/'venv/bin/python'
server_file=root/'server.py'
state_file=root/'process.json'
configuration={key:C[key] for key in ('model_repo','revision','precision','max_new_tokens','max_context','port','variant')}
configuration['decision_model']=C.get('decision_model',False)
configuration['implementation']=hashlib.sha256((C['server_source']+C['nimble_setup']+C['nimble_runtime']+C['decisions_source']).encode()).hexdigest()
digest=hashlib.sha256(json.dumps(configuration,sort_keys=True).encode()).hexdigest()
def models():
    request=urllib.request.Request(f"http://127.0.0.1:{C['port']}/v1/models",headers={'Authorization':'Bearer '+C['server_key']})
    with model_http.open(request,timeout=3) as response: return json.load(response)
existing=json.loads(state_file.read_text()) if state_file.exists() else {}
reuse=False
if existing.get('digest')==digest and existing.get('pid'):
    try: reuse=any(item['id']==C['model_repo'] for item in models()['data'])
    except Exception: pass
emit({'type':'progress','step':'prepare','status':'done'})
if reuse:
    emit({'type':'progress','step':'server','status':'done'})
    emit({'stage':'READY','snapshot_commit':existing.get('snapshot_commit'),'reused':True})
else:
    if existing.get('pid'):
        emit({'type':'progress','step':'stop_previous','status':'running'})
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
        emit({'type':'progress','step':'stop_previous','status':'done'})
    else: emit({'type':'progress','step':'stop_previous','status':'skipped'})
    emit({'stage':'INSTALLING'})
    phase='INSTALLING'
    # Colab can omit ensurepip. Also repair configuration left by a failed
    # previous venv creation, while retaining already installed dependencies.
    run_child([sys.executable,'-m','venv','--system-site-packages','--without-pip',str(root/'venv')],step='venv')
    pip=[sys.executable,'-m','pip','--python',str(python),'install','--disable-pip-version-check']
    requirements=['fastapi==0.143.0','uvicorn==0.54.0','transformers==4.57.3','huggingface-hub==0.36.0','accelerate==1.12.0','safetensors>=0.4,<1']
    if configuration['decision_model']:
        requirements=['fastapi==0.143.0','uvicorn==0.54.0','transformers==5.17.0','peft==0.21.0','accelerate==1.15.0','sentencepiece==0.2.2','pillow==12.3.0','safetensors>=0.4,<1']
    run_child([*pip,*requirements],step='dependencies')
    has_torch=run_child([str(python),'-c','import torch'],step='torch_check',check=False).returncode==0
    if not has_torch:
        run_child([*pip,'torch>=2.6,<3'],step='torch')
    else: emit({'type':'progress','step':'torch','status':'skipped'})
    emit({'stage':'DOWNLOADING'})
    phase='DOWNLOADING'
    download="import json,sys\nfrom pathlib import Path\nc=json.load(sys.stdin)\nsys.path.insert(0,c['root'])\nfrom download import download_snapshot\nsnapshot=download_snapshot(c['model_repo'],stall_timeout=c.get('download_stall_timeout',180),max_attempts=c.get('download_max_attempts',3),retry_backoff=c.get('download_retry_backoff',5),revision=c['revision'],token=c.get('hf_token') or False,cache_dir=c['cache'],allow_patterns=['*.json','*.safetensors','*.model','*.txt','*.tiktoken','*.jinja'])\nprint(json.dumps({'snapshot':snapshot,'commit':Path(snapshot).name}))\n"
    downloaded=run_child([str(python),'-c',download],step='model_download',input=json.dumps({'model_repo':C['model_repo'],'revision':C['revision'],'hf_token':C.get('hf_token'),'cache':str(root/'cache'),'root':str(root),**{key:C[key] for key in ('download_stall_timeout','download_max_attempts','download_retry_backoff') if key in C}}))
    snapshot=json.loads(downloaded.stdout.strip().splitlines()[-1])
    if configuration['decision_model']:
        prepare_file=root/'nimble_setup.py'
        prepare_file.write_text(C['nimble_setup'])
        prepared=run_child([str(python),str(prepare_file)],step='nimble_prepare',input=json.dumps({**C,**snapshot,'root':str(root),'cache':str(root/'cache')}))
        snapshot=json.loads(prepared.stdout.strip().splitlines()[-1])
    emit({'stage':'STARTING','snapshot_commit':snapshot['commit']})
    phase='STARTING'
    emit({'type':'progress','step':'server','status':'running'})
    server_file.write_text(C['server_source']); server_file.chmod(0o600)
    settings={**configuration,'snapshot':snapshot['snapshot'],'server_key':C['server_key']}
    settings.update({key:snapshot[key] for key in ('commit','scorer_source','max_input_tokens','adapter_snapshot','offload_folder') if key in snapshot})
    (root/'decisions.py').write_text(C['decisions_source'])
    (root/'nimble_runtime.py').write_text(C['nimble_runtime'])
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
        if process.poll() is not None:
            error_log=(root/'server.log').read_text(errors='replace')[-16000:]
            raise SetupFailure('server',subprocess.CompletedProcess([],process.returncode,'',error_log))
        try:
            if any(item['id']==C['model_repo'] for item in models()['data']):
                emit({'type':'progress','step':'server','status':'done'})
                emit({'stage':'READY','snapshot_commit':snapshot['commit'],'reused':False})
                break
        except (urllib.error.URLError,TimeoutError,ValueError): pass
        time.sleep(2)
    else:
        terminate(process)
        raise RuntimeError('Model startup timeout')
"""
    return (
        envelope(
            {
                **config,
                "server_source": server,
                "nimble_setup": nimble_setup,
                "nimble_runtime": nimble_runtime,
                "decisions_source": decisions,
                "download_source": download_source,
            }
        )
        + "phase='INSTALLING'\nserver_process=None\ntry:\n"
        + indent(body, "    ")
        + "\nexcept BaseException as error:\n"
        + "    if server_process is not None: terminate(server_process)\n"
        + "    detail=getattr(error,'diagnostic',{})\n"
        + "    if detail.get('step')=='nimble_prepare': phase='PREPARING'\n"
        + "    emit({'stage':'FAILED','phase':phase,'diagnostic':detail})\n"
        + "    raise RuntimeError('SPAS setup failed') from None\n"
        + "finally:\n    if 'guard' in globals(): guard.close()\n"
    )


def inference_script(port, server_key, payload, *, decision=False):
    return (
        envelope(
            {
                "port": port,
                "server_key": server_key,
                "payload": payload,
                "endpoint": "decisions" if decision else "chat/completions",
            }
        )
        + r"""
import urllib.request, urllib.error
request=urllib.request.Request(f"http://127.0.0.1:{C['port']}/v1/{C['endpoint']}",data=json.dumps(C['payload']).encode(),headers={'Authorization':'Bearer '+C['server_key'],'Content-Type':'application/json'})
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
        from pathlib import Path
        import time
        emit({'type':'models','data':json.load(response),'vm_started_at':time.time()-float(Path('/proc/uptime').read_text().split()[0])})
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
