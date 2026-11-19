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
        r"Earlier Checkpoints Milestone Index|"
        r"Recent Pre-Compaction (?:Verbatim )?Dialogue|"
        r"Final Pre-Compaction Exchange|"
        r"Workspace Reality & File Status Notice|"
        r"Restored Active Skills & Spec Docs|"
        r"Restored Active Workspace Files"
        r")\b.*$",
        0,
    ),
    "REHYDRATE_TIMELINE_ITEM": (
        r"^-\s+(?:\[#\d+\s+@\s+step\s+\d+\]:|\[cp\s+@\s+step\s+\d+,|---\s+\[/fork branch ).*$",
        0,
    ),
    "CHECKPOINT_USER_REQS": (
        r"#\s*User Requests\s*\n(?:The following were .*?\n)?(.*?)(?=\n<summary>|\n#\s+[A-Za-z]|\Z)",
        _RE_DOTALL,
    ),
    "SUMMARY_BLOCK": (r"<summary>(.*?)</summary>", _RE_DOTALL),
    "MILESTONE_TOKEN": (
        r"`([^`\n]{2,64})`|([A-Za-z_][A-Za-z0-9_.\-/]{2,48})|([\u4e00-\u9fff]{2,6})",
        0,
    ),
    "ABS_PATH_REF": (
        r"(?:file:///)?(?:[A-Za-z]:[/\\][^\s`\"'<>|()\[\]{},;，。、；：（）「」『』【】《》]+|"
        r"/(?:Users|home|workspace|workspaces|srv|opt|var|mnt|Volumes|app|repo|src|root|private)/[^\s`\"'<>|()\[\]{},;，。、；：（）「」『』【】《》]+)",
        0,
    ),
    "STRUCTURED_MD_HEADING": (r"^\s{0,3}(?:#{1,4}\s+\S|\d+\.\s+\S|\|[^\n]+\|)", _RE_MULTILINE),
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
    "*(Earlier milestones from prior checkpoints no longer detailed in ",
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


_CACHE_MISS = object()


def _norm_path_for_prefix(p: Path | str) -> str:
    """Return platform-normalized path string suitable for exact/prefix boundary checks."""
    s = os.path.normcase(os.path.normpath(str(p)))
    if len(s) > 1 and not (os.name == "nt" and len(s) == 3 and s[1] == ":" and s[2] == os.sep):
        s = s.rstrip("\\/")
    return s


def _is_subpath_norm(c_norm: str, p_norm: str) -> bool:
    if c_norm == p_norm:
        return True
    if p_norm.endswith(os.sep):
        return c_norm.startswith(p_norm)
    return c_norm.startswith(p_norm + os.sep)


def _is_subpath(child: Path, parent: Path) -> bool:
    return _is_subpath_norm(_norm_path_for_prefix(child), _norm_path_for_prefix(parent))


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
            cleaned = val.strip("\r\n")[:600]
            snippets.append(f"{line_pfx}{cleaned}")
    chunks = args.get("ReplacementChunks")
    if isinstance(chunks, list):
        for ch in chunks:
            if isinstance(ch, dict):
                cs, ce = extract_tool_line_range(ch)
                cpfx = f"@@LINES:{cs or ''}:{ce or ''}@@\n" if (cs or ce) else line_pfx
                rc = ch.get("ReplacementContent")
                if isinstance(rc, str) and rc.strip():
                    cleaned_rc = rc.strip("\r\n")[:600]
                    snippets.append(f"{cpfx}{cleaned_rc}")
                elif cpfx:
                    snippets.append(cpfx.rstrip("\n"))
    if not snippets and line_pfx:
        snippets.append(line_pfx.rstrip("\n"))
    return snippets


def _clip_inline_label(text: str, max_chars: int = 140) -> str:
    """Collapse internal whitespace and truncate an inline tool summary label."""
    s = " ".join(text.split())
    if len(s) <= max_chars:
        return s
    return s[: max(1, max_chars - 3)].rstrip() + "..."


def format_ask_question_exchange(questions: list[str], raw_content: str) -> str:
    """Format an `ask_question` Q&A exchange by stripping leading timestamp metadata and echoes."""
    if not raw_content:
        return ""
    lines = raw_content.replace("\r\n", "\n").splitlines()
    idx = 0
    while idx < len(lines):
        s = lines[idx].strip()
        if not s or s.startswith("Created At:") or s.startswith("Completed At:"):
            idx += 1
            continue
        break
    cleaned_ans = strip_nested_rehydration("\n".join(lines[idx:])).strip()
    if not cleaned_ans:
        return ""

    q_clean = [" ".join(q.split()) for q in questions if q and q.strip()]
    if not q_clean:
        return " ".join(cleaned_ans.split())
    if len(q_clean) == 1:
        return f"{q_clean[0]} -> {' '.join(cleaned_ans.split())}"

    ans_re = _re().compile(r"^A(\d+)\s*:\s*(.*)$")
    parsed_pairs: list[str] = []
    all_matched = True
    for ln in cleaned_ans.splitlines():
        s_ln = ln.strip()
        if not s_ln:
            continue
        m = ans_re.match(s_ln)
        if not m:
            all_matched = False
            break
        q_idx = int(m.group(1))
        ans_part = " ".join(m.group(2).split())
        if not (1 <= q_idx <= len(q_clean)) or not ans_part:
            all_matched = False
            break
        parsed_pairs.append(f"Q{q_idx} ({q_clean[q_idx - 1]}) -> A{q_idx}: {ans_part}")
    if all_matched and parsed_pairs:
        return "; ".join(parsed_pairs)
    return f"{' / '.join(q_clean)} -> {' '.join(cleaned_ans.split())}"


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
    elif name == "ask_question" and isinstance(args, dict):
        raw_qs = args.get("questions") or []
        if isinstance(raw_qs, list):
            snippets = [
                str(q.get("question") or "").strip()
                for q in raw_qs
                if isinstance(q, dict) and str(q.get("question") or "").strip()
            ]
        summary = "ask_question"
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


def _is_sensitive_name_str(name_lower: str) -> bool:
    if name_lower in SENSITIVE_BASENAMES:
        return True
    return name_lower == ".env" or (name_lower.startswith(".env.") and name_lower not in SAFE_ENV_TEMPLATES)


def is_sensitive_filename(p: Path) -> bool:
    """Check whether `p` is a `.env*` secret file or private credential file."""
    return _is_sensitive_name_str(p.name.lower())


def _resolve_path_cached(
    norm: str,
    resolve_cache: dict[str, Path | None] | None = None,
) -> Path | None:
    if resolve_cache is not None:
        cached = resolve_cache.get(norm, _CACHE_MISS)
        if cached is not _CACHE_MISS:
            return cached  # type: ignore[return-value]
    try:
        resolved = _path_cls()(norm).resolve()
    except OSError:
        resolved = None
    if resolve_cache is not None:
        resolve_cache[norm] = resolved
    return resolved


def _prepare_root_prefixes(
    roots: Sequence[Path] | None,
    resolve_cache: dict[str, Path | None] | None = None,
) -> tuple[str, ...]:
    if not roots:
        return ()
    out: list[str] = []
    seen: set[str] = set()
    for r in roots:
        r_str = str(r)
        res = _resolve_path_cached(r_str, resolve_cache)
        pref = _norm_path_for_prefix(res if res is not None else r)
        if pref not in seen:
            seen.add(pref)
            out.append(pref)
    return tuple(out)


