#!/usr/bin/env python3
"""Core 4-layer Post-Compact Rehydration engine and `/fork` lineage stitching for `agy-rehydrate`.

Clean, deterministic architecture faithful to the AGY Post-Compact Rehydration design:
- `<CONTEXT_SUMMARY>` is lossy; local `transcript_full.jsonl` + workspace files are lossless ground truth.
- 4 predictable layers with fixed configurable budgets (no fragile adaptive/NLP summary heuristics):
  1. Complete User Request Timeline (`<= 4`-char pure-CJK filter beyond recent 75, `> 800`-char paste folding)
  2. Recent Pre-Compaction Verbatim Dialogue (with single-line tool trajectories)
  3. Restored Active Skills, Spec Docs & Repo Rule Pointers (`AGENTS.md`/`GEMINI.md` paths only)
  4. Restored Active Workspace Files (live disk read: Top modified + Top referenced)
- Cross-platform (Windows + macOS + Linux) across Antigravity Desktop App 2.x, CLI, and IDE.
- Deterministic path security (fail-closed workspace scoping, symlink resolution, secret/binary filtering).
- 48 KiB UTF-8 safe `injectSteps` chunking to avoid AGY's per-SystemMessageStep truncation cap.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime
import json
import os
from pathlib import Path
import re
import stat
import sys
from typing import Any
import unicodedata
from urllib.parse import unquote, urlparse

sys.dont_write_bytecode = True

AGY_PRODUCT_DIRS = ("antigravity", "antigravity-cli", "antigravity-ide")
MARKER_PREFIX = "[POST-COMPACT REHYDRATION v1"

DEFAULT_BUDGET_ALL_USER_CHARS = 24_000
DEFAULT_BUDGET_RECENT_TURNS_CHARS = 28_000
DEFAULT_BUDGET_SKILLS_SPECS_CHARS = 14_000
DEFAULT_BUDGET_FILES_TOTAL_CHARS = 28_000
DEFAULT_BUDGET_SINGLE_FILE_CHARS = 8_000
DEFAULT_MAX_SINGLE_TURN_CHARS = 12_000

MAX_CONFIG_BUDGET_CHARS = 200_000
MAX_CONFIG_FILE_SLOTS = 20

RECENT_USER_VERBATIM_COUNT = 75
SHORT_USER_MAX_CHARS = 4
FOLD_PASTE_THRESHOLD_CHARS = 800
FOLD_PASTE_HEAD_CHARS = 300
FOLD_PASTE_TAIL_CHARS = 200

DEFAULT_MAX_MODIFIED_FILES = 5
DEFAULT_MAX_REFERENCED_FILES = 2
DEFAULT_FILE_SCOPE_SINCE_PREV_CHECKPOINT = True
MAX_REPO_RULE_FILES = 6
MAX_FORK_DEPTH = 8
FRESH_CHECKPOINT_MAX_GAP = 8
MAX_INJECT_STEP_BYTES = 44_000  # Safely under AGY's 49,152-byte (48 KiB) per-SystemMessageStep cap
MAX_FILE_READ_BYTES = 2 * 1024 * 1024

REPO_RULE_NAMES = ("AGENTS.md", "GEMINI.md")
SKIP_SYSTEM_PATH_PREFIXES = (
    "/usr/",
    "/bin/",
    "/sbin/",
    "/etc/",
    "/proc/",
    "/sys/",
    "/dev/",
    "/var/",
    "/tmp/",
    "/system/",
    "/library/",
    "/opt/",
    "c:/windows/",
    "c:/program files/",
    "c:/program files (x86)/",
    "c:/programdata/",
)

# AGY 2.0 App/CLI uses read_file/write_file/edit_file (`Path`);
# Legacy 1.x Desktop IDE uses view_file (`AbsolutePath`) and write_to_file/replace_file_content (`TargetFile`).
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
PATH_ARG_KEYS = ("Path", "TargetFile", "AbsolutePath", "File", "filePath", "file_path", "path", "target_file")

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
        ".envrc",
        ".env~",
        ".git-credentials",
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

TEMP_PATH_SUBSTRINGS = (
    "/dev/shm/",
    "/tmp/",
    "/appdata/local/temp/",
)

_AGY_INTERNAL_SUBDIRS = (
    "builtin/",
    "plugin_data/",
    "conversations/",
    "daemon/",
    "annotations/",
    "scratch/",
    "sidecar_data/",
    "persistent_terminals/",
    "log/",
    "history.jsonl",
)

SKIP_PATH_SUBSTRINGS = (
    "/.system_generated/",
    "/node_modules/",
    "/.git/",
    "/__pycache__/",
    "/.ssh/",
    "/.gnupg/",
    "/.aws/credentials",
    "/.kube/config",
    "/.docker/config.json",
    "/.agents/guardrails.json",
    "/.gemini/config/config.json",
    "/.gemini/config/mcp_config.json",
    "/.gemini/config/plugins/agy-rehydrate/skills/rehydrate/",
    *(f"/.gemini/{prod}/{sub}" for prod in AGY_PRODUCT_DIRS for sub in _AGY_INTERNAL_SUBDIRS),
    *(f"/{prod}/history.jsonl" for prod in AGY_PRODUCT_DIRS),
)

USER_REQ_RE = re.compile(r"<USER_REQUEST>\s*(.*?)\s*</USER_REQUEST>", re.S)
ADD_META_RE = re.compile(r"<ADDITIONAL_METADATA>.*?</ADDITIONAL_METADATA>", re.S)
ASCII_ALNUM_OR_SLASH_RE = re.compile(r"[A-Za-z0-9/\\]")
ABS_PATH_RE = re.compile(
    r"(?<![A-Za-z0-9_.-])((?:[A-Za-z]:[/\\]|/)[A-Za-z0-9_./\\+-]{3,})"
)
REHYDRATE_SYSMSG_START_RE = re.compile(
    r"\A\s*"
    r"(?:The following is a <SYSTEM_MESSAGE>[^\n]*\n[ \t]*(?:\n[ \t]*)?)?"
    r"(?:<SYSTEM_MESSAGE>\s*)?"
    r"(?:The following system message was injected by a PreInvocation hook:\s*)?"
    r"\[POST-COMPACT REHYDRATION v1(?:\s*\|\s*checkpoint=(\d+))?\b[^\]]*\]"
)
NESTED_REHYDRATE_RE = re.compile(
    r"(?:The following is a <SYSTEM_MESSAGE>[^\n]*\n[ \t]*(?:\n[ \t]*)?)?"
    r"<SYSTEM_MESSAGE>\s*(?:The following system message was injected by a PreInvocation hook:\s*)?"
    r"\[POST-COMPACT REHYDRATION [^\]]+\].*?</SYSTEM_MESSAGE>",
    re.S,
)
TIMELINE_SPLIT_RE = re.compile(r"\n(?=- \[#|- --- \[/fork )")
TURN_SPLIT_RE = re.compile(r"\n\n(?=\[Step |\[Pre-Compaction |--- \[/fork )")
FILE_SPLIT_RE = re.compile(
    r"\n\n(?=### (?:`[^`\n]+` \(touched @ step \d+\)|\[(?:Modified|Referenced) @ step \d+\] `|Touched Repo Rule Files \(Paths Only\)))"
)
FENCE_LINE_RE = re.compile(r"^( {0,3})(`{3,}|~{3,})(.*)$")
BACKTICK_RUN_RE = re.compile(r"`{3,}")
STEP_INDEX_START_RE = re.compile(r'^\s*\{\s*"step_index"\s*:\s*(\d+)\s*[,}]')


class RehydrateConfig:
    """Budget & slot configuration merged from defaults, `~/.agents/rehydrate.json`, and `<ws>/.agents/rehydrate.json`."""

    __slots__ = (
        "enabled",
        "budget_all_user_chars",
        "budget_recent_turns_chars",
        "budget_skills_specs_chars",
        "budget_files_total_chars",
        "budget_single_file_chars",
        "max_single_turn_chars",
        "max_modified_files",
        "max_referenced_files",
        "file_scope_since_prev_checkpoint",
        "extra_allowed_paths",
    )

    def __init__(
        self,
        enabled: bool = True,
        budget_all_user_chars: int = DEFAULT_BUDGET_ALL_USER_CHARS,
        budget_recent_turns_chars: int = DEFAULT_BUDGET_RECENT_TURNS_CHARS,
        budget_skills_specs_chars: int = DEFAULT_BUDGET_SKILLS_SPECS_CHARS,
        budget_files_total_chars: int = DEFAULT_BUDGET_FILES_TOTAL_CHARS,
        budget_single_file_chars: int = DEFAULT_BUDGET_SINGLE_FILE_CHARS,
        max_single_turn_chars: int = DEFAULT_MAX_SINGLE_TURN_CHARS,
        max_modified_files: int = DEFAULT_MAX_MODIFIED_FILES,
        max_referenced_files: int = DEFAULT_MAX_REFERENCED_FILES,
        file_scope_since_prev_checkpoint: bool = DEFAULT_FILE_SCOPE_SINCE_PREV_CHECKPOINT,
        extra_allowed_paths: tuple[str, ...] = (),
    ) -> None:
        self.enabled = enabled
        self.budget_all_user_chars = budget_all_user_chars
        self.budget_recent_turns_chars = budget_recent_turns_chars
        self.budget_skills_specs_chars = budget_skills_specs_chars
        self.budget_files_total_chars = budget_files_total_chars
        self.budget_single_file_chars = budget_single_file_chars
        self.max_single_turn_chars = max_single_turn_chars
        self.max_modified_files = max_modified_files
        self.max_referenced_files = max_referenced_files
        self.file_scope_since_prev_checkpoint = file_scope_since_prev_checkpoint
        self.extra_allowed_paths = extra_allowed_paths

    def to_dict(self) -> dict[str, Any]:
        return {
            "enabled": self.enabled,
            "budgetAllUserChars": self.budget_all_user_chars,
            "budgetRecentTurnsChars": self.budget_recent_turns_chars,
            "budgetSkillsSpecsChars": self.budget_skills_specs_chars,
            "budgetFilesTotalChars": self.budget_files_total_chars,
            "budgetSingleFileChars": self.budget_single_file_chars,
            "maxSingleTurnChars": self.max_single_turn_chars,
            "maxModifiedFiles": self.max_modified_files,
            "maxReferencedFiles": self.max_referenced_files,
            "fileScopeSincePrevCheckpoint": self.file_scope_since_prev_checkpoint,
            "extraAllowedPaths": list(self.extra_allowed_paths),
        }


def _bounded_int(raw: Any, default: int, minimum: int = 0, maximum: int = MAX_CONFIG_BUDGET_CHARS) -> int:
    if isinstance(raw, int) and not isinstance(raw, bool) and raw >= minimum:
        return min(raw, maximum)
    return default


def estimate_tokens(text: str) -> int:
    """Rough bilingual token estimator (CJK chars ~1.15 tok, ASCII/Latin ~0.28 tok)."""
    if not text:
        return 0
    cjk = sum(
        1
        for ch in text
        if ("\u4e00" <= ch <= "\u9fff")
        or ("\u3000" <= ch <= "\u30ff")
        or ("\uff00" <= ch <= "\uffef")
    )
    return int(round(cjk * 1.15 + (len(text) - cjk) * 0.28))


def _is_win_drive_abs(val: str) -> bool:
    return (
        len(val) >= 3
        and (("A" <= val[0] <= "Z") or ("a" <= val[0] <= "z"))
        and val[1] == ":"
        and val[2] in ("\\", "/")
    )


def normalize_local_path(raw: str) -> str:
    """Normalize POSIX, Windows drive, extended `\\\\?\\X:\\`, or `file://` URI into a local path (blocking UNC)."""
    val = raw.strip()
    if not val:
        return ""
    if val[:7].lower() == "file://":
        parsed = urlparse(val)
        if parsed.netloc and parsed.netloc.lower() != "localhost":
            return ""
        raw_path = unquote(parsed.path or "")
        if len(raw_path) >= 3 and raw_path[0] == "/" and _is_win_drive_abs(raw_path[1:]):
            val = raw_path[1:]
        else:
            val = raw_path
    if val.startswith("\\\\?\\") and _is_win_drive_abs(val[4:]):
        val = val[4:]
    if val.startswith("\\\\") or val.startswith("//"):
        return ""
    if _is_win_drive_abs(val):
        return os.path.normpath(val)
    if val.startswith("/") and os.name != "nt":
        return os.path.normpath(val)
    return ""


def canonical_path_key(path_str: str) -> str:
    return os.path.normcase(os.path.normpath(path_str))


def _norm_prefix(path_like: Path | str) -> str:
    return os.path.normcase(os.path.normpath(os.fspath(path_like)))


def _is_subpath(child_norm: str, parent_norm: str) -> bool:
    if child_norm == parent_norm:
        return True
    sep = os.sep
    prefix = parent_norm if parent_norm.endswith(sep) else parent_norm + sep
    return child_norm.startswith(prefix)


def resolve_workspace_roots_from_payload(payload: dict[str, Any]) -> list[Path]:
    """Extract deduplicated workspace roots from hook stdin `workspacePaths` and `cwd`."""
    candidates: list[str] = []
    raw_ws = payload.get("workspacePaths")
    if isinstance(raw_ws, list):
        candidates.extend(x for x in raw_ws if isinstance(x, str) and x.strip())
    cwd = payload.get("cwd")
    if isinstance(cwd, str) and cwd.strip():
        candidates.append(cwd)

    roots: list[Path] = []
    seen: set[str] = set()
    for item in candidates:
        norm = normalize_local_path(item)
        if not norm:
            continue
        try:
            resolved = Path(norm).resolve()
        except OSError:
            continue
        key = canonical_path_key(str(resolved))
        if key not in seen:
            seen.add(key)
            roots.append(resolved)
    return roots


def _parse_extra_allowed_paths(raw: Any) -> tuple[str, ...]:
    if not isinstance(raw, list):
        return ()
    out: list[str] = []
    seen: set[str] = set()
    for item in raw:
        if not isinstance(item, str) or not item.strip():
            continue
        expanded = os.path.expanduser(item.strip())
        norm = normalize_local_path(expanded)
        if not norm:
            continue
        try:
            resolved = str(Path(norm).resolve())
        except OSError:
            resolved = norm
        key = canonical_path_key(resolved)
        if key not in seen:
            seen.add(key)
            out.append(resolved)
    return tuple(out)


def _read_config_dict(cfg_path: Path) -> dict[str, Any]:
    if not cfg_path.is_file():
        return {}
    try:
        data = json.loads(cfg_path.read_text(encoding="utf-8-sig"))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def load_rehydrate_config(
    workspace: Path | str | Sequence[Path | str] | None = None,
    *,
    global_config_path: Path | None = None,
) -> RehydrateConfig:
    """Load optional global (`~/.agents/rehydrate.json`) and workspace (`.agents/rehydrate.json`) budgets.

    Security boundary: `extraAllowedPaths` and global `"enabled": false` are global-only and cannot be
    widened or re-enabled by an untrusted workspace `.agents/rehydrate.json`.
    """
    if workspace is None:
        raw_items: list[Path | str] = []
    elif isinstance(workspace, (str, os.PathLike)):
        raw_items = [workspace]
    else:
        raw_items = [w for w in workspace if w is not None]

    ws_list: list[Path] = []
    seen: set[str] = set()
    for item in raw_items:
        p = item if isinstance(item, Path) else Path(item)
        key = canonical_path_key(str(p))
        if key not in seen:
            seen.add(key)
            ws_list.append(p)

    global_cfg = _read_config_dict(global_config_path or (Path.home() / ".agents" / "rehydrate.json"))
    global_disabled = global_cfg.get("enabled") is False
    extra_allowed = _parse_extra_allowed_paths(global_cfg.get("extraAllowedPaths"))

    enabled = True
    budget_all_user = DEFAULT_BUDGET_ALL_USER_CHARS
    budget_recent_turns = DEFAULT_BUDGET_RECENT_TURNS_CHARS
    budget_skills_specs = DEFAULT_BUDGET_SKILLS_SPECS_CHARS
    budget_files_total = DEFAULT_BUDGET_FILES_TOTAL_CHARS
    budget_single_file = DEFAULT_BUDGET_SINGLE_FILE_CHARS
    max_single_turn = DEFAULT_MAX_SINGLE_TURN_CHARS
    max_modified = DEFAULT_MAX_MODIFIED_FILES
    max_referenced = DEFAULT_MAX_REFERENCED_FILES
    file_scope = DEFAULT_FILE_SCOPE_SINCE_PREV_CHECKPOINT

    def _apply_layer(layer: dict[str, Any], *, is_global: bool) -> None:
        nonlocal enabled, budget_all_user, budget_recent_turns, budget_skills_specs
        nonlocal budget_files_total, budget_single_file, max_single_turn
        nonlocal max_modified, max_referenced, file_scope
        if not layer:
            return
        if "enabled" in layer and isinstance(layer["enabled"], bool):
            if is_global or not global_disabled:
                enabled = layer["enabled"]
        if "fileScopeSincePrevCheckpoint" in layer and isinstance(layer["fileScopeSincePrevCheckpoint"], bool):
            file_scope = layer["fileScopeSincePrevCheckpoint"]
        budget_all_user = _bounded_int(layer.get("budgetAllUserChars"), budget_all_user, 1000)
        budget_recent_turns = _bounded_int(layer.get("budgetRecentTurnsChars"), budget_recent_turns, 1000)
        budget_skills_specs = _bounded_int(layer.get("budgetSkillsSpecsChars"), budget_skills_specs, 0)
        budget_files_total = _bounded_int(layer.get("budgetFilesTotalChars"), budget_files_total, 0)
        budget_single_file = _bounded_int(layer.get("budgetSingleFileChars"), budget_single_file, 1000)
        max_single_turn = _bounded_int(layer.get("maxSingleTurnChars"), max_single_turn, 1000)
        max_modified = _bounded_int(layer.get("maxModifiedFiles"), max_modified, 0, MAX_CONFIG_FILE_SLOTS)
        max_referenced = _bounded_int(layer.get("maxReferencedFiles"), max_referenced, 0, MAX_CONFIG_FILE_SLOTS)

    _apply_layer(global_cfg, is_global=True)
    for ws in reversed(ws_list):
        _apply_layer(_read_config_dict(ws / ".agents" / "rehydrate.json"), is_global=False)

    return RehydrateConfig(
        enabled=enabled,
        budget_all_user_chars=budget_all_user,
        budget_recent_turns_chars=budget_recent_turns,
        budget_skills_specs_chars=budget_skills_specs,
        budget_files_total_chars=budget_files_total,
        budget_single_file_chars=budget_single_file,
        max_single_turn_chars=max_single_turn,
        max_modified_files=max_modified,
        max_referenced_files=max_referenced,
        file_scope_since_prev_checkpoint=file_scope,
        extra_allowed_paths=extra_allowed,
    )


def _is_sensitive_name(name_lower: str) -> bool:
    if name_lower in SENSITIVE_BASENAMES:
        return True
    return name_lower == ".env" or (name_lower.startswith(".env.") and name_lower not in SAFE_ENV_TEMPLATES)


def _prepare_prefixes(roots: Sequence[Path] | None) -> tuple[str, ...]:
    if not roots:
        return ()
    out: list[str] = []
    seen: set[str] = set()
    for r in roots:
        try:
            res = r.resolve()
        except OSError:
            res = r
        pref = _norm_prefix(res)
        if pref not in seen:
            seen.add(pref)
            out.append(pref)
    return tuple(out)


def default_custom_skill_roots(artifact_dir: Path | None = None) -> list[Path]:
    """Trusted non-workspace roots for user custom `SKILL.md` and session plan artifacts."""
    home = Path.home()
    roots = [home / ".gemini" / "config" / "skills", home / ".gemini" / "config" / "plugins"]
    if artifact_dir is not None:
        roots.append(artifact_dir)
    return roots


def _is_blocked_system_or_temp_path(
    slash_low: str,
    slash_dir: str,
    allowed_prefixes: tuple[str, ...] | None,
) -> bool:
    if any(sub in slash_low or sub in slash_dir for sub in SKIP_PATH_SUBSTRINGS):
        return True
    matched_temp = [sub for sub in TEMP_PATH_SUBSTRINGS if sub in slash_low or sub in slash_dir]
    if matched_temp:
        if allowed_prefixes is None:
            return True
        allowed_slash = tuple(ap.replace("\\", "/").lower().rstrip("/") + "/" for ap in allowed_prefixes)
        if not any(sub in ap_s for sub in matched_temp for ap_s in allowed_slash):
            return True
        if slash_dir.startswith(("/tmp/", "/dev/shm/")):
            return False
    return slash_low.startswith(SKIP_SYSTEM_PATH_PREFIXES)


def _resolve_eligible_file(
    path_str: str,
    allowed_prefixes: tuple[str, ...] | None = None,
    excluded_prefixes: tuple[str, ...] = (),
    require_resolved_basename: str | None = None,
) -> Path | None:
    """Resolve `path_str` and return its `Path` iff it is an eligible non-sensitive text file `<= 2 MiB` within scope.

    When `allowed_prefixes` is `()` (empty tuple), fail closed.
    When `allowed_prefixes` is `None` (unscoped offline debug call), still enforce secret/binary/blacklist checks.
    """
    norm = normalize_local_path(path_str)
    if not norm:
        return None
    if allowed_prefixes is not None and len(allowed_prefixes) == 0:
        return None

    unresolved = Path(norm)
    if _is_sensitive_name(unresolved.name.lower()) or unresolved.suffix.lower() in SKIP_EXTS:
        return None
    try:
        resolved = unresolved.resolve()
    except OSError:
        return None

    if require_resolved_basename is not None and resolved.name.upper() != require_resolved_basename.upper():
        return None

    res_str = str(resolved)
    slash_low = res_str.replace("\\", "/").lower()
    slash_dir = slash_low if slash_low.endswith("/") else slash_low + "/"
    if _is_blocked_system_or_temp_path(slash_low, slash_dir, allowed_prefixes):
        return None
    if _is_sensitive_name(resolved.name.lower()) or resolved.suffix.lower() in SKIP_EXTS:
        return None

    res_norm = _norm_prefix(res_str)
    if excluded_prefixes and any(_is_subpath(res_norm, ep) for ep in excluded_prefixes):
        return None
    if allowed_prefixes is not None and not any(_is_subpath(res_norm, ap) for ap in allowed_prefixes):
        return None

    try:
        st = os.stat(resolved)
        if not (stat.S_ISREG(st.st_mode) and 0 < st.st_size <= MAX_FILE_READ_BYTES):
            return None
    except OSError:
        return None
    return resolved


def is_eligible_text_file(
    path_str: str,
    allowed_prefixes: tuple[str, ...] | None = None,
    excluded_prefixes: tuple[str, ...] = (),
) -> bool:
    """Return True iff `path_str` is an existing non-sensitive text file `<= 2 MiB` within allowed scope.

    When `allowed_prefixes` is `()` (empty tuple), fail closed.
    When `allowed_prefixes` is `None` (unscoped offline debug call), still enforce secret/binary/blacklist checks.
    """
    return _resolve_eligible_file(path_str, allowed_prefixes=allowed_prefixes, excluded_prefixes=excluded_prefixes) is not None


def is_skill_or_spec(path_str: str) -> bool:
    upper = os.path.basename(path_str.replace("\\", "/").rstrip("/")).upper()
    if not upper.endswith(".MD"):
        return False
    return upper == "SKILL.MD" or any(k in upper for k in ("SPEC", "PLAN", "TEMPLATE", "HANDOFF"))


def extract_user_text(raw: str) -> str:
    if not raw:
        return ""
    matches = USER_REQ_RE.findall(raw)
    if matches:
        return "\n".join(m.strip() for m in matches if m.strip())
    return ADD_META_RE.sub("", raw).strip()


def is_compact_slash_cmd(txt: str) -> bool:
    s = txt.strip().lower()
    return s == "/compact" or s.startswith("/compact ")


def is_rehydration_system_message(content: str, checkpoint: int | None = None) -> bool:
    """Return True iff `content` is an injected rehydration `SYSTEM_MESSAGE` (optionally for `checkpoint`)."""
    if not content or MARKER_PREFIX not in content:
        return False
    m = REHYDRATE_SYSMSG_START_RE.match(content)
    if not m:
        return False
    if checkpoint is None:
        return True
    cp_str = m.group(1)
    return cp_str is not None and int(cp_str) == checkpoint


def strip_nested_rehydration(text: str) -> str:
    if "[POST-COMPACT REHYDRATION" not in text:
        return text
    return NESTED_REHYDRATE_RE.sub("", text).strip()


def is_low_info_short_turn(txt: str) -> bool:
    """Filter older ultra-short backchannels/elliptical turns (`<= 4` non-punct chars, no ASCII/digits/slash)."""
    if ASCII_ALNUM_OR_SLASH_RE.search(txt) or any(ch.isdigit() for ch in txt):
        return False
    core = "".join(
        ch for ch in txt if not ch.isspace() and unicodedata.category(ch)[0] not in ("P", "S", "Z", "C")
    )
    return len(core) <= SHORT_USER_MAX_CHARS


def _advance_fence_state(
    line: str, active: tuple[str, int, str] | None
) -> tuple[str, int, str] | None:
    """Track active CommonMark fenced code block `(char, length, opener_line)` across lines."""
    m = FENCE_LINE_RE.match(line)
    if not m:
        return active
    _, marker, rest = m.groups()
    f_ch, f_len = marker[0], len(marker)
    if active is None:
        if f_ch == "`" and "`" in rest:
            return None
        return (f_ch, f_len, line.rstrip())
    a_ch, a_len, _ = active
    if f_ch == a_ch and f_len >= a_len and not rest.strip():
        return None
    return active


def _scan_fence_state(
    text: str, active: tuple[str, int, str] | None = None
) -> tuple[str, int, str] | None:
    state = active
    for ln in text.split("\n"):
        state = _advance_fence_state(ln, state)
    return state


def _snap_head_tail(text: str, head_target: int, tail_target: int) -> tuple[int, int]:
    r"""Snap head/tail cut indices to `\n` boundaries unless a single line exceeds budget."""
    nl_head = text.rfind("\n", 0, head_target + 1)
    if nl_head > 0:
        next_nl = text.find("\n", nl_head + 1)
        crossing_len = (len(text) if next_nl == -1 else next_nl) - (nl_head + 1)
        head_cut = head_target if crossing_len > head_target else nl_head
    else:
        head_cut = head_target

    if tail_target <= 0:
        return head_cut, len(text)
    raw_tail_start = len(text) - tail_target
    if raw_tail_start <= head_cut:
        return head_cut, raw_tail_start
    if raw_tail_start > 0 and text[raw_tail_start - 1] == "\n":
        tail_start = raw_tail_start
    else:
        nl_tail = text.find("\n", raw_tail_start)
        if nl_tail != -1 and nl_tail + 1 < len(text):
            tail_start = nl_tail + 1
        else:
            tail_start = raw_tail_start
    return head_cut, tail_start


def _clip_preserving_fences(
    text: str,
    head_target: int,
    tail_target: int,
    omission_fn: Any,
    min_omitted: int,
) -> tuple[str, bool]:
    if head_target <= 0 or len(text) <= head_target + tail_target + min_omitted:
        return text, False
    head_cut, tail_start = _snap_head_tail(text, head_target, tail_target)
    omitted = tail_start - head_cut
    if omitted <= min_omitted:
        return text, False

    head = text[:head_cut].rstrip("\r")
    tail = text[tail_start:] if tail_start < len(text) else ""

    head_state = _scan_fence_state(head)
    if head_state is not None:
        head = f"{head}\n{head_state[0] * head_state[1]}"

    if tail:
        tail_entry_state = _scan_fence_state(text[:tail_start])
        if tail_entry_state is not None:
            tail = f"{tail_entry_state[2]}\n{tail}"
        tail_end_state = _scan_fence_state(tail)
        if tail_end_state is not None:
            tail = f"{tail}\n{tail_end_state[0] * tail_end_state[1]}"

    return f"{head}{omission_fn(omitted)}{tail}", True


def fold_user_paste(
    txt: str,
    seg_path: Path | None,
    line_no: int,
    step_idx: int,
    *,
    in_verbatim_section: bool = False,
) -> tuple[str, bool]:
    if len(txt) <= FOLD_PASTE_THRESHOLD_CHARS:
        return txt, False
    if in_verbatim_section:
        return _clip_preserving_fences(
            txt,
            FOLD_PASTE_HEAD_CHARS,
            FOLD_PASTE_TAIL_CHARS,
            lambda om: f"\n  ...[folded {om} chars; full text kept verbatim in Section 2 @ Step {step_idx}]...\n",
            120,
        )
    loc = f"{seg_path} line {line_no} (step {step_idx})" if seg_path else f"step {step_idx}"
    return _clip_preserving_fences(
        txt,
        FOLD_PASTE_HEAD_CHARS,
        FOLD_PASTE_TAIL_CHARS,
        lambda om: f"\n  ...[folded {om} chars of pasted content; read_file {loc} if exact text needed]...\n",
        120,
    )


def extract_tool_file_path(args: dict[str, Any]) -> str:
    for key in PATH_ARG_KEYS:
        val = args.get(key)
        if isinstance(val, str) and val.strip():
            norm = normalize_local_path(val)
            if norm:
                return norm
    return ""


def _collapse_runs(items: Sequence[str]) -> list[tuple[str, int]]:
    runs: list[tuple[str, int]] = []
    for val in items:
        if runs and runs[-1][0] == val:
            runs[-1] = (val, runs[-1][1] + 1)
        else:
            runs.append((val, 1))
    return runs


def _collapse_consecutive(items: Sequence[str]) -> list[str]:
    return [
        (f"({val}) ×{cnt}" if " ×" in val else f"{val} ×{cnt}") if cnt > 1 else val
        for val, cnt in _collapse_runs(items)
    ]


def _format_pending_tools(pending: Sequence[str], *, keep_tail: bool = False) -> str:
    collapsed = _collapse_consecutive(pending)
    if len(collapsed) <= 6:
        return " -> ".join(collapsed)
    if keep_tail:
        return f"(+{len(collapsed) - 6} earlier) -> " + " -> ".join(collapsed[-6:])
    return " -> ".join(collapsed[:6]) + f" (+{len(collapsed) - 6} steps)"


def summarize_tool_calls(tool_calls: list[dict[str, Any]]) -> str:
    raw_parts: list[str] = []
    for tc in tool_calls:
        if not isinstance(tc, dict):
            continue
        name = str(tc.get("name") or "?")
        args = tc.get("args") or tc.get("arguments") or {}
        if not isinstance(args, dict):
            args = {}
        if name in READ_TOOL_NAMES or name in WRITE_TOOL_NAMES:
            p = extract_tool_file_path(args)
            base = os.path.basename(p.replace("\\", "/").rstrip("/"))
            raw_parts.append(f"{name}({base or p})")
        elif name in ("run_command", "run_in_terminal"):
            cmd = str(args.get("CommandLine") or args.get("Command") or "").strip().replace("\n", " ")
            if len(cmd) > 60:
                cmd = cmd[:57] + "..."
            raw_parts.append(f"{name}(`{cmd}`)")
        elif name == "invoke_subagent":
            role = str(args.get("Role") or args.get("TypeName") or "").strip()
            raw_parts.append(f"invoke_subagent({role})" if role else "invoke_subagent")
        else:
            raw_parts.append(name)
    if not raw_parts:
        return ""
    collapsed = _collapse_runs(raw_parts)
    shown = [f"{lbl} ×{cnt}" if cnt > 1 else lbl for lbl, cnt in collapsed[:8]]
    if len(collapsed) > 8:
        shown.append(f"+{sum(cnt for _, cnt in collapsed[8:])} more")
    return ", ".join(shown)


def _fence_for(content: str) -> str:
    runs = BACKTICK_RUN_RE.findall(content)
    if not runs:
        return "```"
    return "`" * (max(len(r) for r in runs) + 1)


def clip_turn_text(entry: str, max_chars: int) -> str:
    if len(entry) <= max_chars:
        return entry
    head_len = int(max_chars * 0.70)
    tail_len = max(80, max_chars - head_len - 100)
    clipped, _ = _clip_preserving_fences(
        entry,
        head_len,
        tail_len,
        lambda om: f"\n\n...[turn clipped {om} chars; search transcript_full.jsonl if exact text needed]...\n\n",
        160,
    )
    return clipped


def read_text_clipped(path_str: Path | str, max_chars: int) -> str | None:
    try:
        with open(path_str, "r", encoding="utf-8-sig") as f:
            text = f.read()
    except (OSError, UnicodeDecodeError):
        return None
    if "\x00" in text[:8192]:
        return None
    if len(text) <= max_chars:
        return text
    head_len = int(max_chars * 0.68)
    tail_len = max(0, max_chars - head_len - 88)
    if len(text) - head_len - tail_len <= 80:
        return text
    head_cut, tail_start = _snap_head_tail(text, head_len, tail_len)
    omitted = tail_start - head_cut
    if omitted <= 80:
        return text
    head_part = text[:head_cut]
    tail_part = text[tail_start:] if tail_len > 0 and tail_start < len(text) else ""
    return (
        f"{head_part}\n"
        f"... [truncated ~{omitted} chars in middle; read_file for full content] ...\n"
        f"{tail_part}"
    )


def find_repo_rule_files(
    path_touch: dict[str, int],
    ws_allowed: tuple[str, ...] | None = None,
) -> list[tuple[str, int]]:
    """Discover `AGENTS.md` / `GEMINI.md` at git or project roots of touched paths (paths only, zero content bloat)."""
    if not path_touch:
        return []
    if ws_allowed is not None and len(ws_allowed) == 0:
        return []
    dir_to_root: dict[str, Path | None] = {}
    repo_rules: dict[str, tuple[str, int]] = {}

    for raw_path, step_idx in path_touch.items():
        norm = normalize_local_path(raw_path)
        if not norm:
            continue
        p = Path(norm)
        try:
            if not p.exists():
                continue
            resolved = p.resolve()
        except OSError:
            continue

        res_str = str(resolved)
        slash_low = res_str.replace("\\", "/").lower()
        slash_dir = slash_low if slash_low.endswith("/") else slash_low + "/"
        if _is_blocked_system_or_temp_path(slash_low, slash_dir, ws_allowed):
            continue

        try:
            cur = resolved if resolved.is_dir() else resolved.parent
        except OSError:
            continue

        cur_norm = _norm_prefix(cur)
        if ws_allowed is not None and not any(_is_subpath(cur_norm, ap) for ap in ws_allowed):
            continue

        cur_key = canonical_path_key(str(cur))
        if cur_key in dir_to_root:
            root = dir_to_root[cur_key]
        else:
            root = None
            walk = cur
            visited: list[str] = []
            for _ in range(16):
                w_norm = _norm_prefix(walk)
                if ws_allowed is not None and not any(_is_subpath(w_norm, ap) for ap in ws_allowed):
                    break
                wkey = canonical_path_key(str(walk))
                if wkey in dir_to_root:
                    root = dir_to_root[wkey]
                    break
                visited.append(wkey)
                try:
                    if (walk / ".git").exists() or any((walk / rn).is_file() for rn in REPO_RULE_NAMES):
                        root = walk
                        break
                except OSError:
                    break
                parent = walk.parent
                if parent == walk:
                    break
                walk = parent
            for vk in visited:
                dir_to_root[vk] = root

        if root is None:
            continue
        for rule_name in REPO_RULE_NAMES:
            rpath = root / rule_name
            try:
                if rpath.is_file():
                    rstr = str(rpath)
                    rkey = canonical_path_key(rstr)
                    if rkey not in repo_rules or step_idx > repo_rules[rkey][1]:
                        repo_rules[rkey] = (rstr, step_idx)
            except OSError:
                continue

    return sorted(repo_rules.values(), key=lambda x: x[1], reverse=True)[:MAX_REPO_RULE_FILES]


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


def _parse_step_line_bytes(raw: bytes) -> tuple[int, float | None] | None:
    if not raw or raw.isspace():
        return None
    try:
        obj = json.loads(raw.decode("utf-8-sig", "replace"))
    except json.JSONDecodeError:
        return None
    if not isinstance(obj, dict):
        return None
    sidx = obj.get("step_index")
    if isinstance(sidx, int) and not isinstance(sidx, bool):
        return sidx, parse_iso_epoch(obj.get("created_at"))
    return None


def read_first_step_info(jsonl_path: Path) -> tuple[int, float | None] | None:
    try:
        with open(jsonl_path, "rb") as f:
            for _ in range(16):
                raw = f.readline()
                if not raw:
                    return None
                parsed = _parse_step_line_bytes(raw)
                if parsed is not None:
                    return parsed
    except OSError:
        return None
    return None


def _find_step_epoch_with_last(
    jsonl_path: Path, target_step: int
) -> tuple[int | None, float | None, float | None, bool]:
    """Return `(last_step, last_epoch, target_epoch, found)` via adaptive tail window + full-line fallback."""
    try:
        st_size = jsonl_path.stat().st_size
        if st_size <= 0:
            return None, None, None, False

        last_step: int | None = None
        last_epoch: float | None = None
        tail_window = 65536
        with open(jsonl_path, "rb") as fb:
            while True:
                seek_pos = max(0, st_size - tail_window)
                fb.seek(seek_pos)
                raw_lines = fb.read().splitlines()
                if seek_pos > 0 and raw_lines:
                    raw_lines = raw_lines[1:]
                found_any = False
                for raw in reversed(raw_lines):
                    parsed = _parse_step_line_bytes(raw)
                    if parsed is None:
                        continue
                    sidx, ep = parsed
                    found_any = True
                    if last_step is None:
                        last_step, last_epoch = sidx, ep
                        if last_step < target_step:
                            return last_step, last_epoch, None, False
                    if sidx == target_step:
                        return last_step, last_epoch, ep, True
                    if sidx < target_step:
                        return last_step, last_epoch, None, False
                if found_any or seek_pos == 0 or tail_window >= 1_048_576:
                    break
                tail_window = min(1_048_576, tail_window * 4)

        step_pat = re.compile(rb'"step_index"\s*:\s*' + str(target_step).encode("ascii") + rb"\b")
        target_epoch: float | None = None
        found_target = False
        scanned_last_step: int | None = last_step
        scanned_last_epoch: float | None = last_epoch
        with open(jsonl_path, "rb") as fb:
            for raw in fb:
                if b'"step_index"' not in raw:
                    continue
                if last_step is None:
                    parsed = _parse_step_line_bytes(raw)
                    if parsed is not None:
                        sidx, ep = parsed
                        scanned_last_step, scanned_last_epoch = sidx, ep
                        if sidx == target_step:
                            found_target, target_epoch = True, ep
                elif step_pat.search(raw):
                    parsed = _parse_step_line_bytes(raw)
                    if parsed is not None and parsed[0] == target_step:
                        return last_step, last_epoch, parsed[1], True
        if found_target and scanned_last_step is not None:
            return scanned_last_step, scanned_last_epoch, target_epoch, True
        return scanned_last_step, scanned_last_epoch, None, False
    except OSError:
        return None, None, None, False


def find_step_epoch_in_transcript(jsonl_path: Path, target_step: int) -> tuple[int, float | None] | None:
    last_step, _, target_epoch, found = _find_step_epoch_with_last(jsonl_path, target_step)
    if found and last_step is not None:
        return last_step, target_epoch
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
                obj = json.loads(raw.decode("utf-8-sig", "replace"))
            except json.JSONDecodeError:
                continue
            if not isinstance(obj, dict):
                continue
            cid = obj.get("conversationId")
            ws = obj.get("workspace")
            if isinstance(cid, str) and cid:
                if isinstance(ws, str) and ws.strip():
                    ws_norm = normalize_local_path(ws)
                    if ws_norm:
                        cid_workspaces.setdefault(cid, set()).add(canonical_path_key(ws_norm))
                disp = obj.get("display")
                disp_low = disp.strip().lower() if isinstance(disp, str) else ""
                ts = obj.get("timestamp")
                if (
                    (disp_low == "/fork" or disp_low.startswith("/fork "))
                    and isinstance(ts, (int, float))
                    and not isinstance(ts, bool)
                ):
                    fork_events.append((cid, float(ts) / 1000.0))
    except OSError:
        pass
    return cid_workspaces, fork_events


def validate_cached_ancestors(
    cached: Any,
    cur_first_step: int,
    brain_dir: Path | None = None,
) -> list[dict[str, Any]] | None:
    if not isinstance(cached, list) or not cached or len(cached) > MAX_FORK_DEPTH:
        return None
    brain_prefix: str | None = None
    if brain_dir is not None:
        try:
            brain_prefix = _norm_prefix(brain_dir.resolve())
        except OSError:
            brain_prefix = _norm_prefix(brain_dir)
    cleaned: list[dict[str, Any]] = []
    for item in cached:
        if not isinstance(item, dict):
            return None
        p_str, cid, s_min, s_max = item.get("path"), item.get("cid"), item.get("min_step"), item.get("max_step")
        if (
            not isinstance(p_str, str)
            or not isinstance(cid, str)
            or not cid
            or not isinstance(s_min, int)
            or isinstance(s_min, bool)
            or not isinstance(s_max, int)
            or isinstance(s_max, bool)
            or s_min < 0
            or s_min > s_max
        ):
            return None
        p_obj = Path(p_str)
        try:
            if not p_obj.is_file():
                return None
            if brain_prefix is not None and not _is_subpath(_norm_prefix(p_obj.resolve()), brain_prefix):
                return None
        except OSError:
            return None
        cleaned.append({"cid": cid, "path": p_str, "min_step": s_min, "max_step": s_max})
    for idx in range(len(cleaned) - 1):
        if cleaned[idx]["max_step"] + 1 != cleaned[idx + 1]["min_step"]:
            return None
    return cleaned if cleaned[-1]["max_step"] + 1 == cur_first_step else None


def resolve_fork_ancestors(full_path: Path, cached_ancestors: Any = None) -> list[dict[str, Any]]:
    """Trace linear `/fork` ancestor chain up to `MAX_FORK_DEPTH` hops with strict `[min_step, K-1]` slicing."""
    first_info = read_first_step_info(full_path)
    if first_info is None or first_info[0] <= 0:
        return []
    cur_first_step, cur_first_epoch = first_info

    parts = full_path.parts
    if len(parts) < 5 or parts[-2] != "logs" or parts[-3] != ".system_generated":
        return []
    brain_dir = full_path.parents[3]
    if not brain_dir.is_dir():
        return []

    valid_cache = validate_cached_ancestors(cached_ancestors, cur_first_step, brain_dir=brain_dir)
    if valid_cache is not None:
        return valid_cache

    cid_workspaces, fork_events = load_history_fork_hints(brain_dir)
    cur_cid = infer_cid_from_transcript_path(full_path)

    # Pre-index brain_dir once (O(N) instead of O(hops * N)), probing compact transcript.jsonl when available.
    indexed_cands: list[tuple[str, Path, Path, int, float | None]] = []
    try:
        with os.scandir(brain_dir) as it:
            for entry in it:
                cand_cid = entry.name
                if cand_cid == cur_cid or not entry.is_dir(follow_symlinks=False):
                    continue
                cand_logs = Path(entry.path) / ".system_generated" / "logs"
                cand_compact = cand_logs / "transcript.jsonl"
                cand_full = cand_logs / "transcript_full.jsonl"
                has_compact = cand_compact.is_file()
                has_full = cand_full.is_file()
                if not has_compact and not has_full:
                    continue
                probe_path = cand_compact if has_compact else cand_full
                seg_path = cand_full if has_full else cand_compact
                cand_first = read_first_step_info(probe_path)
                if cand_first is None and has_compact and has_full:
                    probe_path = cand_full
                    cand_first = read_first_step_info(probe_path)
                if cand_first is None or cand_first[0] < 0 or cand_first[0] >= cur_first_step:
                    continue
                indexed_cands.append((cand_cid, probe_path, seg_path, cand_first[0], cand_first[1]))
    except OSError:
        return []

    visited: set[str] = {cur_cid}
    fork_cids = {fcid for fcid, _ in fork_events}
    cand_max_steps: dict[str, int] = {}
    cand_last_epochs: dict[str, float] = {}
    ancestors_rev: list[dict[str, Any]] = []
    child_cid, child_first_step, child_first_epoch = cur_cid, cur_first_step, cur_first_epoch

    for _ in range(MAX_FORK_DEPTH):
        if child_first_step <= 0:
            break
        need_prev = child_first_step - 1
        child_ws = cid_workspaces.get(child_cid, set())
        best_score: tuple[int, int, int, int, float] | None = None
        best_seg: tuple[str, Path, int, float | None] | None = None

        for cand_cid, probe_path, seg_path, cand_first_step, cand_first_epoch in indexed_cands:
            if cand_cid in visited or cand_first_step > need_prev:
                continue
            known_max = cand_max_steps.get(cand_cid)
            if known_max is not None and known_max < need_prev:
                continue
            if (
                child_first_epoch is not None
                and cand_first_epoch is not None
                and cand_first_epoch > child_first_epoch + 5.0
            ):
                continue
            cand_ws = cid_workspaces.get(cand_cid, set())
            same_ws = int(bool(child_ws and cand_ws and (child_ws & cand_ws)))
            known_last_ep = cand_last_epochs.get(cand_cid)
            if (
                known_last_ep is not None
                and child_first_epoch is not None
                and known_last_ep < child_first_epoch - 86400.0
                and cand_cid not in fork_cids
                and same_ws == 0
            ):
                continue

            cand_last_step, cand_last_epoch, prev_epoch, found = _find_step_epoch_with_last(probe_path, need_prev)
            if not found and probe_path != seg_path:
                cand_last_step, cand_last_epoch, prev_epoch, found = _find_step_epoch_with_last(seg_path, need_prev)
            if cand_last_step is not None:
                cand_max_steps[cand_cid] = cand_last_step
            if cand_last_epoch is not None:
                cand_last_epochs[cand_cid] = cand_last_epoch
            if not found or cand_last_step is None:
                continue

            has_epochs = child_first_epoch is not None and prev_epoch is not None
            if has_epochs:
                assert child_first_epoch is not None and prev_epoch is not None
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
            if fork_cmd_match == 0 and same_ws == 0 and has_epochs and time_delta > 86400.0:
                continue

            exact_tip = int(cand_last_step == need_prev)
            score = (fork_cmd_match, same_ws, int(time_delta <= 300.0), exact_tip, -time_delta)
            if best_score is None or score > best_score:
                best_score = score
                best_seg = (cand_cid, seg_path, cand_first_step, cand_first_epoch)

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


def format_user_timeline(
    user_messages: list[tuple[int, str, Path | None, int]],
    budget_chars: int = DEFAULT_BUDGET_ALL_USER_CHARS,
    *,
    fold_positions: set[int] | frozenset[int] | None = None,
    verbatim_positions: set[int] | frozenset[int] | None = None,
) -> tuple[str, int, int, int, int]:
    """Render complete numbered user prompt timeline with older short-turn denoising and paste folding."""
    total_real = sum(1 for _, txt, _, _ in user_messages if not txt.startswith("--- [/fork branch "))
    verbatim_in_sec2 = verbatim_positions or frozenset()
    forced_fold = (fold_positions or frozenset()) | verbatim_in_sec2

    def render_pass(fold_before_recent: int) -> tuple[list[tuple[bool, bool, str]], int]:
        entries: list[tuple[bool, bool, str]] = []
        idx = 0
        filtered_short = 0
        verbatim_cutoff = total_real - RECENT_USER_VERBATIM_COUNT
        fold_cutoff = total_real - fold_before_recent
        for pos, (step_s, txt, spath, lno) in enumerate(user_messages):
            if txt.startswith("--- [/fork branch "):
                entries.append((False, False, f"- {txt}"))
                continue
            idx += 1
            if idx > 1 and idx <= verbatim_cutoff and is_low_info_short_turn(txt):
                filtered_short += 1
                continue
            out_txt = txt
            did_fold = False
            if idx <= fold_cutoff or pos in forced_fold:
                out_txt, did_fold = fold_user_paste(
                    txt,
                    spath,
                    lno,
                    step_s,
                    in_verbatim_section=(pos in verbatim_in_sec2),
                )
            first_line = out_txt.split("\n", 1)[0]
            sep = "\n" if _advance_fence_state(first_line, None) is not None else " "
            entries.append((True, did_fold, f"- [#{idx} @ step {step_s}]:{sep}{out_txt}"))
        return entries, filtered_short

    entries, filtered_short_count = render_pass(RECENT_USER_VERBATIM_COUNT)
    all_user_block = "\n".join(ln for _, _, ln in entries)

    if len(all_user_block) > budget_chars and total_real > 10:
        entries, filtered_short_count = render_pass(10)
        all_user_block = "\n".join(ln for _, _, ln in entries)

    if len(all_user_block) > budget_chars:
        entries, filtered_short_count = render_pass(0)
        all_user_block = "\n".join(ln for _, _, ln in entries)

    # Safety clip any individual bullet still exceeding budget_chars
    if entries and len(all_user_block) > budget_chars:
        single_cap = max(256, budget_chars) if len(entries) == 1 else max(256, budget_chars - 64)
        clipped_any = False
        new_entries: list[tuple[bool, bool, str]] = []
        for is_real, is_folded, ln in entries:
            if len(ln) > single_cap:
                new_ln = clip_turn_text(ln, single_cap)
                new_entries.append((is_real, is_folded or (new_ln != ln), new_ln))
                clipped_any = True
            else:
                new_entries.append((is_real, is_folded, ln))
        if clipped_any:
            entries = new_entries
            all_user_block = "\n".join(ln for _, _, ln in entries)

    final_entries = entries
    if len(all_user_block) > budget_chars and len(entries) >= 2:
        omit_marker = "- [... earlier middle prompts omitted for budget ...]"
        latest = entries[-1]
        remaining = budget_chars - len(latest[2]) - len(omit_marker) - 2

        head_kept: list[tuple[bool, bool, str]] = []
        head_used = 0
        head_limit = min(5, len(entries) - 1)
        for item in entries[:head_limit]:
            add_len = len(item[2]) + (1 if head_kept else 0)
            if head_used + add_len > remaining:
                break
            head_kept.append(item)
            head_used += add_len

        tail_rev: list[tuple[bool, bool, str]] = []
        tail_used = 0
        tail_budget = remaining - head_used - (1 if head_kept else 0)
        for item in reversed(entries[len(head_kept) : -1]):
            add_len = len(item[2]) + 1
            if tail_used + add_len > tail_budget:
                break
            tail_rev.append(item)
            tail_used += add_len
        tail_rev.reverse()
        tail_kept = [*tail_rev, latest]

        final_entries = [*head_kept, *tail_kept]
        if len(final_entries) < len(entries):
            final_lines = [it[2] for it in head_kept] + [omit_marker] + [it[2] for it in tail_kept]
        else:
            final_lines = [it[2] for it in final_entries]
        all_user_block = "\n".join(final_lines)

    kept_count = sum(1 for is_real, _, _ in final_entries if is_real)
    folded_paste_count = sum(1 for _, is_folded, _ in final_entries if is_folded)
    return all_user_block, total_real, kept_count, filtered_short_count, folded_paste_count


def _safe_fence_opener(active: tuple[str, int, str], max_bytes: int) -> str:
    f_ch, f_len, f_opener = active
    if len(f_opener.encode("utf-8")) <= max(32, max_bytes // 4):
        return f_opener
    return f_ch * f_len


def _split_line_utf8(line: str, max_bytes: int) -> list[str]:
    cap = max(4, max_bytes)
    out: list[str] = []
    buf: list[str] = []
    bb = 0
    for ch in line:
        cb = len(ch.encode("utf-8"))
        if bb + cb > cap and buf:
            out.append("".join(buf))
            buf, bb = [], 0
        buf.append(ch)
        bb += cb
    if buf:
        out.append("".join(buf))
    return out or [""]


def _split_oversized_block(text: str, max_bytes: int) -> list[str]:
    if len(text.encode("utf-8")) <= max_bytes:
        return [text]
    pieces: list[str] = []
    cur_lines: list[str] = []
    cur_bytes = 0
    active_fence: tuple[str, int, str] | None = None

    def _flush(close_fence: tuple[str, int, str] | None) -> None:
        nonlocal cur_lines, cur_bytes
        if not cur_lines:
            return
        if close_fence is not None:
            cur_lines.append(close_fence[0] * close_fence[1])
        pieces.append("\n".join(cur_lines))
        cur_lines, cur_bytes = [], 0

    def _try_append(line: str, lb: int, next_close_bytes: int) -> bool:
        nonlocal cur_lines, cur_bytes
        if cur_lines:
            if cur_bytes + 1 + lb + next_close_bytes <= max_bytes:
                cur_lines.append(line)
                cur_bytes += 1 + lb
                return True
            return False
        if active_fence is not None:
            op = _safe_fence_opener(active_fence, max_bytes)
            op_b = len(op.encode("utf-8")) + 1
            if op_b + lb + next_close_bytes <= max_bytes:
                cur_lines = [op, line]
                cur_bytes = op_b + lb
                return True
            return False
        if lb + next_close_bytes <= max_bytes:
            cur_lines = [line]
            cur_bytes = lb
            return True
        return False

    for line in text.split("\n"):
        next_fence = _advance_fence_state(line, active_fence)
        next_close_bytes = (1 + next_fence[1]) if next_fence is not None else 0
        lb = len(line.encode("utf-8"))

        if not cur_lines and active_fence is not None and next_fence is None:
            active_fence = None
            continue
        if _try_append(line, lb, next_close_bytes):
            active_fence = next_fence
            continue
        if cur_lines:
            _flush(active_fence)
            if active_fence is not None and next_fence is None:
                active_fence = None
                continue
            if _try_append(line, lb, next_close_bytes):
                active_fence = next_fence
                continue

        if active_fence is not None:
            op = _safe_fence_opener(active_fence, max_bytes)
            cl = active_fence[0] * active_fence[1]
            overhead = len(op.encode("utf-8")) + 1 + 1 + len(cl.encode("utf-8"))
            for sl in _split_line_utf8(line, max(32, max_bytes - overhead)):
                pieces.append(f"{op}\n{sl}\n{cl}")
        else:
            for sl in _split_line_utf8(line, max(32, max_bytes)):
                pieces.append(sl)

    if cur_lines:
        _flush(active_fence)
    return pieces


def split_into_inject_chunks(
    sections: list[str],
    max_bytes: int = MAX_INJECT_STEP_BYTES,
    checkpoint: int | None = None,
) -> list[str]:
    """Pack sections into `<= max_bytes` UTF-8 chunks so no `SystemMessageStep` hits AGY's 48 KiB cap."""
    header_reserve = 112
    limit = max(256, max_bytes - header_reserve)
    atomic_blocks: list[str] = []
    for sec in sections:
        if len(sec.encode("utf-8")) <= limit:
            atomic_blocks.append(sec)
        else:
            if sec.startswith("## 1."):
                split_re, sep_str = TIMELINE_SPLIT_RE, "\n"
            elif sec.startswith("## 2."):
                split_re, sep_str = TURN_SPLIT_RE, "\n\n"
            else:
                split_re, sep_str = FILE_SPLIT_RE, "\n\n"
            merged_items: list[str] = []
            for part in split_re.split(sec):
                if merged_items and _scan_fence_state(merged_items[-1]) is not None:
                    merged_items[-1] = f"{merged_items[-1]}{sep_str}{part}"
                else:
                    merged_items.append(part)
            for item in merged_items:
                if len(item.encode("utf-8")) <= limit:
                    atomic_blocks.append(item)
                else:
                    atomic_blocks.extend(_split_oversized_block(item, limit))

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
    return [
        ch if idx == 1 else f"[POST-COMPACT REHYDRATION cont. part {idx}/{total}{cp_note}]\n\n{ch}"
        for idx, ch in enumerate(raw_chunks, 1)
    ]


