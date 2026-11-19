#!/usr/bin/env python3
"""Antigravity PreInvocation hook entrypoint & normal-turn fast path for `agy-rehydrate`.

On ordinary non-compaction turns under `python -S -B`, only `json`, `os`, and `sys` are loaded
(~6 KB source) to scan newly appended `transcript.jsonl` bytes. Heavy helpers, the 4-layer
rehydration engine, and diagnostic CLI subcommands are imported lazily only when a new
`CHECKPOINT` or CLI invocation occurs, while PEP 562 `__getattr__` preserves full symbol
compatibility for callers importing `rehydrate_after_compact`.
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


def _ensure_scripts_dir() -> None:
    global _SCRIPTS_DIR_ADDED
    if not _SCRIPTS_DIR_ADDED:
        scripts_dir = os.path.dirname(os.path.abspath(__file__))
        if scripts_dir not in sys.path:
            sys.path.insert(0, scripts_dir)
        if __name__ == "__main__" and "rehydrate_after_compact" not in sys.modules:
            sys.modules["rehydrate_after_compact"] = sys.modules[__name__]
        _SCRIPTS_DIR_ADDED = True


def configure_utf8_stdio() -> None:
    """Force UTF-8 on stdin/stdout/stderr so Windows locale codepages (cp950/cp936) never corrupt JSON."""
    for stream_name in ("stdin", "stdout", "stderr"):
        stream = getattr(sys, stream_name, None)
        reconfigure = getattr(stream, "reconfigure", None)
        if callable(reconfigure):
            try:
                reconfigure(encoding="utf-8", errors="replace")
            except Exception:
                pass


def _extract_step_index_bytes(raw_line: bytes) -> int:
    """Parse integer `step_index` in a JSONL line without invoking `re`."""
    pos = raw_line.find(b'"step_index"', 0, 256)
    if pos < 0:
        pos = raw_line.rfind(b'"step_index"')
    if pos < 0:
        return -1
    colon = raw_line.find(b":", pos + 12, pos + 28)
    if colon < 0:
        return -1
    i = colon + 1
    n = len(raw_line)
    while i < n and raw_line[i] in (32, 9):
        i += 1
    start = i
    while i < n and 48 <= raw_line[i] <= 57:
        i += 1
    if i == start:
        return -1
    return int(raw_line[start:i])


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
            cand = os.path.join(ad_s, ".system_generated", "logs", "transcript.jsonl")
            if os.path.isfile(cand):
                compact_path = cand
    if compact_path is None and isinstance(cid, str):
        cid_s = cid.strip()
        if cid_s:
            gemini_root = os.path.join(os.path.expanduser("~"), ".gemini")
            best_mtime = -1.0
            for prod in AGY_PRODUCT_DIRS:
                cand = os.path.join(gemini_root, prod, "brain", cid_s, ".system_generated", "logs", "transcript.jsonl")
                try:
                    st = os.stat(cand)
                    if (st.st_mode & 0o170000) == 0o100000 and st.st_mtime > best_mtime:
                        best_mtime = st.st_mtime
                        compact_path = cand
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


def _scan_compact_tail_str(
    compact_path: str,
    prev_offset: int,
    prev_cp: int | None,
    *,
    need_max_step: bool = True,
) -> tuple[int | None, int, int]:
    """Incrementally scan newly appended JSONL bytes for `CHECKPOINT` records in a single file open."""
    with open(compact_path, "rb") as fb:
        fb.seek(0, os.SEEK_END)
        st_size = fb.tell()
        if st_size == prev_offset and prev_offset > 0:
            return prev_cp, -1, st_size

        use_incremental = False
        if 0 < prev_offset < st_size:
            fb.seek(prev_offset - 1)
            use_incremental = fb.read(1) == b"\n"
        if not use_incremental:
            fb.seek(0)

        latest_cp = prev_cp if use_incremental else None
        max_step = -1

        # Normal-turn fast path when state already exists: avoid per-line scanning unless `"CHECKPOINT"` appears
        if not need_max_step and use_incremental:
            delta = st_size - prev_offset
            has_cp_marker = False
            if delta <= 1_048_576:
                tail = fb.read(delta)
                if b'"CHECKPOINT"' not in tail:
                    return latest_cp, -1, st_size
                for raw_line in tail.splitlines():
                    if b'"CHECKPOINT"' in raw_line:
                        try:
                            obj = json.loads(raw_line.decode("utf-8", "replace"))
                            if isinstance(obj, dict) and obj.get("type") == "CHECKPOINT":
                                sidx = obj.get("step_index", -1)
                                if isinstance(sidx, int) and not isinstance(sidx, bool):
                                    latest_cp = sidx
                        except json.JSONDecodeError:
                            pass
                return latest_cp, -1, st_size
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
                return latest_cp, -1, st_size
            fb.seek(prev_offset)

        for raw_line in fb:
            if not raw_line or raw_line.isspace():
                continue
            if b'"CHECKPOINT"' in raw_line:
                try:
                    obj = json.loads(raw_line.decode("utf-8", "replace"))
                    if isinstance(obj, dict):
                        sidx = obj.get("step_index", -1)
                        if isinstance(sidx, int) and not isinstance(sidx, bool):
                            if need_max_step and sidx > max_step:
                                max_step = sidx
                            if obj.get("type") == "CHECKPOINT":
                                latest_cp = sidx
                except json.JSONDecodeError:
                    pass
            elif need_max_step:
                sidx = _extract_step_index_bytes(raw_line)
                if sidx > max_step:
                    max_step = sidx
    return latest_cp, max_step, st_size


def _save_state_str(
    state_path: str,
    cp: int | None,
    offset: int,
    fork_ancestors: list[dict[str, Any]] | None = None,
) -> None:
    try:
        data: dict[str, Any] = {"last_rehydrated_checkpoint": cp, "checked_offset": offset}
        if fork_ancestors:
            data["fork_ancestors"] = fork_ancestors
        with open(state_path, "w", encoding="utf-8") as f:
            f.write(json.dumps(data, indent=2))
    except OSError:
        pass


def run_hook() -> int:
    """Execute `PreInvocation` hook contract (Fail-Open `{}` on any non-trigger or error)."""
    try:
        raw_in = sys.stdin.read()
        if not raw_in.strip():
            sys.stdout.write("{}\n")
            return 0
        payload = json.loads(raw_in)
        if not isinstance(payload, dict):
            sys.stdout.write("{}\n")
            return 0

        compact_path_s, _, state_path_s = _find_transcript_files_str(payload, resolve_full=False)
        if compact_path_s is None or state_path_s is None:
            sys.stdout.write("{}\n")
            return 0

        had_state = False
        state: dict[str, Any] = {}
        try:
            with open(state_path_s, "r", encoding="utf-8") as sf:
                loaded_state = json.loads(sf.read())
            if isinstance(loaded_state, dict):
                state = loaded_state
                had_state = True
        except (OSError, json.JSONDecodeError):
            had_state = False

        prev_cp = state.get("last_rehydrated_checkpoint")
        if not (isinstance(prev_cp, int) and not isinstance(prev_cp, bool)):
            prev_cp = None
        raw_offset = state.get("checked_offset")
        prev_offset = raw_offset if isinstance(raw_offset, int) and not isinstance(raw_offset, bool) and raw_offset >= 0 else 0
        cached_ancestors = state.get("fork_ancestors")
        latest_cp, max_step, new_offset = _scan_compact_tail_str(
            compact_path_s, prev_offset, prev_cp, need_max_step=not had_state
        )

        if latest_cp is None or latest_cp == prev_cp:
            if not had_state or latest_cp != prev_cp or new_offset != prev_offset:
                _save_state_str(state_path_s, latest_cp, new_offset, cached_ancestors)
            sys.stdout.write("{}\n")
            return 0

        full_cand = os.path.join(os.path.dirname(compact_path_s), "transcript_full.jsonl")
        full_path_s = full_cand if os.path.isfile(full_cand) else compact_path_s

        _ensure_scripts_dir()
        import rehydrate_engine
        import rehydrate_helpers

        path_cls = rehydrate_helpers._path_cls()
        full_path = path_cls(full_path_s)
        ws_roots = rehydrate_helpers.resolve_workspace_roots_from_payload(payload)
        cfg = rehydrate_helpers.load_rehydrate_config(ws_roots)
        if not cfg.enabled:
            sys.stdout.write("{}\n")
            return 0

        if not had_state and max_step > latest_cp and (max_step - latest_cp) > FRESH_CHECKPOINT_MAX_GAP:
            ancestors = rehydrate_engine.resolve_fork_ancestors(full_path, cached_ancestors)
            _save_state_str(state_path_s, latest_cp, new_offset, ancestors)
            sys.stdout.write("{}\n")
            return 0

        ad_raw = payload.get("artifactDirectoryPath")
        artifact_dir = path_cls(ad_raw) if isinstance(ad_raw, str) and ad_raw.strip() else None
        ancestors = rehydrate_engine.resolve_fork_ancestors(full_path, cached_ancestors)
        res = rehydrate_engine.build_rehydration(
            full_path,
            ancestors=ancestors,
            config=cfg,
            workspace_roots=ws_roots or None,
            artifact_dir=artifact_dir,
        )
        _save_state_str(state_path_s, latest_cp, new_offset, ancestors)
        if res is None:
            sys.stdout.write("{}\n")
            return 0

        full_msg, stats = res
        chunks = stats.get("chunks") or [full_msg]
        out_json = json.dumps(
            {"injectSteps": [{"systemMessage": {"systemMessage": c}} for c in chunks]},
            ensure_ascii=False,
        )
        sys.stdout.write(out_json + "\n")
        return 0
    except Exception:
        sys.stdout.write("{}\n")
        return 0


def main() -> int:
    configure_utf8_stdio()
    if len(sys.argv) > 1 and sys.argv[1] != "--hook":
        _ensure_scripts_dir()
        import rehydrate_cli

        return rehydrate_cli.run_cli(sys.argv[1:])
    return run_hook()


def __getattr__(name: str) -> Any:
    if name.startswith("__"):
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    _ensure_scripts_dir()
    if name in ("Path", "re", "datetime", "deque"):
        import rehydrate_helpers

        if name == "Path":
            return rehydrate_helpers._path_cls()
        if name == "re":
            return rehydrate_helpers._re()
        if name == "datetime":
            return rehydrate_helpers._datetime_cls()
        return rehydrate_helpers._deque_cls()

    for mod_name in ("rehydrate_helpers", "rehydrate_engine", "rehydrate_cli"):
        mod = __import__(mod_name)
        if hasattr(mod, name):
            return getattr(mod, name)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


if __name__ == "__main__":
    raise SystemExit(main())