def is_trackable_workspace_path(
    path_str: str,
    workspace_roots: list[Path] | None = None,
    allow_skill_or_artifact_roots: list[Path] | None = None,
    excluded_roots: list[Path] | None = None,
    *,
    resolve_cache: dict[str, Path | None] | None = None,
    _allowed_prefixes: tuple[str, ...] | None = None,
    _excluded_prefixes: tuple[str, ...] | None = None,
) -> tuple[bool, Path | None]:
    """Validate path safety & workspace scope without requiring the target to currently exist on disk."""
    norm = normalize_local_path(path_str)
    if not norm:
        return False, None

    # Pre-resolve fast deny on obvious blacklisted paths/extensions/excluded roots (never bypasses post-resolve checks)
    norm_slash = norm.replace("\\", "/").lower()
    norm_slash_dir = norm_slash if norm_slash.endswith("/") else norm_slash + "/"
    for sub in SKIP_PATH_SUBSTRINGS:
        if sub in norm_slash or sub in norm_slash_dir:
            return False, None
    base_low = _fast_basename(norm).lower()
    if _is_sensitive_name_str(base_low):
        return False, None
    dot_i = base_low.rfind(".")
    if dot_i > 0 and base_low[dot_i:] in SKIP_EXTS:
        return False, None
    excl_prefixes = (
        _excluded_prefixes
        if _excluded_prefixes is not None
        else _prepare_root_prefixes(excluded_roots, resolve_cache)
    )
    if excl_prefixes and any(_is_subpath_norm(_norm_path_for_prefix(norm), ep) for ep in excl_prefixes):
        return False, None

    resolved = _resolve_path_cached(norm, resolve_cache)
    if resolved is None:
        return False, None

    res_str = str(resolved)
    slash_lower = res_str.replace("\\", "/").lower()
    slash_dir = slash_lower if slash_lower.endswith("/") else slash_lower + "/"
    for sub in SKIP_PATH_SUBSTRINGS:
        if sub in slash_lower or sub in slash_dir:
            return False, None
    if is_sensitive_filename(resolved) or resolved.suffix.lower() in SKIP_EXTS:
        return False, None

    res_norm = _norm_path_for_prefix(res_str)
    if excl_prefixes and any(_is_subpath_norm(res_norm, ep) for ep in excl_prefixes):
        return False, None

    if _allowed_prefixes is not None:
        if _allowed_prefixes and not any(_is_subpath_norm(res_norm, ap) for ap in _allowed_prefixes):
            return False, None
    elif workspace_roots:
        allowed_roots = list(workspace_roots)
        if allow_skill_or_artifact_roots:
            allowed_roots.extend(allow_skill_or_artifact_roots)
        allowed_prefixes = _prepare_root_prefixes(allowed_roots, resolve_cache)
        if not any(_is_subpath_norm(res_norm, ap) for ap in allowed_prefixes):
            return False, None

    return True, resolved


def is_eligible_text_file(
    path_str: str,
    workspace_roots: list[Path] | None = None,
    allow_skill_or_artifact_roots: list[Path] | None = None,
    excluded_roots: list[Path] | None = None,
    *,
    resolve_cache: dict[str, Path | None] | None = None,
    _allowed_prefixes: tuple[str, ...] | None = None,
    _excluded_prefixes: tuple[str, ...] | None = None,
) -> bool:
    """Verify `path_str` is an existing, non-sensitive text file within allowed scope and <= 2 MiB."""
    ok, resolved = is_trackable_workspace_path(
        path_str,
        workspace_roots=workspace_roots,
        allow_skill_or_artifact_roots=allow_skill_or_artifact_roots,
        excluded_roots=excluded_roots,
        resolve_cache=resolve_cache,
        _allowed_prefixes=_allowed_prefixes,
        _excluded_prefixes=_excluded_prefixes,
    )
    if not ok or resolved is None:
        return False

    try:
        st = os.stat(resolved)
        if (st.st_mode & 0o170000) != 0o100000:
            return False
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


