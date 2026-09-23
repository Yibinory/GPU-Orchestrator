from __future__ import annotations

import base64
import ctypes
import shutil
import sys
import threading
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import tkinter as tk
from tkinter import filedialog, messagebox, ttk

from app import Runtime, UPLOAD_DIR, clean_name, now_iso, runtime as default_runtime, safe_float, safe_int, store


# A native Windows desktop shell around the SSH/runtime layer. It does not start Flask.
COLORS = {
    "bg": "#f4f7f5",
    "surface": "#ffffff",
    "surface_soft": "#f8faf9",
    "ink": "#10221e",
    "muted": "#75827c",
    "line": "#e1eae5",
    "green": "#13a673",
    "green_dark": "#087653",
    "mint": "#dff8ed",
    "lime": "#bdf25b",
    "amber": "#dfa04b",
    "purple": "#9174d5",
    "blue": "#6ca9ed",
    "red": "#df5d58",
    "navy": "#102d29",
    "navy_soft": "#1c4a3f",
}

FONT = "Microsoft YaHei UI"
MONO = "Consolas"
SHANGHAI_TZ = timezone(timedelta(hours=8))
ASSET_DIR = Path(__file__).resolve().parent / "assets"
APP_LOGO_PATH = ASSET_DIR / "gpu_orchestrator_icon.png"
APP_ICON_PATH = ASSET_DIR / "gpu_orchestrator.ico"


def fmt_bytes(value: Any) -> str:
    try:
        number = float(value or 0)
    except (TypeError, ValueError):
        number = 0
    if not number:
        return "—"
    units = ("B", "KB", "MB", "GB", "TB")
    index = 0
    while number >= 1024 and index < len(units) - 1:
        number /= 1024
        index += 1
    return f"{number:.1f} {units[index]}" if index >= 3 else f"{number:.0f} {units[index]}"


def fmt_mb(value: Any) -> str:
    try:
        return f"{int(float(value)):,} MB"
    except (TypeError, ValueError):
        return "—"


def fmt_time(value: str) -> str:
    if not value:
        return "—"
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.astimezone(SHANGHAI_TZ).strftime("%Y-%m-%d %H:%M")
    except (TypeError, ValueError):
        return str(value).replace("T", " ")[:16]


def fmt_duration(value: Any) -> str:
    try:
        seconds = max(0, int(float(value or 0)))
    except (TypeError, ValueError):
        return "未知"
    days, seconds = divmod(seconds, 86400)
    hours, seconds = divmod(seconds, 3600)
    minutes, seconds = divmod(seconds, 60)
    if days:
        return f"{days}天 {hours:02d}:{minutes:02d}:{seconds:02d}"
    return f"{hours:02d}:{minutes:02d}:{seconds:02d}"


def task_identifier(item: dict[str, Any]) -> str:
    number = safe_int(item.get("task_no"), 0)
    if number > 0:
        return f"#{number:04d}"
    return f"#{str(item.get('id') or 'unknown')[:8]}"


STATUS_LABELS = {
    "queued": "等待调度",
    "running": "运行中",
    "paused": "暂停中",
    "success": "已完成",
    "failed": "执行失败",
    "waiting_memory": "等待显存",
    "canceled": "已取消",
}

EXECUTION_LEVEL_LABELS = {
    "idle_only": "默认执行 · 仅空闲 GPU",
    "low_interference": "低干扰执行 · 忙卡低于利用率限制",
    "emergency": "紧急执行 · 有足够显存即运行",
}
EXECUTION_LEVEL_KEYS = {label: key for key, label in EXECUTION_LEVEL_LABELS.items()}


class DesktopRuntimeManager:
    """Owns one SSH/runtime worker per configured server."""

    def __init__(self) -> None:
        self.store = store
        self.runtimes: dict[str, Runtime] = {"default": default_runtime}
        self._ensure_server_config()
        self._ensure_experiment_numbers()
        self._sync_runtimes()

    def _ensure_server_config(self) -> None:
        data = self.store.data
        servers = data.setdefault("servers", [])
        changed = False
        global_settings = data.setdefault("global_settings", {})
        history_samples = max(1, min(120, safe_int(global_settings.get("gpu_history_samples"), 5)))
        if global_settings.get("gpu_history_samples") != history_samples:
            global_settings["gpu_history_samples"] = history_samples
            changed = True
        data.setdefault("gpu_history", {})
        default_record = next((item for item in servers if item.get("id") == "default"), None)
        if default_record is None:
            profile = dict(data.get("profile", {}))
            default_record = {
                "id": "default",
                "name": profile.get("host") or "默认服务器",
                "profile": profile,
                "preferences": dict(data.get("preferences", {})),
                "auto_connect": bool(profile.get("host") and profile.get("username")),
                "enabled": True,
            }
            servers.insert(0, default_record)
            changed = True
        if not default_record.get("_auto_connect_configured"):
            profile = default_record.get("profile") or data.get("profile") or {}
            default_record["auto_connect"] = bool(profile.get("host") and profile.get("username"))
            default_record["_auto_connect_configured"] = True
            changed = True
        for record in servers:
            if "disk_alert_gb" not in record:
                record["disk_alert_gb"] = 5
                changed = True
            else:
                old_threshold = record.get("disk_alert_gb")
                record["disk_alert_gb"] = max(0, safe_float(old_threshold, 5))
                if old_threshold != record["disk_alert_gb"]:
                    changed = True
            if "enabled" not in record:
                record["enabled"] = True
                changed = True
            if "scheduler_paused" not in record:
                record["scheduler_paused"] = False
                changed = True
            if "scheduler_pause_reason" not in record:
                record["scheduler_pause_reason"] = ""
                changed = True
        for experiment in data.setdefault("experiments", []):
            if not experiment.get("server_id"):
                experiment["server_id"] = "default"
                changed = True
        active = data.get("active_server_id")
        if not active or not any(item.get("id") == active for item in servers):
            data["active_server_id"] = servers[0].get("id", "default")
            changed = True
        if changed:
            self.store.save()

    def _ensure_experiment_numbers(self) -> None:
        """Migrate old tasks and keep a never-reused human-facing task number."""
        data = self.store.data
        experiments = data.setdefault("experiments", [])
        highest = max(
            [
                safe_int(item.get("task_no"), safe_int(item.get("created_seq"), 0))
                for item in experiments
            ]
            or [0]
        )
        next_number = max(safe_int(data.get("next_experiment_no"), 1), highest + 1, 1)
        used: set[int] = set()
        changed = False
        for item in experiments:
            number = safe_int(item.get("task_no"), 0)
            if number <= 0 or number in used:
                legacy_number = safe_int(item.get("created_seq"), 0)
                number = legacy_number if legacy_number > 0 and legacy_number not in used else next_number
            used.add(number)
            next_number = max(next_number, number + 1)
            if item.get("task_no") != number:
                item["task_no"] = number
                changed = True
        if safe_int(data.get("next_experiment_no"), 0) != next_number:
            data["next_experiment_no"] = next_number
            changed = True
        if changed:
            self.store.save()

    def _sync_runtimes(self) -> None:
        for record in self.store.data.get("servers", []):
            server_id = str(record.get("id"))
            if server_id and server_id not in self.runtimes:
                self.runtimes[server_id] = Runtime(self.store, server_id)

    @property
    def active_server_id(self) -> str:
        return str(self.store.data.get("active_server_id") or "default")

    @property
    def active_runtime(self) -> Runtime:
        self._sync_runtimes()
        return self.runtimes.setdefault(self.active_server_id, default_runtime)

    @property
    def connected(self) -> bool:
        record = next((item for item in self.store.data.get("servers", []) if item.get("id") == self.active_server_id), {})
        return bool(record.get("enabled", True) and self.active_runtime.connected and self.active_runtime.remote.connected)

    @property
    def remote(self):
        return self.active_runtime.remote

    def runtime_for(self, server_id: str | None) -> Runtime:
        self._sync_runtimes()
        return self.runtimes.get(server_id or "default", default_runtime)

    def _visible_experiments(self) -> list[dict[str, Any]]:
        visible = []
        for item in self.store.data.get("experiments", []):
            visible.append({key: value for key, value in item.items() if key != "local_script"})
        return visible

    def public_state(self) -> dict[str, Any]:
        self._sync_runtimes()
        active_state = self.active_runtime.public_state()
        servers = []
        server_states: dict[str, dict[str, Any]] = {}
        for record in self.store.data.get("servers", []):
            server_id = str(record.get("id"))
            state = self.runtime_for(server_id).public_state()
            profile = state.get("profile") or {}
            servers.append({
                "id": server_id,
                "name": record.get("name") or profile.get("host") or server_id,
                "host": profile.get("host", ""),
                "port": profile.get("port", 22),
                "username": profile.get("username", ""),
                "home": profile.get("home", ""),
                "connected": bool(record.get("enabled", True) and state.get("connected", False)),
                "enabled": bool(record.get("enabled", True)),
                "auto_connect": bool(record.get("auto_connect")),
                "last_error": state.get("last_error", ""),
                "last_poll_at": state.get("last_poll_at", ""),
                "scheduler_paused": bool(state.get("scheduler_paused")),
                "scheduler_pause_reason": state.get("scheduler_pause_reason", ""),
                "disk_blocked": bool((state.get("disk_guard") or {}).get("blocked")),
                "disk_alert_gb": safe_float(record.get("disk_alert_gb"), 5),
            })
            server_states[server_id] = state
        active_state["experiments"] = self._visible_experiments()
        active_state["benchmark_history"] = sorted(self.store.data.get("benchmark_history", []), key=lambda item: item.get("created_at", ""))[-48:]
        active_state["servers"] = servers
        active_state["server_states"] = server_states
        active_state["active_server_id"] = self.active_server_id
        active_state["global_settings"] = dict(self.store.data.setdefault("global_settings", {}))
        active_record = next((item for item in self.store.data.get("servers", []) if item.get("id") == self.active_server_id), {})
        active_state["connected"] = bool(active_record.get("enabled", True) and active_state.get("connected", False))
        return active_state

    def set_active(self, server_id: str) -> None:
        if not any(item.get("id") == server_id for item in self.store.data.get("servers", [])):
            return
        self.store.data["active_server_id"] = server_id
        self.store.save()

    def upsert_server(self, server_id: str | None, name: str, profile: dict[str, Any], auto_connect: bool) -> str:
        self._sync_runtimes()
        server_id = server_id or f"server-{uuid.uuid4().hex[:8]}"
        record = next((item for item in self.store.data.setdefault("servers", []) if item.get("id") == server_id), None)
        if record is None:
            record = {"id": server_id}
            self.store.data["servers"].append(record)
        record.update({"name": name or profile.get("host") or server_id, "profile": dict(profile), "auto_connect": bool(auto_connect), "_auto_connect_configured": True})
        record.setdefault("enabled", True)
        record.setdefault("preferences", {})
        record.setdefault("scheduler_paused", False)
        record.setdefault("scheduler_pause_reason", "")
        self.store.data["active_server_id"] = server_id
        self.store.save()
        self._sync_runtimes()
        return server_id

    def set_disk_alert_gb(self, server_id: str, threshold_gb: float) -> None:
        with self.store.lock:
            record = next((item for item in self.store.data.get("servers", []) if item.get("id") == server_id), None)
            if record is None:
                raise ValueError("服务器不存在")
            record["disk_alert_gb"] = max(0, min(1_000_000, safe_float(threshold_gb, 5)))
            self.store.save()
        runtime = self.runtime_for(server_id)
        with runtime.lock:
            if runtime.connected and runtime.snapshot is not None:
                runtime._update_disk_guard(runtime.snapshot)
                if runtime.disk_guard.get("blocked"):
                    runtime._pause_waiting_for_disk()
            else:
                runtime.disk_guard = {
                    "blocked": False,
                    "free_bytes": None,
                    "threshold_bytes": int(record["disk_alert_gb"] * 1024**3),
                    "message": "",
                }

    def set_gpu_history_samples(self, sample_count: int) -> None:
        sample_count = max(1, min(120, safe_int(sample_count, 5)))
        with self.store.lock:
            self.store.data.setdefault("global_settings", {})["gpu_history_samples"] = sample_count
            for server_history in self.store.data.setdefault("gpu_history", {}).values():
                for history in server_history.values():
                    del history[:-sample_count]
            self.store.save()

    def remove_server(self, server_id: str) -> None:
        if server_id == "default":
            raise ValueError("默认服务器不能删除")
        rt = self.runtime_for(server_id)
        if any(item.get("server_id") == server_id and item.get("status") == "running" for item in self.store.data.get("experiments", [])):
            raise ValueError("该服务器还有运行中的实验，不能删除")
        rt.disconnect()
        self.store.data["servers"] = [item for item in self.store.data.get("servers", []) if item.get("id") != server_id]
        self.store.data["active_server_id"] = "default"
        self.store.save()

    def set_enabled(self, server_id: str, enabled: bool) -> None:
        record = next((item for item in self.store.data.get("servers", []) if item.get("id") == server_id), None)
        if record is None:
            raise ValueError("服务器不存在")
        record["enabled"] = bool(enabled)
        self.store.save()
        if enabled:
            self.auto_connect()
        else:
            self.runtime_for(server_id).disconnect()

    def _experiment_record(self, exp_id: str) -> dict[str, Any] | None:
        return next((item for item in self.store.data.get("experiments", []) if item.get("id") == exp_id), None)

    def set_experiment_paused(self, exp_id: str, paused: bool) -> None:
        with self.store.lock:
            stored = self._experiment_record(exp_id)
            if stored is None:
                raise ValueError("实验不存在")
            server_id = str(stored.get("server_id") or "default")
            status = stored.get("status")
        runtime = self.runtime_for(server_id)
        if paused:
            if status == "paused":
                return
            if status not in ("queued", "waiting_memory", "running"):
                raise ValueError("只有等待中或执行中的任务可以暂停")
            runtime.pause_experiment(exp_id)
        else:
            if status != "paused":
                return
            runtime.resume_experiment(exp_id)

    def pause_all_experiments(self) -> list[str]:
        with self.store.lock:
            server_ids = [str(item.get("id")) for item in self.store.data.get("servers", [])]
            task_ids = [
                str(item.get("id"))
                for item in self.store.data.get("experiments", [])
                if item.get("status") in ("queued", "waiting_memory", "running")
            ]
            for record in self.store.data.get("servers", []):
                record["scheduler_paused"] = True
                record["scheduler_pause_reason"] = "用户手动暂停全部任务"
            self.store.save()
        errors: list[str] = []
        for exp_id in task_ids:
            try:
                self.set_experiment_paused(exp_id, True)
            except Exception as exc:
                errors.append(f"{exp_id}: {exc}")
        if errors:
            raise RuntimeError("；".join(errors))
        return server_ids

    def resume_all_experiments(self) -> None:
        with self.store.lock:
            task_ids = [
                str(item.get("id"))
                for item in self.store.data.get("experiments", [])
                if item.get("status") == "paused"
            ]
            for record in self.store.data.get("servers", []):
                record["scheduler_paused"] = False
                record["scheduler_pause_reason"] = ""
            self.store.save()
        errors: list[str] = []
        for exp_id in task_ids:
            try:
                with self.store.lock:
                    stored = self._experiment_record(exp_id)
                    server_id = str((stored or {}).get("server_id") or "default")
                    paused_process = bool((stored or {}).get("paused_process"))
                runtime = self.runtime_for(server_id)
                if paused_process:
                    runtime.requeue_experiment(exp_id)
                else:
                    runtime.resume_experiment(exp_id)
            except Exception as exc:
                errors.append(f"{exp_id}: {exc}")
        if errors:
            raise RuntimeError("；".join(errors))

    def terminate_experiment(self, exp_id: str) -> None:
        with self.store.lock:
            stored = self._experiment_record(exp_id)
            if stored is None:
                raise ValueError("实验不存在")
            server_id = str(stored.get("server_id") or "default")
        self.runtime_for(server_id).terminate_experiment(exp_id)

    def delete_experiment(self, exp_id: str) -> None:
        local_script: Path | None = None
        with self.store.lock:
            stored = self._experiment_record(exp_id)
            if stored is None:
                raise ValueError("实验不存在")
            if stored.get("status") == "running" or stored.get("paused_process"):
                raise ValueError("正在执行或已暂停进程的任务不能直接删除，请先中断任务")
            dependents = [
                item
                for item in self.store.data.get("experiments", [])
                if exp_id in [str(value) for value in (item.get("depends_on") or [])]
            ]
            if dependents:
                labels = []
                for item in dependents[:3]:
                    task_no = safe_int(item.get("task_no"), 0)
                    labels.append(f"#{task_no:04d} {item.get('name') or item.get('id')}" if task_no else str(item.get("name") or item.get("id")))
                raise ValueError("仍有任务依赖它：" + "、".join(labels))
            raw_script = str(stored.get("local_script") or "").strip()
            if raw_script:
                local_script = Path(raw_script)
            self.store.data["experiments"] = [
                item for item in self.store.data.get("experiments", [])
                if str(item.get("id")) != exp_id
            ]
            self.store.save()
        if local_script is not None:
            try:
                upload_root = UPLOAD_DIR.resolve()
                candidate = local_script.resolve()
                candidate.relative_to(upload_root)
                candidate.unlink(missing_ok=True)
            except (OSError, ValueError):
                pass

    def connect(self, server_id: str, profile: dict[str, Any], password: str, save_password: bool) -> dict[str, Any]:
        record = next(item for item in self.store.data.get("servers", []) if item.get("id") == server_id)
        record["profile"] = dict(profile)
        self.store.save()
        selected_runtime = self.runtime_for(server_id)
        try:
            selected_runtime.connect(profile, password, save_password)
        except Exception as exc:
            selected_runtime.last_error = str(exc)
            self.store.save()
            raise
        return self.public_state()

    def disconnect(self, server_id: str | None = None) -> None:
        self.runtime_for(server_id or self.active_server_id).disconnect()

    def tick(self) -> None:
        for rt in list(self.runtimes.values()):
            record = next((item for item in self.store.data.get("servers", []) if item.get("id") == rt.server_id), {})
            if record.get("enabled", True) and rt.connected:
                rt.tick()
            elif not record.get("enabled", True) and rt.connected:
                rt.disconnect()

    def auto_connect(self) -> None:
        for record in self.store.data.get("servers", []):
            if not record.get("enabled", True) or not record.get("auto_connect"):
                continue
            server_id = str(record.get("id"))
            rt = self.runtime_for(server_id)
            profile = dict(record.get("profile") or {})
            if not profile.get("host") or not profile.get("username") or rt.connected:
                continue
            if not rt.saved_password(profile):
                continue

            def worker(selected=server_id, selected_profile=profile, selected_runtime=rt) -> None:
                try:
                    selected_runtime.connect(selected_profile, "", True)
                except Exception as exc:
                    selected_runtime.last_error = str(exc)

            threading.Thread(target=worker, daemon=True).start()

    def run_benchmark(self, server_id: str, gpu_index: int, seconds: int = 5, conda_env: str = "") -> dict[str, Any]:
        record = next((item for item in self.store.data.get("servers", []) if item.get("id") == server_id), {})
        if not record.get("enabled", True):
            raise RuntimeError("该服务器监控已停用，请先启用监控")
        return self.runtime_for(server_id).run_benchmark(gpu_index, seconds=seconds, conda_env=conda_env)

    def read_log(self, experiment: dict[str, Any]) -> str:
        log = self.runtime_for(str(experiment.get("server_id") or "default"))._read_log(experiment, max_bytes=24000)
        return "".join(log.splitlines(keepends=True)[-200:])

    def read_full_log(self, experiment: dict[str, Any]) -> str:
        runtime = self.runtime_for(str(experiment.get("server_id") or "default"))
        path = str(experiment.get("log_path") or "")
        if not path:
            return ""
        return runtime.remote.read_text(path, tail=None)


