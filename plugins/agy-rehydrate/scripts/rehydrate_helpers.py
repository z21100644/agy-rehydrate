#!/usr/bin/env python3
"""Configuration, path security, Markdown-safe folding, hunk extraction, and summary parsers for `agy-rehydrate`."""

from __future__ import annotations

import json
import os
import sys

TYPE_CHECKING = False
if TYPE_CHECKING:
    from collections.abc import Sequence
    from datetime import datetime
    from pathlib import Path
    from typing import Any

sys.dont_write_bytecode = True

from rehydrate_after_compact import (
    AGY_PRODUCT_DIRS,
    FRESH_CHECKPOINT_MAX_GAP,
    _extract_step_index_bytes,
)

_RE_MOD: Any = None
_PATH_CLS: Any = None
_DATETIME_CLS: Any = None
_DEQUE_CLS: Any = None
_COMPILED_REGEX: dict[str, Any] = {}


def _re() -> Any:
    global _RE_MOD
    if _RE_MOD is None:
        import re as _re_mod

        _RE_MOD = _re_mod
    return _RE_MOD


def _path_cls() -> Any:
    global _PATH_CLS
    if _PATH_CLS is None:
        from pathlib import Path as _path_type

        _PATH_CLS = _path_type
    return _PATH_CLS


def _datetime_cls() -> Any:
    global _DATETIME_CLS
    if _DATETIME_CLS is None:
        from datetime import datetime as _dt_type

        _DATETIME_CLS = _dt_type
    return _DATETIME_CLS


def _deque_cls() -> Any:
    global _DEQUE_CLS
    if _DEQUE_CLS is None:
        from collections import deque as _deque_type

        _DEQUE_CLS = _deque_type
    return _DEQUE_CLS


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
TURN_CLIP_THRESHOLD_CHARS = 4_800
TURN_CLIP_HEAD_CHARS = 3_300
TURN_CLIP_TAIL_CHARS = 1_100
TURN_CLIP_MIN_OMITTED_CHARS = 300

DEFAULT_MAX_MODIFIED_FILES = 3
DEFAULT_MAX_REFERENCED_FILES = 0
DEFAULT_MAX_IN_PROGRESS_READ_FILES = 2
DEFAULT_MAX_MODIFIED_AGE_STEPS = 60
DEFAULT_MAX_READ_SKILL_AGE_STEPS = 25
MAX_FORK_DEPTH = 8
MAX_INJECT_STEP_BYTES = 44_000  # Stay safely under AGY's 49,152-byte (48 KiB) per-SystemMessageStep cap


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

_RE_DOTALL = 16  # re.S == 16
_RE_MULTILINE = 8  # re.M == 8

