#!/usr/bin/env python3
"""4-layer rehydration engine, `/fork` lineage tracer, and 48 KiB UTF-8 chunker for `agy-rehydrate`."""

from __future__ import annotations

import json
import os
import sys
import time

TYPE_CHECKING = False
if TYPE_CHECKING:
    from collections import deque
    from pathlib import Path
    from typing import Any

sys.dont_write_bytecode = True

from rehydrate_after_compact import (
    FRESH_CHECKPOINT_MAX_GAP,
    _extract_step_index_bytes,
    _find_transcript_files_str,
    _save_state_str,
    _scan_compact_tail_str,
)
from rehydrate_helpers import (
    AGY_PRODUCT_DIRS,
    CONFIG_SCRIPT_EXTS,
    CONFIG_SCRIPT_NAMES,
    CONT_MARKER_PREFIX,
    DEFAULT_BUDGET_ALL_USER_CHARS,
    DEFAULT_BUDGET_FILES_TOTAL_CHARS,
    DEFAULT_BUDGET_RECENT_TURNS_CHARS,
    DEFAULT_BUDGET_SINGLE_FILE_CHARS,
    DEFAULT_BUDGET_SKILLS_SPECS_CHARS,
    DEFAULT_MAX_IN_PROGRESS_READ_FILES,
    DEFAULT_MAX_MODIFIED_AGE_STEPS,
    DEFAULT_MAX_MODIFIED_FILES,
    DEFAULT_MAX_READ_SKILL_AGE_STEPS,
    DEFAULT_MAX_RECENT_ENTRIES,
    DEFAULT_MAX_REFERENCED_FILES,
    FOLD_PASTE_HEAD_CHARS,
    FOLD_PASTE_TAIL_CHARS,
    FOLD_PASTE_THRESHOLD_CHARS,
    MARKER_PREFIX,
    MAX_FORK_DEPTH,
    MAX_INJECT_STEP_BYTES,
    OLDER_ASSISTANT_FOLD_THRESHOLD_CHARS,
    OLDER_ASSISTANT_HEAD_CHARS,
    OLDER_ASSISTANT_TAIL_CHARS,
    PROSE_DOC_EXTS,
    PROSE_DOC_STEMS,
    READ_TOOL_NAMES,
    RECENT_USER_VERBATIM_COUNT,
    TURN_CLIP_HEAD_CHARS,
    TURN_CLIP_MIN_OMITTED_CHARS,
    TURN_CLIP_TAIL_CHARS,
    TURN_CLIP_THRESHOLD_CHARS,
    WRITE_TOOL_NAMES,
    RehydrateConfig,
    _clip_inline_label,
    _clip_markdown_to_budget,
    _clip_text_middle,
    _datetime_cls,
    _deque_cls,
    _fast_basename,
    _fold_markdown_middle,
    _modified_file_tier,
    _norm_prompt_for_dedup,
    _parse_signed_int_prefix,
    _path_cls,
    _prepare_root_prefixes,
    _re,
    _rx,
    _strip_leading_ui_tool_badges,
    build_anti_decay_milestone_digest,
    build_workspace_reality_notice,
    canonical_path_key,
    classify_tool_step,
    default_custom_skill_roots,
    extract_checkpoint_attachment_refs,
    extract_checkpoint_truncated_prefixes,
    extract_checkpoint_user_norm_counts,
    extract_edit_snippets,
    extract_user_text,
    fold_user_paste,
    format_ask_question_exchange,
    format_single_tool_call,
    is_compact_slash_cmd,
    is_eligible_text_file,
    is_likely_raw_log_paste,
    is_low_info_short_turn,
    is_skill_or_spec,
    read_modified_hunk_window,
    read_scoped_text_clipped,
    read_text_clipped,
    strip_nested_rehydration,
)

FORK_RESOLVE_DEADLINE_SECS = 3.0
MAX_UNCONFIRMED_FORK_TIP_DELTA_SECS = 86400.0
MAX_UNCONFIRMED_FORK_NONTIP_DELTA_SECS = 3600.0


_ISO_EPOCH_CACHE: dict[str, float | None] = {}
_ISO_CACHE_MISS = object()


def parse_iso_epoch(ts_str: Any) -> float | None:
    if not isinstance(ts_str, str) or not ts_str:
        return None
    cached = _ISO_EPOCH_CACHE.get(ts_str, _ISO_CACHE_MISS)
    if cached is not _ISO_CACHE_MISS:
        return cached  # type: ignore[return-value]
    try:
        norm = ts_str[:-1] + "+00:00" if ts_str.endswith("Z") else ts_str
        val: float | None = _datetime_cls().fromisoformat(norm).timestamp()
    except ValueError:
        val = None
    if len(_ISO_EPOCH_CACHE) < 2048:
        _ISO_EPOCH_CACHE[ts_str] = val
    return val


def _extract_completed_at_epoch(text: str) -> float | None:
    """Extract the latest `Completed At:` ISO timestamp from tool output (raw JSON head or decoded content)."""
    if not text or "Completed At:" not in text[:512]:
        return None
    head = text[:512]
    best: float | None = None
    pos = 0
    while True:
        idx = head.find("Completed At:", pos)
        if idx < 0:
            break
        start = idx + 13
        while start < len(head) and head[start] in (" ", "\t"):
            start += 1
        end = start
        while end < len(head) and head[end] not in ("\r", "\n", "\\", '"', "'", " ", "\t"):
            end += 1
        ep = parse_iso_epoch(head[start:end])
        if ep is not None and (best is None or ep > best):
            best = ep
        pos = end if end > idx else idx + 13
    return best


def _record_path_touch_epoch(
    store: dict[str, tuple[int, float]],
    fpath: str,
    call_step: int,
    *epochs: float | None,
) -> None:
    """Record the latest monotonic completion epoch for a touched path so write latency never triggers false drift."""
    valid = [ep for ep in epochs if ep is not None]
    if not valid:
        return
    eff_epoch = max(valid)
    ck = canonical_path_key(fpath)
    prev = store.get(ck)
    if prev is None:
        store[ck] = (call_step, eff_epoch)
    else:
        store[ck] = (max(prev[0], call_step), max(prev[1], eff_epoch))


def infer_cid_from_transcript_path(tpath: Path) -> str:
    parts = tpath.parts
    if len(parts) >= 4 and parts[-2] == "logs" and parts[-3] == ".system_generated":
        return parts[-4]
    return tpath.parent.name


def infer_artifact_dir_from_transcript_path(tpath: Path) -> Path | None:
    parts = tpath.parts
    if len(parts) >= 4 and parts[-2] == "logs" and parts[-3] == ".system_generated":
        return tpath.parents[2]
    return None


def read_first_step_info(jsonl_path: Path) -> tuple[int, float | None] | None:
    try:
        with open(jsonl_path, "rb") as f:
            for _ in range(16):
                raw = f.readline()
                if not raw:
                    return None
                if not raw.strip():
                    continue
                try:
                    obj = json.loads(raw.decode("utf-8", "replace"))
                except json.JSONDecodeError:
                    continue
                if not isinstance(obj, dict):
                    continue
                sidx = obj.get("step_index")
                if isinstance(sidx, int) and not isinstance(sidx, bool):
                    return sidx, parse_iso_epoch(obj.get("created_at"))
    except OSError:
        return None
    return None


def _resolve_unknown_last_step(fb: Any, target_step: int) -> int:
    """When tail scan could not establish `last_step`, verify whether any non-empty content follows `target_step`."""
    while True:
        nxt = fb.readline()
        if not nxt:
            return target_step
        if not nxt.strip():
            continue
        nxt_sidx = _extract_step_index_bytes(nxt)
        if nxt_sidx > target_step:
            return nxt_sidx
        return -1


def find_step_epoch_in_transcript(jsonl_path: Path, target_step: int) -> tuple[int, float | None] | None:
    try:
        st_size = jsonl_path.stat().st_size
        if st_size <= 0:
            return None

        last_step: int | None = None
        tail_seek_pos = st_size
        with open(jsonl_path, "rb") as fb:
            for tail_window in (65536, 524288):
                seek_pos = max(0, st_size - tail_window)
                tail_seek_pos = seek_pos
                fb.seek(seek_pos)
                chunk = fb.read()
                raw_lines = chunk.splitlines()
                if seek_pos > 0 and raw_lines:
                    raw_lines = raw_lines[1:]

                for raw in reversed(raw_lines):
                    if not raw.strip():
                        continue
                    try:
                        obj = json.loads(raw.decode("utf-8", "replace"))
                    except json.JSONDecodeError:
                        continue
                    if not isinstance(obj, dict):
                        continue
                    sidx = obj.get("step_index")
                    if not isinstance(sidx, int) or isinstance(sidx, bool):
                        continue
                    if last_step is None:
                        last_step = sidx
                        if last_step < target_step:
                            return None
                    if sidx == target_step:
                        return last_step, parse_iso_epoch(obj.get("created_at"))
                    if sidx < target_step:
                        return None

                if seek_pos == 0:
                    return None
                if last_step is not None:
                    break

            # Binary search monotonic `step_index` over `[0, hi]` to avoid multi-MB full scans
            lo = 0
            hi = tail_seek_pos if last_step is not None else st_size
            while hi - lo > 65536:
                mid = (lo + hi) // 2
                fb.seek(mid - 1)
                fb.readline()
                probed = False
                reached_bound = False
                for _ in range(16):
                    if fb.tell() > hi:
                        reached_bound = True
                        break
                    raw = fb.readline()
                    if not raw:
                        reached_bound = True
                        break
                    if not raw.strip():
                        continue
                    try:
                        obj = json.loads(raw.decode("utf-8", "replace"))
                    except json.JSONDecodeError:
                        continue
                    if not isinstance(obj, dict):
                        continue
                    sidx = obj.get("step_index")
                    if not isinstance(sidx, int) or isinstance(sidx, bool):
                        continue
                    probed = True
                    if sidx == target_step:
                        eff_last = last_step if last_step is not None else _resolve_unknown_last_step(fb, target_step)
                        return eff_last, parse_iso_epoch(obj.get("created_at"))
                    if sidx < target_step:
                        lo = fb.tell()
                    else:
                        hi = mid
                    break
                if not probed:
                    if reached_bound:
                        hi = mid
                    else:
                        break

            step_pat = _re().compile(rb'"step_index"\s*:\s*' + str(target_step).encode("ascii") + rb"\b")
            fb.seek(lo)
            while fb.tell() <= hi:
                raw = fb.readline()
                if not raw:
                    break
                if not raw.strip():
                    continue
                head_slice = raw[:256]
                matched = bool(
                    step_pat.search(head_slice)
                    or (len(raw) > 256 and step_pat.search(raw[-256:]))
                    or (b'"step_index"' not in head_slice and step_pat.search(raw))
                )
                if matched:
                    try:
                        obj = json.loads(raw.decode("utf-8", "replace"))
                    except json.JSONDecodeError:
                        continue
                    if not isinstance(obj, dict):
                        continue
                    sidx = obj.get("step_index")
                    if isinstance(sidx, int) and not isinstance(sidx, bool) and sidx == target_step:
                        eff_last = last_step if last_step is not None else _resolve_unknown_last_step(fb, target_step)
                        return eff_last, parse_iso_epoch(obj.get("created_at"))
        return None
    except OSError:
        return None


def load_history_fork_hints(brain_dir: Path) -> tuple[dict[str, set[str]], list[tuple[str, float]]]:
    cid_workspaces: dict[str, set[str]] = {}
    fork_events: list[tuple[str, float]] = []
    hist_path = brain_dir.parent / "history.jsonl"
    if not hist_path.is_file():
        return cid_workspaces, fork_events
    try:
        st_size = hist_path.stat().st_size
        with open(hist_path, "rb") as fb:
            seek_pos = max(0, st_size - 2097152)
            fb.seek(seek_pos)
            lines = fb.read().splitlines()
        if seek_pos > 0 and lines:
            lines = lines[1:]
        for raw in lines:
            if not raw.strip():
                continue
            try:
                obj = json.loads(raw.decode("utf-8", "replace"))
            except json.JSONDecodeError:
                continue
            if not isinstance(obj, dict):
                continue
            cid = obj.get("conversationId")
            ws = obj.get("workspace")
            if isinstance(cid, str) and cid:
                if isinstance(ws, str) and ws:
                    cid_workspaces.setdefault(cid, set()).add(canonical_path_key(ws))
                if obj.get("display") == "/fork" and isinstance(obj.get("timestamp"), (int, float)):
                    fork_events.append((cid, float(obj["timestamp"]) / 1000.0))
    except OSError:
        pass
    return cid_workspaces, fork_events


