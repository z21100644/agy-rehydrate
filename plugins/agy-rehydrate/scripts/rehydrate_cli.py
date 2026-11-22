#!/usr/bin/env python3
"""Read-only diagnostic CLI (`/rehydrate`) for `agy-rehydrate`."""

from __future__ import annotations

import argparse
from datetime import datetime
import json
import os
from pathlib import Path
import sys
from typing import Any

sys.dont_write_bytecode = True

from rehydrate_after_compact import scan_compact_tail
from rehydrate_core import (
    AGY_PRODUCT_DIRS,
    build_rehydration,
    canonical_path_key,
    infer_cid_from_transcript_path,
    load_rehydrate_config,
    normalize_local_path,
    read_first_step_info,
    resolve_fork_ancestors,
)


def discover_recent_transcripts(limit: int = 12) -> list[tuple[float, str, str, Path, Path]]:
    """Return `(mtime, product, cid, compact_path, full_path)` across App, CLI, and IDE sorted newest-first."""
    if limit <= 0:
        return []
    gemini_root = Path.home() / ".gemini"
    found: list[tuple[float, str, str, Path, Path]] = []
    for prod in AGY_PRODUCT_DIRS:
        brain = gemini_root / prod / "brain"
        if not brain.is_dir():
            continue
        try:
            entries = list(brain.iterdir())
        except OSError:
            continue
        for cdir in entries:
            if not cdir.is_dir():
                continue
            logs_dir = cdir / ".system_generated" / "logs"
            tpath = logs_dir / "transcript.jsonl"
            fpath = logs_dir / "transcript_full.jsonl"
            if not tpath.is_file() and not fpath.is_file():
                continue
            compact = tpath if tpath.is_file() else fpath
            full = fpath if fpath.is_file() else tpath
            try:
                mtime = full.stat().st_mtime
            except OSError:
                continue
            found.append((mtime, prod, cdir.name, compact, full))
    found.sort(key=lambda item: item[0], reverse=True)
    return found[:limit]


def resolve_target_transcript(target: str) -> tuple[str, str, Path, Path] | None:
    """Resolve `latest`, conversation UUID (or prefix), or explicit JSONL path."""
    if not isinstance(target, str):
        return None
    raw = target.strip()
    if not raw:
        return None

    needle = raw.lower()
    p_cand: Path | None = None
    is_explicit_file = False
    if needle != "latest":
        try:
            p_cand = Path(raw).expanduser()
            is_explicit_file = p_cand.is_file()
        except OSError:
            p_cand = None
            is_explicit_file = False

    if is_explicit_file and p_cand is not None:
        logs_dir = p_cand.parent
        cid = infer_cid_from_transcript_path(p_cand)
        if p_cand.name == "transcript.jsonl":
            full_sib = logs_dir / "transcript_full.jsonl"
            return "path", cid, p_cand, (full_sib if full_sib.is_file() else p_cand)
        if p_cand.name == "transcript_full.jsonl":
            compact_sib = logs_dir / "transcript.jsonl"
            return "path", cid, (compact_sib if compact_sib.is_file() else p_cand), p_cand
        return "path", cid, p_cand, p_cand

    if (
        needle != "latest"
        and raw != "."
        and ".." not in raw
        and "/" not in raw
        and "\\" not in raw
        and ":" not in raw
        and "\x00" not in raw
    ):
        gemini_root = Path.home() / ".gemini"
        best_exact: tuple[float, str, str, Path, Path] | None = None
        for prod in AGY_PRODUCT_DIRS:
            cdir = gemini_root / prod / "brain" / raw
            logs_dir = cdir / ".system_generated" / "logs"
            tpath = logs_dir / "transcript.jsonl"
            fpath = logs_dir / "transcript_full.jsonl"
            has_t = tpath.is_file()
            has_f = fpath.is_file()
            if not has_t and not has_f:
                continue
            try:
                canon_cdir = cdir.resolve()
            except OSError:
                canon_cdir = cdir
            canon_logs = canon_cdir / ".system_generated" / "logs"
            compact = (canon_logs / "transcript.jsonl") if has_t else (canon_logs / "transcript_full.jsonl")
            full = (canon_logs / "transcript_full.jsonl") if has_f else (canon_logs / "transcript.jsonl")
            try:
                mtime = full.stat().st_mtime
            except OSError:
                continue
            if best_exact is None or mtime > best_exact[0]:
                best_exact = (mtime, prod, canon_cdir.name, compact, full)
        if best_exact is not None:
            _, prod, cid, compact, full = best_exact
            return prod, cid, compact, full

    recent = discover_recent_transcripts(limit=200)
    if not recent:
        return None
    if needle == "latest":
        for _, prod, cid, compact, full in recent:
            if read_first_step_info(full) is not None:
                return prod, cid, compact, full
        _, prod, cid, compact, full = recent[0]
        return prod, cid, compact, full

    for _, prod, cid, compact, full in recent:
        if cid.lower() == needle:
            return prod, cid, compact, full
    for _, prod, cid, compact, full in recent:
        if cid.lower().startswith(needle):
            return prod, cid, compact, full
    return None


