#!/usr/bin/env python3
"""Antigravity PreInvocation hook & CLI: Self-Adaptive Post-Compaction State Rehydration (+ /fork lineage).

Adapted from https://agy-rehydrate.pages.dev/ for cross-platform (Windows / macOS / Linux)
and multi-surface (Antigravity 2.0 App, Antigravity CLI, Antigravity IDE) operation, with:
  - Self-adaptive mode switching (`completed_qa` / `active_coding` / `mid_turn`) + ratio-bounded caps
  - Summary-aware delta deduplication against native `CHECKPOINT` (`# User Requests`)
  - Edit-hunk windowing (`±45` lines around `edit_file` anchors instead of blind head+tail clipping)
  - 3-gate file security (`status == "DONE"` verification + `workspacePaths` boundary + sensitive denylist)
  - Nested/quoted rehydration block stripping + dynamic sequential section numbering

Zero third-party dependencies (Python 3.14+ standard library only).
"""

from __future__ import annotations

import argparse
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime
import json
import os
from pathlib import Path
import re
import sys
from typing import Any
import unicodedata
from urllib.parse import unquote, urlparse

sys.dont_write_bytecode = True

MARKER_PREFIX = "[POST-COMPACT REHYDRATION v1"
CONT_MARKER_PREFIX = "[POST-COMPACT REHYDRATION cont. part"

DEFAULT_BUDGET_ALL_USER_CHARS = 24_000
DEFAULT_BUDGET_RECENT_TURNS_CHARS = 14_000
DEFAULT_BUDGET_SKILLS_SPECS_CHARS = 12_000
DEFAULT_BUDGET_FILES_TOTAL_CHARS = 24_000
DEFAULT_BUDGET_SINGLE_FILE_CHARS = 10_000

DEFAULT_MAX_RECENT_ENTRIES = 6
RECENT_USER_VERBATIM_COUNT = 75
SHORT_USER_MAX_CHARS = 4
FOLD_PASTE_THRESHOLD_CHARS = 800
FOLD_PASTE_HEAD_CHARS = 300
FOLD_PASTE_TAIL_CHARS = 200
OLDER_ASSISTANT_FOLD_THRESHOLD_CHARS = 800
OLDER_ASSISTANT_HEAD_CHARS = 420
OLDER_ASSISTANT_TAIL_CHARS = 220

DEFAULT_MAX_MODIFIED_FILES = 3
DEFAULT_MAX_REFERENCED_FILES = 0
DEFAULT_MAX_IN_PROGRESS_READ_FILES = 2
DEFAULT_MAX_MODIFIED_AGE_STEPS = 60
DEFAULT_MAX_READ_SKILL_AGE_STEPS = 25
MAX_FORK_DEPTH = 8
FRESH_CHECKPOINT_MAX_GAP = 8
MAX_INJECT_STEP_BYTES = 44_000  # Stay safely under AGY's 49,152-byte (48 KiB) per-SystemMessageStep cap

AGY_PRODUCT_DIRS = ("antigravity", "antigravity-cli", "antigravity-ide")

READ_TOOL_NAMES = frozenset({"read_file", "view_file", "view_file_outline", "view_code_item"})
WRITE_TOOL_NAMES = frozenset(
    {
        "edit_file",
        "write_file",
        "write_to_file",
        "replace_file_content",
        "multi_replace_file_content",
    }
)
PATH_ARG_KEYS = (
    "Path",
    "TargetFile",
    "AbsolutePath",
    "File",
    "filePath",
    "file_path",
    "path",
    "target_file",
)

SAFE_ENV_TEMPLATES = frozenset(
    {
        ".env.example",
        ".env.sample",
        ".env.template",
        ".env.dist",
        ".env.schema",
        ".env.defaults",
        ".env.test",
    }
)
SENSITIVE_BASENAMES = frozenset(
    {
        "id_rsa",
        "id_ed25519",
        "id_ecdsa",
        "id_dsa",
        ".npmrc",
        ".pypirc",
        ".netrc",
        "credentials.json",
    }
)

SKIP_EXTS = frozenset(
    {
        ".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp", ".svg", ".ico",
        ".pdf", ".pptx", ".docx", ".xlsx", ".zip", ".tar", ".gz", ".tgz", ".7z",
        ".mp4", ".mov", ".avi", ".mp3", ".wav", ".ogg",
        ".so", ".dylib", ".dll", ".exe", ".bin", ".o", ".a",
        ".pt", ".pth", ".ckpt", ".safetensors", ".onnx", ".npy", ".npz", ".ply",
        ".db", ".sqlite", ".sqlite3", ".wal", ".shm", ".pb", ".pyc", ".pyo", ".pyd",
        ".pem", ".key", ".pfx", ".p12", ".kdbx", ".jks", ".keystore",
    }
)

SKIP_PATH_SUBSTRINGS = (
    "/.system_generated/",
    "/node_modules/",
    "/.git/",
    "/__pycache__/",
    "/.ssh/",
    "/.gnupg/",
    "/dev/shm/",
    "/tmp/",
    "/appdata/local/temp/",
    "/.agents/guardrails.json",
    "/.gemini/config/config.json",
    "/.gemini/config/mcp_config.json",
    "/.gemini/antigravity/builtin/",
    "/.gemini/antigravity-cli/builtin/",
    "/.gemini/antigravity-ide/builtin/",
    "/.gemini/antigravity/plugin_data/",
    "/.gemini/antigravity/conversations/",
    "/.gemini/antigravity/daemon/",
    "/.gemini/antigravity/annotations/",
    "/.gemini/antigravity/scratch/",
    "/.gemini/antigravity/sidecar_data/",
    "/.gemini/antigravity/persistent_terminals/",
    "/.gemini/antigravity-cli/log/",
    "/antigravity-cli/history.jsonl",
    "/agy-rehydrate/skills/rehydrate/",
)

USER_REQ_RE = re.compile(r"<USER_REQUEST>\s*(.*?)\s*</USER_REQUEST>", re.S)
ASCII_ALNUM_OR_SLASH_RE = re.compile(r"[A-Za-z0-9/\\]")
WINDOWS_DRIVE_ABS_RE = re.compile(r"^[A-Za-z]:[\\/]")

NESTED_SYS_MSG_RE = re.compile(
    r"(?:The following is a <SYSTEM_MESSAGE>.*?\n\s*)?"
    r"<SYSTEM_MESSAGE>\s*(?:The following system message was injected by a PreInvocation hook:\s*)?"
    r"\[POST-COMPACT REHYDRATION ([^\]]+)\].*?</SYSTEM_MESSAGE>",
    re.S,
)
REHYDRATE_HDR_LINE_RE = re.compile(
    r"^(?:The following system message was injected by a PreInvocation hook:\s*)?"
    r"\[POST-COMPACT REHYDRATION ([^\]]+)\]\s*$"
)
REHYDRATE_SEC_LINE_RE = re.compile(
    r"^(?:##\s*)?\d+\.\s+(?:"
    r"Complete User Request Timeline|"
    r"Earlier User Request Timeline|"
    r"Recent Pre-Compaction (?:Verbatim )?Dialogue|"
    r"Final Pre-Compaction Exchange|"
    r"Restored Active Skills & Spec Docs|"
    r"Restored Active Workspace Files"
    r")\b.*$"
)
REHYDRATE_BOILERPLATE_PREFIXES = (
    "The harness just compacted earlier history into ",
    "Lossless ground-truth state rehydrated from ",
    "(Note: The messages below are the actual pre-compaction turns kept verbatim.)",
    "*(Note: The messages below are the actual pre-compaction turns kept verbatim.)*",
    "(Note: Exact pre-compaction turns kept verbatim",
    "*(Note: Exact pre-compaction turns kept verbatim",
    "(Note: User prompts, tool trajectories, and the final assistant reply",
    "*(Note: User prompts, tool trajectories, and the final assistant reply",
    "- [... earlier middle prompts omitted for budget ...]",
)
REHYDRATE_TIMELINE_ITEM_RE = re.compile(r"^-\s+(?:\[#\d+\s+@\s+step\s+\d+\]:|---\s+\[/fork branch ).*$")
CHECKPOINT_USER_REQS_RE = re.compile(
    r"#\s*User Requests\s*\n(?:The following were .*?\n)?(.*?)(?=\n<summary>|\n#\s+[A-Za-z]|\Z)",
    re.S,
)


@dataclass(frozen=True)
class RehydrateConfig:
    """Runtime configuration merged from defaults, `~/.agents/rehydrate.json`, and workspace `.agents/rehydrate.json`."""

    enabled: bool = True
    adaptive: bool = True
    budget_all_user_chars: int = DEFAULT_BUDGET_ALL_USER_CHARS
    budget_recent_turns_chars: int = DEFAULT_BUDGET_RECENT_TURNS_CHARS
    budget_skills_specs_chars: int = DEFAULT_BUDGET_SKILLS_SPECS_CHARS
    budget_files_total_chars: int = DEFAULT_BUDGET_FILES_TOTAL_CHARS
    budget_single_file_chars: int = DEFAULT_BUDGET_SINGLE_FILE_CHARS
    max_recent_entries: int = DEFAULT_MAX_RECENT_ENTRIES
    max_modified_files: int = DEFAULT_MAX_MODIFIED_FILES
    max_referenced_files: int = DEFAULT_MAX_REFERENCED_FILES
    max_modified_age_steps: int = DEFAULT_MAX_MODIFIED_AGE_STEPS
    explicit_keys: frozenset[str] = field(default_factory=frozenset)


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


