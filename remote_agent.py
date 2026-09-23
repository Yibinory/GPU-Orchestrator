#!/usr/bin/env python3
"""Small dependency-light probe and benchmark helper installed in ~/.gpu-orchestrator."""

from __future__ import annotations

import argparse
import csv
import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path


def command(args: list[str], timeout: int = 20) -> tuple[int, str, str]:
    try:
        proc = subprocess.run(args, capture_output=True, text=True, timeout=timeout)
        return proc.returncode, proc.stdout, proc.stderr
    except (FileNotFoundError, subprocess.TimeoutExpired) as exc:
        return 127, "", str(exc)


def number(value: str | None, default: float = 0) -> float:
    try:
        return float(str(value).strip().replace("[N/A]", "0"))
    except (TypeError, ValueError):
        return default


def process_runtime(pid: int) -> int:
    """Best-effort process age in seconds on Linux; zero when procfs is unavailable."""
    try:
        raw = Path(f"/proc/{pid}/stat").read_text(encoding="utf-8")
        closing_name = raw.rfind(")")
        fields = raw[closing_name + 2 :].split()
        # The sliced list starts at field 3 (state); starttime is field 22.
        start_ticks = int(fields[19])
        hz = os.sysconf(os.sysconf_names["SC_CLK_TCK"])
        uptime = float(Path("/proc/uptime").read_text(encoding="utf-8").split()[0])
        return max(0, int(uptime - start_ticks / hz))
    except (OSError, ValueError, IndexError, KeyError):
        return 0


def process_owner(pid: int) -> str:
    """Best-effort process owner name; empty string when procfs is unreadable."""
    try:
        for line in Path(f"/proc/{pid}/status").read_text(encoding="utf-8", errors="replace").splitlines():
            if line.startswith("Uid:"):
                uid = int(line.split()[1])
                try:
                    import pwd

                    return pwd.getpwuid(uid).pw_name
                except Exception:
                    return str(uid)
    except (OSError, ValueError, IndexError):
        pass
    return ""


def process_command(pid: int) -> str:
    """Best-effort full command line; empty string when procfs is unreadable."""
    try:
        raw = Path(f"/proc/{pid}/cmdline").read_bytes()
        text = raw.replace(b"\x00", b" ").decode("utf-8", errors="replace").strip()
        return text[:500]
    except OSError:
        return ""


def cpu_usage() -> float:
    def read() -> tuple[float, float]:
        fields = Path("/proc/stat").read_text(encoding="utf-8").splitlines()[0].split()
        values = [float(item) for item in fields[1:]]
        idle = values[3] + (values[4] if len(values) > 4 else 0)
        return sum(values), idle

    try:
        total_a, idle_a = read()
        time.sleep(0.12)
        total_b, idle_b = read()
        total_delta = total_b - total_a
        return round(max(0, min(100, (1 - (idle_b - idle_a) / max(total_delta, 1)) * 100)), 1)
    except Exception:
        return 0.0


def memory_stats() -> dict[str, int | float]:
    values: dict[str, int] = {}
    try:
        for line in Path("/proc/meminfo").read_text(encoding="utf-8").splitlines():
            key, raw = line.split(":", 1)
            values[key] = int(raw.strip().split()[0]) * 1024
    except Exception:
        return {"total_bytes": 0, "used_bytes": 0, "available_bytes": 0, "used_percent": 0}
    total = values.get("MemTotal", 0)
    available = values.get("MemAvailable", values.get("MemFree", 0))
    used = max(0, total - available)
    return {
        "total_bytes": total,
        "used_bytes": used,
        "available_bytes": available,
        "used_percent": round(used / total * 100, 1) if total else 0,
    }


def gpu_stats() -> list[dict[str, object]]:
    fields = [
        "index",
        "name",
        "uuid",
        "memory.total",
        "memory.used",
        "memory.free",
        "utilization.gpu",
        "utilization.memory",
        "temperature.gpu",
        "power.draw",
        "power.limit",
    ]
    code, stdout, _ = command(["nvidia-smi", f"--query-gpu={','.join(fields)}", "--format=csv,noheader,nounits"])
    if code != 0:
        return []
    rows = list(csv.reader(stdout.splitlines(), skipinitialspace=True))
    processes: dict[str, list[dict[str, object]]] = {}
    pcode, pstdout, _ = command(
        ["nvidia-smi", "--query-compute-apps=gpu_uuid,pid,process_name,used_memory", "--format=csv,noheader,nounits"]
    )
    if pcode == 0:
        for row in csv.reader(pstdout.splitlines(), skipinitialspace=True):
            if len(row) < 4:
                continue
            pid = int(number(row[1], 0))
            processes.setdefault(row[0].strip(), []).append(
                {
                    "pid": pid,
                    "name": row[2].strip(),
                    "memory_mb": int(number(row[3], 0)),
                    "runtime_seconds": process_runtime(pid),
                    "user": process_owner(pid),
                    "command": process_command(pid),
                }
            )
    result = []
    for row in rows:
        if len(row) < len(fields):
            continue
        item = {
            "index": int(number(row[0], len(result))),
            "name": row[1].strip(),
            "uuid": row[2].strip(),
            "memory_total_mb": int(number(row[3])),
            "memory_used_mb": int(number(row[4])),
            "memory_free_mb": int(number(row[5])),
            "utilization_gpu": round(number(row[6]), 1),
            "utilization_memory": round(number(row[7]), 1),
            "temperature_c": round(number(row[8]), 1),
            "power_draw_w": round(number(row[9]), 1),
            "power_limit_w": round(number(row[10]), 1),
        }
        item["processes"] = processes.get(item["uuid"], [])
        item["process_count"] = len(item["processes"])
        item["is_idle"] = bool(
            item["process_count"] == 0
            and item["utilization_gpu"] <= 10
            and item["memory_used_mb"] <= max(1024, item["memory_total_mb"] * 0.05)
        )
        name = str(item["name"]).lower()
        hints = (("h100", 10), ("a100", 8), ("h800", 8), ("a800", 7), ("4090", 6), ("l40", 5), ("3090", 4), ("v100", 3), ("t4", 2))
        item["performance_hint"] = next((score for token, score in hints if token in name), 1)
        result.append(item)
    return result