def _clip_markdown_to_budget(
    text: str,
    max_chars: int,
    marker_template: str = "  ...[truncated ~{omitted} chars to fit budget; read_file for full content]...",
) -> str:
    """Clip Markdown text within `max_chars` while keeping code fences balanced and preserving line/link safety."""
    if len(text) <= max_chars:
        return text

    def _fence_count(s: str) -> int:
        return sum(1 for ln in s.splitlines() if ln.lstrip().startswith("```"))

    def _safe_prefix(s: str, limit: int) -> str:
        if limit <= 0:
            return ""
        sub = s if len(s) <= limit else s[:limit]
        if _fence_count(sub) % 2 == 1 and limit >= 4:
            sub2 = s[: limit - 4].rstrip("\r\n")
            sub = f"{sub2}\n```" if _fence_count(sub2) % 2 == 1 else sub2
        return sub[:limit]

    if max_chars < 260:
        return _safe_prefix(text, max_chars)

    for reserve in (120, 280):
        avail = max(120, max_chars - reserve)
        h_target = int(avail * 0.66)
        t_target = max(40, avail - h_target)
        folded, did_fold = _fold_markdown_middle(
            text, h_target, t_target, marker_template, min_omitted=40
        )
        if did_fold and len(folded) <= max_chars:
            return folded

    safe_cap = max(60, max_chars - 8)
    head_len = int(safe_cap * 0.68)
    tail_len = max(0, safe_cap - head_len - 84)
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
    raw_head = text[:head_len]
    raw_tail = text[-tail_len:] if tail_len > 0 else ""
    excess = len(raw_head) + len(mid_msg) + len(raw_tail) - safe_cap
    if excess > 0:
        if len(raw_tail) > excess + 20:
            raw_tail = raw_tail[excess:]
        elif len(raw_head) > excess + 20:
            raw_head = raw_head[: len(raw_head) - excess]
    head, tail = _balance_code_fences(raw_head, raw_tail)
    res = f"{head}{mid_msg}{tail}"
    if len(res) > max_chars:
        return _safe_prefix(text, max_chars)
    return res


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
    line_cum = [0] * (total_lines + 1)
    acc = 0
    for idx_l, ln_txt in enumerate(lines, 1):
        acc += len(ln_txt) + 1
        line_cum[idx_l] = acc

    def _span_chars(s_ln: int, e_ln: int) -> int:
        return max(0, (line_cum[e_ln] - line_cum[s_ln - 1]) - 1) if e_ln >= s_ln else 0

    def _gap_line(a: int, b: int, first_internal: bool) -> str:
        if first_internal:
            return f"... [lines {a}-{b} omitted; read_file for full context] ..."
        return f"... [lines {a}-{b} omitted] ..."

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
    base_radii = (
        {scaled_ctx, context_lines, 28, 20, 15, 11, 8, 5, 3, 1, 0}
        if context_lines >= 30
        else {context_lines, 8, 5, 3, 1, 0}
    )
    radius_steps = sorted(base_radii, reverse=True)
    chosen_merged: list[list[int]] = []
    chosen_hdr_lines = 8
    chosen_fits = False
    last_merged: list[list[int]] = []

    for ctx in radius_steps:
        raw_spans = [(max(1, s - ctx), min(total_lines, e + ctx)) for s, e in anchor_spans]
        raw_spans.sort()
        merge_gap = 6 if ctx >= 8 else 2
        merged: list[list[int]] = []
        for s_ln, e_ln in raw_spans:
            if merged:
                prev_e = merged[-1][1]
                gap_lines = s_ln - prev_e - 1
                if s_ln <= prev_e + merge_gap or (
                    0 < gap_lines <= 4 and _span_chars(prev_e + 1, s_ln - 1) <= 115
                ):
                    merged[-1][1] = max(prev_e, e_ln)
                    continue
            merged.append([s_ln, e_ln])
        last_merged = merged

        if cap >= 5000 and ctx >= 28:
            hdr_lines = 35
        elif ctx >= 8:
            hdr_lines = 20
        elif ctx >= 2:
            hdr_lines = 12
        else:
            hdr_lines = 8
        header_end = min(total_lines, hdr_lines)
        has_sep_hdr = merged[0][0] > header_end + 6
        first_s = merged[0][0] if has_sep_hdr else 1

        # Exact O(H) length calculation before allocating any joined strings
        est_len = 0
        block_count = 0
        if has_sep_hdr:
            est_len += len(f"# [Lines 1-{header_end}: file header]\n") + _span_chars(1, header_end)
            block_count += 1
        prev_end = header_end if has_sep_hdr else 0
        saw_gap = False
        for idx_m, (raw_s, e_ln) in enumerate(merged):
            s_ln = first_s if idx_m == 0 else raw_s
            if prev_end and s_ln > prev_end + 1:
                est_len += len(_gap_line(prev_end + 1, s_ln - 1, not saw_gap))
                saw_gap = True
                block_count += 1
            est_len += len(f"# [Lines {s_ln}-{e_ln}: active edit region]\n") + _span_chars(s_ln, e_ln)
            block_count += 1
            prev_end = e_ln
        if prev_end < total_lines:
            est_len += len(f"... [lines {prev_end + 1}-{total_lines} omitted] ...")
            block_count += 1
        est_len += max(0, block_count - 1)

        chosen_merged = merged
        chosen_hdr_lines = hdr_lines
        if est_len <= cap:
            chosen_fits = True
            break

    assembled = ""
    range_labels: list[str] = []
    if chosen_merged and (chosen_fits or not last_merged):
        header_end = min(total_lines, chosen_hdr_lines)
        blocks: list[str] = []
        if chosen_merged[0][0] > header_end + 6:
            hdr_body = "\n".join(lines[:header_end])
            blocks.append(f"# [Lines 1-{header_end}: file header]\n{hdr_body}")
            range_labels.append(f"1-{header_end}")
        else:
            chosen_merged[0][0] = 1
        prev_end = header_end if range_labels else 0
        saw_gap = False
        for s_ln, e_ln in chosen_merged:
            if prev_end and s_ln > prev_end + 1:
                blocks.append(_gap_line(prev_end + 1, s_ln - 1, not saw_gap))
                saw_gap = True
            chunk_body = "\n".join(lines[s_ln - 1 : e_ln])
            blocks.append(f"# [Lines {s_ln}-{e_ln}: active edit region]\n{chunk_body}")
            range_labels.append(f"{s_ln}-{e_ln}")
            prev_end = e_ln
        if prev_end < total_lines:
            blocks.append(f"... [lines {prev_end + 1}-{total_lines} omitted] ...")
        assembled = "\n".join(blocks)

    # If even ctx=0 exceeds cap across multiple hunks, water-fill per-hunk line budgets so every hunk stays visible
    if (not chosen_fits or len(assembled) > cap) and last_merged:
        hdr_lines = 8 if cap >= 900 else 0
        header_end = min(total_lines, hdr_lines)
        wf_merged = [[s_ln, e_ln] for s_ln, e_ln in last_merged]
        has_sep_hdr = hdr_lines > 0 and wf_merged[0][0] > header_end + 4
        if hdr_lines > 0 and not has_sep_hdr:
            wf_merged[0][0] = 1
        hdr_block = f"# [Lines 1-{header_end}: file header]\n" + "\n".join(lines[:header_end]) if has_sep_hdr else ""

        # Compute exact fixed decorator overhead so every remaining char goes to code lines
        overhead = (len(hdr_block) + 1) if hdr_block else 0
        prev_e_est = header_end if has_sep_hdr else 0
        saw_gap_est = False
        for s_ln, e_ln in wf_merged:
            if prev_e_est and s_ln > prev_e_est + 1:
                overhead += len(_gap_line(prev_e_est + 1, s_ln - 1, not saw_gap_est)) + 1
                saw_gap_est = True
            overhead += len(f"# [Lines {s_ln}-{e_ln}: active edit region (trimmed from {e_ln})]\n") + 1
            prev_e_est = e_ln
        if prev_e_est < total_lines:
            overhead += len(f"... [lines {prev_e_est + 1}-{total_lines} omitted] ...") + 1

        avail_hunks = max(0, cap - overhead)
        if avail_hunks >= len(wf_merged) * 60:
            raw_lens = [_span_chars(s_ln, e_ln) for s_ln, e_ln in wf_merged]
            hunk_budgets = [0] * len(wf_merged)
            remaining_idx = list(range(len(wf_merged)))
            rem_budget = avail_hunks
            # Water-filling: keep short hunks intact and distribute surplus to longer hunks
            while remaining_idx:
                share = rem_budget // len(remaining_idx)
                fitted = [i for i in remaining_idx if raw_lens[i] <= share]
                if not fitted:
                    for i in remaining_idx:
                        hunk_budgets[i] = max(50, share)
                    break
                for i in fitted:
                    hunk_budgets[i] = raw_lens[i]
                    rem_budget -= raw_lens[i]
                remaining_idx = [i for i in remaining_idx if i not in fitted]

            wf_blocks: list[str] = []
            wf_labels: list[str] = []
            if has_sep_hdr:
                wf_blocks.append(hdr_block)
                wf_labels.append(f"1-{header_end}")
            prev_end = header_end if has_sep_hdr else 0
            saw_gap = False
            for idx_h, (s_ln, e_ln) in enumerate(wf_merged):
                if prev_end and s_ln > prev_end + 1:
                    wf_blocks.append(_gap_line(prev_end + 1, s_ln - 1, not saw_gap))
                    saw_gap = True
                h_cap = hunk_budgets[idx_h]
                h_lines = lines[s_ln - 1 : e_ln]
                kept_h: list[str] = []
                used_h = 0
                for ln in h_lines:
                    add_len = len(ln) + (1 if kept_h else 0)
                    if kept_h and used_h + add_len > h_cap:
                        break
                    kept_h.append(ln)
                    used_h += add_len
                if not kept_h and h_lines:
                    kept_h.append(h_lines[0][: max(40, h_cap)])
                shown_e = s_ln + len(kept_h) - 1
                chunk_body = "\n".join(kept_h)
                if shown_e < e_ln:
                    wf_blocks.append(
                        f"# [Lines {s_ln}-{shown_e}: active edit region (trimmed from {e_ln})]\n{chunk_body}"
                    )
                else:
                    wf_blocks.append(f"# [Lines {s_ln}-{shown_e}: active edit region]\n{chunk_body}")
                wf_labels.append(f"{s_ln}-{shown_e}")
                prev_end = shown_e
            if prev_end < total_lines:
                wf_blocks.append(f"... [lines {prev_end + 1}-{total_lines} omitted] ...")
            wf_assembled = "\n".join(wf_blocks)
            if len(wf_assembled) <= cap:
                assembled = wf_assembled
                range_labels = wf_labels
        if not assembled:
            # Fallback if cap is too tiny even for water-filling: assemble ctx=0 once for middle clip
            header_end = min(total_lines, 8)
            fb_blocks: list[str] = []
            fb_labels: list[str] = []
            if last_merged[0][0] > header_end + 6:
                fb_blocks.append(f"# [Lines 1-{header_end}: file header]\n" + "\n".join(lines[:header_end]))
                fb_labels.append(f"1-{header_end}")
            prev_end = header_end if fb_labels else 0
            for s_ln, e_ln in last_merged:
                if prev_end and s_ln > prev_end + 1:
                    fb_blocks.append(_gap_line(prev_end + 1, s_ln - 1, False))
                fb_blocks.append(f"# [Lines {s_ln}-{e_ln}: active edit region]\n" + "\n".join(lines[s_ln - 1 : e_ln]))
                fb_labels.append(f"{s_ln}-{e_ln}")
                prev_end = e_ln
            if prev_end < total_lines:
                fb_blocks.append(f"... [lines {prev_end + 1}-{total_lines} omitted] ...")
            assembled = "\n".join(fb_blocks)
            range_labels = fb_labels

    return _clip_text_middle(assembled, cap), f", hunk lines {'/'.join(range_labels)}"