def _positive_int(raw: Any, default: int, minimum: int = 0) -> int:
    if isinstance(raw, int) and not isinstance(raw, bool) and raw >= minimum:
        return raw
    return default


def resolve_workspace_roots_from_payload(payload: dict[str, Any]) -> list[Path]:
    """Extract deduplicated workspace root directories from an Antigravity hook stdin payload."""
    roots: list[Path] = []
    seen: set[str] = set()
    raw_ws = payload.get("workspacePaths")
    candidates: list[str] = []
    if isinstance(raw_ws, list):
        candidates.extend(x for x in raw_ws if isinstance(x, str) and x.strip())
    cwd = payload.get("cwd")
    if isinstance(cwd, str) and cwd.strip():
        candidates.append(cwd)

    for item in candidates:
        norm = normalize_local_path(item) or item.strip()
        try:
            resolved = Path(norm).resolve()
        except OSError:
            continue
        key = canonical_path_key(str(resolved))
        if key not in seen:
            seen.add(key)
            roots.append(resolved)
    return roots


def load_rehydrate_config(workspace: Path | None = None) -> RehydrateConfig:
    """Load optional global (`~/.agents/rehydrate.json`) and workspace (`.agents/rehydrate.json`) overrides."""
    merged: dict[str, Any] = {}
    candidates = [Path.home() / ".agents" / "rehydrate.json"]
    if workspace is not None:
        candidates.append(workspace / ".agents" / "rehydrate.json")

    for cfg_path in candidates:
        if not cfg_path.is_file():
            continue
        try:
            data = json.loads(cfg_path.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                merged.update(data)
        except (OSError, ValueError):
            continue

    if not merged:
        return RehydrateConfig()

    enabled_raw = merged.get("enabled", True)
    adaptive_raw = merged.get("adaptive", True)
    return RehydrateConfig(
        enabled=enabled_raw is not False,
        adaptive=adaptive_raw is not False,
        budget_all_user_chars=_positive_int(merged.get("budgetAllUserChars"), DEFAULT_BUDGET_ALL_USER_CHARS, 1000),
        budget_recent_turns_chars=_positive_int(
            merged.get("budgetRecentTurnsChars"), DEFAULT_BUDGET_RECENT_TURNS_CHARS, 1000
        ),
        budget_skills_specs_chars=_positive_int(
            merged.get("budgetSkillsSpecsChars"), DEFAULT_BUDGET_SKILLS_SPECS_CHARS, 0
        ),
        budget_files_total_chars=_positive_int(
            merged.get("budgetFilesTotalChars"), DEFAULT_BUDGET_FILES_TOTAL_CHARS, 0
        ),
        budget_single_file_chars=_positive_int(
            merged.get("budgetSingleFileChars"), DEFAULT_BUDGET_SINGLE_FILE_CHARS, 1000
        ),
        max_recent_entries=_positive_int(merged.get("maxRecentEntries"), DEFAULT_MAX_RECENT_ENTRIES, 2),
        max_modified_files=_positive_int(merged.get("maxModifiedFiles"), DEFAULT_MAX_MODIFIED_FILES, 0),
        max_referenced_files=_positive_int(merged.get("maxReferencedFiles"), DEFAULT_MAX_REFERENCED_FILES, 0),
        max_modified_age_steps=_positive_int(
            merged.get("maxModifiedAgeSteps"), DEFAULT_MAX_MODIFIED_AGE_STEPS, 5
        ),
        explicit_keys=frozenset(str(k) for k in merged.keys()),
    )


def normalize_local_path(raw: str) -> str:
    """Convert POSIX, Windows drive, or `file://` URI string into a safe local absolute path (blocking UNC)."""
    val = raw.strip()
    if not val:
        return ""
    if val.lower().startswith("file://"):
        parsed = urlparse(val)
        if parsed.netloc and parsed.netloc.lower() != "localhost":
            return ""
        raw_path = unquote(parsed.path or "")
        if len(raw_path) >= 3 and raw_path[0] == "/" and WINDOWS_DRIVE_ABS_RE.match(raw_path[1:]):
            val = raw_path[1:]
        else:
            val = raw_path

    if val.startswith("\\\\") or val.startswith("//"):
        return ""
    if WINDOWS_DRIVE_ABS_RE.match(val):
        return os.path.normpath(val)
    if val.startswith("/") and os.name != "nt":
        return os.path.normpath(val)
    return ""


def canonical_path_key(path_str: str) -> str:
    """Return platform-normalized key so slash and Windows-case variants deduplicate cleanly."""
    return os.path.normcase(os.path.normpath(path_str))


def _is_subpath(child: Path, parent: Path) -> bool:
    try:
        child.relative_to(parent)
        return True
    except ValueError:
        if os.name == "nt":
            c_norm = os.path.normcase(str(child))
            p_norm = os.path.normcase(str(parent)).rstrip("\\/")
            return c_norm == p_norm or c_norm.startswith(p_norm + os.sep)
        return False


def strip_nested_rehydration(txt: str) -> str:
    """Collapse pasted/quoted `[POST-COMPACT REHYDRATION ...]` blocks so repeated `/compact` never snowballs."""
    if "[POST-COMPACT REHYDRATION " not in txt:
        return txt

    cleaned = NESTED_SYS_MSG_RE.sub(
        lambda m: f"[Quoted POST-COMPACT REHYDRATION ({m.group(1).strip()}) omitted]",
        txt,
    )
    if "[POST-COMPACT REHYDRATION " not in cleaned:
        return cleaned.strip()

    out_lines: list[str] = []
    in_quote = False
    for line in cleaned.splitlines():
        stripped = line.strip()
        hdr_match = REHYDRATE_HDR_LINE_RE.match(stripped)
        if hdr_match:
            out_lines.append(f"[Quoted POST-COMPACT REHYDRATION ({hdr_match.group(1).strip()}) omitted]")
            in_quote = True
            continue
        if in_quote:
            if not stripped:
                continue
            if (
                any(stripped.startswith(pfx) for pfx in REHYDRATE_BOILERPLATE_PREFIXES)
                or REHYDRATE_SEC_LINE_RE.match(stripped)
                or REHYDRATE_TIMELINE_ITEM_RE.match(stripped)
            ):
                continue
            in_quote = False
        out_lines.append(line)

    return "\n".join(out_lines).strip()


def extract_user_text(raw: str) -> str:
    """Extract clean user prompt text from `<USER_REQUEST>` wrapper and strip nested rehydration echoes."""
    if not raw:
        return ""
    matches = USER_REQ_RE.findall(raw)
    if matches:
        body = "\n".join(m.strip() for m in matches if m.strip())
    else:
        cleaned = re.sub(r"<ADDITIONAL_METADATA>.*?</ADDITIONAL_METADATA>", "", raw, flags=re.S)
        cleaned = re.sub(r"<USER_SETTINGS_CHANGE>.*?</USER_SETTINGS_CHANGE>", "", cleaned, flags=re.S)
        body = cleaned.strip()
    return strip_nested_rehydration(body)


def is_compact_slash_cmd(txt: str) -> bool:
    """Return True if user message is solely the `/compact` trigger command."""
    return txt.strip().lower() == "/compact"


def is_low_info_short_turn(txt: str) -> bool:
    """Filter ultra-short backchannels/elliptical turns (<=4 non-punct chars, no ASCII/digits/slashes)."""
    if ASCII_ALNUM_OR_SLASH_RE.search(txt) or any(ch.isdigit() for ch in txt):
        return False
    core = "".join(
        ch
        for ch in txt
        if not ch.isspace() and unicodedata.category(ch)[0] not in ("P", "S", "Z", "C")
    )
    return len(core) <= SHORT_USER_MAX_CHARS


def fold_user_paste(txt: str, seg_path: Path | None, line_no: int, step_idx: int) -> tuple[str, bool]:
    """Fold long pasted blocks (>800 chars) keeping head, tail, and lossless JSONL line pointer."""
    if len(txt) <= FOLD_PASTE_THRESHOLD_CHARS:
        return txt, False
    omitted = len(txt) - FOLD_PASTE_HEAD_CHARS - FOLD_PASTE_TAIL_CHARS
    if omitted <= 120:
        return txt, False
    loc = f"{seg_path} line {line_no} (step {step_idx})" if seg_path else f"step {step_idx}"
    folded = (
        f"{txt[:FOLD_PASTE_HEAD_CHARS]}\n"
        f"  ...[folded {omitted} chars of pasted content; read_file {loc} if exact text needed]...\n"
        f"{txt[-FOLD_PASTE_TAIL_CHARS:]}"
    )
    return folded, True


def extract_tool_file_path(args: dict[str, Any]) -> str:
    """Extract normalized local absolute file path from tool call arguments across App, CLI, and IDE."""
    for key in PATH_ARG_KEYS:
        val = args.get(key)
        if isinstance(val, str):
            norm = normalize_local_path(val)
            if norm:
                return norm
    return ""


def extract_tool_line_range(args: dict[str, Any]) -> tuple[int | None, int | None]:
    """Extract optional 1-based `(start_line, end_line)` bounds from file-reading tool arguments."""
    start_val = args.get("StartLine") if "StartLine" in args else args.get("start_line")
    end_val = args.get("EndLine") if "EndLine" in args else args.get("end_line")
    s_line = start_val if isinstance(start_val, int) and not isinstance(start_val, bool) and start_val >= 1 else None
    e_line = end_val if isinstance(end_val, int) and not isinstance(end_val, bool) and end_val >= 1 else None
    if s_line is not None and e_line is not None and e_line < s_line:
        e_line = s_line
    return s_line, e_line


def extract_edit_snippets(tool_name: str, args: dict[str, Any]) -> list[str]:
    """Extract replacement anchor snippets (and explicit line spans when available) from write/edit arguments."""
    if tool_name not in WRITE_TOOL_NAMES:
        return []
    snippets: list[str] = []
    s_top, e_top = extract_tool_line_range(args)
    line_pfx = f"@@LINES:{s_top or ''}:{e_top or ''}@@\n" if (s_top or e_top) else ""
    for key in ("Replacement", "ReplacementContent"):
        val = args.get(key)
        if isinstance(val, str) and val.strip():
            snippets.append(f"{line_pfx}{val.strip('\r\n')[:600]}")
    chunks = args.get("ReplacementChunks")
    if isinstance(chunks, list):
        for ch in chunks:
            if isinstance(ch, dict):
                cs, ce = extract_tool_line_range(ch)
                cpfx = f"@@LINES:{cs or ''}:{ce or ''}@@\n" if (cs or ce) else line_pfx
                rc = ch.get("ReplacementContent")
                if isinstance(rc, str) and rc.strip():
                    snippets.append(f"{cpfx}{rc.strip('\r\n')[:600]}")
                elif cpfx:
                    snippets.append(cpfx.rstrip("\n"))
    if not snippets and line_pfx:
        snippets.append(line_pfx.rstrip("\n"))
    return snippets


def format_single_tool_call(
    tc: dict[str, Any],
) -> tuple[str, str, str, tuple[int | None, int | None], list[str]]:
    """Return `(tool_name, summary_label, extracted_file_path, line_range, edit_snippets)`."""
    name = str(tc.get("name") or "?")
    args = tc.get("args") or {}
    fpath = ""
    line_range: tuple[int | None, int | None] = (None, None)
    snippets: list[str] = []
    if name in READ_TOOL_NAMES or name in WRITE_TOOL_NAMES:
        arg_dict = args if isinstance(args, dict) else {}
        fpath = extract_tool_file_path(arg_dict)
        if name in READ_TOOL_NAMES:
            line_range = extract_tool_line_range(arg_dict)
        else:
            snippets = extract_edit_snippets(name, arg_dict)
        label = Path(fpath).name if fpath else ""
        summary = f"{name}({label or fpath})" if (label or fpath) else name
    elif name in ("run_command", "run_in_terminal") and isinstance(args, dict):
        cmd = str(args.get("CommandLine") or args.get("Command") or "").strip().replace("\n", " ")
        if len(cmd) > 60:
            cmd = cmd[:57] + "..."
        summary = f"{name}(`{cmd}`)"
    else:
        summary = name
    return name, summary, fpath, line_range, snippets


def is_tool_step_successful(step_obj: dict[str, Any]) -> bool:
    """Verify from transcript step record that a tool execution actually succeeded (`DONE` with no error)."""
    status = step_obj.get("status")
    if isinstance(status, str) and status != "DONE":
        return False
    if step_obj.get("error"):
        return False
    content = str(step_obj.get("content") or "").lstrip()
    if content.startswith("Encountered error in step execution:"):
        return False
    return True


def is_sensitive_filename(p: Path) -> bool:
    """Check whether `p` is a `.env*` secret file or private credential file."""
    lower_name = p.name.lower()
    if lower_name in SENSITIVE_BASENAMES:
        return True
    if lower_name == ".env" or (lower_name.startswith(".env.") and lower_name not in SAFE_ENV_TEMPLATES):
        return True
    return False


def is_eligible_text_file(
    path_str: str,
    workspace_roots: list[Path] | None = None,
    allow_skill_or_artifact_roots: list[Path] | None = None,
) -> bool:
    """Verify `path_str` is an existing, non-sensitive text file within allowed scope and <= 2 MiB."""
    norm = normalize_local_path(path_str)
    if not norm:
        return False
    p = Path(norm)
    try:
        resolved = p.resolve()
    except OSError:
        return False

    slash_lower = str(resolved).replace("\\", "/").lower()
    for sub in SKIP_PATH_SUBSTRINGS:
        if sub in slash_lower:
            return False
    if resolved.suffix.lower() in SKIP_EXTS or is_sensitive_filename(resolved):
        return False

    if workspace_roots:
        allowed_roots = list(workspace_roots)
        if allow_skill_or_artifact_roots:
            allowed_roots.extend(allow_skill_or_artifact_roots)
        if not any(_is_subpath(resolved, root) for root in allowed_roots):
            return False

    try:
        if not resolved.is_file():
            return False
        st = resolved.stat()
        return 0 < st.st_size <= 2 * 1024 * 1024
    except OSError:
        return False


def is_skill_or_spec(path_str: str) -> bool:
    """Check whether `path_str` is a `SKILL.md` or Spec/Plan/Template/Handoff Markdown file."""
    name = Path(path_str).name
    upper = name.upper()
    return upper == "SKILL.MD" or (
        upper.endswith(".MD") and any(k in upper for k in ("SPEC", "PLAN", "TEMPLATE", "HANDOFF"))
    )


PROSE_DOC_EXTS = frozenset({".md", ".mdx", ".rst", ".txt", ".adoc"})
PROSE_DOC_STEMS = frozenset({"readme", "changelog", "history", "notice", "license", "copying", "authors", "contributing"})


def _modified_file_tier(path_str: str) -> int:
    """Classify modified workspace files: `2` = primary code/config, `1` = test suite, `0` = prose doc."""
    p = Path(path_str)
    ext = p.suffix.lower()
    name_low = p.name.lower()
    stem_low = p.stem.lower().split(".")[0]
    if ext in PROSE_DOC_EXTS or stem_low in PROSE_DOC_STEMS:
        return 0
    slash_low = path_str.replace("\\", "/").lower()
    if (
        ".test." in name_low
        or ".spec." in name_low
        or name_low.startswith("test_")
        or stem_low.endswith("_test")
        or any(seg in slash_low for seg in ("/test/", "/tests/", "/__tests__/"))
    ):
        return 1
    return 2


def default_custom_skill_roots(artifact_dir: Path | None = None) -> list[Path]:
    """Return allowed non-workspace roots for Section 3 custom `SKILL.md` and session plan artifacts."""
    home = Path.home()
    candidates = [
        home / ".gemini" / "config" / "skills",
        home / ".gemini" / "config" / "plugins",
        home / ".agents" / "skills",
    ]
    if artifact_dir is not None:
        candidates.append(artifact_dir)
    out: list[Path] = []
    for c in candidates:
        try:
            if c.exists():
                out.append(c.resolve())
        except OSError:
            continue
    return out


def _clip_text_middle(text: str, max_chars: int) -> str:
    if len(text) <= max_chars:
        return text
    head_len = int(max_chars * 0.68)
    tail_len = max_chars - head_len - 80
    omitted = len(text) - head_len - max(0, tail_len)
    return (
        f"{text[:head_len]}\n"
        f"... [truncated ~{omitted} chars in middle; read_file for full content] ...\n"
        f"{text[-tail_len:] if tail_len > 0 else ''}"
    )


def read_text_clipped(path_str: str, max_chars: int) -> str | None:
    """Read UTF-8 file up to `max_chars`, keeping 68% head + tail if middle truncation is required."""
    if max_chars <= 0:
        return None
    try:
        with open(path_str, "r", encoding="utf-8") as f:
            text = f.read(max_chars * 3)
    except (OSError, UnicodeDecodeError):
        return None
    if not text:
        return None
    return _clip_text_middle(text, max_chars)


def read_scoped_text_clipped(
    path_str: str,
    max_chars: int,
    line_range: tuple[int | None, int | None] | None = None,
) -> tuple[str | None, str]:
    """Read only the requested 1-based `(start_line, end_line)` slice when present, avoiding unread file bloat."""
    if max_chars <= 0:
        return None, ""
    s_line, e_line = line_range if line_range else (None, None)
    if s_line is None and e_line is None:
        return read_text_clipped(path_str, max_chars), ""
    start_idx = max(1, s_line or 1)
    end_idx = e_line
    collected: list[str] = []
    last_lineno = start_idx
    try:
        with open(path_str, "r", encoding="utf-8") as f:
            for lineno, line in enumerate(f, 1):
                if lineno < start_idx:
                    continue
                if end_idx is not None and lineno > end_idx:
                    break
                collected.append(line)
                last_lineno = lineno
                if sum(len(x) for x in collected) >= max_chars * 3:
                    break
    except (OSError, UnicodeDecodeError):
        return None, ""
    if not collected:
        return None, ""
    text = "".join(collected)
    return _clip_text_middle(text, max_chars), f", lines {start_idx}-{last_lineno}"


def _find_probe_line_spans(
    full_text: str,
    probe: str,
    hint_range: tuple[int | None, int | None] = (None, None),
    max_matches: int = 4,
) -> list[tuple[int, int]]:
    """Locate all occurrences (up to `max_matches`) of `probe` in `full_text`, prioritizing `hint_range` when given."""
    if not probe or not probe.strip():
        return []
    candidates: list[str] = [probe]
    stripped_probe = probe.strip()
    if stripped_probe and stripped_probe not in candidates:
        candidates.append(stripped_probe)
    first_nonempty = next((ln.strip() for ln in probe.splitlines() if len(ln.strip()) >= 12), "")
    if first_nonempty and first_nonempty not in candidates:
        candidates.append(first_nonempty)

    for cand in candidates:
        hits: list[tuple[int, int]] = []
        start_pos = 0
        step_advance = max(1, len(cand))
        while len(hits) < 24:
            pos = full_text.find(cand, start_pos)
            if pos < 0:
                break
            s_ln = full_text.count("\n", 0, pos) + 1
            e_ln = s_ln + cand.count("\n")
            hits.append((s_ln, e_ln))
            start_pos = pos + step_advance
        if hits:
            hs, he = hint_range
            if hs is not None or he is not None:
                lo = (hs or he or 1) - 15
                hi = (he or hs or 10**9) + 15
                scoped = [sp for sp in hits if sp[1] >= lo and sp[0] <= hi]
                if scoped:
                    return scoped[:max_matches]
            if len(hits) <= max_matches:
                return hits
            # Spread across earliest and latest matches when an identical short snippet appears > max_matches times
            step = max(1, (len(hits) - 1) / (max_matches - 1))
            return [hits[round(i * step)] for i in range(max_matches)]
    return []


def read_modified_hunk_window(
    path_str: str,
    snippets: list[str],
    max_chars: int,
    context_lines: int = 45,
) -> tuple[str | None, str]:
    """Extract file header + `±context_lines` around matched `edit_file` anchors up to `max_chars`."""
    if max_chars <= 0:
        return None, ""
    try:
        with open(path_str, "r", encoding="utf-8") as f:
            full_text = f.read(2 * 1024 * 1024 + 1)
    except (OSError, UnicodeDecodeError):
        return None, ""
    if not full_text:
        return None, ""
    if len(full_text) <= min(max_chars, 2200):
        return full_text, ""

    if not snippets:
        return _clip_text_middle(full_text, max_chars), ""

    lines = full_text.splitlines()
    total_lines = len(lines)
    anchor_spans: list[tuple[int, int]] = []
    for raw_snip in snippets[-6:]:
        snip = raw_snip
        hint_s: int | None = None
        hint_e: int | None = None
        if snip.startswith("@@LINES:"):
            hdr, _, rest = snip.partition("\n")
            parts = hdr.strip("@").split(":")
            if len(parts) >= 3:
                hint_s = int(parts[1]) if parts[1].isdigit() else None
                hint_e = int(parts[2]) if parts[2].isdigit() else None
            snip = rest
        probe = snip.strip("\r\n")
        matched_spans = _find_probe_line_spans(full_text, probe, (hint_s, hint_e), max_matches=4)
        if matched_spans:
            anchor_spans.extend(matched_spans)
        elif hint_s is not None or hint_e is not None:
            s_ln = max(1, min(total_lines, hint_s or hint_e or 1))
            e_ln = max(s_ln, min(total_lines, hint_e or s_ln))
            anchor_spans.append((s_ln, e_ln))

    if not anchor_spans:
        return _clip_text_middle(full_text, max_chars), ""

    cap = max_chars
    scaled_ctx = max(context_lines, min(120, cap // 110)) if context_lines >= 30 else context_lines
    assembled = ""
    range_labels: list[str] = []
    base_radii = {scaled_ctx, context_lines, 28, 15, 8} if context_lines >= 30 else {context_lines, 8}
    radius_steps = sorted(base_radii, reverse=True)
    for ctx in radius_steps:
        raw_spans = [(max(1, s - ctx), min(total_lines, e + ctx)) for s, e in anchor_spans]
        raw_spans.sort()
        merged: list[list[int]] = []
        for s_ln, e_ln in raw_spans:
            if merged and s_ln <= merged[-1][1] + 6:
                merged[-1][1] = max(merged[-1][1], e_ln)
            else:
                merged.append([s_ln, e_ln])

        hdr_lines = 35 if cap >= 5000 and ctx >= 28 else 20
        header_end = min(total_lines, hdr_lines)
        blocks: list[str] = []
        range_labels = []
        if merged[0][0] > header_end + 6:
            hdr_body = "\n".join(lines[:header_end])
            blocks.append(f"# [Lines 1-{header_end}: file header]\n{hdr_body}")
            range_labels.append(f"1-{header_end}")
        elif merged[0][0] <= header_end + 6:
            merged[0][0] = 1

        prev_end = header_end if range_labels else 0
        for s_ln, e_ln in merged:
            if prev_end and s_ln > prev_end + 1:
                blocks.append(f"... [lines {prev_end + 1}-{s_ln - 1} omitted; read_file for full context] ...")
            chunk_body = "\n".join(lines[s_ln - 1 : e_ln])
            blocks.append(f"# [Lines {s_ln}-{e_ln}: active edit region]\n{chunk_body}")
            range_labels.append(f"{s_ln}-{e_ln}")
            prev_end = e_ln

        if prev_end < total_lines:
            blocks.append(f"... [lines {prev_end + 1}-{total_lines} omitted] ...")

        assembled = "\n".join(blocks)
        if len(assembled) <= cap:
            break

    return _clip_text_middle(assembled, cap), f", hunk lines {'/'.join(range_labels)}"


def _norm_prompt_for_dedup(txt: str) -> str:
    return re.sub(r"\s+", " ", txt).strip()


def extract_checkpoint_user_norms(cp_content: str) -> set[str]:
    """Parse native `CHECKPOINT` `# User Requests` items so already-preserved prompts are not duplicated."""
    if not cp_content:
        return set()
    m = CHECKPOINT_USER_REQS_RE.search(cp_content)
    if not m:
        return set()
    raw_block = m.group(1)
    parts = re.split(r"(?m)^\d+\.\s+", raw_block)
    norms: set[str] = set()
    for item in parts[1:]:
        cleaned = item.strip()
        if not cleaned or is_compact_slash_cmd(cleaned):
            continue
        norms.add(_norm_prompt_for_dedup(cleaned))
        stripped_nested = strip_nested_rehydration(cleaned)
        if stripped_nested:
            norms.add(_norm_prompt_for_dedup(stripped_nested))
    return norms


def parse_iso_epoch(ts_str: Any) -> float | None:
    if not isinstance(ts_str, str) or not ts_str:
        return None
    try:
        norm = ts_str[:-1] + "+00:00" if ts_str.endswith("Z") else ts_str
        return datetime.fromisoformat(norm).timestamp()
    except ValueError:
        return None


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
                obj = json.loads(raw.decode("utf-8", "replace"))
                sidx = obj.get("step_index")
                if isinstance(sidx, int):
                    return sidx, parse_iso_epoch(obj.get("created_at"))
    except (OSError, json.JSONDecodeError):
        return None
    return None


def find_step_epoch_in_transcript(jsonl_path: Path, target_step: int) -> tuple[int, float | None] | None:
    try:
        st_size = jsonl_path.stat().st_size
        if st_size <= 0:
            return None
        tail_window = 65536
        with open(jsonl_path, "rb") as fb:
            seek_pos = max(0, st_size - tail_window)
            fb.seek(seek_pos)
            chunk = fb.read()
        raw_lines = chunk.splitlines()
        if seek_pos > 0 and raw_lines:
            raw_lines = raw_lines[1:]

        last_step: int | None = None
        for raw in reversed(raw_lines):
            if not raw.strip():
                continue
            try:
                obj = json.loads(raw.decode("utf-8", "replace"))
            except json.JSONDecodeError:
                continue
            sidx = obj.get("step_index")
            if not isinstance(sidx, int):
                continue
            if last_step is None:
                last_step = sidx
                if last_step < target_step:
                    return None
            if sidx == target_step:
                return last_step, parse_iso_epoch(obj.get("created_at"))
            if sidx < target_step:
                return None

        step_pat = re.compile(rb'"step_index"\s*:\s*' + str(target_step).encode("ascii") + rb"\b")
        with open(jsonl_path, "rb") as fb:
            for raw in fb:
                if step_pat.search(raw[:160]):
                    try:
                        obj = json.loads(raw.decode("utf-8", "replace"))
                    except json.JSONDecodeError:
                        continue
                    if obj.get("step_index") == target_step:
                        return (last_step or target_step), parse_iso_epoch(obj.get("created_at"))
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
            seek_pos = max(0, st_size - 524288)
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
            or not isinstance(s_max, int)
            or s_min > s_max
            or not Path(p_str).is_file()
        ):
            return None
        cleaned.append({"cid": cid, "path": p_str, "min_step": s_min, "max_step": s_max})
    for idx in range(len(cleaned) - 1):
        if cleaned[idx]["max_step"] + 1 != cleaned[idx + 1]["min_step"]:
            return None
    return cleaned if cleaned[-1]["max_step"] + 1 == cur_first_step else None


def resolve_fork_ancestors(full_path: Path, cached_ancestors: Any = None) -> list[dict[str, Any]]:
    """Trace `/fork` parent conversations up to `MAX_FORK_DEPTH` hops with strict step-range isolation."""
    first_info = read_first_step_info(full_path)
    if first_info is None or first_info[0] <= 0:
        return []
    cur_first_step, cur_first_epoch = first_info
    valid_cache = validate_cached_ancestors(cached_ancestors, cur_first_step)
    if valid_cache is not None:
        return valid_cache

    parts = full_path.parts
    if len(parts) < 5 or parts[-2] != "logs" or parts[-3] != ".system_generated":
        return []
    brain_dir = full_path.parents[3]
    if not brain_dir.is_dir():
        return []

    cid_workspaces, fork_events = load_history_fork_hints(brain_dir)
    cur_cid = infer_cid_from_transcript_path(full_path)
    visited: set[str] = {cur_cid}
    ancestors_rev: list[dict[str, Any]] = []
    child_cid, child_first_step, child_first_epoch = cur_cid, cur_first_step, cur_first_epoch

    for _ in range(MAX_FORK_DEPTH):
        if child_first_step <= 0:
            break
        need_prev = child_first_step - 1
        child_ws = cid_workspaces.get(child_cid, set())
        best_score: tuple[int, int, int, float] | None = None
        best_seg: tuple[str, Path, int, float | None] | None = None

        try:
            cand_dirs = list(brain_dir.iterdir())
        except OSError:
            break

        for cand_dir in cand_dirs:
            cand_cid = cand_dir.name
            if cand_cid in visited or not cand_dir.is_dir():
                continue
            cand_logs = cand_dir / ".system_generated" / "logs"
            cand_full = cand_logs / "transcript_full.jsonl"
            if not cand_full.is_file():
                cand_full = cand_logs / "transcript.jsonl"
                if not cand_full.is_file():
                    continue
            cand_first = read_first_step_info(cand_full)
            if cand_first is None or cand_first[0] < 0 or cand_first[0] > need_prev:
                continue
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
                    fcid == cand_cid
                    and (prev_epoch is None or fts >= prev_epoch - 5.0)
                    and (child_first_epoch is None or fts <= child_first_epoch + 5.0)
                    for fcid, fts in fork_events
                )
            )
            cand_ws = cid_workspaces.get(cand_cid, set())
            same_ws = int(bool(child_ws and cand_ws and (child_ws & cand_ws)))
            exact_tip = int(cand_last_step == need_prev)
            score = (fork_cmd_match, same_ws, exact_tip, -time_delta)
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
    tp = payload.get("transcriptPath") or ""
    ad = payload.get("artifactDirectoryPath") or ""
    cid = payload.get("conversationId") or ""

    direct_candidates: list[Path] = []
    if isinstance(tp, str) and tp.strip():
        direct_candidates.append(Path(tp))
    if isinstance(ad, str) and ad.strip():
        direct_candidates.append(Path(ad) / ".system_generated" / "logs" / "transcript.jsonl")

    compact_path = next((c for c in direct_candidates if c.is_file()), None)
    if compact_path is None and isinstance(cid, str) and cid.strip():
        gemini_root = Path.home() / ".gemini"
        cid_hits: list[tuple[float, Path]] = []
        for prod in AGY_PRODUCT_DIRS:
            cand = gemini_root / prod / "brain" / cid / ".system_generated" / "logs" / "transcript.jsonl"
            try:
                if cand.is_file():
                    cid_hits.append((cand.stat().st_mtime, cand))
            except OSError:
                continue
        if cid_hits:
            cid_hits.sort(key=lambda item: item[0], reverse=True)
            compact_path = cid_hits[0][1]

    if compact_path is None:
        return None, None, None
    logs_dir = compact_path.parent
    full_path = logs_dir / "transcript_full.jsonl"
    return compact_path, (full_path if full_path.is_file() else compact_path), logs_dir / "rehydrate_state.json"


