"""Measure native GPU dialog responsiveness using isolated data and no SSH."""
from __future__ import annotations

import copy
import json
import subprocess
import sys
import tempfile
import time
import types
from pathlib import Path

from verify_desktop_ui import ROOT, descendants, fixture


def run_case(count: int, width: int) -> dict:
    sys.path.insert(0, str(ROOT))
    with tempfile.TemporaryDirectory(prefix="gpu-dialog-perf-") as directory:
        backend = types.ModuleType("app")
        backend.__file__ = str(ROOT / "app.py")
        source = (ROOT / "app.py").read_text(encoding="utf-8").replace('BASE_DIR = Path(__file__).resolve().parent', f'BASE_DIR = Path({directory!r})', 1)
        sys.modules["app"] = backend
        exec(compile(source, backend.__file__, "exec"), backend.__dict__)
        import desktop_client as ui
        data = fixture()
        gpu = data["server_states"]["default"]["snapshot"]["gpus"][0]
        gpu["processes"] = [dict(pid=100000 + i, user="research-user-with-long-name-" + str(i), memory_mb=512 + i, runtime_seconds=90000 + i, command="python /data/train.py --dataset=" + "x" * 450) for i in range(count)]
        gpu["process_count"] = count
        backend.store.data.update(copy.deepcopy(data))
        ui.DesktopRuntimeManager.auto_connect = lambda self: None
        ui.DesktopRuntimeManager.public_state = lambda self: copy.deepcopy(data)
        client = ui.DesktopClient()
        client.geometry("1080x700")
        client.update()
        build_dialog = client._dialog
        client._dialog = lambda *args, **kwargs: build_dialog(*args, **dict(kwargs, size=(width, 820)))
        fits = 0
        original_fit = ui.GPUProcessCard._fit_command
        def fit(card):
            nonlocal fits
            fits += 1
            original_fit(card)
        ui.GPUProcessCard._fit_command = fit
        if "--profile" in sys.argv:
            import cProfile
            import pstats
            profiler = cProfile.Profile()
            profiler.enable()
        started = time.perf_counter()
        dialog = client.show_gpu_detail("default", 0)
        opening_ms = (time.perf_counter() - started) * 1000
        if "--profile" in sys.argv:
            profiler.disable()
            pstats.Stats(profiler, stream=sys.stderr).sort_stats("cumtime").print_stats(25)
        repeat_started = time.perf_counter()
        for _ in range(5):
            assert client.show_gpu_detail("default", 0) == dialog, "Repeated clicks create duplicate inspectors"
        repeat_ms = (time.perf_counter() - repeat_started) * 1000 / 5
        beats = []
        def heartbeat():
            beats.append(time.perf_counter())
            client.after(20, heartbeat)
        client.after(20, heartbeat)
        client.after(1600, client.quit)
        client.mainloop()
        gaps = [(b - a) * 1000 for a, b in zip(beats, beats[1:])]
        first_beat_ms = (beats[0] - started) * 1000 if beats else 1600
        cards = [card for card in descendants(dialog) if isinstance(card, ui.GPUProcessCard)]
        result = dict(processes=count, width=width, open_ms=round(opening_ms, 1), repeat_open_ms=round(repeat_ms, 1), first_heartbeat_ms=round(first_beat_ms, 1), heartbeat_count=len(beats), max_heartbeat_gap_ms=round(max(gaps or [1600]), 1), fit_calls=fits, rendered_cards=len(cards))
        assert opening_ms < 1000, result
        assert len(beats) >= 10 and first_beat_ms < 1000 and max(gaps or [1600]) < 500, result
        assert len(cards) == count and all(card.winfo_ismapped() for card in cards), result
        for card in cards:
            # A final Configure may be queued at the benchmark deadline. Fit
            # that width once, then verify another identical fit is a no-op.
            if card.resize_after is not None:
                card.after_cancel(card.resize_after)
                card.resize_after = None
            card._fit_command()
            signature = card.fit_signature
            height = card.command_text.cget("height")
            card._fit_command()
            assert card.fit_signature == signature and card.command_text.cget("height") == height
        dialog.destroy()
        # Closing while deferred batches are queued must cancel those jobs.
        interrupted = client.show_gpu_detail("default", 0)
        interrupted.destroy()
        errors = []
        client.report_callback_exception = lambda kind, value, trace: errors.append(str(value))
        client.after(100, client.quit)
        client.mainloop()
        assert not errors, errors
        assert not client.gpu_detail_dialogs, "Closed inspector remains cached"
        client.destroy()
        return result


if __name__ == "__main__":
    if "--case" in sys.argv:
        print(json.dumps(run_case(int(sys.argv[-2]), int(sys.argv[-1]))))
    else:
        for count, width in ((3, 1040), (3, 640), (24, 640)):
            try:
                completed = subprocess.run([sys.executable, __file__, "--case", str(count), str(width)], capture_output=True, text=True, timeout=15)
                if completed.returncode:
                    raise RuntimeError(completed.stderr)
                print(completed.stdout.strip(), flush=True)
            except subprocess.TimeoutExpired:
                print(json.dumps(dict(processes=count, width=width, timeout_seconds=15)), flush=True)
                raise SystemExit(1)