def _norm_prompt_for_dedup(txt: str) -> str:
    return " ".join(txt.split())


def _split_checkpoint_user_requests(raw_block: str) -> list[tuple[int, str]]:
    """Split `# User Requests` into `(req_no, item_text)` pairs without breaking on embedded Markdown numbered lists."""
    items: list[tuple[int, str]] = []
    cur_top = 0
    sub_num = 0
    cur_lines: list[str] = []
    num_re = _re().compile(r"^(\d+)\.(?:[ \t]+(.*))?$")
    trunc_re = _rx("CHECKPOINT_TRUNCATED_TAIL")

    for line in raw_block.splitlines():
        m = num_re.match(line)
        if m:
            n = int(m.group(1))
            rest = m.group(2) or ""
            if cur_top == 0 and n == 1:
                cur_top = 1
                sub_num = 0
                cur_lines = [rest]
                continue
            if cur_top >= 1:
                prev_nonempty = next((ln.strip() for ln in reversed(cur_lines) if ln.strip()), "")
                if trunc_re.search(prev_nonempty):
                    sub_num = 0
                if n == cur_top + 1 and (sub_num == 0 or n != sub_num + 1 or is_compact_slash_cmd(rest)):
                    items.append((cur_top, "\n".join(cur_lines).strip()))
                    cur_top = n
                    sub_num = 0
                    cur_lines = [rest]
                    continue
                if n == 1:
                    sub_num = 1
                elif sub_num > 0 and n == sub_num + 1:
                    sub_num = n
        elif cur_top >= 1:
            s_ln = line.strip()
            if s_ln.startswith("#") or s_ln in ("---", "***", "___"):
                sub_num = 0
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


_MILESTONE_STOPWORDS = frozenset(
    {
        "the", "and", "for", "with", "from", "that", "this", "into", "onto", "over", "under",
        "when", "only", "also", "fixed", "fix", "updated", "update", "added", "add", "removed",
        "remove", "cleaned", "clean", "verified", "verify", "pass", "fail", "passed", "failed",
        "tests", "test", "file", "files", "code", "line", "lines", "step", "steps", "summary",
        "done", "all", "new", "old", "user", "not", "are", "was", "were", "has", "have", "had",
        "can", "may", "will", "must", "should", "never", "always", "keep", "kept", "using",
        "use", "used", "after", "before", "during", "based", "check", "checks", "checked",
        "run", "running", "current", "existing", "other", "more", "most", "both", "each",
    }
)

_WORKSPACE_LIST_IGNORE = frozenset(
    {".git", ".hg", ".svn", "node_modules", "__pycache__", ".venv", "venv", ".mypy_cache", ".pytest_cache"}
)


def is_likely_raw_log_paste(txt: str) -> bool:
    """Return True if a long pre-window user paste looks like an unstructured raw log/UI dump rather than a structured handoff."""
    if len(txt) < 1600:
        return False
    if _strip_leading_ui_tool_badges(txt) != txt:
        return True
    if len(txt) >= 2200 and not _rx("STRUCTURED_MD_HEADING").search(txt):
        return True
    return False


_GENERIC_MILESTONE_BASENAMES = frozenset(
    {"readme.md", "skill.md", "__init__.py", "index.js", "index.ts", "index.mjs", "main.py", "config.json"}
)

_SKIP_MILESTONE_CONTAINER_PREFIXES = (
    "in progress",
    "pending",
    "remaining",
    "todo",
    "blocked",
    "verification result",
    "scratch file",
    "minor cli",
    "minor diagnostic",
)


def _shorten_milestone_paths(text: str) -> str:
    """Shorten long absolute file paths inside backticks to compact basenames so milestone budgets retain details."""
    if "`" not in text:
        return text
    out: list[str] = []
    pos = 0
    n = len(text)
    while pos < n:
        q1 = text.find("`", pos)
        if q1 < 0:
            out.append(text[pos:])
            break
        q2 = text.find("`", q1 + 1)
        if q2 < 0:
            out.append(text[pos:])
            break
        out.append(text[pos : q1 + 1])
        inner = text[q1 + 1 : q2]
        if (
            len(inner) > 28
            and " " not in inner
            and ("\\" in inner or "/" in inner)
            and not inner.startswith(("http://", "https://"))
        ):
            norm = inner.replace("\\", "/").rstrip("/")
            parts = [p for p in norm.split("/") if p]
            if len(parts) >= 2:
                base = parts[-1]
                short = f"{parts[-2]}/{base}" if base.lower() in _GENERIC_MILESTONE_BASENAMES else base
                out.append(short)
            else:
                out.append(inner)
        else:
            out.append(inner)
        out.append("`")
        pos = q2 + 1
    return "".join(out)


_MILESTONE_DANGLING_TAILS = (
    " and",
    " or",
    " with",
    " from",
    " for",
    " to",
    " of",
    " in",
    " on",
    " at",
    " via",
    " by",
    " &",
    " /",
    " +",
    " ->",
    " =>",
    " |",
)

_MILESTONE_TAIL_PUNCT = " ,;:-(&/[—–"


def _find_unclosed_code_span(s: str) -> tuple[int, int]:
    """Scan inline backtick code spans (single or multi-backtick) and return `(open_pos, fence_len)` if unclosed."""
    if "`" not in s:
        return -1, 0
    n = len(s)
    i = 0
    open_pos = -1
    open_len = 0
    while i < n:
        ch = s[i]
        if ch != "`":
            i += 1
            continue
        j = i + 1
        while j < n and s[j] == "`":
            j += 1
        run = j - i
        if open_pos < 0:
            open_pos = i
            open_len = run
        elif run == open_len:
            # Tolerate a literal backtick wrapped in quotes inside a single-backtick span (e.g. `` `close_tok = "`"` ``)
            if (
                open_len == 1
                and i > open_pos + 1
                and j < n
                and s[i - 1] in ('"', "'")
                and s[j] == s[i - 1]
                and s.find("`", j + 1) >= 0
            ):
                i = j
                continue
            open_pos = -1
            open_len = 0
        i = j
    return open_pos, open_len


