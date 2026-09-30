"""Exercise native layouts with isolated synthetic data; never connect to SSH.

Run `python scripts/verify_desktop_ui.py` for layout checks, or add `--preview`
to inspect the same fixture in a separate desktop window.
"""
from __future__ import annotations

import copy
import sys
import tempfile
import types
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def fixture() -> dict:
    servers = [dict(id="default", name="训练服务器 A", host="gpu-a.example", port=22, username="research", enabled=True, connected=True, auto_connect=False),
               dict(id="second", name="推理服务器 B", host="gpu-b.example", port=2222, username="research", enabled=True, connected=True, auto_connect=False)]
    states = {}
    for server in servers:
        gpus = [dict(index=i, name="NVIDIA GeForce RTX 4090", uuid=f"GPU-demo-{i:04d}", memory_total_mb=24576, memory_used_mb=6000 + i * 2000, memory_free_mb=18576 - i * 2000, utilization_gpu=12 + i * 20, temperature_c=42 + i * 6, scheduler_idle=i == 0, process_count=i, benchmark=dict(tensor_tflops=76.3 + i, fp32_tflops=30.8), processes=[]) for i in range(4 if server["id"] == "default" else 2)]
        states[server["id"]] = dict(connected=True, last_poll_at="2026-09-30T06:00:00+00:00", conda=dict(envs=["pytorch", "inference"]), preferences={}, snapshot=dict(cpu_percent=24.5, load_average=[2.4], memory=dict(total_bytes=128 * 1024**3, used_bytes=48 * 1024**3, used_percent=37.5), disk=dict(path="/home/research", total_bytes=2 * 1024**4, used_bytes=1024**4, free_bytes=1024**4, used_percent=50), gpus=gpus))
    tasks = [dict(id=f"demo-{i}", task_no=i + 1, created_seq=i + 1, name=f"模型训练实验 {i + 1:02d}", script_name=f"train_{i + 1:02d}.sh", server_id="default" if i % 2 else "second", status=("queued", "running", "paused", "success", "failed", "waiting_memory", "canceled")[i % 7], priority=10 + i, execution_level=("idle_only", "low_interference", "emergency")[i % 3], max_gpu_utilization=30, peak_memory_mb=12000, depends_on=[], created_at="2026-09-30T05:00:00+00:00") for i in range(32)]
    for state in states.values():
        gpu = state["snapshot"]["gpus"][0]
        gpu.update(process_count=3, scheduler_idle=False, processes=[
            dict(pid=3958223, user="research", name="python3", memory_mb=12024, runtime_seconds=93601, command="/home/research/miniconda3/envs/pytorch/bin/python -u train.py --model vision-language-large --dataset /data/datasets/visual_reasoning --checkpoint /data/checkpoints/pretrained/model.safetensors --precision bf16 --batch_size 2 --gradient_accumulation_steps 10 --experiment_name vision-language-training"),
            dict(pid=3144207, user="alice", name="python3", memory_mb=6022, runtime_seconds=27060, command="/data/projects/multimodal-retrieval/conda-env/bin/python /data/projects/multimodal-retrieval/src/train.py experiment=alignment_train data_dir=/data/datasets/semantic_retrieval checkpoint_dir=/data/checkpoints/alignment seed=2026 train=true"),
            dict(pid=3412244, user="bob", name="python3", memory_mb=628, runtime_seconds=14340, command="/home/bob/miniconda3/envs/inference/bin/python -m inference.server --config /data/configs/inference.yaml --port 8080"),
        ])
        gpu.update(memory_used_mb=18674, memory_free_mb=5902, utilization_gpu=99, temperature_c=76)
    return dict(servers=servers, server_states=states, experiments=tasks, active_server_id="default", global_settings=dict(gpu_history_samples=5), preferences={}, profile={}, benchmarks=[])


def descendants(widget):
    for child in widget.winfo_children():
        yield child
        yield from descendants(child)


