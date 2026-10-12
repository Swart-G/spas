"""Structured Hugging Face download counters, used only in the model runtime."""

import argparse
import io
import json
import queue
import subprocess
import sys
import threading
import time
from contextlib import nullcontext, suppress

PREFIX = "SPAS_PROGRESS "


def report(step, status, **counters):
    print(
        PREFIX + json.dumps({"type": "progress", "step": step, "status": status, **counters}),
        flush=True,
    )


def _download_once(repo, *, step="model_download", **options):
    from huggingface_hub import file_download, snapshot_download
    from tqdm.auto import tqdm

    lock = threading.RLock()
    bars = {}
    last_emitted = 0.0
    active = True

    def publish(force=False):
        nonlocal last_emitted
        with lock:
            if not active:
                return
            now = time.monotonic()
            if not force and now - last_emitted < 0.2:
                return
            last_emitted = now
            byte_bars = [
                bar
                for bar in bars.values()
                if bar.unit == "B" and (bar.total or 0) > 0 and not bar.transfer
            ]
            file_bars = [bar for bar in bars.values() if bar.unit != "B"]
            selected = byte_bars or file_bars
            total = sum(bar.total or 0 for bar in selected)
            completed = sum(bar.n for bar in selected)
            report(
                step,
                "running",
                total=total,
                completed=completed,
                unit="bytes" if byte_bars else "files",
                files_total=sum(bar.total or 0 for bar in file_bars),
                files_completed=sum(bar.n for bar in file_bars),
            )

    class Counters(tqdm):
        def __init__(self, *args, **kwargs):
            # New Xet clients have network and reconstruction bars for the same
            # weights. Count reconstructed bytes once, rather than adding both.
            self.transfer = "downloading bytes" in str(kwargs.get("desc", "")).lower()
            kwargs.pop("name", None)
            kwargs.update(file=io.StringIO(), disable=False, mininterval=0.2, miniters=1)
            super().__init__(*args, **kwargs)
            with lock:
                bars[id(self)] = self
            publish(True)

        def display(self, *args, **kwargs):
            if id(self) in bars:
                publish()

        def update(self, value=1):
            result = super().update(value)
            publish()
            return result

        def close(self):
            super().close()
            if id(self) in bars:
                publish(True)

    # Hub 0.36 exposes byte counters through its internal context factory;
    # Hub 1.x additionally forwards the public tqdm_class to byte downloads.
    original = file_download._get_progress_bar_context

    def byte_context(**kwargs):
        existing = kwargs.pop("_tqdm_bar", None)
        if existing is not None:
            return nullcontext(existing)
        kwargs.pop("log_level", None)
        kwargs.pop("tqdm_class", None)
        kwargs.setdefault("unit", "B")
        return Counters(**kwargs)

    file_download._get_progress_bar_context = byte_context
    report(step, "running")
    try:
        result = snapshot_download(repo, tqdm_class=Counters, **options)
        publish(True)
        report(step, "done")
        return result
    finally:
        active = False
        file_download._get_progress_bar_context = original


RESULT = "SPAS_DOWNLOAD_RESULT "


class DownloadFailure(RuntimeError):
    def __init__(self, reason, attempts):
        self.reason, self.attempts = reason, attempts
        super().__init__(f"Download failed: {reason}; attempts: {attempts}")


def failure_reason(error):
    """Classify locally; never send exception bodies, URLs or credentials to the UI."""
    status = getattr(getattr(error, "response", None), "status_code", None)
    if status == 429:
        return "rate_limited"
    if isinstance(status, int):
        return "network_error" if status >= 500 or status in (408, 425) else "permanent_error"
    if isinstance(error, OSError) and getattr(error, "errno", None) in (13, 28, 30):
        return "permanent_error"
    names = {base.__name__ for base in type(error).__mro__}
    if names & {
        "TimeoutError",
        "Timeout",
        "ConnectionError",
        "TransportError",
        "RequestException",
        "LocalEntryNotFoundError",
    }:
        return "network_error"
    # hf-xet wraps CAS transport failures in RuntimeError, including on Hub 0.36.
    if isinstance(error, RuntimeError) and any(
        marker in str(error).lower()
        for marker in (
            "connection",
            "timeout",
            "timed out",
            "request error",
            "temporarily unavailable",
        )
    ):
        return "network_error"
    return "permanent_error"