def _clip_milestone_text(text: str, max_chars: int, *, add_ellipsis: bool = True) -> str:
    """Clip a milestone item to `max_chars` on a clean boundary with balanced inline backticks."""
    if len(text) <= max_chars:
        return text
    suffix = "..." if add_ellipsis else ""
    limit = max(1, max_chars - len(suffix))
    raw_cut = text[:limit]

    # If cutting inside a backtick code span, close long single-backtick identifiers cleanly or roll back before span
    unclosed_pos, unclosed_len = _find_unclosed_code_span(raw_cut)
    if unclosed_pos >= 0:
        inside = raw_cut[unclosed_pos + unclosed_len :]
        if add_ellipsis and unclosed_len == 1 and len(inside) >= 18:
            close_tok = "...`"
            inner_end = max(unclosed_pos + 1, max_chars - len(close_tok))
            inner = text[unclosed_pos + 1 : inner_end]
            sep_pos = max(inner.rfind(sep) for sep in ("_", "/", ".", "-", " "))
            if sep_pos >= int(len(inner) * 0.65):
                inner = inner[:sep_pos]
            inner = inner.rstrip(" ,;:-_/.(")
            if len(inner) >= 12:
                return text[: unclosed_pos + 1] + inner + close_tok
        cut = (
            raw_cut[:unclosed_pos].rstrip(_MILESTONE_TAIL_PUNCT)
            if unclosed_pos > 0
            else raw_cut.rstrip(_MILESTONE_TAIL_PUNCT)
        )
    else:
        cut = raw_cut
        if limit < len(text) and not text[limit].isspace() and text[limit] not in ",.;:)]" and " " in cut:
            sp = cut.rfind(" ")
            min_sp = 12 if not add_ellipsis else int(limit * 0.65)
            last_bt = cut.rfind("`")
            while sp >= min_sp and sp <= last_bt and _find_unclosed_code_span(cut[:sp])[0] >= 0:
                sp = cut.rfind(" ", 0, sp)
            if sp >= min_sp:
                cut = cut[:sp]
        cut = cut.rstrip()

    rem_open, _ = _find_unclosed_code_span(cut)
    if rem_open > 0:
        cut = cut[:rem_open].rstrip(_MILESTONE_TAIL_PUNCT)
    while True:
        prev_cut = cut
        cut = cut.rstrip(_MILESTONE_TAIL_PUNCT)
        low_cut = cut.lower()
        for trail in _MILESTONE_DANGLING_TAILS:
            if low_cut.endswith(trail) and len(cut) > len(trail) + 10:
                cut = cut[: -len(trail)].rstrip(_MILESTONE_TAIL_PUNCT)
                break
        if cut == prev_cut:
            break
    cleaned_cut = cut
    return (cleaned_cut + suffix) if cleaned_cut else text[:limit].rstrip() + suffix


def _clean_milestone_bullet(raw: str, max_chars: int = 135) -> str:
    s = " ".join(raw.strip().split())
    if s.startswith("**"):
        close = s.find("**", 2)
        if close > 2:
            head = s[2:close].strip().rstrip(":.")
            rest = s[close + 2 :].strip().lstrip(":").strip()
            s = f"{head}: {rest}" if rest else head
    s = _shorten_milestone_paths(s).rstrip(":.").strip()
    return _clip_milestone_text(s, max_chars)


def _is_line_ref_token(tok: str) -> bool:
    """Return True for transient line-number references such as `l1533` or `l1533-l1567`."""
    if len(tok) < 2 or tok[0] != "l":
        return False
    body = tok[1:]
    if "-" in body:
        a, _, b = body.partition("-")
        if b.startswith("l"):
            b = b[1:]
        return a.isdigit() and b.isdigit()
    return body.isdigit()


def _milestone_signature_tokens(text: str) -> tuple[set[str], set[str]]:
    """Return `(all_tokens, code_tokens)` for deduplicating milestones against newer checkpoints."""
    all_toks: set[str] = set()
    code_toks: set[str] = set()
    stopwords = _MILESTONE_STOPWORDS
    for m in _rx("MILESTONE_TOKEN").finditer(text):
        g_code, g_ascii, g_cjk = m.group(1), m.group(2), m.group(3)
        if g_code:
            raw_c = g_code.strip()
            # Normalize `var = val` assignments and `func(arg)` call signatures to the core symbol
            if "=" in raw_c:
                lhs = raw_c.split("=", 1)[0].strip()
                if lhs and " " not in lhs:
                    raw_c = lhs
            p_idx = raw_c.find("(")
            if p_idx >= 3:
                raw_c = raw_c[:p_idx].strip()
            if "\\" in raw_c or "/" in raw_c:
                raw_c = _fast_basename(raw_c)
            tok = raw_c.lower().rstrip(":.,;)")
            if (
                len(tok) >= 3
                and tok not in stopwords
                and not _is_line_ref_token(tok)
                and any(ch.isalpha() for ch in tok)
            ):
                all_toks.add(tok)
                code_toks.add(tok)
        elif g_ascii:
            raw_a = _fast_basename(g_ascii) if ("/" in g_ascii or "\\" in g_ascii) else g_ascii
            tok = raw_a.lower()
            if len(tok) >= 4 and tok not in stopwords and not _is_line_ref_token(tok):
                all_toks.add(tok)
        elif g_cjk:
            all_toks.add(g_cjk)
    return all_toks, code_toks


def _compact_parent_milestone_label(cleaned: str) -> str:
    """Compress a container bullet header (e.g. `Modified `rehydrate_helpers.py``) for prefixing child milestones."""
    if "`" in cleaned:
        q1 = cleaned.find("`")
        q2 = cleaned.find("`", q1 + 1)
        if q1 >= 0 and q2 > q1 + 1:
            low = cleaned[:q1].strip().lower()
            if low in ("", "modified", "updated", "added", "created", "fixed", "in"):
                return cleaned[q1 : q2 + 1]
    label = cleaned.rstrip(".").strip()
    if len(label) > 54 and label.endswith(")"):
        p_open = label.rfind(" (")
        if p_open >= 10 and "`" not in label[p_open:] and _find_unclosed_code_span(label[:p_open])[0] < 0:
            label = label[:p_open].strip()
    return _clip_milestone_text(label, 54, add_ellipsis=False)


