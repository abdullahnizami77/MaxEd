"""Run manifests: the attributability record (plan section 11).

Every pass writes runs/<run_id>/manifest.json. The before/after comparison
is attributable because the only difference between the pass1 and pass2
manifests is the memory state hash: a two-line diff answers "what exactly
changed between the runs."
"""

from __future__ import annotations

import hashlib
import json
import subprocess
import uuid
from pathlib import Path

from balancecheck.config import REPO_ROOT, Config


def new_run_id(pass_label: str) -> str:
    return f"{pass_label}-{uuid.uuid4().hex[:8]}"


def git_sha() -> str:
    try:
        out = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=REPO_ROOT,
            capture_output=True,
            text=True,
            timeout=10,
        )
        return out.stdout.strip()[:12] if out.returncode == 0 else "no-git"
    except Exception:
        return "no-git"


def prompt_hashes() -> dict[str, str]:
    prompts_dir = REPO_ROOT / "prompts"
    hashes: dict[str, str] = {}
    if prompts_dir.exists():
        for p in sorted(prompts_dir.glob("*.md")):
            hashes[p.name] = hashlib.sha256(p.read_bytes()).hexdigest()[:16]
    return hashes


def config_hash(cfg: Config) -> str:
    blob = json.dumps(
        {"mode": cfg.mode, "model": cfg.model, "seed": cfg.seed, "max_attempts": cfg.max_attempts},
        sort_keys=True,
    )
    return hashlib.sha256(blob.encode()).hexdigest()[:16]


def memory_state_hash(memory_file: Path) -> str:
    if not memory_file.exists() or memory_file.read_text().strip() in ("", "[]"):
        return "empty"
    return hashlib.sha256(memory_file.read_bytes()).hexdigest()[:16]


def write_manifest(
    run_id: str,
    pass_label: str,
    cfg: Config,
    *,
    pool: str,
    mem_hash: str,
    extra: dict | None = None,
) -> Path:
    run_dir = REPO_ROOT / "runs" / run_id
    run_dir.mkdir(parents=True, exist_ok=True)
    manifest = {
        "run_id": run_id,
        "pass_label": pass_label,
        "seed": cfg.seed,
        "git_sha": git_sha(),
        "model_name": cfg.model or "stub",
        "mode": cfg.mode,
        "prompt_hashes": prompt_hashes(),
        "config_hash": config_hash(cfg),
        "pool": pool,
        "memory_state_hash": mem_hash,
        **(extra or {}),
    }
    path = run_dir / "manifest.json"
    path.write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n")
    return path