def stop_worker(process):
    if process.poll() is None:
        with suppress(ProcessLookupError):
            process.terminate()
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            with suppress(ProcessLookupError):
                process.kill()
            process.wait(timeout=5)


def attempt_download(payload, stall_timeout, on_event):
    """A process boundary lets the watchdog stop stuck Python and native Xet threads."""
    process = subprocess.Popen(
        [sys.executable, __file__, "--worker"],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        # Stay in the installer's process group so Ctrl+C also kills this worker.
    )
    messages = queue.Queue()

    def read(stream, structured):
        try:
            for line in stream:
                if not structured:
                    continue
                prefix = (
                    PREFIX
                    if line.startswith(PREFIX)
                    else RESULT
                    if line.startswith(RESULT)
                    else None
                )
                if prefix is not None:
                    try:
                        value = json.loads(line[len(prefix) :])
                        if isinstance(value, dict):
                            messages.put((prefix, value))
                    except ValueError:
                        pass
        finally:
            stream.close()

    readers = [
        threading.Thread(target=read, args=(process.stdout, True), daemon=True),
        threading.Thread(target=read, args=(process.stderr, False), daemon=True),
    ]
    for reader in readers:
        reader.start()
    last_progress = time.monotonic()
    high_water = {}
    result = None
    try:
        process.stdin.write(json.dumps(payload))
        process.stdin.close()
        while True:
            try:
                prefix, event = messages.get(timeout=min(0.2, stall_timeout / 4))
            except queue.Empty:
                prefix, event = None, None
            if prefix == RESULT:
                result = event
                # Even after a result, wait for process exit: lingering threads can hang.
                last_progress = time.monotonic()
            elif prefix == PREFIX:
                for key in ("completed", "files_completed"):
                    value = event.get(key)
                    counter = (event.get("unit"), key)
                    if type(value) in (int, float) and high_water.get(counter, 0) < value <= 10**15:
                        high_water[counter] = value
                        last_progress = time.monotonic()
                on_event(event)
            if (
                process.poll() is not None
                and all(not reader.is_alive() for reader in readers)
                and messages.empty()
            ):
                if result is None:
                    return {"error": "worker_failed"}
                return result
            if time.monotonic() - last_progress >= stall_timeout:
                return {"error": "stalled"}
    finally:
        stop_worker(process)
        for reader in readers:
            reader.join(timeout=5)


def download_snapshot(
    repo, *, step="model_download", stall_timeout=180, max_attempts=3, retry_backoff=5, **options
):
    if stall_timeout <= 0 or max_attempts < 1 or retry_backoff < 0:
        raise ValueError("Invalid download retry settings")
    for attempt in range(1, max_attempts + 1):
        report(
            step,
            "running",
            attempt=attempt,
            max_attempts=max_attempts,
            completed=0,
            total=0,
            files_completed=0,
            files_total=0,
            unit="files",
        )

        def forward(event, attempt=attempt):
            if event.get("status") != "done":
                report(
                    step,
                    event.get("status", "running"),
                    **{
                        key: event[key]
                        for key in ("completed", "total", "files_completed", "files_total", "unit")
                        if key in event
                    },
                    attempt=attempt,
                    max_attempts=max_attempts,
                )

        result = attempt_download(
            {"repo": repo, "step": step, "options": options}, stall_timeout, forward
        )
        if isinstance(result.get("snapshot"), str):
            report(step, "done", attempt=attempt, max_attempts=max_attempts)
            return result["snapshot"]
        reason = result.get("error", "worker_failed")
        if (
            reason not in {"stalled", "network_error", "rate_limited", "worker_failed"}
            or attempt == max_attempts
        ):
            report(step, "failed", attempt=attempt, max_attempts=max_attempts, reason=reason)
            raise DownloadFailure(reason, attempt)
        delay = min(retry_backoff * 2 ** (attempt - 1), 60)
        report(
            step,
            "retrying",
            attempt=attempt,
            max_attempts=max_attempts,
            reason=reason,
            retry_delay=delay,
        )
        time.sleep(delay)
    raise AssertionError("Unreachable download state")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--worker", action="store_true", required=True)
    parser.parse_args()
    payload = json.load(sys.stdin)
    try:
        snapshot = _download_once(payload["repo"], step=payload["step"], **payload["options"])
        result = {"snapshot": snapshot}
    except Exception as error:
        result = {"error": failure_reason(error)}
    print(RESULT + json.dumps(result), flush=True)


if __name__ == "__main__":
    main()
