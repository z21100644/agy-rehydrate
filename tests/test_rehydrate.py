#!/usr/bin/env python3
"""Comprehensive regression and performance test suite for `agy-rehydrate`."""

from __future__ import annotations

import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import time
import unittest

sys.dont_write_bytecode = True

REPO_ROOT = Path(__file__).resolve().parents[1]
PLUGIN_SRC = REPO_ROOT / "plugins" / "agy-rehydrate"
SCRIPTS_DIR = PLUGIN_SRC / "scripts"
sys.path.insert(0, str(SCRIPTS_DIR))

import rehydrate_after_compact as mod  # noqa: E402


def _write_jsonl(path: Path, rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        for row in rows:
            f.write(json.dumps(row, ensure_ascii=False) + "\n")


class RehydrateRegressionTests(unittest.TestCase):
    """End-to-end and unit regression suite for self-adaptive post-compact rehydration."""

    def setUp(self) -> None:
        self.tmp_root = REPO_ROOT / ".tmp_unittest_rehydrate"
        if self.tmp_root.exists():
            shutil.rmtree(self.tmp_root, ignore_errors=True)
        self.ws = self.tmp_root / "workspace"
        self.ws.mkdir(parents=True, exist_ok=True)

    def tearDown(self) -> None:
        if self.tmp_root.exists():
            shutil.rmtree(self.tmp_root, ignore_errors=True)

    def test_trivial_greeting_silent_bypass_and_hook_idempotency(self) -> None:
        logs = self.tmp_root / "brain" / "cid-trivial" / ".system_generated" / "logs"
        rows = [
            {"step_index": 0, "type": "USER_INPUT", "content": "<USER_REQUEST>你好</USER_REQUEST>"},
            {"step_index": 1, "type": "PLANNER_RESPONSE", "content": "你好！有什麼我可以協助的嗎？"},
            {"step_index": 2, "type": "USER_INPUT", "content": "<USER_REQUEST>/compact</USER_REQUEST>"},
            {
                "step_index": 3,
                "type": "CHECKPOINT",
                "content": "<CONTEXT_SUMMARY>\n# User Requests\n1. 你好\n<summary>Greeting.</summary>\n</CONTEXT_SUMMARY>",
            },
        ]
        _write_jsonl(logs / "transcript.jsonl", rows)
        _write_jsonl(logs / "transcript_full.jsonl", rows)

        self.assertIsNone(mod.build_rehydration(logs / "transcript_full.jsonl", workspace_roots=[self.ws]))

        payload = json.dumps({"transcriptPath": str(logs / "transcript.jsonl"), "workspacePaths": [str(self.ws)]})
        p = subprocess.run(
            [sys.executable, "-B", str(SCRIPTS_DIR / "rehydrate_after_compact.py"), "--hook"],
            input=payload,
            text=True,
            capture_output=True,
            encoding="utf-8",
            check=True,
        )
        self.assertEqual(json.loads(p.stdout.strip()), {})
        state_file = logs / "rehydrate_state.json"
        self.assertTrue(state_file.is_file())
        mtime_first = state_file.stat().st_mtime_ns

        # Repeat normal-turn call with unchanged transcript must skip redundant state disk write
        time.sleep(0.02)
        p2 = subprocess.run(
            [sys.executable, "-B", str(SCRIPTS_DIR / "rehydrate_after_compact.py"), "--hook"],
            input=payload,
            text=True,
            capture_output=True,
            encoding="utf-8",
            check=True,
        )
        self.assertEqual(json.loads(p2.stdout.strip()), {})
        self.assertEqual(state_file.stat().st_mtime_ns, mtime_first)

    def test_completed_qa_two_exchanges_and_checkpoint_delta_dedup(self) -> None:
        ref_doc = self.ws / "guide.md"
        ref_doc.write_text("# Guide\n" + ("Line content.\n" * 200), encoding="utf-8")
        logs = self.tmp_root / "brain" / "cid-qa" / ".system_generated" / "logs"
        rows = [
            {"step_index": 0, "type": "USER_INPUT", "content": "<USER_REQUEST>早期需求一：架構設定 A</USER_REQUEST>"},
            {"step_index": 1, "type": "PLANNER_RESPONSE", "content": "收到 A。"},
            {"step_index": 2, "type": "USER_INPUT", "content": "<USER_REQUEST>近期問題二：檢查文件</USER_REQUEST>"},
            {
                "step_index": 3,
                "type": "PLANNER_RESPONSE",
                "content": "先讀取 guide.md。",
                "tool_calls": [{"name": "read_file", "args": {"Path": str(ref_doc)}}],
            },
            {"step_index": 4, "type": "GENERIC", "status": "DONE", "content": "x" * 6000},
            {"step_index": 5, "type": "PLANNER_RESPONSE", "content": "已完成檢查。" + ("中段補充細節文字。" * 130) + "結論無誤。"},
            {"step_index": 6, "type": "USER_INPUT", "content": "<USER_REQUEST>最後問題三：確認目前結論</USER_REQUEST>"},
            {"step_index": 7, "type": "PLANNER_RESPONSE", "content": "最終結論：維持自適應兩輪問答。"},
            {
                "step_index": 8,
                "type": "CHECKPOINT",
                "content": (
                    "<CONTEXT_SUMMARY>\n# User Requests\n"
                    "1. 近期問題二：檢查文件\n2. 最後問題三：確認目前結論\n"
                    "<summary>QA summary.</summary>\n</CONTEXT_SUMMARY>"
                ),
            },
        ]
        _write_jsonl(logs / "transcript_full.jsonl", rows)
        res = mod.build_rehydration(logs / "transcript_full.jsonl", workspace_roots=[self.ws])
        self.assertIsNotNone(res)
        assert res is not None
        msg, st = res
        self.assertEqual(st["session_mode"], "completed_qa")
        self.assertEqual(st["kept_turns"], 4)
        self.assertEqual(st["folded_assistant_turns"], 1)
        self.assertEqual(st["earlier_user_msgs"], 1)
        self.assertEqual(st["files"], 0)
        self.assertIn("早期需求一：架構設定 A", msg)
        self.assertIn("[Step 2 | User]:", msg)
        self.assertIn("(Tools run: read_file(guide.md))", msg)
        self.assertLessEqual(st["chars"], st["ratio_cap"])

    def test_attachment_dedup_provenance_and_truncated_tail_recovery(self) -> None:
        logs = self.tmp_root / "brain" / "cid-att" / ".system_generated" / "logs"
        long_paste = (
            "Ran check git\nAnalyzed a.py:L1-10\nEdited b.py\nCreated c.py\n\n"
            "已完成第一階段修改：\n\n1. **模組甲**\n   - 細節說明\n"
            + ("中間長段實作紀錄文字 " * 260)
            + "\n最終驗收結論：全部 34 項測試通過。"
        )
        prefix_in_cp = long_paste[:1200] + "\n<truncated 3200 bytes>"
        rows = [
            {"step_index": 0, "type": "USER_INPUT", "content": f"<USER_REQUEST>{long_paste}</USER_REQUEST>"},
            {"step_index": 1, "type": "PLANNER_RESPONSE", "content": "收到長篇紀錄。"},
            {
                "step_index": 2,
                "type": "USER_INPUT",
                "media": [{"mime_type": "image/png", "uri": "C:/tmp/media_999_shot.png"}],
                "content": "<USER_REQUEST>看一下呼吸燈配色</USER_REQUEST>",
            },
            {"step_index": 3, "type": "PLANNER_RESPONSE", "content": "配色正常。"},
            {"step_index": 4, "type": "USER_INPUT", "content": "<USER_REQUEST>倒數第二問</USER_REQUEST>"},
            {"step_index": 5, "type": "PLANNER_RESPONSE", "content": "回覆倒數第二問。"},
            {"step_index": 6, "type": "USER_INPUT", "content": "<USER_REQUEST>最後一問</USER_REQUEST>"},
            {"step_index": 7, "type": "PLANNER_RESPONSE", "content": "回覆最後一問。"},
            {
                "step_index": 8,
                "type": "CHECKPOINT",
                "content": (
                    "<CONTEXT_SUMMARY>\n# User Requests\n"
                    f"1. {prefix_in_cp}\n"
                    "2. 看一下呼吸燈配色\n  - [Attachment: image/png (C:/tmp/media_999_shot.png)]\n"
                    "3. 倒數第二問\n4. 最後一問\n"
                    "<summary>Summary.</summary>\n</CONTEXT_SUMMARY>"
                ),
            },
        ]
        _write_jsonl(logs / "transcript_full.jsonl", rows)
        res = mod.build_rehydration(logs / "transcript_full.jsonl", workspace_roots=[self.ws])
        self.assertIsNotNone(res)
        assert res is not None
        msg, st = res
        self.assertNotIn("看一下呼吸燈配色", msg)
        self.assertEqual(st["deduped_summary_user_msgs"], 1)
        self.assertIn("#2 `media_999_shot.png` @ step 2", msg)
        self.assertIn("[Opening preserved in <CONTEXT_SUMMARY>; recovered truncated tail", msg)
        self.assertIn("最終驗收結論：全部 34 項測試通過。", msg)

    def test_file_tier_ranking_indented_hunk_and_three_security_gates(self) -> None:
        outside = self.tmp_root / "outside_secret.txt"
        outside.write_text("TOP_SECRET_OUTSIDE", encoding="utf-8")
        (self.ws / ".env").write_text("SECRET_TOKEN=123", encoding="utf-8")

        src_js = self.ws / "telemetry.mjs"
        test_js = self.ws / "telemetry.test.mjs"
        rd1 = self.ws / "README.md"
        rd2 = self.ws / "README.zh-TW.md"
        rd3 = self.ws / "README.zh-CN.md"
        err_py = self.ws / "denied.py"

        js_lines = [f"export function pad_{i:03d}() {{ return 'val_{i:03d}_padding'; }}" for i in range(1, 351)]
        js_lines[219] = "        if (ok) {\n            cnt += 1;\n        }"
        src_js.write_text("\n".join(js_lines) + "\n", encoding="utf-8")
        test_js.write_text("import assert from 'node:assert';\nassert.ok(true);\n", encoding="utf-8")
        err_py.write_text("DENIED_FILE_CONTENT = 1\n", encoding="utf-8")
        for rd in (rd1, rd2, rd3):
            rd.write_text("# Doc\n" + ("Documentation line.\n" * 180), encoding="utf-8")

        logs = self.tmp_root / "brain" / "cid-sec" / ".system_generated" / "logs"
        rows = [
            {"step_index": 0, "type": "USER_INPUT", "content": "<USER_REQUEST>更新 telemetry 與說明文件</USER_REQUEST>"},
            {
                "step_index": 1,
                "type": "PLANNER_RESPONSE",
                "content": "",
                "tool_calls": [
                    {
                        "name": "edit_file",
                        "args": {
                            "Path": str(src_js),
                            "Replacement": "\n        if (ok) {\n            cnt += 1;\n        }\n",
                        },
                    },
                    {"name": "edit_file", "args": {"Path": str(test_js), "Replacement": "assert.ok(true);"}},
                    {"name": "read_file", "args": {"Path": str(outside)}},
                    {"name": "read_file", "args": {"Path": str(self.ws / ".env")}},
                    {"name": "edit_file", "args": {"Path": str(err_py), "Replacement": "x"}},
                ],
            },
            {"step_index": 2, "type": "TOOL_RESPONSE", "status": "DONE", "content": "ok"},
            {"step_index": 3, "type": "TOOL_RESPONSE", "status": "DONE", "content": "ok"},
            {"step_index": 4, "type": "TOOL_RESPONSE", "status": "DONE", "content": "ok"},
            {"step_index": 5, "type": "TOOL_RESPONSE", "status": "DONE", "content": "ok"},
            {"step_index": 6, "type": "TOOL_RESPONSE", "status": "ERROR", "content": "Permission denied"},
            {
                "step_index": 7,
                "type": "PLANNER_RESPONSE",
                "content": "",
                "tool_calls": [
                    {"name": "edit_file", "args": {"Path": str(rd1), "Replacement": "# Doc"}},
                    {"name": "edit_file", "args": {"Path": str(rd2), "Replacement": "# Doc"}},
                    {"name": "edit_file", "args": {"Path": str(rd3), "Replacement": "# Doc"}},
                ],
            },
            {"step_index": 8, "type": "TOOL_RESPONSE", "status": "DONE", "content": "ok"},
            {"step_index": 9, "type": "TOOL_RESPONSE", "status": "DONE", "content": "ok"},
            {"step_index": 10, "type": "TOOL_RESPONSE", "status": "DONE", "content": "ok"},
            {"step_index": 11, "type": "PLANNER_RESPONSE", "content": "已全部更新完成。"},
            {
                "step_index": 12,
                "type": "CHECKPOINT",
                "content": "<CONTEXT_SUMMARY>\n# User Requests\n1. 更新 telemetry 與說明文件\n<summary>Done</summary>\n</CONTEXT_SUMMARY>",
            },
        ]
        _write_jsonl(logs / "transcript_full.jsonl", rows)
        res = mod.build_rehydration(logs / "transcript_full.jsonl", workspace_roots=[self.ws])
        self.assertIsNotNone(res)
        assert res is not None
        msg, st = res
        self.assertEqual(st["session_mode"], "active_coding")
        self.assertTrue(st["file_paths"][0].endswith("telemetry.mjs"))
        self.assertTrue(st["file_paths"][1].endswith("telemetry.test.mjs"))
        self.assertIn("cnt += 1;", msg)
        self.assertNotIn("TOP_SECRET_OUTSIDE", msg)
        self.assertNotIn("SECRET_TOKEN", msg)
        self.assertNotIn("DENIED_FILE_CONTENT", msg)
        self.assertLessEqual(st["chars"], st["ratio_cap"])

    def test_mid_turn_scoped_read_and_fork_lineage_isolation(self) -> None:
        mod_file = self.ws / "worker.py"
        mod_file.write_text("\n".join(f"ROW_{i:03d} = {i}" for i in range(1, 301)) + "\n", encoding="utf-8")

        parent_logs = self.tmp_root / "brain" / "parent-11112222" / ".system_generated" / "logs"
        _write_jsonl(
            parent_logs / "transcript_full.jsonl",
            [
                {"step_index": 0, "type": "USER_INPUT", "content": "<USER_REQUEST>父分支初始需求</USER_REQUEST>"},
                {"step_index": 1, "type": "PLANNER_RESPONSE", "content": "父分支回覆。"},
                {"step_index": 2, "type": "USER_INPUT", "content": "<USER_REQUEST>分叉後父分支不應洩漏的訊息</USER_REQUEST>"},
            ],
        )

        child_logs = self.tmp_root / "brain" / "child-33334444" / ".system_generated" / "logs"
        _write_jsonl(
            child_logs / "transcript_full.jsonl",
            [
                {"step_index": 2, "type": "USER_INPUT", "content": "<USER_REQUEST>子分支檢查 worker.py 第 150 行</USER_REQUEST>"},
                {
                    "step_index": 3,
                    "type": "PLANNER_RESPONSE",
                    "content": "讀取 145-155 行。",
                    "tool_calls": [{"name": "read_file", "args": {"Path": str(mod_file), "StartLine": 145, "EndLine": 155}}],
                },
                {"step_index": 4, "type": "GENERIC", "status": "DONE", "content": "lines 145-155"},
                {
                    "step_index": 5,
                    "type": "CHECKPOINT",
                    "content": "<CONTEXT_SUMMARY>\n# User Requests\n1. 子分支檢查 worker.py 第 150 行\n<summary>Mid</summary>\n</CONTEXT_SUMMARY>",
                },
            ],
        )
        ancestors = [{"cid": "parent-11112222", "path": str(parent_logs / "transcript_full.jsonl"), "min_step": 0, "max_step": 1}]
        res = mod.build_rehydration(child_logs / "transcript_full.jsonl", ancestors=ancestors, workspace_roots=[self.ws])
        self.assertIsNotNone(res)
        assert res is not None
        msg, st = res
        self.assertEqual(st["session_mode"], "mid_turn")
        self.assertIn("fork_lineage=parent-1(0..1)->child-33(2..4)", msg)
        self.assertIn("父分支初始需求", msg)
        self.assertNotIn("分叉後父分支不應洩漏的訊息", msg)
        self.assertIn("lines 145-155", msg)
        self.assertIn("ROW_150 = 150", msg)
        self.assertNotIn("ROW_010 = 10", msg)

    @unittest.skipUnless(os.name == "nt", "Windows installer batch test")
    def test_install_bat_and_uninstall_bat_isolated_with_python_pinning(self) -> None:
        dst_dir = self.tmp_root / "plugins_dst"
        env = {**os.environ, "AGY_PLUGINS_DST": str(dst_dir)}
        p_in = subprocess.run(
            ["cmd", "/c", str(REPO_ROOT / "install.bat")],
            env=env,
            text=True,
            capture_output=True,
            encoding="utf-8",
            check=True,
        )
        self.assertIn("Installed agy-rehydrate", p_in.stdout)
        installed_hooks = dst_dir / "agy-rehydrate" / "hooks.json"
        self.assertTrue(installed_hooks.is_file())
        hooks_data = json.loads(installed_hooks.read_text(encoding="utf-8"))
        cmd_str = hooks_data["agy-rehydrate"]["PreInvocation"][0]["command"]
        self.assertIn(" -S -B scripts/rehydrate_after_compact.py --hook", cmd_str)
        self.assertIn(" -B scripts/rehydrate_after_compact.py --hook", cmd_str)
        self.assertNotIn("||", cmd_str)

        p_un = subprocess.run(
            ["cmd", "/c", str(REPO_ROOT / "uninstall.bat")],
            env=env,
            text=True,
            capture_output=True,
            encoding="utf-8",
            check=True,
        )
        self.assertIn("Removed", p_un.stdout)
        self.assertFalse((dst_dir / "agy-rehydrate").exists())

    def test_nested_rehydration_stripping_and_utf8_chunk_splitting(self) -> None:
        nested = (
            "The following is a <SYSTEM_MESSAGE> not actually sent by the user.\n"
            "<SYSTEM_MESSAGE>\n[POST-COMPACT REHYDRATION v1 | checkpoint=4]\n"
            "Lossless ground-truth state rehydrated from `transcript_full.jsonl`\n"
            "## 1. Final Pre-Compaction Exchange (2 entries verbatim)\n"
            "[Step 1 | Assistant]: old\n</SYSTEM_MESSAGE>\n"
            "這是真正的新問題"
        )
        stripped = mod.strip_nested_rehydration(nested)
        self.assertIn("這是真正的新問題", stripped)
        self.assertNotIn("Final Pre-Compaction Exchange", stripped)

        huge_sections = [
            "[POST-COMPACT REHYDRATION v1 | checkpoint=50]",
            "Preamble",
            "## 1. Huge\n" + "\n\n".join(f"[Step {i} | User]:\n" + ("漢" * 950) for i in range(38)),
        ]
        chunks = mod.split_into_inject_chunks(huge_sections, max_bytes=mod.MAX_INJECT_STEP_BYTES, checkpoint=50)
        self.assertGreaterEqual(len(chunks), 2)
        for ch in chunks:
            self.assertLessEqual(len(ch.encode("utf-8")), 48000)

    def test_enforce_total_cap_and_cli_subcommands(self) -> None:
        secs = ["[HDR]", "Preamble", "## 1. A\n" + ("a" * 900), "## 2. B\n" + ("b" * 1400)]
        raw_len = len("\n\n".join(secs))
        capped = mod._enforce_total_cap(secs, raw_len - 60)
        self.assertLessEqual(len("\n\n".join(capped)), raw_len - 60)

        logs = self.tmp_root / "brain" / "cid-cli" / ".system_generated" / "logs"
        _write_jsonl(
            logs / "transcript_full.jsonl",
            [
                {"step_index": 0, "type": "USER_INPUT", "content": "<USER_REQUEST>CLI 預覽測試提問內容</USER_REQUEST>"},
                {"step_index": 1, "type": "PLANNER_RESPONSE", "content": "CLI 預覽測試回覆內容，長度足夠觸發正常輸出。" * 8},
            ],
        )
        p_cfg = subprocess.run(
            [sys.executable, "-B", str(SCRIPTS_DIR / "rehydrate_after_compact.py"), "--cwd", str(self.ws), "show-config"],
            text=True,
            capture_output=True,
            encoding="utf-8",
            check=True,
        )
        self.assertTrue(json.loads(p_cfg.stdout)["adaptive"])

        p_ins = subprocess.run(
            [
                sys.executable,
                "-B",
                str(SCRIPTS_DIR / "rehydrate_after_compact.py"),
                "--cwd",
                str(self.ws),
                "inspect",
                str(logs / "transcript_full.jsonl"),
            ],
            text=True,
            capture_output=True,
            encoding="utf-8",
            check=True,
        )
        ins_data = json.loads(p_ins.stdout)
        self.assertTrue(ins_data["simulated"])
        self.assertEqual(ins_data["session_mode"], "completed_qa")

    def test_fast_path_hook_skips_heavy_imports_and_uses_s_flag(self) -> None:
        logs = self.tmp_root / "brain" / "cid-fast" / ".system_generated" / "logs"
        rows = [
            {"step_index": 0, "type": "USER_INPUT", "content": "<USER_REQUEST>normal query</USER_REQUEST>"},
            {"step_index": 1, "type": "PLANNER_RESPONSE", "content": "normal answer"},
        ]
        _write_jsonl(logs / "transcript.jsonl", rows)
        _write_jsonl(logs / "transcript_full.jsonl", rows)
        payload = json.dumps({"transcriptPath": str(logs / "transcript.jsonl"), "workspacePaths": [str(self.ws)]})

        probe_code = "\n".join(
            [
                "import io, sys",
                f"sys.path.insert(0, {repr(str(SCRIPTS_DIR))})",
                "import rehydrate_after_compact as m",
                f"sys.stdin = io.StringIO({repr(payload)})",
                "rc = m.run_hook()",
                "assert rc == 0",
                "for legacy in ('_RE_MOD', '_PATH_CLS', '_DATETIME_CLS', '_DEQUE_CLS', '_COMPILED_REGEX'):",
                "    assert legacy not in m.__dict__, f'legacy global {legacy} leaked on entrypoint'",
                "for banned in (",
                "    'dataclasses', 'pathlib', 'datetime', 'argparse', 'urllib', 'unicodedata',",
                "    'rehydrate_helpers', 'rehydrate_engine', 'rehydrate_cli',",
                "):",
                "    assert banned not in sys.modules, f'{banned} imported on normal turn'",
                "import rehydrate_helpers as mh",
                "assert not mh._COMPILED_REGEX and mh._RE_MOD is None and mh._PATH_CLS is None and mh._DATETIME_CLS is None and mh._DEQUE_CLS is None",
            ]
        )
        p_probe = subprocess.run(
            [sys.executable, "-S", "-B", "-c", probe_code],
            text=True,
            capture_output=True,
            encoding="utf-8",
            check=True,
        )
        self.assertEqual(json.loads(p_probe.stdout.strip()), {})

        p_cli = subprocess.run(
            [sys.executable, "-S", "-B", str(SCRIPTS_DIR / "rehydrate_after_compact.py"), "--hook"],
            input=payload,
            text=True,
            capture_output=True,
            encoding="utf-8",
            check=True,
        )
        self.assertEqual(json.loads(p_cli.stdout.strip()), {})

    def test_sync_nonzero_exit_and_async_running_task_resolution(self) -> None:
        ok_py = self.ws / "ok_file.py"
        denied_py = self.ws / "denied_done.py"
        ok_py.write_text("VALUE = 42\n", encoding="utf-8")
        denied_py.write_text("SHOULD_NOT_APPEAR = 999\n", encoding="utf-8")

        # 1. Resolved background tasks, sync non-zero exit vs fake exit string inside stdout,
        #    DONE step with Encountered error, and flat interim assistant merging.
        logs = self.tmp_root / "brain" / "cid-async" / ".system_generated" / "logs"
        tool_calls_batch = [
            {"name": "run_command", "args": {"CommandLine": "false_cmd"}},
            {"name": "run_command", "args": {"CommandLine": "grep_exit1_text"}},
            {"name": "write_file", "args": {"Path": str(denied_py)}},
            {"name": "write_file", "args": {"Path": str(ok_py)}},
            {"name": "run_command", "args": {"CommandLine": "bg_ok_job"}},
            {"name": "run_command", "args": {"CommandLine": "bg_fail_job"}},
        ]
        rows = [
            {"step_index": 0, "type": "USER_INPUT", "content": "<USER_REQUEST>run checks and update ok_file.py</USER_REQUEST>"},
            {"step_index": 1, "type": "PLANNER_RESPONSE", "content": "", "tool_calls": tool_calls_batch},
            {
                "step_index": 2,
                "type": "GENERIC",
                "status": "DONE",
                "content": "Created At: 2026-11-16T21:00:00+08:00\nCompleted At: 2026-11-16T21:00:01+08:00\nCommand exited with code 1.\n\nCommand output:\nfail",
            },
            {
                "step_index": 3,
                "type": "GENERIC",
                "status": "DONE",
                "content": "Created At: 2026-11-16T21:00:02+08:00\nCompleted At: 2026-11-16T21:00:03+08:00\nCommand exited with code 0.\n\nCommand output:\nCommand exited with code 1.",
            },
            {
                "step_index": 4,
                "type": "GENERIC",
                "status": "DONE",
                "content": "Created At: 2026-11-16T21:00:04+08:00\nCompleted At: 2026-11-16T21:00:05+08:00\nEncountered error in step execution: blocked",
            },
            {"step_index": 5, "type": "GENERIC", "status": "DONE", "content": "Wrote ok_file.py"},
            {
                "step_index": 6,
                "type": "GENERIC",
                "status": "RUNNING",
                "content": "Created At: 2026-11-16T21:00:06+08:00\nTool is running as a background task with task id: cid-async/task-1\nTask logs...",
            },
            {
                "step_index": 7,
                "type": "GENERIC",
                "status": "RUNNING",
                "content": "Created At: 2026-11-16T21:00:07+08:00\nTool is running as a background task with task id: cid-async/task-2\nTask logs...",
            },
            {"step_index": 8, "type": "PLANNER_RESPONSE", "content": "Waiting for background tasks."},
            {
                "step_index": 9,
                "type": "SYSTEM_MESSAGE",
                "status": "DONE",
                "content": 'sender=cid-async/task-1 content=Task id "cid-async/task-1" finished with result:\nCommand exited with code 0.\n\nCommand output:\npassed',
            },
            {
                "step_index": 10,
                "type": "SYSTEM_MESSAGE",
                "status": "DONE",
                "content": 'sender=cid-async/task-2 content=Task id "cid-async/task-2" finished with result:\nCommand exited with code 1.\n\nCommand output:\nboom',
            },
            {"step_index": 11, "type": "PLANNER_RESPONSE", "content": "", "tool_calls": [{"name": "run_command", "args": {"CommandLine": "final_echo"}}]},
            {"step_index": 12, "type": "GENERIC", "status": "DONE", "content": "Command exited with code 0."},
            {"step_index": 13, "type": "PLANNER_RESPONSE", "content": "Done after async jobs."},
            {"step_index": 14, "type": "CHECKPOINT", "content": "<CONTEXT_SUMMARY>\n# User Requests\n1. run checks and update ok_file.py\n<summary>Done</summary>\n</CONTEXT_SUMMARY>"},
        ]
        _write_jsonl(logs / "transcript_full.jsonl", rows)
        res = mod.build_rehydration(logs / "transcript_full.jsonl", workspace_roots=[self.ws])
        self.assertIsNotNone(res)
        assert res is not None
        msg, st = res
        self.assertEqual(st["session_mode"], "active_coding")
        self.assertIn("run_command(`false_cmd`) [ERROR]", msg)
        self.assertIn("run_command(`grep_exit1_text`),", msg)
        self.assertNotIn("run_command(`grep_exit1_text`) [ERROR]", msg)
        self.assertIn("write_file(denied_done.py) [ERROR]", msg)
        self.assertIn("run_command(`bg_ok_job`),", msg)
        self.assertNotIn("run_command(`bg_ok_job`) [ERROR]", msg)
        self.assertNotIn("run_command(`bg_ok_job`) [RUNNING]", msg)
        self.assertIn("run_command(`bg_fail_job`) [ERROR]", msg)
        self.assertEqual(msg.count("(Tools run:"), 1)
        self.assertNotIn("SHOULD_NOT_APPEAR", msg)
        self.assertIn("VALUE = 42", msg)

        # 2. Unresolved RUNNING task at compaction preserves mid_turn + [RUNNING]
        logs_run = self.tmp_root / "brain" / "cid-running" / ".system_generated" / "logs"
        rows_run = [
            {"step_index": 0, "type": "USER_INPUT", "content": "<USER_REQUEST>launch background suite</USER_REQUEST>"},
            {"step_index": 1, "type": "PLANNER_RESPONSE", "content": "", "tool_calls": [{"name": "run_command", "args": {"CommandLine": "still_running"}}]},
            {
                "step_index": 2,
                "type": "GENERIC",
                "status": "RUNNING",
                "content": "Tool is running as a background task with task id: cid-running/task-3",
            },
            {"step_index": 3, "type": "PLANNER_RESPONSE", "content": "Launched background suite; waiting for result."},
            {"step_index": 4, "type": "CHECKPOINT", "content": "<CONTEXT_SUMMARY>\n# User Requests\n1. launch background suite\n<summary>Running</summary>\n</CONTEXT_SUMMARY>"},
        ]
        _write_jsonl(logs_run / "transcript_full.jsonl", rows_run)
        res_run = mod.build_rehydration(logs_run / "transcript_full.jsonl", workspace_roots=[self.ws])
        self.assertIsNotNone(res_run)
        assert res_run is not None
        msg_run, st_run = res_run
        self.assertEqual(st_run["session_mode"], "mid_turn")
        self.assertIn("run_command(`still_running`) [RUNNING]", msg_run)

    def test_primary_code_not_evicted_by_wrappers_and_two_turn_window(self) -> None:
        src_py = self.ws / "guardrails.py"
        hj = self.ws / "hooks.json"
        ibat = self.ws / "install.bat"
        ish = self.ws / "install.sh"
        src_py.write_text("PRIMARY_CODE = True\n", encoding="utf-8")
        hj.write_text("{}\n", encoding="utf-8")
        ibat.write_text("@echo off\n", encoding="utf-8")
        ish.write_text("#!/bin/sh\n", encoding="utf-8")

        logs_a = self.tmp_root / "brain" / "cid-tier" / ".system_generated" / "logs"
        rows_a = [
            {"step_index": 0, "type": "USER_INPUT", "content": "<USER_REQUEST>optimize guardrails and wrappers</USER_REQUEST>"},
            {
                "step_index": 1,
                "type": "PLANNER_RESPONSE",
                "content": "",
                "tool_calls": [
                    {"name": "edit_file", "args": {"Path": str(src_py), "Replacement": "PRIMARY_CODE = True"}},
                    {"name": "edit_file", "args": {"Path": str(hj), "Replacement": "{}"}},
                    {"name": "edit_file", "args": {"Path": str(ibat), "Replacement": "@echo off"}},
                    {"name": "edit_file", "args": {"Path": str(ish), "Replacement": "#!/bin/sh"}},
                ],
            },
            {"step_index": 2, "type": "GENERIC", "status": "DONE", "content": "ok"},
            {"step_index": 3, "type": "GENERIC", "status": "DONE", "content": "ok"},
            {"step_index": 4, "type": "GENERIC", "status": "DONE", "content": "ok"},
            {"step_index": 5, "type": "GENERIC", "status": "DONE", "content": "ok"},
            {"step_index": 6, "type": "PLANNER_RESPONSE", "content": "Updated all 4 files." * 20},
            {"step_index": 7, "type": "CHECKPOINT", "content": "<CONTEXT_SUMMARY>\n# User Requests\n1. optimize guardrails and wrappers\n<summary>Done</summary>\n</CONTEXT_SUMMARY>"},
        ]
        _write_jsonl(logs_a / "transcript_full.jsonl", rows_a)
        res_a = mod.build_rehydration(logs_a / "transcript_full.jsonl", workspace_roots=[self.ws])
        self.assertIsNotNone(res_a)
        assert res_a is not None
        _, st_a = res_a
        self.assertTrue(st_a["file_paths"][0].endswith("guardrails.py"))

        # Turn N-1 primary code edit > 60 steps before checkpoint must survive Turn N cleanup wrappers + docs
        qg = self.ws / "quality_gate.py"
        ub = self.ws / "uninstall.bat"
        us = self.ws / "uninstall.sh"
        tp = self.ws / "tests" / "test_packaging.py"
        rd = self.ws / "README.md"
        tp.parent.mkdir(parents=True, exist_ok=True)
        qg.write_text("QUALITY_GATE_CODE = 1\n", encoding="utf-8")
        ub.write_text("@echo off\n", encoding="utf-8")
        us.write_text("#!/bin/sh\n", encoding="utf-8")
        tp.write_text("class T: pass\n", encoding="utf-8")
        rd.write_text("# README\n" + ("doc line\n" * 40), encoding="utf-8")

        logs_b = self.tmp_root / "brain" / "cid-2turn" / ".system_generated" / "logs"
        rows_b = [
            {"step_index": 100, "type": "USER_INPUT", "content": "<USER_REQUEST>optimize quality_gate.py</USER_REQUEST>"},
            {"step_index": 105, "type": "PLANNER_RESPONSE", "content": "", "tool_calls": [{"name": "edit_file", "args": {"Path": str(qg), "Replacement": "QUALITY_GATE_CODE = 1"}}]},
            {"step_index": 106, "type": "GENERIC", "status": "DONE", "content": "ok"},
            {"step_index": 120, "type": "PLANNER_RESPONSE", "content": "Optimized quality_gate.py." * 15},
            {"step_index": 160, "type": "USER_INPUT", "content": "<USER_REQUEST>remove low-ROI plugins and update uninstall/README</USER_REQUEST>"},
            {
                "step_index": 170,
                "type": "PLANNER_RESPONSE",
                "content": "",
                "tool_calls": [
                    {"name": "edit_file", "args": {"Path": str(ub), "Replacement": "@echo off"}},
                    {"name": "edit_file", "args": {"Path": str(us), "Replacement": "#!/bin/sh"}},
                    {"name": "edit_file", "args": {"Path": str(tp), "Replacement": "class T: pass"}},
                    {"name": "edit_file", "args": {"Path": str(rd), "Replacement": "# README"}},
                ],
            },
            {"step_index": 171, "type": "GENERIC", "status": "DONE", "content": "ok"},
            {"step_index": 172, "type": "GENERIC", "status": "DONE", "content": "ok"},
            {"step_index": 173, "type": "GENERIC", "status": "DONE", "content": "ok"},
            {"step_index": 174, "type": "GENERIC", "status": "DONE", "content": "ok"},
            {"step_index": 182, "type": "PLANNER_RESPONSE", "content": "Cleanup complete." * 15},
            {"step_index": 184, "type": "CHECKPOINT", "content": "<CONTEXT_SUMMARY>\n# User Requests\n1. optimize quality_gate.py\n2. remove low-ROI plugins and update uninstall/README\n<summary>Done</summary>\n</CONTEXT_SUMMARY>"},
        ]
        _write_jsonl(logs_b / "transcript_full.jsonl", rows_b)
        res_b = mod.build_rehydration(logs_b / "transcript_full.jsonl", workspace_roots=[self.ws])
        self.assertIsNotNone(res_b)
        assert res_b is not None
        msg_b, st_b = res_b
        self.assertTrue(any(p.endswith("quality_gate.py") for p in st_b["file_paths"]), st_b["file_paths"])
        self.assertNotIn("README.md", "\n".join(st_b["file_paths"]))
        self.assertIn("QUALITY_GATE_CODE = 1", msg_b)

    def test_markdown_line_safe_folding_and_counter_prompt_dedup(self) -> None:
        long_url = "file:///d:/antigravity/some_very_long_repository_directory/plugins/config.json"
        rich_reply = (
            "Summary sentence opening with clear verdict.\n\n"
            + ("Detail bullet point line that builds up toward the fold threshold.\n" * 6)
            + f"1. **Target Link**: ([config.json]({long_url}))\n\n"
            + "### Metrics Table\n"
            + "| Case | Median |\n"
            + "| :--- | :--- |\n"
            + "| Row 1 | 50 ms |\n"
            + "| Row 2 | 55 ms |\n"
            + "```python\n"
            + ("x = 1234567890\n" * 35)
            + "y = 999\n"
            + "```\n"
            + "Final line after code block."
        )
        logs = self.tmp_root / "brain" / "cid-md" / ".system_generated" / "logs"
        rows = [
            {"step_index": 0, "type": "USER_INPUT", "content": "<USER_REQUEST>開始最佳化</USER_REQUEST>"},
            {"step_index": 1, "type": "PLANNER_RESPONSE", "content": "收到早期開始。"},
            {"step_index": 2, "type": "USER_INPUT", "content": "<USER_REQUEST>中間獨立問題</USER_REQUEST>"},
            {"step_index": 3, "type": "PLANNER_RESPONSE", "content": "中間回答。"},
            {"step_index": 4, "type": "USER_INPUT", "content": "<USER_REQUEST>檢查報表</USER_REQUEST>"},
            {"step_index": 5, "type": "PLANNER_RESPONSE", "content": rich_reply},
            {"step_index": 6, "type": "USER_INPUT", "content": "<USER_REQUEST>開始最佳化</USER_REQUEST>"},
            {"step_index": 7, "type": "PLANNER_RESPONSE", "content": "已完成第二次最佳化。"},
            {
                "step_index": 8,
                "type": "CHECKPOINT",
                "content": (
                    "<CONTEXT_SUMMARY>\n# User Requests\n"
                    "1. 檢查報表\n2. 開始最佳化\n"
                    "<summary>Summary.</summary>\n</CONTEXT_SUMMARY>"
                ),
            },
        ]
        _write_jsonl(logs / "transcript_full.jsonl", rows)
        res = mod.build_rehydration(logs / "transcript_full.jsonl", workspace_roots=[self.ws])
        self.assertIsNotNone(res)
        assert res is not None
        msg, st = res
        self.assertEqual(st["folded_assistant_turns"], 1)
        # Earlier step 0 identical short prompt must NOT be suppressed by step 6 copy inside # User Requests
        self.assertIn("- [#1 @ step 0]: 開始最佳化", msg)
        # Folded reply must not tear the long markdown link mid-URL or leave unclosed ``` fences
        if "[config.json](" in msg:
            self.assertIn(f"[config.json]({long_url})", msg)
        step5_block = msg.split("[Step 5 | Assistant]:", 1)[1].split("[Step 6 | User]:", 1)[0]
        fence_lines = sum(1 for ln in step5_block.splitlines() if ln.lstrip().startswith("```"))
        self.assertEqual(fence_lines % 2, 0, step5_block)

    def test_latest_turn_markdown_safe_clip_and_4500_threshold(self) -> None:
        link_url = "file:///d:/antigravity/agy-rehydrate/tests/test_rehydrate.py"
        link_md = f"[test_rehydrate.py]({link_url})"
        code_target = self.ws / "core.py"
        code_target.write_text("CORE = 1\n", encoding="utf-8")

        # 1. Latest assistant reply of exactly 4,502 chars with link straddling char 3,200 stays unclipped.
        head_4502 = "Opening summary line for active coding reply.\n" + ("Detailed line entry for checkpointing work.\n" * 71)
        self.assertLess(len(head_4502), 3190)
        pad_to_3185 = "x" * (3185 - len(head_4502))
        mid_4502 = f"{pad_to_3185}See {link_md} for tests.\n"
        tail_needed = 4502 - len(head_4502) - len(mid_4502)
        self.assertGreater(tail_needed, 200)
        tail_4502 = ("Tail verification detail line.\n" * (tail_needed // 31))
        tail_4502 += "z" * (4502 - len(head_4502) - len(mid_4502) - len(tail_4502))
        reply_4502 = head_4502 + mid_4502 + tail_4502
        self.assertEqual(len(reply_4502), 4502)

        logs_a = self.tmp_root / "brain" / "cid-4502" / ".system_generated" / "logs"
        _write_jsonl(
            logs_a / "transcript_full.jsonl",
            [
                {"step_index": 0, "type": "USER_INPUT", "content": "<USER_REQUEST>run edits</USER_REQUEST>"},
                {"step_index": 1, "type": "PLANNER_RESPONSE", "content": "", "tool_calls": [{"name": "edit_file", "args": {"Path": str(code_target), "Replacement": "CORE = 1"}}]},
                {"step_index": 2, "type": "GENERIC", "status": "DONE", "content": "ok " + ("log line\n" * 10000)},
                {"step_index": 3, "type": "PLANNER_RESPONSE", "content": reply_4502},
                {"step_index": 4, "type": "CHECKPOINT", "content": "<CONTEXT_SUMMARY>\n# User Requests\n1. run edits\n<summary>Done</summary>\n</CONTEXT_SUMMARY>"},
            ],
        )
        res_a = mod.build_rehydration(logs_a / "transcript_full.jsonl", workspace_roots=[self.ws])
        self.assertIsNotNone(res_a)
        assert res_a is not None
        msg_a, _ = res_a
        self.assertNotIn("turn clipped", msg_a)
        self.assertIn(link_md, msg_a)

        # 2. Oversized user (~5,800 chars) & latest assistant (~6,200 chars) turns fold on line boundaries,
        #    keep the link straddling char 3,300 intact, and balance ``` fences across total - 1,100.
        def _make_structured_turn(target_len: int) -> str:
            pre = "Intro line for oversized structured turn.\n" + ("Preamble line with consistent width padding.\n" * 72)
            pre += f"6. **Regression**: check {link_md} thoroughly.\n"
            self.assertGreater(len(pre), 3310)
            self.assertLess(len(pre), 3430)
            mid = "Mid section line between link and code fence.\n" * 35
            code_lines = ["```python\n"] + [f"val_{i:03d} = {i * 10}\n" for i in range(60)] + ["```\n"]
            code_blk = "".join(code_lines)
            post = "Final closing line after fenced block."
            cur = len(pre) + len(mid) + len(code_blk) + len(post)
            if cur < target_len:
                extra_lines = (target_len - cur) // 46 + 1
                mid += "Mid section line between link and code fence.\n" * extra_lines
            return pre + mid + code_blk + post

        user_big = _make_structured_turn(5800)
        asst_big = _make_structured_turn(6200)
        self.assertGreater(len(user_big), 5700)
        self.assertGreater(len(asst_big), 6100)

        logs_b = self.tmp_root / "brain" / "cid-bigturn" / ".system_generated" / "logs"
        _write_jsonl(
            logs_b / "transcript_full.jsonl",
            [
                {"step_index": 0, "type": "USER_INPUT", "content": f"<USER_REQUEST>{user_big}</USER_REQUEST>"},
                {"step_index": 1, "type": "PLANNER_RESPONSE", "content": "", "tool_calls": [{"name": "run_command", "args": {"CommandLine": "bg"}}]},
                {"step_index": 2, "type": "GENERIC", "status": "RUNNING", "content": "Tool is running as a background task with task id: cid-bigturn/t1\n" + ("x" * 135000)},
                {"step_index": 3, "type": "PLANNER_RESPONSE", "content": asst_big},
                {"step_index": 4, "type": "CHECKPOINT", "content": "<CONTEXT_SUMMARY>\n# User Requests\n1. big turn\n<summary>Running</summary>\n</CONTEXT_SUMMARY>"},
            ],
        )
        res_b = mod.build_rehydration(logs_b / "transcript_full.jsonl", workspace_roots=[self.ws])
        self.assertIsNotNone(res_b)
        assert res_b is not None
        msg_b, st_b = res_b
        self.assertEqual(st_b["session_mode"], "mid_turn")
        user_blk = msg_b.split("[Step 0 | User]:", 1)[1].split("[Step 3 | Assistant]:", 1)[0]
        asst_blk = msg_b.split("[Step 3 | Assistant]:", 1)[1].split("## ", 1)[0]
        for label, blk in (("user", user_blk), ("assistant", asst_blk)):
            self.assertIn("...[turn clipped (", blk, label)
            self.assertIn(link_md, blk, label)
            self.assertIn("Final closing line after fenced block.", blk, label)
            fences = sum(1 for ln in blk.splitlines() if ln.lstrip().startswith("```"))
            self.assertEqual(fences % 2, 0, f"{label} unbalanced fences:\n{blk}")

    def test_multi_workspace_config_and_reordered_jsonl_keys(self) -> None:
        ws1 = self.tmp_root / "ws_primary"
        ws2 = self.tmp_root / "ws_secondary"
        (ws1 / ".agents").mkdir(parents=True, exist_ok=True)
        (ws2 / ".agents").mkdir(parents=True, exist_ok=True)

        # Secondary workspace applies when primary has no .agents/rehydrate.json
        (ws2 / ".agents" / "rehydrate.json").write_text(
            json.dumps({"maxModifiedFiles": 5, "maxReferencedFiles": 2}),
            encoding="utf-8",
        )
        cfg_sec_only = mod.load_rehydrate_config([ws1, ws2])
        self.assertEqual(cfg_sec_only.max_modified_files, 5)
        self.assertEqual(cfg_sec_only.max_referenced_files, 2)

        # Primary workspace overrides conflicting keys in secondary while preserving non-conflicting keys
        (ws1 / ".agents" / "rehydrate.json").write_text(
            json.dumps({"maxModifiedFiles": 4}),
            encoding="utf-8",
        )
        cfg_merged = mod.load_rehydrate_config([ws1, ws2])
        self.assertEqual(cfg_merged.max_modified_files, 4)
        self.assertEqual(cfg_merged.max_referenced_files, 2)

        p_cli = subprocess.run(
            [
                sys.executable,
                "-B",
                str(SCRIPTS_DIR / "rehydrate_after_compact.py"),
                "--cwd",
                str(ws1),
                "--cwd",
                str(ws2),
                "show-config",
            ],
            text=True,
            capture_output=True,
            encoding="utf-8",
            check=True,
        )
        cli_cfg = json.loads(p_cli.stdout)
        self.assertEqual(cli_cfg["workspace"], str(ws1.resolve()))
        self.assertEqual(cli_cfg["workspaces"], [str(ws1.resolve()), str(ws2.resolve())])
        self.assertEqual(cli_cfg["maxModifiedFiles"], 4)
        self.assertEqual(cli_cfg["maxReferencedFiles"], 2)

        # JSONL key-order independence: place "content" first (>2,500 chars) and "step_index" at very end
        target_py = ws2 / "mod_target.py"
        target_py.write_text("REORDERED_OK = 1\n", encoding="utf-8")
        long_ok_tool = "Created At: 2026-11-16T21:00:00+08:00\nCompleted At: 2026-11-16T21:00:01+08:00\nCommand exited with code 0.\n\nCommand output:\n" + (
            "ok payload line\n" * 200
        )
        long_err_tool = "Created At: 2026-11-16T21:00:02+08:00\nCompleted At: 2026-11-16T21:00:03+08:00\nCommand exited with code 1.\n\nCommand output:\n" + (
            "err payload line\n" * 200
        )
        long_write_ok = "Wrote mod_target.py.\n\n" + ("write detail line\n" * 180)
        std_rows = [
            {"step_index": 0, "type": "USER_INPUT", "content": "<USER_REQUEST>run checks and update mod_target.py</USER_REQUEST>"},
            {
                "step_index": 1,
                "type": "PLANNER_RESPONSE",
                "content": "",
                "tool_calls": [
                    {"name": "run_command", "args": {"CommandLine": "ok_cmd"}},
                    {"name": "run_command", "args": {"CommandLine": "fail_cmd"}},
                    {"name": "edit_file", "args": {"Path": str(target_py), "Replacement": "REORDERED_OK = 1"}},
                ],
            },
            {"step_index": 2, "type": "GENERIC", "status": "DONE", "content": long_ok_tool},
            {"step_index": 3, "type": "GENERIC", "status": "DONE", "content": long_err_tool},
            {"step_index": 4, "type": "GENERIC", "status": "DONE", "content": long_write_ok},
            {"step_index": 5, "type": "PLANNER_RESPONSE", "content": "Finished edits and checks."},
            {
                "step_index": 6,
                "type": "CHECKPOINT",
                "content": "<CONTEXT_SUMMARY>\n# User Requests\n1. run checks and update mod_target.py\n<summary>Done</summary>\n</CONTEXT_SUMMARY>",
            },
        ]

        def _reorder_row(row: dict) -> dict:
            order = ("content", "tool_calls", "status", "type", "step_index")
            reordered = {k: row[k] for k in order if k in row}
            for k, v in row.items():
                if k not in reordered:
                    reordered[k] = v
            return reordered

        logs_std = self.tmp_root / "brain" / "cid-std" / ".system_generated" / "logs"
        logs_reo = self.tmp_root / "brain" / "cid-reo" / ".system_generated" / "logs"
        _write_jsonl(logs_std / "transcript_full.jsonl", std_rows)
        _write_jsonl(logs_reo / "transcript_full.jsonl", [_reorder_row(r) for r in std_rows])

        res_std = mod.build_rehydration(logs_std / "transcript_full.jsonl", workspace_roots=[ws1, ws2])
        res_reo = mod.build_rehydration(logs_reo / "transcript_full.jsonl", workspace_roots=[ws1, ws2])
        self.assertIsNotNone(res_std)
        self.assertIsNotNone(res_reo)
        assert res_std is not None and res_reo is not None
        msg_std, st_std = res_std
        msg_reo, st_reo = res_reo
        self.assertEqual(msg_reo, msg_std)
        self.assertEqual(
            {k: v for k, v in st_reo.items() if k != "window_raw_chars"},
            {k: v for k, v in st_std.items() if k != "window_raw_chars"},
        )
        self.assertGreater(st_reo["window_raw_chars"], 10000)
        self.assertGreater(st_std["window_raw_chars"], 10000)
        self.assertIn("run_command(`ok_cmd`),", msg_reo)
        self.assertNotIn("run_command(`ok_cmd`) [ERROR]", msg_reo)
        self.assertIn("run_command(`fail_cmd`) [ERROR]", msg_reo)
        self.assertIn("REORDERED_OK = 1", msg_reo)

        # Also verify scan_compact_tail handles trailing step_index cleanly on full & incremental scans
        cp_found, max_found, off1 = mod.scan_compact_tail(logs_reo / "transcript_full.jsonl", 0, None)
        self.assertEqual(cp_found, 6)
        self.assertEqual(max_found, 6)
        with open(logs_reo / "transcript_full.jsonl", "a", encoding="utf-8") as af:
            af.write(json.dumps({"content": "<USER_REQUEST>next prompt</USER_REQUEST>", "type": "USER_INPUT", "step_index": 7}) + "\n")
        cp2, max2, _ = mod.scan_compact_tail(logs_reo / "transcript_full.jsonl", off1, cp_found)
        self.assertEqual(cp2, 6)
        self.assertEqual(max2, 7)

    def test_line_endings_and_no_pycache(self) -> None:
        self.assertEqual(list(PLUGIN_SRC.rglob("__pycache__")), [])
        hooks_data = json.loads((PLUGIN_SRC / "hooks.json").read_text(encoding="utf-8"))
        cmd = hooks_data["agy-rehydrate"]["PreInvocation"][0]["command"]
        self.assertIn(" -S -B scripts/", cmd)
        for path in REPO_ROOT.rglob("*"):
            if not path.is_file() or ".tmp_" in str(path) or "__pycache__" in path.parts or ".git" in path.parts:
                continue
            raw = path.read_bytes()
            suf = path.suffix.lower()
            if suf == ".bat":
                stripped = raw.replace(b"\r\n", b"")
                self.assertNotIn(b"\n", stripped, f"Expected CRLF line endings in {path}")
            elif suf in {".sh", ".py", ".json", ".md", ".svg"}:
                self.assertNotIn(b"\r", raw, f"Expected LF line endings in {path}")


if __name__ == "__main__":
    unittest.main()