_REGEX_SPECS: dict[str, tuple[str, int]] = {
    "USER_REQ": (r"<USER_REQUEST>\s*(.*?)\s*</USER_REQUEST>", _RE_DOTALL),
    "ASCII_ALNUM_OR_SLASH": (r"[A-Za-z0-9/\\]", 0),
    "NESTED_SYS_MSG": (
        r"(?:The following is a <SYSTEM_MESSAGE>.*?\n\s*)?"
        r"<SYSTEM_MESSAGE>\s*(?:The following system message was injected by a PreInvocation hook:\s*)?"
        r"\[POST-COMPACT REHYDRATION ([^\]]+)\].*?</SYSTEM_MESSAGE>",
        _RE_DOTALL,
    ),
    "REHYDRATE_HDR_LINE": (
        r"^(?:The following system message was injected by a PreInvocation hook:\s*)?"
        r"\[POST-COMPACT REHYDRATION ([^\]]+)\]\s*$",
        0,
    ),
    "REHYDRATE_SEC_LINE": (
        r"^(?:##\s*)?\d+\.\s+(?:"
        r"Complete User Request Timeline|"
        r"Earlier User Request Timeline|"
        r"Recent Pre-Compaction (?:Verbatim )?Dialogue|"
        r"Final Pre-Compaction Exchange|"
        r"Restored Active Skills & Spec Docs|"
        r"Restored Active Workspace Files"
        r")\b.*$",
        0,
    ),
    "REHYDRATE_TIMELINE_ITEM": (
        r"^-\s+(?:\[#\d+\s+@\s+step\s+\d+\]:|---\s+\[/fork branch ).*$",
        0,
    ),
    "CHECKPOINT_USER_REQS": (
        r"#\s*User Requests\s*\n(?:The following were .*?\n)?(.*?)(?=\n<summary>|\n#\s+[A-Za-z]|\Z)",
        _RE_DOTALL,
    ),
    "STEP_INDEX_STR": (r'"step_index"\s*:\s*(\d+)', 0),
    "TYPE_STR": (r'"type"\s*:\s*"([^"]+)"', 0),
    "UI_BADGE_LINE": (
        r"^(?:Ran |Analyzed |Edited |Created |Deleted |Viewed |Searched |Used tool:\s*\S|Error: Model produced invalid output\.)",
        0,
    ),
    "CHECKPOINT_ATTACHMENT_LINE": (r"^[ \t]*-\s*\[Attachment:\s*([^\]]+)\][ \t]*\r?$", _RE_MULTILINE),
    "ATTACHMENT_URI_PAREN": (r"\(([^()]+)\)\s*$", 0),
    "CHECKPOINT_TRUNCATED_TAIL": (r"\r?\n?<truncated\s+\d+\s+bytes>\s*$", 0),
}


def _rx(name: str) -> Any:
    pat = _COMPILED_REGEX.get(name)
    if pat is None:
        raw, flags = _REGEX_SPECS[name]
        pat = _re().compile(raw, flags)
        _COMPILED_REGEX[name] = pat
    return pat


def _is_win_drive_abs(val: str) -> bool:
    return len(val) >= 3 and val[0].isalpha() and val[1] == ":" and val[2] in ("\\", "/")


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


class RehydrateConfig:
    """Runtime configuration merged from defaults, `~/.agents/rehydrate.json`, and workspace `.agents/rehydrate.json`."""

    __slots__ = (
        "enabled",
        "adaptive",
        "budget_all_user_chars",
        "budget_recent_turns_chars",
        "budget_skills_specs_chars",
        "budget_files_total_chars",
        "budget_single_file_chars",
        "max_recent_entries",
        "max_modified_files",
        "max_referenced_files",
        "max_modified_age_steps",
        "explicit_keys",
    )

    def __init__(
        self,
        enabled: bool = True,
        adaptive: bool = True,
        budget_all_user_chars: int = DEFAULT_BUDGET_ALL_USER_CHARS,
        budget_recent_turns_chars: int = DEFAULT_BUDGET_RECENT_TURNS_CHARS,
        budget_skills_specs_chars: int = DEFAULT_BUDGET_SKILLS_SPECS_CHARS,
        budget_files_total_chars: int = DEFAULT_BUDGET_FILES_TOTAL_CHARS,
        budget_single_file_chars: int = DEFAULT_BUDGET_SINGLE_FILE_CHARS,
        max_recent_entries: int = DEFAULT_MAX_RECENT_ENTRIES,
        max_modified_files: int = DEFAULT_MAX_MODIFIED_FILES,
        max_referenced_files: int = DEFAULT_MAX_REFERENCED_FILES,
        max_modified_age_steps: int = DEFAULT_MAX_MODIFIED_AGE_STEPS,
        explicit_keys: frozenset[str] = frozenset(),
    ) -> None:
        self.enabled = enabled
        self.adaptive = adaptive
        self.budget_all_user_chars = budget_all_user_chars
        self.budget_recent_turns_chars = budget_recent_turns_chars
        self.budget_skills_specs_chars = budget_skills_specs_chars
        self.budget_files_total_chars = budget_files_total_chars
        self.budget_single_file_chars = budget_single_file_chars
        self.max_recent_entries = max_recent_entries
        self.max_modified_files = max_modified_files
        self.max_referenced_files = max_referenced_files
        self.max_modified_age_steps = max_modified_age_steps
        self.explicit_keys = explicit_keys


