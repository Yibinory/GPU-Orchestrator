"""Verify persisted GPU masking, all scheduling levels and the web API without SSH."""
from __future__ import annotations

import copy
import sys
import tempfile
import threading
import time
import types
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def run() -> None:
    with tempfile.TemporaryDirectory(prefix="gpu-policy-check-") as directory:
        module = types.ModuleType("gpu_policy_backend")
        module.__file__ = str(ROOT / "app.py")
        source = (ROOT / "app.py").read_text(encoding="utf-8").replace('BASE_DIR = Path(__file__).resolve().parent', f'BASE_DIR = Path({directory!r})', 1)
        sys.modules[module.__name__] = module
        exec(compile(source, module.__file__, "exec"), module.__dict__)
        rt = module.runtime
        rt.stop_event.set()
        module.store.data["servers"].append(dict(id="second", profile={}, enabled=True))
        other = module.manager.runtime_for("second")
        other.stop_event.set()
        raw = dict(gpus=[dict(index=i, uuid=f"GPU-{i}", name="Demo GPU", is_idle=True, memory_total_mb=24576, memory_free_mb=24000, memory_used_mb=576, performance_hint=100 + i, processes=[dict(pid=123, memory_mb=576)]) for i in range(2)])
        rt.snapshot = rt._enrich_snapshot(raw)
        other.snapshot = other._enrich_snapshot(copy.deepcopy(raw))
        job = dict(peak_memory_mb=1024)
        assert rt._choose_gpu(job)["index"] == 1, "Default scheduling changed"
        rt.set_gpu_blocked(1, True, "GPU-1")
        for level in ("idle_only", "low_interference", "emergency"):
            assert rt._choose_gpu(dict(job, execution_level=level))["index"] == 0, level
        assert other._choose_gpu(job)["index"] == 1, "Policy leaked to another server"
        enriched = rt._enrich_snapshot(raw)
        assert enriched["gpus"][1]["scheduling_blocked"]
        assert enriched["gpus"][1]["processes"] == raw["gpus"][1]["processes"], "Masking removed monitoring"
        rt.set_gpu_blocked(0, True)
        for level in ("idle_only", "low_interference", "emergency"):
            assert rt._choose_gpu(dict(job, execution_level=level)) is None
        rt.set_gpu_blocked(1, False)
        assert rt._choose_gpu(job)["index"] == 1, "Unmasking did not affect stale snapshot"
        rt.set_gpu_blocked(1, True)
        moved = copy.deepcopy(raw)
        moved["gpus"][1]["index"] = 7
        assert rt._enrich_snapshot(moved)["gpus"][1]["scheduling_blocked"], "UUID policy lost after reindex"
        persisted = module.StateStore(module.STATE_FILE)
        assert persisted.data["servers"][0]["blocked_gpu_keys"] == ["GPU-0", "GPU-1"]
        # Blocked cards must not reach remote launch, even if chosen earlier.
        assert rt._start(dict(id="queued"), raw["gpus"][1]) is False
        for index, blocked, uuid in ((-1, True, ""), (0, "false", ""), (True, True, ""), (9, True, ""), (0, True, "GPU-stale")):
            try:
                rt.set_gpu_blocked(index, blocked, uuid)
                raise AssertionError("Invalid GPU update accepted")
            except ValueError:
                pass
        rt.set_gpu_blocked(0, False)
        save = module.store.save
        module.store.save = lambda: (_ for _ in ()).throw(OSError("Synthetic save failure"))
        try:
            rt.set_gpu_blocked(0, True)
            raise AssertionError("Save failure hidden")
        except OSError:
            assert not rt._gpu_blocked(raw["gpus"][0]), "Failed save changed policy"
        finally:
            module.store.save = save
        commands = []
        remote, remote_path = rt.remote, rt._remote_path
        rt._remote_path = lambda value: "/home/demo"
        rt.remote = types.SimpleNamespace(
            exec=lambda command, **kwargs: commands.append(command) or dict(code=0, stdout="123\n", stderr=""),
            put=lambda *_args: rt.set_gpu_blocked(0, True),
        )
        try:
            queued = dict(id="upload-race", status="queued", local_script="demo.sh")
            assert rt._start(queued, raw["gpus"][0]) is False
            assert queued["status"] == "queued" and not any(command.startswith("nohup") for command in commands), "Blocked during upload still launched"
        finally:
            rt.remote, rt._remote_path = remote, remote_path
        # An ongoing poll holds the runtime lock; changing local policy must
        # not wait for that SSH operation to finish.
        ready, release = threading.Event(), threading.Event()
        def hold_poll_lock():
            with rt.lock:
                ready.set()
                release.wait(2)
        holder = threading.Thread(target=hold_poll_lock)
        holder.start()
        assert ready.wait(1)
        started = time.perf_counter()
        try:
            rt.set_gpu_blocked(0, True)
            assert time.perf_counter() - started < 0.5, "Toggle waited for SSH poll lock"
        finally:
            release.set()
            holder.join()
        # Masking applies to new allocations, never kills an existing job.
        running = dict(id="running", server_id="default", status="running", assigned_gpu=dict(index=0), pid=123)
        module.store.data["experiments"] = [running]
        api = module.app.test_client()
        response = api.post("/api/gpus/scheduling", json=dict(server_id="default", gpu_index=0, gpu_uuid="GPU-0", blocked=False))
        assert response.status_code == 200
        assert not response.json["server_states"]["default"]["snapshot"]["gpus"][0]["scheduling_blocked"]
        assert running["status"] == "running" and running["pid"] == 123
        response = api.post("/api/gpus/scheduling", json=dict(server_id="second", gpu_index=0, blocked=True))
        assert response.status_code == 200 and other._gpu_blocked(raw["gpus"][0])
        assert not rt._gpu_blocked(raw["gpus"][0])
        for payload, status in (([], 400), (dict(server_id="missing", gpu_index=0, blocked=True), 404), (dict(gpu_index=99, blocked=True), 400), (dict(gpu_index=0, blocked="false"), 400), (dict(gpu_index=0, blocked=True, gpu_uuid="stale"), 400)):
            assert api.post("/api/gpus/scheduling", json=payload).status_code == status
        # Legacy snapshots without UUID still support persisted index policy.
        rt.snapshot = dict(gpus=[dict(index=3, is_idle=True, memory_free_mb=1000)])
        rt.set_gpu_blocked(3, True)
        assert rt._choose_gpu(dict(peak_memory_mb=0, execution_level="emergency")) is None
        assert "index:3" in module.StateStore(module.STATE_FILE).data["servers"][0]["blocked_gpu_keys"]
        print("PASS: all scheduling levels, stale snapshots, UUID reindex, server isolation, persistence, monitoring retention, running tasks, launch/upload guards, save rollback, poll-lock responsiveness and web API validation.")


if __name__ == "__main__":
    run()
