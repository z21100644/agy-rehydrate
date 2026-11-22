#!/usr/bin/env python3
"""Comprehensive unit and integration tests for `agy-rehydrate`."""

from __future__ import annotations

from contextlib import contextmanager
import io
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

sys.dont_write_bytecode = True

_REPO_ROOT = Path(__file__).resolve().parent.parent
PLUGIN_ROOT = (
    _REPO_ROOT / "plugins" / "agy-rehydrate"
    if (_REPO_ROOT / "plugins" / "agy-rehydrate" / "plugin.json").is_file()
    else _REPO_ROOT
)
SCRIPTS_DIR = PLUGIN_ROOT / "scripts"
HOOK_SCRIPT = SCRIPTS_DIR / "rehydrate_after_compact.py"
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

import rehydrate_after_compact as rh_entry
import rehydrate_cli as cli
import rehydrate_core as core


def _write_jsonl(path: Path, records: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8", newline="\n") as f:
        for rec in records:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")


_CM_FENCE_RE = re.compile(r"^ {0,3}(`{3,}|~{3,})(.*)$")


def _commonmark_fences_balanced(text: str) -> bool:
    """Independent CommonMark fenced-code-block balance checker."""
    active: tuple[str, int] | None = None
    for line in text.split("\n"):
        m = _CM_FENCE_RE.match(line)
        if not m:
            continue
        marker, rest = m.groups()
        ch, length = marker[0], len(marker)
        if active is None:
            if ch == "`" and "`" in rest:
                continue
            active = (ch, length)
        else:
            a_ch, a_len = active
            if ch == a_ch and length >= a_len and not rest.strip():
                active = None
    return active is None


@contextmanager
def _symlink_or_mock_resolve(link_path: Path, target_path: Path):
    """Create a real symlink when OS permits, else mock `Path.resolve` for `link_path -> target_path`."""
    try:
        os.symlink(target_path, link_path)
        created_real = True
    except (OSError, NotImplementedError):
        created_real = False

    if created_real:
        try:
            yield
        finally:
            try:
                link_path.unlink()
            except OSError:
                pass
        return

    link_key = os.path.normcase(os.path.normpath(str(link_path)))
    orig_resolve = Path.resolve
    resolved_target = orig_resolve(target_path)

    def _patched_resolve(self: Path, strict: bool = False) -> Path:
        if os.path.normcase(os.path.normpath(str(self))) == link_key:
            return resolved_target
        return orig_resolve(self, strict=strict)

    with mock.patch.object(Path, "resolve", _patched_resolve):
        yield


class RehydrateTests(unittest.TestCase):
    def test_fast_path_skips_heavy_imports_under_python_S_B(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            logs = Path(td) / ".system_generated" / "logs"
            compact = logs / "transcript.jsonl"
            _write_jsonl(
                compact,
                [
                    {"step_index": 0, "type": "USER_INPUT", "content": "<USER_REQUEST>\nhi\n</USER_REQUEST>"},
                    {"step_index": 1, "type": "PLANNER_RESPONSE", "content": "hello"},
                ],
            )
            payload = json.dumps({"transcriptPath": str(compact), "cwd": td})
            probe = (
                "import io, json, sys; "
                f"sys.path.insert(0, {str(SCRIPTS_DIR)!r}); "
                "import rehydrate_after_compact as m; "
                f"sys.stdin = io.StringIO({payload!r}); "
                "buf = io.StringIO(); "
                "orig = sys.stdout; sys.stdout = buf; "
                "rc = m.run_hook(); "
                "sys.stdout = orig; "
                "heavy = [k for k in ('pathlib','datetime','unicodedata','argparse','rehydrate_core','rehydrate_cli') if k in sys.modules]; "
                "print(json.dumps({'rc': rc, 'out': buf.getvalue().strip(), 'heavy': heavy}))"
            )
            res = subprocess.run(
                [sys.executable, "-S", "-B", "-c", probe],
                capture_output=True,
                text=True,
                check=True,
            )
            info = json.loads(res.stdout.strip())
            self.assertEqual(info["rc"], 0)
            self.assertEqual(info["out"], "{}")
            self.assertEqual(info["heavy"], [])

    def test_disabled_config_advances_state_to_keep_fast_path(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            ws = root / "ws"
            (ws / ".agents").mkdir(parents=True)
            (ws / ".agents" / "rehydrate.json").write_text('{"enabled": false}\n', encoding="utf-8")
            logs = root / "brain" / "c-disabled" / ".system_generated" / "logs"
            compact = logs / "transcript.jsonl"
            records = [
                {"step_index": 0, "type": "USER_INPUT", "content": "<USER_REQUEST>\nhello\n</USER_REQUEST>"},
                {"step_index": 1, "type": "CHECKPOINT", "content": "<summary>s</summary>"},
            ]
            _write_jsonl(compact, records)
            payload = json.dumps({"transcriptPath": str(compact), "workspacePaths": [str(ws)], "cwd": str(ws)})

            # First call sees checkpoint -> checks config (disabled) -> returns {} and advances state
            res1 = subprocess.run(
                [sys.executable, "-S", "-B", str(HOOK_SCRIPT), "--hook"],
                input=payload,
                capture_output=True,
                text=True,
                check=True,
            )
            self.assertEqual(res1.stdout.strip(), "{}")
            state_path = logs / "rehydrate_state.json"
            self.assertTrue(state_path.is_file())
            st1 = json.loads(state_path.read_text(encoding="utf-8"))
            self.assertEqual(st1["last_rehydrated_checkpoint"], 1)
            self.assertEqual(st1.get("last_status"), "disabled")

            # Second call with a normal appended step must stay on fast-path without importing rehydrate_core
            records.append({"step_index": 2, "type": "USER_INPUT", "content": "<USER_REQUEST>\nnext\n</USER_REQUEST>"})
            _write_jsonl(compact, records)
            probe = (
                "import io, json, sys; "
                f"sys.path.insert(0, {str(SCRIPTS_DIR)!r}); "
                "import rehydrate_after_compact as m; "
                f"sys.stdin = io.StringIO({payload!r}); "
                "buf = io.StringIO(); orig = sys.stdout; sys.stdout = buf; "
                "m.run_hook(); sys.stdout = orig; "
                "print(json.dumps({'out': buf.getvalue().strip(), 'loaded_core': 'rehydrate_core' in sys.modules}))"
            )
            res2 = subprocess.run([sys.executable, "-S", "-B", "-c", probe], capture_output=True, text=True, check=True)
            info = json.loads(res2.stdout.strip())
            self.assertEqual(info["out"], "{}")
            self.assertFalse(info["loaded_core"])

    def test_hook_fail_open_on_bad_stdin(self) -> None:
        for bad in ("", "   ", "{not json", "[]", json.dumps({"conversationId": "nonexistent-id-xyz"})):
            res = subprocess.run(
                [sys.executable, "-S", "-B", str(HOOK_SCRIPT), "--hook"],
                input=bad,
                capture_output=True,
                text=True,
                check=True,
            )
            self.assertEqual(res.stdout.strip(), "{}")

    def test_full_4_layer_hook_trigger_idempotency_and_non_git_rules(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            ws = root / "workspace"
            sub = ws / "pkg"
            sub.mkdir(parents=True)
            # Intentionally NO .git directory to verify non-git AGENTS.md discovery
            (ws / "AGENTS.md").write_text("# Workspace rules\n", encoding="utf-8")
            src_mod = sub / "app.py"
            src_mod.write_text("def greet():\n    return 'hello world'\n", encoding="utf-8")
            src_ref = sub / "utils.py"
            src_ref.write_text("VALUE = 42\n", encoding="utf-8")
            spec_doc = ws / "DESIGN_SPEC.md"
            spec_doc.write_text("# Spec\n````md\n```python\nx = 1\n```\n````\n", encoding="utf-8")
            secret_env = ws / ".env"
            secret_env.write_text("API_KEY=SUPER_SECRET_123\n", encoding="utf-8")
            outside = root / "outside.txt"
            outside.write_text("OUTSIDE_SECRET\n", encoding="utf-8")
            # Outside AGENTS.md must NOT be discovered beyond workspace boundary
            (root / "GEMINI.md").write_text("# Outside rule\n", encoding="utf-8")

            conv = root / "brain" / "conv-1111"
            scratch = conv / "scratch"
            scratch.mkdir(parents=True)
            scratch_file = scratch / "temp.py"
            scratch_file.write_text("TEMP_SCRATCH = 1\n", encoding="utf-8")

            logs = conv / ".system_generated" / "logs"
            compact = logs / "transcript.jsonl"
            full = logs / "transcript_full.jsonl"
            records = [
                {"step_index": 0, "type": "USER_INPUT", "content": "<USER_REQUEST>\nRefactor app\n</USER_REQUEST>"},
                {
                    "step_index": 1,
                    "type": "PLANNER_RESPONSE",
                    "content": "",
                    "tool_calls": [
                        {"name": "read_file", "args": {"Path": str(spec_doc)}},
                        {"name": "read_file", "args": {"Path": str(src_ref)}},
                        {"name": "read_file", "args": {"Path": str(secret_env)}},
                        {"name": "read_file", "args": {"Path": str(outside)}},
                        {"name": "write_file", "args": {"Path": str(scratch_file)}},
                        {"name": "edit_file", "args": {"Path": str(src_mod)}},
                    ],
                },
                {"step_index": 2, "type": "PLANNER_RESPONSE", "content": "Done editing app.py."},
                {"step_index": 3, "type": "USER_INPUT", "content": "<USER_REQUEST>\n/compact\n</USER_REQUEST>"},
                {"step_index": 4, "type": "CHECKPOINT", "content": "<summary>short summary</summary>"},
            ]
            _write_jsonl(compact, records)
            _write_jsonl(full, records)

            payload = json.dumps(
                {
                    "transcriptPath": str(compact),
                    "artifactDirectoryPath": str(conv),
                    "workspacePaths": [str(ws)],
                    "cwd": str(ws),
                }
            )
            res = subprocess.run(
                [sys.executable, "-S", "-B", str(HOOK_SCRIPT), "--hook"],
                input=payload,
                capture_output=True,
                text=True,
                encoding="utf-8",
                check=True,
            )
            out = json.loads(res.stdout)
            self.assertIn("injectSteps", out)
            injected = "\n".join(s["systemMessage"]["systemMessage"] for s in out["injectSteps"])
            self.assertIn("[POST-COMPACT REHYDRATION v1 | checkpoint=4]", injected)
            self.assertIn("Refactor app", injected)
            self.assertNotIn("/compact", injected)
            self.assertIn("Done editing app.py.", injected)
            self.assertIn("AGENTS.md", injected)
            self.assertNotIn("GEMINI.md", injected)
            self.assertIn("DESIGN_SPEC.md", injected)
            self.assertIn("`````", injected)  # 5-backtick dynamic fence for 4-backtick content
            self.assertIn("def greet():", injected)
            self.assertIn("VALUE = 42", injected)
            # Security boundaries must hold
            self.assertNotIn("SUPER_SECRET_123", injected)
            self.assertNotIn("OUTSIDE_SECRET", injected)
            self.assertNotIn("TEMP_SCRATCH", injected)

            # Second invocation with no new checkpoint must return {}
            res2 = subprocess.run(
                [sys.executable, "-S", "-B", str(HOOK_SCRIPT), "--hook"],
                input=payload,
                capture_output=True,
                text=True,
                encoding="utf-8",
                check=True,
            )
            self.assertEqual(res2.stdout.strip(), "{}")

            # Double idempotency: delete state file after systemMessage is logged -> still {}
            (logs / "rehydrate_state.json").unlink()
            records.append(
                {
                    "step_index": 5,
                    "type": "SYSTEM_MESSAGE",
                    "content": "[POST-COMPACT REHYDRATION v1 | checkpoint=4]\n...",
                }
            )
            _write_jsonl(compact, records)
            _write_jsonl(full, records)
            res3 = subprocess.run(
                [sys.executable, "-S", "-B", str(HOOK_SCRIPT), "--hook"],
                input=payload,
                capture_output=True,
                text=True,
                encoding="utf-8",
                check=True,
            )
            self.assertEqual(res3.stdout.strip(), "{}")

    def test_hook_fail_closed_when_workspace_empty(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            f_out = root / "secret_note.txt"
            f_out.write_text("MUST_NOT_LEAK_WITHOUT_WS\n", encoding="utf-8")
            logs = root / "brain" / "conv-2" / ".system_generated" / "logs"
            compact = logs / "transcript.jsonl"
            full = logs / "transcript_full.jsonl"
            records = [
                {"step_index": 0, "type": "USER_INPUT", "content": "<USER_REQUEST>\nread note\n</USER_REQUEST>"},
                {
                    "step_index": 1,
                    "type": "PLANNER_RESPONSE",
                    "content": "read it",
                    "tool_calls": [{"name": "read_file", "args": {"Path": str(f_out)}}],
                },
                {"step_index": 2, "type": "CHECKPOINT", "content": "<summary>s</summary>"},
            ]
            _write_jsonl(compact, records)
            _write_jsonl(full, records)
            res = subprocess.run(
                [sys.executable, "-S", "-B", str(HOOK_SCRIPT), "--hook"],
                input=json.dumps({"transcriptPath": str(compact)}),
                capture_output=True,
                text=True,
                encoding="utf-8",
                check=True,
            )
            out = json.loads(res.stdout)
            injected = "\n".join(s["systemMessage"]["systemMessage"] for s in out["injectSteps"])
            self.assertNotIn("MUST_NOT_LEAK_WITHOUT_WS", injected)

    def test_multi_checkpoint_file_scope_prevents_stale_files(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            ws = root / "ws"
            ws.mkdir()
            old_file = ws / "abandoned_v1.py"
            old_file.write_text("OLD_ABANDONED_MARKER = 1\n", encoding="utf-8")
            new_file = ws / "active_v2.py"
            new_file.write_text("NEW_ACTIVE_MARKER = 2\n", encoding="utf-8")

            full = root / "brain" / "c-multi" / ".system_generated" / "logs" / "transcript_full.jsonl"
            _write_jsonl(
                full,
                [
                    {"step_index": 0, "type": "USER_INPUT", "content": "<USER_REQUEST>\nPhase 1\n</USER_REQUEST>"},
                    {
                        "step_index": 1,
                        "type": "PLANNER_RESPONSE",
                        "content": "Edited old file",
                        "tool_calls": [{"name": "edit_file", "args": {"Path": str(old_file)}}],
                    },
                    {"step_index": 2, "type": "CHECKPOINT", "content": "<summary>cp1</summary>"},
                    {
                        "step_index": 3,
                        "type": "SYSTEM_MESSAGE",
                        "content": "[POST-COMPACT REHYDRATION v1 | checkpoint=2]\n...",
                    },
                    {"step_index": 4, "type": "USER_INPUT", "content": "<USER_REQUEST>\nPhase 2\n</USER_REQUEST>"},
                    {
                        "step_index": 5,
                        "type": "PLANNER_RESPONSE",
                        "content": "Discussing phase 2 without touching workspace files.",
                    },
                    {"step_index": 6, "type": "CHECKPOINT", "content": "<summary>cp2</summary>"},
                ],
            )
            # Default: fileScopeSincePrevCheckpoint=True -> Phase 1 abandoned file must NOT be re-injected at cp 6
            res_scoped = core.build_rehydration(full, up_to_checkpoint_step=6, workspace_roots=[ws])
            self.assertIsNotNone(res_scoped)
            msg_scoped, stats_scoped = res_scoped
            self.assertEqual(stats_scoped["prev_checkpoint"], 2)
            self.assertEqual(stats_scoped["files"], 0)
            self.assertNotIn("OLD_ABANDONED_MARKER", msg_scoped)

            # Opt-out: fileScopeSincePrevCheckpoint=False -> restores historical workspace file
            cfg_all = core.RehydrateConfig(file_scope_since_prev_checkpoint=False)
            res_all = core.build_rehydration(full, up_to_checkpoint_step=6, config=cfg_all, workspace_roots=[ws])
            self.assertIsNotNone(res_all)
            msg_all, stats_all = res_all
            self.assertEqual(stats_all["files"], 1)
            self.assertIn("OLD_ABANDONED_MARKER", msg_all)

            # When Phase 2 DOES modify a new file, only the new file is restored
            with open(full, "a", encoding="utf-8") as f:
                f.write(
                    json.dumps(
                        {
                            "step_index": 7,
                            "type": "PLANNER_RESPONSE",
                            "content": "Edited active_v2.py",
                            "tool_calls": [{"name": "edit_file", "args": {"Path": str(new_file)}}],
                        }
                    )
                    + "\n"
                )
                f.write(json.dumps({"step_index": 8, "type": "CHECKPOINT", "content": "<summary>cp3</summary>"}) + "\n")
            res_cp3 = core.build_rehydration(full, up_to_checkpoint_step=8, workspace_roots=[ws])
            self.assertIsNotNone(res_cp3)
            msg_cp3, stats_cp3 = res_cp3
            self.assertEqual(stats_cp3["prev_checkpoint"], 6)
            self.assertEqual(stats_cp3["files"], 1)
            self.assertIn("NEW_ACTIVE_MARKER", msg_cp3)
            self.assertNotIn("OLD_ABANDONED_MARKER", msg_cp3)

    def test_config_security_global_disable_extra_allowed_and_caps(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            ws = root / "ws"
            extra = root / "trusted_plugin"
            untrusted_outside = root / "untrusted_outside"
            ws.mkdir()
            extra.mkdir()
            untrusted_outside.mkdir()
            (ws / ".agents").mkdir()

            global_cfg = root / "global_rehydrate.json"
            ws_cfg = ws / ".agents" / "rehydrate.json"

            # 1) Global enabled=false cannot be re-enabled by workspace; workspace extraAllowedPaths is ignored
            global_cfg.write_text(
                json.dumps({"enabled": False, "extraAllowedPaths": [str(extra)]}),
                encoding="utf-8",
            )
            ws_cfg.write_text(
                json.dumps(
                    {
                        "enabled": True,
                        "extraAllowedPaths": [str(untrusted_outside)],
                        "budgetRecentTurnsChars": 99_999_999,
                        "maxModifiedFiles": 999,
                    }
                ),
                encoding="utf-8",
            )
            cfg = core.load_rehydrate_config(ws, global_config_path=global_cfg)
            self.assertFalse(cfg.enabled)
            self.assertEqual(cfg.budget_recent_turns_chars, core.MAX_CONFIG_BUDGET_CHARS)
            self.assertEqual(cfg.max_modified_files, core.MAX_CONFIG_FILE_SLOTS)
            self.assertEqual(len(cfg.extra_allowed_paths), 1)
            self.assertEqual(Path(cfg.extra_allowed_paths[0]).resolve(), extra.resolve())

            # 2) Global extraAllowedPaths allows Layer 4 read outside workspace while still blocking .env
            global_cfg.write_text(json.dumps({"enabled": True, "extraAllowedPaths": [str(extra)]}), encoding="utf-8")
            ws_cfg.unlink()
            cfg2 = core.load_rehydrate_config(ws, global_config_path=global_cfg)
            self.assertTrue(cfg2.enabled)
            plugin_py = extra / "plugin_code.py"
            plugin_py.write_text("TRUSTED_PLUGIN_CODE = True\n", encoding="utf-8")
            plugin_env = extra / ".env"
            plugin_env.write_text("EXTRA_SECRET_ENV = 1\n", encoding="utf-8")
            full = root / "brain" / "c-extra" / ".system_generated" / "logs" / "transcript_full.jsonl"
            _write_jsonl(
                full,
                [
                    {"step_index": 0, "type": "USER_INPUT", "content": "<USER_REQUEST>\nedit plugin\n</USER_REQUEST>"},
                    {
                        "step_index": 1,
                        "type": "PLANNER_RESPONSE",
                        "content": "edited",
                        "tool_calls": [
                            {"name": "edit_file", "args": {"Path": str(plugin_py)}},
                            {"name": "read_file", "args": {"Path": str(plugin_env)}},
                        ],
                    },
                    {"step_index": 2, "type": "CHECKPOINT", "content": "<summary>s</summary>"},
                ],
            )
            res = core.build_rehydration(full, up_to_checkpoint_step=2, config=cfg2, workspace_roots=[ws])
            self.assertIsNotNone(res)
            msg, _ = res
            self.assertIn("TRUSTED_PLUGIN_CODE", msg)
            self.assertNotIn("EXTRA_SECRET_ENV", msg)

    def test_fork_lineage_and_branch_isolation(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            brain = Path(td) / "brain"
            parent_logs = brain / "aaaa1111" / ".system_generated" / "logs"
            child_logs = brain / "bbbb2222" / ".system_generated" / "logs"
            parent_full = parent_logs / "transcript_full.jsonl"
            child_full = child_logs / "transcript_full.jsonl"

            _write_jsonl(
                parent_full,
                [
                    {
                        "step_index": 0,
                        "type": "USER_INPUT",
                        "created_at": "2026-11-22T10:00:00Z",
                        "content": "<USER_REQUEST>\nRoot constraint alpha\n</USER_REQUEST>",
                    },
                    {
                        "step_index": 1,
                        "type": "PLANNER_RESPONSE",
                        "created_at": "2026-11-22T10:00:10Z",
                        "content": "Acknowledged alpha.",
                    },
                    {
                        "step_index": 2,
                        "type": "USER_INPUT",
                        "created_at": "2026-11-22T10:05:00Z",
                        "content": "<USER_REQUEST>\nPOST_FORK_PARENT_SECRET_MUST_NOT_LEAK\n</USER_REQUEST>",
                    },
                ],
            )
            _write_jsonl(
                child_full,
                [
                    {
                        "step_index": 2,
                        "type": "USER_INPUT",
                        "created_at": "2026-11-22T10:01:00Z",
                        "content": "<USER_REQUEST>\nChild branch work beta\n</USER_REQUEST>",
                    },
                    {
                        "step_index": 3,
                        "type": "PLANNER_RESPONSE",
                        "created_at": "2026-11-22T10:01:10Z",
                        "content": "Working on beta.",
                    },
                    {"step_index": 4, "type": "CHECKPOINT", "content": "<summary>s</summary>"},
                ],
            )
            anc = core.resolve_fork_ancestors(child_full)
            self.assertEqual(len(anc), 1)
            self.assertEqual(anc[0]["cid"], "aaaa1111")
            self.assertEqual((anc[0]["min_step"], anc[0]["max_step"]), (0, 1))

            res = core.build_rehydration(child_full, ancestors=anc, workspace_roots=[])
            self.assertIsNotNone(res)
            msg, stats = res
            self.assertEqual(stats["fork_hops"], 1)
            self.assertIn("Root constraint alpha", msg)
            self.assertIn("Child branch work beta", msg)
            self.assertIn("[/fork branch aaaa1111 -> bbbb2222 @ step 2]", msg)
            self.assertNotIn("POST_FORK_PARENT_SECRET_MUST_NOT_LEAK", msg)

    def test_short_cjk_filter_paste_fold_and_turn_atomic_chunking(self) -> None:
        # Short CJK denoising
        self.assertTrue(core.is_low_info_short_turn("好的！"))
        self.assertTrue(core.is_low_info_short_turn("繼續吧"))
        self.assertFalse(core.is_low_info_short_turn("修2處"))
        self.assertFalse(core.is_low_info_short_turn("/plan"))
        self.assertFalse(core.is_low_info_short_turn("請保留完整日誌"))

        # Older paste folding + >75 short filter
        msgs = [(0, "立項初始約束", None, 1)]
        for i in range(1, 80):
            txt = "好的" if i == 2 else ("x" * 1200 if i == 3 else f"prompt {i}")
            msgs.append((i, txt, Path("transcript_full.jsonl"), i + 1))
        block, total, kept, filtered, folded = core.format_user_timeline(msgs)
        self.assertEqual(total, 80)
        self.assertEqual(filtered, 1)
        self.assertEqual(folded, 1)
        self.assertEqual(kept, 79)
        self.assertIn("folded 700 chars", block)

        # Turn-atomic 48 KiB chunking: a single turn containing '### ' headings must NOT be split mid-turn
        turn_with_headings = (
            "[Step 5 | Assistant]:\n"
            + "### Heading A\n"
            + ("中" * 1500)
            + "\n\n### Heading B\n"
            + ("文" * 1500)
        )
        huge_sec = "## 2. Recent Pre-Compaction Verbatim Dialogue\n\n" + "\n\n".join(
            [f"[Step {i} | Assistant]:\n" + ("漢" * 4000) for i in range(4)] + [turn_with_headings]
        )
        chunks = core.split_into_inject_chunks(
            ["[POST-COMPACT REHYDRATION v1 | checkpoint=9]", "hdr", huge_sec],
            checkpoint=9,
        )
        self.assertGreater(len(chunks), 1)
        for ch in chunks:
            self.assertLessEqual(len(ch.encode("utf-8")), core.MAX_INJECT_STEP_BYTES)
        self.assertTrue(
            any(("### Heading A" in ch and "### Heading B" in ch) for ch in chunks),
            "A single assistant turn containing ### headings was erroneously split across chunks",
        )

    def test_bilingual_token_estimator_and_dynamic_fencing(self) -> None:
        self.assertEqual(core._fence_for("plain text"), "```")
        self.assertEqual(core._fence_for("```python\nx=1\n```"), "````")
        self.assertEqual(core._fence_for("````carousel\n```py\n````"), "`````")
        en_tok = core.estimate_tokens("hello world " * 100)  # 1200 ASCII chars -> ~336 tok
        zh_tok = core.estimate_tokens("壓縮效果評估" * 100)  # 600 CJK chars -> ~690 tok
        self.assertGreater(zh_tok, en_tok)
        self.assertGreater(zh_tok, 600)

    def test_pep562_compat_and_cli_subcommands(self) -> None:
        self.assertTrue(callable(rh_entry.resolve_fork_ancestors))
        self.assertTrue(callable(rh_entry.build_rehydration))

        with tempfile.TemporaryDirectory() as td:
            res = subprocess.run(
                [sys.executable, "-B", str(HOOK_SCRIPT), "--cwd", td, "show-config"],
                capture_output=True,
                text=True,
                encoding="utf-8",
                check=True,
            )
            cfg_json = json.loads(res.stdout)
            self.assertEqual(cfg_json["budgetRecentTurnsChars"], 28000)
            self.assertEqual(cfg_json["budgetFilesTotalChars"], 28000)
            self.assertEqual(cfg_json["budgetSingleFileChars"], 8000)
            self.assertEqual(cfg_json["maxModifiedFiles"], 5)
            self.assertEqual(cfg_json["maxReferencedFiles"], 2)
            self.assertTrue(cfg_json["fileScopeSincePrevCheckpoint"])
            self.assertEqual(cfg_json["extraAllowedPaths"], [])

    # -------------------------------------------------------------------------
    # Core regression tests (#5, #7, #11)
    # -------------------------------------------------------------------------

    def test_posix_temp_path_scoping_and_system_blocks(self) -> None:
        # Scoped POSIX /tmp/ and /dev/shm/ workspaces are allowed; unscoped or /etc/, /dev/null stay blocked
        self.assertFalse(
            core._is_blocked_system_or_temp_path("/tmp/pytest_ws/app.py", "/tmp/pytest_ws/app.py/", ("/tmp/pytest_ws",))
        )
        self.assertFalse(
            core._is_blocked_system_or_temp_path("/dev/shm/ws/app.py", "/dev/shm/ws/app.py/", ("/dev/shm/ws",))
        )
        self.assertTrue(core._is_blocked_system_or_temp_path("/tmp/pytest_ws/app.py", "/tmp/pytest_ws/app.py/", None))
        self.assertTrue(
            core._is_blocked_system_or_temp_path("/tmp/other/app.py", "/tmp/other/app.py/", ("/home/user/ws",))
        )
        self.assertTrue(core._is_blocked_system_or_temp_path("/etc/passwd", "/etc/passwd/", ("/etc",)))
        self.assertTrue(core._is_blocked_system_or_temp_path("/dev/null", "/dev/null/", ("/dev/shm/ws",)))
        self.assertTrue(core._is_blocked_system_or_temp_path("/dev/null", "/dev/null/", ("/dev",)))

        # End-to-end temp dir eligibility & expanded sensitive basenames
        with tempfile.TemporaryDirectory() as td:
            ws = Path(td)
            ok_file = ws / "app.py"
            ok_file.write_text("x = 1\n", encoding="utf-8")
            ws_pref = core._prepare_prefixes([ws])
            self.assertTrue(core.is_eligible_text_file(str(ok_file), allowed_prefixes=ws_pref))
            self.assertFalse(core.is_eligible_text_file(str(ok_file), allowed_prefixes=None))

            for secret_name in (".envrc", ".env~", ".git-credentials"):
                sf = ws / secret_name
                sf.write_text("SECRET=1\n", encoding="utf-8")
                self.assertFalse(core.is_eligible_text_file(str(sf), allowed_prefixes=ws_pref), secret_name)

            aws_cred = ws / ".aws" / "credentials"
            aws_cred.parent.mkdir(parents=True)
            aws_cred.write_text("[default]\naws_secret_access_key=abc\n", encoding="utf-8")
            self.assertFalse(core.is_eligible_text_file(str(aws_cred), allowed_prefixes=ws_pref))

    def test_symlink_skill_scope_and_reverse_env_symlink_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            ws = root / "ws"
            ws.mkdir()
            fake_home = root / "home"
            token_store = fake_home / ".gemini" / "config" / "plugins" / "secret_plugin" / "token_store.json"
            token_store.parent.mkdir(parents=True)
            token_store.write_text('{"token": "SUPER_SECRET_PLUGIN_TOKEN"}\n', encoding="utf-8")

            prod_cfg = ws / "prod_cfg.txt"
            prod_cfg.write_text("REVERSE_ENV_SYMLINK_SECRET=42\n", encoding="utf-8")

            fake_skill_link = ws / "SKILL.md"
            reverse_env_link = ws / ".env"
            full = root / "brain" / "c-sym" / ".system_generated" / "logs" / "transcript_full.jsonl"
            _write_jsonl(
                full,
                [
                    {"step_index": 0, "type": "USER_INPUT", "content": "<USER_REQUEST>\ncheck files\n</USER_REQUEST>"},
                    {
                        "step_index": 1,
                        "type": "PLANNER_RESPONSE",
                        "content": "checked",
                        "tool_calls": [
                            {"name": "read_file", "args": {"Path": str(fake_skill_link)}},
                            {"name": "read_file", "args": {"Path": str(reverse_env_link)}},
                        ],
                    },
                    {"step_index": 2, "type": "CHECKPOINT", "content": "<summary>s</summary>"},
                ],
            )

            with (
                mock.patch.object(Path, "home", return_value=fake_home),
                _symlink_or_mock_resolve(fake_skill_link, token_store),
                _symlink_or_mock_resolve(reverse_env_link, prod_cfg),
            ):
                ws_pref = core._prepare_prefixes([ws])
                skill_pref = core._prepare_prefixes([ws, *core.default_custom_skill_roots(None)])
                self.assertIsNone(
                    core._resolve_eligible_file(
                        str(fake_skill_link),
                        allowed_prefixes=skill_pref,
                        require_resolved_basename="SKILL.MD",
                    )
                )
                self.assertFalse(core.is_eligible_text_file(str(reverse_env_link), allowed_prefixes=ws_pref))

                res = core.build_rehydration(full, up_to_checkpoint_step=2, workspace_roots=[ws])
                self.assertIsNotNone(res)
                msg, stats = res
                self.assertEqual(stats["skills"], 0)
                self.assertEqual(stats["files"], 0)
                self.assertNotIn("SUPER_SECRET_PLUGIN_TOKEN", msg)
                self.assertNotIn("REVERSE_ENV_SYMLINK_SECRET", msg)

    def test_workspace_roots_unc_and_extended_win_paths(self) -> None:
        # Extended-length local Windows drive accepted; UNC, device paths, and non-ASCII drive rejected
        self.assertEqual(
            core.normalize_local_path(r"\\?\D:\antigravity\a.py"),
            os.path.normpath(r"D:\antigravity\a.py"),
        )
        for blocked in (
            r"\\?\UNC\attacker.invalid\share\a.py",
            r"\\.\PhysicalDrive0",
            r"\\attacker.invalid\share",
            "//attacker.invalid/share",
            "file://attacker.invalid/share",
            "Ä:\\bad\\path.py",
        ):
            self.assertEqual(core.normalize_local_path(blocked), "", blocked)

        # resolve_workspace_roots_from_payload must NEVER call Path.resolve on rejected UNC/remote URIs
        seen_resolve_inputs: list[str] = []
        orig_resolve = Path.resolve

        def spy_resolve(self: Path, strict: bool = False) -> Path:
            seen_resolve_inputs.append(str(self))
            return orig_resolve(self, strict=strict)

        payload = {
            "workspacePaths": [
                r"\\attacker.invalid\share",
                "//attacker.invalid/share",
                "file://attacker.invalid/share",
            ],
            "cwd": r"\\attacker.invalid\cwd",
        }
        with mock.patch.object(Path, "resolve", spy_resolve):
            roots = core.resolve_workspace_roots_from_payload(payload)
        self.assertEqual(roots, [])
        self.assertEqual(seen_resolve_inputs, [])

    def test_fork_rejects_old_unrelated_exact_tip_and_inherits_ancestor_prev_cp(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            ws = root / "ws"
            ws.mkdir()
            pre_cp_file = ws / "pre_cp1_abandoned.py"
            pre_cp_file.write_text("PRE_CP_ABANDONED_ANCESTOR = 1\n", encoding="utf-8")
            post_cp_file = ws / "post_cp1_active.py"
            post_cp_file.write_text("POST_CP_ACTIVE_ANCESTOR = 2\n", encoding="utf-8")

            brain = root / "brain"
            # 1) Unrelated 21-day-old session ending right at step 4 (exact_tip=1, huge time_delta)
            old_full = brain / "unrelated_old" / ".system_generated" / "logs" / "transcript_full.jsonl"
            _write_jsonl(
                old_full,
                [
                    {"step_index": 0, "type": "USER_INPUT", "created_at": "2026-11-01T10:00:00Z", "content": "old0"},
                    {"step_index": 4, "type": "PLANNER_RESPONSE", "created_at": "2026-11-01T10:01:00Z", "content": "old4"},
                ],
            )
            # 2) True parent forked 2s ago at step 4, then continued to step 6 (exact_tip=0, delta=2s)
            parent_full = brain / "true_parent" / ".system_generated" / "logs" / "transcript_full.jsonl"
            _write_jsonl(
                parent_full,
                [
                    {"step_index": 0, "type": "USER_INPUT", "created_at": "2026-11-22T10:00:00Z", "content": "<USER_REQUEST>\nRoot objective\n</USER_REQUEST>"},
                    {
                        "step_index": 1,
                        "type": "PLANNER_RESPONSE",
                        "created_at": "2026-11-22T10:00:05Z",
                        "content": "edited pre_cp",
                        "tool_calls": [{"name": "edit_file", "args": {"Path": str(pre_cp_file)}}],
                    },
                    {"step_index": 2, "type": "CHECKPOINT", "created_at": "2026-11-22T10:00:10Z", "content": "<summary>cp1</summary>"},
                    {
                        "step_index": 4,
                        "type": "PLANNER_RESPONSE",
                        "created_at": "2026-11-22T10:00:20Z",
                        "content": "edited post_cp",
                        "tool_calls": [{"name": "edit_file", "args": {"Path": str(post_cp_file)}}],
                    },
                    {"step_index": 6, "type": "USER_INPUT", "created_at": "2026-11-22T10:02:00Z", "content": "parent continued"},
                ],
            )
            # 3) Child forked at step 5 (2 seconds after true_parent step 4)
            child_full = brain / "child_branch" / ".system_generated" / "logs" / "transcript_full.jsonl"
            _write_jsonl(
                child_full,
                [
                    {"step_index": 5, "type": "USER_INPUT", "created_at": "2026-11-22T10:00:22Z", "content": "<USER_REQUEST>\nChild task\n</USER_REQUEST>"},
                    {"step_index": 6, "type": "PLANNER_RESPONSE", "created_at": "2026-11-22T10:00:25Z", "content": "Child reply"},
                    {"step_index": 7, "type": "CHECKPOINT", "created_at": "2026-11-22T10:00:30Z", "content": "<summary>cp2</summary>"},
                ],
            )

            # True parent must win over 21-day-old unrelated exact-tip session
            anc = core.resolve_fork_ancestors(child_full)
            self.assertEqual(len(anc), 1)
            self.assertEqual(anc[0]["cid"], "true_parent")

            # build_rehydration must inherit prev_checkpoint=2 from ancestor and exclude pre_cp1_abandoned.py
            res = core.build_rehydration(child_full, ancestors=anc, workspace_roots=[ws])
            self.assertIsNotNone(res)
            msg, stats = res
            self.assertEqual(stats["prev_checkpoint"], 2)
            self.assertIn("POST_CP_ACTIVE_ANCESTOR", msg)
            self.assertNotIn("PRE_CP_ABANDONED_ANCESTOR", msg)

            # If true_parent is removed, 21-day-old unrelated exact-tip session must NOT be stitched
            parent_full.unlink()
            self.assertEqual(core.resolve_fork_ancestors(child_full), [])

            # 0-byte sibling transcript.jsonl must fall back to valid transcript_full.jsonl during fork discovery
            zero_parent_logs = brain / "zero_compact_parent" / ".system_generated" / "logs"
            _write_jsonl(
                zero_parent_logs / "transcript_full.jsonl",
                [
                    {"step_index": 0, "type": "USER_INPUT", "created_at": "2026-11-22T10:00:00Z", "content": "root"},
                    {"step_index": 4, "type": "PLANNER_RESPONSE", "created_at": "2026-11-22T10:00:20Z", "content": "step4"},
                ],
            )
            (zero_parent_logs / "transcript.jsonl").write_bytes(b"")
            anc_fb = core.resolve_fork_ancestors(child_full)
            self.assertEqual(len(anc_fb), 1)
            self.assertEqual(anc_fb[0]["cid"], "zero_compact_parent")

    def test_fence_safe_clipping_folding_and_oversized_chunking(self) -> None:
        # 1) clip_turn_text on 3-backtick, 4-backtick, and tilde fenced turns
        for opener, closer in (("```python", "```"), ("````markdown", "````"), ("~~~yaml", "~~~")):
            body = "\n".join(f"line_{i} = 'payload_{i}'" for i in range(400))
            turn = f"[Step 10 | Assistant]:\n{opener}\n{body}\n{closer}\nDone."
            clipped = core.clip_turn_text(turn, max_chars=2000)
            self.assertIn("turn clipped", clipped)
            self.assertTrue(_commonmark_fences_balanced(clipped), f"Unbalanced fence for {opener}:\n{clipped}")

        # 2) fold_user_paste on fenced paste with >200 chars after closing fence
        pasted = "Check this code:\n```python\n" + ("x = 1234567890\n" * 60) + "```\n" + ("after " * 60)
        folded, did_fold = core.fold_user_paste(pasted, Path("transcript_full.jsonl"), 12, 3)
        self.assertTrue(did_fold)
        self.assertTrue(_commonmark_fences_balanced(folded), f"Unbalanced folded paste:\n{folded}")

        # 3) Oversized >44 KB single fenced CJK block chunk splitting
        big_cjk_code = "\n".join(f"項目_{i} = '測試資料內容'" * 3 for i in range(600))
        sec4_huge = (
            "## 4. Restored Active Workspace Files (Live Disk Read)\n"
            "### [Modified @ step 5] `D:\\ws\\huge_cjk.py`\n"
            f"```\n{big_cjk_code}\n```"
        )
        chunks = core.split_into_inject_chunks(
            ["[POST-COMPACT REHYDRATION v1 | checkpoint=8]", "hdr", sec4_huge],
            max_bytes=core.MAX_INJECT_STEP_BYTES,
            checkpoint=8,
        )
        self.assertGreater(len(chunks), 1)
        for ch in chunks:
            utf8_len = len(ch.encode("utf-8"))
            self.assertLessEqual(utf8_len, core.MAX_INJECT_STEP_BYTES)
            self.assertLessEqual(utf8_len + 260, 49_152)
            self.assertTrue(_commonmark_fences_balanced(ch), "Unbalanced fence in split chunk")

        # 4) FILE_SPLIT_RE must NOT split mid-file on inner `### `func()`` markdown headings
        api_md_body = ("說明文字" * 2000) + "\n\n### `build_rehydration()`\n\n" + ("細部規格" * 2000)
        other_md_body = "其他內容" * 2500
        sec4_docs = (
            "## 4. Restored Active Workspace Files (Live Disk Read)\n"
            f"### [Modified @ step 5] `D:\\ws\\API.md`\n```\n{api_md_body}\n```\n\n"
            f"### [Modified @ step 6] `D:\\ws\\OTHER.md`\n```\n{other_md_body}\n```"
        )
        doc_chunks = core.split_into_inject_chunks(
            ["[POST-COMPACT REHYDRATION v1 | checkpoint=9]", "hdr", sec4_docs],
            max_bytes=core.MAX_INJECT_STEP_BYTES,
            checkpoint=9,
        )
        self.assertGreater(len(doc_chunks), 1)
        for ch in doc_chunks:
            self.assertLessEqual(len(ch.encode("utf-8")), core.MAX_INJECT_STEP_BYTES)
            self.assertTrue(_commonmark_fences_balanced(ch), "Inner ### `func()` heading broke outer fence")
        # Both halves of API.md should remain in the same chunk since API.md itself is < 44 KB
        self.assertTrue(
            any(("D:\\ws\\API.md" in ch and "### `build_rehydration()`" in ch) for ch in doc_chunks),
            "API.md was split across chunks at inner ### `build_rehydration()` heading",
        )

        # 5) Quoted `[Step 0 | User]:` inside a code fence in Layer 2 must NOT split mid-fence across chunks
        fenced_quote_turn = (
            "[Step 12 | Assistant]:\n```markdown\n"
            + ("前段日誌" * 2600)
            + "\n\n[Step 0 | User]:\nquoted example\n\n"
            + ("後段日誌" * 1600)
            + "\n```"
        )
        sec2_quoted = (
            "## 2. Recent Pre-Compaction Verbatim Dialogue\n\n"
            "[Step 10 | Assistant]:\n" + ("前置對話" * 3500) + "\n\n"
            + fenced_quote_turn
        )
        q_chunks = core.split_into_inject_chunks(
            ["[POST-COMPACT REHYDRATION v1 | checkpoint=12]", "hdr", sec2_quoted],
            max_bytes=core.MAX_INJECT_STEP_BYTES,
            checkpoint=12,
        )
        self.assertGreater(len(q_chunks), 1)
        for ch in q_chunks:
            self.assertLessEqual(len(ch.encode("utf-8")), core.MAX_INJECT_STEP_BYTES)
            self.assertTrue(_commonmark_fences_balanced(ch), "Quoted [Step 0 | User]: inside fence broke chunk balance")

    def test_user_timeline_budget_enforcement_and_kept_count_accuracy(self) -> None:
        # 1) total_real <= 10 (6 x 6,000-char prompts) must fold & stay within budget
        msgs_small = [(i, f"PROMPT_{i}_" + ("a" * 6000), Path("t.jsonl"), i + 1) for i in range(6)]
        blk1, tot1, kept1, _, fold1 = core.format_user_timeline(msgs_small, budget_chars=24_000)
        self.assertEqual(tot1, 6)
        self.assertLessEqual(len(blk1), 24_256)
        self.assertGreater(fold1, 0)
        rendered_bullets_1 = len(re.findall(r"^- \[#\d+ @ step \d+\]:", blk1, re.M))
        self.assertEqual(kept1, rendered_bullets_1)

        # 2) Single 30,000-char prompt must stay within budget
        msgs_one = [(0, "BIG_" + ("z" * 30_000), Path("t.jsonl"), 1)]
        blk2, tot2, kept2, _, fold2 = core.format_user_timeline(msgs_one, budget_chars=24_000)
        self.assertEqual((tot2, kept2), (1, 1))
        self.assertLessEqual(len(blk2), 24_256)
        self.assertGreaterEqual(fold2, 1)

        # 3) 10 prompts where prompt #1 is huge (25K): latest prompt #10 MUST NOT be dropped
        msgs_10 = [(0, "FIRST_" + ("h" * 25_000), Path("t.jsonl"), 1)] + [
            (i, f"SHORT_PROMPT_{i}", Path("t.jsonl"), i + 1) for i in range(1, 10)
        ]
        blk3, tot3, kept3, _, _ = core.format_user_timeline(msgs_10, budget_chars=24_000)
        self.assertEqual(tot3, 10)
        self.assertIn("SHORT_PROMPT_9", blk3)
        self.assertLessEqual(len(blk3), 24_256)
        self.assertEqual(kept3, len(re.findall(r"^- \[#\d+ @ step \d+\]:", blk3, re.M)))

        # 4) 150 prompts with middle omission: kept_count must match actual rendered bullets
        msgs_150 = [(i, f"USER_TASK_{i}_" + ("m" * 240), Path("t.jsonl"), i + 1) for i in range(150)]
        blk4, tot4, kept4, _, _ = core.format_user_timeline(msgs_150, budget_chars=12_000)
        self.assertEqual(tot4, 150)
        self.assertLess(kept4, 150)
        self.assertIn("earlier middle prompts omitted", blk4)
        self.assertIn("USER_TASK_149_", blk4)
        self.assertEqual(kept4, len(re.findall(r"^- \[#\d+ @ step \d+\]:", blk4, re.M)))

    def test_tool_trajectory_dedup_and_compact_preserves_pending_tools(self) -> None:
        # Consecutive identical tool calls collapse into `×N`
        repeated_tcs = [
            {"name": "run_command", "args": {"CommandLine": "pytest -q"}},
            {"name": "run_command", "args": {"CommandLine": "pytest -q"}},
            {"name": "run_command", "args": {"CommandLine": "pytest -q"}},
            {"name": "read_file", "args": {"Path": r"D:\ws\app.py"}},
            {"name": "read_file", "args": {"Path": r"D:\ws\app.py"}},
        ]
        summary = core.summarize_tool_calls(repeated_tcs)
        self.assertIn("run_command(`pytest -q`) ×3", summary)
        self.assertIn("read_file(app.py) ×2", summary)

        # /compact must NOT clear pending_tools, and [Pre-Compaction In-Progress Tools] keeps newest 6
        with tempfile.TemporaryDirectory() as td:
            full = Path(td) / "brain" / "c-pend" / ".system_generated" / "logs" / "transcript_full.jsonl"
            recs: list[dict] = [
                {"step_index": 0, "type": "USER_INPUT", "content": "<USER_REQUEST>\nrun tools\n</USER_REQUEST>"},
            ]
            for i in range(1, 9):
                recs.append(
                    {
                        "step_index": i,
                        "type": "PLANNER_RESPONSE",
                        "content": "",
                        "tool_calls": [{"name": f"tool_{i}", "args": {}}],
                    }
                )
            recs.append({"step_index": 9, "type": "USER_INPUT", "content": "<USER_REQUEST>\n/compact\n</USER_REQUEST>"})
            recs.append({"step_index": 10, "type": "CHECKPOINT", "content": "<summary>s</summary>"})
            _write_jsonl(full, recs)

            res = core.build_rehydration(full, up_to_checkpoint_step=10, workspace_roots=[])
            self.assertIsNotNone(res)
            msg, _ = res
            self.assertIn("[Pre-Compaction In-Progress Tools]:", msg)
            self.assertIn("tool_8", msg)
            self.assertIn("tool_3", msg)
            self.assertNotIn("tool_1", msg)

    def test_read_text_clipped_line_snapping_and_nul_rejection(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            code = root / "long_code.py"
            lines = [f"def func_{i:04d}(arg_alpha, arg_beta): return {i}" for i in range(400)]
            code.write_text("\n".join(lines) + "\n", encoding="utf-8")

            clipped = core.read_text_clipped(code, max_chars=2000)
            self.assertIsNotNone(clipped)
            assert clipped is not None
            self.assertIn("... [truncated ~", clipped)
            head, rest = clipped.split("\n... [truncated ~", 1)
            _, tail = rest.split("] ...\n", 1)
            # Head must end on a complete line (no partial `def func_...`)
            self.assertRegex(head.splitlines()[-1], r"^def func_\d{4}\(arg_alpha, arg_beta\): return \d+$")
            # Tail must start on a complete line
            self.assertRegex(tail.splitlines()[0], r"^def func_\d{4}\(arg_alpha, arg_beta\): return \d+$")

            # BOM-less UTF-16LE contains NUL bytes and must be rejected
            utf16_file = root / "utf16_nul.txt"
            utf16_file.write_bytes("HELLO WORLD\n".encode("utf-16-le"))
            self.assertIsNone(core.read_text_clipped(utf16_file, max_chars=2000))

    def test_layer4_evicted_modified_do_not_steal_referenced_and_alias_dedup(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            ws = root / "ws"
            ws.mkdir()

            refs = []
            for i in (1, 2):
                rf = ws / f"ref{i}.py"
                rf.write_text(f"REF_MARKER_{i} = {i}\n", encoding="utf-8")
                refs.append(rf)

            mods = []
            for i in range(1, 8):
                mf = ws / f"mod{i}.py"
                mf.write_text(f"MOD_MARKER_{i} = {i}\n", encoding="utf-8")
                mods.append(mf)

            alias_path = ws / "mod7_alias.py"
            recs: list[dict] = [
                {"step_index": 0, "type": "USER_INPUT", "content": "<USER_REQUEST>\nedit 7 files\n</USER_REQUEST>"},
                {
                    "step_index": 1,
                    "type": "PLANNER_RESPONSE",
                    "content": "read refs",
                    "tool_calls": [{"name": "read_file", "args": {"Path": str(rf)}} for rf in refs],
                },
            ]
            for idx, mf in enumerate(mods, start=2):
                recs.append(
                    {
                        "step_index": idx,
                        "type": "PLANNER_RESPONSE",
                        "content": "",
                        "tool_calls": [
                            {"name": "read_file", "args": {"Path": str(mf)}},
                            {"name": "edit_file", "args": {"Path": str(mf)}},
                        ],
                    }
                )
            recs.append(
                {
                    "step_index": 9,
                    "type": "PLANNER_RESPONSE",
                    "content": "done",
                    "tool_calls": [{"name": "read_file", "args": {"Path": str(alias_path)}}],
                }
            )
            recs.append({"step_index": 10, "type": "CHECKPOINT", "content": "<summary>s</summary>"})

            full = root / "brain" / "c-l4" / ".system_generated" / "logs" / "transcript_full.jsonl"
            _write_jsonl(full, recs)
            with _symlink_or_mock_resolve(alias_path, mods[-1]):
                res = core.build_rehydration(full, up_to_checkpoint_step=10, workspace_roots=[ws])
            self.assertIsNotNone(res)
            msg, stats = res
            self.assertEqual(stats["files"], 7)  # 5 Modified + 2 genuine Referenced
            for i in range(3, 8):
                self.assertIn(f"MOD_MARKER_{i}", msg)
            # Evicted mod1 & mod2 must NOT steal Referenced slots
            self.assertNotIn("MOD_MARKER_1", msg)
            self.assertNotIn("MOD_MARKER_2", msg)
            self.assertIn("REF_MARKER_1", msg)
            self.assertIn("REF_MARKER_2", msg)
            # Alias resolving to mod7.py must NOT duplicate mod7.py
            self.assertEqual(msg.count("MOD_MARKER_7 = 7"), 1)

    def test_rehydration_sysmsg_idempotency_and_nested_strip_safety(self) -> None:
        # Real markers match; quoted markers inside arbitrary SYSTEM_MESSAGE text do NOT
        self.assertTrue(core.is_rehydration_system_message("[POST-COMPACT REHYDRATION v1 | checkpoint=10]\n...", 10))
        self.assertFalse(core.is_rehydration_system_message("[POST-COMPACT REHYDRATION v1 | checkpoint=4]\n...", 10))
        self.assertTrue(
            core.is_rehydration_system_message(
                "<SYSTEM_MESSAGE>\nThe following system message was injected by a PreInvocation hook:\n"
                "[POST-COMPACT REHYDRATION v1 | checkpoint=10]\nbody\n</SYSTEM_MESSAGE>",
                10,
            )
        )
        quoted_swarm_msg = (
            "The following is a <SYSTEM_MESSAGE> not actually sent by the user.\n\n"
            "<SYSTEM_MESSAGE>\n[swarm] Please audit `[POST-COMPACT REHYDRATION v1 | checkpoint=10]` handling.\n"
            "</SYSTEM_MESSAGE>"
        )
        self.assertFalse(core.is_rehydration_system_message(quoted_swarm_msg, 10))
        self.assertFalse(core.is_rehydration_system_message(quoted_swarm_msg))

        # End-to-end: quoted marker after CP 10 must NOT suppress CP 10
        with tempfile.TemporaryDirectory() as td:
            full = Path(td) / "brain" / "c-quote" / ".system_generated" / "logs" / "transcript_full.jsonl"
            _write_jsonl(
                full,
                [
                    {"step_index": 0, "type": "USER_INPUT", "content": "<USER_REQUEST>\nwork\n</USER_REQUEST>"},
                    {"step_index": 10, "type": "CHECKPOINT", "content": "<summary>s</summary>"},
                    {"step_index": 11, "type": "SYSTEM_MESSAGE", "content": quoted_swarm_msg},
                ],
            )
            res = core.build_rehydration(full, workspace_roots=[])
            self.assertIsNotNone(res)
            self.assertEqual(res[1]["checkpoint"], 10)

        # NESTED_REHYDRATE_RE must NOT swallow unrelated multi-line content between an earlier mention and a nested block
        sample = (
            "The following is a <SYSTEM_MESSAGE> explanation line.\n"
            "IMPORTANT_CONTENT_MUST_SURVIVE\n"
            "<SYSTEM_MESSAGE>\n[POST-COMPACT REHYDRATION v1 | checkpoint=2]\nold\n</SYSTEM_MESSAGE>\n"
            "AFTER_LINE"
        )
        cleaned = core.strip_nested_rehydration(sample)
        self.assertIn("IMPORTANT_CONTENT_MUST_SURVIVE", cleaned)
        self.assertIn("AFTER_LINE", cleaned)
        self.assertNotIn("[POST-COMPACT REHYDRATION v1 | checkpoint=2]", cleaned)

    def test_config_invalid_workspace_falls_back_to_global_value(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            ws = root / "ws"
            (ws / ".agents").mkdir(parents=True)
            global_cfg = root / "global.json"
            ws_cfg = ws / ".agents" / "rehydrate.json"

            global_cfg.write_text(
                json.dumps(
                    {
                        "budgetSingleFileChars": 2500,
                        "maxModifiedFiles": 3,
                        "fileScopeSincePrevCheckpoint": False,
                    }
                ),
                encoding="utf-8",
            )
            ws_cfg.write_text(
                json.dumps(
                    {
                        "budgetSingleFileChars": "bad",
                        "maxModifiedFiles": -5,
                        "fileScopeSincePrevCheckpoint": "nope",
                    }
                ),
                encoding="utf-8",
            )
            cfg = core.load_rehydrate_config(ws, global_config_path=global_cfg)
            self.assertEqual(cfg.budget_single_file_chars, 2500)
            self.assertEqual(cfg.max_modified_files, 3)
            self.assertFalse(cfg.file_scope_since_prev_checkpoint)

    # -------------------------------------------------------------------------
    # Hook & CLI regression tests (#6, #10)
    # -------------------------------------------------------------------------

    def test_hooks_json_portable_command_and_timeout(self) -> None:
        hj = json.loads((PLUGIN_ROOT / "hooks.json").read_text(encoding="utf-8"))
        entry = hj["agy-rehydrate"]["PreInvocation"][0]
        cmd = entry["command"]
        self.assertNotIn("CorixLab", cmd)
        self.assertNotIn("C:\\", cmd)
        self.assertIn("python -S -B scripts/rehydrate_after_compact.py --hook", cmd)
        self.assertIn("python3 -S -B scripts/rehydrate_after_compact.py --hook", cmd)
        self.assertGreaterEqual(entry["timeout"], 15)

        shell_cmd = ["cmd", "/c", cmd] if os.name == "nt" else ["sh", "-c", cmd]
        res = subprocess.run(shell_cmd, cwd=str(PLUGIN_ROOT), input="{}", capture_output=True, text=True, check=True)
        self.assertEqual(res.stdout.strip(), "{}")

    def test_hook_fresh_gap_enforced_with_existing_state_and_skips_fork_scan(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            ws = root / "ws"
            ws.mkdir()
            logs = root / "brain" / "c-stale" / ".system_generated" / "logs"
            compact = logs / "transcript.jsonl"
            full = logs / "transcript_full.jsonl"
            state_p = logs / "rehydrate_state.json"

            # Simulate plugin disabled across CP 10 and re-enabled at step 40 (had_state=True, prev_cp=None)
            records = [
                {"step_index": 5, "type": "USER_INPUT", "content": "<USER_REQUEST>\nforked\n</USER_REQUEST>"},
                {"step_index": 10, "type": "CHECKPOINT", "content": "<summary>old cp</summary>"},
                {"step_index": 40, "type": "USER_INPUT", "content": "<USER_REQUEST>\nmuch later\n</USER_REQUEST>"},
            ]
            _write_jsonl(compact, records)
            _write_jsonl(full, records)
            rh_entry._save_state_str(str(state_p), None, 0)

            payload = json.dumps({"transcriptPath": str(compact), "cwd": str(ws)})
            buf = io.StringIO()
            with (
                mock.patch.object(sys, "stdin", io.StringIO(payload)),
                mock.patch.object(sys, "stdout", buf),
                mock.patch.object(core, "resolve_fork_ancestors", side_effect=AssertionError("must skip fork scan")),
            ):
                rc = rh_entry.run_hook()
            self.assertEqual(rc, 0)
            self.assertEqual(buf.getvalue().strip(), "{}")
            st = json.loads(state_p.read_text(encoding="utf-8"))
            self.assertEqual(st["last_rehydrated_checkpoint"], 10)
            self.assertEqual(st["last_status"], "stale_skipped")

    def test_hook_full_lag_retries_and_monotonic_truncation_guard(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            ws = root / "ws"
            ws.mkdir()
            logs = root / "brain" / "c-lag" / ".system_generated" / "logs"
            compact = logs / "transcript.jsonl"
            full = logs / "transcript_full.jsonl"
            state_p = logs / "rehydrate_state.json"

            base_recs = [
                {"step_index": 0, "type": "USER_INPUT", "content": "<USER_REQUEST>\nphase 1\n</USER_REQUEST>"},
                {"step_index": 50, "type": "CHECKPOINT", "content": "<summary>cp50</summary>"},
                {"step_index": 51, "type": "SYSTEM_MESSAGE", "content": "[POST-COMPACT REHYDRATION v1 | checkpoint=50]\n..."},
                {"step_index": 60, "type": "USER_INPUT", "content": "<USER_REQUEST>\nphase 2\n</USER_REQUEST>"},
            ]
            cp100_rec = {"step_index": 100, "type": "CHECKPOINT", "content": "<summary>cp100</summary>"}
            _write_jsonl(compact, [*base_recs, cp100_rec])
            _write_jsonl(full, base_recs)  # transcript_full.jsonl lags behind!
            rh_entry._save_state_str(str(state_p), 50, 10, None, last_status="injected")

            payload = json.dumps({"transcriptPath": str(compact), "cwd": str(ws)})

            def _call_hook() -> dict:
                buf = io.StringIO()
                with mock.patch.object(sys, "stdin", io.StringIO(payload)), mock.patch.object(sys, "stdout", buf):
                    self.assertEqual(rh_entry.run_hook(), 0)
                return json.loads(buf.getvalue())

            # 1) full_lag -> returns {} and MUST NOT advance last_rehydrated_checkpoint or checked_offset
            self.assertEqual(_call_hook(), {})
            st_lag = json.loads(state_p.read_text(encoding="utf-8"))
            self.assertEqual(st_lag["last_rehydrated_checkpoint"], 50)
            self.assertEqual(st_lag["checked_offset"], 10)

            # 2) Once transcript_full.jsonl catches up, next turn retries and injects CP 100
            _write_jsonl(full, [*base_recs, cp100_rec])
            out2 = _call_hook()
            self.assertIn("injectSteps", out2)
            self.assertIn("checkpoint=100", out2["injectSteps"][0]["systemMessage"]["systemMessage"])
            st_ok = json.loads(state_p.read_text(encoding="utf-8"))
            self.assertEqual(st_ok["last_rehydrated_checkpoint"], 100)
            self.assertEqual(st_ok["last_status"], "injected")

            # 3) Momentary 0-byte truncation -> preserves prev_cp=100 & last_status, resets checked_offset=0
            compact.write_bytes(b"")
            self.assertEqual(_call_hook(), {})
            st_trunc = json.loads(state_p.read_text(encoding="utf-8"))
            self.assertEqual(st_trunc["last_rehydrated_checkpoint"], 100)
            self.assertEqual(st_trunc["checked_offset"], 0)
            self.assertEqual(st_trunc["last_status"], "injected")

            # 4) Reappearance with older CP 50 (or CP 100) never rolls back prev_cp or re-injects
            _write_jsonl(compact, base_recs)
            self.assertEqual(_call_hook(), {})
            self.assertEqual(json.loads(state_p.read_text(encoding="utf-8"))["last_rehydrated_checkpoint"], 100)

    def test_extract_step_index_bytes_and_scan_compact_tail_hardening(self) -> None:
        self.assertEqual(rh_entry._extract_step_index_bytes(b'{"step_index":0,"type":"USER_INPUT"}\n'), 0)
        self.assertEqual(rh_entry._extract_step_index_bytes(b'\xef\xbb\xbf{"step_index" \t : \t 42}\n'), 42)
        self.assertEqual(rh_entry._extract_step_index_bytes(b'{"q": "step_index", "count": 9999, "step_index": 2}\n'), 2)
        self.assertEqual(rh_entry._extract_step_index_bytes(b'{"meta": {"step_index": 9999}, "step_index": 5}\n'), 5)
        self.assertEqual(
            rh_entry._extract_step_index_bytes(
                b'{"content": "' + b"x" * 300 + b'", "step_index": 5, "tool_calls": [{"args": {"step_index": 9999}}]}\n'
            ),
            5,
        )
        for bad_line in (
            b'{"step_index": 123.5}\n',
            b'{"step_index": 5 .2}\n',
            b'{"step_index": 1e3}\n',
            b'{"step_index": -5}\n',
            b'{"step_index": true}\n',
            b'{"step_index": 01}\n',
            b'{"meta": {"step_index": 9999}}\n',
        ):
            self.assertEqual(rh_entry._extract_step_index_bytes(bad_line), -1, bad_line)

        with tempfile.TemporaryDirectory() as td:
            p = Path(td) / "compact.jsonl"
            raw = (
                b'\xef\xbb\xbf{"step_index": 10, "type": "CHECKPOINT"}\n'
                b'{"type": "CHECKPOINT"}\n'
                b'{"step_index": -2, "type": "CHECKPOINT"}\n'
                b'{"step_index": 4, "type": "CHECKPOINT"}\n'
                b'{"step_index": 12, "type": "USER_INPUT"}\n'
            )
            p.write_bytes(raw)
            self.assertEqual(rh_entry.scan_compact_tail(p, 0, None, need_max_step=True), (10, 12, len(raw)))
            self.assertEqual(rh_entry.scan_compact_tail(p, 0, None, need_max_step=False), (10, -1, len(raw)))
            # prev_offset == st_size with need_max_step=True must still scan max_step
            self.assertEqual(rh_entry.scan_compact_tail(p, len(raw), 10, need_max_step=True), (10, 12, len(raw)))

    def test_save_state_permission_retry_stdio_bom_broken_pipe_and_discovery(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            # 1) _save_state_str PermissionError retry succeeds on 3rd attempt
            st_path = str(root / "rehydrate_state.json")
            calls = {"n": 0}
            real_replace = os.replace

            def _flaky_replace(src: str, dst: str) -> None:
                calls["n"] += 1
                if calls["n"] <= 2:
                    raise PermissionError("transient lock")
                real_replace(src, dst)

            with mock.patch.object(os, "replace", side_effect=_flaky_replace):
                rh_entry._save_state_str(st_path, 7, 128, None, last_status="injected")
            self.assertEqual(calls["n"], 3)
            self.assertEqual(json.loads(Path(st_path).read_text(encoding="utf-8"))["last_rehydrated_checkpoint"], 7)

            with mock.patch.object(os, "replace", side_effect=PermissionError("locked")):
                rh_entry._save_state_str(st_path, 9, 256)
            self.assertEqual(list(root.glob("*.tmp")), [])

            # 2) UTF-8 BOM stdin triggers rehydration (not silently dropped)
            ws = root / "ws"
            ws.mkdir()
            logs = root / "brain" / "c-bom" / ".system_generated" / "logs"
            compact = logs / "transcript.jsonl"
            full = logs / "transcript_full.jsonl"
            recs = [
                {"step_index": 0, "type": "USER_INPUT", "content": "<USER_REQUEST>\n中文 BOM 測試\n</USER_REQUEST>"},
                {"step_index": 1, "type": "CHECKPOINT", "content": "<summary>s</summary>"},
            ]
            _write_jsonl(compact, recs)
            _write_jsonl(full, recs)
            bom_payload = ("\ufeff" + json.dumps({"transcriptPath": str(compact), "cwd": str(ws)}, ensure_ascii=False)).encode(
                "utf-8"
            )
            fake_in = io.TextIOWrapper(io.BytesIO(bom_payload), encoding="cp950", errors="strict")
            fake_out = io.StringIO()
            with mock.patch.object(sys, "stdin", fake_in), mock.patch.object(sys, "stdout", fake_out):
                self.assertEqual(rh_entry.run_hook(), 0)
            bom_out = json.loads(fake_out.getvalue())
            self.assertIn("injectSteps", bom_out)
            self.assertIn("中文 BOM 測試", bom_out["injectSteps"][0]["systemMessage"]["systemMessage"])

            # 3) BrokenPipeError on stdout still returns exit 0
            class _BrokenStdout:
                def write(self, _s: str) -> int:
                    raise BrokenPipeError("broken pipe")

                def flush(self) -> None:
                    raise BrokenPipeError("broken pipe")

            with mock.patch.object(sys, "stdin", io.StringIO("{}")), mock.patch.object(sys, "stdout", _BrokenStdout()):
                self.assertEqual(rh_entry.run_hook(), 0)

            # 4) _find_transcript_files_str: traversal rejected & transcript_full.jsonl fallback
            ad_full_only = root / "conv-full-only"
            ad_logs = ad_full_only / ".system_generated" / "logs"
            ad_logs.mkdir(parents=True)
            ad_full = ad_logs / "transcript_full.jsonl"
            ad_full.write_text('{"step_index": 0}\n', encoding="utf-8")
            c1, f1, _ = rh_entry._find_transcript_files_str({"artifactDirectoryPath": f"  {ad_full_only}  "})
            self.assertEqual((c1, f1), (str(ad_full), str(ad_full)))

            fake_home = root / "home"
            evil_logs = fake_home / ".gemini" / "antigravity" / "evil" / ".system_generated" / "logs"
            evil_logs.mkdir(parents=True)
            (evil_logs / "transcript.jsonl").write_text('{"step_index": 0}\n', encoding="utf-8")
            good_logs = fake_home / ".gemini" / "antigravity" / "brain" / "cid-full-only" / ".system_generated" / "logs"
            good_logs.mkdir(parents=True)
            good_full = good_logs / "transcript_full.jsonl"
            good_full.write_text('{"step_index": 0}\n', encoding="utf-8")
            with mock.patch.object(os.path, "expanduser", return_value=str(fake_home)):
                for bad_cid in ("../evil", "..\\evil", "a/b", "a\\b", ".", "..", "C:evil"):
                    self.assertEqual(
                        rh_entry._find_transcript_files_str({"conversationId": bad_cid}),
                        (None, None, None),
                        bad_cid,
                    )
                c2, f2, _ = rh_entry._find_transcript_files_str({"conversationId": "cid-full-only"})
                self.assertEqual((c2, f2), (str(good_full), str(good_full)))

    def test_pep562_private_no_heavy_import_and_no_stdlib_leaks(self) -> None:
        probe = (
            "import json, sys; "
            f"sys.path.insert(0, {str(SCRIPTS_DIR)!r}); "
            "import rehydrate_after_compact as m; "
            "has_priv = hasattr(m, '_nonexistent_private'); "
            "heavy_after_priv = ('rehydrate_core' in sys.modules or 'rehydrate_cli' in sys.modules); "
            "leaks = [n for n in ('re', 'stat', 'unicodedata', 'Path', 'datetime', 'argparse', 'urlparse') if hasattr(m, n)]; "
            "ok_pub = callable(m.build_rehydration) and isinstance(m.MARKER_PREFIX, str) and callable(m.run_cli); "
            "print(json.dumps({'has_priv': has_priv, 'heavy': heavy_after_priv, 'leaks': leaks, 'ok_pub': ok_pub}))"
        )
        res = subprocess.run([sys.executable, "-S", "-B", "-c", probe], capture_output=True, text=True, check=True)
        info = json.loads(res.stdout.strip())
        self.assertEqual(info, {"has_priv": False, "heavy": False, "leaks": [], "ok_pub": True})

    def test_cli_split_cwd_validation_target_resolution_status_and_checkpoint(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            ws1 = root / "ws1"
            ws2 = root / "ws2"
            (ws1 / ".agents").mkdir(parents=True)
            (ws2 / ".agents").mkdir(parents=True)
            (ws1 / ".agents" / "rehydrate.json").write_text('{"budgetSingleFileChars": 1111}\n', encoding="utf-8")
            (ws2 / ".agents" / "rehydrate.json").write_text('{"budgetSingleFileChars": 2222}\n', encoding="utf-8")

            # 1) Split --cwd before & after subcommand merges in order (ws1 wins)
            buf = io.StringIO()
            with mock.patch.object(sys, "stdout", buf):
                rc = cli.run_cli(["--cwd", str(ws1), "show-config", "--cwd", str(ws2)])
            self.assertEqual(rc, 0)
            cfg_out = json.loads(buf.getvalue())
            self.assertEqual(cfg_out["workspaces"], [str(ws1.resolve()), str(ws2.resolve())])
            self.assertEqual(cfg_out["budgetSingleFileChars"], 1111)

            # Reject empty/UNC/remote file:// --cwd
            for bad_cwd in ("", "   ", r"\\evil.invalid\share", "//evil.invalid/share", "file://evil.invalid/share"):
                buf = io.StringIO()
                with mock.patch.object(sys, "stdout", buf):
                    self.assertEqual(cli.run_cli(["show-config", "--cwd", bad_cwd]), 1, bad_cwd)

            # 2) status --limit < 1 rejected
            for bad_lim in ("0", "-3"):
                buf = io.StringIO()
                with mock.patch.object(sys, "stdout", buf):
                    self.assertEqual(cli.run_cli(["status", "--limit", bad_lim]), 1, bad_lim)

            # 3) --checkpoint < 0 and missing checkpoint <= N exit 1; mismatch annotates output
            sample_full = root / "custom_snapshot.jsonl"
            _write_jsonl(
                sample_full,
                [
                    {"step_index": 0, "type": "USER_INPUT", "content": "<USER_REQUEST>\ninit\n</USER_REQUEST>"},
                    {"step_index": 4, "type": "CHECKPOINT", "content": "<summary>cp4</summary>"},
                ],
            )
            sib_full = root / "transcript_full.jsonl"
            _write_jsonl(sib_full, [{"step_index": 0, "type": "USER_INPUT", "content": "sibling"}])
            sib_compact = root / "transcript.jsonl"
            _write_jsonl(sib_compact, [{"step_index": 0, "type": "USER_INPUT", "content": "compact"}])

            buf = io.StringIO()
            with mock.patch.object(sys, "stdout", buf):
                self.assertEqual(cli.run_cli(["inspect", str(sample_full), "--checkpoint", "-1"]), 1)

            buf = io.StringIO()
            with mock.patch.object(sys, "stdout", buf):
                self.assertEqual(cli.run_cli(["inspect", str(sample_full), "--checkpoint", "1"]), 1)
            self.assertIn("no checkpoint", buf.getvalue().lower())

            buf = io.StringIO()
            with mock.patch.object(sys, "stdout", buf):
                self.assertEqual(cli.run_cli(["inspect", str(sample_full), "--checkpoint", "999"]), 0)
            insp = json.loads(buf.getvalue())
            self.assertEqual(insp["checkpoint"], 4)
            self.assertEqual(insp["requested_checkpoint"], 999)
            self.assertIn("note", insp)

            buf = io.StringIO()
            with mock.patch.object(sys, "stdout", buf):
                self.assertEqual(cli.run_cli(["preview", str(sample_full), "--checkpoint", "999"]), 0)
            self.assertIn("Note:", buf.getvalue())

            # 4) resolve_target_transcript: blank rejected, custom file honored, transcript.jsonl upgraded
            self.assertIsNone(cli.resolve_target_transcript(""))
            self.assertIsNone(cli.resolve_target_transcript("   "))
            r_custom = cli.resolve_target_transcript(str(sample_full))
            self.assertIsNotNone(r_custom)
            assert r_custom is not None
            self.assertEqual((r_custom[2], r_custom[3]), (sample_full, sample_full))
            r_comp = cli.resolve_target_transcript(str(sib_compact))
            self.assertIsNotNone(r_comp)
            assert r_comp is not None
            self.assertEqual((r_comp[2], r_comp[3]), (sib_compact, sib_full))

            # 5) Exact CID > prefix match & status formatting (steps=empty, skipped(stale), disabled, rehydrated)
            fake_home = root / "fake_home"
            brain = fake_home / ".gemini" / "antigravity" / "brain"

            l_exact = brain / "abcd" / ".system_generated" / "logs"
            l_exact.mkdir(parents=True)
            (l_exact / "transcript.jsonl").write_text('{"step_index": 2, "type": "CHECKPOINT"}\n', encoding="utf-8")
            (l_exact / "rehydrate_state.json").write_text(
                json.dumps({"last_rehydrated_checkpoint": 2, "checked_offset": 10, "last_status": "stale_skipped"}),
                encoding="utf-8",
            )
            os.utime(l_exact / "transcript.jsonl", (1000, 1000))

            l_pref = brain / "abcd1234" / ".system_generated" / "logs"
            l_pref.mkdir(parents=True)
            (l_pref / "transcript.jsonl").write_bytes(b"")
            os.utime(l_pref / "transcript.jsonl", (2000, 2000))

            l_dis = brain / "sess-dis" / ".system_generated" / "logs"
            l_dis.mkdir(parents=True)
            (l_dis / "transcript.jsonl").write_text('{"step_index": 5, "type": "CHECKPOINT"}\n', encoding="utf-8")
            (l_dis / "rehydrate_state.json").write_text(
                json.dumps({"last_rehydrated_checkpoint": 5, "checked_offset": 10, "last_status": "disabled"}),
                encoding="utf-8",
            )
            os.utime(l_dis / "transcript.jsonl", (3000, 3000))

            l_inj = brain / "sess-inj" / ".system_generated" / "logs"
            l_inj.mkdir(parents=True)
            (l_inj / "transcript.jsonl").write_text('{"step_index": 8, "type": "CHECKPOINT"}\n', encoding="utf-8")
            (l_inj / "rehydrate_state.json").write_text(
                json.dumps({"last_rehydrated_checkpoint": 8, "checked_offset": 10, "last_status": "injected"}),
                encoding="utf-8",
            )
            os.utime(l_inj / "transcript.jsonl", (4000, 4000))

            with mock.patch.object(Path, "home", return_value=fake_home):
                r_exact = cli.resolve_target_transcript("abcd")
                self.assertIsNotNone(r_exact)
                assert r_exact is not None
                self.assertEqual(r_exact[1], "abcd")

                # A `./latest` file in CWD must NOT shadow `resolve_target_transcript("latest")`
                cwd_latest = root / "latest"
                cwd_latest.write_text("not a transcript\n", encoding="utf-8")
                orig_cwd = os.getcwd()
                try:
                    os.chdir(root)
                    r_latest = cli.resolve_target_transcript("latest")
                finally:
                    os.chdir(orig_cwd)
                self.assertIsNotNone(r_latest)
                assert r_latest is not None
                self.assertEqual(r_latest[0], "antigravity")
                self.assertEqual(r_latest[1], "sess-inj")

                buf = io.StringIO()
                with mock.patch.object(sys, "stdout", buf):
                    self.assertEqual(cli.run_cli(["status", "--limit", "5"]), 0)
                st_out = buf.getvalue()
                self.assertIn("steps=empty", st_out)
                self.assertIn("skipped(stale)=2", st_out)
                self.assertIn("disabled=5", st_out)
                self.assertIn("rehydrated=8", st_out)


if __name__ == "__main__":
    unittest.main(verbosity=2)