def _positive_int(raw: Any, default: int, minimum: int = 0) -> int:
    if isinstance(raw, int) and not isinstance(raw, bool) and raw >= minimum:
        return raw
    return default


def resolve_workspace_roots_from_payload(payload: dict[str, Any]) -> list[Path]:
    """Extract deduplicated workspace root directories from an Antigravity hook stdin payload."""
    path_cls = _path_cls()
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
            resolved = path_cls(norm).resolve()
        except OSError:
            continue
        key = canonical_path_key(str(resolved))
        if key not in seen:
            seen.add(key)
            roots.append(resolved)
    return roots


def load_rehydrate_config(workspace: Path | str | Sequence[Path | str] | None = None) -> RehydrateConfig:
    """Load optional global (`~/.agents/rehydrate.json`) and workspace (`.agents/rehydrate.json`) overrides.

    When multiple workspaces are supplied (`ws_roots[0]` primary, `ws_roots[1:]` secondary), files are
    merged in ascending precedence: global -> secondary workspaces (reverse order) -> primary workspace.
    """
    path_cls = _path_cls()
    ws_list: list[Path] = []
    seen_keys: set[str] = set()
    if workspace is None:
        raw_items: list[Path | str] = []
    elif isinstance(workspace, (str, os.PathLike)):
        raw_items = [workspace]
    else:
        raw_items = [w for w in workspace if w is not None]

    for item in raw_items:
        p = item if isinstance(item, path_cls) else path_cls(item)
        key = canonical_path_key(str(p))
        if key not in seen_keys:
            seen_keys.add(key)
            ws_list.append(p)

    merged: dict[str, Any] = {}
    candidates = [path_cls.home() / ".agents" / "rehydrate.json"]
    for ws in reversed(ws_list):
        candidates.append(ws / ".agents" / "rehydrate.json")

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


_NORM_PATH_CACHE: dict[str, str] = {}
_CANON_KEY_CACHE: dict[str, str] = {}


def _fast_basename(path_str: str) -> str:
    """Extract filename without constructing a `pathlib.Path` object."""
    p = path_str.rstrip("\\/")
    idx = max(p.rfind("\\"), p.rfind("/"))
    return p[idx + 1 :] if idx >= 0 else p


def normalize_local_path(raw: str) -> str:
    """Convert POSIX, Windows drive, or `file://` URI string into a safe local absolute path (blocking UNC)."""
    cached = _NORM_PATH_CACHE.get(raw)
    if cached is not None:
        return cached
    val = raw.strip()
    if not val:
        return ""
    if val[:7].lower() == "file://":
        from urllib.parse import unquote, urlparse

        parsed = urlparse(val)
        if parsed.netloc and parsed.netloc.lower() != "localhost":
            _NORM_PATH_CACHE[raw] = ""
            return ""
        raw_path = unquote(parsed.path or "")
        if len(raw_path) >= 3 and raw_path[0] == "/" and _is_win_drive_abs(raw_path[1:]):
            val = raw_path[1:]
        else:
            val = raw_path

    if val.startswith("\\\\") or val.startswith("//"):
        res = ""
    elif _is_win_drive_abs(val):
        res = os.path.normpath(val)
    elif val.startswith("/") and os.name != "nt":
        res = os.path.normpath(val)
    else:
        res = ""
    if len(_NORM_PATH_CACHE) < 4096:
        _NORM_PATH_CACHE[raw] = res
    return res