class DesktopClient(tk.Tk):
    def __init__(self) -> None:
        if sys.platform == "win32":
            try:
                ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID("Yibinory.GPUOrchestrator")
            except (AttributeError, OSError):
                pass
        super().__init__()
        self.title("算力调度台 · GPU Orchestrator")
        self.geometry("1280x820")
        self.minsize(1080, 700)
        self.configure(bg=COLORS["bg"])
        self.protocol("WM_DELETE_WINDOW", self.on_close)

        self.manager = DesktopRuntimeManager()
        self.state: dict[str, Any] = self.manager.public_state()
        self.current_view = "dashboard"
        self.selected_log = None
        self.queue_filter = "all"
        self.benchmark_running: set[tuple[str, int]] = set()
        self.log_loading = False
        self.gpu_widgets: dict[int, dict[str, Any]] = {}
        self.gpu_topology: tuple[Any, ...] | None = None
        self.activity_signature: tuple[Any, ...] | None = None
        self.storage_signature: tuple[Any, ...] | None = None
        self.queue_signature: tuple[Any, ...] | None = None
        self.benchmark_signature: tuple[Any, ...] | None = None
        self.benchmark_widgets: dict[tuple[str, int], dict[str, Any]] = {}
        self.history_signature: tuple[Any, ...] | None = None
        self.log_signature: tuple[Any, ...] | None = None
        self.dashboard_empty = False
        self.refresh_after_id: str | None = None
        self.dashboard_server_signature: tuple[Any, ...] | None = None
        self.dashboard_server_panels: dict[str, dict[str, Any]] = {}
        self.status_var = tk.StringVar(value="准备就绪")
        self.header_kicker = tk.StringVar(value="LIVE RESOURCE MAP")
        self.header_title = tk.StringVar(value="资源总览")
        self.monitor_summary = tk.StringVar(value="监控服务器：0 · 活跃：0")
        self.connection_text = tk.StringVar(value="离线")
        self.last_sync = tk.StringVar(value="尚未同步")

        self._configure_styles()
        self._load_brand_assets()
        self._build_shell()
        self._show_view("dashboard")
        self.after(250, self.refresh_state)
        self.after(750, self.manager.auto_connect)
        self.after(5000, self.refresh_logs_if_needed)

    def _load_brand_assets(self) -> None:
        """Apply the project logo to the window and keep a small sidebar copy."""
        self.app_logo_image: tk.PhotoImage | None = None
        self.sidebar_logo_image: tk.PhotoImage | None = None
        bitmap_loaded = False
        try:
            if APP_ICON_PATH.exists():
                self.iconbitmap(default=str(APP_ICON_PATH))
                bitmap_loaded = True
        except tk.TclError:
            pass
        try:
            if APP_LOGO_PATH.exists():
                encoded_logo = base64.b64encode(APP_LOGO_PATH.read_bytes()).decode("ascii")
                self.app_logo_image = tk.PhotoImage(data=encoded_logo)
                if not bitmap_loaded:
                    try:
                        self.iconphoto(True, self.app_logo_image)
                    except tk.TclError:
                        pass
                scale = max(1, self.app_logo_image.width() // 32)
                self.sidebar_logo_image = self.app_logo_image.subsample(scale, scale)
        except (OSError, tk.TclError):
            self.app_logo_image = None
            self.sidebar_logo_image = None

    def _configure_styles(self) -> None:
        style = ttk.Style(self)
        try:
            style.theme_use("clam")
        except tk.TclError:
            pass
        style.configure("App.TFrame", background=COLORS["bg"])
        style.configure("Card.TFrame", background=COLORS["surface"])
        style.configure("Treeview", background=COLORS["surface"], fieldbackground=COLORS["surface"], foreground=COLORS["ink"], rowheight=36, font=(FONT, 10), borderwidth=0)
        style.configure("Treeview.Heading", background=COLORS["surface_soft"], foreground=COLORS["muted"], font=(FONT, 9, "bold"), relief="flat")
        style.map("Treeview", background=[("selected", COLORS["mint"])], foreground=[("selected", COLORS["green_dark"])])
        style.configure(
            "TCombobox",
            padding=(10, 6),
            arrowsize=14,
            font=(FONT, 10),
            foreground=COLORS["ink"],
            fieldbackground=COLORS["surface"],
            background=COLORS["surface"],
            bordercolor=COLORS["line"],
            lightcolor=COLORS["line"],
            darkcolor=COLORS["line"],
            arrowcolor=COLORS["green_dark"],
            selectbackground=COLORS["mint"],
            selectforeground=COLORS["green_dark"],
            relief="flat",
        )
        style.map(
            "TCombobox",
            fieldbackground=[("disabled", "#f0f4f2"), ("readonly", COLORS["surface"]), ("focus", COLORS["surface"])],
            foreground=[("disabled", "#a2afa9"), ("readonly", COLORS["ink"])],
            bordercolor=[("focus", COLORS["green"]), ("!focus", COLORS["line"])],
            lightcolor=[("focus", COLORS["green"]), ("!focus", COLORS["line"])],
            darkcolor=[("focus", COLORS["green_dark"]), ("!focus", COLORS["line"])],
            arrowcolor=[("disabled", "#a2afa9"), ("focus", COLORS["green_dark"]), ("!focus", COLORS["green_dark"])],
        )
        style.configure(
            "TScrollbar",
            background="#bfd6ca",
            troughcolor="#edf3ef",
            bordercolor="#edf3ef",
            lightcolor="#edf3ef",
            darkcolor="#edf3ef",
            arrowcolor="#5f7f72",
            borderwidth=0,
            relief="flat",
            width=11,
        )
        style.map(
            "TScrollbar",
            background=[("pressed", "#76b396"), ("active", "#9fcab4"), ("!active", "#bfd6ca")],
            arrowcolor=[("pressed", COLORS["green_dark"]), ("active", COLORS["green_dark"]), ("!active", "#5f7f72")],
        )
        style.configure("Green.Horizontal.TProgressbar", troughcolor="#e9f0ec", background=COLORS["green"], bordercolor="#e9f0ec", lightcolor=COLORS["green"], darkcolor=COLORS["green"])
        style.configure("Purple.Horizontal.TProgressbar", troughcolor="#eeeaf8", background=COLORS["purple"], bordercolor="#eeeaf8", lightcolor=COLORS["purple"], darkcolor=COLORS["purple"])
        style.configure("Blue.Horizontal.TProgressbar", troughcolor="#eaf2fc", background=COLORS["blue"], bordercolor="#eaf2fc", lightcolor=COLORS["blue"], darkcolor=COLORS["blue"])
        style.configure("Amber.Horizontal.TProgressbar", troughcolor="#fbf2e2", background=COLORS["amber"], bordercolor="#fbf2e2", lightcolor=COLORS["amber"], darkcolor=COLORS["amber"])
        style.configure("TEntry", padding=6, font=(FONT, 10))
        style.configure("TNotebook", background=COLORS["bg"], borderwidth=0)

    def _build_shell(self) -> None:
        self.sidebar = tk.Frame(self, bg=COLORS["navy"], width=245)
        self.sidebar.grid(row=0, column=0, sticky="ns")
        self.sidebar.grid_propagate(False)
        self.main = tk.Frame(self, bg=COLORS["bg"])
        self.main.grid(row=0, column=1, sticky="nsew")
        self.rowconfigure(0, weight=1)
        self.columnconfigure(1, weight=1)
        self._build_sidebar()
        self._build_main()

    def _build_sidebar(self) -> None:
        brand = tk.Frame(self.sidebar, bg=COLORS["navy"])
        brand.pack(fill="x", padx=18, pady=(28, 25))
        if self.sidebar_logo_image is not None:
            tk.Label(brand, image=self.sidebar_logo_image, bg=COLORS["navy"], bd=0).pack(side="left", padx=(0, 10))
        else:
            mark = tk.Canvas(brand, width=29, height=30, bg=COLORS["navy"], highlightthickness=0)
            mark.pack(side="left", padx=(0, 10))
            mark.create_polygon(3, 25, 8, 25, 13, 9, 9, 9, fill=COLORS["lime"], outline="")
            mark.create_polygon(11, 25, 16, 25, 22, 3, 18, 3, fill=COLORS["lime"], outline="")
            mark.create_polygon(19, 25, 24, 25, 28, 12, 24, 12, fill=COLORS["lime"], outline="")
        tk.Label(brand, text="算力调度台", bg=COLORS["navy"], fg="#f3fff9", font=(FONT, 15, "bold")).pack(anchor="w")
        tk.Label(brand, text="GPU ORCHESTRATOR", bg=COLORS["navy"], fg="#79a495", font=(MONO, 8)).pack(anchor="w", pady=(3, 0))

        server = tk.Frame(self.sidebar, bg="#193c35", highlightbackground="#285449", highlightthickness=1)
        server.pack(fill="x", padx=18, pady=(0, 28))
        tk.Label(server, text="监控概览", bg="#193c35", fg="#78a094", font=(MONO, 9)).pack(anchor="w", padx=14, pady=(14, 7))
        tk.Label(server, textvariable=self.monitor_summary, bg="#193c35", fg="#ffffff", font=(FONT, 11, "bold"), anchor="w").pack(fill="x", padx=14)
        tk.Label(server, text="多服务器资源实时同步", bg="#193c35", fg="#8aa99f", font=(FONT, 9), anchor="w").pack(fill="x", padx=14, pady=(5, 10))
        state_row = tk.Frame(server, bg="#193c35")
        state_row.pack(fill="x", padx=14, pady=(0, 14))
        self.connection_dot = tk.Canvas(state_row, width=9, height=9, bg="#193c35", highlightthickness=0)
        self.connection_dot.pack(side="left", padx=(0, 7))
        self.connection_dot.create_oval(1, 1, 8, 8, fill=COLORS["red"], outline="")
        tk.Label(state_row, textvariable=self.connection_text, bg="#193c35", fg="#8fe4c2", font=(FONT, 9)).pack(side="left")
        tk.Button(server, text="服务器管理", command=self.open_server_manager, relief="flat", bd=0, bg="#235047", fg="#bde9d7", activebackground="#2e6257", activeforeground="#ffffff", font=(FONT, 9), pady=5).pack(fill="x", padx=11, pady=(0, 11))

        self.nav_buttons: dict[str, tk.Button] = {}
        for view, label, symbol in (
            ("dashboard", "资源总览", "◈"),
            ("queue", "实验队列", "≡"),
            ("benchmarks", "GPU 测试", "ϟ"),
            ("logs", "运行日志", "▤"),
            ("settings", "全局设置", "⚙"),
        ):
            button = tk.Button(
                self.sidebar,
                text=f"  {symbol}    {label}",
                command=lambda current=view: self._show_view(current),
                relief="flat",
                bd=0,
                anchor="w",
                padx=14,
                pady=11,
                bg=COLORS["navy"],
                fg="#8da9a0",
                activebackground=COLORS["navy_soft"],
                activeforeground="#f7fff9",
                font=(FONT, 11),
            )
            button.pack(fill="x", padx=12, pady=2)
            self.nav_buttons[view] = button

        bottom = tk.Frame(self.sidebar, bg=COLORS["navy"])
        bottom.pack(side="bottom", fill="x", padx=18, pady=18)
        tip = tk.Frame(bottom, bg="#16372f", highlightbackground="#2b5146", highlightthickness=1)
        tip.pack(fill="x", pady=(0, 21))
        tk.Label(tip, text="✦", bg="#16372f", fg=COLORS["lime"], font=(FONT, 15)).pack(side="left", anchor="n", padx=(10, 6), pady=11)
        tk.Label(tip, text="调度策略\n空闲优先 · 紧急任务按显存与\nTensor 速度择卡", justify="left", bg="#16372f", fg="#9dc1b4", font=(FONT, 9), padx=0, pady=10).pack(side="left")
        tk.Label(bottom, text="NATIVE DESKTOP CLIENT · v0.2", bg=COLORS["navy"], fg="#52786c", font=(MONO, 8)).pack(anchor="w")

    def _build_main(self) -> None:
        topbar = tk.Frame(self.main, bg=COLORS["bg"], height=94)
        topbar.pack(fill="x", padx=36, pady=(0, 3))
        topbar.pack_propagate(False)
        heading = tk.Frame(topbar, bg=COLORS["bg"])
        heading.pack(side="left", anchor="center")
        tk.Label(heading, textvariable=self.header_kicker, bg=COLORS["bg"], fg="#8a9993", font=(MONO, 8)).pack(anchor="w")
        tk.Label(heading, textvariable=self.header_title, bg=COLORS["bg"], fg=COLORS["ink"], font=(FONT, 24, "bold")).pack(anchor="w", pady=(5, 0))
        actions = tk.Frame(topbar, bg=COLORS["bg"])
        actions.pack(side="right", anchor="center")
        tk.Label(actions, textvariable=self.last_sync, bg=COLORS["bg"], fg="#8a9893", font=(MONO, 9)).pack(side="left", padx=(0, 12))
        tk.Button(actions, text="↻", command=self.manual_refresh, relief="flat", bd=0, bg=COLORS["bg"], fg="#788681", activebackground=COLORS["bg"], font=(FONT, 19)).pack(side="left", padx=(0, 10))
        self.connect_button = tk.Button(actions, text="服务器管理  →", command=self.open_server_manager, relief="flat", bd=0, bg=COLORS["green"], fg="#ffffff", activebackground=COLORS["green_dark"], activeforeground="#ffffff", font=(FONT, 10, "bold"), padx=15, pady=9)
        self.connect_button.pack(side="left")

        self.content = tk.Frame(self.main, bg=COLORS["bg"])
        self.content.pack(fill="both", expand=True, padx=36, pady=(0, 22))
        self.views: dict[str, tk.Frame] = {}
        self._build_dashboard()
        self._build_queue()
        self._build_benchmarks()
        self._build_logs()
        self._build_settings()
        self.status_bar = tk.Label(self.main, textvariable=self.status_var, bg="#eaf1ed", fg="#6e8077", anchor="w", padx=12, pady=5, font=(FONT, 9))
        self.status_bar.pack(fill="x", side="bottom")

    def _new_view(self, name: str) -> tk.Frame:
        frame = tk.Frame(self.content, bg=COLORS["bg"])
        self.views[name] = frame
        return frame

    def _card(self, parent: tk.Misc, **kwargs: Any) -> tk.Frame:
        return tk.Frame(parent, bg=COLORS["surface"], highlightbackground=COLORS["line"], highlightthickness=1, bd=0, **kwargs)

    def _entry(self, parent: tk.Misc, **kwargs: Any) -> tk.Entry:
        options: dict[str, Any] = {
            "bg": COLORS["surface_soft"],
            "fg": COLORS["ink"],
            "insertbackground": COLORS["green_dark"],
            "selectbackground": COLORS["mint"],
            "selectforeground": COLORS["green_dark"],
            "disabledbackground": "#eef3f0",
            "disabledforeground": "#9aa8a1",
            "relief": "flat",
            "bd": 0,
            "highlightthickness": 1,
            "highlightbackground": COLORS["line"],
            "highlightcolor": COLORS["green"],
            "font": (FONT, 10),
        }
        options.update(kwargs)
        return tk.Entry(parent, **options)

    def _spinbox(self, parent: tk.Misc, **kwargs: Any) -> tk.Spinbox:
        options: dict[str, Any] = {
            "bg": COLORS["surface_soft"],
            "fg": COLORS["ink"],
            "buttonbackground": COLORS["surface_soft"],
            "activebackground": COLORS["mint"],
            "insertbackground": COLORS["green_dark"],
            "selectbackground": COLORS["mint"],
            "selectforeground": COLORS["green_dark"],
            "disabledbackground": "#eef3f0",
            "disabledforeground": "#9aa8a1",
            "relief": "flat",
            "bd": 0,
            "highlightthickness": 1,
            "highlightbackground": COLORS["line"],
            "highlightcolor": COLORS["green"],
            "font": (FONT, 10),
        }
        options.update(kwargs)
        return tk.Spinbox(parent, **options)

    def _checkbutton(self, parent: tk.Misc, **kwargs: Any) -> tk.Checkbutton:
        options: dict[str, Any] = {
            "bg": COLORS["surface"],
            "fg": COLORS["muted"],
            "activebackground": COLORS["surface"],
            "activeforeground": COLORS["green_dark"],
            "selectcolor": COLORS["mint"],
            "disabledforeground": "#a2afa9",
            "highlightthickness": 0,
            "relief": "flat",
            "bd": 0,
            "font": (FONT, 9),
            "anchor": "w",
        }
        options.update(kwargs)
        return tk.Checkbutton(parent, **options)

    def _section_title(self, parent: tk.Misc, kicker: str, title: str) -> tk.Frame:
        row = tk.Frame(parent, bg=COLORS["bg"])
        tk.Label(row, text=kicker, bg=COLORS["bg"], fg="#84a49a", font=(MONO, 8)).pack(anchor="w")
        tk.Label(row, text=title, bg=COLORS["bg"], fg=COLORS["ink"], font=(FONT, 15, "bold")).pack(anchor="w", pady=(4, 0))
        return row

    def _build_dashboard(self) -> None:
        view = self._new_view("dashboard")
        scroll_shell = tk.Frame(view, bg=COLORS["bg"])
        scroll_shell.pack(fill="both", expand=True)
        self.dashboard_canvas = tk.Canvas(scroll_shell, bg=COLORS["bg"], highlightthickness=0, bd=0)
        dashboard_scroll = ttk.Scrollbar(scroll_shell, orient="vertical", command=self.dashboard_canvas.yview)
        self.dashboard_canvas.configure(yscrollcommand=dashboard_scroll.set)
        self.dashboard_canvas.pack(side="left", fill="both", expand=True)
        dashboard_scroll.pack(side="right", fill="y")
        self.dashboard_scroll_frame = tk.Frame(self.dashboard_canvas, bg=COLORS["bg"])
        dashboard_window = self.dashboard_canvas.create_window((0, 0), window=self.dashboard_scroll_frame, anchor="nw")
        self.dashboard_scroll_frame.bind("<Configure>", lambda _event: self.dashboard_canvas.configure(scrollregion=self.dashboard_canvas.bbox("all")))
        self.dashboard_canvas.bind("<Configure>", lambda event: self.dashboard_canvas.itemconfigure(dashboard_window, width=event.width))
        self.bind_all("<MouseWheel>", self._scroll_dashboard)
        self.bind_all("<Button-4>", self._scroll_dashboard)
        self.bind_all("<Button-5>", self._scroll_dashboard)
        body = self.dashboard_scroll_frame
        self.metric_vars: dict[str, tk.StringVar] = {}
        self.metric_bars: dict[str, ttk.Progressbar] = {}
        metrics = tk.Frame(body, bg=COLORS["bg"])
        metrics.pack(fill="x")
        for index, (key, title, code, color, detail) in enumerate((
            ("cpu", "CPU 使用率", "CPU", COLORS["green"], "负载 —"),
            ("ram", "系统内存", "RAM", COLORS["blue"], "— / —"),
            ("gpu", "GPU 显存占用", "GPU", COLORS["purple"], "— 张显卡"),
            ("disk", "主目录存储", "HOME", COLORS["amber"], "— 可用"),
        )):
            card = self._card(metrics)
            card.grid(row=0, column=index, sticky="nsew", padx=(0 if index == 0 else 7, 0))
            metrics.columnconfigure(index, weight=1)
            head = tk.Frame(card, bg=COLORS["surface"])
            head.pack(fill="x", padx=17, pady=(16, 0))
            tk.Label(head, text=title, bg=COLORS["surface"], fg=COLORS["muted"], font=(FONT, 9, "bold")).pack(side="left")
            tk.Label(head, text=code, bg=COLORS["surface"], fg="#b4c0bb", font=(MONO, 8)).pack(side="right")
            value_var = tk.StringVar(value="—")
            detail_var = tk.StringVar(value=detail)
            self.metric_vars[key] = value_var
            self.metric_vars[key + "_detail"] = detail_var
            tk.Label(card, textvariable=value_var, bg=COLORS["surface"], fg=COLORS["ink"], font=(FONT, 25, "bold")).pack(anchor="w", padx=17, pady=(9, 0))
            tk.Label(card, textvariable=detail_var, bg=COLORS["surface"], fg="#88958f", font=(FONT, 8)).pack(anchor="w", padx=17, pady=(5, 0))
            bar = ttk.Progressbar(card, style=f"{'Green' if key == 'cpu' else 'Blue' if key == 'ram' else 'Purple' if key == 'gpu' else 'Amber'}.Horizontal.TProgressbar", maximum=100, value=0)
            bar.pack(fill="x", padx=17, pady=(11, 16))
            self.metric_bars[key] = bar

        gpu_heading = tk.Frame(body, bg=COLORS["bg"])
        gpu_heading.pack(fill="x", pady=(29, 12))
        self._section_title(gpu_heading, "SERVER RESOURCE MAP", "服务器资源").pack(side="left")
        self.gpu_live = tk.Label(gpu_heading, text="● 实时轮询", bg=COLORS["bg"], fg=COLORS["green_dark"], font=(FONT, 9))
        self.gpu_live.pack(side="right", anchor="s")
        self.gpu_grid = tk.Frame(body, bg=COLORS["bg"])
        self.gpu_grid.pack(fill="x")

        lower = tk.Frame(body, bg=COLORS["bg"])
        lower.pack(fill="both", expand=True, pady=(14, 0))
        lower.columnconfigure(0, weight=3)
        lower.columnconfigure(1, weight=2)
        self.activity_card = self._card(lower)
        self.activity_card.grid(row=0, column=0, sticky="nsew", padx=(0, 7))
        self.storage_card = self._card(lower)
        self.storage_card.grid(row=0, column=1, sticky="nsew", padx=(7, 0))
        tk.Label(self.activity_card, text="QUEUE ACTIVITY", bg=COLORS["surface"], fg="#84a49a", font=(MONO, 8)).pack(anchor="w", padx=19, pady=(17, 0))
        activity_head = tk.Frame(self.activity_card, bg=COLORS["surface"])
        activity_head.pack(fill="x", padx=19)
        tk.Label(activity_head, text="队列动态", bg=COLORS["surface"], fg=COLORS["ink"], font=(FONT, 14, "bold")).pack(side="left", pady=(4, 10))
        tk.Button(activity_head, text="查看全部 →", command=lambda: self._show_view("queue"), bg=COLORS["surface"], fg=COLORS["green_dark"], relief="flat", bd=0, font=(FONT, 9, "bold")).pack(side="right")
        self.activity_body = tk.Frame(self.activity_card, bg=COLORS["surface"])
        self.activity_body.pack(fill="both", expand=True, padx=19, pady=(0, 14))
        tk.Label(self.storage_card, text="HOME STORAGE", bg=COLORS["surface"], fg="#84a49a", font=(MONO, 8)).pack(anchor="w", padx=19, pady=(17, 0))
        storage_head = tk.Frame(self.storage_card, bg=COLORS["surface"])
        storage_head.pack(fill="x", padx=19)
        tk.Label(storage_head, text="主目录空间", bg=COLORS["surface"], fg=COLORS["ink"], font=(FONT, 14, "bold")).pack(side="left", pady=(4, 10))
        self.storage_path_var = tk.StringVar(value="~")
        tk.Label(storage_head, textvariable=self.storage_path_var, bg=COLORS["surface"], fg="#81928a", font=(MONO, 8)).pack(side="right", pady=(7, 10))
        self.storage_body = tk.Frame(self.storage_card, bg=COLORS["surface"])
        self.storage_body.pack(fill="both", expand=True, padx=19, pady=(0, 14))

    def _scroll_dashboard(self, event: Any) -> None:
        if self.current_view != "dashboard":
            return
        if getattr(event, "num", None) == 4:
            delta = -3
        elif getattr(event, "num", None) == 5:
            delta = 3
        else:
            delta = -3 if getattr(event, "delta", 0) > 0 else 3
        self.dashboard_canvas.yview_scroll(delta, "units")

    def _build_queue(self) -> None:
        view = self._new_view("queue")
        heading = tk.Frame(view, bg=COLORS["bg"])
        heading.pack(fill="x", pady=(12, 20))
        left = tk.Frame(heading, bg=COLORS["bg"])
        left.pack(side="left")
        tk.Label(left, text="EXPERIMENT QUEUE", bg=COLORS["bg"], fg="#84a49a", font=(MONO, 8)).pack(anchor="w")
        tk.Label(left, text="实验运行队列", bg=COLORS["bg"], fg=COLORS["ink"], font=(FONT, 23, "bold")).pack(anchor="w", pady=(4, 0))
        tk.Label(left, text="按优先级、配置顺序与 GPU 基准速度自动调度。", bg=COLORS["bg"], fg="#87938e", font=(FONT, 9)).pack(anchor="w", pady=(5, 0))
        tk.Button(heading, text="＋ 提交 .sh 实验", command=self.open_experiment_dialog, relief="flat", bd=0, bg=COLORS["green"], fg="#ffffff", activebackground=COLORS["green_dark"], font=(FONT, 10, "bold"), padx=14, pady=8).pack(side="right", anchor="n")
        self.queue_summary_vars = {name: tk.StringVar(value="0") for name in ("queued", "running", "paused", "success", "failed")}
        summary = tk.Frame(view, bg=COLORS["bg"])
        summary.pack(fill="x", pady=(0, 13))
        for index, (key, label) in enumerate((("queued", "等待中"), ("running", "运行中"), ("paused", "暂停中"), ("success", "已完成"), ("failed", "需关注"))):
            card = self._card(summary)
            card.grid(row=0, column=index, sticky="ew", padx=(0 if index == 0 else 6, 0))
            summary.columnconfigure(index, weight=1)
            tk.Label(card, text=label, bg=COLORS["surface"], fg="#86938d", font=(FONT, 9)).pack(anchor="w", padx=15, pady=(13, 0))
            tk.Label(card, textvariable=self.queue_summary_vars[key], bg=COLORS["surface"], fg=COLORS["ink"], font=(FONT, 20, "bold")).pack(anchor="w", padx=15, pady=(4, 12))
        self.scheduler_notice_var = tk.StringVar(value="调度状态检查中")
        self.scheduler_notice = tk.Label(view, textvariable=self.scheduler_notice_var, bg="#eff9f3", fg=COLORS["green_dark"], anchor="w", padx=13, pady=8, font=(FONT, 9))
        self.scheduler_notice.pack(fill="x", pady=(0, 13))
        controls = tk.Frame(view, bg=COLORS["bg"])
        controls.pack(fill="x", pady=(0, 10))
        tk.Button(controls, text="查看日志", command=self.open_selected_log, relief="flat", bd=0, bg=COLORS["surface"], fg=COLORS["green_dark"], font=(FONT, 9, "bold"), padx=12, pady=7).pack(side="left")
        tk.Button(controls, text="编辑选中任务", command=self.edit_selected_experiment, relief="flat", bd=0, bg=COLORS["surface"], fg=COLORS["green_dark"], font=(FONT, 9), padx=12, pady=7).pack(side="left", padx=8)
        tk.Button(controls, text="重新等待选中任务", command=self.retry_selected, relief="flat", bd=0, bg=COLORS["surface"], fg=COLORS["green_dark"], font=(FONT, 9), padx=12, pady=7).pack(side="left", padx=8)
        tk.Button(controls, text="暂停 / 继续任务", command=self.toggle_selected_pause, relief="flat", bd=0, bg=COLORS["surface"], fg=COLORS["green_dark"], font=(FONT, 9), padx=12, pady=7).pack(side="left")
        tk.Button(controls, text="暂停全部任务", command=self.pause_all_tasks, relief="flat", bd=0, bg="#fff5e8", fg=COLORS["amber"], font=(FONT, 9), padx=12, pady=7).pack(side="left", padx=8)
        tk.Button(controls, text="全部重新等待", command=self.resume_all_tasks, relief="flat", bd=0, bg=COLORS["mint"], fg=COLORS["green_dark"], font=(FONT, 9), padx=12, pady=7).pack(side="left")
        tk.Button(controls, text="中断选中任务", command=self.cancel_selected, relief="flat", bd=0, bg=COLORS["surface"], fg=COLORS["red"], font=(FONT, 9), padx=12, pady=7).pack(side="left", padx=(8, 0))
        tk.Button(controls, text="删除选中任务", command=self.delete_selected_experiment, relief="flat", bd=0, bg="#fff1ef", fg=COLORS["red"], font=(FONT, 9), padx=12, pady=7).pack(side="left", padx=8)
        table_card = self._card(view)
        table_card.pack(fill="both", expand=True)
        toolbar = tk.Frame(table_card, bg=COLORS["surface"])
        toolbar.pack(fill="x", padx=14, pady=12)
        self.filter_buttons: dict[str, tk.Button] = {}
        for key, label in (("all", "全部"), ("queued", "等待"), ("running", "运行中"), ("paused", "暂停"), ("success", "已完成"), ("failed", "需关注")):
            button = tk.Button(toolbar, text=label, command=lambda current=key: self.set_queue_filter(current), relief="flat", bd=0, bg=COLORS["mint"] if key == "all" else COLORS["surface"], fg=COLORS["green_dark"] if key == "all" else COLORS["muted"], font=(FONT, 9, "bold" if key == "all" else "normal"), padx=10, pady=5)
            button.pack(side="left", padx=(0, 5))
            self.filter_buttons[key] = button
        tk.Label(toolbar, text="数字越小优先级越高", bg=COLORS["surface"], fg="#9aa59f", font=(FONT, 8)).pack(side="right")
        table_frame = tk.Frame(table_card, bg=COLORS["surface"])
        table_frame.pack(fill="both", expand=True, padx=12, pady=(0, 12))
        columns = ("name", "server", "status", "priority", "strategy", "gpu", "memory")
        self.queue_tree = ttk.Treeview(table_frame, columns=columns, show="headings", selectmode="browse")
        headings = {"name": "实验", "server": "服务器", "status": "状态", "priority": "优先级", "strategy": "执行策略", "gpu": "目标 GPU", "memory": "显存门槛"}
        widths = {"name": 220, "server": 130, "status": 100, "priority": 75, "strategy": 150, "gpu": 180, "memory": 105}
        for column in columns:
            self.queue_tree.heading(column, text=headings[column])
            self.queue_tree.column(column, width=widths[column], anchor="w")
        self.queue_tree.tag_configure("running", foreground=COLORS["green_dark"])
        self.queue_tree.tag_configure("success", foreground="#4b944f")
        self.queue_tree.tag_configure("failed", foreground=COLORS["red"])
        self.queue_tree.tag_configure("paused", foreground=COLORS["amber"])
        self.queue_tree.tag_configure("waiting_memory", foreground="#bd504a")
        self.queue_tree.pack(fill="both", expand=True, side="left")
        scroll = ttk.Scrollbar(table_frame, orient="vertical", command=self.queue_tree.yview)
        scroll.pack(side="right", fill="y")
        self.queue_tree.configure(yscrollcommand=scroll.set)
        self.queue_tree.bind("<Double-1>", lambda _event: self.open_selected_log())

    def _build_benchmarks(self) -> None:
        view = self._new_view("benchmarks")
        scroll_shell = tk.Frame(view, bg=COLORS["bg"])
        scroll_shell.pack(fill="both", expand=True)
        self.benchmark_canvas = tk.Canvas(scroll_shell, bg=COLORS["bg"], highlightthickness=0, bd=0)
        benchmark_scroll = ttk.Scrollbar(scroll_shell, orient="vertical", command=self.benchmark_canvas.yview)
        self.benchmark_canvas.configure(yscrollcommand=benchmark_scroll.set)
        self.benchmark_canvas.pack(side="left", fill="both", expand=True)
        benchmark_scroll.pack(side="right", fill="y")
        self.benchmark_scroll_frame = tk.Frame(self.benchmark_canvas, bg=COLORS["bg"])
        benchmark_window = self.benchmark_canvas.create_window((0, 0), window=self.benchmark_scroll_frame, anchor="nw")
        self.benchmark_scroll_frame.bind("<Configure>", lambda _event: self.benchmark_canvas.configure(scrollregion=self.benchmark_canvas.bbox("all")))
        self.benchmark_canvas.bind("<Configure>", lambda event: self.benchmark_canvas.itemconfigure(benchmark_window, width=event.width))
        self.bind_all("<MouseWheel>", self._scroll_benchmark, add="+")
        self.bind_all("<Button-4>", self._scroll_benchmark, add="+")
        self.bind_all("<Button-5>", self._scroll_benchmark, add="+")
        body = self.benchmark_scroll_frame
        heading = tk.Frame(body, bg=COLORS["bg"])
        heading.pack(fill="x", pady=(12, 20))
        tk.Label(heading, text="TENSOR BENCHMARK", bg=COLORS["bg"], fg="#84a49a", font=(MONO, 8)).pack(anchor="w")
        tk.Label(heading, text="GPU 计算测试", bg=COLORS["bg"], fg=COLORS["ink"], font=(FONT, 23, "bold")).pack(anchor="w", pady=(4, 0))
        tk.Label(heading, text="PyTorch CUDA 矩阵乘法实测 Tensor FP16 与 FP32 吞吐。", bg=COLORS["bg"], fg="#87938e", font=(FONT, 9)).pack(anchor="w", pady=(5, 0))
        notice = tk.Frame(body, bg="#f2fbf6", highlightbackground="#d9eee3", highlightthickness=1)
        notice.pack(fill="x", pady=(0, 15))
        tk.Label(notice, text="ⓘ  测试会在目标显卡上短时运行矩阵乘法；建议选择包含 PyTorch CUDA 的 Conda 环境。", bg="#f2fbf6", fg="#6f8d80", font=(FONT, 9), padx=13, pady=10).pack(anchor="w")
        self.benchmark_cards = tk.Frame(body, bg=COLORS["bg"])
        self.benchmark_cards.pack(fill="x")
        history = self._card(body)
        history.pack(fill="both", expand=True, pady=(15, 0))
        tk.Label(history, text="HISTORY", bg=COLORS["surface"], fg="#84a49a", font=(MONO, 8)).pack(anchor="w", padx=19, pady=(15, 0))
        tk.Label(history, text="最近测试结果", bg=COLORS["surface"], fg=COLORS["ink"], font=(FONT, 14, "bold")).pack(anchor="w", padx=19, pady=(4, 10))
        frame = tk.Frame(history, bg=COLORS["surface"])
        frame.pack(fill="both", expand=True, padx=12, pady=(0, 12))
        cols = ("time", "gpu", "tensor", "fp32", "env", "status")
        self.history_tree = ttk.Treeview(frame, columns=cols, show="headings", height=5)
        labels = {"time": "时间", "gpu": "GPU", "tensor": "Tensor FP16", "fp32": "FP32", "env": "环境", "status": "状态"}
        for col in cols:
            self.history_tree.heading(col, text=labels[col])
            self.history_tree.column(col, width=125, anchor="w")
        self.history_tree.pack(fill="both", expand=True)

    def _scroll_benchmark(self, event: Any) -> None:
        if self.current_view != "benchmarks":
            return
        if getattr(event, "num", None) == 4:
            delta = -3
        elif getattr(event, "num", None) == 5:
            delta = 3
        else:
            delta = -3 if getattr(event, "delta", 0) > 0 else 3
        self.benchmark_canvas.yview_scroll(delta, "units")

    def _build_logs(self) -> None:
        view = self._new_view("logs")
        heading = tk.Frame(view, bg=COLORS["bg"])
        heading.pack(fill="x", pady=(12, 20))
        tk.Label(heading, text="RUN LOGS", bg=COLORS["bg"], fg="#84a49a", font=(MONO, 8)).pack(anchor="w")
        tk.Label(heading, text="运行日志", bg=COLORS["bg"], fg=COLORS["ink"], font=(FONT, 23, "bold")).pack(anchor="w", pady=(4, 0))
        tk.Label(heading, text="日志直接读取自服务器 ~/.gpu-orchestrator/runs。", bg=COLORS["bg"], fg="#87938e", font=(FONT, 9)).pack(anchor="w", pady=(5, 0))
        body = tk.Frame(view, bg=COLORS["bg"])
        body.pack(fill="both", expand=True)
        body.columnconfigure(0, weight=0)
        body.columnconfigure(1, weight=1)
        self.log_list_card = self._card(body)
        self.log_list_card.grid(row=0, column=0, sticky="nsew", padx=(0, 7))
        self.log_view_card = self._card(body)
        self.log_view_card.grid(row=0, column=1, sticky="nsew", padx=(7, 0))
        tk.Label(self.log_list_card, text="EXPERIMENTS", bg=COLORS["surface"], fg="#84a49a", font=(MONO, 8)).pack(anchor="w", padx=15, pady=(15, 0))
        tk.Label(self.log_list_card, text="任务列表", bg=COLORS["surface"], fg=COLORS["ink"], font=(FONT, 14, "bold")).pack(anchor="w", padx=15, pady=(4, 10))
        self.log_list_body = tk.Frame(self.log_list_card, bg=COLORS["surface"], width=280)
        self.log_list_body.pack(fill="both", expand=True, padx=8, pady=(0, 10))
        log_head = tk.Frame(self.log_view_card, bg=COLORS["surface"])
        log_head.pack(fill="x", padx=17, pady=(15, 8))
        self.log_title_var = tk.StringVar(value="选择一个任务")
        tk.Label(log_head, text="TAIL OUTPUT", bg=COLORS["surface"], fg="#84a49a", font=(MONO, 8)).pack(anchor="w")
        tk.Label(log_head, textvariable=self.log_title_var, bg=COLORS["surface"], fg=COLORS["ink"], font=(FONT, 14, "bold")).pack(side="left", pady=(4, 0))
        tk.Button(log_head, text="详情", command=self.open_full_log, relief="flat", bd=0, bg=COLORS["mint"], fg=COLORS["green_dark"], font=(FONT, 9, "bold"), padx=10, pady=5).pack(side="right", padx=(7, 0), pady=(3, 0))
        tk.Button(log_head, text="↻ 刷新", command=self.load_selected_log, relief="flat", bd=0, bg=COLORS["surface"], fg=COLORS["green_dark"], font=(FONT, 9, "bold")).pack(side="right", pady=(5, 0))
        tk.Label(self.log_view_card, text="自动刷新最近 200 行（最多读取 24 KB）；点击“详情”加载完整日志。", bg=COLORS["surface"], fg="#9aa59f", font=(FONT, 8)).pack(anchor="w", padx=18, pady=(0, 6))
        self.log_text = tk.Text(self.log_view_card, bg="#122c27", fg="#a6c8b7", insertbackground="#a6c8b7", relief="flat", bd=0, wrap="word", font=(MONO, 9), padx=18, pady=14)
        self.log_text.pack(fill="both", expand=True, padx=10, pady=(0, 10))
        self.log_text.insert("1.0", "选择左侧实验查看最新日志。")
        self.log_text.configure(state="disabled")

    def _build_settings(self) -> None:
        view = self._new_view("settings")
        heading = tk.Frame(view, bg=COLORS["bg"])
        heading.pack(fill="x", pady=(12, 20))
        tk.Label(heading, text="GLOBAL SETTINGS", bg=COLORS["bg"], fg="#84a49a", font=(MONO, 8)).pack(anchor="w")
        tk.Label(heading, text="全局设置", bg=COLORS["bg"], fg=COLORS["ink"], font=(FONT, 23, "bold")).pack(anchor="w", pady=(4, 0))
        tk.Label(heading, text="调度采样基准适用于所有已连接服务器。", bg=COLORS["bg"], fg="#87938e", font=(FONT, 9)).pack(anchor="w", pady=(5, 0))
        card = self._card(view)
        card.pack(fill="x", anchor="n")
        tk.Label(card, text="GPU 调度采样", bg=COLORS["surface"], fg=COLORS["ink"], font=(FONT, 14, "bold")).pack(anchor="w", padx=20, pady=(18, 4))
        tk.Label(card, text="判断忙卡是否能接收共享任务时，显存按最近采样中的峰值占用估算，GPU 利用率按最近采样中的最高值判断。空闲卡仍按当前状态判断。", bg=COLORS["surface"], fg="#78867f", font=(FONT, 9), wraplength=780, justify="left").pack(anchor="w", padx=20, pady=(0, 15))
        row = tk.Frame(card, bg=COLORS["surface"])
        row.pack(fill="x", padx=20, pady=(0, 18))
        tk.Label(row, text="采样次数（1–120）", bg=COLORS["surface"], fg="#65746c", font=(FONT, 9, "bold")).pack(side="left")
        self.gpu_history_samples_var = tk.StringVar(value="5")
        self.gpu_history_samples_entry = self._spinbox(row, textvariable=self.gpu_history_samples_var, from_=1, to=120, width=7)
        self.gpu_history_samples_entry.pack(side="left", padx=(12, 0))
        tk.Label(row, text="次 / 每张 GPU", bg=COLORS["surface"], fg="#8b9892", font=(FONT, 9)).pack(side="left", padx=8)
        tk.Button(row, text="保存设置", command=self.save_global_settings, relief="flat", bd=0, bg=COLORS["green"], fg="#ffffff", font=(FONT, 9, "bold"), padx=13, pady=7).pack(side="right")

    def _show_view(self, view: str) -> None:
        self.current_view = view
        for name, frame in self.views.items():
            frame.pack_forget()
        self.views[view].pack(fill="both", expand=True)
        for name, button in self.nav_buttons.items():
            button.configure(bg=COLORS["navy_soft"] if name == view else COLORS["navy"], fg="#f7fff9" if name == view else "#8da9a0")
        labels = {
            "dashboard": ("LIVE RESOURCE MAP", "资源总览"),
            "queue": ("EXPERIMENT QUEUE", "实验队列"),
            "benchmarks": ("TENSOR BENCHMARK", "GPU 测试"),
            "logs": ("RUN LOGS", "运行日志"),
            "settings": ("GLOBAL SETTINGS", "全局设置"),
        }
        self.header_kicker.set(labels[view][0])
        self.header_title.set(labels[view][1])
        if view == "dashboard":
            self.render_dashboard()
        elif view == "queue":
            self.render_queue()
        elif view == "benchmarks":
            self.render_benchmarks()
        elif view == "logs":
            self.render_logs()
        elif view == "settings":
            self.render_settings()

    def render_settings(self) -> None:
        if not hasattr(self, "gpu_history_samples_var"):
            return
        settings = self.state.get("global_settings") or {}
        self.gpu_history_samples_var.set(str(max(1, min(120, safe_int(settings.get("gpu_history_samples"), 5)))))

    def save_global_settings(self) -> None:
        try:
            count = safe_int(self.gpu_history_samples_var.get(), 0)
            if count < 1 or count > 120:
                raise ValueError("采样次数需设置为 1 到 120")
            self.manager.set_gpu_history_samples(count)
            self.state = self.manager.public_state()
            self.render_settings()
            self.set_status(f"全局 GPU 采样基准已更新为最近 {count} 次")
        except Exception as exc:
            messagebox.showerror("设置未保存", str(exc), parent=self)

    def set_status(self, message: str, error: bool = False) -> None:
        self.status_var.set(message)
        self.status_bar.configure(fg=COLORS["red"] if error else "#6e8077")

    def manual_refresh(self) -> None:
        self.set_status("正在刷新…")
        self._tick_in_background()

    def refresh_state(self) -> None:
        try:
            self.state = self.manager.public_state()
            self.render_all()
        except Exception as exc:
            self.set_status(str(exc), True)
        if self.refresh_after_id is None and self.winfo_exists():
            self.refresh_after_id = self.after(3000, self._periodic_refresh)

    def _periodic_refresh(self) -> None:
        self.refresh_after_id = None
        self.refresh_state()

    def refresh_logs_if_needed(self) -> None:
        if self.current_view == "logs" and self.selected_log:
            self.load_selected_log()
        self.after(5000, self.refresh_logs_if_needed)

    def render_all(self) -> None:
        active_id = str(self.state.get("active_server_id") or "default")
        active_server = next((item for item in self.state.get("servers", []) if item.get("id") == active_id), {})
        active_enabled = bool(active_server.get("enabled", True))
        servers = list(self.state.get("servers") or [])
        monitored = [item for item in servers if item.get("enabled", True)]
        active = [item for item in monitored if item.get("connected")]
        self.monitor_summary.set(f"监控服务器：{len(monitored)} · 活跃：{len(active)}")
        if active:
            self.connection_text.set(f"{len(active)} 台已连接 · 自动轮询中")
        elif monitored:
            self.connection_text.set(f"{len(monitored)} 台待连接")
        else:
            self.connection_text.set("没有启用的监控服务器")
        self.connection_dot.delete("all")
        dot_color = COLORS["green"] if active else (COLORS["amber"] if monitored else COLORS["red"])
        self.connection_dot.create_oval(1, 1, 8, 8, fill=dot_color, outline="")
        self.connect_button.configure(text="服务器管理  →")
        poll_times = [str(item.get("last_poll_at") or "") for item in servers]
        poll_times.append(str(self.state.get("last_poll_at") or ""))
        last_poll = max((value for value in poll_times if value), default="")
        self.last_sync.set(f"同步于 {fmt_time(last_poll)}" if last_poll else "尚未同步")
        server_states = self.state.get("server_states") or {}
        disk_alerts = []
        manual_pauses = []
        for server in servers:
            server_id = str(server.get("id") or "default")
            state = server_states.get(server_id) or {}
            title = server.get("name") or server.get("host") or server_id
            guard = state.get("disk_guard") or {}
            if server.get("enabled", True) and guard.get("blocked"):
                disk_alerts.append(f"{title}：{guard.get('message') or '磁盘可用空间不足'}")
            elif server.get("enabled", True) and state.get("scheduler_paused"):
                manual_pauses.append(title)
        if disk_alerts:
            self.set_status("调度已暂停 · " + "；".join(disk_alerts), True)
        elif manual_pauses:
            self.set_status("调度已手动暂停 · " + "、".join(manual_pauses))
        elif not active_enabled and active_server:
            self.set_status("当前选择的服务器监控已停用")
        elif self.state.get("last_error"):
            self.set_status(self.state["last_error"], True)
        elif active:
            self.set_status("自动轮询中 · 调度器已就绪")
        if self.current_view == "dashboard":
            self.render_dashboard()
        elif self.current_view == "queue":
            self.render_queue()
        elif self.current_view == "benchmarks":
            self.render_benchmarks()
        elif self.current_view == "logs":
            self.render_logs()

    def render_dashboard(self) -> None:
        servers = self.state.get("servers", [])
        server_states = self.state.get("server_states", {})
        entries = []
        for server in servers:
            server_id = str(server.get("id"))
            state = server_states.get(server_id) or {}
            entries.append((server, state, state.get("snapshot") if server.get("enabled", True) else None))

        snapshots = [snapshot for _server, _state, snapshot in entries if snapshot]
        if snapshots:
            cpu = sum(float(snapshot.get("cpu_percent") or 0) for snapshot in snapshots) / len(snapshots)
            memory_total = sum(float((snapshot.get("memory") or {}).get("total_bytes") or 0) for snapshot in snapshots)
            memory_used = sum(float((snapshot.get("memory") or {}).get("used_bytes") or 0) for snapshot in snapshots)
            ram = memory_used / memory_total * 100 if memory_total else 0
            all_gpus = [gpu for snapshot in snapshots for gpu in snapshot.get("gpus", [])]
            gpu_total = sum(float(gpu.get("memory_total_mb") or 0) for gpu in all_gpus)
            gpu_used = sum(float(gpu.get("memory_used_mb") or 0) for gpu in all_gpus)
            gpu_percent = gpu_used / gpu_total * 100 if gpu_total else 0
            disk_total = sum(float((snapshot.get("disk") or {}).get("total_bytes") or 0) for snapshot in snapshots)
            disk_used = sum(float((snapshot.get("disk") or {}).get("used_bytes") or 0) for snapshot in snapshots)
            disk_free = sum(float((snapshot.get("disk") or {}).get("free_bytes") or 0) for snapshot in snapshots)
            disk_percent = disk_used / disk_total * 100 if disk_total else 0
            self.metric_vars["cpu"].set(f"{cpu:.1f}%")
            self.metric_vars["cpu_detail"].set(f"{len(snapshots)} 台服务器平均")
            self.metric_vars["ram"].set(f"{ram:.1f}%")
            self.metric_vars["ram_detail"].set(f"{fmt_bytes(memory_used)} / {fmt_bytes(memory_total)}")
            self.metric_vars["gpu"].set(f"{gpu_percent:.1f}%")
            self.metric_vars["gpu_detail"].set(f"{len(all_gpus)} 张显卡 · {fmt_mb(gpu_used)} / {fmt_mb(gpu_total)}")
            self.metric_vars["disk"].set(f"{disk_percent:.1f}%")
            self.metric_vars["disk_detail"].set(f"{fmt_bytes(disk_free)} 可用 · {len(snapshots)} 台")
            self.metric_bars["cpu"]["value"] = cpu
            self.metric_bars["ram"]["value"] = ram
            self.metric_bars["gpu"]["value"] = gpu_percent
            self.metric_bars["disk"]["value"] = disk_percent
        else:
            for key in ("cpu", "ram", "gpu", "disk"):
                self.metric_vars[key].set("—")
                self.metric_bars[key]["value"] = 0
            self.metric_vars["cpu_detail"].set("等待服务器同步")
            self.metric_vars["ram_detail"].set("— / —")
            self.metric_vars["gpu_detail"].set("— 张显卡")
            self.metric_vars["disk_detail"].set("— 可用")
        self.dashboard_empty = not bool(snapshots)
        self.render_server_overviews(entries)
        self.render_activity()
        self.render_storage_collection(entries)

    def render_server_overviews(self, entries: list[tuple[dict[str, Any], dict[str, Any], dict[str, Any] | None]]) -> None:
        signature = tuple((str(server.get("id")), bool(server.get("enabled", True)), tuple(safe_int(gpu.get("index"), index) for index, gpu in enumerate((snapshot or {}).get("gpus", [])))) for server, _state, snapshot in entries)
        if signature != self.dashboard_server_signature:
            self._clear(self.gpu_grid)
            self.dashboard_server_panels = {}
            self.dashboard_server_signature = signature
            if not entries:
                tk.Label(self.gpu_grid, text="还没有配置服务器，请在服务器管理中添加。", bg=COLORS["surface_soft"], fg="#99a7a1", font=(FONT, 10), padx=20, pady=28).pack(fill="x")
            for row, (server, _state, _snapshot) in enumerate(entries):
                server_id = str(server.get("id"))
                panel = self._card(self.gpu_grid)
                panel.grid(row=row, column=0, sticky="ew", pady=(0, 12))
                self.gpu_grid.columnconfigure(0, weight=1)
                header = tk.Frame(panel, bg=COLORS["surface"])
                header.pack(fill="x", padx=17, pady=(15, 0))
                name_label = tk.Label(header, text=server.get("name") or server.get("host") or server_id, bg=COLORS["surface"], fg=COLORS["ink"], font=(FONT, 12, "bold"), anchor="w")
                name_label.pack(side="left")
                endpoint_label = tk.Label(header, text=f"{server.get('host', '')}:{server.get('port', 22)} · {server.get('username', '')}", bg=COLORS["surface"], fg="#8a9892", font=(MONO, 8))
                endpoint_label.pack(side="left", padx=12)
                status_var = tk.StringVar(value="等待同步")
                status_label = tk.Label(header, textvariable=status_var, bg=COLORS["surface"], fg=COLORS["green_dark"], font=(FONT, 8))
                status_label.pack(side="right")
                resources = tk.Frame(panel, bg=COLORS["surface"])
                resources.pack(fill="x", padx=17, pady=(12, 0))
                resource_vars: dict[str, tk.StringVar] = {}
                resource_bars: dict[str, ttk.Progressbar] = {}
                resource_defs = (("cpu", "CPU", "Green.Horizontal.TProgressbar"), ("ram", "内存", "Blue.Horizontal.TProgressbar"), ("gpu", "GPU 显存", "Purple.Horizontal.TProgressbar"), ("disk", "主目录", "Amber.Horizontal.TProgressbar"))
                for index, (key, label, style) in enumerate(resource_defs):
                    card = tk.Frame(resources, bg=COLORS["surface_soft"])
                    card.grid(row=0, column=index, sticky="ew", padx=(0 if index == 0 else 5, 0))
                    resources.columnconfigure(index, weight=1)
                    value_var = tk.StringVar(value="—")
                    detail_var = tk.StringVar(value="等待同步")
                    resource_vars[key] = value_var
                    resource_vars[key + "_detail"] = detail_var
                    tk.Label(card, text=label, bg=COLORS["surface_soft"], fg="#84928b", font=(FONT, 8, "bold")).pack(anchor="w", padx=10, pady=(8, 0))
                    tk.Label(card, textvariable=value_var, bg=COLORS["surface_soft"], fg=COLORS["ink"], font=(FONT, 15, "bold")).pack(anchor="w", padx=10, pady=(3, 0))
                    tk.Label(card, textvariable=detail_var, bg=COLORS["surface_soft"], fg="#8a9892", font=(MONO, 7)).pack(anchor="w", padx=10, pady=(2, 4))
                    bar = ttk.Progressbar(card, style=style, maximum=100, value=0)
                    bar.pack(fill="x", padx=10, pady=(0, 8))
                    resource_bars[key] = bar
                gpu_header = tk.Frame(panel, bg=COLORS["surface"])
                gpu_header.pack(fill="x", padx=17, pady=(14, 8))
                tk.Label(gpu_header, text="GPU 列表", bg=COLORS["surface"], fg=COLORS["ink"], font=(FONT, 10, "bold")).pack(side="left")
                gpu_grid = tk.Frame(panel, bg=COLORS["surface"])
                gpu_grid.pack(fill="x", padx=17, pady=(0, 7))
                self.dashboard_server_panels[server_id] = {
                    "panel": panel,
                    "name": name_label,
                    "endpoint": endpoint_label,
                    "status": status_var,
                    "status_label": status_label,
                    "resource_vars": resource_vars,
                    "resource_bars": resource_bars,
                    "gpu_grid": gpu_grid,
                    "gpu_state": {"widgets": {}, "topology": None},
                }

        for server, state, snapshot in entries:
            server_id = str(server.get("id"))
            panel = self.dashboard_server_panels.get(server_id)
            if not panel:
                continue
            panel["name"].configure(text=server.get("name") or server.get("host") or server_id)
            panel["endpoint"].configure(text=f"{server.get('host', '')}:{server.get('port', 22)} · {server.get('username', '')}")
            vars_by_key = panel["resource_vars"]
            bars = panel["resource_bars"]
            if not server.get("enabled", True):
                panel["status"].set("已停用 · 不监控")
                panel["status_label"].configure(fg=COLORS["muted"])
                for key in ("cpu", "ram", "gpu", "disk"):
                    vars_by_key[key].set("—")
                    vars_by_key[key + "_detail"].set("监控已停用")
                    bars[key]["value"] = 0
                self.render_gpu_cards([], parent=panel["gpu_grid"], server_id=server_id, widget_state=panel["gpu_state"])
                continue
            if not snapshot:
                state_connected = bool(state.get("connected"))
                panel["status"].set("已连接 · 等待首轮同步" if state_connected else "未连接 · 等待自动连接")
                panel["status_label"].configure(fg=COLORS["green_dark"] if state_connected else COLORS["muted"])
                for key in ("cpu", "ram", "gpu", "disk"):
                    vars_by_key[key].set("—")
                    vars_by_key[key + "_detail"].set("等待同步")
                    bars[key]["value"] = 0
                self.render_gpu_cards([], parent=panel["gpu_grid"], server_id=server_id, widget_state=panel["gpu_state"])
                continue
            guard = state.get("disk_guard") or {}
            if guard.get("blocked"):
                panel["status"].set("调度暂停 · 磁盘空间不足")
                panel["status_label"].configure(fg=COLORS["red"])
            elif state.get("scheduler_paused"):
                panel["status"].set("调度暂停 · 手动")
                panel["status_label"].configure(fg=COLORS["amber"])
            else:
                panel["status"].set(f"{'已连接' if state.get('connected') else '未连接 · 最近数据'} · {fmt_time(state.get('last_poll_at'))}")
                panel["status_label"].configure(fg=COLORS["green_dark"] if state.get("connected") else COLORS["muted"])
            memory = snapshot.get("memory") or {}
            disk = snapshot.get("disk") or {}
            gpus = snapshot.get("gpus") or []
            cpu = float(snapshot.get("cpu_percent") or 0)
            ram = float(memory.get("used_percent") or 0)
            total_gpu = sum(float(gpu.get("memory_total_mb") or 0) for gpu in gpus)
            used_gpu = sum(float(gpu.get("memory_used_mb") or 0) for gpu in gpus)
            gpu_percent = used_gpu / total_gpu * 100 if total_gpu else 0
            disk_percent = float(disk.get("used_percent") or 0)
            vars_by_key["cpu"].set(f"{cpu:.1f}%")
            vars_by_key["cpu_detail"].set(f"负载 {(snapshot.get('load_average') or [0])[0]}")
            vars_by_key["ram"].set(f"{ram:.1f}%")
            vars_by_key["ram_detail"].set(f"{fmt_bytes(memory.get('used_bytes'))} / {fmt_bytes(memory.get('total_bytes'))}")
            vars_by_key["gpu"].set(f"{gpu_percent:.1f}%")
            vars_by_key["gpu_detail"].set(f"{len(gpus)} 张显卡")
            vars_by_key["disk"].set(f"{disk_percent:.1f}%")
            vars_by_key["disk_detail"].set(f"{fmt_bytes(disk.get('free_bytes'))} 可用")
            bars["cpu"]["value"] = cpu
            bars["ram"]["value"] = ram
            bars["gpu"]["value"] = gpu_percent
            bars["disk"]["value"] = disk_percent
            self.render_gpu_cards(gpus, parent=panel["gpu_grid"], server_id=server_id, widget_state=panel["gpu_state"])

    def _clear(self, frame: tk.Misc) -> None:
        for child in frame.winfo_children():
            child.destroy()

    def _meter(self, parent: tk.Misc, label: str, style: str, number_width: int = 7) -> tuple[ttk.Progressbar, tk.Label]:
        row = tk.Frame(parent, bg=COLORS["surface"])
        row.pack(fill="x", pady=4)
        tk.Label(row, text=label, bg=COLORS["surface"], fg="#85928c", font=(FONT, 8), width=6, anchor="w").pack(side="left")
        bar = ttk.Progressbar(row, style=style, maximum=100, value=0)
        bar.pack(side="left", fill="x", expand=True, padx=8)
        number_label = tk.Label(row, text="0.0%", bg=COLORS["surface"], fg="#52625b", font=(MONO, 8), width=number_width, anchor="e")
        number_label.pack(side="right")
        return bar, number_label

    def _bind_gpu_detail(self, widget: tk.Misc, server_id: str, gpu_index: int) -> None:
        if widget.winfo_class() == "Button":
            return
        widget.bind("<Button-1>", lambda _event, selected_server=server_id, selected_gpu=gpu_index: self.show_gpu_detail(selected_server, selected_gpu))
        for child in widget.winfo_children():
            self._bind_gpu_detail(child, server_id, gpu_index)

    def render_gpu_cards(self, gpus: list[dict[str, Any]], parent: tk.Misc | None = None, server_id: str | None = None, widget_state: dict[str, Any] | None = None) -> None:
        target_parent = parent or self.gpu_grid
        selected_server = server_id or str(self.state.get("active_server_id") or "default")
        legacy = widget_state is None
        state = widget_state or {"widgets": self.gpu_widgets, "topology": self.gpu_topology}
        gpu_ids = tuple(safe_int(gpu.get("index"), index) for index, gpu in enumerate(gpus))
        topology = (selected_server, *gpu_ids)
        if topology != state.get("topology"):
            self._clear(target_parent)
            state["widgets"] = {}
            state["topology"] = topology
            if legacy:
                self.gpu_widgets = state["widgets"]
                self.gpu_topology = topology
            if not gpus:
                tk.Label(target_parent, text="未检测到 NVIDIA GPU 或 nvidia-smi 不可用。", bg=COLORS["surface_soft"], fg="#99a7a1", font=(FONT, 10), padx=20, pady=28).pack(fill="x")
                return
            columns = min(3, max(1, len(gpus)))
            for index, gpu in enumerate(gpus):
                gpu_index = safe_int(gpu.get("index"), index)
                card = self._card(target_parent)
                card.grid(row=index // columns, column=index % columns, sticky="nsew", padx=(0 if index % columns == 0 else 6, 0), pady=(0, 8))
                target_parent.columnconfigure(index % columns, weight=1)
                top = tk.Frame(card, bg=COLORS["surface"])
                top.pack(fill="x", padx=17, pady=(16, 0))
                tk.Label(top, text=f"GPU {gpu_index}", bg=COLORS["mint"], fg=COLORS["green_dark"], font=(MONO, 9, "bold"), padx=8, pady=5).pack(side="left")
                temp_label = tk.Label(top, bg=COLORS["surface"], font=(MONO, 8))
                temp_label.pack(side="right")
                name_label = tk.Label(card, bg=COLORS["surface"], fg=COLORS["ink"], font=(FONT, 10, "bold"), anchor="w")
                name_label.pack(fill="x", padx=17, pady=(10, 0))
                uuid_label = tk.Label(card, bg=COLORS["surface"], fg="#99a49f", font=(MONO, 7), anchor="w")
                uuid_label.pack(fill="x", padx=17, pady=(3, 8))
                memory_bar, memory_label = self._meter(card, "显存", "Purple.Horizontal.TProgressbar")
                utilization_bar, utilization_label = self._meter(card, "利用率", "Green.Horizontal.TProgressbar")
                foot = tk.Frame(card, bg=COLORS["surface"])
                foot.pack(fill="x", padx=17, pady=(10, 14))
                state_label = tk.Label(foot, bg=COLORS["surface"], font=(FONT, 8))
                state_label.pack(side="left")
                speed_label = tk.Label(foot, bg=COLORS["surface"], fg="#53615b", font=(MONO, 8))
                speed_label.pack(side="left", padx=8)
                tk.Button(foot, text="测试 ϟ", command=lambda selected_server=selected_server, selected=gpu_index: self.run_benchmark(selected_server, selected), bg=COLORS["surface"], fg=COLORS["green_dark"], relief="flat", bd=0, font=(FONT, 8, "bold")).pack(side="right")
                state["widgets"][gpu_index] = {
                    "card": card,
                    "temp": temp_label,
                    "name": name_label,
                    "uuid": uuid_label,
                    "memory_bar": memory_bar,
                    "memory_label": memory_label,
                    "utilization_bar": utilization_bar,
                    "utilization_label": utilization_label,
                    "state": state_label,
                    "speed": speed_label,
                }
                self._bind_gpu_detail(card, selected_server, gpu_index)

        for gpu in gpus:
            gpu_index = safe_int(gpu.get("index"), 0)
            widget = state["widgets"].get(gpu_index)
            if not widget:
                continue
            temp = gpu.get("temperature_c", "—")
            try:
                hot = float(temp) > 80
            except (TypeError, ValueError):
                hot = False
            widget["temp"].configure(text=f"{temp}°C", fg=COLORS["red"] if hot else "#8c9893")
            widget["name"].configure(text=gpu.get("name", "Unknown GPU"))
            widget["uuid"].configure(text=str(gpu.get("uuid", ""))[:26])
            memory_percent = float(gpu.get("memory_used_mb") or 0) / max(float(gpu.get("memory_total_mb") or 1), 1) * 100
            widget["memory_bar"]["value"] = memory_percent
            widget["memory_label"].configure(text=f"{memory_percent:.1f}%")
            utilization = float(gpu.get("utilization_gpu") or 0)
            widget["utilization_bar"]["value"] = utilization
            widget["utilization_label"].configure(text=f"{utilization:.1f}%")
            idle = bool(gpu.get("scheduler_idle")) and not gpu.get("reserved_mb")
            state_text = "空闲可调度" if idle else ("队列已占用" if gpu.get("reserved_mb") else f"{gpu.get('process_count', 0)} 个进程")
            widget["state"].configure(text=f"● {state_text}", fg=COLORS["green_dark"] if idle else COLORS["amber"])
            bench = gpu.get("benchmark") or {}
            speed = f"{float(bench.get('tensor_tflops')):.2f} TFLOPS" if bench.get("tensor_tflops") else "未测试"
            widget["speed"].configure(text=speed)

    def show_gpu_detail(self, server_id: str, gpu_index: int) -> None:
        dialog = tk.Toplevel(self)
        dialog.title(f"GPU {gpu_index} 详情")
        dialog.configure(bg=COLORS["bg"])
        dialog.transient(self)
        dialog.geometry("820x540")
        dialog.minsize(700, 420)
        server = next((item for item in self.state.get("servers", []) if item.get("id") == server_id), {})
        title_var = tk.StringVar(value=f"GPU {gpu_index}")
        subtitle_var = tk.StringVar(value=server.get("name") or server.get("host") or server_id)
        memory_var = tk.StringVar(value="显存：—")
        usage_var = tk.StringVar(value="利用率：—")
        temperature_var = tk.StringVar(value="温度：—")
        status_var = tk.StringVar(value="状态：—")
        tk.Label(dialog, text="GPU DETAIL", bg=COLORS["bg"], fg="#84a49a", font=(MONO, 8)).pack(anchor="w", padx=22, pady=(20, 0))
        header = tk.Frame(dialog, bg=COLORS["bg"])
        header.pack(fill="x", padx=22, pady=(4, 13))
        tk.Label(header, textvariable=title_var, bg=COLORS["bg"], fg=COLORS["ink"], font=(FONT, 20, "bold")).pack(side="left")
        tk.Label(header, textvariable=subtitle_var, bg=COLORS["bg"], fg="#82918a", font=(FONT, 9)).pack(side="left", padx=14, pady=(6, 0))
        summary = self._card(dialog)
        summary.pack(fill="x", padx=22, pady=(0, 13))
        for variable in (memory_var, usage_var, temperature_var, status_var):
            tk.Label(summary, textvariable=variable, bg=COLORS["surface"], fg="#52625b", font=(FONT, 9), padx=14, pady=12).pack(side="left", fill="x", expand=True)
        process_card = self._card(dialog)
        process_card.pack(fill="both", expand=True, padx=22, pady=(0, 20))
        tk.Label(process_card, text="运行中的计算进程", bg=COLORS["surface"], fg=COLORS["ink"], font=(FONT, 13, "bold")).pack(anchor="w", padx=16, pady=(14, 10))
        process_frame = tk.Frame(process_card, bg=COLORS["surface"])
        process_frame.pack(fill="both", expand=True, padx=10, pady=(0, 10))
        columns = ("name", "pid", "memory", "runtime")
        process_tree = ttk.Treeview(process_frame, columns=columns, show="headings")
        headings = {"name": "程序", "pid": "PID", "memory": "显存占用", "runtime": "已运行时间"}
        widths = {"name": 390, "pid": 90, "memory": 130, "runtime": 150}
        for column in columns:
            process_tree.heading(column, text=headings[column])
            process_tree.column(column, width=widths[column], anchor="w")
        process_tree.pack(side="left", fill="both", expand=True)
        process_scroll = ttk.Scrollbar(process_frame, orient="vertical", command=process_tree.yview)
        process_scroll.pack(side="right", fill="y")
        process_tree.configure(yscrollcommand=process_scroll.set)

        def update() -> None:
            if not dialog.winfo_exists():
                return
            state = self.state.get("server_states", {}).get(server_id)
            if not state and server_id == self.state.get("active_server_id"):
                state = self.state
            gpu = next((item for item in ((state or {}).get("snapshot") or {}).get("gpus", []) if safe_int(item.get("index"), -1) == gpu_index), None)
            if not gpu:
                title_var.set(f"GPU {gpu_index} · 暂无数据")
                memory_var.set("显存：—")
                usage_var.set("利用率：—")
                temperature_var.set("温度：—")
                status_var.set("状态：未连接")
            else:
                total = safe_int(gpu.get("memory_total_mb"), 0)
                used = safe_int(gpu.get("memory_used_mb"), 0)
                free = safe_int(gpu.get("memory_free_mb"), 0)
                percent = used / max(total, 1) * 100
                title_var.set(f"GPU {gpu_index} · {gpu.get('name', 'Unknown GPU')}")
                memory_var.set(f"显存：{percent:.1f}% · {free:,} MB 可用")
                usage_var.set(f"利用率：{float(gpu.get('utilization_gpu') or 0):.1f}%")
                temperature_var.set(f"温度：{gpu.get('temperature_c', '—')}°C")
                idle = bool(gpu.get("scheduler_idle")) and not gpu.get("reserved_mb")
                status_var.set("状态：空闲可调度" if idle else f"状态：{gpu.get('process_count', 0)} 个进程")
                for row in process_tree.get_children():
                    process_tree.delete(row)
                processes = gpu.get("processes") or []
                if not processes:
                    process_tree.insert("", "end", values=("暂无可见计算进程", "—", "—", "—"))
                else:
                    for process in processes:
                        process_tree.insert("", "end", values=(process.get("name") or "未知程序", process.get("pid") or "—", f"{safe_int(process.get('memory_mb'), 0):,} MB", fmt_duration(process.get("runtime_seconds"))))
            dialog.after(2000, update)

        update()

    def render_activity(self) -> None:
        items = sorted(self.state.get("experiments", []), key=lambda item: safe_int(item.get("created_seq"), 0), reverse=True)[:5]
        signature = tuple((item.get("id"), item.get("task_no"), item.get("server_id"), item.get("status"), (item.get("assigned_gpu") or {}).get("index"), item.get("priority"), item.get("failure_reason")) for item in items)
        if signature == self.activity_signature:
            return
        self.activity_signature = signature
        self._clear(self.activity_body)
        if not items:
            tk.Label(self.activity_body, text="还没有实验任务", bg=COLORS["surface"], fg="#99a7a1", font=(FONT, 10), pady=20).pack()
            return
        server_names = {str(server.get("id")): server.get("name") or server.get("host") or server.get("id") for server in self.state.get("servers", [])}
        for item in items:
            row = tk.Frame(self.activity_body, bg=COLORS["surface"])
            row.pack(fill="x", pady=4)
            color = COLORS["green"] if item.get("status") == "running" else COLORS["red"] if item.get("status") == "failed" else "#b5c0bb"
            tk.Label(row, text="●", bg=COLORS["surface"], fg=color, font=(FONT, 9)).pack(side="left", padx=(0, 8))
            server_label = server_names.get(str(item.get("server_id") or "default"), item.get("server_id") or "默认")
            detail = f"{server_label} · GPU {item['assigned_gpu']['index']} · {item.get('conda_env') or '默认 Python'}" if item.get("assigned_gpu") else f"{server_label} · {item.get('script_name', '')} · 优先级 {item.get('priority')}"
            text_frame = tk.Frame(row, bg=COLORS["surface"])
            text_frame.pack(side="left", fill="x", expand=True)
            tk.Label(text_frame, text=f"{task_identifier(item)}  {item.get('name', '')}", bg=COLORS["surface"], fg="#3c4c45", font=(FONT, 9, "bold"), anchor="w").pack(fill="x")
            tk.Label(text_frame, text=detail, bg=COLORS["surface"], fg="#9aa59f", font=(FONT, 8), anchor="w").pack(fill="x", pady=(2, 0))
            tk.Label(row, text=STATUS_LABELS.get(item.get("status"), item.get("status", "")), bg=COLORS["surface"], fg="#819089", font=(FONT, 8)).pack(side="right")

    def render_storage_collection(self, entries: list[tuple[dict[str, Any], dict[str, Any], dict[str, Any] | None]]) -> None:
        signature = tuple((str(server.get("id")), bool(server.get("enabled", True)), (snapshot or {}).get("disk", {}).get("used_bytes"), (snapshot or {}).get("disk", {}).get("free_bytes"), (snapshot or {}).get("disk", {}).get("used_percent")) for server, _state, snapshot in entries)
        if signature == self.storage_signature:
            return
        self.storage_signature = signature
        self._clear(self.storage_body)
        self.storage_path_var.set(f"{len(entries)} 台服务器")
        if not entries:
            tk.Label(self.storage_body, text="暂无服务器", bg=COLORS["surface"], fg="#99a7a1", font=(FONT, 10), pady=20).pack()
            return
        for server, _state, snapshot in entries:
            row = tk.Frame(self.storage_body, bg=COLORS["surface"])
            row.pack(fill="x", pady=6)
            title = server.get("name") or server.get("host") or server.get("id")
            tk.Label(row, text=title, bg=COLORS["surface"], fg=COLORS["ink"], font=(FONT, 9, "bold"), anchor="w").pack(fill="x")
            if not server.get("enabled", True):
                tk.Label(row, text="监控已停用", bg=COLORS["surface"], fg=COLORS["muted"], font=(FONT, 8), anchor="w").pack(fill="x", pady=(2, 0))
                continue
            disk = (snapshot or {}).get("disk") or {}
            used_percent = float(disk.get("used_percent") or 0)
            detail = f"{used_percent:.1f}% 已使用 · {fmt_bytes(disk.get('free_bytes'))} 可用"
            tk.Label(row, text=detail, bg=COLORS["surface"], fg="#85938c", font=(MONO, 8), anchor="w").pack(fill="x", pady=(2, 3))
            bar = ttk.Progressbar(row, style="Amber.Horizontal.TProgressbar", maximum=100, value=used_percent)
            bar.pack(fill="x")
        

    def render_storage(self, disk: dict[str, Any]) -> None:
        signature = (disk.get("path"), disk.get("total_bytes"), disk.get("used_bytes"), disk.get("free_bytes"), disk.get("used_percent"))
        if signature == self.storage_signature:
            return
        self.storage_signature = signature
        self._clear(self.storage_body)
        self.storage_path_var.set(disk.get("path", "~"))
        total = float(disk.get("total_bytes") or 0)
        used = float(disk.get("used_bytes") or 0)
        free = float(disk.get("free_bytes") or 0)
        used_percent = float(disk.get("used_percent") or 0)
        body = tk.Frame(self.storage_body, bg=COLORS["surface"])
        body.pack(fill="both", expand=True, pady=(2, 0))
        left = tk.Frame(body, bg=COLORS["surface"])
        left.pack(side="left", padx=(8, 22), pady=12)
        canvas = tk.Canvas(left, width=105, height=105, bg=COLORS["surface"], highlightthickness=0)
        canvas.pack()
        canvas.create_oval(8, 8, 97, 97, outline="#e8efeb", width=12)
        if used_percent:
            canvas.create_arc(8, 8, 97, 97, start=90, extent=-min(359.9, used_percent * 3.6), outline=COLORS["amber"], width=12, style="arc")
        canvas.create_text(52, 48, text=f"{free / 1024 ** 3:.1f}" if free else "—", fill=COLORS["ink"], font=(FONT, 17, "bold"))
        canvas.create_text(52, 68, text="GB 可用", fill="#8f9d96", font=(FONT, 8))
        legend = tk.Frame(body, bg=COLORS["surface"])
        legend.pack(side="left", fill="x", expand=True, pady=17)
        for color, label, value in ((COLORS["amber"], "已使用", fmt_bytes(used)), ("#b8dbba", "可用空间", fmt_bytes(free)), ("#dbe6df", "总容量", fmt_bytes(total))):
            row = tk.Frame(legend, bg=COLORS["surface"])
            row.pack(fill="x", pady=5)
            tk.Label(row, text="●", bg=COLORS["surface"], fg=color, font=(FONT, 8)).pack(side="left")
            tk.Label(row, text=label, bg=COLORS["surface"], fg="#8b9892", font=(FONT, 8)).pack(side="left", padx=5)
            tk.Label(row, text=value, bg=COLORS["surface"], fg="#4e5e56", font=(MONO, 8)).pack(side="right")

    def render_queue(self) -> None:
        items = self.state.get("experiments", [])
        server_names = {str(item.get("id")): item.get("name") or item.get("host") or item.get("id") for item in self.state.get("servers", [])}
        counts = {}
        for item in items:
            counts[item.get("status")] = counts.get(item.get("status"), 0) + 1
        self.queue_summary_vars["queued"].set(str(counts.get("queued", 0) + counts.get("waiting_memory", 0)))
        self.queue_summary_vars["running"].set(str(counts.get("running", 0)))
        self.queue_summary_vars["paused"].set(str(counts.get("paused", 0)))
        self.queue_summary_vars["success"].set(str(counts.get("success", 0)))
        self.queue_summary_vars["failed"].set(str(counts.get("failed", 0) + counts.get("canceled", 0)))
        notices = []
        for server in self.state.get("servers", []):
            server_id = str(server.get("id") or "default")
            state = (self.state.get("server_states") or {}).get(server_id) or {}
            title = server.get("name") or server.get("host") or server_id
            guard = state.get("disk_guard") or {}
            if server.get("enabled", True) and guard.get("blocked"):
                notices.append(f"{title}：{guard.get('message') or '磁盘可用空间不足'}")
            elif server.get("enabled", True) and state.get("scheduler_paused"):
                notices.append(f"{title}：已手动暂停调度")
        if notices:
            self.scheduler_notice_var.set("调度暂停 · " + "；".join(notices))
            self.scheduler_notice.configure(bg="#fff5e8", fg="#a56a18")
        else:
            self.scheduler_notice_var.set("调度正常 · 等待任务会按服务器分别调度")
            self.scheduler_notice.configure(bg="#eff9f3", fg=COLORS["green_dark"])
        if not hasattr(self, "queue_tree"):
            return
        visible = []
        for item in items:
            status = item.get("status")
            if self.queue_filter == "all" or (self.queue_filter == "queued" and status in ("queued", "waiting_memory")) or (self.queue_filter == "failed" and status in ("failed", "canceled")) or status == self.queue_filter:
                visible.append(item)
        visible.sort(key=lambda item: (safe_int(item.get("priority"), 50), safe_int(item.get("created_seq"), 0)))
        signature = (self.queue_filter, tuple((item.get("id"), item.get("task_no"), item.get("server_id"), item.get("status"), item.get("priority"), item.get("execution_level"), item.get("max_gpu_utilization"), (item.get("assigned_gpu") or {}).get("index"), item.get("peak_memory_mb"), item.get("failure_reason"), item.get("pause_reason"), item.get("dependency_reason"), tuple(item.get("depends_on") or [])) for item in visible))
        if signature == self.queue_signature:
            return
        self.queue_signature = signature
        selected = set(self.queue_tree.selection())
        wanted = {str(item.get("id")) for item in visible}
        for row in self.queue_tree.get_children():
            if row not in wanted:
                self.queue_tree.delete(row)
        for item in visible:
            item_id = str(item.get("id"))
            server_name = server_names.get(str(item.get("server_id") or "default"), item.get("server_id") or "默认")
            gpu = f"GPU {item['assigned_gpu'].get('index')} · {item['assigned_gpu'].get('name', '')}" if item.get("assigned_gpu") else "待分配"
            memory = f"{safe_int(item.get('peak_memory_mb')):,} MB" if safe_int(item.get("peak_memory_mb")) else "自动"
            if item.get("dependency_reason"):
                strategy = "等待前序任务"
            elif item.get("depends_on"):
                strategy = "前序完成 · " + "、".join(str(value)[:8] for value in item.get("depends_on")[:2])
            elif item.get("execution_level") == "low_interference":
                strategy = f"低干扰 · 利用率低于 {safe_int(item.get('max_gpu_utilization'), 30)}%"
            elif item.get("execution_level") == "emergency":
                strategy = "紧急 · 有显存即跑"
            else:
                strategy = "默认 · GPU 空闲"
            values = (f"{task_identifier(item)}  {item.get('name')}  ·  {item.get('script_name')}", server_name, STATUS_LABELS.get(item.get("status"), item.get("status")), f"P{item.get('priority')}", strategy, gpu, memory)
            if item_id in self.queue_tree.get_children():
                self.queue_tree.item(item_id, values=values, tags=(item.get("status"),))
            else:
                self.queue_tree.insert("", "end", iid=item_id, values=values, tags=(item.get("status"),))
        for position, item in enumerate(visible):
            self.queue_tree.move(str(item.get("id")), "", position)
        for item_id in selected & wanted:
            self.queue_tree.selection_add(item_id)

    def set_queue_filter(self, value: str) -> None:
        self.queue_filter = value
        for key, button in self.filter_buttons.items():
            button.configure(bg=COLORS["mint"] if key == value else COLORS["surface"], fg=COLORS["green_dark"] if key == value else COLORS["muted"], font=(FONT, 9, "bold" if key == value else "normal"))
        self.render_queue()

    def _run_queue_action(self, action: Any, success_message: str) -> None:
        self.set_status("正在更新任务状态…")

        def worker() -> None:
            try:
                action()
            except Exception as exc:
                self.after(0, lambda error=str(exc): self.set_status(error, True))
            else:
                def finish() -> None:
                    self.refresh_state()
                    self.set_status(success_message)
                self.after(0, finish)

        threading.Thread(target=worker, daemon=True).start()

    def toggle_selected_pause(self) -> None:
        item = self._selected_item()
        if not item:
            self.set_status("请先选择一个实验任务", True)
            return
        status = item.get("status")
        if status in ("queued", "waiting_memory", "running"):
            paused = True
            message = "实验已暂停"
        elif status == "paused":
            paused = False
            message = "实验已恢复"
        else:
            self.set_status("只有等待中、运行中或暂停中的任务可以切换", True)
            return
        exp_id = str(item.get("id"))
        self._run_queue_action(lambda: self.manager.set_experiment_paused(exp_id, paused), message)

    def pause_all_tasks(self) -> None:
        self._run_queue_action(self.manager.pause_all_experiments, "所有未完成任务已暂停")

    def resume_all_tasks(self) -> None:
        self._run_queue_action(self.manager.resume_all_experiments, "暂停任务已重新进入等待队列")

    def render_benchmarks(self) -> None:
        servers = list(self.state.get("servers") or [])
        server_states = self.state.get("server_states") or {}
        active_server_id = str(self.state.get("active_server_id") or "default")
        groups = []
        for server in servers:
            server_id = str(server.get("id") or "default")
            state = server_states.get(server_id) or {}
            if server_id == active_server_id and not state:
                state = self.state
            enabled = bool(server.get("enabled", True))
            snapshot = state.get("snapshot") or {}
            gpus = list(snapshot.get("gpus") or []) if enabled else []
            envs = list((state.get("conda") or {}).get("envs") or [])
            default_env = (state.get("preferences") or {}).get("conda_env") or ""
            groups.append((server_id, server, state, gpus, envs, default_env))

        topology = tuple(
            (
                server_id,
                server.get("name") or server.get("host") or server_id,
                server.get("host") or "",
                bool(server.get("enabled", True)),
                bool(server.get("connected")),
                tuple((safe_int(gpu.get("index"), index), gpu.get("name") or "") for index, gpu in enumerate(gpus)),
                tuple(envs),
                default_env,
            )
            for server_id, server, _state, gpus, envs, default_env in groups
        )
        if topology != self.benchmark_signature:
            self.benchmark_signature = topology
            self._clear(self.benchmark_cards)
            self.benchmark_widgets = {}
            if not groups:
                tk.Label(self.benchmark_cards, text="请先在服务器管理中添加服务器。", bg=COLORS["surface_soft"], fg="#99a7a1", font=(FONT, 10), padx=20, pady=28).pack(fill="x")
            for server_id, server, _state, gpus, envs, default_env in groups:
                section = tk.Frame(self.benchmark_cards, bg=COLORS["bg"])
                section.pack(fill="x", pady=(0, 12))
                section_header = tk.Frame(section, bg=COLORS["surface"], highlightbackground=COLORS["line"], highlightthickness=1)
                section_header.pack(fill="x", pady=(0, 7))
                server_title = server.get("name") or server.get("host") or server_id
                tk.Label(section_header, text=f"服务器 · {server_title}", bg=COLORS["surface"], fg=COLORS["ink"], font=(FONT, 11, "bold"), anchor="w").pack(side="left", padx=14, pady=10)
                if not server.get("enabled", True):
                    server_status = "监控已停用"
                elif server.get("connected"):
                    server_status = f"已连接 · {len(gpus)} 张显卡"
                else:
                    server_status = "未连接" if not gpus else f"未连接 · 显示上次资源快照（{len(gpus)} 张显卡）"
                tk.Label(section_header, text=server_status, bg=COLORS["surface"], fg="#82918a", font=(MONO, 8)).pack(side="right", padx=14, pady=10)
                grid = tk.Frame(section, bg=COLORS["bg"])
                grid.pack(fill="x")
                if not gpus:
                    message = "监控已停用，启用后才能执行测试。" if not server.get("enabled", True) else "尚未连接或暂无 NVIDIA GPU。"
                    tk.Label(grid, text=message, bg=COLORS["surface_soft"], fg="#99a7a1", font=(FONT, 10), padx=20, pady=22).pack(fill="x")
                    continue
                columns = min(3, max(1, len(gpus)))
                for index, gpu in enumerate(gpus):
                    gpu_index = safe_int(gpu.get("index"), index)
                    card = self._card(grid)
                    card.grid(row=index // columns, column=index % columns, sticky="nsew", padx=(0 if index % columns == 0 else 6, 0), pady=(0, 8))
                    grid.columnconfigure(index % columns, weight=1)
                    top = tk.Frame(card, bg=COLORS["surface"])
                    top.pack(fill="x", padx=17, pady=(16, 0))
                    detail_command = lambda _event, selected_server=server_id, selected_gpu=gpu_index: self.show_gpu_detail(selected_server, selected_gpu)
                    top.bind("<Button-1>", detail_command)
                    title_label = tk.Label(top, text=f"GPU {gpu_index} · {gpu.get('name')}", bg=COLORS["surface"], fg=COLORS["ink"], font=(FONT, 10, "bold"), anchor="w")
                    title_label.pack(side="left")
                    title_label.bind("<Button-1>", detail_command)
                    temp_label = tk.Label(top, text="—°C", bg=COLORS["surface"], fg="#8c9893", font=(MONO, 8))
                    temp_label.pack(side="right")
                    memory_label = tk.Label(card, text="显存 —", bg=COLORS["surface"], fg="#7e8d86", font=(MONO, 8), anchor="w")
                    memory_label.pack(fill="x", padx=17, pady=(8, 0))
                    score = tk.Frame(card, bg=COLORS["surface"])
                    score.pack(fill="x", padx=17, pady=(17, 10))
                    score_labels: dict[str, tk.Label] = {}
                    for column, label in enumerate(("Tensor FP16", "FP32")):
                        box = tk.Frame(score, bg=COLORS["surface_soft"])
                        box.grid(row=0, column=column, sticky="ew", padx=(0 if column == 0 else 5, 0))
                        score.columnconfigure(column, weight=1)
                        tk.Label(box, text=label, bg=COLORS["surface_soft"], fg="#899790", font=(FONT, 8)).pack(anchor="w", padx=10, pady=(9, 0))
                        value_label = tk.Label(box, text="— TFLOPS", bg=COLORS["surface_soft"], fg=COLORS["ink"], font=(MONO, 13, "bold"))
                        value_label.pack(anchor="w", padx=10, pady=(5, 9))
                        score_labels[label] = value_label
                    actions = tk.Frame(card, bg=COLORS["surface"])
                    actions.pack(fill="x", padx=17, pady=(4, 15))
                    env_value = default_env if default_env in envs else "默认 Python"
                    env_var = tk.StringVar(value=env_value)
                    combo = ttk.Combobox(actions, textvariable=env_var, state="readonly", values=["默认 Python"] + envs, width=16)
                    combo.pack(side="left")
                    button = tk.Button(actions, text="运行测试 ϟ", command=lambda selected_server=server_id, selected_gpu=gpu_index, var=env_var: self.run_benchmark(selected_server, selected_gpu, "" if var.get() == "默认 Python" else var.get()), relief="flat", bd=0, bg="#f0fbf6", fg=COLORS["green_dark"], font=(FONT, 9, "bold"), padx=8, pady=5)
                    button.pack(side="right")
                    self.benchmark_widgets[(server_id, gpu_index)] = {
                        "temp": temp_label,
                        "memory": memory_label,
                        "tensor": score_labels["Tensor FP16"],
                        "fp32": score_labels["FP32"],
                        "button": button,
                    }

        for server_id, _server, _state, gpus, _envs, _default_env in groups:
            for gpu in gpus:
                gpu_index = safe_int(gpu.get("index"), 0)
                widget = self.benchmark_widgets.get((server_id, gpu_index))
                if not widget:
                    continue
                temp = gpu.get("temperature_c", "—")
                try:
                    hot = float(temp) > 80
                except (TypeError, ValueError):
                    hot = False
                widget["temp"].configure(text=f"{temp}°C", fg=COLORS["red"] if hot else "#8c9893")
                total_memory = safe_int(gpu.get("memory_total_mb"), 0)
                used_memory = safe_int(gpu.get("memory_used_mb"), 0)
                memory_percent = used_memory / max(total_memory, 1) * 100
                widget["memory"].configure(text=f"显存 {memory_percent:.1f}%")
                bench = gpu.get("benchmark") or {}
                widget["tensor"].configure(text=f"{float(bench.get('tensor_tflops')):.2f} TFLOPS" if bench.get("tensor_tflops") else "— TFLOPS")
                widget["fp32"].configure(text=f"{float(bench.get('fp32_tflops')):.2f} TFLOPS" if bench.get("fp32_tflops") else "— TFLOPS")
                widget["button"].configure(text="测试中…" if (server_id, gpu_index) in self.benchmark_running else "运行测试 ϟ")
        self.render_history()

    def render_history(self) -> None:
        if not hasattr(self, "history_tree"):
            return
        signature = tuple((item.get("created_at"), item.get("gpu_index"), item.get("tensor_tflops"), item.get("fp32_tflops"), item.get("ok"), item.get("error")) for item in self.state.get("benchmark_history", []))
        if signature == self.history_signature:
            return
        self.history_signature = signature
        for row in self.history_tree.get_children():
            self.history_tree.delete(row)
        server_names = {str(item.get("id")): item.get("name") or item.get("host") or item.get("id") for item in self.state.get("servers", [])}
        for item in list(reversed(self.state.get("benchmark_history", [])))[:12]:
            server_name = server_names.get(str(item.get("server_id") or "default"), item.get("server_id") or "默认")
            self.history_tree.insert("", "end", values=(fmt_time(item.get("created_at")), f"{server_name} · GPU {item.get('gpu_index')} · {item.get('gpu_name', '—')}", f"{float(item.get('tensor_tflops')):.2f} TFLOPS" if item.get("tensor_tflops") else "—", f"{float(item.get('fp32_tflops')):.2f} TFLOPS" if item.get("fp32_tflops") else "—", item.get("env") or "默认 Python", "成功" if item.get("ok") else item.get("error", "失败")))

    def render_logs(self) -> None:
        if not hasattr(self, "log_list_body"):
            return
        items = sorted(self.state.get("experiments", []), key=lambda item: safe_int(item.get("created_seq"), 0), reverse=True)
        if not items:
            if self.log_signature == (None, ()):
                return
            self.log_signature = (None, ())
            self._clear(self.log_list_body)
            tk.Label(self.log_list_body, text="暂无实验", bg=COLORS["surface"], fg="#99a7a1", font=(FONT, 10), pady=20).pack()
            return
        if not self.selected_log or not any(item.get("id") == self.selected_log for item in items):
            self.selected_log = items[0].get("id")
        signature = (self.selected_log, tuple((item.get("id"), item.get("task_no"), item.get("status"), item.get("created_seq"), item.get("name"), item.get("script_name")) for item in items))
        if signature == self.log_signature:
            return
        self.log_signature = signature
        self._clear(self.log_list_body)
        for item in items:
            selected = item.get("id") == self.selected_log
            button = tk.Button(self.log_list_body, text=f"{task_identifier(item)}  {item.get('name')}\\n{item.get('script_name')} · {fmt_time(item.get('created_at'))}\\n{STATUS_LABELS.get(item.get('status'), item.get('status'))}", command=lambda exp_id=item.get("id"): self.select_log(exp_id), justify="left", anchor="w", relief="flat", bd=0, bg="#f1f8f4" if selected else COLORS["surface"], fg="#425149", font=(FONT, 9), padx=9, pady=8)
            button.pack(fill="x", pady=2)
        self.load_selected_log()

    def select_log(self, exp_id: str) -> None:
        self.selected_log = exp_id
        self.render_logs()

    def open_selected_log(self) -> None:
        selection = self.queue_tree.selection()
        if not selection:
            self.set_status("请先选择一个实验任务", True)
            return
        self.selected_log = selection[0]
        self._show_view("logs")
        self.render_logs()

    def load_selected_log(self) -> None:
        if not self.selected_log or self.log_loading:
            return
        item = next((exp for exp in self.state.get("experiments", []) if exp.get("id") == self.selected_log), None)
        if not item or not self.manager.runtime_for(str(item.get("server_id") or "default")).connected:
            return
        self.log_loading = True
        selected_id = self.selected_log

        def worker() -> None:
            try:
                log = self.manager.read_log(item)
                self.after(0, lambda: self._set_log_text(selected_id, log))
            except Exception as exc:
                self.after(0, lambda error=str(exc): self._set_log_text(selected_id, error))
            finally:
                self.after(0, lambda: setattr(self, "log_loading", False))

        threading.Thread(target=worker, daemon=True).start()

    def _set_log_text(self, exp_id: str, content: str) -> None:
        if exp_id != self.selected_log:
            return
        self.log_title_var.set(next((item.get("name", "") for item in self.state.get("experiments", []) if item.get("id") == exp_id), ""))
        self.log_text.configure(state="normal")
        self.log_text.delete("1.0", "end")
        self.log_text.insert("1.0", content or "暂无输出")
        self.log_text.configure(state="disabled")
        self.log_text.see("end")

    def open_full_log(self) -> None:
        if not self.selected_log:
            self.set_status("请先选择一个实验任务", True)
            return
        item = next((exp for exp in self.state.get("experiments", []) if exp.get("id") == self.selected_log), None)
        if not item:
            self.set_status("实验任务不存在", True)
            return
        target_runtime = self.manager.runtime_for(str(item.get("server_id") or "default"))
        if not target_runtime.connected:
            messagebox.showinfo("完整日志", "请先连接该实验所在的服务器。", parent=self)
            return
        dialog = tk.Toplevel(self)
        dialog.title(f"完整日志 · {item.get('name') or task_identifier(item)}")
        dialog.configure(bg=COLORS["bg"])
        dialog.transient(self)
        dialog.geometry("1000x720")
        dialog.minsize(700, 450)
        header = tk.Frame(dialog, bg=COLORS["bg"])
        header.pack(fill="x", padx=18, pady=(16, 8))
        tk.Label(header, text=f"完整日志 · {task_identifier(item)} {item.get('name') or ''}", bg=COLORS["bg"], fg=COLORS["ink"], font=(FONT, 13, "bold")).pack(side="left")
        status_var = tk.StringVar(value="正在读取服务器上的完整日志…")
        tk.Label(dialog, textvariable=status_var, bg=COLORS["bg"], fg=COLORS["muted"], font=(FONT, 8), anchor="w").pack(fill="x", padx=19, pady=(0, 7))
        body = tk.Frame(dialog, bg="#122c27")
        body.pack(fill="both", expand=True, padx=18, pady=(0, 16))
        text_widget = tk.Text(body, bg="#122c27", fg="#a6c8b7", insertbackground="#a6c8b7", relief="flat", bd=0, wrap="none", font=(MONO, 9), padx=14, pady=12)
        scrollbar = ttk.Scrollbar(body, orient="vertical", command=text_widget.yview)
        horizontal = ttk.Scrollbar(body, orient="horizontal", command=text_widget.xview)
        text_widget.configure(yscrollcommand=scrollbar.set, xscrollcommand=horizontal.set, state="disabled")
        text_widget.grid(row=0, column=0, sticky="nsew")
        scrollbar.grid(row=0, column=1, sticky="ns")
        horizontal.grid(row=1, column=0, sticky="ew")
        body.rowconfigure(0, weight=1)
        body.columnconfigure(0, weight=1)

        def worker() -> None:
            try:
                content = self.manager.read_full_log(item)
                error = ""
            except Exception as exc:
                content = ""
                error = str(exc)

            def show_result() -> None:
                if not dialog.winfo_exists():
                    return
                text_widget.configure(state="normal")
                text_widget.delete("1.0", "end")
                text_widget.insert("1.0", content or (error or "暂无输出"))
                text_widget.configure(state="disabled")
                text_widget.see("end")
                status_var.set(error or f"已加载完整日志 · {len(content):,} 个字符")

            self.after(0, show_result)

        threading.Thread(target=worker, daemon=True).start()

    def _tick_in_background(self) -> None:
        if not any(rt.connected for rt in self.manager.runtimes.values()):
            self.refresh_state()
            return

        def worker() -> None:
            self.manager.tick()
            self.after(0, self.refresh_state)

        threading.Thread(target=worker, daemon=True).start()

    def open_server_manager(self) -> None:
        dialog = tk.Toplevel(self)
        dialog.title("服务器管理")
        dialog.configure(bg=COLORS["bg"])
        dialog.transient(self)
        dialog.grab_set()
        dialog.geometry("760x560")
        dialog.minsize(700, 500)

        tk.Label(dialog, text="SERVER MANAGEMENT", bg=COLORS["bg"], fg="#84a49a", font=(MONO, 8)).pack(anchor="w", padx=22, pady=(20, 0))
        tk.Label(dialog, text="服务器管理", bg=COLORS["bg"], fg=COLORS["ink"], font=(FONT, 20, "bold")).pack(anchor="w", padx=22, pady=(4, 0))
        tk.Label(dialog, text="每条记录对应一个独立 SSH 连接；相同 IP 但不同端口、账号或连接用途可以分别保存。", bg=COLORS["bg"], fg="#87958e", font=(FONT, 9)).pack(anchor="w", padx=22, pady=(4, 14))
        table_frame = tk.Frame(dialog, bg=COLORS["surface"], highlightbackground=COLORS["line"], highlightthickness=1)
        table_frame.pack(fill="both", expand=True, padx=22, pady=(0, 13))
        columns = ("name", "host", "status", "disk", "monitor", "auto")
        tree = ttk.Treeview(table_frame, columns=columns, show="headings", selectmode="browse", height=6)
        headings = {"name": "名称", "host": "连接地址", "status": "连接状态", "disk": "磁盘告警", "monitor": "监控", "auto": "启动时自动连接"}
        widths = {"name": 125, "host": 170, "status": 90, "disk": 85, "monitor": 60, "auto": 115}
        for column in columns:
            tree.heading(column, text=headings[column])
            tree.column(column, width=widths[column], anchor="w")
        tree.pack(fill="both", expand=True, side="left", padx=7, pady=7)
        scroll = ttk.Scrollbar(table_frame, orient="vertical", command=tree.yview)
        scroll.pack(side="right", fill="y", pady=7)
        tree.configure(yscrollcommand=scroll.set)

        def refresh() -> None:
            self.state = self.manager.public_state()
            for row in tree.get_children():
                tree.delete(row)
            for item in self.state.get("servers", []):
                status = "已连接" if item.get("connected") else ("连接失败" if item.get("last_error") else "离线")
                monitor = "启用" if item.get("enabled", True) else "已停用"
                threshold = safe_float(item.get("disk_alert_gb"), 5)
                tree.insert("", "end", iid=str(item.get("id")), values=(item.get("name") or item.get("host"), f"{item.get('host')}:{item.get('port', 22)} · {item.get('username', '')}", status, f"{threshold:g} GB" if threshold else "关闭", monitor, "是" if item.get("auto_connect") else "否"))
            active_id = str(self.state.get("active_server_id") or "default")
            if tree.exists(active_id):
                tree.selection_set(active_id)
                tree.focus(active_id)

        def selected_id() -> str | None:
            selection = tree.selection()
            return selection[0] if selection else None

        def select_server(_event: Any = None) -> None:
            server_id = selected_id()
            if server_id:
                self.manager.set_active(server_id)
                self.refresh_state()

        tree.bind("<<TreeviewSelect>>", select_server)
        tree.bind("<Double-1>", lambda _event: self.open_connection_dialog(selected_id(), parent=dialog, on_saved=refresh) if selected_id() else None)
        tk.Label(dialog, text="先单击选中一台服务器，再使用下方按钮；新增连接不会覆盖已有服务器记录。", bg=COLORS["bg"], fg="#7d8d85", font=(FONT, 9)).pack(anchor="w", padx=22, pady=(0, 8))
        buttons = tk.Frame(dialog, bg=COLORS["bg"])
        buttons.pack(fill="x", padx=22, pady=(0, 18))
        tk.Button(buttons, text="新建服务器", command=lambda: self.open_connection_dialog(new_server=True, parent=dialog, on_saved=refresh), relief="flat", bd=0, bg=COLORS["green"], fg="#ffffff", font=(FONT, 9, "bold"), padx=12, pady=7).pack(side="left")
        tk.Button(buttons, text="编辑", command=lambda: (self.open_connection_dialog(selected_id(), parent=dialog, on_saved=refresh) if selected_id() else self.set_status("请先选择服务器", True)), relief="flat", bd=0, bg=COLORS["surface"], fg=COLORS["green_dark"], font=(FONT, 9), padx=12, pady=7).pack(side="left", padx=7)
        tk.Button(buttons, text="磁盘告警", command=lambda: (self.open_disk_alert_dialog(selected_id(), parent=dialog, on_saved=refresh) if selected_id() else self.set_status("请先选择服务器", True)), relief="flat", bd=0, bg=COLORS["surface"], fg=COLORS["green_dark"], font=(FONT, 9), padx=12, pady=7).pack(side="left")

        def toggle_monitoring() -> None:
            server_id = selected_id()
            if not server_id:
                self.set_status("请先选择服务器", True)
                return
            item = next((item for item in self.state.get("servers", []) if item.get("id") == server_id), None)
            if item is None:
                return
            enabled = not bool(item.get("enabled", True))
            self.manager.set_enabled(server_id, enabled)
            self.refresh_state()
            refresh()
            self.set_status(f"已{'启用' if enabled else '停用'} {item.get('name') or server_id} 的资源监控")

        tk.Button(buttons, text="启用/停用监控", command=toggle_monitoring, relief="flat", bd=0, bg=COLORS["surface"], fg=COLORS["amber"], font=(FONT, 9), padx=12, pady=7).pack(side="left")

        def remove() -> None:
            server_id = selected_id()
            if not server_id:
                self.set_status("请先选择服务器", True)
                return
            if not messagebox.askyesno("删除服务器", "只删除本机保存的连接配置，不会删除服务器上的文件。继续吗？", parent=dialog):
                return
            try:
                self.manager.remove_server(server_id)
                self.refresh_state()
                refresh()
            except Exception as exc:
                messagebox.showerror("无法删除", str(exc), parent=dialog)

        tk.Button(buttons, text="删除", command=remove, relief="flat", bd=0, bg="#fff1ef", fg=COLORS["red"], font=(FONT, 9), padx=12, pady=7).pack(side="left")
        tk.Button(buttons, text="关闭", command=dialog.destroy, relief="flat", bd=0, bg=COLORS["surface"], fg=COLORS["muted"], font=(FONT, 9), padx=12, pady=7).pack(side="right")
        refresh()

    def open_disk_alert_dialog(self, server_id: str, parent: tk.Misc | None = None, on_saved: Any = None) -> None:
        record = next((item for item in self.manager.store.data.get("servers", []) if item.get("id") == server_id), None)
        if record is None:
            self.set_status("服务器不存在", True)
            return
        dialog = tk.Toplevel(parent or self)
        dialog.title("磁盘告警容量")
        dialog.configure(bg=COLORS["surface"])
        dialog.transient(parent or self)
        dialog.grab_set()
        dialog.resizable(False, False)
        name = record.get("name") or record.get("profile", {}).get("host") or server_id
        tk.Label(dialog, text="SERVER DISK ALERT", bg=COLORS["surface"], fg="#84a49a", font=(MONO, 8)).grid(row=0, column=0, columnspan=2, sticky="w", padx=24, pady=(20, 0))
        tk.Label(dialog, text=f"{name} · 磁盘告警", bg=COLORS["surface"], fg=COLORS["ink"], font=(FONT, 18, "bold")).grid(row=1, column=0, columnspan=2, sticky="w", padx=24, pady=(5, 4))
        tk.Label(dialog, text="主目录可用空间低于此值时报警并暂停该服务器的任务调度。设为 0 可关闭磁盘告警。", bg=COLORS["surface"], fg="#7d8d85", font=(FONT, 9), wraplength=410, justify="left").grid(row=2, column=0, columnspan=2, sticky="w", padx=24, pady=(0, 16))
        tk.Label(dialog, text="告警容量（GB）", bg=COLORS["surface"], fg="#65746c", font=(FONT, 9, "bold")).grid(row=3, column=0, sticky="w", padx=24, pady=(0, 8))
        value = tk.StringVar(value=f"{safe_float(record.get('disk_alert_gb'), 5):g}")
        entry = self._spinbox(dialog, textvariable=value, from_=0, to=1000000, increment=0.5, width=18)
        entry.grid(row=3, column=1, sticky="ew", padx=(10, 24), pady=(0, 8))
        buttons = tk.Frame(dialog, bg=COLORS["surface"])
        buttons.grid(row=4, column=0, columnspan=2, sticky="e", padx=24, pady=(10, 18))
        tk.Button(buttons, text="取消", command=dialog.destroy, relief="flat", bd=0, bg=COLORS["surface"], fg=COLORS["muted"], font=(FONT, 9), padx=10, pady=7).pack(side="left", padx=5)

        def save() -> None:
            raw = value.get().strip()
            try:
                threshold = float(raw)
                if threshold < 0 or threshold > 1_000_000:
                    raise ValueError
                self.manager.set_disk_alert_gb(server_id, threshold)
                self.refresh_state()
                if on_saved:
                    on_saved()
                dialog.destroy()
                self.set_status(f"{name} 磁盘告警容量已设为 {threshold:g} GB" if threshold else f"{name} 磁盘告警已关闭")
            except ValueError:
                messagebox.showwarning("容量无效", "请输入 0 到 1,000,000 之间的 GB 数值。", parent=dialog)
            except Exception as exc:
                messagebox.showerror("设置未保存", str(exc), parent=dialog)

        tk.Button(buttons, text="保存设置  →", command=save, relief="flat", bd=0, bg=COLORS["green"], fg="#ffffff", font=(FONT, 9, "bold"), padx=13, pady=7).pack(side="left", padx=5)

    def open_connection_dialog(self, server_id: str | None = None, parent: tk.Misc | None = None, on_saved: Any = None, new_server: bool = False) -> None:
        target_parent = parent or self
        if not new_server and not server_id:
            server_id = self.manager.active_server_id
        record = next((item for item in self.manager.store.data.get("servers", []) if item.get("id") == server_id), None)
        profile = dict((record or {}).get("profile") or {})
        if not profile and server_id == self.manager.active_server_id:
            profile = dict(self.state.get("profile") or {})
        dialog = tk.Toplevel(target_parent)
        dialog.title("新增服务器" if new_server else "连接设置")
        dialog.configure(bg=COLORS["surface"])
        dialog.transient(target_parent)
        dialog.grab_set()
        dialog.resizable(False, False)
        name_var = tk.StringVar(value=(record or {}).get("name") or profile.get("host", ""))
        tk.Label(dialog, text="SSH CONNECTION", bg=COLORS["surface"], fg="#84a49a", font=(MONO, 8)).grid(row=0, column=0, columnspan=2, sticky="w", padx=25, pady=(22, 0))
        tk.Label(dialog, text="新增服务器" if new_server else "连接 Linux 服务器", bg=COLORS["surface"], fg=COLORS["ink"], font=(FONT, 19, "bold")).grid(row=1, column=0, columnspan=2, sticky="w", padx=25, pady=(5, 4))
        tk.Label(dialog, text="连接后会在你的主目录创建 ~/.gpu-orchestrator，不需要 root 权限。", bg=COLORS["surface"], fg="#7d8d85", font=(FONT, 9)).grid(row=2, column=0, columnspan=2, sticky="w", padx=25, pady=(0, 18))
        entries: dict[str, tk.Entry] = {}
        fields = (("name", "显示名称", name_var.get()), ("host", "服务器 IP / 域名", profile.get("host", "")), ("port", "SSH 端口", str(profile.get("port", 22))), ("username", "用户名", profile.get("username", "")), ("password", "密码（留空使用已保存密码）", ""))
        for row, (key, label, value) in enumerate(fields, 3):
            tk.Label(dialog, text=label, bg=COLORS["surface"], fg="#65746c", font=(FONT, 9, "bold")).grid(row=row, column=0, sticky="w", padx=25, pady=(0, 5))
            entry = self._entry(dialog, width=35, show="*" if key == "password" else "")
            entry.insert(0, value)
            entry.grid(row=row, column=1, padx=(10, 25), pady=(0, 10))
            entries[key] = entry
        tk.Label(dialog, text="轮询间隔（秒）", bg=COLORS["surface"], fg="#65746c", font=(FONT, 9, "bold")).grid(row=8, column=0, sticky="w", padx=25, pady=(0, 5))
        interval = self._spinbox(dialog, from_=2, to=60, width=33)
        interval.delete(0, "end")
        interval.insert(0, str(profile.get("poll_interval", 5)))
        interval.grid(row=8, column=1, padx=(10, 25), pady=(0, 10))
        save_var = tk.BooleanVar(value=True)
        auto_var = tk.BooleanVar(value=bool((record or {}).get("auto_connect", True)))
        self._checkbutton(dialog, text="保存密码到本机系统密钥环（下次自动连接需要）", variable=save_var).grid(row=9, column=0, columnspan=2, sticky="w", padx=25, pady=(0, 5))
        self._checkbutton(dialog, text="应用启动时自动连接此服务器", variable=auto_var).grid(row=10, column=0, columnspan=2, sticky="w", padx=25, pady=(0, 12))
        tk.Label(dialog, text="首次连接会自动接受 SSH 主机指纹；正式环境建议改为 known_hosts 校验。", bg="#fff9ed", fg="#9a7c43", font=(FONT, 8), padx=10, pady=8).grid(row=11, column=0, columnspan=2, sticky="ew", padx=25)
        buttons = tk.Frame(dialog, bg=COLORS["surface"])
        buttons.grid(row=12, column=0, columnspan=2, sticky="e", padx=25, pady=20)
        tk.Button(buttons, text="取消", command=dialog.destroy, relief="flat", bd=0, bg=COLORS["surface"], fg=COLORS["muted"], font=(FONT, 9), padx=10, pady=7).pack(side="left", padx=5)
        if server_id and self.manager.runtime_for(server_id).connected:
            tk.Button(buttons, text="断开连接", command=lambda: self.disconnect(dialog, server_id), relief="flat", bd=0, bg="#fff1ef", fg=COLORS["red"], font=(FONT, 9), padx=10, pady=7).pack(side="left", padx=5)
        connect_button = tk.Button(buttons, text="开始连接  →", relief="flat", bd=0, bg=COLORS["green"], fg="#ffffff", font=(FONT, 9, "bold"), padx=13, pady=7)
        connect_button.pack(side="left", padx=5)

        def connect() -> None:
            profile_data = {
                "host": entries["host"].get().strip(),
                "port": safe_int(entries["port"].get(), 22),
                "username": entries["username"].get().strip(),
                "home": profile.get("home", ""),
                "poll_interval": max(2, min(60, safe_int(interval.get(), 5))),
            }
            password = entries["password"].get()
            connect_button.configure(state="disabled", text="连接中…")
            self.set_status(f"正在连接 {profile_data['host']}…")

            def worker() -> None:
                try:
                    selected_id = self.manager.upsert_server(server_id, entries["name"].get().strip(), profile_data, auto_var.get())
                    self.manager.connect(selected_id, profile_data, password, save_var.get())
                    def succeeded() -> None:
                        if dialog.winfo_exists():
                            dialog.destroy()
                        self.refresh_state()
                        if on_saved:
                            on_saved()
                        self.set_status("服务器连接成功")
                    self.after(0, succeeded)
                except Exception as exc:
                    def failed() -> None:
                        if dialog.winfo_exists():
                            connect_button.configure(state="normal", text="开始连接  →")
                            messagebox.showerror("连接失败", str(exc), parent=dialog)
                        self.set_status(str(exc), True)
                    self.after(0, failed)

            threading.Thread(target=worker, daemon=True).start()

        connect_button.configure(command=connect)

    def disconnect(self, dialog: tk.Toplevel, server_id: str | None = None) -> None:
        self.manager.disconnect(server_id)
        dialog.destroy()
        self.refresh_state()
        self.set_status("已断开服务器连接")

    def open_experiment_dialog(self, experiment: dict[str, Any] | None = None) -> None:
        editing = experiment is not None
        if editing:
            with self.manager.store.lock:
                stored = next((item for item in self.manager.store.data.get("experiments", []) if item.get("id") == experiment.get("id")), None)
                experiment = dict(stored) if stored else None
            if not experiment:
                self.set_status("实验不存在", True)
                return
            if experiment.get("status") == "running" or experiment.get("paused_process"):
                self.set_status("执行中的任务请先暂停或停止后再编辑", True)
                return
        dialog = tk.Toplevel(self)
        dialog.title("编辑实验任务" if editing else "提交一个实验")
        dialog.configure(bg=COLORS["surface"])
        dialog.transient(self)
        dialog.grab_set()
        dialog.resizable(False, False)
        prefs = self.state.get("preferences") or {}
        profile = self.state.get("profile") or {}
        tk.Label(dialog, text="EDIT EXPERIMENT" if editing else "NEW EXPERIMENT", bg=COLORS["surface"], fg="#84a49a", font=(MONO, 8)).grid(row=0, column=0, columnspan=2, sticky="w", padx=25, pady=(22, 0))
        tk.Label(dialog, text="编辑实验任务" if editing else "提交一个实验", bg=COLORS["surface"], fg=COLORS["ink"], font=(FONT, 19, "bold")).grid(row=1, column=0, columnspan=2, sticky="w", padx=25, pady=(5, 4))
        tk.Label(dialog, text="修改配置后重新进入等待队列；运行中的任务需要先暂停或停止。" if editing else "选择脚本、工作目录和运行环境；相同配置会成为下次默认值。", bg=COLORS["surface"], fg="#7d8d85", font=(FONT, 9)).grid(row=2, column=0, columnspan=2, sticky="w", padx=25, pady=(0, 18))
        initial_script = str((experiment or {}).get("local_script") or "")
        path_var = tk.StringVar(value=initial_script)
        name_var = tk.StringVar(value=str((experiment or {}).get("name") or ""))
        workdir_var = tk.StringVar(value=str((experiment or {}).get("workdir") if editing else (prefs.get("workdir") or profile.get("home") or "")))
        initial_server_id = str((experiment or {}).get("server_id") or self.state.get("active_server_id") or "default")
        initial_state = self.state.get("server_states", {}).get(initial_server_id) or (self.state if initial_server_id == self.state.get("active_server_id") else {})
        envs = list((initial_state.get("conda") or {}).get("envs") or [])
        initial_env = str((experiment or {}).get("conda_env") if editing else (prefs.get("conda_env") or ""))
        env_var = tk.StringVar(value=initial_env or "默认 Python")
        if initial_env and initial_env not in envs:
            envs.insert(0, initial_env)
        priority_var = tk.StringVar(value=str((experiment or {}).get("priority") if editing else 50))
        level_code = str((experiment or {}).get("execution_level") if editing else "idle_only")
        level_var = tk.StringVar(value=EXECUTION_LEVEL_LABELS.get(level_code, EXECUTION_LEVEL_LABELS["idle_only"]))
        memory_var = tk.StringVar(value=str((experiment or {}).get("peak_memory_mb") if editing else (prefs.get("peak_memory_mb") or "")))
        max_util_var = tk.StringVar(value=str((experiment or {}).get("max_gpu_utilization", 30) if editing else prefs.get("max_gpu_utilization", 30)))
        auto_var = tk.BooleanVar(value=bool((experiment or {}).get("auto_retry_oom", True)))
        initial_dependencies = set(self._normalize_dependency_ids((experiment or {}).get("depends_on", [])))
        server_options = [f"{item.get('name') or item.get('host') or item.get('id')} · {item.get('host') or '未配置'}" for item in self.state.get("servers", [])]
        server_ids = [str(item.get("id")) for item in self.state.get("servers", [])]
        active_server_id = str(self.state.get("active_server_id") or "default")
        selected_server_id = initial_server_id if editing else active_server_id
        server_var = tk.StringVar(value=server_options[server_ids.index(selected_server_id)] if selected_server_id in server_ids else (server_options[0] if server_options else ""))

        def row_label(row: int, text: str) -> None:
            tk.Label(dialog, text=text, bg=COLORS["surface"], fg="#65746c", font=(FONT, 9, "bold")).grid(row=row, column=0, sticky="w", padx=25, pady=(0, 5))

        row_label(3, "实验名称")
        self._entry(dialog, textvariable=name_var, width=35).grid(row=3, column=1, padx=(10, 25), pady=(0, 10))
        row_label(4, "目标服务器")
        server_combo = ttk.Combobox(dialog, textvariable=server_var, state="disabled" if editing else "readonly", values=server_options, width=32)
        server_combo.grid(row=4, column=1, padx=(10, 25), pady=(0, 10))
        row_label(5, "选择 .sh 文件")
        file_frame = tk.Frame(dialog, bg=COLORS["surface"])
        file_frame.grid(row=5, column=1, sticky="ew", padx=(10, 25), pady=(0, 10))
        tk.Button(file_frame, text="选择文件…", command=lambda: self.choose_script(path_var, path_label), relief="flat", bd=0, bg=COLORS["mint"], fg=COLORS["green_dark"], font=(FONT, 9, "bold"), padx=8, pady=5).pack(side="left")
        initial_script_label = Path(initial_script).name if initial_script else "尚未选择"
        path_label = tk.Label(file_frame, text=initial_script_label, bg=COLORS["surface"], fg="#8d9993", font=(FONT, 8), width=25, anchor="w")
        path_label.pack(side="left", padx=8)
        row_label(6, "服务器工作目录")
        self._entry(dialog, textvariable=workdir_var, width=35).grid(row=6, column=1, padx=(10, 25), pady=(0, 10))
        row_label(7, "Conda 环境")
        env_combo = ttk.Combobox(dialog, textvariable=env_var, state="readonly", values=["默认 Python"] + envs, width=32)
        env_combo.grid(row=7, column=1, padx=(10, 25), pady=(0, 10))

        row_label(8, "前序任务（可多选）")
        dependency_frame = tk.Frame(dialog, bg=COLORS["surface"])
        dependency_frame.grid(row=8, column=1, sticky="ew", padx=(10, 25), pady=(0, 3))
        dependency_list = tk.Listbox(
            dependency_frame,
            height=4,
            width=35,
            selectmode=tk.MULTIPLE,
            exportselection=False,
            bg=COLORS["surface_soft"],
            fg=COLORS["ink"],
            selectbackground=COLORS["mint"],
            selectforeground=COLORS["green_dark"],
            relief="flat",
            bd=0,
            highlightthickness=1,
            highlightbackground=COLORS["line"],
            highlightcolor=COLORS["green"],
            font=(FONT, 9),
        )
        dependency_list.pack(side="left", fill="both", expand=True)
        dependency_scroll = ttk.Scrollbar(dependency_frame, orient="vertical", command=dependency_list.yview)
        dependency_scroll.pack(side="right", fill="y")
        dependency_list.configure(yscrollcommand=dependency_scroll.set)
        dependency_ids_by_index: list[str] = []
        dependency_selection = set(initial_dependencies)
        server_names = {
            str(item.get("id")): str(item.get("name") or item.get("host") or item.get("id"))
            for item in self.state.get("servers", [])
        }

        def refresh_dependency_options() -> None:
            nonlocal dependency_ids_by_index
            dependency_ids_by_index = []
            dependency_list.delete(0, tk.END)
            candidates = sorted(
                (
                    item
                    for item in self.state.get("experiments", [])
                    if str(item.get("id")) != str((experiment or {}).get("id"))
                ),
                key=lambda item: safe_int(item.get("created_seq"), 0),
            )
            for item in candidates:
                item_id = str(item.get("id"))
                dependency_ids_by_index.append(item_id)
                server_name = server_names.get(str(item.get("server_id") or "default"), "默认服务器")
                status = STATUS_LABELS.get(item.get("status"), item.get("status") or "等待调度")
                dependency_list.insert(tk.END, f"{task_identifier(item)} · {item.get('name') or item_id} · {server_name} · {status}")
            for index, item_id in enumerate(dependency_ids_by_index):
                if item_id in dependency_selection:
                    dependency_list.selection_set(index)
            if candidates:
                dependency_hint.configure(text="默认无前序；可按 Ctrl/Shift 多选已有任务。")
            else:
                dependency_hint.configure(text="暂无可选任务，默认无前序任务。")

        dependency_hint = tk.Label(dialog, text="", bg=COLORS["surface"], fg="#9aa7a0", font=(FONT, 8))
        dependency_hint.grid(row=9, column=1, sticky="w", padx=(10, 25), pady=(0, 8))
        def remember_dependencies(_event: Any = None) -> None:
            dependency_selection.clear()
            dependency_selection.update(
                dependency_ids_by_index[index]
                for index in dependency_list.curselection()
                if index < len(dependency_ids_by_index)
            )

        dependency_list.bind("<<ListboxSelect>>", remember_dependencies)
        refresh_dependency_options()

        def apply_server_defaults(_event: Any = None) -> None:
            selected_id = server_ids[server_options.index(server_var.get())] if server_var.get() in server_options else active_server_id
            selected_state = self.state.get("server_states", {}).get(selected_id, {})
            selected_profile = selected_state.get("profile") or {}
            selected_prefs = selected_state.get("preferences") or {}
            selected_envs = (selected_state.get("conda") or {}).get("envs") or []
            current_env = env_var.get() if editing and selected_id == initial_server_id else ""
            env_values = list(selected_envs)
            if current_env and current_env != "默认 Python" and current_env not in env_values:
                env_values.insert(0, current_env)
            env_combo.configure(values=["默认 Python"] + env_values)
            if not editing or selected_id != initial_server_id:
                workdir_var.set(selected_prefs.get("workdir") or selected_profile.get("home") or "")
                selected_env = selected_prefs.get("conda_env") or "默认 Python"
                env_var.set(selected_env if selected_env in env_values else "默认 Python")
                memory_var.set(str(selected_prefs.get("peak_memory_mb") or ""))
                selected_level = selected_prefs.get("execution_level") or "idle_only"
                level_var.set(EXECUTION_LEVEL_LABELS.get(selected_level, EXECUTION_LEVEL_LABELS["idle_only"]))
                max_util_var.set(str(selected_prefs.get("max_gpu_utilization", 30)))

        server_combo.bind("<<ComboboxSelected>>", apply_server_defaults)
        apply_server_defaults()
        if env_var.get() not in envs and env_var.get() != "默认 Python":
            env_var.set("默认 Python")
        row_label(10, "优先级（越小越先）")
        self._spinbox(dialog, textvariable=priority_var, from_=1, to=999, width=33).grid(row=10, column=1, padx=(10, 25), pady=(0, 10))
        row_label(11, "执行级别")
        level_combo = ttk.Combobox(dialog, textvariable=level_var, state="readonly", values=tuple(EXECUTION_LEVEL_LABELS.values()), width=32)
        level_combo.grid(row=11, column=1, padx=(10, 25), pady=(0, 10))
        row_label(12, "忙卡利用率上限（%）")
        max_util_entry = self._spinbox(dialog, textvariable=max_util_var, from_=0, to=100, width=33)
        max_util_entry.grid(row=12, column=1, padx=(10, 25), pady=(0, 3))
        tk.Label(dialog, text="低干扰执行时，最近采样的最高 GPU 利用率需低于此值。", bg=COLORS["surface"], fg="#9aa7a0", font=(FONT, 8)).grid(row=13, column=1, sticky="w", padx=(10, 25), pady=(0, 8))
        row_label(14, "峰值显存（MB）")
        self._entry(dialog, textvariable=memory_var, width=35).grid(row=14, column=1, padx=(10, 25), pady=(0, 4))
        tk.Label(dialog, text="填 0 或留空 = 自动学习；OOM 后自动提高门槛。", bg=COLORS["surface"], fg="#9aa7a0", font=(FONT, 8)).grid(row=15, column=1, sticky="w", padx=(10, 25), pady=(0, 8))
        self._checkbutton(dialog, text="显存不足后自动等待重试", variable=auto_var).grid(row=16, column=0, columnspan=2, sticky="w", padx=25, pady=(0, 8))
        buttons = tk.Frame(dialog, bg=COLORS["surface"])
        buttons.grid(row=17, column=0, columnspan=2, sticky="e", padx=25, pady=18)
        tk.Button(buttons, text="取消", command=dialog.destroy, relief="flat", bd=0, bg=COLORS["surface"], fg=COLORS["muted"], font=(FONT, 9), padx=10, pady=7).pack(side="left", padx=5)
        submit = tk.Button(buttons, text="保存并重新等待  →" if editing else "加入队列  →", relief="flat", bd=0, bg=COLORS["green"], fg="#ffffff", font=(FONT, 9, "bold"), padx=13, pady=7)
        submit.pack(side="left", padx=5)

        def update_util_entry(_event: Any = None) -> None:
            max_util_entry.configure(state="normal" if EXECUTION_LEVEL_KEYS.get(level_var.get()) == "low_interference" else "disabled")

        level_combo.bind("<<ComboboxSelected>>", update_util_entry)
        update_util_entry()

        def submit_experiment() -> None:
            script = path_var.get()
            if not script or not Path(script).is_file():
                messagebox.showwarning("缺少脚本", "请选择一个 .sh 文件。", parent=dialog)
                return
            name = clean_name(name_var.get(), Path(script).stem)
            level = EXECUTION_LEVEL_KEYS.get(level_var.get(), "idle_only")
            env = "" if env_var.get() == "默认 Python" else env_var.get()
            remember_dependencies()
            depends_on = [
                dependency_ids_by_index[index]
                for index in dependency_list.curselection()
                if index < len(dependency_ids_by_index)
            ]
            selected_server_id = server_ids[server_options.index(server_var.get())] if server_var.get() in server_options else active_server_id
            try:
                if editing:
                    self.update_experiment(experiment["id"], name, script, workdir_var.get().strip(), env, safe_int(priority_var.get(), 50), level, max(0, safe_int(memory_var.get(), 0)), auto_var.get(), max(0, min(100, safe_int(max_util_var.get(), 30))), depends_on)
                else:
                    self.add_experiment(selected_server_id, name, script, workdir_var.get().strip(), env, safe_int(priority_var.get(), 50), level, max(0, safe_int(memory_var.get(), 0)), auto_var.get(), max(0, min(100, safe_int(max_util_var.get(), 30))), depends_on)
                dialog.destroy()
                self.set_status("实验配置已更新并重新进入等待队列" if editing else "实验已加入队列")
            except Exception as exc:
                messagebox.showerror("提交失败", str(exc), parent=dialog)

        submit.configure(command=submit_experiment)

    def choose_script(self, variable: tk.StringVar, label: tk.Label) -> None:
        path = filedialog.askopenfilename(title="选择 Shell 脚本", filetypes=(("Shell script", "*.sh"), ("All files", "*.*")))
        if path:
            variable.set(path)
            label.configure(text=Path(path).name)

    def _normalize_dependency_ids(self, values: Any) -> list[str]:
        if isinstance(values, str):
            values = [values]
        if not isinstance(values, (list, tuple, set)):
            return []
        result: list[str] = []
        for value in values:
            value = str(value or "").strip()
            if value and value not in result:
                result.append(value)
        return result

    def _validate_dependency_ids(self, exp_id: str, dependency_ids: list[str]) -> None:
        with self.manager.store.lock:
            records = {
                str(item.get("id")): item
                for item in self.manager.store.data.get("experiments", [])
                if item.get("id")
            }
            missing = [dependency_id for dependency_id in dependency_ids if dependency_id not in records and dependency_id != exp_id]
            if missing:
                raise ValueError(f"前序任务不存在：{', '.join(missing)}")
            graph = {
                item_id: set(self._normalize_dependency_ids(item.get("depends_on", [])))
                for item_id, item in records.items()
            }
            graph[exp_id] = set(dependency_ids)
        if exp_id in dependency_ids:
            raise ValueError("任务不能把自己设置为前序任务")
        visiting: set[str] = set()
        visited: set[str] = set()

        def has_cycle(node: str) -> bool:
            if node in visiting:
                return True
            if node in visited:
                return False
            visiting.add(node)
            if any(has_cycle(dependency_id) for dependency_id in graph.get(node, set())):
                return True
            visiting.remove(node)
            visited.add(node)
            return False

        if has_cycle(exp_id):
            raise ValueError("前序任务不能形成循环依赖")

    def update_experiment(self, exp_id: str, name: str, script: str, workdir: str, env: str, priority: int, level: str, peak: int, auto_retry: bool, max_gpu_utilization: int = 30, depends_on: list[str] | None = None) -> None:
        depends_on = self._normalize_dependency_ids(depends_on or [])
        self._validate_dependency_ids(exp_id, depends_on)
        with self.manager.store.lock:
            stored = next((item for item in self.manager.store.data.get("experiments", []) if item.get("id") == exp_id), None)
            if stored is None:
                raise ValueError("实验不存在")
            if stored.get("status") == "running" or stored.get("paused_process"):
                raise ValueError("执行中的任务需要先暂停或停止")
            server_id = str(stored.get("server_id") or "default")
        target_runtime = self.manager.runtime_for(server_id)
        if not script or not Path(script).is_file():
            raise ValueError("请选择仍然存在的本地 .sh 文件")
        script_path = Path(script)
        local_script = Path(stored.get("local_script") or (UPLOAD_DIR / f"{exp_id}.sh"))
        local_script.parent.mkdir(parents=True, exist_ok=True)
        # 编辑任务时，文件选择框通常已经指向任务缓存中的原文件。
        # Windows 对同一文件执行 copy2 会抛出 SameFileError，因此相同路径时直接复用。
        try:
            same_script = script_path.resolve() == local_script.resolve()
        except OSError:
            same_script = False
        if not same_script:
            shutil.copy2(script_path, local_script)
        with self.manager.store.lock:
            stored = next((item for item in self.manager.store.data.get("experiments", []) if item.get("id") == exp_id), None)
            if stored is None:
                raise ValueError("实验不存在")
            data = self.manager.store.data
            record = next((item for item in data.setdefault("servers", []) if item.get("id") == server_id), None)
            disk_guard = target_runtime.disk_guard or {}
            if record and record.get("scheduler_paused"):
                new_status = "paused"
                pause_reason = record.get("scheduler_pause_reason") or "该服务器调度已暂停"
            elif disk_guard.get("blocked"):
                new_status = "paused"
                pause_reason = disk_guard.get("message") or "磁盘可用空间不足，已暂停调度"
            else:
                new_status = "queued"
                pause_reason = ""
            stored.update({
                "name": name,
                "script_name": Path(script).name,
                "local_script": str(local_script),
                "workdir": workdir,
                "conda_env": env,
                "priority": max(1, min(999, priority)),
                "execution_level": level,
                "max_gpu_utilization": max(0, min(100, safe_int(max_gpu_utilization, 30))),
                "peak_memory_mb": max(0, peak),
                "auto_peak_memory_mb": 0,
                "auto_retry_oom": auto_retry,
                "depends_on": depends_on,
                "dependency_reason": "",
                "status": new_status,
                "pause_reason": pause_reason,
                "paused_process": False,
                "failure_reason": "",
                "validation_error": "",
                "finished_at": "",
                "exit_code": None,
                "assigned_gpu": None,
                "pid": 0,
                "process_group_id": 0,
            })
            if record is not None:
                record.setdefault("preferences", {}).update({"workdir": workdir, "conda_env": env, "peak_memory_mb": max(0, peak), "execution_level": level, "max_gpu_utilization": max(0, min(100, safe_int(max_gpu_utilization, 30)))})
            data.setdefault("preferences", {}).update({"workdir": workdir, "conda_env": env, "peak_memory_mb": max(0, peak), "execution_level": level, "max_gpu_utilization": max(0, min(100, safe_int(max_gpu_utilization, 30)))})
            self.manager.store.save()
        if target_runtime.connected and new_status == "queued":
            self._tick_in_background()

    def add_experiment(self, server_id: str, name: str, script: str, workdir: str, env: str, priority: int, level: str, peak: int, auto_retry: bool, max_gpu_utilization: int = 30, depends_on: list[str] | None = None) -> None:
        UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
        exp_id = uuid.uuid4().hex[:12]
        depends_on = self._normalize_dependency_ids(depends_on or [])
        self._validate_dependency_ids(exp_id, depends_on)
        local_script = UPLOAD_DIR / f"{exp_id}.sh"
        shutil.copy2(script, local_script)
        target_server_id = server_id or "default"
        target_runtime = self.manager.runtime_for(target_server_id)
        with self.manager.store.lock:
            data = self.manager.store.data
            experiments = data.setdefault("experiments", [])
            sequence = max([safe_int(item.get("created_seq"), 0) for item in experiments] or [0]) + 1
            task_no = max(safe_int(data.get("next_experiment_no"), 1), 1)
            data["next_experiment_no"] = task_no + 1
            record = next((item for item in data.setdefault("servers", []) if item.get("id") == target_server_id), None)
            disk_guard = target_runtime.disk_guard or {}
            if record and record.get("scheduler_paused"):
                initial_status = "paused"
                pause_reason = record.get("scheduler_pause_reason") or "该服务器调度已暂停"
            elif disk_guard.get("blocked"):
                initial_status = "paused"
                pause_reason = disk_guard.get("message") or "磁盘可用空间不足，已暂停调度"
            else:
                initial_status = "queued"
                pause_reason = ""
            experiments.append({
                "id": exp_id,
                "task_no": task_no,
                "server_id": target_server_id,
                "name": name,
                "script_name": Path(script).name,
                "local_script": str(local_script),
                "workdir": workdir,
                "conda_env": env,
                "priority": max(1, min(999, priority)),
                "execution_level": level,
                "max_gpu_utilization": max(0, min(100, safe_int(max_gpu_utilization, 30))),
                "peak_memory_mb": max(0, peak),
                "auto_peak_memory_mb": 0,
                "auto_retry_oom": auto_retry,
                "depends_on": depends_on,
                "dependency_reason": "",
                "status": initial_status,
                "pause_reason": pause_reason,
                "paused_process": False,
                "created_at": now_iso(),
                "created_seq": sequence,
                "attempts": 0,
                "oom_attempts": 0,
                "failure_reason": "",
                "validation_error": "",
            })
            if record is not None:
                record.setdefault("preferences", {}).update({"workdir": workdir, "conda_env": env, "peak_memory_mb": max(0, peak), "execution_level": level, "max_gpu_utilization": max(0, min(100, safe_int(max_gpu_utilization, 30)))})
            data.setdefault("preferences", {}).update({"workdir": workdir, "conda_env": env, "peak_memory_mb": max(0, peak), "execution_level": level, "max_gpu_utilization": max(0, min(100, safe_int(max_gpu_utilization, 30)))})
            self.manager.store.save()
        if target_runtime.connected and initial_status == "queued":
            self._tick_in_background()

    def _selected_item(self) -> dict[str, Any] | None:
        selection = self.queue_tree.selection()
        if not selection:
            return None
        return next((item for item in self.state.get("experiments", []) if item.get("id") == selection[0]), None)

    def edit_selected_experiment(self) -> None:
        item = self._selected_item()
        if not item:
            self.set_status("请先选择一个实验任务", True)
            return
        if item.get("status") == "running" or item.get("paused_process"):
            self.set_status("执行中的任务请先暂停或停止后再编辑", True)
            return
        self.open_experiment_dialog(item)

    def delete_selected_experiment(self) -> None:
        item = self._selected_item()
        if not item:
            self.set_status("请先选择一个实验任务", True)
            return
        if item.get("status") == "running" or item.get("paused_process"):
            self.set_status("正在执行或已暂停进程的任务不能直接删除，请先中断任务", True)
            return
        task_name = f"{task_identifier(item)}  {item.get('name') or item.get('script_name') or '实验任务'}"
        confirmed = messagebox.askyesno(
            "删除实验任务",
            f"确定删除 {task_name} 吗？\n\n本地任务记录和脚本缓存会删除，服务器上的历史日志会保留。",
            parent=self,
        )
        if not confirmed:
            return
        exp_id = str(item.get("id"))

        def action() -> None:
            self.manager.delete_experiment(exp_id)
            if self.selected_log == exp_id:
                self.selected_log = None
            self.queue_signature = None
            self.log_signature = None

        self._run_queue_action(action, "实验任务已删除")

    def retry_selected(self) -> None:
        item = self._selected_item()
        if not item:
            self.set_status("请先选择一个实验任务", True)
            return
        with self.manager.store.lock:
            stored = next((exp for exp in self.manager.store.data.get("experiments", []) if exp.get("id") == item.get("id")), None)
            if stored and (stored.get("status") == "running" or (stored.get("status") == "paused" and stored.get("paused_process"))):
                self.set_status("执行中的任务请先恢复或停止，不能直接重试", True)
                return
            if stored and stored.get("status") not in ("success", "failed", "canceled", "paused", "queued", "waiting_memory"):
                self.set_status("当前任务状态不能重试", True)
                return
            if stored:
                stored.update({"status": "queued", "failure_reason": "", "validation_error": "", "dependency_reason": "", "finished_at": "", "pause_reason": "", "paused_process": False, "assigned_gpu": None, "pid": 0, "process_group_id": 0})
                self.manager.store.save()
        self._tick_in_background()
        self.set_status("实验已重新加入队列")

    def cancel_selected(self) -> None:
        item = self._selected_item()
        if not item:
            self.set_status("请先选择一个实验任务", True)
            return
        exp_id = str(item.get("id"))

        def action() -> None:
            with self.manager.store.lock:
                stored = next((exp for exp in self.manager.store.data.get("experiments", []) if exp.get("id") == exp_id), None)
                if not stored:
                    return
                status = stored.get("status")
            if status in ("running", "paused"):
                self.manager.terminate_experiment(exp_id)
            with self.manager.store.lock:
                stored = next((exp for exp in self.manager.store.data.get("experiments", []) if exp.get("id") == exp_id), None)
                if stored and stored.get("status") in ("queued", "waiting_memory", "running", "paused"):
                    stored.update({"status": "canceled", "finished_at": now_iso(), "failure_reason": "已取消", "pause_reason": "", "paused_process": False})
                    self.manager.store.save()

        self._run_queue_action(action, "任务已中断")

    def run_benchmark(self, server_id: str, gpu_index: int, conda_env: str = "") -> None:
        key = (server_id or "default", gpu_index)
        if key in self.benchmark_running:
            return
        target_runtime = self.manager.runtime_for(server_id)
        server = next((item for item in self.state.get("servers", []) if item.get("id") == server_id), {})
        if not target_runtime.connected:
            self.set_status("请先连接服务器", True)
            return
        self.benchmark_running.add(key)
        self.set_status(f"正在上传测试探针并启动 {server.get('name') or server_id} · GPU {gpu_index}…")

        def worker() -> None:
            try:
                result = self.manager.run_benchmark(server_id, gpu_index, seconds=5, conda_env=conda_env)
                tensor = result.get("tensor_tflops", "—")
                fp32 = result.get("fp32_tflops", "—")
                self.after(0, lambda: self.set_status(f"GPU {gpu_index} 测试完成：Tensor {tensor} / FP32 {fp32} TFLOPS"))
            except Exception as exc:
                self.after(0, lambda: (self.set_status(f"GPU {gpu_index} 测试失败：{exc}", True), messagebox.showerror("GPU 测试失败", str(exc), parent=self)))
            finally:
                self.benchmark_running.discard(key)
                self.after(0, self.refresh_state)

        threading.Thread(target=worker, daemon=True).start()

    def _show_server_error(self) -> None:
        if self.state.get("last_error"):
            self.set_status(self.state["last_error"], True)

    def on_close(self) -> None:
        try:
            if self.refresh_after_id is not None:
                self.after_cancel(self.refresh_after_id)
            for selected_runtime in set(self.manager.runtimes.values()):
                selected_runtime.disconnect()
        finally:
            self.destroy()


if __name__ == "__main__":
    client = DesktopClient()
    client.mainloop()
