#!/usr/bin/env python3
"""Read-only diagnostic CLI (`/rehydrate`) for `agy-rehydrate`."""

from __future__ import annotations

import argparse
import json
import os
import sys

TYPE_CHECKING = False
if TYPE_CHECKING:
    from pathlib import Path

sys.dont_write_bytecode = True

from rehydrate_engine import (
    build_rehydration,
    infer_cid_from_transcript_path,
    read_first_step_info,
    resolve_fork_ancestors,
    scan_compact_tail,
)
from rehydrate_helpers import (
    AGY_PRODUCT_DIRS,
    _datetime_cls,
    _path_cls,
    canonical_path_key,
    load_rehydrate_config,
)


def discover_recent_transcripts(limit: int = 12) -> list[tuple[float, str, str, Path, Path]]:
    """Return `(mtime, product, cid, compact_path, full_path)` across App, CLI, and IDE sorted newest-first."""
    path_cls = _path_cls()
    gemini_root = path_cls.home() / ".gemini"
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
    return found[: max(1, limit)]


def resolve_target_transcript(target: str) -> tuple[str, str, Path, Path] | None:
    """Resolve `latest`, conversation UUID (or prefix), or explicit JSONL path."""
    path_cls = _path_cls()
    p_cand = path_cls(target)
    if p_cand.is_file():
        logs_dir = p_cand.parent
        compact = logs_dir / "transcript.jsonl"
        full = logs_dir / "transcript_full.jsonl"
        cid = infer_cid_from_transcript_path(p_cand)
        return "path", cid, (compact if compact.is_file() else p_cand), (full if full.is_file() else p_cand)

    recent = discover_recent_transcripts(limit=200)
    if not recent:
        return None
    if target == "latest":
        for _, prod, cid, compact, full in recent:
            if read_first_step_info(full) is not None:
                return prod, cid, compact, full
        _, prod, cid, compact, full = recent[0]
        return prod, cid, compact, full
    needle = target.strip().lower()
    for _, prod, cid, compact, full in recent:
        if cid.lower() == needle or cid.lower().startswith(needle):
            return prod, cid, compact, full
    return None