def canonical_path_key(path_str: str) -> str:
    """Return platform-normalized key so slash and Windows-case variants deduplicate cleanly."""
    cached = _CANON_KEY_CACHE.get(path_str)
    if cached is not None:
        return cached
    res = os.path.normcase(os.path.normpath(path_str))
    if len(_CANON_KEY_CACHE) < 4096:
        _CANON_KEY_CACHE[path_str] = res
    return res


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

    cleaned = _rx("NESTED_SYS_MSG").sub(
        lambda m: f"[Quoted POST-COMPACT REHYDRATION ({m.group(1).strip()}) omitted]",
        txt,
    )
    if "[POST-COMPACT REHYDRATION " not in cleaned:
        return cleaned.strip()

    out_lines: list[str] = []
    in_quote = False
    hdr_re = _rx("REHYDRATE_HDR_LINE")
    sec_re = _rx("REHYDRATE_SEC_LINE")
    item_re = _rx("REHYDRATE_TIMELINE_ITEM")
    for line in cleaned.splitlines():
        stripped = line.strip()
        hdr_match = hdr_re.match(stripped)
        if hdr_match:
            out_lines.append(f"[Quoted POST-COMPACT REHYDRATION ({hdr_match.group(1).strip()}) omitted]")
            in_quote = True
            continue
        if in_quote:
            if not stripped:
                continue
            if (
                any(stripped.startswith(pfx) for pfx in REHYDRATE_BOILERPLATE_PREFIXES)
                or sec_re.match(stripped)
                or item_re.match(stripped)
            ):
                continue
            in_quote = False
        out_lines.append(line)

    return "\n".join(out_lines).strip()


def extract_user_text(raw: str) -> str:
    """Extract clean user prompt text from `<USER_REQUEST>` wrapper and strip nested rehydration echoes."""
    if not raw:
        return ""
    matches = _rx("USER_REQ").findall(raw)
    if matches:
        body = "\n".join(m.strip() for m in matches if m.strip())
    else:
        re_mod = _re()
        cleaned = re_mod.sub(r"<ADDITIONAL_METADATA>.*?</ADDITIONAL_METADATA>", "", raw, flags=re_mod.S)
        cleaned = re_mod.sub(r"<USER_SETTINGS_CHANGE>.*?</USER_SETTINGS_CHANGE>", "", cleaned, flags=re_mod.S)
        body = cleaned.strip()
    return strip_nested_rehydration(body)


def is_compact_slash_cmd(txt: str) -> bool:
    """Return True if user message is solely the `/compact` trigger command."""
    return txt.strip().lower() == "/compact"


def is_low_info_short_turn(txt: str) -> bool:
    """Filter ultra-short backchannels/elliptical turns (<=4 non-punct chars, no ASCII/digits/slashes)."""
    if len(txt) > 24 or _rx("ASCII_ALNUM_OR_SLASH").search(txt) or any(ch.isdigit() for ch in txt):
        return False
    import unicodedata

    core = "".join(
        ch
        for ch in txt
        if not ch.isspace() and unicodedata.category(ch)[0] not in ("P", "S", "Z", "C")
    )
    return len(core) <= SHORT_USER_MAX_CHARS


def _strip_leading_ui_tool_badges(txt: str) -> str:
    """Collapse leading copied Antigravity UI step-badge lines (`Ran ...`, `Analyzed ...`) so prose isn't buried."""
    lines = txt.splitlines()
    badge_count = 0
    idx = 0
    badge_re = _rx("UI_BADGE_LINE")
    while idx < len(lines):
        stripped = lines[idx].strip()
        if not stripped:
            idx += 1
            continue
        if badge_re.match(stripped):
            badge_count += 1
            idx += 1
            continue
        break
    if badge_count >= 3 and idx < len(lines):
        rest = "\n".join(lines[idx:]).lstrip()
        if rest:
            return f"[{badge_count} UI tool-log lines]\n{rest}"
    return txt


def _balance_code_fences(head: str, tail: str) -> tuple[str, str]:
    """Close unclosed triple-backtick code fences in `head` and reopen in `tail` if split mid-block."""
    head_fences = sum(1 for ln in head.splitlines() if ln.lstrip().startswith("```"))
    tail_fences = sum(1 for ln in tail.splitlines() if ln.lstrip().startswith("```"))
    if head_fences % 2 == 1:
        head = f"{head.rstrip()}\n```"
    if tail_fences % 2 == 1:
        tail = f"```\n{tail.lstrip()}"
    return head, tail


