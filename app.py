from __future__ import annotations

import hashlib
import json
import os
import re
import shlex
import shutil
import threading
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Any

from flask import Flask, jsonify, render_template, request

try:
    import keyring
except Exception:  # pragma: no cover - optional on headless Linux
    keyring = None

try:
    import paramiko
except Exception:  # pragma: no cover - surfaced by the UI when unavailable
    paramiko = None


BASE_DIR = Path(__file__).resolve().parent
DATA_DIR = BASE_DIR / "data"
UPLOAD_DIR = DATA_DIR / "uploads"
STATE_FILE = DATA_DIR / "state.json"
REMOTE_AGENT_FILE = BASE_DIR / "remote_agent.py"
DISK_GUARD_MIN_FREE_BYTES = 5 * 1024**3
DISK_GUARD_MIN_FREE_MB = DISK_GUARD_MIN_FREE_BYTES // 1024**2
DATA_DIR.mkdir(exist_ok=True)
UPLOAD_DIR.mkdir(exist_ok=True)

app = Flask(__name__, static_folder="static", template_folder="templates")
app.config["JSON_SORT_KEYS"] = False


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def safe_int(value: Any, default: int = 0) -> int:
    try:
        return int(float(str(value).strip()))
    except (TypeError, ValueError):
        return default