def build_rehydration(
    full_path: Path,
    up_to_checkpoint_step: int | None = None,
    ancestors: list[dict[str, Any]] | None = None,
    config: RehydrateConfig | None = None,
    workspace_roots: list[Path] | None = None,
    artifact_dir: Path | None = None,
    simulate_if_no_checkpoint: bool = False,
    *,
    expected_checkpoint: int | None = None,
    max_fresh_gap: int | None = None,
    out_status: dict[str, Any] | None = None,
    cached_ancestors: Any = None,
) -> tuple[str, dict[str, Any]] | None:
    """Build deterministic 4-layer lossless rehydration payload and UTF-8 safe `injectSteps` chunks."""
    cfg = config or RehydrateConfig()
    checkpoints: list[int] = []
    already_rehydrated_after: set[int] = set()
    max_seen_step = -1
    cur_first_step: int | None = None
    tail_raw_lines: list[str] = []

    def _set_status(reason: str, cp_val: int | None = None) -> None:
        if out_status is not None:
            out_status["reason"] = reason
            out_status["target_cp"] = cp_val
            out_status["max_seen_step"] = max_seen_step

    with open(full_path, "r", encoding="utf-8-sig", errors="replace") as f:
        last_cp: int | None = None
        for line in f:
            if not line or line.isspace():
                continue
            need_parse = (
                cur_first_step is None
                or '"CHECKPOINT"' in line
                or MARKER_PREFIX in line
            )
            if not need_parse and up_to_checkpoint_step is not None:
                m_step = STEP_INDEX_START_RE.match(line)
                if m_step is None or int(m_step.group(1)) > up_to_checkpoint_step:
                    need_parse = True

            if not need_parse:
                if len(tail_raw_lines) >= 4:
                    tail_raw_lines.pop(0)
                tail_raw_lines.append(line)
                continue

            try:
                obj = json.loads(line)
            except json.JSONDecodeError:
                continue
            if not isinstance(obj, dict):
                continue
            step_idx = obj.get("step_index", -1)
            if not isinstance(step_idx, int) or isinstance(step_idx, bool) or step_idx < 0:
                continue
            if cur_first_step is None:
                cur_first_step = step_idx
            if up_to_checkpoint_step is not None and step_idx > up_to_checkpoint_step:
                break
            if step_idx > max_seen_step:
                max_seen_step = step_idx
            stype = obj.get("type")
            if stype == "CHECKPOINT":
                checkpoints.append(step_idx)
                last_cp = step_idx
            elif stype == "SYSTEM_MESSAGE" and last_cp is not None:
                if is_rehydration_system_message(str(obj.get("content") or ""), checkpoint=last_cp):
                    already_rehydrated_after.add(last_cp)

    for raw_tail in reversed(tail_raw_lines):
        try:
            obj = json.loads(raw_tail)
        except json.JSONDecodeError:
            continue
        if not isinstance(obj, dict):
            continue
        step_idx = obj.get("step_index", -1)
        if not isinstance(step_idx, int) or isinstance(step_idx, bool) or step_idx < 0:
            continue
        if up_to_checkpoint_step is not None and step_idx > up_to_checkpoint_step:
            continue
        if step_idx > max_seen_step:
            max_seen_step = step_idx
        break

    latest_full_cp = max(checkpoints) if checkpoints else None
    if expected_checkpoint is not None and (latest_full_cp is None or latest_full_cp < expected_checkpoint):
        _set_status("full_lag", latest_full_cp)
        return None

    simulated = False
    if (
        simulate_if_no_checkpoint
        and latest_full_cp is not None
        and latest_full_cp in already_rehydrated_after
        and max_seen_step > latest_full_cp + 2
    ):
        target_cp = max_seen_step + 1
        simulated = True
    elif latest_full_cp is not None:
        target_cp = latest_full_cp
        if up_to_checkpoint_step is None and target_cp in already_rehydrated_after:
            _set_status("already_rehydrated", target_cp)
            return None
    elif simulate_if_no_checkpoint and max_seen_step >= 0:
        target_cp = max_seen_step + 1
        simulated = True
    else:
        _set_status("no_checkpoint", None)
        return None

    if (
        max_fresh_gap is not None
        and not simulated
        and up_to_checkpoint_step is None
        and max_seen_step > target_cp
        and (max_seen_step - target_cp) > max_fresh_gap
    ):
        _set_status("stale_skipped", target_cp)
        return None

    cur_cid = infer_cid_from_transcript_path(full_path)
    effective_artifact_dir = artifact_dir or infer_artifact_dir_from_transcript_path(full_path)
    scratch_excluded = _prepare_prefixes([effective_artifact_dir / "scratch"]) if effective_artifact_dir else ()

    if workspace_roots is None:
        ws_allowed: tuple[str, ...] | None = None
        skill_allowed: tuple[str, ...] | None = None
        art_allowed: tuple[str, ...] | None = None
    else:
        effective_ws_roots = [*workspace_roots, *(Path(p) for p in cfg.extra_allowed_paths)]
        ws_allowed = _prepare_prefixes(effective_ws_roots)
        skill_allowed = _prepare_prefixes([*effective_ws_roots, *default_custom_skill_roots(effective_artifact_dir)])
        art_allowed = _prepare_prefixes(
            [*effective_ws_roots, *([effective_artifact_dir] if effective_artifact_dir else [])]
        )

    effective_first_step = cur_first_step if cur_first_step is not None else 0
    if ancestors is not None:
        resolved_ancestors = ancestors
    elif effective_first_step <= 0:
        resolved_ancestors = []
    else:
        resolved_ancestors = resolve_fork_ancestors(full_path, cached_ancestors=cached_ancestors)

    segments: list[dict[str, Any]] = [
        *resolved_ancestors,
        {"cid": cur_cid, "path": str(full_path), "min_step": effective_first_step, "max_step": target_cp - 1},
    ]

    all_checkpoints: list[int] = list(checkpoints)
    user_messages: list[tuple[int, str, Path | None, int]] = []
    dialogue_entries: list[tuple[str, int | None]] = []
    modified_touch: dict[str, tuple[str, int]] = {}
    read_touch: dict[str, tuple[str, int]] = {}
    skill_spec_touch: dict[str, tuple[str, int]] = {}
    touched_paths: dict[str, int] = {}
    pending_tools: list[str] = []

    for seg_idx, seg in enumerate(segments):
        seg_path = Path(seg["path"])
        seg_min = int(seg["min_step"])
        seg_max = min(int(seg["max_step"]), target_cp - 1)
        if seg_min > seg_max or not seg_path.is_file():
            continue
        if seg_idx > 0:
            fork_banner = (
                f"--- [/fork branch {str(segments[seg_idx - 1]['cid'])[:8]} "
                f"-> {str(seg['cid'])[:8]} @ step {seg_min}] ---"
            )
            user_messages.append((seg_min, fork_banner, None, 0))
            dialogue_entries.append((fork_banner, None))

        with open(seg_path, "r", encoding="utf-8-sig", errors="replace") as f:
            for line_no, line in enumerate(f, 1):
                if (
                    '"USER_INPUT"' not in line
                    and '"PLANNER_RESPONSE"' not in line
                    and '"CHECKPOINT"' not in line
                ):
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
                if stype == "CHECKPOINT":
                    all_checkpoints.append(step_idx)
                elif stype == "USER_INPUT":
                    utext = extract_user_text(str(obj.get("content") or ""))
                    if utext and not is_compact_slash_cmd(utext):
                        user_pos = len(user_messages)
                        user_messages.append((step_idx, utext, seg_path, line_no))
                        dialogue_entries.append((f"[Step {step_idx} | User]:\n{utext}", user_pos))
                        pending_tools.clear()
                elif stype == "PLANNER_RESPONSE":
                    tcs = obj.get("tool_calls") or []
                    valid_tcs = [tc for tc in tcs if isinstance(tc, dict)]
                    if valid_tcs:
                        tool_summary = summarize_tool_calls(valid_tcs)
                        if tool_summary:
                            pending_tools.append(tool_summary)
                        for tc in valid_tcs:
                            tname = str(tc.get("name") or "")
                            targs = tc.get("args") or tc.get("arguments") or {}
                            if not isinstance(targs, dict):
                                continue
                            fpath = extract_tool_file_path(targs)
                            if fpath:
                                fkey = canonical_path_key(fpath)
                                touched_paths[fpath] = step_idx
                                if is_skill_or_spec(fpath):
                                    skill_spec_touch[fkey] = (fpath, step_idx)
                                elif tname in WRITE_TOOL_NAMES:
                                    modified_touch[fkey] = (fpath, step_idx)
                                elif tname in READ_TOOL_NAMES:
                                    read_touch[fkey] = (fpath, step_idx)
                            elif tname in ("run_command", "run_in_terminal"):
                                cmd_str = str(targs.get("CommandLine") or targs.get("Command") or "")
                                for cand in ABS_PATH_RE.findall(cmd_str)[:8]:
                                    cleaned = cand.rstrip(".,;:)'\"`")
                                    if len(cleaned) >= 4:
                                        touched_paths[cleaned] = step_idx
                    mcontent = strip_nested_rehydration(str(obj.get("content") or "").strip())
                    if mcontent:
                        tool_prefix = ""
                        if pending_tools:
                            tool_prefix = f"  (Tools run: {_format_pending_tools(pending_tools)})\n"
                            pending_tools.clear()
                        dialogue_entries.append((f"[Step {step_idx} | Assistant]:\n{tool_prefix}{mcontent}", None))

    if pending_tools:
        dialogue_entries.append(
            (f"[Pre-Compaction In-Progress Tools]: {_format_pending_tools(pending_tools, keep_tail=True)}", None)
        )

    prev_cp = max((c for c in all_checkpoints if c < target_cp), default=None)
    file_min_step = (prev_cp + 1) if (cfg.file_scope_since_prev_checkpoint and prev_cp is not None) else -1

    # Layer 2: Recent Pre-Compaction Verbatim Dialogue (computed before Layer 1 to deduplicate long pastes)
    kept_turn_items: list[tuple[str, int | None, bool]] = []
    used_turns = 0
    for entry, u_pos in reversed(dialogue_entries):
        clipped_entry = clip_turn_text(entry, cfg.max_single_turn_chars)
        if used_turns + len(clipped_entry) > cfg.budget_recent_turns_chars and kept_turn_items:
            break
        kept_turn_items.append((clipped_entry, u_pos, clipped_entry == entry))
        used_turns += len(clipped_entry) + 2
    kept_turn_items.reverse()
    while kept_turn_items and kept_turn_items[0][0].startswith("--- [/fork branch "):
        kept_turn_items.pop(0)
    kept_turns = [t for t, _, _ in kept_turn_items]
    dialogue_user_positions = {u_pos for _, u_pos, _ in kept_turn_items if u_pos is not None}
    dialogue_verbatim_user_positions = {
        u_pos for _, u_pos, is_verbatim in kept_turn_items if u_pos is not None and is_verbatim
    }

    # Layer 1: Complete User Request Timeline (fold >800-char pastes already present in Layer 2)
    all_user_block, real_user_count, kept_user_count, filtered_short_count, folded_paste_count = format_user_timeline(
        user_messages,
        budget_chars=cfg.budget_all_user_chars,
        fold_positions=dialogue_user_positions,
        verbatim_positions=dialogue_verbatim_user_positions,
    )

    # Layer 3: Active Skills, Spec Docs & Repo Rule Pointers
    skill_blocks: list[str] = []
    restored_skill_paths: list[str] = []
    restored_resolved_keys: set[str] = set()
    used_skills = 0
    if cfg.budget_skills_specs_chars >= 800:
        for _, (spath, sstep) in sorted(skill_spec_touch.items(), key=lambda x: x[1][1], reverse=True):
            is_skill_md = os.path.basename(spath).upper() == "SKILL.MD"
            scope = skill_allowed if is_skill_md else art_allowed
            resolved = _resolve_eligible_file(
                spath,
                allowed_prefixes=scope,
                excluded_prefixes=scratch_excluded,
                require_resolved_basename="SKILL.MD" if is_skill_md else None,
            )
            if resolved is None:
                continue
            rkey = canonical_path_key(str(resolved))
            if rkey in restored_resolved_keys:
                continue
            rem = cfg.budget_skills_specs_chars - used_skills
            if rem < 800:
                break
            content = read_text_clipped(resolved, min(cfg.budget_single_file_chars, max(200, rem - len(spath) - 48)))
            if content:
                restored_resolved_keys.add(rkey)
                fence = _fence_for(content)
                block = f"### `{spath}` (touched @ step {sstep})\n{fence}\n{content}\n{fence}"
                skill_blocks.append(block)
                restored_skill_paths.append(spath)
                used_skills += len(block) + 2

    # Layer 4: Active Workspace Files (Top Modified + Top Referenced; scoped to current compaction cycle by default)
    cycle_mod_step_by_rkey: dict[str, int] = {}
    mod_candidates: list[tuple[str, Path, str, int]] = []
    for _, (p, st) in sorted(modified_touch.items(), key=lambda x: x[1][1], reverse=True):
        if st < file_min_step:
            continue
        resolved = _resolve_eligible_file(p, allowed_prefixes=ws_allowed, excluded_prefixes=scratch_excluded)
        if resolved is None:
            continue
        rkey = canonical_path_key(str(resolved))
        if rkey in restored_resolved_keys or rkey in cycle_mod_step_by_rkey:
            continue
        cycle_mod_step_by_rkey[rkey] = st
        if len(mod_candidates) < cfg.max_modified_files:
            mod_candidates.append((rkey, resolved, p, st))
    mod_rkeys = {k for k, _, _, _ in mod_candidates}

    ref_candidates: list[tuple[str, Path, str, int]] = []
    seen_ref_rkeys: set[str] = set()
    if cfg.max_referenced_files > 0:
        for _, (p, st) in sorted(read_touch.items(), key=lambda x: x[1][1], reverse=True):
            if st < file_min_step:
                continue
            resolved = _resolve_eligible_file(p, allowed_prefixes=ws_allowed, excluded_prefixes=scratch_excluded)
            if resolved is None:
                continue
            rkey = canonical_path_key(str(resolved))
            if rkey in restored_resolved_keys or rkey in seen_ref_rkeys:
                continue
            mod_st = cycle_mod_step_by_rkey.get(rkey)
            if mod_st is not None and (mod_st >= st or rkey in mod_rkeys):
                continue
            seen_ref_rkeys.add(rkey)
            ref_candidates.append((rkey, resolved, p, st))
            if len(ref_candidates) >= cfg.max_referenced_files:
                break

    file_blocks: list[str] = []
    restored_file_paths: list[str] = []
    used_files = 0
    if cfg.budget_files_total_chars >= 800:
        for role_label, items in (("Modified", mod_candidates), ("Referenced", ref_candidates)):
            for _, resolved_path, fpath, fstep in items:
                rem = cfg.budget_files_total_chars - used_files
                if rem < 800:
                    break
                content = read_text_clipped(
                    resolved_path, min(cfg.budget_single_file_chars, max(200, rem - len(fpath) - 64))
                )
                if content:
                    fence = _fence_for(content)
                    block = f"### [{role_label} @ step {fstep}] `{fpath}`\n{fence}\n{content}\n{fence}"
                    file_blocks.append(block)
                    restored_file_paths.append(f"{role_label}:{fpath}")
                    used_files += len(block) + 2

    lineage_tag = ""
    if resolved_ancestors:
        chain_parts = [
            f"{str(seg['cid'])[:8]}({seg['min_step']}..{min(int(seg['max_step']), target_cp - 1)})"
            for seg in segments
            if int(seg["min_step"]) <= min(int(seg["max_step"]), target_cp - 1)
        ]
        lineage_tag = f" | fork_lineage={'->'.join(chain_parts)}"

    omitted_middle_count = max(0, real_user_count - filtered_short_count - kept_user_count)
    hdr_notes: list[str] = []
    if filtered_short_count > 0:
        hdr_notes.append(f"{filtered_short_count} older <={SHORT_USER_MAX_CHARS}-char short turns filtered")
    if folded_paste_count > 0:
        hdr_notes.append(f"{folded_paste_count} long pastes folded")
    if omitted_middle_count > 0:
        hdr_notes.append(f"{omitted_middle_count} middle prompts omitted for budget")
    if hdr_notes:
        sec1_hdr = (
            f"## 1. Complete User Request Timeline "
            f"({kept_user_count}/{real_user_count} messages; {'; '.join(hdr_notes)})"
        )
    else:
        sec1_hdr = f"## 1. Complete User Request Timeline ({real_user_count} messages verbatim)"

    repo_rule_entries = find_repo_rule_files(touched_paths, ws_allowed=ws_allowed)
    raw_transcript_paths: list[str] = []
    for seg in segments:
        sp_str = str(seg.get("path") or "")
        if sp_str and sp_str not in raw_transcript_paths:
            raw_transcript_paths.append(sp_str)
    transcript_list_md = "\n".join(f"  - `{p}`" for p in raw_transcript_paths)

    header_note = (
        "The harness just compacted earlier history into `<CONTEXT_SUMMARY>`, which is lossy.\n"
        "Below is a PARTIAL verbatim slice rehydrated from local logs and disk "
        "(user request timeline, recent pre-compaction turns, and recently touched files).\n"
        "- Whenever `<CONTEXT_SUMMARY>` conflicts with the verbatim text or live files below, "
        "ALWAYS trust the records below.\n"
        "- Do NOT treat this slice as the complete world state: earlier tool outputs, subagent edits, "
        "and older files are NOT all included here.\n"
        "- Absence from `<CONTEXT_SUMMARY>` or this block does NOT mean a capability, file, or prior setup "
        "does not exist. Before claiming something is unavailable or asking the user to check manually, "
        "verify on disk or search the full raw transcript(s):\n"
        f"{transcript_list_md}"
    )

    marker_line = f"{MARKER_PREFIX} | checkpoint={target_cp}{lineage_tag}]"
    sections = [marker_line, header_note]
    layer_chars: dict[str, int] = {
        "header": len(marker_line) + len(header_note) + 2,
        "timeline": 0,
        "dialogue": 0,
        "skills_specs": 0,
        "files": 0,
    }
    if all_user_block:
        sec1 = f"{sec1_hdr}\n{all_user_block}"
        sections.append(sec1)
        layer_chars["timeline"] = len(sec1)
    if kept_turns:
        sec2 = (
            f"## 2. Recent Pre-Compaction Verbatim Dialogue ({len(kept_turns)} entries kept verbatim)\n"
            "*(Note: The messages below are the actual pre-compaction turns kept verbatim.)*\n\n"
            + "\n\n".join(kept_turns)
        )
        sections.append(sec2)
        layer_chars["dialogue"] = len(sec2)
    sec3_parts: list[str] = []
    if repo_rule_entries:
        sec3_parts.append(
            "### Touched Repo Rule Files (Paths Only)\n"
            "*(AGY only auto-loads rules from cwd upward; read the relevant file below when working in that repo.)*\n"
            + "\n".join(f"- `{rpath}` (repo touched @ step {rstep})" for rpath, rstep in repo_rule_entries)
        )
    sec3_parts.extend(skill_blocks)
    if sec3_parts:
        sec3 = "## 3. Restored Active Skills, Spec Docs & Repo Rule Pointers\n" + "\n\n".join(sec3_parts)
        sections.append(sec3)
        layer_chars["skills_specs"] = len(sec3)
    if file_blocks:
        sec4 = "## 4. Restored Active Workspace Files (Live Disk Read)\n" + "\n\n".join(file_blocks)
        sections.append(sec4)
        layer_chars["files"] = len(sec4)

    if len(sections) <= 2:
        _set_status("empty", target_cp)
        return None

    full_msg = "\n\n".join(sections)
    chunks = split_into_inject_chunks(sections, max_bytes=MAX_INJECT_STEP_BYTES, checkpoint=target_cp)
    _set_status("injected", target_cp)
    return full_msg, {
        "checkpoint": target_cp,
        "prev_checkpoint": prev_cp,
        "simulated": simulated,
        "fork_hops": len(resolved_ancestors),
        "fork_ancestors": resolved_ancestors,
        "max_seen_step": max_seen_step,
        "user_msgs": real_user_count,
        "kept_user_msgs": kept_user_count,
        "filtered_short_msgs": filtered_short_count,
        "folded_paste_msgs": folded_paste_count,
        "kept_turns": len(kept_turns),
        "repo_rules": len(repo_rule_entries),
        "skills": len(skill_blocks),
        "skill_paths": restored_skill_paths,
        "files": len(file_blocks),
        "file_paths": restored_file_paths,
        "layer_chars": layer_chars,
        "chars": len(full_msg),
        "utf8_bytes": len(full_msg.encode("utf-8")),
        "inject_chunks": len(chunks),
        "chunks": chunks,
        "est_tokens": estimate_tokens(full_msg),
    }