def _is_table_sep_line(line: str) -> bool:
    s = line.strip()
    return bool(s) and "-" in s and "|" in s and set(s) <= set("|:- \t")


def _fold_markdown_middle(
    text: str,
    head_chars: int,
    tail_chars: int,
    marker_template: str,
    min_omitted: int = 150,
) -> tuple[str, bool]:
    """Fold long Markdown middle at line boundaries, stripping dangling table headers and balancing fences."""
    total = len(text)
    if total <= head_chars + tail_chars + min_omitted:
        return text, False

    head_min = max(80, int(head_chars * 0.55))
    head_max = min(total, head_chars + 140)
    head_end = head_chars
    para_break = text.rfind("\n\n", head_min, head_max)
    if para_break >= int(head_chars * 0.72):
        head_end = para_break
    else:
        nl = text.rfind("\n", head_min, head_max)
        if nl >= head_min:
            head_end = nl
        else:
            nl_prev = text.rfind("\n", 80, head_min)
            if nl_prev >= 80:
                head_end = nl_prev
            else:
                nl_next = text.find("\n", head_max, max(head_max, total - tail_chars - min_omitted))
                if nl_next >= 0:
                    head_end = nl_next
                else:
                    sp = text.rfind(" ", max(80, head_chars - 200), head_max)
                    if sp > 0:
                        head_end = sp

    head = text[:head_end].rstrip()
    head_lines = head.splitlines()
    while len(head_lines) >= 2 and _is_table_sep_line(head_lines[-1]) and "|" in head_lines[-2]:
        candidate = "\n".join(head_lines[:-2]).rstrip()
        if len(candidate) >= 120:
            head_lines = candidate.splitlines()
            head = candidate
        else:
            break
    while head_lines and (head_lines[-1].lstrip().startswith("#") or _is_table_sep_line(head_lines[-1])):
        candidate = "\n".join(head_lines[:-1]).rstrip()
        if len(candidate) >= 120:
            head_lines = candidate.splitlines()
            head = candidate
        else:
            break

    raw_tail = total - tail_chars
    tail_lo = max(len(head), total - tail_chars - max(140, int(tail_chars * 0.30)))
    tail_hi = total - max(40, int(tail_chars * 0.55))
    tail_start = raw_tail
    best_dist = 10**9
    pos = tail_lo
    while pos < tail_hi:
        nl = text.find("\n", pos, tail_hi)
        if nl < 0:
            break
        cand_start = nl + 1
        dist = abs(cand_start - raw_tail)
        if dist < best_dist or (dist == best_dist and cand_start < tail_start):
            best_dist = dist
            tail_start = cand_start
        pos = nl + 1
    if best_dist == 10**9:
        nl_after = text.find("\n", max(len(head), tail_hi), max(len(head), total - 40))
        if nl_after >= 0:
            tail_start = nl_after + 1
        else:
            nl_before = text.rfind("\n", min(tail_lo, len(head) + min_omitted), tail_lo)
            if nl_before >= 0 and nl_before + 1 > len(head):
                tail_start = nl_before + 1
            else:
                sp_t = text.find(" ", max(len(head), raw_tail), min(total - 40, raw_tail + 200))
                if sp_t >= 0:
                    tail_start = sp_t + 1

    omitted = max(0, tail_start - len(head))
    if omitted <= min_omitted:
        return text, False

    tail = text[tail_start:].lstrip("\r\n")
    head, tail = _balance_code_fences(head, tail)
    marker = marker_template.format(omitted=omitted)
    return f"{head}\n{marker}\n{tail}", True


