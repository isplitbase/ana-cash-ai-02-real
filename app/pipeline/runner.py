from __future__ import annotations
import json
import os
import subprocess
import tempfile
from pathlib import Path
from typing import Any, Dict

PROJECT_ROOT = Path(__file__).resolve().parents[2]  # ~/cash-ai-01
ORIGINALS_DIR = PROJECT_ROOT / "app" / "pipeline" / "originals"

def _run(cmd: list[str], cwd: Path, env: Dict[str, str]) -> None:
    p = subprocess.run(
        cmd,
        cwd=str(cwd),
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )
    if p.returncode != 0:
        raise RuntimeError(f"Command failed: {' '.join(cmd)}\n--- output ---\n{p.stdout}")

def run_001_002_003(payload: Dict[str, Any]) -> Dict[str, Any]:
    data_json = {
        "BS": payload.get("BS", []),
        "PL": payload.get("PL", []),
        "販売費": payload.get("SGA", []),
        "製造原価": payload.get("MFG", []),
    }

    run_dir = Path(tempfile.mkdtemp(prefix="cashai_", dir="/tmp"))
    (run_dir / "data.json").write_text(json.dumps(data_json, ensure_ascii=False), encoding="utf-8")

    api_key = os.environ.get("OPENAI_API_KEY", "")
    env = dict(os.environ)
    if api_key:
        env["OPENAI_API_KEY2"] = api_key

    # ★ここが重要：cash-ai-01 直下をPYTHONPATHに入れる（google/colabスタブを拾える）
    env["PYTHONPATH"] = str(PROJECT_ROOT)

    _run(["python3", str(ORIGINALS_DIR / "cloab001.py")], cwd=run_dir, env=env)
    _run(["python3", str(ORIGINALS_DIR / "cloab002.py")], cwd=run_dir, env=env)
    _run(["python3", str(ORIGINALS_DIR / "cloab003.py")], cwd=run_dir, env=env)

    out_path = run_dir / "output_updated.json"
    if not out_path.exists():
        out_path = run_dir / "output.json"
    if not out_path.exists():
        raise RuntimeError("output_updated.json / output.json が生成されませんでした。")

    return json.loads(out_path.read_text(encoding="utf-8"))