def status() -> dict[str, object]:
    disk_path = os.path.expanduser("~")
    try:
        disk = shutil.disk_usage(disk_path)
    except OSError:
        try:
            disk = shutil.disk_usage(os.getcwd())
        except OSError:
            disk = (0, 0, 0)
    memory = memory_stats()
    load = os.getloadavg() if hasattr(os, "getloadavg") else (0, 0, 0)
    return {
        "cpu_percent": cpu_usage(),
        "load_average": [round(item, 2) for item in load],
        "memory": memory,
        "disk": {
            "path": disk_path,
            "total_bytes": disk.total,
            "used_bytes": disk.used,
            "free_bytes": disk.free,
            "used_percent": round(disk.used / disk.total * 100, 1) if disk.total else 0,
        },
        "gpus": gpu_stats(),
        "hostname": os.uname().nodename if hasattr(os, "uname") else "",
        "python": sys.version.split()[0],
        "timestamp": time.time(),
    }


def benchmark(gpu_index: int, seconds: int) -> dict[str, object]:
    try:
        import torch
    except Exception:
        return {
            "ok": False,
            "error": "当前 Python/Conda 环境没有安装 PyTorch，无法执行 Tensor 测试",
            "backend": "none",
        }
    if not torch.cuda.is_available():
        return {"ok": False, "error": "PyTorch 未检测到 CUDA", "backend": "torch"}
    try:
        device = torch.device(f"cuda:{gpu_index}")
        torch.cuda.set_device(device)
        props = torch.cuda.get_device_properties(device)
        free_bytes, total_bytes = torch.cuda.mem_get_info(device)
        size = 4096 if free_bytes > 3 * 1024**3 else 2048

        def measure(dtype: object, budget: float, matrix_size: int) -> tuple[float, int, float]:
            a = torch.randn((matrix_size, matrix_size), device=device, dtype=dtype)
            b = torch.randn((matrix_size, matrix_size), device=device, dtype=dtype)
            for _ in range(4):
                _ = a @ b
            torch.cuda.synchronize(device)
            started = time.perf_counter()
            count = 0
            while time.perf_counter() - started < budget:
                _ = a @ b
                count += 1
            torch.cuda.synchronize(device)
            elapsed = max(time.perf_counter() - started, 1e-6)
            del a, b
            return 2 * matrix_size**3 * count / elapsed / 1e12, count, elapsed

        try:
            tensor_tflops, tensor_iters, tensor_seconds = measure(torch.float16, max(1.0, seconds * 0.6), size)
        except RuntimeError as exc:
            if "out of memory" not in str(exc).lower() or size <= 1024:
                raise
            torch.cuda.empty_cache()
            size //= 2
            tensor_tflops, tensor_iters, tensor_seconds = measure(torch.float16, max(1.0, seconds * 0.6), size)
        torch.cuda.empty_cache()
        fp32_tflops, fp32_iters, fp32_seconds = measure(torch.float32, max(1.0, seconds * 0.4), min(size, 2048))
        return {
            "ok": True,
            "backend": "pytorch",
            "gpu_name": props.name,
            "gpu_index": gpu_index,
            "memory_total_mb": round(total_bytes / 1024**2),
            "tensor_tflops": round(tensor_tflops, 3),
            "fp32_tflops": round(fp32_tflops, 3),
            "tensor_iterations": tensor_iters,
            "fp32_iterations": fp32_iters,
            "matrix_size": size,
            "tensor_seconds": round(tensor_seconds, 2),
            "fp32_seconds": round(fp32_seconds, 2),
        }
    except Exception as exc:
        try:
            torch.cuda.empty_cache()
        except Exception:
            pass
        return {"ok": False, "backend": "pytorch", "error": str(exc), "gpu_index": gpu_index}


def main() -> None:
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("status")
    bench = sub.add_parser("benchmark")
    bench.add_argument("--gpu-index", type=int, default=0)
    bench.add_argument("--seconds", type=int, default=5)
    args = parser.parse_args()
    if args.command == "status":
        print(json.dumps(status(), ensure_ascii=False))
    elif args.command == "benchmark":
        print(json.dumps(benchmark(args.gpu_index, args.seconds), ensure_ascii=False))


if __name__ == "__main__":
    main()