def fold_user_paste(
    txt: str,
    seg_path: Path | None,
    line_no: int,
    step_idx: int,
    truncated_prefixes: list[str] | None = None,
) -> tuple[str, bool]:
    """Fold long pasted blocks (>800 chars), recovering only the tail when `<CONTEXT_SUMMARY>` kept the prefix."""
    loc = f"{seg_path} line {line_no} (step {step_idx})" if seg_path else f"step {step_idx}"
    if truncated_prefixes and len(txt) > 550:
        norm_txt = _norm_prompt_for_dedup(txt)
        if any(norm_txt.startswith(pfx) for pfx in truncated_prefixes):
            tail_keep = min(len(txt), 550)
            return (
                f"[Opening preserved in <CONTEXT_SUMMARY>; recovered truncated tail "
                f"({len(txt)} chars total; read_file {loc} for full text)]:\n"
                f"...{txt[-tail_keep:]}"
            ), True

    cleaned = _strip_leading_ui_tool_badges(txt)
    if len(cleaned) <= FOLD_PASTE_THRESHOLD_CHARS:
        return cleaned, cleaned != txt
    folded, did_fold = _fold_markdown_middle(
        cleaned,
        FOLD_PASTE_HEAD_CHARS,
        FOLD_PASTE_TAIL_CHARS,
        f"  ...[folded {{omitted}} chars of pasted content; read_file {loc} if exact text needed]...",
        min_omitted=120,
    )
    return folded, did_fold or (cleaned != txt)


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
        label = _fast_basename(fpath) if fpath else ""
        summary = f"{name}({label or fpath})" if (label or fpath) else name
    elif name in ("run_command", "run_in_terminal") and isinstance(args, dict):
        cmd = str(args.get("CommandLine") or args.get("Command") or "").strip().replace("\n", " ")
        if len(cmd) > 60:
            cmd = cmd[:57] + "..."
        summary = f"{name}(`{cmd}`)"
    else:
        summary = name
    return name, summary, fpath, line_range, snippets


def _parse_signed_int_prefix(s: str) -> int | None:
    s = s.lstrip()
    if not s:
        return None
    neg = False
    if s[0] == "-":
        neg = True
        s = s[1:]
    end = 0
    n = len(s)
    while end < n and s[end].isdigit():
        end += 1
    if end == 0:
        return None
    val = int(s[:end])
    return -val if neg else val


def classify_tool_step(step_obj: dict[str, Any]) -> tuple[str, str]:
    """Classify a tool step as `(state, task_id)` where `state` is `'ok'`, `'error'`, or `'running'`."""
    status = step_obj.get("status")
    content = str(step_obj.get("content") or "")
    if status == "RUNNING":
        marker = "Tool is running as a background task with task id:"
        pos = content.find(marker, 0, 512)
        task_id = ""
        if pos >= 0:
            after = content[pos + len(marker) :].lstrip()
            if after:
                task_id = after.split(None, 1)[0]
        return "running", task_id
    if isinstance(status, str) and status != "DONE":
        return "error", ""
    if step_obj.get("error"):
        return "error", ""
    header_part = content[:768].replace("\r\n", "\n")
    cut = len(header_part)
    for sep in ("\n\nCommand output:", "\n\nNo output from", "\nShowing lines "):
        idx = header_part.find(sep)
        if 0 <= idx < cut:
            cut = idx
    header_part = header_part[:cut]
    if "Encountered error in step execution:" in header_part:
        return "error", ""
    exit_marker = "Command exited with code "
    epos = header_part.find(exit_marker)
    if epos >= 0:
        code_val = _parse_signed_int_prefix(header_part[epos + len(exit_marker) :])
        if code_val is not None and code_val != 0:
            return "error", ""
    return "ok", ""


def is_tool_step_successful(step_obj: dict[str, Any]) -> bool:
    """Verify from transcript step record that a tool execution actually succeeded (`DONE` with no error)."""
    return classify_tool_step(step_obj)[0] == "ok"


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
    p = _path_cls()(norm)
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
    upper = _fast_basename(path_str).upper()
    if not upper.endswith(".MD"):
        return False
    return upper == "SKILL.MD" or any(k in upper for k in ("SPEC", "PLAN", "TEMPLATE", "HANDOFF"))


