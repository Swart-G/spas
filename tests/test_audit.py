import json
import multiprocessing
import stat

from llm.gateway.core.audit import RequestLog


def test_shared_handlers_reopen_after_another_writer_rotates(tmp_path):
    gateway, probe = RequestLog(tmp_path), RequestLog(tmp_path)
    try:
        gateway.record(entry="gateway-before")
        probe.record(entry="probe-before")
        gateway.handler.maxBytes = 1
        gateway.record(entry="gateway-after")
        probe.record(entry="probe-after")
        assert (
            gateway.recent()
            == probe.recent()
            == [
                {"entry": "gateway-after"},
                {"entry": "probe-after"},
            ]
        )
        gateway.handler.maxBytes = 2 * 1024 * 1024
        probe.handler.maxBytes = 1
        probe.record(entry="probe-rotated")
        gateway.record(entry="gateway-reopened")
        assert gateway.recent() == [
            {"entry": "probe-rotated"},
            {"entry": "gateway-reopened"},
        ]
        for path in tmp_path.glob("requests.jsonl*"):
            assert stat.S_IMODE(path.stat().st_mode) == 0o600
    finally:
        gateway.close()
        probe.close()


def write_logs(directory, barrier, writer):
    log = RequestLog(directory)
    log.handler.maxBytes = 512
    log.handler.backupCount = 64
    try:
        log.record(writer=writer, entry=0, label="СПАС")
        barrier.wait(timeout=10)
        for entry in range(1, 50):
            log.record(writer=writer, entry=entry, label="СПАС")
    finally:
        log.close()


def test_processes_coordinate_rotations_without_losing_entries(tmp_path):
    context = multiprocessing.get_context("spawn")
    barrier = context.Barrier(3)
    processes = [
        context.Process(target=write_logs, args=(tmp_path, barrier, writer)) for writer in range(3)
    ]
    try:
        for process in processes:
            process.start()
        for process in processes:
            process.join(timeout=15)
            assert process.exitcode == 0
        entries = []
        for path in tmp_path.glob("requests.jsonl*"):
            if path.suffix == ".lock":
                continue
            entries.extend(json.loads(line) for line in path.read_text().splitlines())
        assert len(entries) == 150
        assert {(entry["writer"], entry["entry"]) for entry in entries} == {
            (writer, entry) for writer in range(3) for entry in range(50)
        }
        assert all(entry["label"] == "СПАС" for entry in entries)
    finally:
        for process in processes:
            if process.is_alive():
                process.terminate()
            if process.pid is not None:
                process.join(timeout=5)