def extract_checkpoint_milestones(cp_content: str) -> tuple[list[str], list[str]]:
    """Extract concise completed milestones, key fixes/findings, and `Commitments/Constraints` from a `CHECKPOINT` summary."""
    if not cp_content:
        return [], []
    m_sum = _rx("SUMMARY_BLOCK").search(cp_content)
    body = m_sum.group(1) if m_sum else cp_content
    primary_done: list[str] = []
    secondary_findings: list[str] = []
    constraint_items: list[str] = []
    sec_kind = ""
    parent_stack: list[tuple[int, str, int]] = []
    next_container_id = 1
    container_emitted: dict[int, int] = {}
    findings_last_idx_by_cid: dict[int, int] = {}
    skip_below_indent: int | None = None

    for line in body.splitlines():
        s_ln = line.strip()
        if not s_ln:
            continue
        if s_ln.startswith("#"):
            parent_stack.clear()
            skip_below_indent = None
            low = s_ln.lstrip("#").strip().lower()
            i = 0
            while i < len(low) and low[i].isdigit():
                i += 1
            if i > 0 and i < len(low) and low[i] in (".", ")", ":"):
                low = low[i + 1 :].strip()
            if any(
                k in low
                for k in (
                    "in progress",
                    "pending",
                    "remaining",
                    "todo",
                    "next step",
                    "not started",
                    "blocked",
                    "active context",
                    "task overview",
                )
            ):
                sec_kind = ""
            elif any(k in low for k in ("done", "completed", "resolved design decision", "key decision", "progress")):
                sec_kind = "done"
            elif any(
                k in low
                for k in ("errors & fixes", "errors and fixes", "bugs & fixes", "bugs and fixes", "key findings")
            ):
                sec_kind = "findings"
            elif "commitment" in low or "constraint" in low:
                sec_kind = "constraints"
            else:
                sec_kind = ""
            continue
        if not sec_kind:
            continue

        expanded = line.replace("\t", "  ")
        indent = len(expanded) - len(expanded.lstrip(" "))
        if not (s_ln.startswith("- ") or s_ln.startswith("* ")):
            continue

        if skip_below_indent is not None:
            if indent > skip_below_indent:
                continue
            skip_below_indent = None

        while parent_stack and parent_stack[-1][0] >= indent:
            parent_stack.pop()

        b_raw = s_ln[2:].strip()
        if not b_raw:
            continue

        if sec_kind == "constraints":
            if indent == 0:
                cleaned_c = _clean_milestone_bullet(b_raw, max_chars=120)
                if len(cleaned_c) >= 12 and len(constraint_items) < 4:
                    constraint_items.append(cleaned_c)
            continue

        cleaned_raw = _clean_milestone_bullet(b_raw, max_chars=140)
        low_clean = cleaned_raw.lower()
        if any(low_clean.startswith(pref) for pref in _SKIP_MILESTONE_CONTAINER_PREFIXES):
            skip_below_indent = indent
            continue

        is_container = b_raw.endswith(":") and (b_raw.endswith(("**:", "`:")) or len(b_raw) <= 95)
        if is_container:
            if len(cleaned_raw) >= 4:
                parent_stack.append((indent, _compact_parent_milestone_label(cleaned_raw), next_container_id))
                next_container_id += 1
            continue

        # Leaf bullet: combine with innermost parent container label when indented
        if indent > 0 and not parent_stack:
            continue
        if len(cleaned_raw) < 12:
            continue

        parent_label = parent_stack[-1][1] if parent_stack else ""
        parent_cid = parent_stack[-1][2] if parent_stack else 0
        per_container_max = 3 if sec_kind == "done" else 2

        if parent_label:
            p_check = parent_label.strip("`").lower()
            child_self_anchored = (
                sec_kind == "done"
                and not parent_label.startswith("`")
                and cleaned_raw.startswith("`")
                and ": " in cleaned_raw[:64]
                and len(parent_label) + 2 + len(cleaned_raw) > 140
            )
            if (p_check and p_check in cleaned_raw.lower()) or child_self_anchored:
                item = _clip_milestone_text(cleaned_raw, 140)
            elif sec_kind == "findings" and cleaned_raw.lower().startswith(("fix:", "resolved:", "root cause:")):
                item = _clip_milestone_text(f"{parent_label} -> {cleaned_raw}", 145)
            else:
                item = _clip_milestone_text(f"{parent_label}: {cleaned_raw}", 140)
        else:
            item = _clip_milestone_text(cleaned_raw, 135)

        if sec_kind == "done":
            if parent_cid:
                if container_emitted.get(parent_cid, 0) >= per_container_max:
                    continue
                container_emitted[parent_cid] = container_emitted.get(parent_cid, 0) + 1
            if len(primary_done) < 8:
                primary_done.append(item)
        elif sec_kind == "findings":
            # In Errors & Fixes, upgrade a prior symptom leaf under the same Error container with its `Fix:` leaf
            if (
                parent_cid
                and cleaned_raw.lower().startswith(("fix:", "resolved:"))
                and parent_cid in findings_last_idx_by_cid
            ):
                secondary_findings[findings_last_idx_by_cid[parent_cid]] = item
                continue
            if parent_cid:
                if container_emitted.get(parent_cid, 0) >= per_container_max:
                    continue
                container_emitted[parent_cid] = container_emitted.get(parent_cid, 0) + 1
            if len(secondary_findings) < 4:
                if parent_cid and not cleaned_raw.lower().startswith(("fix:", "resolved:")):
                    findings_last_idx_by_cid[parent_cid] = len(secondary_findings)
                secondary_findings.append(item)

    done_items = (primary_done[:8] + secondary_findings[:4])[:10]
    return done_items, constraint_items


def build_anti_decay_milestone_digest(
    prev_checkpoints: list[tuple[int, int, str]],
    latest_cp_content: str,
    transcript_path: Path | None,
    budget_chars: int = 1200,
) -> tuple[str, int]:
    """Summarize earlier checkpoint milestones no longer covered by the latest `<CONTEXT_SUMMARY>`, with line pointers."""
    if not prev_checkpoints or budget_chars < 220:
        return "", 0

    latest_low = latest_cp_content.lower()
    latest_toks, _ = _milestone_signature_tokens(latest_cp_content)

    # Scan newest earlier checkpoint -> oldest so the richest surviving wording wins on dedup
    kept_token_sets: list[set[str]] = []
    by_cp: dict[tuple[int, int], list[str]] = {}
    for cp_step, cp_line, cp_txt in reversed(prev_checkpoints):
        done_items, constraint_items = extract_checkpoint_milestones(cp_txt)
        candidates = [(item, False) for item in done_items] + [(item, True) for item in constraint_items]
        for item, is_constraint in candidates:
            all_toks, code_toks = _milestone_signature_tokens(item)
            if not all_toks:
                continue
            if item.lower() in latest_low:
                continue
            direct_hits = all_toks & latest_toks
            overlap_latest = len(direct_hits)
            if overlap_latest < len(all_toks):
                overlap_latest += sum(1 for t in (all_toks - direct_hits) if t in latest_low)
            code_missing = [t for t in code_toks if t not in latest_low]
            # Still well-covered in latest summary -> skip to avoid duplication
            if code_toks and not code_missing:
                if len(code_toks) >= 2 or overlap_latest / len(all_toks) >= 0.45:
                    continue
            if overlap_latest / len(all_toks) >= 0.60 and not (code_toks and len(code_missing) == len(code_toks)):
                continue
            # Deduplicate against a newer earlier-checkpoint milestone already kept
            if any(
                len(all_toks & prev_ts) / max(1, min(len(all_toks), len(prev_ts))) >= 0.60
                for prev_ts in kept_token_sets
            ):
                continue
            kept_token_sets.append(all_toks)
            label = f"[Constraint] {item}" if is_constraint else item
            by_cp.setdefault((cp_step, cp_line), []).append(label)

    if not by_cp:
        return "", 0

    path_hint = f"`{transcript_path}`" if transcript_path else "`transcript_full.jsonl`"
    note = (
        f"*(Earlier milestones from prior checkpoints no longer detailed in `<CONTEXT_SUMMARY>`; "
        f"`read_file` {path_hint} at the cited line for full archived summary)*"
    )
    avail = budget_chars - len(note) - 2
    if avail < 100:
        return "", 0

    # Prefer keeping the most recent dropped checkpoints first, then display chronologically.
    # Cap per-checkpoint items when multiple checkpoints have decayed items so older checkpoints aren't starved.
    selected_rows: list[tuple[int, int, str, int]] = []
    used = 0
    per_cp_limit = 4 if len(by_cp) >= 2 else 8
    for cp_step, cp_line in sorted(by_cp.keys(), reverse=True):
        items = by_cp[(cp_step, cp_line)][:per_cp_limit]
        prefix = f"- [cp @ step {cp_step}, line {cp_line}]: " if cp_line > 0 else f"- [cp @ step {cp_step}]: "
        row_items: list[str] = []
        row_len = len(prefix)
        for it in items:
            sep_len = 2 if row_items else 0
            if used + row_len + sep_len + len(it) + (1 if selected_rows else 0) > avail:
                break
            row_items.append(it)
            row_len += sep_len + len(it)
        if row_items:
            row_str = prefix + "; ".join(row_items)
            selected_rows.append((cp_step, cp_line, row_str, len(row_items)))
            used += len(row_str) + 1

    if not selected_rows:
        return "", 0
    selected_rows.sort(key=lambda x: x[0])
    total_count = sum(cnt for _, _, _, cnt in selected_rows)
    return note + "\n" + "\n".join(row for _, _, row, _ in selected_rows), total_count