PROSE_DOC_EXTS = frozenset({".md", ".mdx", ".rst", ".txt", ".adoc"})
PROSE_DOC_STEMS = frozenset({"readme", "changelog", "history", "notice", "license", "copying", "authors", "contributing"})
CONFIG_SCRIPT_EXTS = frozenset(
    {
        ".json", ".jsonc", ".json5", ".toml", ".yaml", ".yml", ".ini", ".cfg", ".conf", ".lock",
        ".bat", ".cmd", ".sh", ".bash", ".zsh", ".ps1", ".psm1",
        ".cmake", ".mk", ".gradle", ".props", ".targets", ".xml", ".plist",
        ".gitattributes", ".gitignore", ".editorconfig",
    }
)
CONFIG_SCRIPT_NAMES = frozenset(
    {"makefile", "dockerfile", "containerfile", "justfile", "procfile", ".gitattributes", ".gitignore", ".editorconfig"}
)


def _modified_file_tier(path_str: str) -> int:
    """Classify modified files: `3` = primary source, `2` = config/build/wrapper, `1` = test, `0` = prose doc."""
    name_low = _fast_basename(path_str).lower()
    dot_idx = name_low.rfind(".")
    ext = name_low[dot_idx:] if dot_idx >= 0 else ""
    stem_low = name_low.split(".", 1)[0]
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
    if ext in CONFIG_SCRIPT_EXTS or name_low in CONFIG_SCRIPT_NAMES:
        return 2
    return 3


def default_custom_skill_roots(artifact_dir: Path | None = None) -> list[Path]:
    """Return allowed non-workspace roots for Section 3 custom `SKILL.md` and session plan artifacts."""
    home = _path_cls().home()
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
    tail_len = max(0, max_chars - head_len - 84)
    if head_len > 80:
        nl_h = text.rfind("\n", int(head_len * 0.65), head_len + 1)
        if nl_h > 0:
            head_len = nl_h
    if tail_len > 80:
        raw_tail_start = len(text) - tail_len
        nl_t = text.find("\n", raw_tail_start, len(text) - int(tail_len * 0.65))
        if nl_t >= 0 and nl_t + 1 < len(text):
            tail_len = len(text) - (nl_t + 1)
    omitted = max(0, len(text) - head_len - tail_len)
    mid_msg = f"\n... [truncated ~{omitted} chars in middle; read_file for full content] ...\n"
    head = text[:head_len]
    tail = text[-tail_len:] if tail_len > 0 else ""
    excess = len(head) + len(mid_msg) + len(tail) - max_chars
    if excess > 0:
        if tail_len > excess + 20:
            tail = tail[excess:]
        elif head_len > excess + 20:
            head = head[: len(head) - excess]
        else:
            return text[:max_chars]
    return f"{head}{mid_msg}{tail}"


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
    return " ".join(txt.split())


def _split_checkpoint_user_requests(raw_block: str) -> list[tuple[int, str]]:
    """Split `# User Requests` into `(req_no, item_text)` pairs without breaking on embedded Markdown numbered lists."""
    items: list[tuple[int, str]] = []
    cur_top = 0
    sub_num = 0
    cur_lines: list[str] = []
    num_re = _re().compile(r"^(\d+)\.\s+(.*)$")
    trunc_re = _rx("CHECKPOINT_TRUNCATED_TAIL")

    for line in raw_block.splitlines():
        m = num_re.match(line)
        if m:
            n = int(m.group(1))
            rest = m.group(2)
            if cur_top == 0 and n == 1:
                cur_top = 1
                cur_lines = [rest]
                continue
            if cur_top >= 1:
                prev_nonempty = next((ln.strip() for ln in reversed(cur_lines) if ln.strip()), "")
                prev_was_truncated = bool(trunc_re.search(prev_nonempty))
                if prev_was_truncated:
                    sub_num = 0
                if n == cur_top + 1 and sub_num == 0:
                    items.append((cur_top, "\n".join(cur_lines).strip()))
                    cur_top = n
                    cur_lines = [rest]
                    continue
                if n == 1:
                    sub_num = 1
                elif sub_num > 0 and n == sub_num + 1:
                    sub_num = n
        if cur_top >= 1:
            cur_lines.append(line)

    if cur_top >= 1 and cur_lines:
        items.append((cur_top, "\n".join(cur_lines).strip()))
    return items


