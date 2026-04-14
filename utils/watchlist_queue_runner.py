#!/usr/bin/env python3
"""
自选股历史批跑队列。

用途：
1. 等待当前 scrape 任务结束
2. 从自选股 CSV 的指定起始位置开始，按批次顺序执行 history 50 页
3. 为每个批次写独立日志，便于隔夜查看
"""

from __future__ import annotations

import argparse
import csv
import json
import os
import subprocess
import sys
import time
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path
from typing import Iterable


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_CSV = Path("/Users/zhangyang/Desktop/自选股.csv")
LOG_DIR = ROOT / "data" / "overnight_runs"


@dataclass
class StockItem:
    index: int
    name: str
    code: str


def now_str() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def load_watchlist(csv_path: Path) -> list[StockItem]:
    items: list[StockItem] = []
    with csv_path.open("r", encoding="utf-16", newline="") as f:
        reader = csv.DictReader(f, delimiter="\t")
        for idx, row in enumerate(reader, start=1):
            name = (row.get("名称") or row.get("股票名称") or row.get("name") or "").strip()
            code = (row.get("代码") or row.get("股票代码") or row.get("symbol") or "").strip().lstrip("'")
            if not name:
                continue
            items.append(StockItem(index=idx, name=name, code=code))
    return items


def batched(items: list[StockItem], size: int) -> Iterable[list[StockItem]]:
    for i in range(0, len(items), size):
        yield items[i : i + size]


def load_failed_batches(manifest_path: Path) -> list[list[StockItem]]:
    data = json.loads(manifest_path.read_text(encoding="utf-8"))
    failed_batches: list[list[StockItem]] = []
    for batch in data.get("batches", []):
        if batch.get("returncode") != -15:
            continue
        items = [
            StockItem(
                index=int(item["index"]),
                name=item["name"],
                code=item["code"],
            )
            for item in batch.get("stocks", [])
        ]
        if items:
            failed_batches.append(items)
    return failed_batches


def active_scrape_processes() -> list[str]:
    proc = subprocess.run(
        ["ps", "ax", "-o", "pid=,command="],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=True,
    )
    lines: list[str] = []
    me = str(os.getpid())
    for raw in proc.stdout.splitlines():
        line = raw.strip()
        if not line or "main.py scrape" not in line:
            continue
        pid = line.split(None, 1)[0]
        if pid == me:
            continue
        lines.append(line)
    return lines


def wait_for_idle(poll_seconds: int) -> None:
    while True:
        active = active_scrape_processes()
        if not active:
            return
        print(f"[{now_str()}] 等待当前 scrape 任务结束，共 {len(active)} 个进程仍在运行")
        for line in active[:3]:
            print(f"  - {line}")
        sys.stdout.flush()
        time.sleep(poll_seconds)


def run_batch(batch_no: int, batch: list[StockItem], pages: int, workers: int) -> dict:
    codes = [item.code or item.name for item in batch]
    names = [item.name for item in batch]
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    log_path = LOG_DIR / f"batch_{batch_no:02d}_{stamp}.log"
    cmd = [
        str(ROOT / "venv" / "bin" / "python"),
        "main.py",
        "scrape",
        "--stocks",
        *codes,
        "--mode",
        "history",
        "--pages",
        str(pages),
        "--workers",
        str(workers),
        "--yes",
        "--no-preflight",
    ]
    env = dict(os.environ)
    env["PYTHONUNBUFFERED"] = "1"
    rc = None

    with log_path.open("w", encoding="utf-8") as log:
        log.write(f"[{now_str()}] 批次开始: {', '.join(names)}\n")
        log.write(f"命令: {' '.join(cmd)}\n\n")
        log.flush()

        proc = subprocess.Popen(
            cmd,
            cwd=ROOT,
            stdout=log,
            stderr=subprocess.STDOUT,
            text=True,
            env=env,
        )
        rc = proc.wait()

        log.write(f"\n[{now_str()}] 批次结束: returncode={rc}\n")
    return {
        "batch_no": batch_no,
        "started_at": stamp,
        "finished_at": now_str(),
        "returncode": rc,
        "log_path": str(log_path),
        "stocks": [asdict(item) for item in batch],
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="顺序跑自选股历史批次")
    parser.add_argument("--csv", default=str(DEFAULT_CSV))
    parser.add_argument("--start-index", type=int, default=21)
    parser.add_argument("--batch-size", type=int, default=10)
    parser.add_argument("--pages", type=int, default=50)
    parser.add_argument("--workers", type=int, default=1)
    parser.add_argument("--wait-poll-seconds", type=int, default=60)
    parser.add_argument("--rerun-failed-manifest", default="")
    args = parser.parse_args()

    LOG_DIR.mkdir(parents=True, exist_ok=True)
    manifest_path = LOG_DIR / "queue_manifest.json"

    rerun_manifest = Path(args.rerun_failed_manifest) if args.rerun_failed_manifest else None
    if rerun_manifest:
        failed_batches = load_failed_batches(rerun_manifest)
        pending_batches = failed_batches
        pending_count = sum(len(batch) for batch in failed_batches)
    else:
        items = load_watchlist(Path(args.csv))
        pending = [item for item in items if item.index >= args.start_index]
        pending_batches = list(batched(pending, args.batch_size))
        pending_count = len(pending)

    manifest = {
        "started_at": now_str(),
        "csv": str(args.csv),
        "start_index": args.start_index,
        "batch_size": args.batch_size,
        "pages": args.pages,
        "workers": args.workers,
        "pending_count": pending_count,
        "rerun_failed_manifest": str(rerun_manifest) if rerun_manifest else "",
        "batches": [],
    }
    manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")

    if rerun_manifest:
        print(f"[{now_str()}] 失败批次重跑已启动，待处理股票 {pending_count} 只，共 {len(pending_batches)} 批")
    else:
        print(f"[{now_str()}] 队列已启动，待处理股票 {pending_count} 只")
    wait_for_idle(args.wait_poll_seconds)

    for batch_no, batch in enumerate(pending_batches, start=1):
        result = run_batch(batch_no, batch, args.pages, args.workers)
        manifest["batches"].append(result)
        manifest["finished_at"] = now_str()
        manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"[{now_str()}] 批次 {batch_no} 完成，returncode={result['returncode']}")
        sys.stdout.flush()

    manifest["finished_at"] = now_str()
    manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"[{now_str()}] 全部批次执行完毕")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