def scan_compact_tail(compact_path: Path, prev_offset: int, prev_cp: int | None) -> tuple[int | None, int, int]:
    """Incrementally scan newly appended JSONL bytes for `CHECKPOINT` records."""
    st_size = compact_path.stat().st_size
    if st_size == prev_offset and prev_offset > 0:
        return prev_cp, -1, st_size
    use_incremental = False
    if 0 < prev_offset < st_size:
        with open(compact_path, "rb") as fb_chk:
            fb_chk.seek(prev_offset - 1)
            use_incremental = fb_chk.read(1) == b"\n"
    latest_cp = prev_cp if use_incremental else None
    max_step = -1
    with open(compact_path, "rb") as fb:
        if use_incremental:
            fb.seek(prev_offset)
        for raw_line in fb:
            if not raw_line.strip():
                continue
            if b'"CHECKPOINT"' in raw_line:
                try:
                    obj = json.loads(raw_line.decode("utf-8", "replace"))
                    sidx = obj.get("step_index", -1)
                    max_step = max(max_step, sidx)
                    if obj.get("type") == "CHECKPOINT":
                        latest_cp = sidx
                except json.JSONDecodeError:
                    pass
            else:
                m = re.search(rb'"step_index"\s*:\s*(\d+)', raw_line[:128])
                if m:
                    max_step = max(max_step, int(m.group(1)))
    return latest_cp, max_step, st_size


