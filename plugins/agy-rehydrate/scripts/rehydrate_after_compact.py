#!/usr/bin/env python3
"""Antigravity PreInvocation hook entrypoint & fast path for `agy-rehydrate`.

On normal non-compaction turns under `python -S -B`, only `json`, `os`, and `sys`
are imported (~1 ms in-process incremental byte-offset tail check returning `{}`).
The 4-layer rehydration core (`rehydrate_core`) and diagnostic CLI (`rehydrate_cli`)
are imported lazily only when a new `CHECKPOINT` or CLI subcommand is invoked.
"""

from __future__ import annotations

import json
import os
import sys

TYPE_CHECKING = False
if TYPE_CHECKING:
    from typing import Any

sys.dont_write_bytecode = True

AGY_PRODUCT_DIRS = ("antigravity", "antigravity-cli", "antigravity-ide")
FRESH_CHECKPOINT_MAX_GAP = 8
_SCRIPTS_DIR_ADDED = False

_CORE_PUBLIC_CONSTANTS = frozenset(
    {
        "AGY_PRODUCT_DIRS",
        "MARKER_PREFIX",
        "DEFAULT_BUDGET_ALL_USER_CHARS",
        "DEFAULT_BUDGET_RECENT_TURNS_CHARS",
        "DEFAULT_BUDGET_SKILLS_SPECS_CHARS",
        "DEFAULT_BUDGET_FILES_TOTAL_CHARS",
        "DEFAULT_BUDGET_SINGLE_FILE_CHARS",
        "DEFAULT_MAX_SINGLE_TURN_CHARS",
        "MAX_CONFIG_BUDGET_CHARS",
        "MAX_CONFIG_FILE_SLOTS",
        "RECENT_USER_VERBATIM_COUNT",
        "SHORT_USER_MAX_CHARS",
        "FOLD_PASTE_THRESHOLD_CHARS",
        "FOLD_PASTE_HEAD_CHARS",
        "FOLD_PASTE_TAIL_CHARS",
        "DEFAULT_MAX_MODIFIED_FILES",
        "DEFAULT_MAX_REFERENCED_FILES",
        "DEFAULT_FILE_SCOPE_SINCE_PREV_CHECKPOINT",
        "MAX_REPO_RULE_FILES",
        "MAX_FORK_DEPTH",
        "FRESH_CHECKPOINT_MAX_GAP",
        "MAX_AUTOCOMPACT_TURN_GAP",
        "MAX_CHILD_TRANSCRIPTS",
        "OVERSIZED_FILE_FACTOR",
        "OVERSIZED_FILE_PREVIEW_CHARS",
        "MAX_INJECT_STEP_BYTES",
        "MAX_FILE_READ_BYTES",
        "REPO_RULE_NAMES",
        "SKIP_SYSTEM_PATH_PREFIXES",
        "READ_TOOL_NAMES",
        "WRITE_TOOL_NAMES",
        "PATH_ARG_KEYS",
        "SAFE_ENV_TEMPLATES",
        "SENSITIVE_BASENAMES",
        "SKIP_EXTS",
        "TEMP_PATH_SUBSTRINGS",
        "SKIP_PATH_SUBSTRINGS",
    }
)


def _ensure_scripts_dir() -> None:
    global _SCRIPTS_DIR_ADDED
    if not _SCRIPTS_DIR_ADDED:
        scripts_dir = os.path.dirname(os.path.abspath(__file__))
        if scripts_dir not in sys.path:
            sys.path.insert(0, scripts_dir)
        if "rehydrate_after_compact" not in sys.modules and __name__ in sys.modules:
            sys.modules["rehydrate_after_compact"] = sys.modules[__name__]
        _SCRIPTS_DIR_ADDED = True


def configure_utf8_stdio() -> None:
    """Force UTF-8 on stdin/stdout/stderr so Windows codepages (cp950/cp936) never corrupt JSON."""
    for stream_name in ("stdin", "stdout", "stderr"):
        stream = getattr(sys, stream_name, None)
        reconfigure = getattr(stream, "reconfigure", None)
        if callable(reconfigure):
            try:
                reconfigure(encoding="utf-8", errors="replace")
            except Exception:
                pass


def _read_stdin_utf8() -> str:
    """Read hook stdin UTF-8/BOM-safely even when invoked directly under non-UTF-8 codepages."""
    stdin = sys.stdin
    if stdin is None:
        return ""
    buf = getattr(stdin, "buffer", None)
    if buf is not None:
        raw = buf.read()
        if isinstance(raw, (bytes, bytearray)):
            return raw.decode("utf-8-sig", "replace")
        return str(raw).lstrip("\ufeff")
    return stdin.read().lstrip("\ufeff")


