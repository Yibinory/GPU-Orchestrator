"""Check native scrolling with isolated multi-server data and no SSH."""
from __future__ import annotations

import copy
import json
import sys
import tempfile
import time
import types

from verify_desktop_ui import ROOT, fixture


def run() -> None:
    sys.path.insert(0, str(ROOT))
    with tempfile.TemporaryDirectory(prefix="gpu-scroll-check-") as directory:
        backend = types.ModuleType("app")
        backend.__file__ = str(ROOT / "app.py")
        sys.modules["app"] = backend
        source = (ROOT / "app.py").read_text(encoding="utf-8").replace('BASE_DIR = Path(__file__).resolve().parent', f'BASE_DIR = Path({directory!r})', 1)
        exec(compile(source, backend.__file__, "exec"), backend.__dict__)
        import desktop_client as ui
        data = fixture()
        for state in data["server_states"].values():
            template = state["snapshot"]["gpus"][0]
            state["snapshot"]["gpus"] = [dict(copy.deepcopy(template), index=i, uuid=f"GPU-scroll-{i:04d}") for i in range(12)]
        backend.store.data.update(copy.deepcopy(data))
        ui.DesktopRuntimeManager.auto_connect = lambda self: None
        ui.DesktopRuntimeManager.public_state = lambda self: copy.deepcopy(data)
        client = ui.DesktopClient()
        errors = []
        client.report_callback_exception = lambda kind, value, trace: errors.append(str(value))

        def drain() -> None:
            client.after(80, client.quit)
            client.mainloop()
            client.update()

        results = []
        try:
            client.geometry("1080x700")
            client.update()
            drain()
            for view, canvas in (("dashboard", client.dashboard_canvas), ("benchmarks", client.benchmark_canvas), ("logs", client.log_list_canvas)):
                client._show_view(view)
                client.update()
                drain()
                region_updates = []
                configure = canvas.configure
                def observe_region(cnf=None, **kwargs):
                    if "scrollregion" in kwargs:
                        region_updates.append(kwargs["scrollregion"])
                    return configure(cnf, **kwargs)
                canvas.configure = observe_region
                timings = []
                for i in range(16):
                    started = time.perf_counter()
                    canvas.yview_moveto((i % 8) / 10)
                    client.update()
                    timings.append((time.perf_counter() - started) * 1000)
                assert not region_updates, "Moving the content recalculated its unchanged scroll region"

                canvas.yview_moveto(.1)
                client.update()
                previous = canvas.canvasy(0)
                scroll_calls = []
                move = canvas.yview_moveto
                def observe_move(position):
                    scroll_calls.append(position)
                    move(position)
                canvas.yview_moveto = observe_move
                # High-resolution deltas must accumulate without 20 redraws.
                for _ in range(20):
                    canvas.wheel(types.SimpleNamespace(delta=-30))
                drain()
                expected = 240 * client.winfo_fpixels("1i") / 96
                assert len(scroll_calls) == 1 and abs(canvas.canvasy(0) - previous - expected) <= 2, (view, scroll_calls)
                canvas.wheel(types.SimpleNamespace(delta=30))
                drain()
                assert abs(canvas.canvasy(0) - previous - expected + expected / 20) <= 2, "Small upward input lost"
                canvas.wheel(types.SimpleNamespace(delta=0))
                assert canvas._scroll_after is None, "Zero input scrolled the page"
                canvas.yview_moveto(0)
                canvas.wheel(types.SimpleNamespace(delta=120))
                drain()
                assert canvas.yview()[0] == 0, "Scrolling beyond the top moved the content"
                canvas.scroll_view("scroll", 1, "units")
                drain()
                assert canvas.canvasy(0) >= 15, "Scrollbar arrows only moved one pixel"
                canvas.yview_moveto = move
                position = canvas.yview()[0]
                started = time.perf_counter()
                client.refresh_state()
                client.update()
                refresh_ms = (time.perf_counter() - started) * 1000
                assert abs(canvas.yview()[0] - position) < .001, "Periodic refresh reset the scroll position"
                result = dict(view=view, average_scroll_ms=round(sum(timings) / len(timings), 1), max_scroll_ms=round(max(timings), 1), unchanged_refresh_ms=round(refresh_ms, 1))
                assert max(timings) < 500 and refresh_ms < 250, result
                results.append(result)
                canvas.configure = configure

            client._show_view("dashboard")
            client.update()
            original = client.dashboard_server_panels["default"]["gpu_state"]["widgets"][0]
            data["server_states"]["default"]["snapshot"]["gpus"][0].update(utilization_gpu=17, scheduling_blocked=True)
            client.refresh_state()
            client.update()
            current = client.dashboard_server_panels["default"]["gpu_state"]["widgets"][0]
            assert current is original and current["utilization_label"].cget("text") == "17.0%"
            assert current["policy_button"].cget("text") == "屏蔽", "Live data stopped updating"
            client._show_view("benchmarks")
            client.update()
            client._scroll_benchmark(types.SimpleNamespace(widget=client.history_tree, delta=-120))
            assert client.benchmark_canvas._scroll_after is None, "History table scrolled its parent too"
            client.geometry("1600x960")
            client.update()
            assert client.benchmark_canvas._content_size[0] == client.benchmark_canvas.winfo_width(), "Resize did not update the viewport"
            # Close with a queued wheel event: the destroyed canvas must leave
            # no callback in Tk's event queue.
            client.benchmark_canvas.wheel(types.SimpleNamespace(delta=-120))
            assert client.benchmark_canvas._scroll_after is not None
            client.benchmark_canvas.destroy()
            drain()
            assert not errors, errors
            print(json.dumps(results), flush=True)
            print("PASS: 24 GPUs, three scrolling views, unchanged-region/refresh checks, wheel bursts, fractional deltas, boundaries, scrollbar arrows, nested tables, live updates, resize and callback cleanup.")
        finally:
            client.destroy()


if __name__ == "__main__":
    run()