def format_user_timeline(
    user_messages: list[tuple[int, str, Path | None, int]],
    budget_chars: int = DEFAULT_BUDGET_ALL_USER_CHARS,
) -> tuple[str, int, int, int, int]:
    """Render earlier user request timeline with short-turn denoising and long-paste folding."""
    total_real = sum(1 for _, txt, _, _ in user_messages if not txt.startswith("--- [/fork branch "))
    if total_real == 0:
        return "", 0, 0, 0, 0

    def render_pass(fold_before_recent: int) -> tuple[list[str], int, int, int]:
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
            if idx <= fold_cutoff or "[Quoted POST-COMPACT REHYDRATION" in txt:
                out_txt, did_fold = fold_user_paste(txt, spath, lno, step_s)
                if did_fold:
                    folded_pastes += 1
            kept_real += 1
            lines.append(f"- [#{idx} @ step {step_s}]: {out_txt}")
        return lines, kept_real, filtered_short, folded_pastes

    user_lines, kept_count, filtered_short_count, folded_paste_count = render_pass(RECENT_USER_VERBATIM_COUNT)
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
            omitted_line = "- [... earlier middle prompts omitted for budget ...]"
            all_user_block = (
                f"{head_part}\n{omitted_line}\n" + "\n".join(tail_lines)
                if tail_lines
                else f"{head_part}\n{omitted_line}"
            )

    if len(all_user_block) > budget_chars:
        all_user_block = _clip_text_middle(all_user_block, max(160, budget_chars))

    return all_user_block, total_real, kept_count, filtered_short_count, folded_paste_count