def safe_float(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def q(value: str) -> str:
    """Quote a value for a POSIX shell command sent to the remote Linux host."""
    return shlex.quote(str(value))


def clean_name(value: str, fallback: str = "experiment") -> str:
    value = re.sub(r"[^\w\-.一-鿿 ]+", "", value or "", flags=re.UNICODE).strip()
    return value[:80] or fallback


def detect_oom(log_text: str, exit_code: int | None) -> bool:
    text = (log_text or "").lower()
    patterns = (
        "cuda out of memory",
        "out of memory",
        "cublas_status_alloc_failed",
        "cuda error: out of memory",
        "resource exhausted",
        "oom-kill",
        "out-of-memory",
    )
    return any(pattern in text for pattern in patterns) or exit_code in (137, 139)


class StateStore:
    def __init__(self, path: Path):
        self.path = path
        self.lock = threading.RLock()
        self.data = self._load()

    @staticmethod
    def _default() -> dict[str, Any]:
        return {
            "profile": {
                "host": "",
                "port": 22,
                "username": "",
                "home": "",
                "poll_interval": 5,
            },
            "preferences": {
                "workdir": "",
                "conda_env": "",
                "peak_memory_mb": 0,
                "execution_level": "idle_only",
            },
            "experiments": [],
            "next_experiment_no": 1,
            "benchmark_history": [],
            "last_snapshot": None,
            "server_snapshots": {},
            "servers": [],
            "active_server_id": "",
            "last_error": "",
        }

    def _load(self) -> dict[str, Any]:
        if not self.path.exists():
            return self._default()
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
            default = self._default()
            for key, value in default.items():
                data.setdefault(key, value)
            return data
        except (OSError, json.JSONDecodeError):
            return self._default()

    def save(self) -> None:
        with self.lock:
            temp = self.path.with_suffix(".tmp")
            temp.write_text(json.dumps(self.data, ensure_ascii=False, indent=2), encoding="utf-8")
            temp.replace(self.path)


class RemoteClient:
    def __init__(self) -> None:
        self.client = None
        self.sftp = None
        self.lock = threading.RLock()
        self.profile: dict[str, Any] = {}

    @property
    def connected(self) -> bool:
        return bool(self.client and self.client.get_transport() and self.client.get_transport().is_active())

    def connect(self, profile: dict[str, Any], password: str) -> str:
        if paramiko is None:
            raise RuntimeError("未安装 paramiko，请先执行 pip install -r requirements.txt")
        self.close()
        host = str(profile.get("host", "")).strip()
        username = str(profile.get("username", "")).strip()
        port = safe_int(profile.get("port"), 22)
        if not host or not username:
            raise ValueError("服务器 IP 和账号不能为空")
        client = paramiko.SSHClient()
        # The first connection is deliberately convenient for a personal tool. The UI shows a warning.
        client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
        client.connect(
            hostname=host,
            port=port,
            username=username,
            password=password,
            timeout=15,
            banner_timeout=15,
            auth_timeout=15,
            allow_agent=False,
            look_for_keys=False,
        )
        self.client = client
        self.sftp = client.open_sftp()
        self.profile = {**profile, "port": port}
        home = self.exec('printf "%s" "$HOME"')["stdout"].strip()
        if home:
            self.profile["home"] = home
        return home

    def close(self) -> None:
        with self.lock:
            try:
                if self.sftp:
                    self.sftp.close()
            except Exception:
                pass
            try:
                if self.client:
                    self.client.close()
            except Exception:
                pass
            self.sftp = None
            self.client = None

    def exec(self, command: str, timeout: int = 30) -> dict[str, Any]:
        with self.lock:
            if not self.connected:
                raise RuntimeError("尚未连接服务器")
            stdin, stdout, stderr = self.client.exec_command(f"bash -lc {q(command)}", timeout=timeout)
            exit_code = stdout.channel.recv_exit_status()
            return {
                "code": exit_code,
                "stdout": stdout.read().decode("utf-8", errors="replace"),
                "stderr": stderr.read().decode("utf-8", errors="replace"),
            }

    def put(self, local_path: Path, remote_path: str) -> None:
        with self.lock:
            if not self.connected or not self.sftp:
                raise RuntimeError("尚未连接服务器")
            self.sftp.put(str(local_path), remote_path)

    def read_text(self, remote_path: str, tail: int = 120000) -> str:
        with self.lock:
            if not self.connected or not self.sftp:
                raise RuntimeError("尚未连接服务器")
            with self.sftp.open(remote_path, "rb") as handle:
                handle.seek(0, os.SEEK_END)
                size = handle.tell()
                handle.seek(max(0, size - tail), os.SEEK_SET)
                return handle.read().decode("utf-8", errors="replace")


class Runtime:
    def __init__(self, store: StateStore, server_id: str = "default"):
        self.store = store
        self.server_id = server_id
        self.remote = RemoteClient()
        self.lock = threading.RLock()
        self.conda_info: dict[str, Any] = {"available": False, "envs": [], "conda_exe": "", "profile": ""}
        snapshots = store.data.setdefault("server_snapshots", {})
        self.snapshot: dict[str, Any] | None = snapshots.get(server_id)
        if self.snapshot is None and server_id == "default":
            self.snapshot = store.data.get("last_snapshot")
        self.connected = False
        self.remote_root = ""
        self.last_poll_at = str((self.snapshot or {}).get("polled_at") or "")
        self.last_error = ""
        self.disk_guard: dict[str, Any] = {
            "blocked": False,
            "free_bytes": None,
            "threshold_bytes": DISK_GUARD_MIN_FREE_BYTES,
            "message": "",
        }
        self.stop_event = threading.Event()
        self._server_record()
        self.thread = threading.Thread(target=self._loop, name="gpu-orchestrator", daemon=True)
        self.thread.start()

    def _server_record(self) -> dict[str, Any]:
        servers = self.store.data.setdefault("servers", [])
        record = next((item for item in servers if item.get("id") == self.server_id), None)
        if record is None:
            legacy_profile = dict(self.store.data.get("profile", {})) if self.server_id == "default" else {}
            legacy_preferences = dict(self.store.data.get("preferences", {})) if self.server_id == "default" else {}
            record = {
                "id": self.server_id,
                "name": legacy_profile.get("host") or "默认服务器",
                "profile": legacy_profile,
                "preferences": legacy_preferences,
                "auto_connect": False,
                "enabled": True,
                "scheduler_paused": False,
                "scheduler_pause_reason": "",
            }
            servers.append(record)
        record.setdefault("profile", {})
        record.setdefault("preferences", {})
        record.setdefault("name", record.get("profile", {}).get("host") or self.server_id)
        record.setdefault("auto_connect", False)
        record.setdefault("enabled", True)
        record.setdefault("scheduler_paused", False)
        record.setdefault("scheduler_pause_reason", "")
        return record

    def _profile_data(self) -> dict[str, Any]:
        return self._server_record().setdefault("profile", {})

    def _preferences_data(self) -> dict[str, Any]:
        record = self._server_record()
        preferences = record.setdefault("preferences", {})
        if not preferences and self.server_id == "default":
            preferences.update(self.store.data.get("preferences", {}))
        return preferences

    def _server_experiments(self) -> list[dict[str, Any]]:
        return [item for item in self.store.data.get("experiments", []) if item.get("server_id", "default") == self.server_id]

    def _server_benchmarks(self) -> list[dict[str, Any]]:
        return [item for item in self.store.data.get("benchmark_history", []) if item.get("server_id", "default") == self.server_id]

    def _loop(self) -> None:
        while not self.stop_event.is_set():
            interval = max(2, safe_int(self._profile_data().get("poll_interval"), 5))
            enabled = self._server_record().get("enabled", True)
            if self.connected and enabled:
                try:
                    self.tick()
                except Exception as exc:
                    with self.lock:
                        self.last_error = str(exc)
                        self.store.save()
            elif self.connected and not enabled:
                self.disconnect()
            self.stop_event.wait(interval)

    def _keyring_name(self, profile: dict[str, Any]) -> str:
        raw = f"{profile.get('host','')}|{profile.get('port',22)}|{profile.get('username','')}"
        return hashlib.sha256(raw.encode()).hexdigest()[:32]

    def saved_password(self, profile: dict[str, Any]) -> str:
        if keyring is None:
            return ""
        try:
            return keyring.get_password("gpu-orchestrator", self._keyring_name(profile)) or ""
        except Exception:
            return ""

    def save_password(self, profile: dict[str, Any], password: str) -> None:
        if keyring is None or not password:
            return
        try:
            keyring.set_password("gpu-orchestrator", self._keyring_name(profile), password)
        except Exception:
            pass

    def connect(self, profile: dict[str, Any], password: str, save_password: bool) -> dict[str, Any]:
        with self.lock:
            if not password:
                password = self.saved_password(profile)
            if not password:
                raise ValueError("请输入密码；若已保存密码，请确认当前服务器配置一致")
            home = self.remote.connect(profile, password)
            profile = {**profile, "home": home or profile.get("home", "")}
            record = self._server_record()
            record["profile"] = profile
            record["name"] = record.get("name") or profile.get("host") or self.server_id
            if self.server_id == "default":
                self.store.data["profile"] = profile
            if save_password:
                self.save_password(profile, password)
            self.remote_root = self._remote_path("~/.gpu-orchestrator")
            self.remote.exec(f"mkdir -p {q(self.remote_root)}/runs {q(self.remote_root)}/benchmarks")
            self.ensure_agent()
            self.conda_info = self._detect_conda()
            self.connected = True
            self.last_error = ""
            self.store.save()
            self.tick()
            return self.public_state()

    def ensure_agent(self) -> None:
        if not self.connected and not self.remote.connected:
            raise RuntimeError("尚未连接服务器")
        if not self.remote_root:
            self.remote_root = self._remote_path("~/.gpu-orchestrator")
        self.remote.put(REMOTE_AGENT_FILE, f"{self.remote_root}/agent.py")
        self.remote.exec(f"chmod 700 {q(self.remote_root)}/agent.py")

    def disconnect(self) -> None:
        with self.lock:
            self.connected = False
            self.remote.close()
            self.conda_info = {"available": False, "envs": [], "conda_exe": "", "profile": ""}
            self.disk_guard = {
                "blocked": False,
                "free_bytes": None,
                "threshold_bytes": DISK_GUARD_MIN_FREE_BYTES,
                "message": "",
            }

    def _remote_path(self, value: str) -> str:
        home = self.remote.profile.get("home") or self._profile_data().get("home") or "$HOME"
        value = str(value or "").strip()
        if not value:
            return str(home)
        if value == "~":
            return str(home)
        if value.startswith("~/"):
            return f"{home}/{value[2:]}"
        if value.startswith("/"):
            return value
        return f"{home}/{value}"

    def _detect_conda(self) -> dict[str, Any]:
        script = r'''
set -e
CONDA_EXE="$(command -v conda 2>/dev/null || true)"
if [ -z "$CONDA_EXE" ]; then
  for candidate in "$HOME/miniconda3/bin/conda" "$HOME/anaconda3/bin/conda" "$HOME/mambaforge/bin/conda"; do
    if [ -x "$candidate" ]; then CONDA_EXE="$candidate"; break; fi
  done
fi
if [ -z "$CONDA_EXE" ]; then printf '%s' '{}'; exit 0; fi
CONDA_ROOT="$(cd "$(dirname "$CONDA_EXE")/.." && pwd)"
PROFILE="$CONDA_ROOT/etc/profile.d/conda.sh"
if [ -f "$PROFILE" ]; then . "$PROFILE"; fi
JSON="$(conda env list --json 2>/dev/null || printf '%s' '{}')"
python3 - "$CONDA_EXE" "$PROFILE" "$JSON" <<'PY'
import json, sys
try: data=json.loads(sys.argv[3])
except Exception: data={}
data["conda_exe"]=sys.argv[1]
data["profile"]=sys.argv[2]
print(json.dumps(data, ensure_ascii=False))
PY
'''
        result = self.remote.exec(script, timeout=30)
        lines = [line.strip() for line in result["stdout"].splitlines() if line.strip()]
        if not lines:
            return {"available": False, "envs": [], "conda_exe": "", "profile": ""}
        try:
            data = json.loads(lines[-1])
        except json.JSONDecodeError:
            return {"available": False, "envs": [], "conda_exe": "", "profile": ""}
        env_paths = data.get("envs", []) if isinstance(data, dict) else []
        conda_exe = data.get("conda_exe", "") if isinstance(data, dict) else ""
        conda_root = PurePosixPath(conda_exe).parent.parent if conda_exe else None
        envs = []
        for path in env_paths:
            env_path = PurePosixPath(path) if path else PurePosixPath("")
            name = "base" if conda_root and env_path == conda_root else env_path.name
            if name and name not in envs:
                envs.append(name)
        return {
            "available": bool(data.get("conda_exe")),
            "envs": sorted(envs),
            "env_paths": env_paths,
            "conda_exe": conda_exe,
            "profile": data.get("profile", ""),
        }

    def poll(self) -> dict[str, Any]:
        result = self.remote.exec(f"python3 {q(self.remote_root + '/agent.py')} status", timeout=30)
        lines = [line.strip() for line in result["stdout"].splitlines() if line.strip()]
        if result["code"] != 0 or not lines:
            raise RuntimeError(result["stderr"].strip() or "远程状态采集失败")
        try:
            snapshot = json.loads(lines[-1])
        except json.JSONDecodeError as exc:
            raise RuntimeError(f"远程状态格式错误: {exc}") from exc
        snapshot["polled_at"] = now_iso()
        self.snapshot = snapshot
        self.last_poll_at = snapshot["polled_at"]
        return snapshot

    def _update_disk_guard(self, snapshot: dict[str, Any] | None) -> bool:
        disk = (snapshot or {}).get("disk") or {}
        free_bytes = safe_int(disk.get("free_bytes"), -1)
        if free_bytes < 0:
            self.disk_guard = {
                "blocked": False,
                "free_bytes": None,
                "threshold_bytes": DISK_GUARD_MIN_FREE_BYTES,
                "message": "",
            }
            return False
        blocked = free_bytes < DISK_GUARD_MIN_FREE_BYTES
        message = (
            f"主目录可用空间仅 {max(0, free_bytes) / 1024**3:.1f} GB，"
            f"低于安全阈值 {DISK_GUARD_MIN_FREE_MB} MB"
            if blocked
            else ""
        )
        self.disk_guard = {
            "blocked": blocked,
            "free_bytes": free_bytes,
            "threshold_bytes": DISK_GUARD_MIN_FREE_BYTES,
            "message": message,
        }
        return blocked

    def _pause_waiting_for_disk(self) -> None:
        reason = str(self.disk_guard.get("message") or "磁盘可用空间不足，已暂停调度")
        changed = False
        for exp in self._server_experiments():
            status = exp.get("status")
            if status == "running":
                try:
                    self._signal_process(exp, "STOP")
                except Exception as exc:
                    exp["failure_reason"] = f"磁盘保护暂停进程时出现问题：{exc}"
                exp["status"] = "paused"
                exp["pause_reason"] = reason
                exp["paused_process"] = True
                exp["paused_at"] = now_iso()
                changed = True
            elif status in ("queued", "waiting_memory"):
                exp["status"] = "paused"
                exp["pause_reason"] = reason
                exp["paused_process"] = False
                exp["paused_at"] = now_iso()
                changed = True
            else:
                continue
        if changed:
            self.store.save()

    def _scheduler_blocked(self) -> bool:
        record = self._server_record()
        return bool(record.get("scheduler_paused")) or bool(self.disk_guard.get("blocked"))

    def _latest_benchmarks(self) -> dict[int, dict[str, Any]]:
        result = {}
        for item in self._server_benchmarks():
            idx = safe_int(item.get("gpu_index"), -1)
            if idx >= 0:
                result[idx] = item
        return result

    def _enrich_snapshot(self, snapshot: dict[str, Any] | None) -> dict[str, Any] | None:
        if snapshot is None:
            return None
        enriched = json.loads(json.dumps(snapshot))
        benchmarks = self._latest_benchmarks()
        for gpu in enriched.get("gpus", []):
            bench = benchmarks.get(safe_int(gpu.get("index"), -1))
            gpu["benchmark"] = bench
            gpu["reserved_mb"] = 0
            gpu["scheduler_idle"] = bool(gpu.get("is_idle"))
        for exp in self._server_experiments():
            if exp.get("status") in ("running", "paused") and exp.get("assigned_gpu"):
                idx = safe_int((exp.get("assigned_gpu") or {}).get("index"), -1)
                for gpu in enriched.get("gpus", []):
                    if safe_int(gpu.get("index"), -1) == idx:
                        gpu["reserved_mb"] += max(0, safe_int(exp.get("peak_memory_mb"), 0))
                        gpu["scheduler_idle"] = False
                        break
        for gpu in enriched.get("gpus", []):
            gpu["scheduler_free_mb"] = max(0, safe_int(gpu.get("memory_free_mb"), 0) - safe_int(gpu.get("reserved_mb"), 0))
        return enriched

    def _read_log(self, exp: dict[str, Any]) -> str:
        path = exp.get("log_path")
        if not path or not self.connected:
            return ""
        try:
            return self.remote.read_text(path, tail=60000)
        except Exception:
            return ""

    def _finish_running(self) -> None:
        for exp in self._server_experiments():
            if exp.get("status") != "running" and not (exp.get("status") == "paused" and exp.get("paused_process")):
                continue
            exit_path = exp.get("exit_path", "")
            code_text = ""
            try:
                code_text = self.remote.read_text(exit_path, tail=100).strip() if exit_path else ""
            except Exception:
                code_text = ""
            if code_text and re.fullmatch(r"-?\d+", code_text):
                code = safe_int(code_text, 1)
                exp["exit_code"] = code
                exp["finished_at"] = now_iso()
                if code == 0:
                    exp["status"] = "success"
                    exp["failure_reason"] = ""
                    exp["pause_reason"] = ""
                    exp["paused_process"] = False
                else:
                    log = self._read_log(exp)
                    if detect_oom(log, code) and exp.get("auto_retry_oom", True):
                        exp["status"] = "waiting_memory"
                        exp["failure_reason"] = "检测到显存不足，已自动提高显存门槛，等待下次满足条件"
                        exp["pause_reason"] = ""
                        exp["paused_process"] = False
                        gpu = None
                        if self.snapshot:
                            for item in self.snapshot.get("gpus", []):
                                if safe_int(item.get("index"), -1) == safe_int((exp.get("assigned_gpu") or {}).get("index"), -1):
                                    gpu = item
                                    break
                        observed_free = safe_int((gpu or {}).get("memory_free_mb"), 0)
                        old_peak = safe_int(exp.get("peak_memory_mb"), 0)
                        inferred = max(old_peak + 512, observed_free + 512, 1024)
                        exp["peak_memory_mb"] = inferred
                        exp["auto_peak_memory_mb"] = inferred
                        exp["oom_attempts"] = safe_int(exp.get("oom_attempts"), 0) + 1
                    else:
                        exp["status"] = "failed"
                        exp["failure_reason"] = f"脚本退出码 {code}"
                        exp["pause_reason"] = ""
                        exp["paused_process"] = False
                continue
            pid = safe_int(exp.get("pid"), 0)
            if pid:
                check = self.remote.exec(f"kill -0 {pid} >/dev/null 2>&1; printf '%s' $?", timeout=10)
                if check["stdout"].strip() not in ("0", ""):
                    exp["status"] = "failed"
                    exp["finished_at"] = now_iso()
                    exp["failure_reason"] = "远程进程已结束，但没有写入退出码"
                    exp["pause_reason"] = ""
                    exp["paused_process"] = False

    def _sort_key(self, exp: dict[str, Any]) -> tuple[int, int]:
        return safe_int(exp.get("priority"), 50), safe_int(exp.get("created_seq"), 0)

    def _gpu_speed(self, gpu: dict[str, Any]) -> float:
        bench = gpu.get("benchmark") or {}
        measured = safe_float(bench.get("tensor_tflops"), 0)
        if measured > 0:
            return measured
        return safe_float(gpu.get("performance_hint"), 0)

    def _choose_gpu(self, exp: dict[str, Any]) -> dict[str, Any] | None:
        if not self.snapshot:
            return None
        required = max(0, safe_int(exp.get("peak_memory_mb"), 0))
        level = exp.get("execution_level", "idle_only")
        eligible = []
        for gpu in self.snapshot.get("gpus", []):
            free_mb = safe_int(gpu.get("scheduler_free_mb", gpu.get("memory_free_mb")), 0)
            enough = free_mb >= required
            idle = bool(gpu.get("scheduler_idle", gpu.get("is_idle")))
            if enough:
                eligible.append((gpu, idle))
        if level == "emergency":
            idle_choices = [gpu for gpu, idle in eligible if idle]
            choices = idle_choices or [gpu for gpu, _idle in eligible]
        else:
            choices = [gpu for gpu, idle in eligible if idle]
        choices.sort(key=lambda item: (-self._gpu_speed(item), safe_int(item.get("index"), 0)))
        return choices[0] if choices else None

    def _conda_run_prefix(self, env: str) -> str:
        profile = self.conda_info.get("profile", "")
        if not env:
            return ""
        if not profile:
            return f"conda run --no-capture-output -n {q(env)}"
        return f". {q(profile)} >/dev/null 2>&1 && conda run --no-capture-output -n {q(env)}"

    def _start(self, exp: dict[str, Any], gpu: dict[str, Any]) -> None:
        exp_id = exp["id"]
        run_dir = f"{self.remote_root}/runs/{exp_id}"
        script_path = f"{run_dir}/run.sh"
        log_path = f"{run_dir}/run.log"
        exit_path = f"{run_dir}/exit.code"
        workdir = self._remote_path(exp.get("workdir", ""))
        env = str(exp.get("conda_env", "")).strip()
        self.remote.exec(f"mkdir -p {q(run_dir)}")
        self.remote.put(Path(exp["local_script"]), script_path)
        self.remote.exec(f"chmod 700 {q(script_path)}")
        run_command = f"bash {q(script_path)}"
        conda_prefix = self._conda_run_prefix(env)
        if conda_prefix:
            run_command = f"{conda_prefix} {run_command}"
        inner = (
            f"exec >{q(log_path)} 2>&1\n"
            f"printf '%s\\n' 'started {now_iso()}'\n"
            f"cd -- {q(workdir)}\n"
            f"if [ $? -ne 0 ]; then code=111; printf '%s\\n' \"$code\" > {q(exit_path)}; exit \"$code\"; fi\n"
            f"export CUDA_DEVICE_ORDER=PCI_BUS_ID\n"
            f"export CUDA_VISIBLE_DEVICES={q(str(gpu.get('index', 0)))}\n"
            f"{run_command}\n"
            f"code=$?\n"
            f"printf '%s\\n' \"$code\" > {q(exit_path)}\n"
            f"exit \"$code\"\n"
        )
        # The same experiment may be retried or re-queued. Remove the prior
        # attempt's exit marker before starting, otherwise the next poll could
        # mistake an old exit code for the new process.
        self.remote.exec(f"rm -f {q(exit_path)}")
        result = self.remote.exec(f"nohup setsid bash -lc {q(inner)} >/dev/null 2>&1 & echo $!", timeout=15)
        pid_lines = [line.strip() for line in result["stdout"].splitlines() if line.strip()]
        pid = safe_int(pid_lines[-1] if pid_lines else 0, 0)
        if result["code"] != 0 or not pid:
            raise RuntimeError(result["stderr"].strip() or "无法启动远程实验")
        exp.update(
            {
                "status": "running",
                "pid": pid,
                "process_group_id": pid,
                "started_at": now_iso(),
                "attempts": safe_int(exp.get("attempts"), 0) + 1,
                "assigned_gpu": {
                    "index": safe_int(gpu.get("index"), 0),
                    "name": gpu.get("name", ""),
                    "tensor_tflops": safe_float((gpu.get("benchmark") or {}).get("tensor_tflops"), 0) or None,
                },
                "script_path": script_path,
                "log_path": log_path,
                "exit_path": exit_path,
                "remote_workdir": workdir,
                "validation_error": "",
                "pause_reason": "",
                "paused_process": False,
            }
        )

    def _experiment(self, exp_id: str) -> dict[str, Any] | None:
        return next((item for item in self._server_experiments() if item.get("id") == exp_id), None)

    def _dependency_ids(self, exp: dict[str, Any]) -> list[str]:
        raw = exp.get("depends_on", [])
        if isinstance(raw, str):
            raw = [raw]
        if not isinstance(raw, (list, tuple, set)):
            return []
        result: list[str] = []
        for value in raw:
            value = str(value or "").strip()
            if value and value not in result:
                result.append(value)
        return result

    def _dependencies_ready(self, exp: dict[str, Any]) -> tuple[bool, str]:
        dependency_ids = self._dependency_ids(exp)
        if not dependency_ids:
            return True, ""
        records = {str(item.get("id")): item for item in self.store.data.get("experiments", []) if item.get("id")}
        waiting: list[str] = []
        blocked: list[str] = []
        for dependency_id in dependency_ids:
            dependency = records.get(dependency_id)
            if dependency is None:
                blocked.append(f"任务 {dependency_id} 不存在")
                continue
            status = str(dependency.get("status") or "queued")
            name = str(dependency.get("name") or dependency_id)
            if status == "success":
                continue
            if status in ("failed", "canceled"):
                status_label = {"failed": "执行失败", "canceled": "已取消"}.get(status, status)
                blocked.append(f"{name}（{status_label}）")
            else:
                waiting.append(name)
        if blocked:
            return False, "前序任务未成功完成：" + "、".join(blocked[:3])
        if waiting:
            return False, "等待前序任务完成：" + "、".join(waiting[:3])
        return True, ""

    def _signal_process(self, exp: dict[str, Any], signal_name: str) -> None:
        pid = safe_int(exp.get("pid"), 0)
        process_group_id = safe_int(exp.get("process_group_id"), 0) or pid
        if not pid:
            raise RuntimeError("任务没有可用的远程进程 PID")
        command = (
            f"if kill -{signal_name} -- -{process_group_id} >/dev/null 2>&1; then code=0; "
            f"elif kill -{signal_name} {pid} >/dev/null 2>&1; then code=0; else code=1; fi; printf '%s' \"$code\""
        )
        result = self.remote.exec(command, timeout=10)
        if result["stdout"].strip() != "0":
            raise RuntimeError(result["stderr"].strip() or f"无法发送 {signal_name} 信号")

    def pause_experiment(self, exp_id: str) -> None:
        with self.lock:
            exp = self._experiment(exp_id)
            if not exp:
                raise ValueError("实验不存在")
            status = exp.get("status")
            if status == "paused":
                return
            if status == "running":
                self._signal_process(exp, "STOP")
                exp["paused_process"] = True
            elif status not in ("queued", "waiting_memory"):
                raise ValueError("只有等待中或执行中的任务可以暂停")
            else:
                exp["paused_process"] = False
            exp["status"] = "paused"
            exp["pause_reason"] = "用户手动暂停"
            exp["paused_at"] = now_iso()
            self.store.save()

    def resume_experiment(self, exp_id: str) -> None:
        with self.lock:
            exp = self._experiment(exp_id)
            if not exp:
                raise ValueError("实验不存在")
            if exp.get("status") != "paused":
                return
            if exp.get("paused_process"):
                pid = safe_int(exp.get("pid"), 0)
                check = self.remote.exec(f"kill -0 {pid} >/dev/null 2>&1; printf '%s' $?", timeout=10)
                if check["stdout"].strip() not in ("0", ""):
                    exp["status"] = "failed"
                    exp["finished_at"] = now_iso()
                    exp["failure_reason"] = "暂停期间远程进程已结束，无法恢复"
                    exp["pause_reason"] = ""
                    exp["paused_process"] = False
                    self.store.save()
                    return
                self._signal_process(exp, "CONT")
                exp["status"] = "running"
            else:
                exp["status"] = "queued"
            exp["pause_reason"] = ""
            exp["paused_process"] = False
            exp["resumed_at"] = now_iso()
            self.store.save()

    def requeue_experiment(self, exp_id: str) -> None:
        """Stop a paused process and put the task back into the scheduler queue."""
        with self.lock:
            exp = self._experiment(exp_id)
            if not exp:
                raise ValueError("实验不存在")
            if exp.get("status") != "paused":
                return
            if exp.get("paused_process"):
                pid = safe_int(exp.get("pid"), 0)
                process_group_id = safe_int(exp.get("process_group_id"), 0) or pid
                if not pid:
                    raise RuntimeError("暂停任务没有可用的远程进程 PID")
                command = (
                    f"kill -TERM -- -{process_group_id} >/dev/null 2>&1 || kill -TERM {pid} >/dev/null 2>&1 || true; "
                    f"sleep 1; kill -KILL -- -{process_group_id} >/dev/null 2>&1 || kill -KILL {pid} >/dev/null 2>&1 || true"
                )
                self.remote.exec(command, timeout=15)
            exp.update({
                "status": "queued",
                "pause_reason": "",
                "paused_process": False,
                "assigned_gpu": None,
                "pid": 0,
                "process_group_id": 0,
                "finished_at": "",
                "failure_reason": "",
            })
            exp["resumed_at"] = now_iso()
            self.store.save()

    def terminate_experiment(self, exp_id: str) -> None:
        with self.lock:
            exp = self._experiment(exp_id)
            if not exp or exp.get("status") not in ("running", "paused"):
                return
            pid = safe_int(exp.get("pid"), 0)
            if not pid:
                return
            process_group_id = safe_int(exp.get("process_group_id"), 0) or pid
            # _start() launches each experiment with setsid, so the recorded
            # PID is also the root of an isolated process group/session. Stop
            # the whole group, then force-kill it if children ignore TERM.
            command = (
                f"if kill -TERM -- -{process_group_id} >/dev/null 2>&1; then :; "
                f"else kill -TERM {pid} >/dev/null 2>&1 || true; fi; "
                f"for attempt in 1 2 3 4 5; do "
                f"if kill -0 -- -{process_group_id} >/dev/null 2>&1 || kill -0 {pid} >/dev/null 2>&1; "
                f"then sleep 1; else exit 0; fi; done; "
                f"kill -KILL -- -{process_group_id} >/dev/null 2>&1 || "
                f"kill -KILL {pid} >/dev/null 2>&1 || true"
            )
            self.remote.exec(command, timeout=15)

    def _schedule(self) -> None:
        if not self.connected or not self.snapshot or self._scheduler_blocked():
            return
        candidates = [
            item
            for item in self._server_experiments()
            if item.get("status") in ("queued", "waiting_memory")
        ]
        candidates.sort(key=self._sort_key)
        for exp in candidates:
            dependencies_ready, dependency_reason = self._dependencies_ready(exp)
            if not dependencies_ready:
                exp["dependency_reason"] = dependency_reason
                continue
            exp["dependency_reason"] = ""
            env = str(exp.get("conda_env", "")).strip()
            if env and env not in self.conda_info.get("envs", []):
                exp["validation_error"] = f"找不到 Conda 环境：{env}"
                continue
            gpu = self._choose_gpu(exp)
            if not gpu:
                continue
            try:
                self._start(exp, gpu)
                gpu["scheduler_idle"] = False
                gpu["reserved_mb"] = safe_int(gpu.get("reserved_mb"), 0) + max(0, safe_int(exp.get("peak_memory_mb"), 0))
                gpu["scheduler_free_mb"] = max(0, safe_int(gpu.get("memory_free_mb"), 0) - safe_int(gpu.get("reserved_mb"), 0))
            except Exception as exc:
                exp["status"] = "failed"
                exp["finished_at"] = now_iso()
                exp["failure_reason"] = str(exc)

    def tick(self) -> None:
        with self.lock:
            if not self.connected or not self._server_record().get("enabled", True):
                return
            try:
                self.poll()
                self._update_disk_guard(self.snapshot)
                self._finish_running()
                if self.disk_guard.get("blocked"):
                    self._pause_waiting_for_disk()
                self.snapshot = self._enrich_snapshot(self.snapshot)
                self._schedule()
                self.snapshot = self._enrich_snapshot(self.snapshot)
                self.store.data.setdefault("server_snapshots", {})[self.server_id] = self.snapshot
                if self.server_id == "default":
                    self.store.data["last_snapshot"] = self.snapshot
                self.last_error = ""
                self.store.save()
            except Exception as exc:
                self.last_error = str(exc)
                self.store.save()

    def run_benchmark(self, gpu_index: int, seconds: int = 5, conda_env: str = "") -> dict[str, Any]:
        with self.lock:
            if not self.connected or not self.remote.connected:
                raise RuntimeError("请先连接服务器")
            self.ensure_agent()
            command = f"python3 {q(self.remote_root + '/agent.py')} benchmark --gpu-index {safe_int(gpu_index)} --seconds {max(2, min(20, safe_int(seconds, 5)))}"
            if conda_env:
                prefix = self._conda_run_prefix(conda_env)
                command = f"{prefix} {command}"
        # The matrix multiplication can take a long time on a busy server.  Do
        # not keep Runtime.lock while waiting for the SSH command, otherwise a
        # normal UI state refresh would wait behind the benchmark.
        result = self.remote.exec(command, timeout=90)
        lines = [line.strip() for line in result["stdout"].splitlines() if line.strip()]
        if result["code"] != 0 or not lines:
            raise RuntimeError(result["stderr"].strip() or "显卡测试失败")
        try:
            data = json.loads(lines[-1])
        except json.JSONDecodeError as exc:
            raise RuntimeError(f"测试返回格式错误: {exc}") from exc
        if data.get("ok") is False:
            raise RuntimeError(str(data.get("error") or "显卡测试失败"))
        data["gpu_index"] = safe_int(gpu_index)
        data["created_at"] = now_iso()
        data["env"] = conda_env
        data["server_id"] = self.server_id
        with self.lock:
            self.store.data.setdefault("benchmark_history", []).append(data)
            self.store.data["benchmark_history"] = self.store.data["benchmark_history"][-48:]
            self.snapshot = self._enrich_snapshot(self.snapshot)
            self.store.data.setdefault("server_snapshots", {})[self.server_id] = self.snapshot
            if self.server_id == "default":
                self.store.data["last_snapshot"] = self.snapshot
            self.store.save()
        return data

    def public_state(self) -> dict[str, Any]:
        acquired = self.lock.acquire(blocking=False)
        try:
            profile = dict(self._profile_data())
            profile.pop("password", None)
            record = self._server_record()
            experiments = []
            for item in self._server_experiments():
                visible = {key: value for key, value in item.items() if key != "local_script"}
                experiments.append(visible)
            return {
                "connected": self.connected and self.remote.connected,
                "server_id": self.server_id,
                "server_name": self._server_record().get("name", self.server_id),
                "profile": profile,
                "preferences": self._preferences_data(),
                "snapshot": self._enrich_snapshot(self.snapshot),
                "conda": self.conda_info,
                "experiments": experiments,
                "benchmark_history": self._server_benchmarks(),
                "last_error": self.last_error,
                "last_poll_at": self.last_poll_at,
                "remote_root": self.remote_root,
                "disk_guard": dict(self.disk_guard),
                "scheduler_paused": bool(record.get("scheduler_paused")),
                "scheduler_pause_reason": record.get("scheduler_pause_reason", ""),
            }
        finally:
            if acquired:
                self.lock.release()


store = StateStore(STATE_FILE)
runtime = Runtime(store)


class RuntimeManager:
    """Owns one Runtime worker per configured server for the web UI."""

    def __init__(self, store_ref: StateStore) -> None:
        self.store = store_ref
        self.runtimes: dict[str, Runtime] = {"default": runtime}
        self._ensure_server_config()
        self._ensure_experiment_numbers()
        self._sync_runtimes()

    def _ensure_server_config(self) -> None:
        data = self.store.data
        servers = data.setdefault("servers", [])
        changed = False
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
            [safe_int(item.get("task_no"), safe_int(item.get("created_seq"), 0)) for item in experiments]
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

    def runtime_for(self, server_id: str | None) -> Runtime:
        self._sync_runtimes()
        return self.runtimes.get(server_id or "default", runtime)

    def _visible_experiments(self) -> list[dict[str, Any]]:
        return [
            {key: value for key, value in item.items() if key != "local_script"}
            for item in self.store.data.get("experiments", [])
        ]

    def public_state(self) -> dict[str, Any]:
        self._sync_runtimes()
        active_state = self.runtime_for(self.active_server_id).public_state()
        servers = []
        server_states: dict[str, dict[str, Any]] = {}
        for record in self.store.data.get("servers", []):
            server_id = str(record.get("id"))
            state = self.runtime_for(server_id).public_state()
            profile = state.get("profile") or {}
            servers.append(
                {
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
                }
            )
            server_states[server_id] = state
        active_state["experiments"] = self._visible_experiments()
        active_state["benchmark_history"] = sorted(
            self.store.data.get("benchmark_history", []), key=lambda item: item.get("created_at", "")
        )[-48:]
        active_state["servers"] = servers
        active_state["server_states"] = server_states
        active_state["active_server_id"] = self.active_server_id
        active_record = next(
            (item for item in self.store.data.get("servers", []) if item.get("id") == self.active_server_id), {}
        )
        active_state["connected"] = bool(
            active_record.get("enabled", True) and active_state.get("connected", False)
        )
        return active_state

    def set_active(self, server_id: str) -> None:
        if not any(item.get("id") == server_id for item in self.store.data.get("servers", [])):
            raise ValueError("服务器不存在")
        self.store.data["active_server_id"] = server_id
        self.store.save()

    def upsert_server(self, server_id: str | None, name: str, profile: dict[str, Any], auto_connect: bool) -> str:
        self._sync_runtimes()
        server_id = server_id or f"server-{uuid.uuid4().hex[:8]}"
        record = next(
            (item for item in self.store.data.setdefault("servers", []) if item.get("id") == server_id), None
        )
        if record is None:
            record = {"id": server_id}
            self.store.data["servers"].append(record)
        record.update(
            {
                "name": name or profile.get("host") or server_id,
                "profile": dict(profile),
                "auto_connect": bool(auto_connect),
                "_auto_connect_configured": True,
            }
        )
        record.setdefault("enabled", True)
        record.setdefault("preferences", {})
        record.setdefault("scheduler_paused", False)
        record.setdefault("scheduler_pause_reason", "")
        self.store.data["active_server_id"] = server_id
        self.store.save()
        self._sync_runtimes()
        return server_id

    def remove_server(self, server_id: str) -> None:
        if server_id == "default":
            raise ValueError("默认服务器不能删除")
        rt = self.runtime_for(server_id)
        if any(
            item.get("server_id") == server_id and item.get("status") == "running"
            for item in self.store.data.get("experiments", [])
        ):
            raise ValueError("该服务器还有运行中的实验，不能删除")
        rt.disconnect()
        # The runtime's poll loop calls _server_record() every iteration,
        # which would re-create the deleted record. Stop it before removing.
        rt.stop_event.set()
        self.runtimes.pop(server_id, None)
        self.store.data["servers"] = [
            item for item in self.store.data.get("servers", []) if item.get("id") != server_id
        ]
        self.store.data["active_server_id"] = "default"
        self.store.save()

    def set_enabled(self, server_id: str, enabled: bool) -> None:
        record = next(
            (item for item in self.store.data.get("servers", []) if item.get("id") == server_id), None
        )
        if record is None:
            raise ValueError("服务器不存在")
        record["enabled"] = bool(enabled)
        self.store.save()
        if enabled:
            self.auto_connect()
        else:
            self.runtime_for(server_id).disconnect()

    def set_scheduler_paused(self, server_id: str, paused: bool) -> None:
        record = next(
            (item for item in self.store.data.get("servers", []) if item.get("id") == server_id), None
        )
        if record is None:
            raise ValueError("服务器不存在")
        record["scheduler_paused"] = bool(paused)
        record["scheduler_pause_reason"] = "用户手动暂停调度" if paused else ""
        self.store.save()

    def _experiment_record(self, exp_id: str) -> dict[str, Any] | None:
        return next(
            (item for item in self.store.data.get("experiments", []) if item.get("id") == exp_id), None
        )

    def set_experiment_paused(self, exp_id: str, paused: bool) -> None:
        with self.store.lock:
            stored = self._experiment_record(exp_id)
            if stored is None:
                raise ValueError("实验不存在")
            server_id = str(stored.get("server_id") or "default")
            status = stored.get("status")
        target = self.runtime_for(server_id)
        if paused:
            if status == "paused":
                return
            if status not in ("queued", "waiting_memory", "running"):
                raise ValueError("只有等待中或执行中的任务可以暂停")
            target.pause_experiment(exp_id)
        else:
            if status != "paused":
                return
            target.resume_experiment(exp_id)

    def pause_all_experiments(self) -> None:
        with self.store.lock:
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
                target = self.runtime_for(server_id)
                if paused_process:
                    target.requeue_experiment(exp_id)
                else:
                    target.resume_experiment(exp_id)
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
                    labels.append(
                        f"#{task_no:04d} {item.get('name') or item.get('id')}"
                        if task_no
                        else str(item.get("name") or item.get("id"))
                    )
                raise ValueError("仍有任务依赖它：" + "、".join(labels))
            raw_script = str(stored.get("local_script") or "").strip()
            if raw_script:
                local_script = Path(raw_script)
            self.store.data["experiments"] = [
                item for item in self.store.data.get("experiments", []) if str(item.get("id")) != exp_id
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

    def retry_experiment(self, exp_id: str) -> None:
        with self.store.lock:
            stored = self._experiment_record(exp_id)
            if stored is None:
                raise ValueError("实验不存在")
            if stored.get("status") == "running" or (
                stored.get("status") == "paused" and stored.get("paused_process")
            ):
                raise ValueError("执行中的任务请先恢复或停止，不能直接重试")
            if stored.get("status") not in ("success", "failed", "canceled", "paused", "queued", "waiting_memory"):
                raise ValueError("当前任务状态不能重试")
            server_id = str(stored.get("server_id") or "default")
            stored.update(
                {
                    "status": "queued",
                    "failure_reason": "",
                    "validation_error": "",
                    "dependency_reason": "",
                    "finished_at": "",
                    "pause_reason": "",
                    "paused_process": False,
                    "assigned_gpu": None,
                    "pid": 0,
                    "process_group_id": 0,
                }
            )
            self.store.save()
        target = self.runtime_for(server_id)
        if target.connected:
            threading.Thread(target=target.tick, daemon=True).start()

    def cancel_experiment(self, exp_id: str) -> None:
        with self.store.lock:
            stored = self._experiment_record(exp_id)
            if stored is None:
                raise ValueError("实验不存在")
            server_id = str(stored.get("server_id") or "default")
            status = stored.get("status")
        if status in ("running", "paused"):
            try:
                self.terminate_experiment(exp_id)
            except Exception:
                pass
        with self.store.lock:
            stored = self._experiment_record(exp_id)
            if stored and stored.get("status") in ("queued", "waiting_memory", "running", "paused"):
                stored.update(
                    {
                        "status": "canceled",
                        "finished_at": now_iso(),
                        "failure_reason": "已取消",
                        "pause_reason": "",
                        "paused_process": False,
                    }
                )
                self.store.save()

    def requeue_experiment(self, exp_id: str) -> None:
        with self.store.lock:
            stored = self._experiment_record(exp_id)
            if stored is None:
                raise ValueError("实验不存在")
            if stored.get("status") != "paused":
                raise ValueError("只有暂停中的任务可以重新等待")
            server_id = str(stored.get("server_id") or "default")
        self.runtime_for(server_id).requeue_experiment(exp_id)

    @staticmethod
    def _normalize_dependency_ids(values: Any) -> list[str]:
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
        if exp_id in dependency_ids:
            raise ValueError("任务不能把自己设置为前序任务")
        with self.store.lock:
            records = {
                str(item.get("id")): item
                for item in self.store.data.get("experiments", [])
                if item.get("id")
            }
            missing = [dependency_id for dependency_id in dependency_ids if dependency_id not in records]
            if missing:
                raise ValueError(f"前序任务不存在：{', '.join(missing)}")
            graph = {
                item_id: set(self._normalize_dependency_ids(item.get("depends_on", [])))
                for item_id, item in records.items()
            }
            graph[exp_id] = set(dependency_ids)
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

    def _initial_queue_status(self, server_id: str, target_runtime: Runtime) -> tuple[str, str]:
        record = next(
            (item for item in self.store.data.get("servers", []) if item.get("id") == server_id), None
        )
        disk_guard = target_runtime.disk_guard or {}
        if record and record.get("scheduler_paused"):
            return "paused", record.get("scheduler_pause_reason") or "该服务器调度已暂停"
        if disk_guard.get("blocked"):
            return "paused", disk_guard.get("message") or "磁盘可用空间不足，已暂停调度"
        return "queued", ""

    def add_experiment(
        self,
        server_id: str,
        name: str,
        script_src: Path,
        script_name: str,
        workdir: str,
        env: str,
        priority: int,
        level: str,
        peak: int,
        auto_retry: bool,
        depends_on: list[str] | None = None,
    ) -> str:
        UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
        exp_id = uuid.uuid4().hex[:12]
        depends_on = self._normalize_dependency_ids(depends_on or [])
        self._validate_dependency_ids(exp_id, depends_on)
        local_script = UPLOAD_DIR / f"{exp_id}.sh"
        shutil.copy2(script_src, local_script)
        try:
            script_src.unlink(missing_ok=True)
        except OSError:
            pass
        target_server_id = server_id or "default"
        target_runtime = self.runtime_for(target_server_id)
        with self.store.lock:
            data = self.store.data
            experiments = data.setdefault("experiments", [])
            sequence = max([safe_int(item.get("created_seq"), 0) for item in experiments] or [0]) + 1
            task_no = max(safe_int(data.get("next_experiment_no"), 1), 1)
            data["next_experiment_no"] = task_no + 1
            initial_status, pause_reason = self._initial_queue_status(target_server_id, target_runtime)
            experiments.append(
                {
                    "id": exp_id,
                    "task_no": task_no,
                    "server_id": target_server_id,
                    "name": name,
                    "script_name": script_name,
                    "local_script": str(local_script),
                    "workdir": workdir,
                    "conda_env": env,
                    "priority": max(1, min(999, priority)),
                    "execution_level": level,
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
                }
            )
            record = next(
                (item for item in data.setdefault("servers", []) if item.get("id") == target_server_id), None
            )
            if record is not None:
                record.setdefault("preferences", {}).update(
                    {"workdir": workdir, "conda_env": env, "peak_memory_mb": max(0, peak), "execution_level": level}
                )
            data.setdefault("preferences", {}).update(
                {"workdir": workdir, "conda_env": env, "peak_memory_mb": max(0, peak), "execution_level": level}
            )
            self.store.save()
        if target_runtime.connected and initial_status == "queued":
            threading.Thread(target=target_runtime.tick, daemon=True).start()
        return exp_id

    def update_experiment(
        self,
        exp_id: str,
        name: str,
        script_src: Path | None,
        script_name: str | None,
        workdir: str,
        env: str,
        priority: int,
        level: str,
        peak: int,
        auto_retry: bool,
        depends_on: list[str] | None = None,
    ) -> None:
        depends_on = self._normalize_dependency_ids(depends_on or [])
        self._validate_dependency_ids(exp_id, depends_on)
        with self.store.lock:
            stored = self._experiment_record(exp_id)
            if stored is None:
                raise ValueError("实验不存在")
            if stored.get("status") == "running" or stored.get("paused_process"):
                raise ValueError("执行中的任务需要先暂停或停止")
            server_id = str(stored.get("server_id") or "default")
            local_script = Path(str(stored.get("local_script") or (UPLOAD_DIR / f"{exp_id}.sh")))
        local_script.parent.mkdir(parents=True, exist_ok=True)
        if script_src is not None:
            try:
                same_script = script_src.resolve() == local_script.resolve()
            except OSError:
                same_script = False
            if not same_script:
                shutil.copy2(script_src, local_script)
            else:
                script_src = None
            try:
                script_src.unlink(missing_ok=True) if script_src else None
            except OSError:
                pass
        target_runtime = self.runtime_for(server_id)
        with self.store.lock:
            stored = self._experiment_record(exp_id)
            if stored is None:
                raise ValueError("实验不存在")
            data = self.store.data
            record = next(
                (item for item in data.setdefault("servers", []) if item.get("id") == server_id), None
            )
            new_status, pause_reason = self._initial_queue_status(server_id, target_runtime)
            stored.update(
                {
                    "name": name,
                    "script_name": script_name or stored.get("script_name"),
                    "local_script": str(local_script),
                    "workdir": workdir,
                    "conda_env": env,
                    "priority": max(1, min(999, priority)),
                    "execution_level": level,
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
                }
            )
            if record is not None:
                record.setdefault("preferences", {}).update(
                    {"workdir": workdir, "conda_env": env, "peak_memory_mb": max(0, peak), "execution_level": level}
                )
            data.setdefault("preferences", {}).update(
                {"workdir": workdir, "conda_env": env, "peak_memory_mb": max(0, peak), "execution_level": level}
            )
            self.store.save()
        if target_runtime.connected and new_status == "queued":
            threading.Thread(target=target_runtime.tick, daemon=True).start()

    def connect(self, server_id: str, profile: dict[str, Any], password: str, save_password: bool) -> dict[str, Any]:
        record = next(
            (item for item in self.store.data.get("servers", []) if item.get("id") == server_id), None
        )
        if record is None:
            raise ValueError("服务器不存在")
        record["profile"] = dict(profile)
        self.store.save()
        selected_runtime = self.runtime_for(server_id)
        try:
            selected_runtime.connect(profile, password, save_password)
        except Exception as exc:
            selected_runtime.last_error = str(exc)
            self.store.save()
            raise
        self.store.data["active_server_id"] = server_id
        self.store.save()
        return self.public_state()

    def disconnect(self, server_id: str | None = None) -> None:
        self.runtime_for(server_id or self.active_server_id).disconnect()

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
        record = next(
            (item for item in self.store.data.get("servers", []) if item.get("id") == server_id), {}
        )
        if not record.get("enabled", True):
            raise RuntimeError("该服务器监控已停用，请先启用监控")
        return self.runtime_for(server_id).run_benchmark(gpu_index, seconds=seconds, conda_env=conda_env)

    def read_log(self, experiment: dict[str, Any]) -> str:
        return self.runtime_for(str(experiment.get("server_id") or "default"))._read_log(experiment)


manager = RuntimeManager(store)


def _profile_from_payload(payload: dict[str, Any]) -> dict[str, Any]:
    return {
        "host": str(payload.get("host", "")).strip(),
        "port": safe_int(payload.get("port"), 22),
        "username": str(payload.get("username", "")).strip(),
        "home": str(payload.get("home", "")).strip(),
        "poll_interval": max(2, min(60, safe_int(payload.get("poll_interval"), 5))),
    }


def _form_level(value: str) -> str:
    return value if value in ("idle_only", "emergency") else "idle_only"


@app.route("/", methods=["GET"])
def index():
    return render_template("index.html")


@app.route("/api/state", methods=["GET"])
def api_state():
    return jsonify(manager.public_state())


@app.route("/api/connect", methods=["POST"])
def api_connect():
    payload = request.get_json(silent=True) or {}
    server_id = str(payload.get("server_id") or manager.active_server_id or "default")
    profile = _profile_from_payload(payload)
    try:
        return jsonify(manager.connect(server_id, profile, str(payload.get("password", "")), bool(payload.get("save_password"))))
    except Exception as exc:
        return jsonify({"error": str(exc)}), 400


@app.route("/api/disconnect", methods=["POST"])
def api_disconnect():
    payload = request.get_json(silent=True) or {}
    manager.disconnect(str(payload.get("server_id") or "") or None)
    return jsonify(manager.public_state())


@app.route("/api/poll", methods=["POST"])
def api_poll():
    payload = request.get_json(silent=True) or {}
    server_id = str(payload.get("server_id") or "") or manager.active_server_id
    try:
        manager.runtime_for(server_id).tick()
        return jsonify(manager.public_state())
    except Exception as exc:
        return jsonify({"error": str(exc)}), 400


@app.route("/api/servers", methods=["POST"])
def api_upsert_server():
    payload = request.get_json(silent=True) or {}
    profile = _profile_from_payload(payload)
    name = str(payload.get("name") or "").strip()
    server_id = str(payload.get("id") or "").strip() or None
    try:
        new_id = manager.upsert_server(server_id, name, profile, bool(payload.get("auto_connect")))
    except Exception as exc:
        return jsonify({"error": str(exc)}), 400
    password = str(payload.get("password") or "")
    if payload.get("connect_now") and (password or manager.runtime_for(new_id).saved_password(profile)):
        try:
            manager.connect(new_id, profile, password, bool(payload.get("save_password")))
        except Exception as exc:
            state = manager.public_state()
            state["warning"] = f"服务器已保存，但连接失败：{exc}"
            return jsonify(state)
    return jsonify(manager.public_state())


@app.route("/api/servers/<server_id>/delete", methods=["POST"])
def api_delete_server(server_id: str):
    try:
        manager.remove_server(server_id)
        return jsonify(manager.public_state())
    except Exception as exc:
        return jsonify({"error": str(exc)}), 400


@app.route("/api/servers/<server_id>/enabled", methods=["POST"])
def api_server_enabled(server_id: str):
    payload = request.get_json(silent=True) or {}
    try:
        manager.set_enabled(server_id, bool(payload.get("enabled", True)))
        return jsonify(manager.public_state())
    except Exception as exc:
        return jsonify({"error": str(exc)}), 400


@app.route("/api/servers/<server_id>/active", methods=["POST"])
def api_server_active(server_id: str):
    try:
        manager.set_active(server_id)
        return jsonify(manager.public_state())
    except Exception as exc:
        return jsonify({"error": str(exc)}), 400


@app.route("/api/servers/<server_id>/scheduler", methods=["POST"])
def api_server_scheduler(server_id: str):
    payload = request.get_json(silent=True) or {}
    try:
        manager.set_scheduler_paused(server_id, bool(payload.get("paused", False)))
        return jsonify(manager.public_state())
    except Exception as exc:
        return jsonify({"error": str(exc)}), 400


@app.route("/api/experiments", methods=["POST"])
def api_create_experiment():
    upload = request.files.get("script")
    if upload is None or not upload.filename:
        return jsonify({"error": "请选择一个 .sh 文件"}), 400
    if not upload.filename.lower().endswith(".sh"):
        return jsonify({"error": "当前只接受 .sh 文件"}), 400
    temp_path = UPLOAD_DIR / f"upload-{uuid.uuid4().hex[:8]}.sh"
    upload.save(temp_path)
    prefs = manager.runtime_for(str(request.form.get("server_id") or "") or None)._preferences_data()
    try:
        manager.add_experiment(
            server_id=str(request.form.get("server_id") or "") or manager.active_server_id,
            name=clean_name(request.form.get("name", ""), Path(upload.filename).stem),
            script_src=temp_path,
            script_name=upload.filename,
            workdir=str(request.form.get("workdir", "")).strip() or str(prefs.get("workdir", "")).strip(),
            env=str(request.form.get("conda_env", "")).strip() or str(prefs.get("conda_env", "")).strip(),
            priority=safe_int(request.form.get("priority"), 50),
            level=_form_level(str(request.form.get("execution_level", "idle_only"))),
            peak=safe_int(request.form.get("peak_memory_mb"), safe_int(prefs.get("peak_memory_mb"), 0)),
            auto_retry=request.form.get("auto_retry_oom") == "true",
            depends_on=request.form.getlist("depends_on"),
        )
    except Exception as exc:
        try:
            temp_path.unlink(missing_ok=True)
        except OSError:
            pass
        return jsonify({"error": str(exc)}), 400
    return jsonify(manager.public_state()), 201


@app.route("/api/experiments/<exp_id>/update", methods=["POST"])
def api_update_experiment(exp_id: str):
    upload = request.files.get("script")
    temp_path: Path | None = None
    if upload is not None and upload.filename:
        if not upload.filename.lower().endswith(".sh"):
            return jsonify({"error": "当前只接受 .sh 文件"}), 400
        temp_path = UPLOAD_DIR / f"upload-{uuid.uuid4().hex[:8]}.sh"
        upload.save(temp_path)
    stored = manager._experiment_record(exp_id)
    if stored is None:
        if temp_path is not None:
            temp_path.unlink(missing_ok=True)
        return jsonify({"error": "实验不存在"}), 404
    prefs = manager.runtime_for(str(stored.get("server_id") or "default"))._preferences_data()
    try:
        manager.update_experiment(
            exp_id=exp_id,
            name=clean_name(request.form.get("name", ""), stored.get("name") or "experiment"),
            script_src=temp_path,
            script_name=upload.filename if temp_path is not None else None,
            workdir=str(request.form.get("workdir", "")).strip() or str(prefs.get("workdir", "")).strip(),
            env=str(request.form.get("conda_env", "")).strip() or str(prefs.get("conda_env", "")).strip(),
            priority=safe_int(request.form.get("priority"), safe_int(stored.get("priority"), 50)),
            level=_form_level(str(request.form.get("execution_level", stored.get("execution_level", "idle_only")))),
            peak=safe_int(request.form.get("peak_memory_mb"), safe_int(stored.get("peak_memory_mb"), 0)),
            auto_retry=request.form.get("auto_retry_oom") == "true",
            depends_on=request.form.getlist("depends_on"),
        )
    except Exception as exc:
        if temp_path is not None:
            try:
                temp_path.unlink(missing_ok=True)
            except OSError:
                pass
        return jsonify({"error": str(exc)}), 400
    return jsonify(manager.public_state())


@app.route("/api/experiments/<exp_id>/pause", methods=["POST"])
def api_pause_experiment(exp_id: str):
    try:
        manager.set_experiment_paused(exp_id, True)
        return jsonify(manager.public_state())
    except Exception as exc:
        return jsonify({"error": str(exc)}), 400


@app.route("/api/experiments/<exp_id>/resume", methods=["POST"])
def api_resume_experiment(exp_id: str):
    try:
        manager.set_experiment_paused(exp_id, False)
        return jsonify(manager.public_state())
    except Exception as exc:
        return jsonify({"error": str(exc)}), 400


@app.route("/api/experiments/<exp_id>/requeue", methods=["POST"])
def api_requeue_experiment(exp_id: str):
    try:
        manager.requeue_experiment(exp_id)
        return jsonify(manager.public_state())
    except Exception as exc:
        return jsonify({"error": str(exc)}), 400


@app.route("/api/experiments/<exp_id>/retry", methods=["POST"])
def api_retry(exp_id: str):
    try:
        manager.retry_experiment(exp_id)
        return jsonify(manager.public_state())
    except Exception as exc:
        return jsonify({"error": str(exc)}), 400


@app.route("/api/experiments/<exp_id>/cancel", methods=["POST"])
def api_cancel(exp_id: str):
    try:
        manager.cancel_experiment(exp_id)
        return jsonify(manager.public_state())
    except Exception as exc:
        return jsonify({"error": str(exc)}), 400


@app.route("/api/experiments/<exp_id>/delete", methods=["POST"])
def api_delete_experiment(exp_id: str):
    try:
        manager.delete_experiment(exp_id)
        return jsonify(manager.public_state())
    except Exception as exc:
        return jsonify({"error": str(exc)}), 400


@app.route("/api/experiments/pause_all", methods=["POST"])
def api_pause_all():
    try:
        manager.pause_all_experiments()
        return jsonify(manager.public_state())
    except Exception as exc:
        return jsonify({"error": str(exc)}), 400


@app.route("/api/experiments/resume_all", methods=["POST"])
def api_resume_all():
    try:
        manager.resume_all_experiments()
        return jsonify(manager.public_state())
    except Exception as exc:
        return jsonify({"error": str(exc)}), 400


@app.route("/api/experiments/<exp_id>/log", methods=["GET"])
def api_log(exp_id: str):
    item = manager._experiment_record(exp_id)
    if item is None:
        return jsonify({"error": "实验不存在"}), 404
    if not manager.runtime_for(str(item.get("server_id") or "default")).connected:
        return jsonify({"log": "当前未连接服务器", "path": item.get("log_path", "")})
    try:
        return jsonify({"log": manager.read_log(item), "path": item.get("log_path", "")})
    except Exception as exc:
        return jsonify({"error": str(exc)}), 400


@app.route("/api/benchmark", methods=["POST"])
def api_benchmark():
    payload = request.get_json(silent=True) or {}
    server_id = str(payload.get("server_id") or "") or manager.active_server_id
    try:
        result = manager.run_benchmark(
            server_id,
            safe_int(payload.get("gpu_index"), 0),
            seconds=safe_int(payload.get("seconds"), 5),
            conda_env=str(payload.get("conda_env", "")).strip(),
        )
        return jsonify({"result": result, "state": manager.public_state()})
    except Exception as exc:
        return jsonify({"error": str(exc)}), 400


@app.route("/api/preferences", methods=["POST"])
def api_preferences():
    payload = request.get_json(silent=True) or {}
    server_id = str(payload.get("server_id") or "") or manager.active_server_id
    with manager.store.lock:
        prefs = manager.runtime_for(server_id)._preferences_data()
        if "workdir" in payload:
            prefs["workdir"] = str(payload.get("workdir") or "")
        if "conda_env" in payload:
            prefs["conda_env"] = str(payload.get("conda_env") or "")
        if "peak_memory_mb" in payload:
            prefs["peak_memory_mb"] = max(0, safe_int(payload.get("peak_memory_mb"), 0))
        if "execution_level" in payload and payload.get("execution_level") in ("idle_only", "emergency"):
            prefs["execution_level"] = payload.get("execution_level")
        store.save()
    return jsonify(manager.public_state())


def _auto_connect_worker() -> None:
    time.sleep(0.8)
    try:
        manager.auto_connect()
    except Exception:
        pass


if __name__ == "__main__":
    threading.Thread(target=_auto_connect_worker, name="auto-connect", daemon=True).start()
    app.run(host="127.0.0.1", port=8765, debug=False, threaded=True)