def extract_checkpoint_truncated_prefixes(cp_content: str) -> list[str]:
    """Extract normalized prefixes of `# User Requests` items truncated by `<truncated N bytes>`."""
    if not cp_content or "<truncated " not in cp_content:
        return []
    m = _rx("CHECKPOINT_USER_REQS").search(cp_content)
    if not m:
        return []
    att_re = _rx("CHECKPOINT_ATTACHMENT_LINE")
    trunc_re = _rx("CHECKPOINT_TRUNCATED_TAIL")
    prefixes: list[str] = []
    for _, item in _split_checkpoint_user_requests(m.group(1)):
        cleaned = att_re.sub("", item).strip()
        if not trunc_re.search(cleaned):
            continue
        pfx = trunc_re.sub("", cleaned).strip()
        norm_pfx = _norm_prompt_for_dedup(pfx)
        if len(norm_pfx) >= 120:
            prefixes.append(norm_pfx)
    return prefixes


def extract_checkpoint_user_norm_counts(cp_content: str) -> dict[str, int]:
    """Count normalized `# User Requests` items in native `CHECKPOINT` for multiset-safe deduplication."""
    if not cp_content:
        return {}
    m = _rx("CHECKPOINT_USER_REQS").search(cp_content)
    if not m:
        return {}
    att_re = _rx("CHECKPOINT_ATTACHMENT_LINE")
    counts: dict[str, int] = {}
    for _, item in _split_checkpoint_user_requests(m.group(1)):
        cleaned = item.strip()
        if not cleaned or is_compact_slash_cmd(cleaned):
            continue
        variants: set[str] = set()
        variants.add(_norm_prompt_for_dedup(cleaned))
        no_att = att_re.sub("", cleaned).strip()
        if no_att and not is_compact_slash_cmd(no_att):
            variants.add(_norm_prompt_for_dedup(no_att))
            stripped_nested = strip_nested_rehydration(no_att)
            if stripped_nested:
                variants.add(_norm_prompt_for_dedup(stripped_nested))
        else:
            stripped_nested = strip_nested_rehydration(cleaned)
            if stripped_nested:
                variants.add(_norm_prompt_for_dedup(stripped_nested))
        for v in variants:
            if v:
                counts[v] = counts.get(v, 0) + 1
    return counts


def extract_checkpoint_user_norms(cp_content: str) -> set[str]:
    """Parse native `CHECKPOINT` `# User Requests` items (stripping attachment sub-lines) to prevent duplication."""
    return set(extract_checkpoint_user_norm_counts(cp_content).keys())


def extract_checkpoint_attachment_refs(cp_content: str, media_step_by_name: dict[str, int]) -> list[str]:
    """Map `<CONTEXT_SUMMARY>` `# User Requests` attachment lines back to their original pre-compaction step indices."""
    if not cp_content:
        return []
    m = _rx("CHECKPOINT_USER_REQS").search(cp_content)
    if not m:
        return []
    att_re = _rx("CHECKPOINT_ATTACHMENT_LINE")
    uri_re = _rx("ATTACHMENT_URI_PAREN")
    refs: list[str] = []
    for req_no, req_body in _split_checkpoint_user_requests(m.group(1)):
        for att_match in att_re.finditer(req_body):
            att_desc = att_match.group(1).strip()
            uri_match = uri_re.search(att_desc)
            if uri_match:
                raw_uri = uri_match.group(1).strip().replace("\\", "/")
                bname = _fast_basename(raw_uri)
            else:
                bname = att_desc
            orig_step = media_step_by_name.get(bname.lower())
            if orig_step is not None:
                refs.append(f"#{req_no} `{bname}` @ step {orig_step}")
            else:
                refs.append(f"#{req_no} `{bname}`")
    return refs