def run() -> None:
    # The backend is imported against a temporary data directory before loading
    # the UI. Existing data/state.json and the running client remain untouched.
    with tempfile.TemporaryDirectory(prefix="gpu-ui-check-") as directory:
        backend = types.ModuleType("app")
        backend.__file__ = str(ROOT / "app.py")
        source = (ROOT / "app.py").read_text(encoding="utf-8")
        source = source.replace('BASE_DIR = Path(__file__).resolve().parent', f'BASE_DIR = Path({directory!r})', 1)
        sys.modules["app"] = backend
        exec(compile(source, backend.__file__, "exec"), backend.__dict__)
        import desktop_client as ui
        data = fixture()
        backend.store.data.update(copy.deepcopy(data))
        ui.DesktopRuntimeManager.auto_connect = lambda self: None
        def deny_connection(*_args, **_kwargs):
            raise RuntimeError("演示窗口仅用于界面预览，请启动正式客户端连接服务器。")
        ui.DesktopRuntimeManager.connect = deny_connection
        ui.DesktopRuntimeManager.public_state = lambda self: copy.deepcopy(data)
        ui.DesktopClient.load_selected_log = lambda self: self._set_log_text(self.selected_log, "[14:00:01] Initializing CUDA runtime\n[14:00:02] GPU 0 · NVIDIA GeForce RTX 4090\n[14:00:03] Epoch 01 / 100  loss=0.2841\n" * 80) if self.selected_log else None
        client = ui.DesktopClient()
        client.title("算力调度台 · 统一风格预览（演示数据）")
        errors = []
        client.report_callback_exception = lambda kind, value, trace: errors.append(str(value))

        if "--preview" in sys.argv:
            client.withdraw()
            client.update()
            client.deiconify()
            if "--dialog" in sys.argv:
                name = sys.argv[sys.argv.index("--dialog") + 1]
                actions = {"experiment": client.open_experiment_dialog, "connection": lambda: client.open_connection_dialog(new_server=True), "servers": client.open_server_manager, "gpu": lambda: client.show_gpu_detail("default", 0)}
                client.after(300, actions[name])
            client.mainloop()
            return

        try:
            for width, height in ((1080, 700), (1280, 820), (1600, 960)):
                client.geometry(f"{width}x{height}")
                for view in client.views:
                    client._show_view(view)
                    client.update()
                    assert client.header_title.get(), view
                    assert client.status_bar.winfo_height() > 10, "Status bar collapsed"
                    assert client.status_bar.winfo_y() + client.status_bar.winfo_height() <= client.main.winfo_height(), "Status bar clipped"
                    if view == "queue":
                        buttons = [w for w in descendants(client.views[view]) if w.winfo_class() in ("Button", "TButton")]
                        assert len(buttons) == 15, "Queue action/filter lost"
                        for button in buttons:
                            assert button.winfo_ismapped(), button.cget("text")
                            assert button.winfo_rootx() + button.winfo_width() <= client.winfo_rootx() + client.winfo_width(), f"Clipped action: {button.cget('text')}"
                        assert len(client.queue_tree.get_children()) == 32
                        client.queue_tree.selection_set("demo-5")
                        client.set_queue_filter("queued")
                        assert set(client.queue_tree.get_children()) == {t["id"] for t in data["experiments"] if t["status"] in ("queued", "waiting_memory")}
                        client.set_queue_filter("all")
                    if view == "logs":
                        assert len(client.log_list_body.winfo_children()) == 32
                        assert all("\\n" not in w.cget("text") for w in client.log_list_body.winfo_children())
                        bounds = client.log_list_canvas.bbox("all")
                        assert bounds[3] > client.log_list_canvas.winfo_height(), "Task list not scrollable"
                        assert client.log_text.winfo_width() > 300
                        client.select_log("demo-9")
                        assert client.log_title_var.get() == "模型训练实验 10"
                    if view == "dashboard":
                        for panel in client.dashboard_server_panels.values():
                            parent = panel["gpu_grid"]
                            for widget in panel["gpu_state"]["widgets"].values():
                                card = widget["card"]
                                assert card.winfo_x() + card.winfo_width() <= parent.winfo_width(), "GPU card clipped"
            client._show_view("dashboard")
            client.update()
            assert client.sidebar_logo_image.width() >= 60, "Brand logo too small"
            for bar in client.metric_bars.values():
                for value in (0, 6.1, 50, 100):
                    bar["value"] = value
                    client.update()
                    fill = bar.coords("fill")
                    assert abs(fill[2] - bar.winfo_width() * value / 100) < 1, "Resource bar does not show actual percentage"
                    assert bar.itemcget("fill", "fill") != bar.itemcget("track", "fill"), "Resource fill matches track"
                    assert bar.itemcget("fill", "state") == ("hidden" if value == 0 else "normal")
            live_cards = dict(client.dashboard_server_panels["default"]["gpu_state"]["widgets"])
            client.geometry("1080x700")
            client.update()
            client.geometry("1600x960")
            client.update()
            assert live_cards == client.dashboard_server_panels["default"]["gpu_state"]["widgets"], "Resize rebuilt live GPU widgets"
            for action in (client.open_server_manager, client.open_experiment_dialog, lambda: client.open_connection_dialog(new_server=True), lambda: client.open_disk_alert_dialog("default"), lambda: client.show_gpu_detail("default", 0)):
                action()
                client.update()
                dialogs = [w for w in client.winfo_children() if w.winfo_class() == "Toplevel"]
                assert dialogs, "Dialog failed to open"
                for dialog in dialogs:
                    assert dialog.ui_header.winfo_ismapped() and dialog.ui_footer.winfo_ismapped(), "Dialog missing shared header/footer"
                    assert dialog.ui_footer.winfo_rooty() + dialog.ui_footer.winfo_height() <= dialog.winfo_rooty() + dialog.winfo_height(), "Dialog footer clipped"
                    if dialog.title() == "提交实验":
                        sections = dialog.ui_content.winfo_children()
                        dialog.geometry("700x600")
                        client.update()
                        assert [int(section.grid_info()["column"]) for section in sections] == [0, 0], "Narrow experiment form does not stack"
                        dialog.geometry("1020x860")
                        client.update()
                    for widget in descendants(dialog):
                        if widget.winfo_class() == "TCombobox" and str(widget.cget("state")) != "disabled":
                            values = widget.cget("values")
                            if len(values) > 1:
                                widget.event_generate("<ButtonPress-1>", x=widget.winfo_width() - 12, y=widget.winfo_height() // 2)
                                widget.event_generate("<ButtonRelease-1>", x=widget.winfo_width() - 12, y=widget.winfo_height() // 2)
                                client.update()
                                popdown = widget.tk.call("ttk::combobox::PopdownWindow", widget)
                                assert widget.tk.call("winfo", "ismapped", popdown), "Dropdown arrow does not open options"
                                widget.tk.call("ttk::combobox::Unpost", widget)
                                widget.current(1)
                                widget.event_generate("<<ComboboxSelected>>")
                                client.update()
                                assert widget.get() == values[1], "Dropdown selection broken"
                        if widget.winfo_class() == "TSpinbox" and "disabled" not in widget.state():
                            before = float(widget.get())
                            step = float(widget.cget("increment"))
                            widget.event_generate("<<Increment>>")
                            client.update()
                            assert float(widget.get()) == before + step, "Number control increment broken"
                            widget.event_generate("<<Decrement>>")
                            client.update()
                            assert float(widget.get()) == before, "Number control decrement broken"
                        if widget.winfo_class() == "TCheckbutton":
                            variable = str(widget.cget("variable"))
                            before = widget.tk.getboolean(widget.tk.globalgetvar(variable))
                            widget.invoke()
                            assert widget.tk.getboolean(widget.tk.globalgetvar(variable)) != before, "Checkbox no longer toggles"
                            widget.invoke()
                    dialog.destroy()
            # Exercise the live process inspector, including command wrapping,
            # copy fidelity, card reuse, process churn and disconnected data.
            # Freeze the fixture poll while mutating this snapshot; the dialog's
            # own refresh timer remains active and is exercised below.
            if client.refresh_after_id is not None:
                client.after_cancel(client.refresh_after_id)
                client.refresh_after_id = None
            dialog = client.show_gpu_detail("default", 0)
            client.update()
            refresh = next(w for w in descendants(dialog.ui_footer) if w.winfo_class() == "TButton" and w.cget("text") == "刷新")
            def process_cards():
                return sorted([w for w in descendants(dialog) if isinstance(w, ui.GPUProcessCard)], key=lambda w: w.winfo_y())
            cards = process_cards()
            assert [w.user.cget("text") for w in cards] == ["research", "alice", "bob"]
            gpu = client.state["server_states"]["default"]["snapshot"]["gpus"][0]
            saved_gpu = copy.deepcopy(gpu)
            for card, process in zip(cards, gpu["processes"]):
                assert card.command_text.get("1.0", "end-1c") == process["command"], "Command truncated"
                assert str(card.command_text.cget("state")) == "disabled", "Command is editable"
            canvas = cards[0].master.master
            first = cards[0]
            first.command_text.tag_add("sel", "1.0", "1.12")
            canvas.yview_moveto(0.3)
            client.update()
            position = canvas.yview()[0]
            gpu["processes"][0]["runtime_seconds"] += 30
            refresh.invoke()
            client.update()
            assert process_cards() == cards, "Refresh rebuilt unchanged process cards"
            assert first.command_text.tag_ranges("sel"), "Refresh erased command selection"
            assert abs(canvas.yview()[0] - position) < 0.02, "Refresh reset scroll position"
            assert "02:00:31" in first.meta_labels[1].cget("text"), "Runtime counter not refreshed"
            # Capture clipboard writes in memory; leave the user's clipboard alone.
            clipboard = []
            first.clipboard_clear = clipboard.clear
            first.clipboard_append = clipboard.append
            first.copy_button.invoke()
            assert clipboard == [gpu["processes"][0]["command"]], "Copy changed command contents"
            long_command = "python /data/训练/train.py --config 'quoted path.yaml'\n--payload=" + "x" * 2400
            gpu["processes"][0]["command"] = long_command
            gpu["processes"][0]["user"] = "research-user-with-a-long-name"
            refresh.invoke()
            for width in (640, 1040):
                dialog.geometry(f"{width}x820")
                client.update()
                assert first.command_text.get("1.0", "end-1c") == long_command
                display_lines = first.command_text.count("1.0", "end-1c", "displaylines")[0] + 1
                assert int(first.command_text.cget("height")) >= display_lines, "Wrapped command clipped"
                assert first.meta.winfo_x() + first.meta.winfo_width() <= first.head.winfo_width() + 1, "Process metadata clipped"
                assert dialog.ui_footer.winfo_ismapped(), "Close actions scrolled away"
            first.copy_button.invoke()
            assert clipboard == [long_command], "Long command copy truncated"
            first.command_text.event_generate("<MouseWheel>", delta=-120)
            client.update()
            assert canvas.yview()[0] > 0, "Wheel over command does not scroll process list"
            gpu["processes"][2]["memory_mb"] = 16000
            refresh.invoke()
            client.update()
            assert process_cards()[0] == cards[2], "Processes not sorted by memory"
            gpu["processes"] = [dict(pid=778899, memory_mb=256, name="legacy-python", runtime_seconds=10)]
            refresh.invoke()
            client.update()
            legacy = process_cards()[0]
            assert len(process_cards()) == 1 and legacy.user.cget("text") == "未知用户"
            assert legacy.command_text.get("1.0", "end-1c") == "legacy-python", "Older agent fallback broken"
            gpu.update(processes=[], process_count=0)
            refresh.invoke()
            client.update()
            assert not process_cards(), "Exited processes still visible"
            assert any(w.winfo_ismapped() and w.winfo_class() == "Label" and w.cget("text") == "该显卡当前没有计算进程" for w in descendants(dialog))
            gpu.update(copy.deepcopy(saved_gpu))
            refresh.invoke()
            client.update()
            assert len(process_cards()) == 3, "New processes did not appear"
            client.state["server_states"]["default"]["connected"] = False
            refresh.invoke()
            client.update()
            assert not process_cards(), "Disconnected inspector shows stale processes"
            assert all(w["value"] == 0 for w in descendants(dialog) if isinstance(w, ui.ResourceBar))
            client.state = copy.deepcopy(data)
            refresh.invoke()
            client.update()
            assert len(process_cards()) == 3, "Reconnect did not restore inspector"
            client.state["server_states"]["default"]["snapshot"]["gpus"][0]["processes"][0]["runtime_seconds"] += 60
            client.after(2100, client.quit)
            client.mainloop()
            assert "02:01:01" in process_cards()[0].meta_labels[1].cget("text"), "Automatic refresh did not update process counters"
            dialog.destroy()
            client.after(2200, client.quit)
            client.mainloop()
            assert not errors, "Destroyed inspector left a scheduled callback"
            client.refresh_state()
            # Full logs use a fake connected runtime; the network worker stays
            # disconnected and the data source contains only synthetic text.
            runtime_for = client.manager.runtime_for
            client.manager.runtime_for = lambda server_id: types.SimpleNamespace(connected=True)
            client.manager.read_full_log = lambda item: "Full synthetic log\n" * 100
            client.selected_log = "demo-9"
            client.open_full_log()
            client.after(250, client.quit)
            client.mainloop()
            client.manager.runtime_for = runtime_for
            for dialog in [w for w in client.winfo_children() if w.winfo_class() == "Toplevel"]:
                assert dialog.ui_footer.winfo_ismapped(), "Full log missing footer"
                assert any("Full synthetic log" in w.get("1.0", "end") for w in descendants(dialog) if w.winfo_class() == "Text"), "Full log did not load"
                dialog.destroy()
            for kind in ("info", "warning", "error", "question"):
                def dismiss() -> None:
                    dialog = next(w for w in client.winfo_children() if w.winfo_class() == "Toplevel")
                    text = "取消" if kind == "question" else "知道了"
                    next(w for w in descendants(dialog.ui_footer) if w.winfo_class() == "TButton" and w.cget("text") == text).invoke()
                client.after(50, dismiss)
                accepted = client._message(kind, "演示提示", "Synthetic UI validation message")
                assert accepted == (kind != "question"), "Confirmation result changed"
            # Submit the real form callbacks to a capture sink, checking that
            # the visual refactor still passes every scheduling option.
            script = Path(directory) / "demo.sh"
            script.write_text("#!/bin/sh\necho demo\n", encoding="utf-8")
            saved = []
            client.add_experiment = lambda *args: saved.append(args)
            client.update_experiment = lambda *args: saved.append(args)
            def choose_demo(variable, label):
                variable.set(str(script))
                label.configure(text=script.name)
            client.choose_script = choose_demo
            for editing in (False, True):
                client.open_experiment_dialog(data["experiments"][0] if editing else None)
                client.update()
                dialog = next(w for w in client.winfo_children() if w.winfo_class() == "Toplevel")
                basic, scheduling = dialog.ui_content.winfo_children()
                entries = [w for w in descendants(basic) if w.winfo_class() == "TEntry"]
                for entry, value in zip(entries, ("UI 验证实验", "/home/research")):
                    entry.delete(0, "end")
                    entry.insert(0, value)
                combos = [w for w in descendants(basic) if w.winfo_class() == "TCombobox"]
                combos[1].current(list(combos[1].cget("values")).index("pytorch"))
                combos[1].event_generate("<<ComboboxSelected>>")
                controls = list(descendants(scheduling))
                level = next(w for w in controls if w.winfo_class() == "TCombobox")
                level.current(1)
                level.event_generate("<<ComboboxSelected>>")
                client.update()
                for widget, value in zip([w for w in controls if w.winfo_class() == "TSpinbox"], ("17", "25")):
                    widget.delete(0, "end")
                    widget.insert(0, value)
                memory = next(w for w in controls if w.winfo_class() == "TEntry")
                memory.delete(0, "end")
                memory.insert(0, "12000")
                dependency = next(w for w in descendants(basic) if w.winfo_class() == "Listbox")
                dependency.selection_set(0, 2)
                next(w for w in descendants(basic) if w.winfo_class() == "TButton" and w.cget("text") == "选择文件…").invoke()
                next(w for w in descendants(dialog.ui_footer) if w.winfo_class() == "TButton" and w.cget("text") != "取消").invoke()
                assert saved[-1][0] == ("demo-0" if editing else "default")
                assert saved[-1][1:10] == ("UI 验证实验", str(script), "/home/research", "pytorch", 17, "low_interference", 12000, True, 25), saved[-1]
                assert saved[-1][10] == (["demo-1", "demo-2", "demo-3"] if editing else ["demo-0", "demo-1", "demo-2"]), "Dependencies dropped from form"
            # Verify transitions to empty/offline resources rebuild cleanly.
            data.update(servers=[], server_states={}, experiments=[])
            client.state = copy.deepcopy(data)
            for view in client.views:
                client._show_view(view)
                client.update()
            assert not errors, errors
            print("PASS: five views at three sizes; visible resource bars at 0/6.1/50/100%; enlarged logo; queue actions and filters; scrollable logs; GPU resize; six themed dialogs; live GPU process cards, exact command copy and wrapping, narrow layout, sorting and process churn, offline/reconnect states, callback cleanup; dropdowns, numeric input and checkboxes; four message/confirmation styles; new/edit form payloads including dependencies; empty states; no Tk callback errors.")
        finally:
            client.destroy()


if __name__ == "__main__":
    run()