def _write_stdout(text: str) -> None:
    """Write hook output while suppressing broken-pipe / closed-stream errors for strict Fail-Open."""
    stdout = sys.stdout
    if stdout is None:
        return
    try:
        stdout.write(text)
        stdout.flush()
    except UnicodeEncodeError:
        buf = getattr(stdout, "buffer", None)
        if buf is not None:
            try:
                buf.write(text.encode("utf-8", "replace"))
                buf.flush()
            except (OSError, BrokenPipeError, ValueError):
                pass
    except (OSError, BrokenPipeError, ValueError):
        pass


def _extract_step_index_bytes(raw_line: bytes) -> int:
    """Extract non-negative top-level integer `step_index` from a JSONL line without importing `re`."""
    if b'"step_index"' not in raw_line:
        return -1
    n = len(raw_line)
    i = 3 if raw_line.startswith(b"\xef\xbb\xbf") else 0
    while i < n and raw_line[i] in b" \t\r\n":
        i += 1
    if i >= n or raw_line[i] != 123:  # '{'
        return -1
    i += 1
    while i < n and raw_line[i] in b" \t\r\n":
        i += 1
    if raw_line.startswith(b'"step_index"', i):
        j = i + 12
        while j < n and raw_line[j] in b" \t\r\n":
            j += 1
        if j < n and raw_line[j] == 58:  # ':'
            j += 1
            while j < n and raw_line[j] in b" \t\r\n":
                j += 1
            start = j
            while j < n and 48 <= raw_line[j] <= 57:
                j += 1
            if j > start and (j - start == 1 or raw_line[start] != 48):
                k = j
                while k < n and raw_line[k] in b" \t\r\n":
                    k += 1
                if k < n and raw_line[k] in b",}":
                    return int(raw_line[start:j])
    clean_line = raw_line[3:] if raw_line.startswith(b"\xef\xbb\xbf") else raw_line
    try:
        obj = json.loads(clean_line.decode("utf-8", "replace"))
    except (ValueError, UnicodeError):
        return -1
    if isinstance(obj, dict):
        sidx = obj.get("step_index")
        if isinstance(sidx, int) and not isinstance(sidx, bool) and sidx >= 0:
            return sidx
    return -1


def _find_transcript_files_str(
    payload: dict[str, Any],
    *,
    resolve_full: bool = True,
) -> tuple[str | None, str | None, str | None]:
    """Locate `(transcript.jsonl, transcript_full.jsonl, rehydrate_state.json)` using only `os.path`."""
    tp = payload.get("transcriptPath") or ""
    ad = payload.get("artifactDirectoryPath") or ""
    cid = payload.get("conversationId") or ""

    compact_path: str | None = None
    if isinstance(tp, str):
        tp_s = tp.strip()
        if tp_s and os.path.isfile(tp_s):
            compact_path = tp_s
    if compact_path is None and isinstance(ad, str):
        ad_s = ad.strip()
        if ad_s:
            logs_dir = os.path.join(ad_s, ".system_generated", "logs")
            for fname in ("transcript.jsonl", "transcript_full.jsonl"):
                cand = os.path.join(logs_dir, fname)
                if os.path.isfile(cand):
                    compact_path = cand
                    break
    if compact_path is None and isinstance(cid, str):
        cid_s = cid.strip()
        if (
            cid_s
            and cid_s != "."
            and ".." not in cid_s
            and "/" not in cid_s
            and "\\" not in cid_s
            and ":" not in cid_s
            and "\x00" not in cid_s
        ):
            gemini_root = os.path.join(os.path.expanduser("~"), ".gemini")
            best_mtime = -1.0
            for prod in AGY_PRODUCT_DIRS:
                logs_dir = os.path.join(gemini_root, prod, "brain", cid_s, ".system_generated", "logs")
                for fname in ("transcript.jsonl", "transcript_full.jsonl"):
                    cand = os.path.join(logs_dir, fname)
                    try:
                        st = os.stat(cand)
                        if (st.st_mode & 0o170000) == 0o100000:
                            if st.st_mtime > best_mtime:
                                best_mtime = st.st_mtime
                                compact_path = cand
                            break
                    except OSError:
                        continue

    if compact_path is None:
        return None, None, None
    logs_dir = os.path.dirname(compact_path)
    if resolve_full:
        full_cand = os.path.join(logs_dir, "transcript_full.jsonl")
        full_path = full_cand if os.path.isfile(full_cand) else compact_path
    else:
        full_path = compact_path
    return compact_path, full_path, os.path.join(logs_dir, "rehydrate_state.json")