def _record_modified_touch(
    store: dict[str, tuple[str, int, list[str]]],
    path_str: str,
    step_idx: int,
    snippets: list[str],
) -> None:
    key = canonical_path_key(path_str)
    prev = store.get(key)
    combined = list(prev[2]) if prev and prev[1] >= step_idx - 25 else []
    for s in snippets:
        if s and s not in combined:
            combined.append(s)
    store[key] = (path_str, step_idx, combined[-4:])


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
            min(starts) if starts else None,
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
) -> tuple[str, bool]:
    if role == "banner" or role == "in_progress":
        return body, False
    if role == "user":
        clipped = body if len(body) <= 4500 else (body[:3200] + "\n...[turn clipped]...\n" + body[-1000:])
        return f"[Step {step_idx} | User]:\n{clipped}", False
    if condense_assistant and len(body) > OLDER_ASSISTANT_FOLD_THRESHOLD_CHARS:
        omitted = len(body) - OLDER_ASSISTANT_HEAD_CHARS - OLDER_ASSISTANT_TAIL_CHARS
        if omitted > 150:
            folded = (
                f"{body[:OLDER_ASSISTANT_HEAD_CHARS]}\n"
                f"  ...[assistant details summarized in <CONTEXT_SUMMARY>; {omitted} chars folded]...\n"
                f"{body[-OLDER_ASSISTANT_TAIL_CHARS:]}"
            )
            return f"[Step {step_idx} | Assistant]:\n{tool_prefix}{folded}", True
    clipped = body if len(body) <= 4500 else (body[:3200] + "\n...[turn clipped]...\n" + body[-1000:])
    return f"[Step {step_idx} | Assistant]:\n{tool_prefix}{clipped}", False


def _flush_step_batch(batch: list[str], pending_tools: list[str]) -> None:
    if not batch:
        return
    shown = batch[:8]
    label = ", ".join(shown)
    if len(batch) > 8:
        label += f", +{len(batch) - 8} more"
    pending_tools.append(label)
    batch.clear()