def _normalize_cli_cwd(raw_c: str) -> Path | None:
    val = raw_c.strip()
    if not val:
        return None
    if val[:7].lower() == "file://" or val.startswith("\\\\") or val.startswith("//"):
        norm = normalize_local_path(val)
    else:
        expanded = os.path.expanduser(val)
        if not os.path.isabs(expanded):
            expanded = os.path.abspath(expanded)
        norm = normalize_local_path(expanded)
    if not norm:
        return None
    try:
        return Path(norm).resolve()
    except OSError:
        return None


def _format_cp_state(last_cp: Any, last_status: str | None) -> str:
    if last_cp is None:
        return "rehydrated=None"
    if last_status == "stale_skipped":
        return f"skipped(stale)={last_cp}"
    if last_status == "disabled":
        return f"disabled={last_cp}"
    if last_status in (None, "injected", "already_rehydrated"):
        return f"rehydrated={last_cp}"
    return f"skipped({last_status})={last_cp}"


def run_cli(argv: list[str]) -> int:
    """Read-only diagnostic CLI for `/rehydrate` (`status`, `inspect`, `preview`, `show-config`)."""
    parser = argparse.ArgumentParser(
        description="Inspect AGY Post-Compact Rehydration state and previews.",
    )
    parser.add_argument(
        "--cwd",
        dest="root_cwd",
        action="append",
        default=[],
        help="Workspace root directory (repeatable for multi-root workspaces)",
    )
    sub_common = argparse.ArgumentParser(add_help=False)
    sub_common.add_argument(
        "--cwd",
        dest="sub_cwd",
        action="append",
        default=[],
        help="Workspace root directory (repeatable for multi-root workspaces)",
    )
    sub = parser.add_subparsers(dest="subcmd", required=True)

    p_status = sub.add_parser(
        "status", parents=[sub_common], help="List recent App/CLI/IDE sessions and checkpoint/fork state"
    )
    p_status.add_argument("--limit", type=int, default=8, help="Max recent sessions to display (default: 8)")

    p_inspect = sub.add_parser(
        "inspect", parents=[sub_common], help="Dry-run rehydration stats and restored file list for a session"
    )
    p_inspect.add_argument("target", nargs="?", default="latest", help="'latest', conversation ID/prefix, or path")
    p_inspect.add_argument("--checkpoint", type=int, default=None, help="Inspect a specific checkpoint step index")

    p_preview = sub.add_parser(
        "preview", parents=[sub_common], help="Preview rendered rehydration sections for a session"
    )
    p_preview.add_argument("target", nargs="?", default="latest", help="'latest', conversation ID/prefix, or path")
    p_preview.add_argument("--checkpoint", type=int, default=None, help="Preview a specific checkpoint step index")
    p_preview.add_argument("--full", action="store_true", help="Print full rehydrated markdown text")

    sub.add_parser("show-config", parents=[sub_common], help="Display effective budgets merged with .agents/rehydrate.json")

    args = parser.parse_args(argv)
    raw_cwds: list[str] = list(args.root_cwd) + list(args.sub_cwd)
    if not raw_cwds:
        raw_cwds = [os.getcwd()]
    workspaces: list[Path] = []
    seen_ws: set[str] = set()
    for raw_c in raw_cwds:
        resolved_ws = _normalize_cli_cwd(raw_c)
        if resolved_ws is None:
            print(f"Error: invalid --cwd path: {raw_c!r}")
            return 1
        wkey = canonical_path_key(str(resolved_ws))
        if wkey not in seen_ws:
            seen_ws.add(wkey)
            workspaces.append(resolved_ws)
    cfg = load_rehydrate_config(workspaces)

    if args.subcmd == "show-config":
        out = {
            "workspace": str(workspaces[0]),
            "workspaces": [str(w) for w in workspaces],
            **cfg.to_dict(),
        }
        print(json.dumps(out, indent=2, ensure_ascii=False))
        return 0

    if args.subcmd == "status":
        if args.limit < 1:
            print(f"Error: --limit must be >= 1 (got {args.limit}).")
            return 1
        rows = discover_recent_transcripts(limit=args.limit)
        if not rows:
            print("No Antigravity transcripts found under ~/.gemini/{antigravity,antigravity-cli,antigravity-ide}/brain.")
            return 0
        for mtime, prod, cid, compact, full in rows:
            ts_str = datetime.fromtimestamp(mtime).strftime("%Y-%m-%d %H:%M:%S")
            try:
                cp, max_step, _ = scan_compact_tail(compact, 0, None)
            except OSError:
                print(f"[{ts_str}] {prod:<15} {cid}  [sandbox-restricted; run outside sandbox to inspect]")
                continue
            first_info = read_first_step_info(full)
            first_step = first_info[0] if first_info else 0
            state_file = full.parent / "rehydrate_state.json"
            last_rehydrated = None
            last_status: str | None = None
            if state_file.is_file():
                try:
                    st_obj = json.loads(state_file.read_text(encoding="utf-8-sig"))
                    if isinstance(st_obj, dict):
                        last_rehydrated = st_obj.get("last_rehydrated_checkpoint")
                        raw_st = st_obj.get("last_status")
                        if isinstance(raw_st, str) and raw_st:
                            last_status = raw_st
                except (OSError, ValueError):
                    pass
            steps_flag = "steps=empty" if max_step < 0 else f"steps={first_step}..{max_step}"
            fork_flag = f"fork@{first_step}" if first_step > 0 else "root"
            cp_flag = f"cp={cp} ({_format_cp_state(last_rehydrated, last_status)})" if cp is not None else "no-cp"
            print(f"[{ts_str}] {prod:<15} {cid}  {steps_flag}  {fork_flag}  {cp_flag}")
        return 0

    explicit_cp = getattr(args, "checkpoint", None)
    if explicit_cp is not None and explicit_cp < 0:
        print(f"Error: --checkpoint must be >= 0 (got {explicit_cp}).")
        return 1

    resolved = resolve_target_transcript(args.target)
    if resolved is None:
        print(f"Conversation transcript not found for target: {args.target}")
        return 1
    prod, cid, _, full_path = resolved
    cli_status: dict[str, Any] = {}
    try:
        res = build_rehydration(
            full_path,
            up_to_checkpoint_step=explicit_cp if explicit_cp is not None else 999_999_999,
            ancestors=None,
            config=cfg,
            workspace_roots=workspaces,
            simulate_if_no_checkpoint=(explicit_cp is None),
            out_status=cli_status,
        )
    except OSError as exc:
        print(f"Cannot read transcript {full_path} ({exc}); rerun outside sandbox if inspecting another session.")
        return 1
    if res is None:
        if explicit_cp is not None:
            reason = cli_status.get("reason")
            if reason == "empty":
                print(f"Error: checkpoint <= {explicit_cp} has no rehydratable content in {full_path}.")
            else:
                print(f"Error: no checkpoint found at or before step {explicit_cp} in {full_path}.")
            return 1
        print(f"No steps available to inspect in {full_path}")
        return 0

    full_msg, stats = res
    ancestors = stats.get("fork_ancestors")
    if ancestors is None:
        ancestors = resolve_fork_ancestors(full_path)
    summary = {k: v for k, v in stats.items() if k != "chunks"}
    summary["product"] = prod
    summary["conversation_id"] = cid
    summary["transcript_full"] = str(full_path)
    summary["fork_ancestors"] = ancestors

    actual_cp = stats.get("checkpoint")
    cp_mismatch_note: str | None = None
    if explicit_cp is not None and actual_cp != explicit_cp:
        cp_mismatch_note = (
            f"No CHECKPOINT at exact step {explicit_cp}; "
            f"inspected latest checkpoint <= {explicit_cp} (step {actual_cp})."
        )
        summary["requested_checkpoint"] = explicit_cp
        summary["note"] = cp_mismatch_note

    if args.subcmd == "inspect":
        print(json.dumps(summary, indent=2, ensure_ascii=False))
        return 0

    if cp_mismatch_note:
        print(f"Note: {cp_mismatch_note}\n")
    if args.full:
        print(full_msg)
    else:
        lines = full_msg.splitlines()
        print("\n".join(lines[:45]))
        if len(lines) > 45:
            print(f"\n... [{len(lines) - 45} more lines; pass --full to print complete payload] ...")
    return 0