def validate_cached_ancestors(cached: Any, cur_first_step: int) -> list[dict[str, Any]] | None:
    if not isinstance(cached, list) or not cached:
        return None
    cleaned: list[dict[str, Any]] = []
    for item in cached:
        if not isinstance(item, dict):
            return None
        p_str, cid, s_min, s_max = item.get("path"), item.get("cid"), item.get("min_step"), item.get("max_step")
        if (
            not isinstance(p_str, str)
            or not isinstance(cid, str)
            or not isinstance(s_min, int)
            or isinstance(s_min, bool)
            or not isinstance(s_max, int)
            or isinstance(s_max, bool)
            or s_min > s_max
            or not os.path.isfile(p_str)
        ):
            return None
        cleaned.append({"cid": cid, "path": p_str, "min_step": s_min, "max_step": s_max})
    for idx in range(len(cleaned) - 1):
        if cleaned[idx]["max_step"] + 1 != cleaned[idx + 1]["min_step"]:
            return None
    return cleaned if cleaned[-1]["max_step"] + 1 == cur_first_step else None


def resolve_fork_ancestors(
    full_path: Path,
    cached_ancestors: Any = None,
    first_info: tuple[int, float | None] | None = None,
) -> list[dict[str, Any]]:
    """Trace `/fork` parent conversations up to `MAX_FORK_DEPTH` hops with strict step-range isolation."""
    effective_first = first_info if first_info is not None else read_first_step_info(full_path)
    if effective_first is None or effective_first[0] <= 0:
        return []
    cur_first_step, cur_first_epoch = effective_first
    valid_cache = validate_cached_ancestors(cached_ancestors, cur_first_step)
    if valid_cache is not None:
        return valid_cache

    parts = full_path.parts
    if len(parts) < 5 or parts[-2] != "logs" or parts[-3] != ".system_generated":
        return []
    brain_dir = full_path.parents[3]
    if not brain_dir.is_dir():
        return []

    deadline = time.monotonic() + FORK_RESOLVE_DEADLINE_SECS
    cid_workspaces, fork_events = load_history_fork_hints(brain_dir)
    fork_event_by_cid: dict[str, list[float]] = {}
    for fcid, fts in fork_events:
        fork_event_by_cid.setdefault(fcid, []).append(fts)

    cur_cid = infer_cid_from_transcript_path(full_path)
    visited: set[str] = {cur_cid}
    ancestors_rev: list[dict[str, Any]] = []
    child_cid, child_first_step, child_first_epoch = cur_cid, cur_first_step, cur_first_epoch

    try:
        raw_cand_dirs = [d for d in brain_dir.iterdir() if d.is_dir()]
    except OSError:
        return []

    # Cache per-candidate file paths and first-step info across all hops to avoid repeated disk scans
    cand_meta_cache: dict[str, tuple[Path, Path, float, float, tuple[int, float | None]] | None] = {}

    def _get_cand_meta(cand_dir: Path) -> tuple[Path, Path, float, float, tuple[int, float | None]] | None:
        cid = cand_dir.name
        if cid in cand_meta_cache:
            return cand_meta_cache[cid]
        cand_logs = cand_dir / ".system_generated" / "logs"
        full_cand = cand_logs / "transcript_full.jsonl"
        compact_cand = cand_logs / "transcript.jsonl"
        has_full = full_cand.is_file()
        has_compact = compact_cand.is_file()
        if not has_full and not has_compact:
            cand_meta_cache[cid] = None
            return None
        cand_full = full_cand if has_full else compact_cand
        cand_scan = compact_cand if has_compact else full_cand
        try:
            full_mt = cand_full.stat().st_mtime
            scan_mt = cand_scan.stat().st_mtime if cand_scan != cand_full else full_mt
        except OSError:
            cand_meta_cache[cid] = None
            return None
        c_first = read_first_step_info(cand_scan)
        if c_first is None and cand_scan != cand_full:
            c_first = read_first_step_info(cand_full)
        if c_first is None or c_first[0] < 0:
            cand_meta_cache[cid] = None
            return None
        meta = (cand_full, cand_scan, full_mt, scan_mt, c_first)
        cand_meta_cache[cid] = meta
        return meta

    for _ in range(MAX_FORK_DEPTH):
        if child_first_step <= 0 or time.monotonic() >= deadline:
            break
        need_prev = child_first_step - 1
        child_ws = cid_workspaces.get(child_cid, set())
        best_score: tuple[int, int, int, int, float] | None = None
        best_seg: tuple[str, Path, int, float | None] | None = None

        # Check explicit `/fork` event candidates first, then same-workspace, then remaining dirs
        def _cand_priority(d: Path) -> tuple[int, int]:
            cid = d.name
            has_fe = 0 if cid in fork_event_by_cid else 1
            cws = cid_workspaces.get(cid, set())
            is_sw = 0 if (child_ws and cws and (child_ws & cws)) else 1
            return (has_fe, is_sw)

        ordered_dirs = sorted(raw_cand_dirs, key=_cand_priority)
        saw_non_fe = False

        for cand_dir in ordered_dirs:
            cand_cid = cand_dir.name
            if cand_cid in visited:
                continue
            in_fe = cand_cid in fork_event_by_cid
            if not in_fe:
                if not saw_non_fe:
                    saw_non_fe = True
                    # A confirmed `fork_cmd_match == 1` cannot be beaten by any non-fork-event candidate
                    if best_score is not None and best_score[0] == 1:
                        break
                if time.monotonic() >= deadline:
                    break

            cand_ws = cid_workspaces.get(cand_cid, set())
            same_ws = int(bool(child_ws and cand_ws and (child_ws & cand_ws)))
            if not in_fe and child_ws and cand_ws and not same_ws:
                continue

            meta = _get_cand_meta(cand_dir)
            if meta is None:
                continue
            cand_full, cand_scan, full_mt, scan_mt, cand_first = meta
            if cand_first[0] > need_prev:
                continue
            if child_first_epoch is not None and cand_first[1] is not None and cand_first[1] > child_first_epoch + 5.0:
                continue

            hit = find_step_epoch_in_transcript(cand_scan, need_prev)
            if hit is None and cand_scan != cand_full and full_mt > scan_mt + 1.0:
                hit = find_step_epoch_in_transcript(cand_full, need_prev)
            if hit is None:
                continue
            cand_last_step, prev_epoch = hit
            if child_first_epoch is not None and prev_epoch is not None:
                if prev_epoch > child_first_epoch + 5.0:
                    continue
                time_delta = max(0.0, child_first_epoch - prev_epoch)
            else:
                time_delta = 1e9

            fork_cmd_match = int(
                any(
                    (prev_epoch is None or fts >= prev_epoch - 5.0)
                    and (child_first_epoch is None or fts <= child_first_epoch + 5.0)
                    for fts in fork_event_by_cid.get(cand_cid, ())
                )
            )
            exact_tip = int(cand_last_step == need_prev)
            if not fork_cmd_match:
                if child_ws and cand_ws and not same_ws:
                    continue
                if child_first_epoch is not None and prev_epoch is not None:
                    max_delta = (
                        MAX_UNCONFIRMED_FORK_TIP_DELTA_SECS
                        if exact_tip
                        else MAX_UNCONFIRMED_FORK_NONTIP_DELTA_SECS
                    )
                    if time_delta > max_delta:
                        continue

            if time_delta <= 120.0:
                time_tier = 3
            elif time_delta <= 1800.0:
                time_tier = 2
            elif time_delta <= MAX_UNCONFIRMED_FORK_TIP_DELTA_SECS:
                time_tier = 1
            else:
                time_tier = 0
            score = (fork_cmd_match, same_ws, time_tier, exact_tip, -time_delta)
            if best_score is None or score > best_score:
                best_score = score
                best_seg = (cand_cid, cand_full, cand_first[0], cand_first[1])

        if best_seg is None:
            break
        p_cid, p_path, p_first_step, p_first_epoch = best_seg
        ancestors_rev.append({"cid": p_cid, "path": str(p_path), "min_step": p_first_step, "max_step": need_prev})
        visited.add(p_cid)
        child_cid, child_first_step, child_first_epoch = p_cid, p_first_step, p_first_epoch
        if p_first_step <= 0:
            break

    ancestors_rev.reverse()
    return ancestors_rev


def find_transcript_files(payload: dict[str, Any]) -> tuple[Path | None, Path | None, Path | None]:
    """Locate `(transcript.jsonl, transcript_full.jsonl, rehydrate_state.json)` across App, CLI, and IDE."""
    c_str, f_str, s_str = _find_transcript_files_str(payload)
    if c_str is None or f_str is None or s_str is None:
        return None, None, None
    path_cls = _path_cls()
    return path_cls(c_str), path_cls(f_str), path_cls(s_str)


def scan_compact_tail(compact_path: Path | str, prev_offset: int, prev_cp: int | None) -> tuple[int | None, int, int]:
    """Incrementally scan newly appended JSONL bytes for `CHECKPOINT` records."""
    return _scan_compact_tail_str(str(compact_path), prev_offset, prev_cp)