def scan_compact_tail(
    compact_path: str | os.PathLike[str],
    prev_offset: int = 0,
    prev_cp: int | None = None,
    *,
    need_max_step: bool = True,
) -> tuple[int | None, int, int]:
    """Incrementally scan newly appended JSONL bytes for `CHECKPOINT` records."""
    path_s = os.fspath(compact_path)
    with open(path_s, "rb") as fb:
        fb.seek(0, os.SEEK_END)
        st_size = fb.tell()
        if not need_max_step and st_size == prev_offset and prev_offset > 0:
            return prev_cp, -1, st_size

        use_incremental = False
        if 0 < prev_offset < st_size:
            fb.seek(prev_offset - 1)
            use_incremental = fb.read(1) == b"\n"
        if not use_incremental:
            fb.seek(0)

        scan_start = prev_offset if use_incremental else 0
        latest_cp = prev_cp if use_incremental else None
        max_step = -1

        if not need_max_step:
            delta = st_size - scan_start
            if delta <= 1_048_576:
                tail = fb.read(delta)
                end_offset = fb.tell()
                if b'"CHECKPOINT"' not in tail:
                    return latest_cp, -1, end_offset
                for raw_line in tail.split(b"\n"):
                    if b'"CHECKPOINT"' in raw_line:
                        clean_line = raw_line[3:] if raw_line.startswith(b"\xef\xbb\xbf") else raw_line
                        try:
                            obj = json.loads(clean_line.decode("utf-8", "replace"))
                            if isinstance(obj, dict) and obj.get("type") == "CHECKPOINT":
                                sidx = obj.get("step_index", -1)
                                if (
                                    isinstance(sidx, int)
                                    and not isinstance(sidx, bool)
                                    and sidx >= 0
                                    and (latest_cp is None or sidx > latest_cp)
                                ):
                                    latest_cp = sidx
                        except json.JSONDecodeError:
                            pass
                return latest_cp, -1, end_offset
            has_cp_marker = False
            overlap = b""
            while True:
                chunk = fb.read(262_144)
                if not chunk:
                    break
                if b'"CHECKPOINT"' in (overlap + chunk):
                    has_cp_marker = True
                    break
                overlap = chunk[-16:]
            if not has_cp_marker:
                return latest_cp, -1, fb.tell()
            fb.seek(scan_start)

        for raw_line in fb:
            if not raw_line or raw_line.isspace():
                continue
            if b'"CHECKPOINT"' in raw_line:
                clean_line = raw_line[3:] if raw_line.startswith(b"\xef\xbb\xbf") else raw_line
                try:
                    obj = json.loads(clean_line.decode("utf-8", "replace"))
                    if isinstance(obj, dict):
                        sidx = obj.get("step_index", -1)
                        if isinstance(sidx, int) and not isinstance(sidx, bool) and sidx >= 0:
                            if need_max_step and sidx > max_step:
                                max_step = sidx
                            if obj.get("type") == "CHECKPOINT" and (latest_cp is None or sidx > latest_cp):
                                latest_cp = sidx
                except json.JSONDecodeError:
                    pass
            elif need_max_step:
                sidx = _extract_step_index_bytes(raw_line)
                if sidx > max_step:
                    max_step = sidx
        return latest_cp, max_step, fb.tell()


def _save_state_str(
    state_path: str,
    cp: int | None,
    offset: int,
    fork_ancestors: list[dict[str, Any]] | None = None,
    last_status: str | None = None,
) -> None:
    tmp_path = f"{state_path}.{os.getpid()}.tmp"
    try:
        data: dict[str, Any] = {"last_rehydrated_checkpoint": cp, "checked_offset": offset}
        if fork_ancestors:
            data["fork_ancestors"] = fork_ancestors
        if last_status:
            data["last_status"] = last_status
        with open(tmp_path, "w", encoding="utf-8") as f:
            f.write(json.dumps(data, indent=2))
        for attempt in range(4):
            try:
                os.replace(tmp_path, state_path)
                return
            except PermissionError:
                if attempt >= 3:
                    raise
                import time

                time.sleep(0.02 * (attempt + 1))
    except OSError:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass


def run_hook() -> int:
    """Execute `PreInvocation` hook contract (strict Fail-Open `{}` on non-trigger or error)."""
    try:
        raw_in = _read_stdin_utf8()
        if not raw_in.strip():
            _write_stdout("{}\n")
            return 0
        payload = json.loads(raw_in)
        if not isinstance(payload, dict):
            _write_stdout("{}\n")
            return 0

        compact_path_s, _, state_path_s = _find_transcript_files_str(payload, resolve_full=False)
        if compact_path_s is None or state_path_s is None:
            _write_stdout("{}\n")
            return 0

        had_state = False
        state: dict[str, Any] = {}
        try:
            with open(state_path_s, "r", encoding="utf-8") as sf:
                loaded = json.loads(sf.read().lstrip("\ufeff"))
            if isinstance(loaded, dict):
                state = loaded
                had_state = True
        except (OSError, json.JSONDecodeError):
            had_state = False

        prev_cp = state.get("last_rehydrated_checkpoint")
        if not (isinstance(prev_cp, int) and not isinstance(prev_cp, bool) and prev_cp >= 0):
            prev_cp = None
        raw_offset = state.get("checked_offset")
        prev_offset = (
            raw_offset
            if isinstance(raw_offset, int) and not isinstance(raw_offset, bool) and raw_offset >= 0
            else 0
        )
        cached_ancestors = state.get("fork_ancestors")
        prev_status = state.get("last_status") if isinstance(state.get("last_status"), str) else None

        latest_cp, _, new_offset = scan_compact_tail(
            compact_path_s, prev_offset, prev_cp, need_max_step=False
        )

        if latest_cp is None or (prev_cp is not None and latest_cp <= prev_cp):
            if not had_state or new_offset != prev_offset:
                _save_state_str(
                    state_path_s,
                    prev_cp,
                    new_offset,
                    cached_ancestors,
                    last_status=prev_status,
                )
            _write_stdout("{}\n")
            return 0

        full_cand = os.path.join(os.path.dirname(compact_path_s), "transcript_full.jsonl")
        full_path_s = full_cand if os.path.isfile(full_cand) else compact_path_s

        _ensure_scripts_dir()
        from pathlib import Path
        import rehydrate_core

        full_path = Path(full_path_s)
        ws_roots = rehydrate_core.resolve_workspace_roots_from_payload(payload)
        cfg = rehydrate_core.load_rehydrate_config(ws_roots)
        if not cfg.enabled:
            _save_state_str(state_path_s, latest_cp, new_offset, cached_ancestors, last_status="disabled")
            _write_stdout("{}\n")
            return 0

        ad_raw = payload.get("artifactDirectoryPath")
        ad_s = ad_raw.strip() if isinstance(ad_raw, str) else ""
        artifact_dir = Path(ad_s) if ad_s else None

        hook_status: dict[str, Any] = {}
        res = rehydrate_core.build_rehydration(
            full_path,
            config=cfg,
            workspace_roots=ws_roots,
            artifact_dir=artifact_dir,
            expected_checkpoint=latest_cp,
            max_fresh_gap=FRESH_CHECKPOINT_MAX_GAP,
            out_status=hook_status,
            cached_ancestors=cached_ancestors,
        )
        if res is None:
            reason = str(hook_status.get("reason") or "empty")
            if reason != "full_lag":
                _save_state_str(state_path_s, latest_cp, new_offset, cached_ancestors, last_status=reason)
            _write_stdout("{}\n")
            return 0

        full_msg, stats = res
        saved_cp = stats.get("checkpoint")
        if not (isinstance(saved_cp, int) and not isinstance(saved_cp, bool) and saved_cp >= 0):
            saved_cp = latest_cp
        saved_ancestors = stats.get("fork_ancestors")
        if saved_ancestors is None:
            saved_ancestors = cached_ancestors
        _save_state_str(state_path_s, saved_cp, new_offset, saved_ancestors, last_status="injected")

        chunks = stats.get("chunks") or [full_msg]
        out_json = json.dumps(
            {"injectSteps": [{"systemMessage": {"systemMessage": c}} for c in chunks]},
            ensure_ascii=False,
        )
        _write_stdout(out_json + "\n")
        return 0
    except Exception:
        _write_stdout("{}\n")
        return 0


def main() -> int:
    configure_utf8_stdio()
    if len(sys.argv) > 1 and sys.argv[1] != "--hook":
        _ensure_scripts_dir()
        import rehydrate_cli

        return rehydrate_cli.run_cli(sys.argv[1:])
    return run_hook()


def __getattr__(name: str) -> Any:
    if name.startswith("_"):
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    _ensure_scripts_dir()
    for mod_name in ("rehydrate_core", "rehydrate_cli"):
        mod = __import__(mod_name)
        if hasattr(mod, name):
            val = getattr(mod, name)
            if isinstance(val, type(sys)):
                continue
            if getattr(val, "__module__", None) == mod_name:
                return val
            if mod_name == "rehydrate_core" and name in _CORE_PUBLIC_CONSTANTS:
                return val
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


if __name__ == "__main__":
    raise SystemExit(main())
