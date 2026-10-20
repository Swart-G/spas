import json
import os
from types import SimpleNamespace

import pytest

from llm.gateway.colab import download

WORKER = r"""
import json,os,sys,time
from pathlib import Path
payload=json.load(sys.stdin)
c=payload['options']
root=Path(c['cache_dir'])
count=root/'attempts'
attempt=int(count.read_text())+1 if count.exists() else 1
count.write_text(str(attempt))
with (root/'pids').open('a') as f: f.write(str(os.getpid())+'\n')
assert c['token']=='private-secret'
assert not c.get('force_download')
print('private-secret private URL',file=sys.stderr,flush=True)
case=c['case']
if case=='always_stall' or (case=='stall_then_resume' and attempt==1):
    (root/'partial').write_text('retained')
    while True:
        print('SPAS_PROGRESS '+json.dumps({'step':payload['step'],'status':'running',
             'unit':'bytes','completed':1,'total':10}),flush=True)
        time.sleep(.02)
if case=='permanent' or (case=='network_then_resume' and attempt==1):
    error='permanent_error' if case=='permanent' else 'network_error'
    print('SPAS_DOWNLOAD_RESULT '+json.dumps({'error':error}),flush=True)
else:
    if case=='stall_then_resume': assert (root/'partial').read_text()=='retained'
    for n in range(1,6):
        print('SPAS_PROGRESS '+json.dumps({'step':payload['step'],'status':'running',
             'unit':'bytes','completed':n,'total':5}),flush=True)
        time.sleep(.06)
    print('SPAS_DOWNLOAD_RESULT '+json.dumps({'snapshot':str(root/'snapshot')}),flush=True)
"""


@pytest.fixture
def worker(tmp_path, monkeypatch):
    path = tmp_path / "worker.py"
    path.write_text(WORKER)
    monkeypatch.setattr(download, "__file__", str(path))
    yield tmp_path
    pids = tmp_path / "pids"
    if pids.exists():
        for pid in pids.read_text().splitlines():
            with pytest.raises(ProcessLookupError):
                os.kill(int(pid), 0)


def invoke(root, case, **options):
    return download.download_snapshot(
        "team/model",
        token="private-secret",
        cache_dir=str(root),
        case=case,
        stall_timeout=0.2,
        retry_backoff=0,
        **options,
    )


@pytest.mark.parametrize("case", ["stall_then_resume", "network_then_resume"])
def test_stall_or_network_failure_retries_and_reuses_partial_cache(worker, case, capsys):
    assert invoke(worker, case) == str(worker / "snapshot")
    assert (worker / "attempts").read_text() == "2"
    events = [
        json.loads(line[len(download.PREFIX) :]) for line in capsys.readouterr().out.splitlines()
    ]
    retry = next(event for event in events if event["status"] == "retrying")
    assert retry["reason"] == ("stalled" if case == "stall_then_resume" else "network_error")
    assert retry["attempt"] == 1
    assert events[-1]["status"] == "done" and events[-1]["attempt"] == 2
    assert "private" not in json.dumps(events)


def test_unchanged_counter_heartbeats_do_not_hide_stall_and_retries_are_bounded(worker):
    with pytest.raises(download.DownloadFailure) as error:
        invoke(worker, "always_stall", max_attempts=2)
    assert error.value.reason == "stalled" and error.value.attempts == 2
    assert (worker / "attempts").read_text() == "2"


def test_permanent_failure_does_not_retry(worker):
    with pytest.raises(download.DownloadFailure) as error:
        invoke(worker, "permanent")
    assert error.value.reason == "permanent_error" and error.value.attempts == 1
    assert (worker / "attempts").read_text() == "1"


def test_progress_prevents_false_timeout_on_slow_but_active_download(worker):
    assert invoke(worker, "active") == str(worker / "snapshot")
    assert (worker / "attempts").read_text() == "1"


def test_cancellation_stops_worker_without_retry(worker):
    def cancel(event):
        raise KeyboardInterrupt()

    with pytest.raises(KeyboardInterrupt):
        download.attempt_download(
            {
                "repo": "team/model",
                "step": "model_download",
                "options": {
                    "cache_dir": str(worker),
                    "token": "private-secret",
                    "case": "always_stall",
                },
            },
            0.2,
            cancel,
        )
    assert (worker / "attempts").read_text() == "1"


@pytest.mark.parametrize(
    "status,reason",
    [
        (401, "permanent_error"),
        (403, "permanent_error"),
        (404, "permanent_error"),
        (429, "rate_limited"),
        (503, "network_error"),
    ],
)
def test_http_failure_classification_never_exposes_response(status, reason):
    error = Exception("private-secret")
    error.response = SimpleNamespace(status_code=status)
    assert download.failure_reason(error) == reason


def test_disk_full_is_permanent_and_xet_transport_failure_is_retryable():
    assert download.failure_reason(OSError(28, "private path")) == "permanent_error"
    assert (
        download.failure_reason(RuntimeError("CAS request error: private-secret"))
        == "network_error"
    )