def _summarize_tool_chain(tools: list[str], max_items: int = 6) -> str:
    """Keep opening steps + final edit/verification steps when tool batches exceed `max_items`."""
    if len(tools) <= max_items:
        return " -> ".join(tools)
    head_n = max(1, max_items // 3)
    tail_n = max(1, max_items - head_n)
    omitted = len(tools) - head_n - tail_n
    return " -> ".join([*tools[:head_n], f"(+{omitted} steps)", *tools[-tail_n:]])


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
    cfg = config or RehydrateConfig()
    checkpoints: list[int] = []
    checkpoint_contents: dict[int, str] = {}
    already_rehydrated_after: set[int] = set()
    max_seen_step = -1

    with open(full_path, "r", encoding="utf-8", errors="replace") as f:
        last_cp: int | None = None
        for line in f:
            if not line.strip():
                continue
            try:
                obj = json.loads(line)
            except json.JSONDecodeError:
                continue
            step_idx = obj.get("step_index", -1)
            if isinstance(step_idx, int):
                if up_to_checkpoint_step is not None and step_idx > up_to_checkpoint_step:
                    break
                max_seen_step = max(max_seen_step, step_idx)
            stype = obj.get("type")
            if stype == "CHECKPOINT" and isinstance(step_idx, int):
                checkpoints.append(step_idx)
                checkpoint_contents[step_idx] = str(obj.get("content") or "")
                last_cp = step_idx
            elif stype == "SYSTEM_MESSAGE" and last_cp is not None:
                if MARKER_PREFIX in str(obj.get("content") or ""):
                    already_rehydrated_after.add(last_cp)

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

    cur_cid = infer_cid_from_transcript_path(full_path)
    effective_artifact_dir = artifact_dir or infer_artifact_dir_from_transcript_path(full_path)
    skill_extra_roots = default_custom_skill_roots(effective_artifact_dir)

    cur_first_info = read_first_step_info(full_path)
    cur_first_step = cur_first_info[0] if cur_first_info is not None else 0
    resolved_ancestors = ancestors if ancestors is not None else resolve_fork_ancestors(full_path)
    segments: list[dict[str, Any]] = [
        *resolved_ancestors,
        {"cid": cur_cid, "path": str(full_path), "min_step": cur_first_step, "max_step": target_cp - 1},
    ]

    user_messages: list[tuple[int, str, Path | None, int]] = []
    dialogue_entries: list[tuple[int, str, str, str]] = []
    modified_touch: dict[str, tuple[str, int, list[str]]] = {}
    read_touch: dict[str, tuple[str, int, tuple[int | None, int | None]]] = {}
    skill_spec_touch: dict[str, tuple[str, int, bool]] = {}
    pending_tools: list[str] = []
    pending_call_queue: deque[tuple[str, str, str, tuple[int | None, int | None], list[str], int]] = deque()
    cur_batch_summaries: list[str] = []
    last_user_step = -1
    in_progress_turn = False
    window_raw_chars = 0

    for seg_idx, seg in enumerate(segments):
        seg_path = Path(seg["path"])
        seg_min = int(seg["min_step"])
        seg_max = min(int(seg["max_step"]), target_cp - 1)
        if seg_min > seg_max or not seg_path.is_file():
            continue
        pending_call_queue.clear()
        _flush_step_batch(cur_batch_summaries, pending_tools)

        if seg_idx > 0:
            fork_banner = (
                f"--- [/fork branch {str(segments[seg_idx - 1]['cid'])[:8]} "
                f"-> {str(seg['cid'])[:8]} @ step {seg_min}] ---"
            )
            user_messages.append((seg_min, fork_banner, None, 0))
            dialogue_entries.append((seg_min, "banner", "", fork_banner))

        with open(seg_path, "r", encoding="utf-8", errors="replace") as f:
            for line_no, line in enumerate(f, 1):
                if not line.strip():
                    continue
                try:
                    obj = json.loads(line)
                except json.JSONDecodeError:
                    continue
                step_idx = obj.get("step_index", -1)
                if not isinstance(step_idx, int) or step_idx < seg_min:
                    continue
                if step_idx > seg_max:
                    break
                stype = obj.get("type")
                if step_idx > prev_cp_step and stype not in ("CHECKPOINT", "SYSTEM_MESSAGE"):
                    window_raw_chars += len(str(obj.get("content") or ""))
                    if obj.get("tool_calls"):
                        window_raw_chars += len(json.dumps(obj.get("tool_calls"), ensure_ascii=False))
                if stype == "USER_INPUT":
                    pending_call_queue.clear()
                    cur_batch_summaries.clear()
                    pending_tools.clear()
                    utext = extract_user_text(str(obj.get("content") or ""))
                    if utext and not is_compact_slash_cmd(utext):
                        last_user_step = step_idx
                        in_progress_turn = True
                        user_messages.append((step_idx, utext, seg_path, line_no))
                        dialogue_entries.append((step_idx, "user", "", utext))
                elif stype == "PLANNER_RESPONSE":
                    pending_call_queue.clear()
                    _flush_step_batch(cur_batch_summaries, pending_tools)
                    mcontent = strip_nested_rehydration(str(obj.get("content") or "").strip())
                    if mcontent:
                        tool_prefix = ""
                        if pending_tools:
                            joined_tools = _summarize_tool_chain(pending_tools, max_items=6)
                            tool_prefix = f"  (Tools run: {joined_tools})\n"
                            pending_tools.clear()
                        dialogue_entries.append((step_idx, "assistant", tool_prefix, mcontent))

                    tcs = obj.get("tool_calls") or []
                    valid_tcs = [tc for tc in tcs if isinstance(tc, dict)]
                    if valid_tcs:
                        in_progress_turn = True
                        for tc in valid_tcs:
                            tname, tsummary, fpath, lrange, esnips = format_single_tool_call(tc)
                            pending_call_queue.append((tname, tsummary, fpath, lrange, esnips, step_idx))
                    elif mcontent:
                        in_progress_turn = False
                elif stype not in ("CHECKPOINT", "SYSTEM_MESSAGE") and pending_call_queue:
                    in_progress_turn = True
                    tname, tsummary, fpath, lrange, esnips, call_step = pending_call_queue.popleft()
                    if is_tool_step_successful(obj):
                        cur_batch_summaries.append(tsummary)
                        if fpath:
                            if is_skill_or_spec(fpath):
                                _record_skill_spec_touch(
                                    skill_spec_touch, fpath, call_step, tname in WRITE_TOOL_NAMES
                                )
                            elif tname in WRITE_TOOL_NAMES:
                                _record_modified_touch(modified_touch, fpath, call_step, esnips)
                            elif tname in READ_TOOL_NAMES:
                                _record_read_touch(read_touch, fpath, call_step, lrange)
                    else:
                        cur_batch_summaries.append(f"{tsummary} [ERROR]")
                    if not pending_call_queue:
                        _flush_step_batch(cur_batch_summaries, pending_tools)

    if simulated:
        in_progress_turn = False

    _flush_step_batch(cur_batch_summaries, pending_tools)
    if pending_tools:
        dialogue_entries.append(
            (
                target_cp - 1,
                "in_progress",
                "",
                f"[Pre-Compaction In-Progress Tools]: {_summarize_tool_chain(pending_tools, max_items=6)}",
            )
        )

    def _is_recent_mod_step(st: int) -> bool:
        return (last_user_step >= 0 and st >= last_user_step) or max(0, target_cp - st) <= cfg.max_modified_age_steps

    max_mod_spec_age = max(120, cfg.max_modified_age_steps * 2)
    has_recent_mod = any(_is_recent_mod_step(st) for _, st, _ in modified_touch.values()) or any(
        was_mod and max(0, target_cp - st) <= max_mod_spec_age for _, st, was_mod in skill_spec_touch.values()
    )

    if in_progress_turn:
        session_mode = "mid_turn"
    elif has_recent_mod:
        session_mode = "active_coding"
    else:
        session_mode = "completed_qa"

    eff_recent_entries = cfg.max_recent_entries
    eff_recent_budget = cfg.budget_recent_turns_chars
    eff_user_budget = cfg.budget_all_user_chars
    eff_files_budget = cfg.budget_files_total_chars
    eff_single_file_budget = cfg.budget_single_file_chars
    eff_skills_budget = cfg.budget_skills_specs_chars
    ratio_cap: int | None = None
    frame_reserve = 720
    content_pool: int | None = None

    if cfg.adaptive:
        win_base = max(window_raw_chars, 6000)
        if session_mode == "completed_qa":
            ratio_cap = max(1600, min(7200, int(win_base * 0.10)))
            content_pool = max(880, ratio_cap - frame_reserve)
            if "maxRecentEntries" not in cfg.explicit_keys:
                eff_recent_entries = 4
            if "budgetRecentTurnsChars" not in cfg.explicit_keys:
                share = 0.84 if eff_recent_entries > 4 else 0.76
                eff_recent_budget = int(content_pool * share)
        elif session_mode == "active_coding":
            ratio_cap = max(6500, min(22000, int(win_base * 0.20)))
            content_pool = max(4500, ratio_cap - frame_reserve)
            if "maxRecentEntries" not in cfg.explicit_keys:
                eff_recent_entries = 4
            if "budgetRecentTurnsChars" not in cfg.explicit_keys:
                eff_recent_budget = min(7500, int(content_pool * 0.36))
            if "budgetSingleFileChars" not in cfg.explicit_keys:
                eff_single_file_budget = min(cfg.budget_single_file_chars, 6500)
        else:  # mid_turn (256K auto-compaction mid-tool-loop)
            ratio_cap = max(8000, min(32000, int(win_base * 0.24)))
            content_pool = max(6000, ratio_cap - frame_reserve)
            if "maxRecentEntries" not in cfg.explicit_keys:
                eff_recent_entries = 6
            if "budgetRecentTurnsChars" not in cfg.explicit_keys:
                eff_recent_budget = min(14000, int(content_pool * 0.42))

    kept_turn_pairs: list[tuple[int, str]] = []
    used_turns = 0
    real_turn_count = 0
    seen_latest_assistant = False
    folded_assistant_count = 0
    for s_idx, role, tool_pfx, body in reversed(dialogue_entries):
        is_banner = role == "banner"
        if not is_banner and real_turn_count >= eff_recent_entries:
            break
        condense = role == "assistant" and seen_latest_assistant
        rendered, did_fold = _render_dialogue_entry(s_idx, role, tool_pfx, body, condense_assistant=condense)
        if used_turns + len(rendered) > eff_recent_budget:
            if kept_turn_pairs:
                break
            rendered = _clip_text_middle(rendered, max(360, eff_recent_budget))
        if role == "assistant":
            seen_latest_assistant = True
            if did_fold:
                folded_assistant_count += 1
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
    summary_user_norms = extract_checkpoint_user_norms(target_cp_content) if cfg.adaptive else set()

    earlier_user_messages: list[tuple[int, str, Path | None, int]] = []
    deduped_summary_prompts = 0
    for s_idx, utxt, spath, lno in user_messages:
        if s_idx >= verbatim_start_step:
            continue
        if not utxt.startswith("--- [/fork branch ") and summary_user_norms:
            if _norm_prompt_for_dedup(utxt) in summary_user_norms:
                deduped_summary_prompts += 1
                continue
        earlier_user_messages.append((s_idx, utxt, spath, lno))
    while earlier_user_messages and earlier_user_messages[-1][1].startswith("--- [/fork branch "):
        earlier_user_messages.pop()
    while earlier_user_messages and earlier_user_messages[0][1].startswith("--- [/fork branch "):
        if len(earlier_user_messages) == 1 or earlier_user_messages[1][1].startswith("--- [/fork branch "):
            earlier_user_messages.pop(0)
        else:
            break

    eligible_skills: list[tuple[str, int, bool]] = []
    for _, (spath, sstep, was_mod) in sorted(skill_spec_touch.items(), key=lambda x: x[1][1], reverse=True):
        age = max(0, target_cp - sstep)
        if age > (max_mod_spec_age if was_mod else DEFAULT_MAX_READ_SKILL_AGE_STEPS):
            continue
        is_skill_md = Path(spath).name.upper() == "SKILL.MD"
        extra_roots = (
            skill_extra_roots if is_skill_md else ([effective_artifact_dir] if effective_artifact_dir else [])
        )
        if is_eligible_text_file(
            spath,
            workspace_roots=workspace_roots,
            allow_skill_or_artifact_roots=[r for r in extra_roots if r is not None],
        ):
            eligible_skills.append((spath, sstep, was_mod))

    def _mod_sort_key(item: tuple[str, tuple[str, int, list[str]]]) -> tuple[int, int, int, int]:
        _, (p, st, snips) = item
        hot_band = 1 if ((last_user_step >= 0 and st >= last_user_step) or max(0, target_cp - st) <= 35) else 0
        tier = _modified_file_tier(p)
        return (hot_band, tier, st, len(snips))

    mod_candidates = [
        (key, p, st, (None, None), snips)
        for key, (p, st, snips) in sorted(modified_touch.items(), key=_mod_sort_key, reverse=True)
        if key not in skill_spec_touch
        and _is_recent_mod_step(st)
        and is_eligible_text_file(p, workspace_roots=workspace_roots)
    ][: cfg.max_modified_files]
    mod_keys = {key for key, _, _, _, _ in mod_candidates}

    eligible_reads = [
        (key, p, st, lr, [])
        for key, (p, st, lr) in sorted(read_touch.items(), key=lambda x: x[1][1], reverse=True)
        if key not in skill_spec_touch
        and key not in mod_keys
        and is_eligible_text_file(p, workspace_roots=workspace_roots)
    ]
    if cfg.max_referenced_files > 0:
        ref_candidates = eligible_reads[: cfg.max_referenced_files]
    elif in_progress_turn and last_user_step >= 0:
        ref_candidates = [item for item in eligible_reads if item[2] >= last_user_step][
            :DEFAULT_MAX_IN_PROGRESS_READ_FILES
        ]
    else:
        ref_candidates = []
    skipped_referenced_count = max(0, len(eligible_reads) - len(ref_candidates))

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
    if eff_user_budget >= 200:
        (
            earlier_user_block,
            earlier_real_count,
            kept_earlier_count,
            filtered_short_count,
            folded_paste_count,
        ) = format_user_timeline(earlier_user_messages, budget_chars=eff_user_budget)
    else:
        earlier_user_block = ""
        earlier_real_count = sum(1 for _, txt, _, _ in earlier_user_messages if not txt.startswith("--- [/fork branch "))
        kept_earlier_count = 0
        filtered_short_count = 0
        folded_paste_count = 0

    if rem_pool is not None and "budgetAllUserChars" not in cfg.explicit_keys and earlier_user_block:
        rem_pool = max(0, rem_pool - len(earlier_user_block) - 96)

    if rem_pool is not None and "budgetSkillsSpecsChars" not in cfg.explicit_keys:
        if session_mode == "completed_qa":
            eff_skills_budget = 0
        else:
            eff_skills_budget = min(
                cfg.budget_skills_specs_chars,
                int(rem_pool * 0.35) if will_have_files else rem_pool,
            )

    skill_blocks: list[str] = []
    restored_skill_paths: list[str] = []
    used_skills = 0
    if eff_skills_budget >= 800:
        for spath, sstep, _ in eligible_skills:
            rem = eff_skills_budget - used_skills
            if rem < 800:
                break
            header_overhead = len(spath) + 48
            content = read_text_clipped(spath, min(eff_single_file_budget, max(200, rem - header_overhead)))
            if content:
                block = f"### `{spath}` (touched @ step {sstep})\n```\n{content}\n```"
                skill_blocks.append(block)
                restored_skill_paths.append(spath)
                used_skills += len(block) + 2

    if rem_pool is not None and "budgetSkillsSpecsChars" not in cfg.explicit_keys and skill_blocks:
        rem_pool = max(0, rem_pool - used_skills - 56)

    if rem_pool is not None and "budgetFilesTotalChars" not in cfg.explicit_keys:
        if session_mode == "completed_qa" and cfg.max_referenced_files == 0:
            eff_files_budget = 0
        else:
            eff_files_budget = min(cfg.budget_files_total_chars, rem_pool)

    file_blocks: list[str] = []
    restored_file_paths: list[str] = []
    used_files = 0
    if eff_files_budget >= 800:
        for role_label, items in (("Modified", mod_candidates), ("Referenced", ref_candidates)):
            for _, fpath, fstep, lrange, snips in items:
                rem = eff_files_budget - used_files
                if rem < 800:
                    break
                header_overhead = len(fpath) + 120
                max_slice = min(eff_single_file_budget, max(200, rem - header_overhead))
                if role_label == "Referenced":
                    content, range_note = read_scoped_text_clipped(fpath, max_slice, lrange)
                elif cfg.adaptive:
                    hunk_ctx = 12 if _modified_file_tier(fpath) == 0 else 45
                    content, range_note = read_modified_hunk_window(fpath, snips, max_slice, context_lines=hunk_ctx)
                else:
                    content, range_note = read_text_clipped(fpath, max_slice), ""
                if content:
                    block = f"### [{role_label} @ step {fstep}{range_note}] `{fpath}`\n```\n{content}\n```"
                    file_blocks.append(block)
                    restored_file_paths.append(f"{role_label}:{fpath}")
                    used_files += len(block) + 2

    # Silent bypass for trivial greetings/Q&A already 100% covered by native CHECKPOINT
    if (
        cfg.adaptive
        and not simulated
        and session_mode == "completed_qa"
        and not resolved_ancestors
        and earlier_real_count == 0
        and not skill_blocks
        and not file_blocks
        and sum(len(t) for t in kept_turns) < 160
        and all("(Tools run:" not in t for t in kept_turns)
    ):
        return None

    lineage_tag = ""
    if resolved_ancestors:
        chain_parts = [
            f"{str(seg['cid'])[:8]}({seg['min_step']}..{min(int(seg['max_step']), target_cp - 1)})"
            for seg in segments
            if int(seg["min_step"]) <= min(int(seg["max_step"]), target_cp - 1)
        ]
        lineage_tag = f" | fork_lineage={'->'.join(chain_parts)}"
    mode_tag = f" | mode={session_mode}" if cfg.adaptive else ""

    numbered_sections: list[tuple[str, str]] = []
    if earlier_real_count > 0 and earlier_user_block:
        delta_note = ", delta beyond <CONTEXT_SUMMARY>" if deduped_summary_prompts > 0 else ""
        if filtered_short_count > 0 or folded_paste_count > 0:
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

    if kept_turns:
        if session_mode == "completed_qa" and len(kept_turns) <= 2 and folded_assistant_count == 0:
            sec2_title = f"Final Pre-Compaction Exchange ({len(kept_turns)} entries verbatim)"
            sec2_body = "\n\n".join(kept_turns)
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
            sec2_body = f"{sec2_note}\n\n" + "\n\n".join(kept_turns)
        else:
            sec2_title = f"Recent Pre-Compaction Verbatim Dialogue ({len(kept_turns)} entries kept verbatim)"
            sec2_body = "\n\n".join(kept_turns)
        numbered_sections.append((sec2_title, sec2_body))
    if skill_blocks:
        numbered_sections.append(("Restored Active Skills & Spec Docs", "\n\n".join(skill_blocks)))
    if file_blocks:
        numbered_sections.append(("Restored Active Workspace Files (Live Disk Read)", "\n\n".join(file_blocks)))

    if not numbered_sections:
        return None

    sections = [
        f"{MARKER_PREFIX} | checkpoint={target_cp}{mode_tag}{lineage_tag}]",
        (
            "Lossless ground-truth state rehydrated from `transcript_full.jsonl` and live workspace files. "
            "Trust these verbatim records and live slices over `<CONTEXT_SUMMARY>` whenever details differ; "
            "if prior Q&A is already answered with no pending action, ignore `<CONTEXT_SUMMARY>`'s "
            "`RESUMING MY OWN TASK IN PROGRESS` template and answer the user's new message directly."
        ),
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

    full_msg = "\n\n".join(sections)
    chunks = split_into_inject_chunks(sections, max_bytes=MAX_INJECT_STEP_BYTES, checkpoint=target_cp)
    return full_msg, {
        "checkpoint": target_cp,
        "simulated": simulated,
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
        "kept_turns": len(kept_turns),
        "folded_assistant_turns": folded_assistant_count,
        "verbatim_start_step": verbatim_start_step,
        "skills": len(skill_blocks),
        "skill_paths": restored_skill_paths,
        "files": len(file_blocks),
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
    """Guarantee `len('\\n\\n'.join(sections)) <= cap` by trimming trailing section bodies if needed."""
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
            clipped_body = _clip_text_middle(body, target_body_len)
            if len(clipped_body) > target_body_len:
                clipped_body = clipped_body[:target_body_len]
            out[idx] = f"{hdr}\n{clipped_body}"
            break
        if len(out) > 3:
            out.pop(idx)
            idx -= 1
            continue
        if sep and body:
            keep = max(80, len(body) - overflow)
            out[idx] = f"{hdr}\n{body[:keep]}"
        break
    return out


def _split_oversized_block(text: str, max_bytes: int) -> list[str]:
    if len(text.encode("utf-8")) <= max_bytes:
        return [text]
    pieces: list[str] = []
    cur_lines: list[str] = []
    cur_bytes = 0
    for line in text.split("\n"):
        lb = len(line.encode("utf-8")) + 1
        if lb > max_bytes:
            if cur_lines:
                pieces.append("\n".join(cur_lines))
                cur_lines, cur_bytes = [], 0
            buf: list[str] = []
            bb = 0
            for ch in line:
                cb = len(ch.encode("utf-8"))
                if bb + cb > max_bytes - 64 and buf:
                    pieces.append("".join(buf))
                    buf, bb = [], 0
                buf.append(ch)
                bb += cb
            if buf:
                pieces.append("".join(buf))
            continue
        if cur_lines and cur_bytes + lb > max_bytes:
            pieces.append("\n".join(cur_lines))
            cur_lines, cur_bytes = [line], lb
        else:
            cur_lines.append(line)
            cur_bytes += lb
    if cur_lines:
        pieces.append("\n".join(cur_lines))
    return pieces


def split_into_inject_chunks(
    sections: list[str],
    max_bytes: int = MAX_INJECT_STEP_BYTES,
    checkpoint: int | None = None,
) -> list[str]:
    """Pack sections into <=max_bytes UTF-8 chunks so no single SystemMessageStep hits AGY's 48 KiB cap."""
    header_reserve = 96
    limit = max(4096, max_bytes - header_reserve)
    atomic_blocks: list[str] = []
    for sec in sections:
        if len(sec.encode("utf-8")) <= limit:
            atomic_blocks.append(sec)
        else:
            sub_items = re.split(r"\n\n(?=### |\[Step |\[Pre-Compaction |--- \[/fork )", sec)
            for item in sub_items:
                if len(item.encode("utf-8")) <= limit:
                    atomic_blocks.append(item)
                else:
                    for para in item.split("\n\n"):
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

    cp_note = f" | checkpoint={checkpoint}" if checkpoint is not None else ""
    total = len(raw_chunks)
    tagged: list[str] = []
    for idx, ch in enumerate(raw_chunks, 1):
        if idx == 1:
            tagged.append(ch)
        else:
            tagged.append(f"{CONT_MARKER_PREFIX} {idx}/{total}{cp_note}]\n\n{ch}")
    return tagged


def save_state(
    state_path: Path,
    cp: int | None,
    offset: int,
    fork_ancestors: list[dict[str, Any]] | None = None,
) -> None:
    try:
        data: dict[str, Any] = {"last_rehydrated_checkpoint": cp, "checked_offset": offset}
        if fork_ancestors:
            data["fork_ancestors"] = fork_ancestors
        state_path.write_text(json.dumps(data, indent=2), encoding="utf-8")
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

        ws_roots = resolve_workspace_roots_from_payload(payload)
        primary_ws = ws_roots[0] if ws_roots else None
        cfg = load_rehydrate_config(primary_ws)
        if not cfg.enabled:
            sys.stdout.write("{}\n")
            return 0

        compact_path, full_path, state_path = find_transcript_files(payload)
        if compact_path is None or full_path is None or state_path is None:
            sys.stdout.write("{}\n")
            return 0

        had_state = state_path.is_file()
        state: dict[str, Any] = {}
        if had_state:
            try:
                state = json.loads(state_path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                had_state = False

        prev_cp = state.get("last_rehydrated_checkpoint")
        prev_offset = int(state.get("checked_offset") or 0)
        cached_ancestors = state.get("fork_ancestors")
        latest_cp, max_step, new_offset = scan_compact_tail(compact_path, prev_offset, prev_cp)

        if latest_cp is None or latest_cp == prev_cp:
            save_state(state_path, latest_cp, new_offset, cached_ancestors)
            sys.stdout.write("{}\n")
            return 0

        if not had_state and max_step > latest_cp and (max_step - latest_cp) > FRESH_CHECKPOINT_MAX_GAP:
            ancestors = resolve_fork_ancestors(full_path, cached_ancestors)
            save_state(state_path, latest_cp, new_offset, ancestors)
            sys.stdout.write("{}\n")
            return 0

        ad_raw = payload.get("artifactDirectoryPath")
        artifact_dir = Path(ad_raw) if isinstance(ad_raw, str) and ad_raw.strip() else None
        ancestors = resolve_fork_ancestors(full_path, cached_ancestors)
        res = build_rehydration(
            full_path,
            ancestors=ancestors,
            config=cfg,
            workspace_roots=ws_roots or None,
            artifact_dir=artifact_dir,
        )
        save_state(state_path, latest_cp, new_offset, ancestors)
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


def discover_recent_transcripts(limit: int = 12) -> list[tuple[float, str, str, Path, Path]]:
    """Return `(mtime, product, cid, compact_path, full_path)` across App, CLI, and IDE sorted newest-first."""
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
    return found[: max(1, limit)]


def resolve_target_transcript(target: str) -> tuple[str, str, Path, Path] | None:
    """Resolve `latest`, conversation UUID (or prefix), or explicit JSONL path."""
    p_cand = Path(target)
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
    common.add_argument("--cwd", default=None, help="Workspace root directory")

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
    workspace = Path(args.cwd or os.getcwd()).resolve()
    cfg = load_rehydrate_config(workspace)

    if args.subcmd == "show-config":
        print(
            json.dumps(
                {
                    "workspace": str(workspace),
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
                ts_str = datetime.fromtimestamp(mtime).strftime("%Y-%m-%d %H:%M:%S")
                print(f"[{ts_str}] {prod:<15} {cid}  [sandbox-restricted; run outside sandbox to inspect]")
                continue
            first_info = read_first_step_info(full)
            first_step = first_info[0] if first_info else 0
            state_file = full.parent / "rehydrate_state.json"
            last_rehydrated = None
            if state_file.is_file():
                try:
                    last_rehydrated = json.loads(state_file.read_text(encoding="utf-8")).get(
                        "last_rehydrated_checkpoint"
                    )
                except (OSError, ValueError):
                    pass
            ts_str = datetime.fromtimestamp(mtime).strftime("%Y-%m-%d %H:%M:%S")
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
            workspace_roots=[workspace],
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


def main() -> int:
    configure_utf8_stdio()
    if len(sys.argv) > 1 and sys.argv[1] != "--hook":
        return run_cli(sys.argv[1:])
    return run_hook()


if __name__ == "__main__":
    raise SystemExit(main())