def _format_user_timeline_ex(
    user_messages: list[tuple[int, str, Path | None, int]],
    budget_chars: int = DEFAULT_BUDGET_ALL_USER_CHARS,
    truncated_prefixes: list[str] | None = None,
    prev_cp_step: int = -1,
) -> tuple[str, int, int, int, int, bool]:
    """Render earlier user request timeline, also reporting whether budget omission/clipping occurred."""
    total_real = sum(1 for _, txt, _, _ in user_messages if not txt.startswith("--- [/fork branch "))
    if total_real == 0:
        return "", 0, 0, 0, 0, False

    def render_pass(
        fold_before_recent: int,
        fold_before_step: int = -1,
        fold_pre_window_logs: bool = False,
    ) -> tuple[list[str], int, int, int]:
        lines: list[str] = []
        idx = 0
        kept_real = 0
        filtered_short = 0
        folded_pastes = 0
        verbatim_cutoff = total_real - RECENT_USER_VERBATIM_COUNT
        fold_cutoff = total_real - fold_before_recent
        for step_s, txt, spath, lno in user_messages:
            if txt.startswith("--- [/fork branch "):
                lines.append(f"- {txt}")
                continue
            idx += 1
            if 1 < idx <= verbatim_cutoff and is_low_info_short_turn(txt):
                filtered_short += 1
                continue
            out_txt = txt
            has_trunc_pfx = False
            if truncated_prefixes and len(txt) > 550:
                norm_t = _norm_prompt_for_dedup(txt)
                has_trunc_pfx = any(norm_t.startswith(p) for p in truncated_prefixes)
            should_fold = (
                has_trunc_pfx
                or idx <= fold_cutoff
                or (fold_before_step >= 0 and step_s < fold_before_step)
                or (fold_pre_window_logs and prev_cp_step >= 0 and step_s < prev_cp_step and is_likely_raw_log_paste(txt))
                or "[Quoted POST-COMPACT REHYDRATION" in txt
            )
            if should_fold:
                out_txt, did_fold = fold_user_paste(txt, spath, lno, step_s, truncated_prefixes=truncated_prefixes)
                if did_fold:
                    folded_pastes += 1
            kept_real += 1
            lines.append(f"- [#{idx} @ step {step_s}]: {out_txt}")
        return lines, kept_real, filtered_short, folded_pastes

    user_lines, kept_count, filtered_short_count, folded_paste_count = render_pass(
        RECENT_USER_VERBATIM_COUNT, fold_pre_window_logs=(prev_cp_step >= 0)
    )
    all_user_block = "\n".join(user_lines)
    budget_truncated = False

    if len(all_user_block) > budget_chars and prev_cp_step >= 0:
        user_lines, kept_count, filtered_short_count, folded_paste_count = render_pass(
            RECENT_USER_VERBATIM_COUNT, fold_before_step=prev_cp_step
        )
        all_user_block = "\n".join(user_lines)

    if len(all_user_block) > budget_chars:
        user_lines, kept_count, filtered_short_count, folded_paste_count = render_pass(0)
        all_user_block = "\n".join(user_lines)

    if len(all_user_block) > budget_chars and len(user_lines) > 2:
        head_cnt = min(5, max(1, len(user_lines) // 3))
        head_part = "\n".join(user_lines[:head_cnt])
        if len(head_part) < budget_chars - 120:
            tail_lines: list[str] = []
            used = len(head_part) + 80
            for ul in reversed(user_lines[head_cnt:]):
                if used + len(ul) + 1 > budget_chars:
                    break
                tail_lines.append(ul)
                used += len(ul) + 1
            tail_lines.reverse()
            if len(tail_lines) < len(user_lines) - head_cnt:
                kept_lines = [*user_lines[:head_cnt], *tail_lines]
                kept_count = sum(1 for ln in kept_lines if not ln.startswith("- --- [/fork branch "))
                omitted_line = "- [... earlier middle prompts omitted for budget ...]"
                all_user_block = (
                    f"{head_part}\n{omitted_line}\n" + "\n".join(tail_lines)
                    if tail_lines
                    else f"{head_part}\n{omitted_line}"
                )
                budget_truncated = True

    if len(all_user_block) > budget_chars:
        all_user_block = _clip_markdown_to_budget(all_user_block, max(160, budget_chars))
        kept_count = min(kept_count, sum(1 for ln in all_user_block.splitlines() if ln.startswith("- [#")))
        budget_truncated = True

    return all_user_block, total_real, kept_count, filtered_short_count, folded_paste_count, budget_truncated


def format_user_timeline(
    user_messages: list[tuple[int, str, Path | None, int]],
    budget_chars: int = DEFAULT_BUDGET_ALL_USER_CHARS,
    truncated_prefixes: list[str] | None = None,
) -> tuple[str, int, int, int, int]:
    """Render earlier user request timeline with short-turn denoising and long-paste folding."""
    all_user_block, total_real, kept_count, filtered_short_count, folded_paste_count, _ = _format_user_timeline_ex(
        user_messages,
        budget_chars=budget_chars,
        truncated_prefixes=truncated_prefixes,
    )
    return all_user_block, total_real, kept_count, filtered_short_count, folded_paste_count


def _record_modified_touch(
    store: dict[str, tuple[str, int, list[str], int]],
    path_str: str,
    step_idx: int,
    snippets: list[str],
    prev_cp_step: int = -1,
) -> None:
    key = canonical_path_key(path_str)
    prev = store.get(key)
    crossed_cp = prev is not None and prev_cp_step >= 0 and step_idx > prev_cp_step and prev[1] <= prev_cp_step
    combined = list(prev[2]) if (prev and not crossed_cp and prev[1] >= step_idx - 80) else []
    for s in snippets:
        if s and s not in combined:
            combined.append(s)
    edit_count = (prev[3] + 1) if (prev and prev[1] >= step_idx - 120) else 1
    store[key] = (path_str, step_idx, combined[-6:], edit_count)


def _record_read_touch(
    store: dict[str, tuple[str, int, tuple[int | None, int | None]]],
    path_str: str,
    step_idx: int,
    line_range: tuple[int | None, int | None],
) -> None:
    key = canonical_path_key(path_str)
    prev = store.get(key)
    if prev is None or prev[1] < step_idx - 12:
        store[key] = (path_str, step_idx, line_range)
        return
    p_range = prev[2]
    if p_range == (None, None) or line_range == (None, None):
        merged: tuple[int | None, int | None] = (None, None)
    else:
        starts = [x for x in (p_range[0], line_range[0]) if x is not None]
        ends = [x for x in (p_range[1], line_range[1]) if x is not None]
        merged = (
            min(starts) if len(starts) == 2 else None,
            max(ends) if len(ends) == 2 else None,
        )
    store[key] = (path_str, step_idx, merged)


def _record_skill_spec_touch(
    store: dict[str, tuple[str, int, bool]],
    path_str: str,
    step_idx: int,
    was_modified: bool,
) -> None:
    key = canonical_path_key(path_str)
    prev = store.get(key)
    store[key] = (path_str, step_idx, bool(was_modified or (prev and prev[2])))


def _render_dialogue_entry(
    step_idx: int,
    role: str,
    tool_prefix: str,
    body: str,
    condense_assistant: bool,
    condense_user_in_summary: bool = False,
    assistant_head_chars: int = OLDER_ASSISTANT_HEAD_CHARS,
    assistant_tail_chars: int = OLDER_ASSISTANT_TAIL_CHARS,
) -> tuple[str, bool]:
    if role == "banner" or role == "in_progress":
        return body, False
    if role == "user":
        cleaned_u = _strip_leading_ui_tool_badges(body) if len(body) > 900 else body
        if condense_user_in_summary and len(cleaned_u) > FOLD_PASTE_THRESHOLD_CHARS:
            folded_u, did_fold_u = _fold_markdown_middle(
                cleaned_u,
                FOLD_PASTE_HEAD_CHARS,
                FOLD_PASTE_TAIL_CHARS,
                "  ...[user prompt already verbatim in <CONTEXT_SUMMARY> # User Requests; {omitted} chars folded]...",
                min_omitted=150,
            )
            if did_fold_u:
                return f"[Step {step_idx} | User]:\n{folded_u}", True
        if len(cleaned_u) > TURN_CLIP_THRESHOLD_CHARS:
            clipped, _ = _fold_markdown_middle(
                cleaned_u,
                TURN_CLIP_HEAD_CHARS,
                TURN_CLIP_TAIL_CHARS,
                "  ...[turn clipped ({omitted} chars)]...",
                min_omitted=TURN_CLIP_MIN_OMITTED_CHARS,
            )
        else:
            clipped = cleaned_u
        return f"[Step {step_idx} | User]:\n{clipped}", False
    if condense_assistant and len(body) > OLDER_ASSISTANT_FOLD_THRESHOLD_CHARS:
        folded, did_fold = _fold_markdown_middle(
            body,
            assistant_head_chars,
            assistant_tail_chars,
            "  ...[assistant details summarized in <CONTEXT_SUMMARY>; {omitted} chars folded]...",
            min_omitted=150,
        )
        if did_fold:
            return f"[Step {step_idx} | Assistant]:\n{tool_prefix}{folded}", True
    if len(body) > TURN_CLIP_THRESHOLD_CHARS:
        clipped, _ = _fold_markdown_middle(
            body,
            TURN_CLIP_HEAD_CHARS,
            TURN_CLIP_TAIL_CHARS,
            "  ...[turn clipped ({omitted} chars)]...",
            min_omitted=TURN_CLIP_MIN_OMITTED_CHARS,
        )
    else:
        clipped = body
    return f"[Step {step_idx} | Assistant]:\n{tool_prefix}{clipped}", False


def _flush_step_batch(batch: list[list[str]], pending_tools: list[list[list[str]]]) -> None:
    if not batch:
        return
    pending_tools.append(list(batch))
    batch.clear()


def _summarize_tool_chain(tools: list[str], max_items: int = 6) -> str:
    """Keep opening steps, middle `ask_question` decisions, and final edit/verification steps."""
    if len(tools) <= max_items:
        return " -> ".join(tools)
    head_n = max(1, max_items // 3)
    tail_n = max(1, max_items - head_n)
    if len(tools) <= head_n + tail_n:
        return " -> ".join(tools)
    mid_aq = [i for i in range(head_n, len(tools) - tail_n) if "ask_question(" in tools[i]][-3:]
    if not mid_aq:
        omitted = len(tools) - head_n - tail_n
        return " -> ".join([*tools[:head_n], f"(+{omitted} steps)", *tools[-tail_n:]])
    keep_indices = [*range(head_n), *mid_aq, *range(len(tools) - tail_n, len(tools))]
    parts: list[str] = []
    prev_idx = -1
    for idx in keep_indices:
        gap = idx - prev_idx - 1
        if gap > 0:
            parts.append(f"(+{gap} steps)")
        parts.append(tools[idx])
        prev_idx = idx
    return " -> ".join(parts)


def _format_batch_labels(batches: list[list[list[str]]]) -> list[str]:
    formatted_batches: list[str] = []
    for batch in batches:
        shown: list[str] = []
        for item in batch[:8]:
            tsum, state = item[0], item[1]
            if state == "error":
                shown.append(f"{tsum} [ERROR]")
            elif state == "running":
                shown.append(f"{tsum} [RUNNING]")
            else:
                shown.append(tsum)
        label = ", ".join(shown)
        if len(batch) > 8:
            label += f", +{len(batch) - 8} more"
        if label:
            formatted_batches.append(label)
    return formatted_batches


def _format_tool_batches(batches: list[list[list[str]]]) -> str:
    labels = _format_batch_labels(batches)
    if not labels:
        return ""
    return f"  (Tools run: {_summarize_tool_chain(labels, max_items=6)})\n"


def _resolve_running_task_from_system_msg(
    scontent: str,
    running_by_task_id: dict[str, list[list[str]]],
    turn_running_task_ids: set[str],
) -> None:
    if not scontent or not running_by_task_id:
        return
    pos = scontent.find('Task id "', 0, 640)
    if pos < 0:
        return
    id_start = pos + len('Task id "')
    id_end = scontent.find('"', id_start, id_start + 160)
    if id_end <= id_start:
        return
    tid = scontent[id_start:id_end]
    refs = running_by_task_id.get(tid)
    if not refs:
        return
    after = scontent[id_end + 1 : id_end + 401].replace("\r\n", "\n")
    cut = after.find("\n\nCommand output:")
    if cut >= 0:
        after = after[:cut]
    outcome = ""
    if "finished with result:" in after:
        if "Encountered error in step execution:" in after:
            outcome = "error"
        else:
            exit_marker = "Command exited with code "
            epos = after.find(exit_marker)
            if epos >= 0:
                code_val = _parse_signed_int_prefix(after[epos + len(exit_marker) :])
                outcome = "error" if (code_val is not None and code_val != 0) else "ok"
            else:
                outcome = "ok"
    else:
        low = after.lower()
        if any(w in low for w in ("failed", "was killed", "killed", "timed out", "cancelled", "terminated")):
            outcome = "error"
    if outcome in ("ok", "error"):
        running_by_task_id.pop(tid, None)
        turn_running_task_ids.discard(tid)
        for it in refs:
            it[1] = outcome


def build_rehydration(
    full_path: Path,
    up_to_checkpoint_step: int | None = None,
    ancestors: list[dict[str, Any]] | None = None,
    config: RehydrateConfig | None = None,
    workspace_roots: list[Path] | None = None,
    artifact_dir: Path | None = None,
    simulate_if_no_checkpoint: bool = False,
) -> tuple[str, dict[str, Any]] | None:
    """Build lean, non-overlapping, permission-safe 4-layer lossless rehydration payload and chunks."""
    path_cls = _path_cls()
    step_str_re = _rx("STEP_INDEX_STR")
    type_str_re = _rx("TYPE_STR")
    cfg = config or RehydrateConfig()
    checkpoints: list[int] = []
    checkpoint_contents: dict[int, str] = {}
    checkpoint_lines: dict[int, int] = {}
    already_rehydrated_after: set[int] = set()
    max_seen_step = -1

    with open(full_path, "r", encoding="utf-8", errors="replace") as f:
        last_cp: int | None = None
        last_nonempty_line = ""
        for cp_lno, line in enumerate(f, 1):
            if not line.strip():
                continue
            if up_to_checkpoint_step is not None:
                m_s = step_str_re.search(line[:180]) or step_str_re.search(line[-64:])
                if m_s:
                    s_cand = int(m_s.group(1))
                    if s_cand > up_to_checkpoint_step:
                        break
                    if s_cand > max_seen_step:
                        max_seen_step = s_cand
            if line.lstrip().startswith("{"):
                last_nonempty_line = line
            has_cp = '"CHECKPOINT"' in line
            has_sys_marker = last_cp is not None and MARKER_PREFIX in line and '"SYSTEM_MESSAGE"' in line
            if not has_cp and not has_sys_marker:
                continue
            try:
                obj = json.loads(line)
            except json.JSONDecodeError:
                continue
            if not isinstance(obj, dict):
                continue
            step_idx = obj.get("step_index", -1)
            if isinstance(step_idx, int) and not isinstance(step_idx, bool):
                if up_to_checkpoint_step is not None and step_idx > up_to_checkpoint_step:
                    break
                if step_idx > max_seen_step:
                    max_seen_step = step_idx
            stype = obj.get("type")
            if stype == "CHECKPOINT" and isinstance(step_idx, int) and not isinstance(step_idx, bool):
                checkpoints.append(step_idx)
                checkpoint_contents[step_idx] = str(obj.get("content") or "")
                checkpoint_lines[step_idx] = cp_lno
                last_cp = step_idx
            elif stype == "SYSTEM_MESSAGE" and last_cp is not None:
                if MARKER_PREFIX in str(obj.get("content") or ""):
                    already_rehydrated_after.add(last_cp)
        if (up_to_checkpoint_step is None or up_to_checkpoint_step >= max_seen_step) and last_nonempty_line:
            m_last = step_str_re.search(last_nonempty_line[:180]) or step_str_re.search(last_nonempty_line[-64:])
            if m_last:
                max_seen_step = max(max_seen_step, int(m_last.group(1)))
            else:
                try:
                    last_obj = json.loads(last_nonempty_line)
                    if isinstance(last_obj, dict):
                        s_last = last_obj.get("step_index", -1)
                        if isinstance(s_last, int) and not isinstance(s_last, bool):
                            max_seen_step = max(max_seen_step, s_last)
                except json.JSONDecodeError:
                    pass

    simulated = False
    if (
        simulate_if_no_checkpoint
        and checkpoints
        and checkpoints[-1] in already_rehydrated_after
        and max_seen_step > checkpoints[-1] + 2
    ):
        target_cp = max_seen_step + 1
        prev_cp_step = checkpoints[-1]
        target_cp_content = ""
        simulated = True
    elif checkpoints:
        target_cp = checkpoints[-1]
        prev_cp_step = checkpoints[-2] if len(checkpoints) >= 2 else -1
        target_cp_content = checkpoint_contents.get(target_cp, "")
        if up_to_checkpoint_step is None and target_cp in already_rehydrated_after:
            return None
    elif simulate_if_no_checkpoint and max_seen_step >= 0:
        target_cp = max_seen_step + 1
        prev_cp_step = -1
        target_cp_content = ""
        simulated = True
    else:
        return None

    delayed_gap = 0
    delayed_post_cp = False
    if (
        not simulated
        and (up_to_checkpoint_step is None or up_to_checkpoint_step >= max_seen_step)
        and max_seen_step > target_cp + FRESH_CHECKPOINT_MAX_GAP
    ):
        delayed_gap = max_seen_step - target_cp
        delayed_post_cp = True

    prev_checkpoints: list[tuple[int, int, str]] = (
        [
            (cp_s, checkpoint_lines.get(cp_s, 0), checkpoint_contents.get(cp_s, ""))
            for cp_s in checkpoints[:-1]
            if checkpoint_contents.get(cp_s)
        ]
        if (not simulated and target_cp_content and len(checkpoints) >= 2)
        else []
    )

    cur_cid = infer_cid_from_transcript_path(full_path)
    effective_artifact_dir = artifact_dir or infer_artifact_dir_from_transcript_path(full_path)
    skill_extra_roots = default_custom_skill_roots(effective_artifact_dir)
    scratch_excluded_roots = [effective_artifact_dir / "scratch"] if effective_artifact_dir else None

    cur_first_info = read_first_step_info(full_path)
    cur_first_step = cur_first_info[0] if cur_first_info is not None else 0
    resolved_ancestors = (
        ancestors if ancestors is not None else resolve_fork_ancestors(full_path, first_info=cur_first_info)
    )
    segments: list[dict[str, Any]] = [
        *resolved_ancestors,
        {"cid": cur_cid, "path": str(full_path), "min_step": cur_first_step, "max_step": target_cp - 1},
    ]

    user_messages: list[tuple[int, str, Path | None, int]] = []
    dialogue_entries: list[tuple[int, str, list[list[list[str]]], str]] = []
    media_step_by_name: dict[str, int] = {}
    modified_touch: dict[str, tuple[str, int, list[str], int]] = {}
    read_touch: dict[str, tuple[str, int, tuple[int | None, int | None]]] = {}
    path_touch_epoch: dict[str, tuple[int, float]] = {}
    skill_spec_touch: dict[str, tuple[str, int, bool]] = {}
    pending_tools: list[list[list[str]]] = []
    turn_buffered_assistants: list[tuple[int, list[list[list[str]]], str]] = []
    pending_call_queue: deque[
        tuple[str, str, str, tuple[int | None, int | None], list[str], int, float | None]
    ] = _deque_cls()()
    cur_batch_summaries: list[list[str]] = []
    running_by_task_id: dict[str, list[list[str]]] = {}
    turn_running_task_ids: set[str] = set()
    user_turn_steps: list[int] = []
    last_user_step = -1
    in_progress_turn = False
    window_raw_chars = 0

    def _extract_fast_created_at(hdr_s: str) -> float | None:
        cpos = hdr_s.find('"created_at"')
        if cpos < 0:
            return None
        q1 = hdr_s.find('"', cpos + 12)
        if q1 < 0:
            return None
        q2 = hdr_s.find('"', q1 + 1, q1 + 48)
        if q2 <= q1:
            return None
        return parse_iso_epoch(hdr_s[q1 + 1 : q2])

    def _flush_uncompleted_assistants() -> None:
        if not turn_buffered_assistants:
            return
        for b_step, b_batches, b_body in turn_buffered_assistants:
            dialogue_entries.append((b_step, "assistant", b_batches, b_body))
        turn_buffered_assistants.clear()

    for seg_idx, seg in enumerate(segments):
        seg_path = path_cls(seg["path"])
        seg_min = int(seg["min_step"])
        seg_max = min(int(seg["max_step"]), target_cp - 1)
        if seg_min > seg_max or not seg_path.is_file():
            continue
        pending_call_queue.clear()
        running_by_task_id.clear()
        turn_running_task_ids.clear()
        _flush_step_batch(cur_batch_summaries, pending_tools)
        _flush_uncompleted_assistants()

        if seg_idx > 0:
            fork_banner = (
                f"--- [/fork branch {str(segments[seg_idx - 1]['cid'])[:8]} "
                f"-> {str(seg['cid'])[:8]} @ step {seg_min}] ---"
            )
            user_messages.append((seg_min, fork_banner, None, 0))
            dialogue_entries.append((seg_min, "banner", [], fork_banner))

        with open(seg_path, "r", encoding="utf-8", errors="replace") as f:
            for line_no, line in enumerate(f, 1):
                if not line.strip():
                    continue
                hdr = line[:240]
                m_step = step_str_re.search(hdr)
                m_type = type_str_re.search(hdr) if m_step is not None else None
                if m_step is not None and m_type is not None:
                    fast_sidx = int(m_step.group(1))
                    fast_type = m_type.group(1)
                    if fast_sidx < seg_min:
                        continue
                    if fast_sidx > seg_max:
                        break
                    if fast_type == "CHECKPOINT" or (fast_type == "SYSTEM_MESSAGE" and not running_by_task_id):
                        continue
                    if fast_type not in ("USER_INPUT", "PLANNER_RESPONSE", "CHECKPOINT", "SYSTEM_MESSAGE"):
                        if not pending_call_queue:
                            if fast_sidx > prev_cp_step:
                                window_raw_chars += max(0, len(line) - 140)
                            continue
                        hdr_chk = line[:768]
                        if (
                            len(line) > 2048
                            and pending_call_queue[0][0] != "ask_question"
                            and ('"content":' in hdr_chk or '"content" :' in hdr_chk)
                            and '"ERROR"' not in hdr
                            and '"error"' not in hdr
                            and "Encountered error in step execution:" not in hdr_chk
                            and (
                                "Command exited with code " not in hdr_chk
                                or "Command exited with code 0." in hdr_chk
                            )
                            and '"error"' not in line[-320:]
                            and ('"status": "DONE"' in hdr or '"status":"DONE"' in hdr)
                        ):
                            if fast_sidx > prev_cp_step:
                                window_raw_chars += max(0, len(line) - 140)
                            in_progress_turn = True
                            tname, tsummary, fpath, lrange, esnips, call_step, call_epoch = pending_call_queue.popleft()
                            cur_batch_summaries.append([tsummary, "ok", ""])
                            if fpath:
                                _record_path_touch_epoch(
                                    path_touch_epoch,
                                    fpath,
                                    call_step,
                                    call_epoch,
                                    _extract_fast_created_at(hdr),
                                    _extract_completed_at_epoch(hdr_chk),
                                )
                                if is_skill_or_spec(fpath):
                                    _record_skill_spec_touch(
                                        skill_spec_touch, fpath, call_step, tname in WRITE_TOOL_NAMES
                                    )
                                elif tname in WRITE_TOOL_NAMES:
                                    _record_modified_touch(
                                        modified_touch, fpath, call_step, esnips, prev_cp_step=prev_cp_step
                                    )
                                elif tname in READ_TOOL_NAMES:
                                    _record_read_touch(read_touch, fpath, call_step, lrange)
                            if not pending_call_queue:
                                _flush_step_batch(cur_batch_summaries, pending_tools)
                            continue

                try:
                    obj = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if not isinstance(obj, dict):
                    continue
                step_idx = obj.get("step_index", -1)
                if not isinstance(step_idx, int) or isinstance(step_idx, bool) or step_idx < seg_min:
                    continue
                if step_idx > seg_max:
                    break
                stype = obj.get("type")
                if step_idx > prev_cp_step and stype not in ("CHECKPOINT", "SYSTEM_MESSAGE"):
                    content_len = len(str(obj.get("content") or ""))
                    window_raw_chars += content_len
                    if obj.get("tool_calls"):
                        window_raw_chars += max(0, len(line) - content_len - 180)
                if stype == "USER_INPUT":
                    raw_media = obj.get("media")
                    if isinstance(raw_media, list):
                        for m_item in raw_media:
                            if isinstance(m_item, dict):
                                m_uri = str(m_item.get("uri") or "").strip()
                                if m_uri:
                                    media_step_by_name[_fast_basename(m_uri).lower()] = step_idx
                    utext = extract_user_text(str(obj.get("content") or ""))
                    if utext and not is_compact_slash_cmd(utext):
                        pending_call_queue.clear()
                        turn_running_task_ids.clear()
                        _flush_step_batch(cur_batch_summaries, pending_tools)
                        _flush_uncompleted_assistants()
                        if pending_tools:
                            if (
                                dialogue_entries
                                and dialogue_entries[-1][1] == "assistant"
                                and dialogue_entries[-1][0] > last_user_step
                            ):
                                p_step, p_role, p_batches, p_body = dialogue_entries[-1]
                                dialogue_entries[-1] = (p_step, p_role, list(p_batches) + list(pending_tools), p_body)
                            else:
                                interrupted_labels = _format_batch_labels(pending_tools)
                                if interrupted_labels:
                                    dialogue_entries.append(
                                        (
                                            max(0, step_idx - 1),
                                            "in_progress",
                                            [],
                                            f"[Interrupted Turn Tools]: {_summarize_tool_chain(interrupted_labels, max_items=6)}",
                                        )
                                    )
                            pending_tools.clear()
                        last_user_step = step_idx
                        user_turn_steps.append(step_idx)
                        in_progress_turn = True
                        user_messages.append((step_idx, utext, seg_path, line_no))
                        dialogue_entries.append((step_idx, "user", [], utext))
                elif stype == "PLANNER_RESPONSE":
                    pending_call_queue.clear()
                    _flush_step_batch(cur_batch_summaries, pending_tools)
                    mcontent = strip_nested_rehydration(str(obj.get("content") or "").strip())
                    tcs = obj.get("tool_calls") or []
                    valid_tcs = [tc for tc in tcs if isinstance(tc, dict)]
                    if valid_tcs:
                        in_progress_turn = True
                        call_ep = parse_iso_epoch(obj.get("created_at"))
                        if mcontent:
                            turn_buffered_assistants.append((step_idx, list(pending_tools), mcontent))
                            pending_tools.clear()
                        for tc in valid_tcs:
                            tname, tsummary, fpath, lrange, esnips = format_single_tool_call(tc)
                            pending_call_queue.append((tname, tsummary, fpath, lrange, esnips, step_idx, call_ep))
                    elif mcontent:
                        in_progress_turn = False
                        merged_batches = [b for _, bs, _ in turn_buffered_assistants for b in bs] + list(pending_tools)
                        turn_buffered_assistants.clear()
                        pending_tools.clear()
                        if (
                            dialogue_entries
                            and dialogue_entries[-1][1] == "assistant"
                            and dialogue_entries[-1][0] > last_user_step
                        ):
                            _, _, prev_batches, prev_body = dialogue_entries.pop()
                            merged_batches = list(prev_batches) + merged_batches
                            if prev_body and prev_body not in mcontent:
                                mcontent = f"{prev_body}\n\n[Follow-up @ step {step_idx}]:\n{mcontent}"
                        dialogue_entries.append((step_idx, "assistant", merged_batches, mcontent))
                elif stype == "SYSTEM_MESSAGE" and running_by_task_id:
                    _resolve_running_task_from_system_msg(
                        str(obj.get("content") or ""), running_by_task_id, turn_running_task_ids
                    )
                elif stype not in ("CHECKPOINT", "SYSTEM_MESSAGE") and pending_call_queue:
                    in_progress_turn = True
                    tname, tsummary, fpath, lrange, esnips, call_step, call_epoch = pending_call_queue.popleft()
                    tc_state, task_id = classify_tool_step(obj)
                    if tc_state == "ok":
                        if tname == "ask_question":
                            aq_exchange = format_ask_question_exchange(esnips, str(obj.get("content") or ""))
                            if aq_exchange:
                                tsummary = f"ask_question({_clip_inline_label(aq_exchange, 140)})"
                                user_messages.append((step_idx, f"[ask_question] {aq_exchange}", seg_path, line_no))
                        cur_batch_summaries.append([tsummary, "ok", ""])
                        if fpath:
                            _record_path_touch_epoch(
                                path_touch_epoch,
                                fpath,
                                call_step,
                                call_epoch,
                                parse_iso_epoch(obj.get("created_at")),
                                _extract_completed_at_epoch(str(obj.get("content") or "")),
                            )
                            if is_skill_or_spec(fpath):
                                _record_skill_spec_touch(
                                    skill_spec_touch, fpath, call_step, tname in WRITE_TOOL_NAMES
                                )
                            elif tname in WRITE_TOOL_NAMES:
                                _record_modified_touch(
                                    modified_touch, fpath, call_step, esnips, prev_cp_step=prev_cp_step
                                )
                            elif tname in READ_TOOL_NAMES:
                                _record_read_touch(read_touch, fpath, call_step, lrange)
                    elif tc_state == "running":
                        rec = [tsummary, "running", task_id]
                        cur_batch_summaries.append(rec)
                        if task_id:
                            running_by_task_id.setdefault(task_id, []).append(rec)
                            turn_running_task_ids.add(task_id)
                    else:
                        cur_batch_summaries.append([tsummary, "error", ""])
                    if not pending_call_queue:
                        _flush_step_batch(cur_batch_summaries, pending_tools)

    if turn_running_task_ids:
        in_progress_turn = True
    if simulated or delayed_post_cp:
        in_progress_turn = False

    _flush_step_batch(cur_batch_summaries, pending_tools)
    _flush_uncompleted_assistants()
    if pending_tools and not delayed_post_cp:
        in_prog_labels = _format_batch_labels(pending_tools)
        if in_prog_labels:
            dialogue_entries.append(
                (
                    target_cp - 1,
                    "in_progress",
                    [],
                    f"[Pre-Compaction In-Progress Tools]: {_summarize_tool_chain(in_prog_labels, max_items=6)}",
                )
            )

    two_turn_floor = (
        user_turn_steps[-2] if len(user_turn_steps) >= 2 else (user_turn_steps[-1] if user_turn_steps else -1)
    )
    four_turn_floor = (
        user_turn_steps[-4] if len(user_turn_steps) >= 4 else (user_turn_steps[0] if user_turn_steps else -1)
    )

    def _is_recent_mod_step(st: int, path_str: str = "") -> bool:
        if "maxModifiedAgeSteps" in cfg.explicit_keys:
            return (last_user_step >= 0 and st >= last_user_step) or max(0, target_cp - st) <= cfg.max_modified_age_steps
        if (two_turn_floor >= 0 and st >= two_turn_floor) or max(0, target_cp - st) <= cfg.max_modified_age_steps:
            return True
        if (
            prev_cp_step >= 0
            and st > prev_cp_step
            and four_turn_floor >= 0
            and st >= four_turn_floor
            and max(0, target_cp - st) <= max(120, cfg.max_modified_age_steps * 2)
            and _modified_file_tier(path_str) >= 2
        ):
            return True
        return False

    max_mod_spec_age = max(120, cfg.max_modified_age_steps * 2)
    eff_max_modified_files = (
        max(cfg.max_modified_files, 4)
        if (cfg.adaptive and "maxModifiedFiles" not in cfg.explicit_keys and cfg.max_modified_files > 0)
        else cfg.max_modified_files
    )

    path_resolve_cache: dict[str, Path | None] = {}
    # Allow-lists are always explicit: a scope with no trusted root gets an empty tuple (deny), never "unrestricted"
    ws_allowed_prefixes = _prepare_root_prefixes(workspace_roots, path_resolve_cache)
    scratch_excl_prefixes = (
        _prepare_root_prefixes(scratch_excluded_roots, path_resolve_cache) if scratch_excluded_roots else ()
    )
    skill_allowed_prefixes = _prepare_root_prefixes(
        [*(workspace_roots or ()), *(r for r in skill_extra_roots if r is not None)],
        path_resolve_cache,
    )
    art_allowed_prefixes = _prepare_root_prefixes(
        [*(workspace_roots or ()), *([effective_artifact_dir] if effective_artifact_dir else [])],
        path_resolve_cache,
    )

    eligibility_cache: dict[tuple[str, int], bool] = {}

    def _cached_eligible(path_str: str, extra_mode: int = 0) -> bool:
        ck = (canonical_path_key(path_str), extra_mode)
        hit = eligibility_cache.get(ck)
        if hit is not None:
            return hit
        if extra_mode == 1:
            allowed_pfx = skill_allowed_prefixes
            excl_pfx: tuple[str, ...] = ()
        elif extra_mode == 2:
            allowed_pfx = art_allowed_prefixes
            excl_pfx = ()
        else:
            allowed_pfx = ws_allowed_prefixes
            excl_pfx = scratch_excl_prefixes
        ok = is_eligible_text_file(
            path_str,
            workspace_roots=workspace_roots,
            resolve_cache=path_resolve_cache,
            _allowed_prefixes=allowed_pfx,
            _excluded_prefixes=excl_pfx,
        )
        eligibility_cache[ck] = ok
        return ok

    eligible_skills: list[tuple[str, int, bool]] = []
    for _, (spath, sstep, was_mod) in sorted(skill_spec_touch.items(), key=lambda x: x[1][1], reverse=True):
        age = max(0, target_cp - sstep)
        if age > (max_mod_spec_age if was_mod else DEFAULT_MAX_READ_SKILL_AGE_STEPS):
            continue
        is_skill_md = _fast_basename(spath).upper() == "SKILL.MD"
        if _cached_eligible(spath, 1 if is_skill_md else 2):
            eligible_skills.append((spath, sstep, was_mod))

    def _mod_sort_key(item: tuple[str, tuple[str, int, list[str], int]]) -> tuple[int, int, int, int]:
        _, (p, st, _, edit_cnt) = item
        in_last_turn = last_user_step >= 0 and st >= last_user_step
        in_active_window = (
            in_last_turn
            or (two_turn_floor >= 0 and st >= two_turn_floor)
            or max(0, target_cp - st) <= 35
        )
        tier = _modified_file_tier(p)
        band = 2 if (in_last_turn and tier >= 1) else (1 if in_active_window else 0)
        return (band, tier, edit_cnt, st)

    mod_candidates: list[tuple[str, str, int, tuple[int | None, int | None], list[str]]] = []
    if eff_max_modified_files > 0:
        for key, (p, st, snips, _) in sorted(modified_touch.items(), key=_mod_sort_key, reverse=True):
            if key in skill_spec_touch or not _is_recent_mod_step(st, p):
                continue
            if _cached_eligible(p, 0):
                mod_candidates.append((key, p, st, (None, None), snips))
                if len(mod_candidates) >= eff_max_modified_files:
                    break
    mod_keys = {key for key, _, _, _, _ in mod_candidates}

    raw_read_items = [
        (key, p, st, lr)
        for key, (p, st, lr) in sorted(read_touch.items(), key=lambda x: x[1][1], reverse=True)
        if key not in skill_spec_touch and key not in mod_keys
    ]
    ref_candidates: list[tuple[str, str, int, tuple[int | None, int | None], list[str]]] = []
    if cfg.max_referenced_files > 0:
        for key, p, st, lr in raw_read_items:
            if _cached_eligible(p, 0):
                ref_candidates.append((key, p, st, lr, []))
                if len(ref_candidates) >= cfg.max_referenced_files:
                    break
    elif in_progress_turn and last_user_step >= 0:
        for key, p, st, lr in raw_read_items:
            if st >= last_user_step and _cached_eligible(p, 0):
                ref_candidates.append((key, p, st, lr, []))
                if len(ref_candidates) >= DEFAULT_MAX_IN_PROGRESS_READ_FILES:
                    break
    total_eligible_reads = sum(1 for _, p, _, _ in raw_read_items if _cached_eligible(p, 0))
    skipped_referenced_count = max(0, total_eligible_reads - len(ref_candidates))

    has_recent_mod = bool(mod_candidates) or any(was_mod for _, _, was_mod in eligible_skills)

    if in_progress_turn:
        session_mode = "mid_turn"
    elif has_recent_mod and not delayed_post_cp:
        session_mode = "active_coding"
    else:
        session_mode = "completed_qa"

    cp_attachment_refs = extract_checkpoint_attachment_refs(target_cp_content, media_step_by_name)
    lineage_tag = ""
    if resolved_ancestors:
        chain_parts = [
            f"{str(seg['cid'])[:8]}({seg['min_step']}..{min(int(seg['max_step']), target_cp - 1)})"
            for seg in segments
            if int(seg["min_step"]) <= min(int(seg["max_step"]), target_cp - 1)
        ]
        lineage_tag = f" | fork_lineage={'->'.join(chain_parts)}"
    mode_tag = f" | mode={session_mode}" if cfg.adaptive else ""
    delayed_tag = f" | delayed_gap={delayed_gap}" if delayed_post_cp else ""
    marker_line = f"{MARKER_PREFIX} | checkpoint={target_cp}{mode_tag}{delayed_tag}{lineage_tag}]"

    preamble = (
        "Lossless ground-truth state rehydrated from `transcript_full.jsonl` and live workspace files. "
        "Trust these verbatim records and live slices over `<CONTEXT_SUMMARY>` whenever details differ; "
        "if prior Q&A is already answered with no pending action, ignore `<CONTEXT_SUMMARY>`'s "
        "`RESUMING MY OWN TASK IN PROGRESS` template and answer the user's new message directly."
    )
    if delayed_post_cp:
        preamble += (
            f" Note: Checkpoint {target_cp} occurred {delayed_gap} steps ago (mid-turn auto-compaction); "
            f"pre-checkpoint recent dialogue is omitted so it does not collide with live steps "
            f"{target_cp + 1}..{max_seen_step}."
        )
    if cp_attachment_refs:
        preamble += (
            f" Historical attachments re-mounted by `<CONTEXT_SUMMARY>` ({', '.join(cp_attachment_refs[:6])}) "
            "belong to earlier pre-compaction steps — do NOT treat them as newly uploaded in the current turn."
        )

    eff_recent_entries = cfg.max_recent_entries
    eff_recent_budget = cfg.budget_recent_turns_chars
    eff_user_budget = cfg.budget_all_user_chars
    eff_files_budget = cfg.budget_files_total_chars
    eff_single_file_budget = cfg.budget_single_file_chars
    eff_skills_budget = cfg.budget_skills_specs_chars
    ratio_cap: int | None = None
    frame_reserve = max(720, len(marker_line) + len(preamble) + 320)
    content_pool: int | None = None

    if cfg.adaptive:
        win_base = max(window_raw_chars, 6000)
        if session_mode == "completed_qa":
            ratio_cap = max(2200, min(7200, int(win_base * 0.10)))
            content_pool = max(480, ratio_cap - frame_reserve)
            if "maxRecentEntries" not in cfg.explicit_keys:
                eff_recent_entries = 4
            if "budgetRecentTurnsChars" not in cfg.explicit_keys:
                share = 0.84 if eff_recent_entries > 4 else 0.76
                eff_recent_budget = int(content_pool * share)
        elif session_mode == "active_coding":
            ratio_cap = max(6500, min(22000, int(win_base * 0.20)))
            content_pool = max(2400, ratio_cap - frame_reserve)
            if "maxRecentEntries" not in cfg.explicit_keys:
                eff_recent_entries = 4
            if "budgetRecentTurnsChars" not in cfg.explicit_keys:
                eff_recent_budget = min(7500, int(content_pool * 0.36))
            if "budgetSingleFileChars" not in cfg.explicit_keys:
                eff_single_file_budget = min(cfg.budget_single_file_chars, 6500)
        else:  # mid_turn (256K auto-compaction mid-tool-loop)
            ratio_cap = max(8000, min(32000, int(win_base * 0.24)))
            content_pool = max(3200, ratio_cap - frame_reserve)
            if "maxRecentEntries" not in cfg.explicit_keys:
                eff_recent_entries = 6
            if "budgetRecentTurnsChars" not in cfg.explicit_keys:
                eff_recent_budget = min(14000, int(content_pool * 0.42))

    summary_user_counts = extract_checkpoint_user_norm_counts(target_cp_content) if cfg.adaptive else {}
    truncated_summary_prefixes = extract_checkpoint_truncated_prefixes(target_cp_content) if cfg.adaptive else []
    turn_summary_counts = dict(summary_user_counts)

    kept_turn_pairs: list[tuple[int, str]] = []
    used_turns = 0
    real_turn_count = 0
    seen_latest_assistant = False
    folded_assistant_count = 0
    folded_user_count = 0
    if not delayed_post_cp:
        rev_dialogue = list(reversed(dialogue_entries))
        for rev_idx, (s_idx, role, tool_batches, body) in enumerate(rev_dialogue):
            is_banner = role == "banner"
            if not is_banner and real_turn_count >= eff_recent_entries:
                break
            condense_cand = role == "assistant" and seen_latest_assistant
            condense_u = False
            if role == "user" and not body.startswith("--- [/fork branch ") and turn_summary_counts:
                norm_u = _norm_prompt_for_dedup(body)
                if turn_summary_counts.get(norm_u, 0) > 0:
                    turn_summary_counts[norm_u] -= 1
                    condense_u = True
            tool_pfx = _format_tool_batches(tool_batches) if role == "assistant" else ""

            pair_user_reserve = 0
            if condense_cand and (real_turn_count + 1 < eff_recent_entries) and (rev_idx + 1 < len(rev_dialogue)):
                nxt_role, nxt_body = rev_dialogue[rev_idx + 1][1], rev_dialogue[rev_idx + 1][3]
                if nxt_role == "user":
                    pair_user_reserve = min(400, max(120, len(nxt_body) + 40))
                else:
                    pair_user_reserve = 180
                if eff_recent_entries - real_turn_count > 2 and rev_idx + 2 < len(rev_dialogue):
                    pair_user_reserve += 520
            avail_budget = eff_recent_budget - pair_user_reserve

            if condense_cand:
                # Budget-driven folding: keep earlier assistant reply verbatim whenever remaining budget allows
                rendered, did_fold = _render_dialogue_entry(
                    s_idx,
                    role,
                    tool_pfx,
                    body,
                    condense_assistant=False,
                    condense_user_in_summary=False,
                )
                if used_turns + len(rendered) > avail_budget and len(body) > OLDER_ASSISTANT_FOLD_THRESHOLD_CHARS:
                    rem_for_entry = avail_budget - used_turns
                    prefix_ov = len(f"[Step {s_idx} | Assistant]:\n") + len(tool_pfx) + 220
                    target_body = max(
                        OLDER_ASSISTANT_HEAD_CHARS + OLDER_ASSISTANT_TAIL_CHARS,
                        rem_for_entry - prefix_ov,
                    )
                    dyn_head = max(OLDER_ASSISTANT_HEAD_CHARS, int(target_body * 0.65))
                    dyn_tail = max(OLDER_ASSISTANT_TAIL_CHARS, target_body - dyn_head)
                    rendered, did_fold = _render_dialogue_entry(
                        s_idx,
                        role,
                        tool_pfx,
                        body,
                        condense_assistant=True,
                        condense_user_in_summary=False,
                        assistant_head_chars=dyn_head,
                        assistant_tail_chars=dyn_tail,
                    )
                    if did_fold and used_turns + len(rendered) > avail_budget:
                        tight_head = max(OLDER_ASSISTANT_HEAD_CHARS, int(dyn_head * 0.75))
                        tight_tail = max(OLDER_ASSISTANT_TAIL_CHARS, int(dyn_tail * 0.75))
                        rendered, did_fold = _render_dialogue_entry(
                            s_idx,
                            role,
                            tool_pfx,
                            body,
                            condense_assistant=True,
                            condense_user_in_summary=False,
                            assistant_head_chars=tight_head,
                            assistant_tail_chars=tight_tail,
                        )
            else:
                rendered, did_fold = _render_dialogue_entry(
                    s_idx,
                    role,
                    tool_pfx,
                    body,
                    condense_assistant=False,
                    condense_user_in_summary=condense_u,
                )

            if used_turns + len(rendered) > avail_budget:
                rem_turn_chars = avail_budget - used_turns
                if kept_turn_pairs and rem_turn_chars < (220 if role == "user" else 550):
                    break
                rendered = _clip_markdown_to_budget(rendered, max(220, rem_turn_chars))
            if role == "assistant":
                seen_latest_assistant = True
                if did_fold:
                    folded_assistant_count += 1
            elif role == "user" and did_fold:
                folded_user_count += 1
            kept_turn_pairs.append((s_idx, rendered))
            used_turns += len(rendered) + 2
            if not is_banner:
                real_turn_count += 1
    kept_turn_pairs.reverse()
    while kept_turn_pairs and kept_turn_pairs[0][1].startswith("--- [/fork branch "):
        _, popped = kept_turn_pairs.pop(0)
        used_turns = max(0, used_turns - len(popped) - 2)
    kept_turns = [entry for _, entry in kept_turn_pairs]

    non_banner_steps = [s for s, entry in kept_turn_pairs if not entry.startswith("--- [/fork branch ")]
    verbatim_start_step = min(non_banner_steps) if non_banner_steps else target_cp

    if summary_user_counts:
        rem_counts = dict(summary_user_counts)
        for s_idx, utxt, _, _ in user_messages:
            if s_idx >= verbatim_start_step and not utxt.startswith("--- [/fork branch "):
                norm_u = _norm_prompt_for_dedup(utxt)
                if rem_counts.get(norm_u, 0) > 0:
                    rem_counts[norm_u] -= 1
    else:
        rem_counts = {}

    earlier_rev: list[tuple[int, str, Path | None, int]] = []
    deduped_summary_prompts = 0
    for s_idx, utxt, spath, lno in reversed(user_messages):
        if s_idx >= verbatim_start_step:
            continue
        if not utxt.startswith("--- [/fork branch ") and rem_counts:
            norm_u = _norm_prompt_for_dedup(utxt)
            if rem_counts.get(norm_u, 0) > 0:
                rem_counts[norm_u] -= 1
                deduped_summary_prompts += 1
                continue
        earlier_rev.append((s_idx, utxt, spath, lno))
    earlier_rev.reverse()
    earlier_user_messages = earlier_rev
    while earlier_user_messages and earlier_user_messages[-1][1].startswith("--- [/fork branch "):
        earlier_user_messages.pop()
    while earlier_user_messages and earlier_user_messages[0][1].startswith("--- [/fork branch "):
        if len(earlier_user_messages) == 1 or earlier_user_messages[1][1].startswith("--- [/fork branch "):
            earlier_user_messages.pop(0)
        else:
            break

    if content_pool is None:
        rem_pool = None
    elif "budgetRecentTurnsChars" in cfg.explicit_keys:
        rem_pool = content_pool
    else:
        rem_pool = max(0, content_pool - used_turns)
    will_have_skills = bool(eligible_skills) and (
        "budgetSkillsSpecsChars" in cfg.explicit_keys or session_mode != "completed_qa"
    )
    will_have_files = bool(mod_candidates or ref_candidates) and (
        "budgetFilesTotalChars" in cfg.explicit_keys
        or session_mode != "completed_qa"
        or cfg.max_referenced_files > 0
    )

    if rem_pool is not None and "budgetAllUserChars" not in cfg.explicit_keys:
        eff_user_budget = min(
            cfg.budget_all_user_chars,
            int(rem_pool * 0.25) if (will_have_skills or will_have_files) else rem_pool,
        )

    total_real_users = sum(1 for _, txt, _, _ in user_messages if not txt.startswith("--- [/fork branch "))
    earlier_budget_truncated = False
    if eff_user_budget >= 200:
        (
            earlier_user_block,
            earlier_real_count,
            kept_earlier_count,
            filtered_short_count,
            folded_paste_count,
            earlier_budget_truncated,
        ) = _format_user_timeline_ex(
            earlier_user_messages,
            budget_chars=eff_user_budget,
            truncated_prefixes=truncated_summary_prefixes,
            prev_cp_step=prev_cp_step,
        )
    else:
        earlier_user_block = ""
        earlier_real_count = sum(1 for _, txt, _, _ in earlier_user_messages if not txt.startswith("--- [/fork branch "))
        kept_earlier_count = 0
        filtered_short_count = 0
        folded_paste_count = 0

    if rem_pool is not None and "budgetAllUserChars" not in cfg.explicit_keys and earlier_user_block:
        rem_pool = max(0, rem_pool - len(earlier_user_block) - 96)

    milestone_digest = ""
    decayed_milestones_count = 0
    if prev_checkpoints and target_cp_content:
        if rem_pool is None:
            ms_budget = 1400
        elif will_have_skills or will_have_files:
            ms_budget = min(1200, int(rem_pool * 0.18))
        else:
            ms_budget = min(1400, int(rem_pool * 0.45))
        if ms_budget >= 240:
            milestone_digest, decayed_milestones_count = build_anti_decay_milestone_digest(
                prev_checkpoints,
                target_cp_content,
                full_path,
                budget_chars=ms_budget,
            )
            if rem_pool is not None and milestone_digest:
                rem_pool = max(0, rem_pool - len(milestone_digest) - 64)

    if rem_pool is not None and "budgetSkillsSpecsChars" not in cfg.explicit_keys:
        if session_mode == "completed_qa":
            eff_skills_budget = 0
        else:
            eff_skills_budget = min(
                cfg.budget_skills_specs_chars,
                int(rem_pool * 0.35) if will_have_files else rem_pool,
            )

    skill_blocks: list[str] = []
    skill_block_entries: list[tuple[str, str]] = []
    used_skills = 0
    if eff_skills_budget >= 800:
        for spath, sstep, _ in eligible_skills:
            rem = eff_skills_budget - used_skills
            if rem < 800:
                break
            header_overhead = len(spath) + 48
            content = read_text_clipped(spath, min(eff_single_file_budget, max(200, rem - header_overhead)))
            if content:
                block_hdr = f"### `{spath}` (touched @ step {sstep})"
                block = f"{block_hdr}\n```\n{content}\n```"
                skill_blocks.append(block)
                skill_block_entries.append((block_hdr, spath))
                used_skills += len(block) + 2

    if rem_pool is not None and "budgetSkillsSpecsChars" not in cfg.explicit_keys and skill_blocks:
        rem_pool = max(0, rem_pool - used_skills - 56)

    pre_ws_notice = ""
    pre_ws_counts: dict[str, int] | None = None
    pre_injected_keys: set[str] | None = None
    ws_reserve = 0
    if (
        workspace_roots
        and rem_pool is not None
        and will_have_files
        and "budgetFilesTotalChars" not in cfg.explicit_keys
        and rem_pool >= 400
    ):
        pre_injected_keys = {fkey for fkey, *_ in mod_candidates} | {fkey for fkey, *_ in ref_candidates}
        pre_ws_cap = min(1200, max(240, rem_pool // 3))
        pre_ws_notice, pre_ws_counts = build_workspace_reality_notice(
            modified_touch=modified_touch,
            read_touch=read_touch,
            path_touch_epoch=path_touch_epoch,
            target_cp_content=target_cp_content,
            workspace_roots=workspace_roots,
            injected_file_keys=pre_injected_keys,
            excluded_roots=scratch_excluded_roots,
            max_chars=pre_ws_cap,
            resolve_cache=path_resolve_cache,
        )
        if pre_ws_notice:
            ws_reserve = len(pre_ws_notice) + 52

    if rem_pool is not None and "budgetFilesTotalChars" not in cfg.explicit_keys:
        if session_mode == "completed_qa" and cfg.max_referenced_files == 0:
            eff_files_budget = 0
        else:
            eff_files_budget = min(cfg.budget_files_total_chars, max(0, rem_pool - 64 - ws_reserve))

    file_blocks: list[str] = []
    file_block_entries: list[tuple[str, str]] = []
    injected_file_keys: set[str] = set()
    used_files = 0
    if eff_files_budget >= 800:
        ordered_file_items: list[tuple[str, str, str, int, tuple[int | None, int | None], list[str], float]] = []
        for role_label, items in (("Modified", mod_candidates), ("Referenced", ref_candidates)):
            for fkey, fpath, fstep, lrange, snips in items:
                if role_label == "Modified":
                    ftier = _modified_file_tier(fpath)
                    weight = 1.25 if ftier >= 2 else (0.90 if ftier == 1 else 0.70)
                else:
                    weight = 0.85
                ordered_file_items.append((role_label, fkey, fpath, fstep, lrange, snips, weight))

        use_balanced_share = (
            cfg.adaptive and "budgetSingleFileChars" not in cfg.explicit_keys and len(ordered_file_items) >= 2
        )
        for idx_f, (role_label, fkey, fpath, fstep, lrange, snips, cur_weight) in enumerate(ordered_file_items):
            rem = eff_files_budget - used_files
            if rem < 800:
                break
            header_overhead = len(fpath) + 120
            file_cap = eff_single_file_budget
            if use_balanced_share:
                max_fit = max(1, rem // 1100)
                active_rem = ordered_file_items[idx_f : idx_f + max_fit]
                if len(active_rem) >= 2:
                    total_w = sum(it[6] for it in active_rem)
                    fair_share = int(rem * (cur_weight / total_w))
                    reserve_rest = min(max(0, rem - 1200), 1400 * (len(active_rem) - 1))
                    file_cap = min(
                        eff_single_file_budget,
                        max(1200, min(int(fair_share * 1.15), rem - reserve_rest)),
                    )
            max_slice = min(file_cap, max(200, rem - header_overhead))
            if role_label == "Referenced":
                content, range_note = read_scoped_text_clipped(fpath, max_slice, lrange)
            elif cfg.adaptive:
                hunk_ctx = 12 if _modified_file_tier(fpath) == 0 else 45
                content, range_note = read_modified_hunk_window(fpath, snips, max_slice, context_lines=hunk_ctx)
            else:
                content, range_note = read_text_clipped(fpath, max_slice), ""
            if content:
                block_hdr = f"### [{role_label} @ step {fstep}{range_note}] `{fpath}`"
                block = f"{block_hdr}\n```\n{content}\n```"
                file_blocks.append(block)
                file_block_entries.append((block_hdr, f"{role_label}:{fpath}"))
                injected_file_keys.add(fkey)
                used_files += len(block) + 2

    if pre_injected_keys is not None and pre_ws_counts is not None and injected_file_keys == pre_injected_keys:
        workspace_notice, ws_notice_counts = pre_ws_notice, pre_ws_counts
    else:
        files_sec_used = (used_files + 64) if file_blocks else 0
        if rem_pool is None:
            ws_notice_budget = 1200
        else:
            extra_floor = (
                360
                if (
                    rem_pool >= 1200
                    and pre_injected_keys is not None
                    and len(injected_file_keys) < len(pre_injected_keys)
                )
                else 0
            )
            ws_notice_budget = min(
                1200,
                max(len(pre_ws_notice), extra_floor, max(0, rem_pool - files_sec_used)),
            )
        workspace_notice, ws_notice_counts = build_workspace_reality_notice(
            modified_touch=modified_touch,
            read_touch=read_touch,
            path_touch_epoch=path_touch_epoch,
            target_cp_content=target_cp_content,
            workspace_roots=workspace_roots,
            injected_file_keys=injected_file_keys,
            excluded_roots=scratch_excluded_roots,
            max_chars=ws_notice_budget,
            resolve_cache=path_resolve_cache,
        )

    # Silent bypass for trivial greetings/Q&A already 100% covered by native CHECKPOINT
    if (
        cfg.adaptive
        and not simulated
        and not delayed_post_cp
        and session_mode == "completed_qa"
        and not resolved_ancestors
        and not cp_attachment_refs
        and earlier_real_count == 0
        and not milestone_digest
        and not workspace_notice
        and not skill_blocks
        and not file_blocks
        and sum(len(t) for t in kept_turns) < 160
        and all("(Tools run:" not in t for t in kept_turns)
    ):
        return None

    numbered_sections: list[tuple[str, str]] = []
    if earlier_real_count > 0 and earlier_user_block:
        delta_note = ", delta beyond <CONTEXT_SUMMARY>" if deduped_summary_prompts > 0 else ""
        if (
            filtered_short_count > 0
            or folded_paste_count > 0
            or earlier_budget_truncated
            or kept_earlier_count < earlier_real_count
        ):
            sec1_title = (
                f"Earlier User Request Timeline (steps < {verbatim_start_step}; "
                f"{kept_earlier_count}/{earlier_real_count} earlier prompts kept{delta_note})"
            )
        else:
            sec1_title = (
                f"Earlier User Request Timeline (steps < {verbatim_start_step}; "
                f"{earlier_real_count} earlier prompts verbatim{delta_note})"
            )
        numbered_sections.append((sec1_title, earlier_user_block))

    if milestone_digest:
        numbered_sections.append(
            (
                f"Earlier Checkpoints Milestone Index ({decayed_milestones_count} milestones recovered across prior compactions)",
                milestone_digest,
            )
        )

    if kept_turns:
        if (
            session_mode == "completed_qa"
            and len(kept_turns) <= 2
            and folded_assistant_count == 0
            and folded_user_count == 0
        ):
            sec2_title = f"Final Pre-Compaction Exchange ({len(kept_turns)} entries verbatim)"
            sec2_body = "\n\n".join(kept_turns)
        elif folded_assistant_count > 0 or folded_user_count > 0:
            if folded_assistant_count > 0 and folded_user_count > 0:
                sec2_title = (
                    f"Recent Pre-Compaction Dialogue ({len(kept_turns)} entries; "
                    f"latest turn verbatim, {folded_assistant_count} earlier replies & "
                    f"{folded_user_count} duplicate prompt condensed)"
                )
                sec2_note = (
                    "*(Note: Recent unique user prompts, tool trajectories, and the final assistant reply are verbatim; "
                    "duplicate long user prompts and middle paragraphs of earlier assistant replies are condensed "
                    "because `<CONTEXT_SUMMARY>` already preserves them.)*"
                )
            elif folded_assistant_count > 0:
                sec2_title = (
                    f"Recent Pre-Compaction Dialogue ({len(kept_turns)} entries; "
                    f"latest turn verbatim, {folded_assistant_count} earlier replies condensed)"
                )
                sec2_note = (
                    "*(Note: User prompts, tool trajectories, and the final assistant reply are verbatim; "
                    "middle paragraphs of earlier assistant replies are condensed because `<CONTEXT_SUMMARY>` "
                    "already summarizes them.)*"
                )
            else:
                sec2_title = (
                    f"Recent Pre-Compaction Dialogue ({len(kept_turns)} entries; "
                    f"duplicate long prompt condensed)"
                )
                sec2_note = (
                    "*(Note: Tool trajectories and replies are verbatim; duplicate long user prompts are condensed "
                    "because `<CONTEXT_SUMMARY>` already preserves them.)*"
                )
            sec2_body = f"{sec2_note}\n\n" + "\n\n".join(kept_turns)
        else:
            sec2_title = f"Recent Pre-Compaction Verbatim Dialogue ({len(kept_turns)} entries kept verbatim)"
            sec2_body = "\n\n".join(kept_turns)
        numbered_sections.append((sec2_title, sec2_body))

    if workspace_notice:
        numbered_sections.append(("Workspace Reality & File Status Notice", workspace_notice))
    if skill_blocks:
        numbered_sections.append(("Restored Active Skills & Spec Docs", "\n\n".join(skill_blocks)))
    if file_blocks:
        numbered_sections.append(("Restored Active Workspace Files (Live Disk Read)", "\n\n".join(file_blocks)))

    if not numbered_sections:
        return None

    sections = [
        marker_line,
        preamble,
        *[f"## {idx}. {title}\n{body}" for idx, (title, body) in enumerate(numbered_sections, 1)],
    ]

    explicit_budget_keys = {
        "budgetAllUserChars",
        "budgetRecentTurnsChars",
        "budgetSkillsSpecsChars",
        "budgetFilesTotalChars",
        "budgetSingleFileChars",
    }
    if cfg.adaptive and ratio_cap is not None and not (cfg.explicit_keys & explicit_budget_keys):
        sections = _enforce_total_cap(sections, ratio_cap)

    # Reconcile skill/file stats with surviving sections after `_enforce_total_cap`
    surviving_skill_sec = next(
        (s for s in sections[2:] if s.partition("\n")[0].endswith(". Restored Active Skills & Spec Docs")),
        None,
    )
    surviving_file_sec = next(
        (s for s in sections[2:] if s.partition("\n")[0].endswith(". Restored Active Workspace Files (Live Disk Read)")),
        None,
    )
    surviving_skill_lines = set(surviving_skill_sec.splitlines()) if surviving_skill_sec is not None else set()
    surviving_file_lines = set(surviving_file_sec.splitlines()) if surviving_file_sec is not None else set()
    restored_skill_paths = [sp for hdr, sp in skill_block_entries if hdr in surviving_skill_lines]
    restored_file_paths = [fp for hdr, fp in file_block_entries if hdr in surviving_file_lines]

    full_msg = "\n\n".join(sections)
    chunks = split_into_inject_chunks(sections, max_bytes=MAX_INJECT_STEP_BYTES, checkpoint=target_cp)
    return full_msg, {
        "checkpoint": target_cp,
        "simulated": simulated,
        "delayed_post_cp": delayed_post_cp,
        "delayed_gap": delayed_gap,
        "adaptive": cfg.adaptive,
        "session_mode": session_mode,
        "window_raw_chars": window_raw_chars,
        "ratio_cap": ratio_cap,
        "in_progress_turn": in_progress_turn,
        "fork_hops": len(resolved_ancestors),
        "user_msgs": total_real_users,
        "deduped_summary_user_msgs": deduped_summary_prompts,
        "earlier_user_msgs": earlier_real_count,
        "kept_earlier_user_msgs": kept_earlier_count,
        "filtered_short_msgs": filtered_short_count,
        "folded_paste_msgs": folded_paste_count,
        "decayed_milestones": decayed_milestones_count,
        "kept_turns": len(kept_turns),
        "folded_assistant_turns": folded_assistant_count,
        "folded_user_turns": folded_user_count,
        "verbatim_start_step": verbatim_start_step,
        "stale_paths": ws_notice_counts["stale_paths"],
        "externally_modified_files": ws_notice_counts["externally_modified_files"],
        "uninjected_modified_files": ws_notice_counts["uninjected_modified_files"],
        "skills": len(restored_skill_paths),
        "skill_paths": restored_skill_paths,
        "files": len(restored_file_paths),
        "file_paths": restored_file_paths,
        "skipped_referenced_files": skipped_referenced_count,
        "sections": max(0, len(sections) - 2),
        "chars": len(full_msg),
        "utf8_bytes": len(full_msg.encode("utf-8")),
        "inject_chunks": len(chunks),
        "chunks": chunks,
        "est_tokens": len(full_msg) // 3,
    }


def _enforce_total_cap(sections: list[str], cap: int) -> list[str]:
    """Guarantee `len('\\n\\n'.join(sections)) <= cap` by trimming trailing section bodies cleanly."""
    joined_len = sum(len(s) for s in sections) + 2 * max(0, len(sections) - 1)
    if joined_len <= cap or len(sections) <= 2:
        return sections
    out = list(sections)
    idx = len(out) - 1
    while idx >= 2:
        joined_len = sum(len(s) for s in out) + 2 * max(0, len(out) - 1)
        overflow = joined_len - cap
        if overflow <= 0:
            break
        hdr, sep, body = out[idx].partition("\n")
        if sep and len(body) - overflow >= 220:
            target_body_len = len(body) - overflow
            # For multi-file sections, drop trailing whole file blocks first so surviving code blocks stay intact
            if "Restored Active Workspace Files" in hdr or "Restored Active Skills & Spec Docs" in hdr:
                sub_items = _split_section_fence_aware(body)
                if len(sub_items) >= 2:
                    trimmed_items = list(sub_items)
                    while len(trimmed_items) > 1 and len("\n\n".join(trimmed_items)) > target_body_len:
                        trimmed_items.pop()
                    cand_body = "\n\n".join(trimmed_items)
                    if len(cand_body) <= target_body_len:
                        out[idx] = f"{hdr}\n{cand_body}"
                        break
                    body = cand_body
            elif "Workspace Reality & File Status Notice" in hdr:
                kept_ws = []
                used_ws = 0
                for ln in body.splitlines():
                    sep_w = 1 if kept_ws else 0
                    if used_ws + sep_w + len(ln) <= target_body_len:
                        kept_ws.append(ln)
                        used_ws += sep_w + len(ln)
                if kept_ws:
                    out[idx] = f"{hdr}\n" + "\n".join(kept_ws)
                    break
            clipped_body = _clip_markdown_to_budget(body, target_body_len)
            out[idx] = f"{hdr}\n{clipped_body}"
            break
        if len(out) > 3:
            out.pop(idx)
            idx -= 1
            continue
        if sep and body:
            keep = max(80, len(body) - overflow)
            sub = body[:keep]
            if sum(1 for ln in sub.splitlines() if ln.lstrip().startswith("```")) % 2 == 1 and keep >= 4:
                sub2 = body[: keep - 4].rstrip("\r\n")
                if sum(1 for ln in sub2.splitlines() if ln.lstrip().startswith("```")) % 2 == 1:
                    sub = f"{sub2}\n```"
                else:
                    sub = sub2
            out[idx] = f"{hdr}\n{sub}"
        break
    return out


_SECTION_SUBITEM_PREFIXES = (
    "### ",
    "[Step ",
    "[Pre-Compaction ",
    "[Interrupted Turn ",
    "--- [/fork ",
)


def _split_section_fence_aware(sec: str) -> list[str]:
    """Split a section on blank-line sub-item boundaries only when outside fenced code blocks."""
    lines = sec.split("\n")
    items: list[str] = []
    cur: list[str] = []
    in_fence = False
    i = 0
    n = len(lines)
    while i < n:
        ln = lines[i]
        if (
            not in_fence
            and ln == ""
            and cur
            and i + 1 < n
            and lines[i + 1].startswith(_SECTION_SUBITEM_PREFIXES)
        ):
            items.append("\n".join(cur))
            cur = []
            i += 1
            continue
        cur.append(ln)
        if ln.lstrip().startswith("```"):
            in_fence = not in_fence
        i += 1
    if cur:
        items.append("\n".join(cur))
    return items


def _split_paragraphs_fence_aware(item: str) -> list[str]:
    """Split on blank lines only outside fenced code blocks so code blocks stay atomic unless oversized."""
    lines = item.split("\n")
    paras: list[str] = []
    cur: list[str] = []
    in_fence = False
    for ln in lines:
        if not in_fence and ln == "":
            if cur:
                paras.append("\n".join(cur))
                cur = []
            continue
        cur.append(ln)
        if ln.lstrip().startswith("```"):
            in_fence = not in_fence
    if cur:
        paras.append("\n".join(cur))
    return paras


def _fence_opener_label(line: str, max_bytes: int) -> str:
    stripped = line.lstrip()
    cap = max(3, min(40, max_bytes // 4))
    if len(stripped.encode("utf-8")) <= cap:
        return stripped
    return "```"


def _split_oversized_block(text: str, max_bytes: int) -> list[str]:
    """Split an oversized text/code block into `<= max_bytes` UTF-8 pieces with balanced triple-backtick fences."""
    if len(text.encode("utf-8")) <= max_bytes:
        return [text]
    pieces: list[str] = []
    cur_lines: list[str] = []
    cur_bytes = 0
    open_fence: str | None = None

    def _flush_piece(reopen_next: bool) -> None:
        nonlocal cur_lines, cur_bytes
        if not cur_lines:
            return
        if open_fence is not None and len(cur_lines) == 1 and cur_lines[0] == open_fence:
            if not reopen_next:
                cur_lines = []
                cur_bytes = 0
            return
        if open_fence is not None:
            if reopen_next and len(cur_lines) >= 2 and cur_lines[-1] == open_fence:
                cur_lines.pop()
            else:
                cur_lines.append("```")
        pieces.append("\n".join(cur_lines))
        if reopen_next and open_fence is not None:
            cur_lines = [open_fence]
            cur_bytes = len(open_fence.encode("utf-8"))
        else:
            cur_lines = []
            cur_bytes = 0

    for line in text.split("\n"):
        is_fence = line.lstrip().startswith("```")
        lb = len(line.encode("utf-8"))

        if open_fence is not None and is_fence:
            add_b = (1 + lb) if cur_lines else lb
            if cur_lines and cur_bytes + add_b <= max_bytes:
                cur_lines.append(line)
                cur_bytes += add_b
            else:
                cur_lines.append("```")
                cur_bytes += 4 if len(cur_lines) > 1 else 3
            open_fence = None
            continue

        next_fence = _fence_opener_label(line, max_bytes) if (open_fence is None and is_fence) else open_fence
        need_close = 4 if next_fence is not None else 0
        add_b = (1 + lb) if cur_lines else lb

        if open_fence is None and is_fence and cur_lines and (cur_bytes + add_b + need_close + 24 > max_bytes):
            _flush_piece(reopen_next=False)
            add_b = lb

        if cur_bytes + add_b + need_close <= max_bytes:
            cur_lines.append(line)
            cur_bytes += add_b
            open_fence = next_fence
            continue

        if cur_lines and not (open_fence is not None and len(cur_lines) == 1 and cur_lines[0] == open_fence):
            _flush_piece(reopen_next=(open_fence is not None))

        add_b = (1 + lb) if cur_lines else lb
        if cur_bytes + add_b + need_close <= max_bytes:
            cur_lines.append(line)
            cur_bytes += add_b
            open_fence = next_fence
            continue

        if open_fence is None and is_fence:
            norm_fence = _fence_opener_label(line, max_bytes)
            cur_lines = [norm_fence]
            cur_bytes = len(norm_fence.encode("utf-8"))
            open_fence = norm_fence
            continue

        buf: list[str] = []
        bb = 0
        overhead = cur_bytes + (1 if cur_lines else 0) + (4 if open_fence is not None else 0)
        part_limit = max(4, max_bytes - overhead)
        for ch in line:
            cb = len(ch.encode("utf-8"))
            if bb + cb > part_limit and buf:
                cur_lines.append("".join(buf))
                cur_bytes += (1 if len(cur_lines) > 1 else 0) + bb
                _flush_piece(reopen_next=(open_fence is not None))
                buf, bb = [], 0
                overhead = cur_bytes + (1 if cur_lines else 0) + (4 if open_fence is not None else 0)
                part_limit = max(4, max_bytes - overhead)
            buf.append(ch)
            bb += cb
        if buf:
            cur_lines.append("".join(buf))
            cur_bytes += (1 if len(cur_lines) > 1 else 0) + bb

    _flush_piece(reopen_next=False)
    return pieces


def split_into_inject_chunks(
    sections: list[str],
    max_bytes: int = MAX_INJECT_STEP_BYTES,
    checkpoint: int | None = None,
) -> list[str]:
    """Pack sections into <=max_bytes UTF-8 chunks with balanced code fences across chunk boundaries."""
    cp_note = f" | checkpoint={checkpoint}" if checkpoint is not None else ""
    header_reserve = len(f"{CONT_MARKER_PREFIX} 9999/9999{cp_note}]\n\n".encode("utf-8"))
    limit = max(32, max_bytes - header_reserve)
    atomic_blocks: list[str] = []
    for sec in sections:
        if len(sec.encode("utf-8")) <= limit:
            atomic_blocks.append(sec)
        else:
            for item in _split_section_fence_aware(sec):
                if len(item.encode("utf-8")) <= limit:
                    atomic_blocks.append(item)
                else:
                    for para in _split_paragraphs_fence_aware(item):
                        atomic_blocks.extend(_split_oversized_block(para, limit))

    raw_chunks: list[str] = []
    cur_parts: list[str] = []
    cur_bytes = 0
    for blk in atomic_blocks:
        bb = len(blk.encode("utf-8"))
        sep = 2 if cur_parts else 0
        if cur_parts and cur_bytes + sep + bb > limit:
            raw_chunks.append("\n\n".join(cur_parts))
            cur_parts = [blk]
            cur_bytes = bb
        else:
            cur_parts.append(blk)
            cur_bytes += sep + bb
    if cur_parts:
        raw_chunks.append("\n\n".join(cur_parts))

    if len(raw_chunks) <= 1:
        return raw_chunks

    total = len(raw_chunks)
    tagged: list[str] = []
    for idx, ch in enumerate(raw_chunks, 1):
        if idx == 1:
            tagged.append(ch)
        else:
            tagged.append(f"{CONT_MARKER_PREFIX} {idx}/{total}{cp_note}]\n\n{ch}")
    return tagged


def save_state(
    state_path: Path | str,
    cp: int | None,
    offset: int,
    fork_ancestors: list[dict[str, Any]] | None = None,
) -> None:
    _save_state_str(str(state_path), cp, offset, fork_ancestors)