def run_cli(argv: list[str]) -> int:
    """Read-only diagnostic CLI for `/rehydrate` (`status`, `inspect`, `preview`, `show-config`)."""
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument(
        "--cwd",
        action="append",
        default=argparse.SUPPRESS,
        help="Workspace root directory (repeatable for multi-root workspaces)",
    )

    parser = argparse.ArgumentParser(
        description="Inspect AGY Post-Compact Rehydration state and previews.",
        parents=[common],
    )
    sub = parser.add_subparsers(dest="subcmd", required=True)

    p_status = sub.add_parser(
        "status", parents=[common], help="List recent App/CLI/IDE sessions and checkpoint/fork state"
    )
    p_status.add_argument("--limit", type=int, default=8, help="Max recent sessions to display (default: 8)")

    p_inspect = sub.add_parser(
        "inspect", parents=[common], help="Dry-run rehydration stats and restored file list for a session"
    )
    p_inspect.add_argument("target", nargs="?", default="latest", help="'latest', conversation ID/prefix, or path")
    p_inspect.add_argument("--checkpoint", type=int, default=None, help="Inspect a specific checkpoint step index")

    p_preview = sub.add_parser(
        "preview", parents=[common], help="Preview rendered rehydration sections for a session"
    )
    p_preview.add_argument("target", nargs="?", default="latest", help="'latest', conversation ID/prefix, or path")
    p_preview.add_argument("--checkpoint", type=int, default=None, help="Preview a specific checkpoint step index")
    p_preview.add_argument("--full", action="store_true", help="Print full rehydrated markdown text")

    sub.add_parser("show-config", parents=[common], help="Display effective budgets merged with .agents/rehydrate.json")

    args = parser.parse_args(argv)
    path_cls = _path_cls()
    dt_cls = _datetime_cls()
    raw_cwds = getattr(args, "cwd", None) or [os.getcwd()]
    workspaces: list[Path] = []
    seen_ws: set[str] = set()
    for raw_c in raw_cwds:
        resolved_ws = path_cls(raw_c).resolve()
        wkey = canonical_path_key(str(resolved_ws))
        if wkey not in seen_ws:
            seen_ws.add(wkey)
            workspaces.append(resolved_ws)
    cfg = load_rehydrate_config(workspaces)

    if args.subcmd == "show-config":
        print(
            json.dumps(
                {
                    "workspace": str(workspaces[0]),
                    "workspaces": [str(w) for w in workspaces],
                    "enabled": cfg.enabled,
                    "adaptive": cfg.adaptive,
                    "budgetAllUserChars": cfg.budget_all_user_chars,
                    "budgetRecentTurnsChars": cfg.budget_recent_turns_chars,
                    "budgetSkillsSpecsChars": cfg.budget_skills_specs_chars,
                    "budgetFilesTotalChars": cfg.budget_files_total_chars,
                    "budgetSingleFileChars": cfg.budget_single_file_chars,
                    "maxRecentEntries": cfg.max_recent_entries,
                    "maxModifiedFiles": cfg.max_modified_files,
                    "maxReferencedFiles": cfg.max_referenced_files,
                    "maxModifiedAgeSteps": cfg.max_modified_age_steps,
                },
                indent=2,
                ensure_ascii=False,
            )
        )
        return 0

    if args.subcmd == "status":
        rows = discover_recent_transcripts(limit=args.limit)
        if not rows:
            print("No Antigravity transcripts found under ~/.gemini/{antigravity,antigravity-cli,antigravity-ide}/brain.")
            return 0
        for mtime, prod, cid, compact, full in rows:
            try:
                cp, max_step, _ = scan_compact_tail(compact, 0, None)
            except OSError:
                ts_str = dt_cls.fromtimestamp(mtime).strftime("%Y-%m-%d %H:%M:%S")
                print(f"[{ts_str}] {prod:<15} {cid}  [sandbox-restricted; run outside sandbox to inspect]")
                continue
            first_info = read_first_step_info(full)
            first_step = first_info[0] if first_info else 0
            state_file = full.parent / "rehydrate_state.json"
            last_rehydrated = None
            if state_file.is_file():
                try:
                    st_obj = json.loads(state_file.read_text(encoding="utf-8"))
                    if isinstance(st_obj, dict):
                        last_rehydrated = st_obj.get("last_rehydrated_checkpoint")
                except (OSError, ValueError):
                    pass
            ts_str = dt_cls.fromtimestamp(mtime).strftime("%Y-%m-%d %H:%M:%S")
            fork_flag = f"fork@{first_step}" if first_step > 0 else "root"
            cp_flag = f"cp={cp} (rehydrated={last_rehydrated})" if cp is not None else "no-cp"
            print(f"[{ts_str}] {prod:<15} {cid}  steps={first_step}..{max_step}  {fork_flag}  {cp_flag}")
        return 0

    resolved = resolve_target_transcript(args.target)
    if resolved is None:
        print(f"Conversation transcript not found for target: {args.target}")
        return 1
    prod, cid, _, full_path = resolved
    ancestors = resolve_fork_ancestors(full_path)
    explicit_cp = getattr(args, "checkpoint", None)
    try:
        res = build_rehydration(
            full_path,
            up_to_checkpoint_step=explicit_cp if explicit_cp is not None else 999_999_999,
            ancestors=ancestors,
            config=cfg,
            workspace_roots=workspaces,
            simulate_if_no_checkpoint=(explicit_cp is None),
        )
    except OSError as exc:
        print(f"Cannot read transcript {full_path} ({exc}); rerun outside sandbox if inspecting another session.")
        return 1
    if res is None:
        if read_first_step_info(full_path) is not None:
            bypass_info = {
                "product": prod,
                "conversation_id": cid,
                "transcript_full": str(full_path),
                "adaptive": cfg.adaptive,
                "adaptive_silent_bypass": True,
                "reason": "Trivial completed Q&A already 100% covered by <CONTEXT_SUMMARY>",
                "chars": 0,
                "utf8_bytes": 0,
                "inject_chunks": 0,
            }
            if args.subcmd == "inspect":
                print(json.dumps(bypass_info, indent=2, ensure_ascii=False))
            else:
                print("[Adaptive Silent Bypass] 0 bytes injected — trivial Q&A already fully covered by <CONTEXT_SUMMARY>.")
            return 0
        print(f"No steps available to inspect in {full_path}")
        return 0

    full_msg, stats = res
    summary = {k: v for k, v in stats.items() if k != "chunks"}
    summary["product"] = prod
    summary["conversation_id"] = cid
    summary["transcript_full"] = str(full_path)
    summary["fork_ancestors"] = ancestors

    if args.subcmd == "inspect":
        print(json.dumps(summary, indent=2, ensure_ascii=False))
        return 0

    if args.full:
        print(full_msg)
    else:
        lines = full_msg.splitlines()
        preview_head = "\n".join(lines[:45])
        print(preview_head)
        if len(lines) > 45:
            print(f"\n... [{len(lines) - 45} more lines; pass --full to print complete payload] ...")
    return 0