def _format_rel_or_abs(p: Path, workspace_roots: list[Path]) -> str:
    for root in workspace_roots:
        if _is_subpath(p, root):
            try:
                rel = p.relative_to(root)
                rel_s = str(rel)
                if rel_s and rel_s != ".":
                    return f"{root.name}/{rel_s}" if len(workspace_roots) > 1 else rel_s
            except ValueError:
                pass
    return str(p)


def build_workspace_reality_notice(
    modified_touch: dict[str, tuple[str, int, list[str], int]],
    read_touch: dict[str, tuple[str, int, tuple[int | None, int | None]]],
    path_touch_epoch: dict[str, tuple[int, float]],
    target_cp_content: str,
    workspace_roots: list[Path] | None,
    injected_file_keys: set[str],
    excluded_roots: list[Path] | None = None,
    max_chars: int = 1200,
    resolve_cache: dict[str, Path | None] | None = None,
) -> tuple[str, dict[str, int]]:
    """Detect missing/stale workspace paths, external disk edits, and uninjected modified files with zero leakage."""
    empty_counts = {
        "stale_paths": 0,
        "externally_modified_files": 0,
        "uninjected_modified_files": 0,
    }
    if not workspace_roots or max_chars < 180:
        return "", empty_counts

    rcache = resolve_cache if resolve_cache is not None else {}
    roots = list(workspace_roots)
    root_prefixes = _prepare_root_prefixes(roots, rcache)
    excl_prefixes = _prepare_root_prefixes(excluded_roots, rcache)
    root_entries: list[tuple[Path, str]] = []
    for r in roots:
        rr = _resolve_path_cached(str(r), rcache)
        root_entries.append((r, _norm_path_for_prefix(rr if rr is not None else r)))
    root_keys = {rk for _, rk in root_entries}

    stat_cache: dict[str, os.stat_result | None] = {}

    def _cached_stat(p: Path) -> os.stat_result | None:
        sk = _norm_path_for_prefix(p)
        hit = stat_cache.get(sk, _CACHE_MISS)
        if hit is not _CACHE_MISS:
            return hit  # type: ignore[return-value]
        try:
            st = os.stat(p)
        except OSError:
            st = None
        stat_cache[sk] = st
        return st

    # 1. Missing workspace paths (from successful tool touches + explicit paths in <CONTEXT_SUMMARY>)
    missing_raw: dict[str, Path] = {}
    tracked_resolved: dict[str, Path | None] = {}
    for store in (modified_touch, read_touch):
        for key, item in store.items():
            if key in tracked_resolved:
                continue
            p_str = item[0]
            ok, resolved = is_trackable_workspace_path(
                p_str,
                workspace_roots=roots,
                excluded_roots=excluded_roots,
                resolve_cache=rcache,
                _allowed_prefixes=root_prefixes,
                _excluded_prefixes=excl_prefixes,
            )
            tracked_resolved[key] = resolved if ok else None
            if ok and resolved is not None:
                res_norm = _norm_path_for_prefix(resolved)
                if res_norm not in root_keys and _cached_stat(resolved) is None:
                    missing_raw[res_norm] = resolved

    if target_cp_content:
        seen_raw_cands: set[str] = set()
        for m in _rx("ABS_PATH_REF").finditer(target_cp_content):
            raw_cand = m.group(0).partition("#")[0].rstrip(".,:;`'\"*)")
            if not raw_cand or "*" in raw_cand or "?" in raw_cand:
                continue
            # Strip trailing :line or :line:col suffix while preserving Windows drive colon (index 1)
            while len(raw_cand) > 3:
                cpos = raw_cand.rfind(":")
                if cpos > 2 and raw_cand[cpos + 1 :].isdigit():
                    raw_cand = raw_cand[:cpos]
                else:
                    break
            # Ignore truncated prefix right before brace-expansion `{a,b}`
            end_pos = m.end()
            if end_pos < len(target_cp_content) and target_cp_content[end_pos] == "{":
                continue
            cand_key = raw_cand.lower() if os.name == "nt" else raw_cand
            if cand_key in seen_raw_cands:
                continue
            seen_raw_cands.add(cand_key)
            ok, resolved = is_trackable_workspace_path(
                raw_cand,
                workspace_roots=roots,
                excluded_roots=excluded_roots,
                resolve_cache=rcache,
                _allowed_prefixes=root_prefixes,
                _excluded_prefixes=excl_prefixes,
            )
            if ok and resolved is not None:
                res_norm = _norm_path_for_prefix(resolved)
                if res_norm not in root_keys and _cached_stat(resolved) is None:
                    missing_raw[res_norm] = resolved

    # Collapse missing children into their highest missing ancestor under a workspace root
    collapsed_missing: dict[str, tuple[Path, int, Path]] = {}
    affected_roots: list[Path] = []
    seen_affected_roots: set[str] = set()
    for mp_norm, mp in missing_raw.items():
        containing = next(((r, r_norm) for r, r_norm in root_entries if _is_subpath_norm(mp_norm, r_norm)), None)
        if containing is None:
            continue
        containing_root, root_norm = containing
        cur = mp
        while cur.parent != cur:
            par = cur.parent
            par_norm = _norm_path_for_prefix(par)
            if par_norm == root_norm or not _is_subpath_norm(par_norm, root_norm):
                break
            if _cached_stat(par) is not None:
                break
            cur = par
        ck = _norm_path_for_prefix(cur)
        prev = collapsed_missing.get(ck)
        if prev is None:
            collapsed_missing[ck] = (cur, 1, containing_root)
        else:
            collapsed_missing[ck] = (prev[0], prev[1] + 1, prev[2])
        if root_norm not in seen_affected_roots:
            seen_affected_roots.add(root_norm)
            affected_roots.append(containing_root)

    # 2. Externally modified existing files (not already live-read into Section 4)
    ext_modified: list[tuple[int, Path]] = []
    seen_ext_keys: set[str] = set()
    for store in (modified_touch, read_touch):
        for key in store:
            if key in injected_file_keys or key in seen_ext_keys or key not in path_touch_epoch:
                continue
            resolved = tracked_resolved.get(key)
            if resolved is None:
                continue
            st = _cached_stat(resolved)
            if st is None or (st.st_mode & 0o170000) != 0o100000:
                continue
            t_step, t_epoch = path_touch_epoch[key]
            if st.st_mtime > t_epoch + 5.0:
                seen_ext_keys.add(key)
                ext_modified.append((t_step, resolved))
    ext_modified.sort(key=lambda x: x[0], reverse=True)

    # 3. Existing modified files omitted from Section 4 (only when ZERO modified files were injected)
    any_mod_injected = any(k in injected_file_keys for k in modified_touch)
    uninjected_mods: list[tuple[int, Path, bool]] = []
    if not any_mod_injected and modified_touch:
        for key, (_, st_idx, _, _) in sorted(modified_touch.items(), key=lambda x: x[1][1], reverse=True):
            resolved = tracked_resolved.get(key)
            if resolved is None:
                continue
            st = _cached_stat(resolved)
            if st is None or (st.st_mode & 0o170000) != 0o100000:
                continue
            uninjected_mods.append((st_idx, resolved, key in seen_ext_keys))

    if not collapsed_missing and not ext_modified and not uninjected_mods:
        return "", empty_counts

    sorted_missing = (
        sorted(collapsed_missing.values(), key=lambda x: (-x[1], str(x[0]).lower())) if collapsed_missing else []
    )
    live_entries_by_root: list[tuple[Path, list[str]]] = []
    if collapsed_missing:
        for ws_root in affected_roots[:2]:
            try:
                entries: list[str] = []
                with os.scandir(ws_root) as it:
                    for ent in it:
                        name = ent.name
                        if name in _WORKSPACE_LIST_IGNORE or name.startswith(".tmp_"):
                            continue
                        try:
                            is_d = ent.is_dir(follow_symlinks=False)
                        except OSError:
                            is_d = False
                        entries.append(f"`{name}/`" if is_d else f"`{name}`")
                entries.sort(key=str.lower)
                if entries:
                    live_entries_by_root.append((ws_root, entries))
            except OSError:
                pass

    def _render_notice_lines(
        max_missing: int,
        max_roots: int,
        max_entries: int,
        max_ext: int,
        max_uninj: int,
        compact: bool,
    ) -> list[str]:
        out: list[str] = []
        if sorted_missing:
            shown_m: list[str] = []
            for mp, cnt, _ in sorted_missing[:max_missing]:
                if cnt > 1:
                    shown_m.append(f"`{mp}` ({cnt} refs missing)")
                else:
                    shown_m.append(f"`{mp}`")
            extra_m = f", +{len(sorted_missing) - max_missing} more" if len(sorted_missing) > max_missing else ""
            if compact:
                out.append(f"- **Stale / missing paths** (NOT on disk): {', '.join(shown_m)}{extra_m}")
            else:
                out.append(
                    f"- **Stale / missing paths** (referenced in `<CONTEXT_SUMMARY>` or earlier steps, "
                    f"NOT currently on disk): {', '.join(shown_m)}{extra_m}"
                )
            for ws_root, entries in live_entries_by_root[:max_roots]:
                shown_e = ", ".join(entries[:max_entries])
                more_e = f", +{len(entries) - max_entries} more" if len(entries) > max_entries else ""
                out.append(f"- **Current live entries in `{ws_root}`**: {shown_e}{more_e}")

        if ext_modified:
            shown_ext = [
                f"`{_format_rel_or_abs(rp, roots)}` (changed after step {st})"
                for st, rp in ext_modified[:max_ext]
            ]
            more_ext = f", +{len(ext_modified) - max_ext} more" if len(ext_modified) > max_ext else ""
            out.append(f"- **Modified on disk since last read/edit**: {', '.join(shown_ext)}{more_ext}")

        if uninjected_mods:
            shown_um = [
                f"`{_format_rel_or_abs(rp, roots)}` (@ step {st}{', modified externally' if is_ext else ''})"
                for st, rp, is_ext in uninjected_mods[:max_uninj]
            ]
            more_um = f", +{len(uninjected_mods) - max_uninj} more" if len(uninjected_mods) > max_uninj else ""
            if compact:
                out.append(
                    "- **Modified files not injected in this payload** "
                    f"(run `read_file` before editing): {', '.join(shown_um)}{more_um}"
                )
            else:
                out.append(
                    "- **Modified files not injected in this payload** "
                    "(run `read_file` before editing or citing line numbers — do NOT rely on stale line numbers "
                    f"in `<CONTEXT_SUMMARY>`): {', '.join(shown_um)}{more_um}"
                )
        return out

    def _fit_bullet_line(line: str, limit: int) -> str:
        if len(line) <= limit:
            return line
        pfx, sep, body = line.partition(": ")
        if not sep or len(pfx) + 8 >= limit:
            return line[: max(1, limit - 3)].rstrip() + "..."
        avail = limit - len(pfx) - 2
        if avail <= 6:
            return f"{pfx}: ..."
        parts = body.split(", ")
        kept_parts: list[str] = []
        for idx_p, part in enumerate(parts):
            rem_count = len(parts) - idx_p - 1
            suffix = f", +{rem_count} more" if rem_count > 0 else ""
            cand = ", ".join(kept_parts + [part]) + suffix
            if len(cand) <= avail:
                kept_parts.append(part)
            else:
                break
        if kept_parts:
            rem_count = len(parts) - len(kept_parts)
            suffix = f", +{rem_count} more" if rem_count > 0 else ""
            return f"{pfx}: {', '.join(kept_parts)}{suffix}"
        # Single item exceeds avail: clip cleanly inside backticks if present
        first = parts[0]
        if first.startswith("`"):
            close_bt = first.find("`", 1)
            if close_bt > 1:
                inner = first[1:close_bt]
                tail_note = first[close_bt + 1 :]
                keep_inner = avail - len(tail_note) - 5
                if keep_inner >= 8:
                    return f"{pfx}: `...{inner[-keep_inner:]}`{tail_note}"
        clipped = first[: max(1, avail - 3)].rstrip()
        if clipped.count("`") % 2 == 1:
            clipped = clipped[: max(1, len(clipped) - 1)] + "`"
        return f"{pfx}: {clipped}..."

    # Structured item-level shrinking preserves complete Markdown bullets (never mid-clips prose with `read_file`)
    profiles = (
        (6, 2, 15, 5, 5, False),
        (4, 2, 10, 4, 4, False),
        (3, 1, 8, 3, 3, True),
        (2, 1, 6, 2, 2, True),
        (1, 1, 4, 1, 1, True),
    )
    block = ""
    tight_lines: list[str] = []
    for prof in profiles:
        cand_lines = _render_notice_lines(*prof)
        tight_lines = cand_lines
        cand_block = "\n".join(cand_lines)
        if len(cand_block) <= max_chars:
            block = cand_block
            break

    if not block and tight_lines:
        kept_lines: list[str] = []
        used_c = 0
        for ln in tight_lines:
            sep_len = 1 if kept_lines else 0
            if used_c + sep_len + len(ln) <= max_chars:
                kept_lines.append(ln)
                used_c += sep_len + len(ln)
            elif not kept_lines:
                kept_lines.append(_fit_bullet_line(ln, max_chars))
                used_c = len(kept_lines[0])
                break
        block = "\n".join(kept_lines)

    return block, {
        "stale_paths": len(collapsed_missing),
        "externally_modified_files": len(ext_modified),
        "uninjected_modified_files": len(uninjected_mods),
    }
