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

    def test_duplicate_long_user_fold_fence_safe_budget_cap_and_ask_question(self) -> None:
        # 1. Verify _split_checkpoint_user_requests handles CP-666-style item with embedded 1..4 sublists
        #    followed immediately by `(N+1). 3. ...` and `(N+2). /compact`.
        long_step10 = (
            "**整體評估：架構與測試均已完整。**\n\n"
            "### 1. 第一節重點\n"
            + ("重點說明與效能數據細節列。\n" * 95)
            + "\n### 2. 已完成項目\n"
            + "1. **子項一**\n   * 詳細內容甲\n"
            + "2. **子項二**\n   * 詳細內容乙\n"
            + "3. **子項三**\n   * 詳細內容丙\n"
            + "4. **子項四**\n   * 詳細內容丁\n\n"
            + "### 3. 後續建議\n"
            + "1. **建議一**\n   * 細節戊\n"
            + "2. **建議二**\n   * 細節己\n"
            + "3. **建議三**\n   * 細節庚\n"
            + "4. **建議四**\n   * 細節辛\n\n"
            + "結論收尾確認。"
        )
        self.assertGreater(len(long_step10), 1200)
        cp_block = (
            "1. 前期獨立問題\n"
            f"2. \n{long_step10}\n"
            "3. 3. 剩餘小細節：檢查同步\n"
            "4. /compact \n"
        )
        split_items = mod._split_checkpoint_user_requests(cp_block)
        self.assertEqual([no for no, _ in split_items], [1, 2, 3, 4])

        # 2. End-to-end test: ask_question in < verbatim_start_step (## 1) and >= verbatim_start_step (pinned in
        #    >6-step tool chain with >2,048-byte GENERIC step), plus duplicate long user prompt folding in ## 2.
        app_py = self.ws / "app.py"
        app_py.write_text("APP_READY = True\n", encoding="utf-8")

        logs = self.tmp_root / "brain" / "cid-edge" / ".system_generated" / "logs"
        rows: list[dict] = [
            {"step_index": 0, "type": "USER_INPUT", "content": "<USER_REQUEST>初始評估請求</USER_REQUEST>"},
            {
                "step_index": 1,
                "type": "PLANNER_RESPONSE",
                "content": "請先確認插件範圍。",
                "tool_calls": [
                    {
                        "name": "ask_question",
                        "args": {"questions": [{"is_multi_select": False, "question": "是否刪除舊插件？", "options": ["A1", "A2"]}]},
                    }
                ],
            },
            {
                "step_index": 2,
                "type": "GENERIC",
                "status": "DONE",
                "content": "Created At: 2026-11-17T00:00:00+08:00\nCompleted At: 2026-11-17T00:00:01+08:00\nA1: (Recommended) 刪除並保留核心兩支",
            },
            {"step_index": 3, "type": "PLANNER_RESPONSE", "content": "已完成早期清理。"},
            {"step_index": 4, "type": "USER_INPUT", "content": "<USER_REQUEST>前期獨立問題</USER_REQUEST>"},
            {"step_index": 5, "type": "PLANNER_RESPONSE", "content": "前期獨立回覆。"},
            {"step_index": 10, "type": "USER_INPUT", "content": f"<USER_REQUEST>{long_step10}</USER_REQUEST>"},
        ]

        # 10-call tool loop between steps 11 and 30 where call #5 is ask_question with >2,200-char GENERIC output
        step_cursor = 11
        for call_no in range(1, 11):
            if call_no == 1:
                tc = {"name": "edit_file", "args": {"Path": str(app_py), "Replacement": "APP_READY = True"}}
                res_txt = "Edited app.py"
            elif call_no == 5:
                tc = {
                    "name": "ask_question",
                    "args": {"questions": [{"is_multi_select": False, "question": "確認拆分方案？", "options": ["A1", "A2"]}]},
                }
                res_txt = (
                    "Created At: 2026-11-17T00:10:00+08:00\n"
                    "Completed At: 2026-11-17T00:10:05+08:00\n"
                    "A1: (Recommended) 拆分為四模組；"
                    + ("補充架構說明文字 " * 260)
                )
            else:
                tc = {"name": "run_command", "args": {"CommandLine": f"check_{call_no}"}}
                res_txt = f"Command exited with code 0.\n\nCommand output:\nok_{call_no}"
            rows.append({"step_index": step_cursor, "type": "PLANNER_RESPONSE", "content": "", "tool_calls": [tc]})
            step_cursor += 1
            rows.append({"step_index": step_cursor, "type": "GENERIC", "status": "DONE", "content": res_txt})
            step_cursor += 1

        # step_cursor is now 31
        rows.extend(
            [
                {"step_index": 31, "type": "PLANNER_RESPONSE", "content": "已依選擇完成四模組拆分。"},
                {"step_index": 32, "type": "USER_INPUT", "content": "<USER_REQUEST>3. 剩餘小細節：檢查同步</USER_REQUEST>"},
                {"step_index": 33, "type": "PLANNER_RESPONSE", "content": "已完成同步檢查。"},
                {"step_index": 34, "type": "USER_INPUT", "content": "<USER_REQUEST>/compact</USER_REQUEST>"},
                {
                    "step_index": 35,
                    "type": "CHECKPOINT",
                    "content": f"<CONTEXT_SUMMARY>\n# User Requests\n{cp_block}<summary>Done.</summary>\n</CONTEXT_SUMMARY>",
                },
            ]
        )
        _write_jsonl(logs / "transcript_full.jsonl", rows)
        res = mod.build_rehydration(logs / "transcript_full.jsonl", workspace_roots=[self.ws])
        self.assertIsNotNone(res)
        assert res is not None
        msg, st = res

        sec1 = msg.split("## 1.", 1)[1].split("## 2.", 1)[0]
        self.assertIn("初始評估請求", sec1)
        self.assertIn("[ask_question] 是否刪除舊插件？ -> A1: (Recommended) 刪除並保留核心兩支", sec1)
        self.assertNotIn("確認拆分方案？", sec1)
        self.assertNotIn("整體評估：架構與測試均已完整", sec1)
        self.assertNotIn("剩餘小細節：檢查同步", sec1)

        self.assertEqual(st["folded_user_turns"], 1)
        sec2 = msg.split("## 2.", 1)[1]
        self.assertIn("...[user prompt already verbatim in <CONTEXT_SUMMARY> # User Requests;", sec2)
        self.assertIn("(+", msg)
        self.assertIn("ask_question(確認拆分方案？ -> A1: (Recommended) 拆分為四模組；", msg)

        # 3. Fence-safe _enforce_total_cap & _clip_markdown_to_budget across odd fence splits
        file_a = "### [Modified] `a.py`\n```python\n" + "".join(f"a_{i:03d} = {i}\n" for i in range(60)) + "```\n"
        file_b = "### [Modified] `b.py`\n```python\n" + "".join(f"b_{i:03d} = {i}\n" for i in range(40)) + "```"
        sec_hdr = "## 2. Restored Active Workspace Files"
        raw_secs = [
            "[POST-COMPACT REHYDRATION v1 | checkpoint=1]",
            "Preamble",
            "## 1. User\n- [#1 @ step 0]: prompt",
            f"{sec_hdr}\n{file_a}\n{file_b}",
        ]
        raw_len = len("\n\n".join(raw_secs))
        base_prefix_len = len("\n\n".join(raw_secs[:3])) + 2 + len(sec_hdr) + 1
        for cap_delta in (350, 520, 750):
            target_cap = raw_len - cap_delta
            capped = mod._enforce_total_cap(raw_secs, target_cap)
            joined = "\n\n".join(capped)
            self.assertLessEqual(len(joined), target_cap)
            fences = sum(1 for ln in joined.splitlines() if ln.lstrip().startswith("```"))
            self.assertEqual(fences % 2, 0, f"Unbalanced fences at delta={cap_delta}:\n{joined}")
        tight_cap = base_prefix_len + 180
        capped_tight = mod._enforce_total_cap(raw_secs, tight_cap)
        joined_tight = "\n\n".join(capped_tight)
        self.assertLessEqual(len(joined_tight), tight_cap)
        self.assertEqual(
            sum(1 for ln in joined_tight.splitlines() if ln.lstrip().startswith("```")) % 2,
            0,
            joined_tight,
        )

    def test_record_read_touch_range_merging_preserves_open_bounds(self) -> None:
        p = str(self.ws / "lines_demo.py")

        store1: dict = {}
        mod._record_read_touch(store1, p, 1, (None, 50))
        mod._record_read_touch(store1, p, 2, (100, 200))
        self.assertEqual(store1[mod.canonical_path_key(p)][2], (None, 200))

        store2: dict = {}
        mod._record_read_touch(store2, p, 1, (100, 200))
        mod._record_read_touch(store2, p, 2, (None, 50))
        self.assertEqual(store2[mod.canonical_path_key(p)][2], (None, 200))

        store3: dict = {}
        mod._record_read_touch(store3, p, 1, (None, 50))
        mod._record_read_touch(store3, p, 2, (100, None))
        self.assertEqual(store3[mod.canonical_path_key(p)][2], (None, None))

        store4: dict = {}
        mod._record_read_touch(store4, p, 1, (50, None))
        mod._record_read_touch(store4, p, 2, (10, 40))
        self.assertEqual(store4[mod.canonical_path_key(p)][2], (10, None))

        store5: dict = {}
        mod._record_read_touch(store5, p, 1, (20, 50))
        mod._record_read_touch(store5, p, 2, (100, 200))
        self.assertEqual(store5[mod.canonical_path_key(p)][2], (20, 200))

        # End-to-end check: reading (None, 50) then (100, 200) must preserve lines 1..200 (not drop 1..99)
        demo_file = self.ws / "lines_demo.py"
        demo_file.write_text("".join(f"line_{i:03d} = {i}\n" for i in range(1, 251)), encoding="utf-8")
        logs = self.tmp_root / "brain" / "cid-read-merge" / ".system_generated" / "logs"
        rows = [
            {"step_index": 0, "type": "USER_INPUT", "content": "<USER_REQUEST>Inspect lines_demo.py ranges</USER_REQUEST>"},
            {
                "step_index": 1,
                "type": "PLANNER_RESPONSE",
                "content": "Reading first 50 lines then lines 100-200.",
                "tool_calls": [
                    {"name": "view_file", "args": {"AbsolutePath": str(demo_file), "EndLine": 50}},
                    {"name": "view_file", "args": {"AbsolutePath": str(demo_file), "StartLine": 100, "EndLine": 200}},
                ],
            },
            {"step_index": 2, "type": "GENERIC", "status": "DONE", "content": "Viewed lines 1-50."},
            {"step_index": 3, "type": "GENERIC", "status": "DONE", "content": "Viewed lines 100-200."},
            {
                "step_index": 4,
                "type": "CHECKPOINT",
                "content": "<CONTEXT_SUMMARY>\n# User Requests\n1. Inspect lines_demo.py ranges\n<summary>Mid-read.</summary>\n</CONTEXT_SUMMARY>",
            },
        ]
        _write_jsonl(logs / "transcript_full.jsonl", rows)
        res = mod.build_rehydration(logs / "transcript_full.jsonl", workspace_roots=[self.ws])
        self.assertIsNotNone(res)
        assert res is not None
        msg, _ = res
        self.assertIn("lines 1-200", msg)
        self.assertIn("line_001 = 1", msg)
        self.assertIn("line_200 = 200", msg)
        self.assertNotIn("line_240 = 240", msg)

    def test_non_dict_jsonl_rows_and_corrupt_state_do_not_crash(self) -> None:
        brain = self.tmp_root / "brain"
        logs = brain / "cid-nondict" / ".system_generated" / "logs"
        logs.mkdir(parents=True, exist_ok=True)
        code_file = self.ws / "robust.py"
        code_file.write_text("ROBUST_OK = True\n", encoding="utf-8")

        raw_lines = [
            "null",
            "[]",
            "123",
            '"just a string"',
            "true",
            json.dumps({"step_index": True, "type": "CHECKPOINT", "content": "bool step_index"}),
            json.dumps(
                {
                    "step_index": 0,
                    "type": "USER_INPUT",
                    "created_at": "2026-11-18T10:00:00Z",
                    "content": "<USER_REQUEST>Update robust.py safely</USER_REQUEST>",
                }
            ),
            "null",
            json.dumps(
                {
                    "step_index": 1,
                    "type": "PLANNER_RESPONSE",
                    "created_at": "2026-11-18T10:00:05Z",
                    "content": "",
                    "tool_calls": [
                        {"name": "edit_file", "args": {"Path": str(code_file), "Replacement": "ROBUST_OK = True"}}
                    ],
                }
            ),
            "[1, 2, 3]",
            json.dumps({"step_index": 2, "type": "GENERIC", "status": "DONE", "content": "Updated robust.py."}),
            json.dumps(
                {
                    "step_index": 3,
                    "type": "PLANNER_RESPONSE",
                    "created_at": "2026-11-18T10:00:10Z",
                    "content": "Updated robust.py successfully.",
                }
            ),
            json.dumps(
                {
                    "step_index": 4,
                    "type": "CHECKPOINT",
                    "created_at": "2026-11-18T10:00:15Z",
                    "content": "<CONTEXT_SUMMARY>\n# User Requests\n1. Update robust.py safely\n<summary>Done.</summary>\n</CONTEXT_SUMMARY>",
                }
            ),
            "null",
            "[]",
        ]
        for fname in ("transcript.jsonl", "transcript_full.jsonl"):
            (logs / fname).write_text("\n".join(raw_lines) + "\n", encoding="utf-8")

        hist_path = self.tmp_root / "history.jsonl"
        hist_path.write_text(
            "\n".join(
                [
                    "null",
                    "[]",
                    "42",
                    json.dumps({"conversationId": "cid-nondict", "workspace": str(self.ws), "display": "/fork", "timestamp": 1795000000000}),
                ]
            )
            + "\n",
            encoding="utf-8",
        )

        # Direct API calls must all ignore non-dict rows cleanly
        first_info = mod.read_first_step_info(logs / "transcript_full.jsonl")
        self.assertIsNotNone(first_info)
        assert first_info is not None
        self.assertEqual(first_info[0], 0)

        step_hit = mod.find_step_epoch_in_transcript(logs / "transcript_full.jsonl", 3)
        self.assertIsNotNone(step_hit)
        assert step_hit is not None
        self.assertEqual(step_hit[0], 4)

        ws_map, fevents = mod.load_history_fork_hints(brain)
        self.assertIn("cid-nondict", ws_map)
        self.assertEqual(len(fevents), 1)

        cp_found, max_found, _ = mod.scan_compact_tail(logs / "transcript.jsonl", 0, None)
        self.assertEqual(cp_found, 4)
        self.assertEqual(max_found, 4)

        res = mod.build_rehydration(logs / "transcript_full.jsonl", workspace_roots=[self.ws])
        self.assertIsNotNone(res)
        assert res is not None
        msg, st = res
        self.assertEqual(st["checkpoint"], 4)
        self.assertIn("ROBUST_OK = True", msg)

        # Non-dict rehydrate_state.json must not crash hook subprocess or CLI `list`
        state_file = logs / "rehydrate_state.json"
        state_file.write_text("[]", encoding="utf-8")
        payload = json.dumps({"transcriptPath": str(logs / "transcript.jsonl"), "workspacePaths": [str(self.ws)]})
        p_hook = subprocess.run(
            [sys.executable, "-B", str(SCRIPTS_DIR / "rehydrate_after_compact.py"), "--hook"],
            input=payload,
            text=True,
            capture_output=True,
            encoding="utf-8",
            check=True,
        )
        hook_out = json.loads(p_hook.stdout.strip())
        self.assertIn("injectSteps", hook_out)

        fake_home = self.tmp_root / "fake_home"
        fake_logs = fake_home / ".gemini" / "antigravity" / "brain" / "cid-nondict-cli" / ".system_generated" / "logs"
        fake_logs.mkdir(parents=True, exist_ok=True)
        for fname in ("transcript.jsonl", "transcript_full.jsonl"):
            (fake_logs / fname).write_text("\n".join(raw_lines) + "\n", encoding="utf-8")
        (fake_logs / "rehydrate_state.json").write_text("null", encoding="utf-8")
        cli_env = dict(os.environ)
        cli_env["HOME"] = str(fake_home)
        cli_env["USERPROFILE"] = str(fake_home)
        p_status = subprocess.run(
            [
                sys.executable,
                "-B",
                str(SCRIPTS_DIR / "rehydrate_after_compact.py"),
                "status",
                "--limit",
                "5",
            ],
            text=True,
            capture_output=True,
            encoding="utf-8",
            env=cli_env,
            check=True,
        )
        self.assertIn("cid-nondict-cli", p_status.stdout)

    def test_find_step_epoch_reordered_huge_tail_and_fork_scoring(self) -> None:
        brain = self.tmp_root / "brain"

        # 1. Reordered JSONL (>180 KB, step_index at end of line > 2 KB, plus non-dict rows near midpoint)
        reo_logs = brain / "cid-reo-search" / ".system_generated" / "logs"
        reo_logs.mkdir(parents=True, exist_ok=True)
        reo_path = reo_logs / "transcript_full.jsonl"
        reo_lines: list[str] = []
        for s in range(60):
            if s == 30:
                reo_lines.extend(["null"] * 20)
            pad = ("x" * 2800) + f"_step_{s}"
            row_str = json.dumps(
                {
                    "content": pad,
                    "created_at": f"2026-11-18T08:{s // 60:02d}:{s % 60:02d}Z",
                    "type": "PLANNER_RESPONSE",
                    "step_index": s,
                }
            )
            reo_lines.append(row_str)
        reo_path.write_text("\n".join(reo_lines) + "\n", encoding="utf-8")
        self.assertGreater(reo_path.stat().st_size, 160000)

        hit_reo = mod.find_step_epoch_in_transcript(reo_path, 12)
        self.assertIsNotNone(hit_reo)
        assert hit_reo is not None
        self.assertEqual(hit_reo[0], 59)
        self.assertIsNotNone(hit_reo[1])

        # 2. Huge trailing line > 512 KB must NOT falsely treat an earlier target_step as exact_tip
        huge_logs = brain / "cid-huge-tail" / ".system_generated" / "logs"
        huge_logs.mkdir(parents=True, exist_ok=True)
        huge_path = huge_logs / "transcript_full.jsonl"
        huge_rows = [
            {"step_index": 0, "type": "USER_INPUT", "created_at": "2026-11-18T09:00:00Z", "content": "start"},
            {"step_index": 5, "type": "PLANNER_RESPONSE", "created_at": "2026-11-18T09:01:00Z", "content": "mid"},
            {
                "step_index": 6,
                "type": "GENERIC",
                "created_at": "2026-11-18T09:02:00Z",
                "content": "Z" * 620000,
            },
        ]
        _write_jsonl(huge_path, huge_rows)
        hit_mid = mod.find_step_epoch_in_transcript(huge_path, 5)
        self.assertIsNotNone(hit_mid)
        assert hit_mid is not None
        self.assertEqual(hit_mid[0], 6)  # NOT 5 (must not fake exact_tip!)

        # Huge final step itself IS exact_tip when EOF immediately follows
        hit_tip = mod.find_step_epoch_in_transcript(huge_path, 6)
        self.assertIsNotNone(hit_tip)
        assert hit_tip is not None
        self.assertEqual(hit_tip[0], 6)

        # Huge trailing corrupt/non-JSON line after step 5 must return -1 (unknown), NOT 5
        corrupt_tail_path = huge_logs / "transcript_corrupt_tail.jsonl"
        corrupt_tail_path.write_text(
            json.dumps(huge_rows[0]) + "\n" + json.dumps(huge_rows[1]) + "\n" + ("{" + "Q" * 620000) + "\n",
            encoding="utf-8",
        )
        hit_corrupt = mod.find_step_epoch_in_transcript(corrupt_tail_path, 5)
        self.assertIsNotNone(hit_corrupt)
        assert hit_corrupt is not None
        self.assertEqual(hit_corrupt[0], -1)

        # 3. /fork scoring without history `/fork` entry:
        #    Recent parent that kept chatting (exact_tip=0, delta=30s) MUST beat old unrelated exact_tip=1 (delta=3h)
        ws_a = str((self.ws / "proj_a").resolve())
        ws_b = str((self.ws / "proj_b").resolve())
        hist_rows = [
            {"conversationId": "parent-recent-continued", "workspace": ws_a, "display": "msg", "timestamp": 1795000000000},
            {"conversationId": "unrelated-old-exact-tip", "workspace": ws_a, "display": "msg", "timestamp": 1795000000000},
            {"conversationId": "child-fork-score", "workspace": ws_a, "display": "msg", "timestamp": 1795000000000},
            {"conversationId": "unrelated-other-ws", "workspace": ws_b, "display": "msg", "timestamp": 1795000000000},
        ]
        _write_jsonl(self.tmp_root / "history.jsonl", hist_rows)

        p_recent = brain / "parent-recent-continued" / ".system_generated" / "logs"
        _write_jsonl(
            p_recent / "transcript_full.jsonl",
            [
                {"step_index": 0, "type": "USER_INPUT", "created_at": "2026-11-18T09:50:00Z", "content": "init"},
                {"step_index": 9, "type": "PLANNER_RESPONSE", "created_at": "2026-11-18T09:59:30Z", "content": "fork point"},
                {"step_index": 10, "type": "USER_INPUT", "created_at": "2026-11-18T10:05:00Z", "content": "continued later"},
            ],
        )
        p_old = brain / "unrelated-old-exact-tip" / ".system_generated" / "logs"
        _write_jsonl(
            p_old / "transcript_full.jsonl",
            [
                {"step_index": 0, "type": "USER_INPUT", "created_at": "2026-11-18T06:50:00Z", "content": "old"},
                {"step_index": 9, "type": "PLANNER_RESPONSE", "created_at": "2026-11-18T07:00:00Z", "content": "old tip"},
            ],
        )
        # Also create 15 unrelated large step-0 conversations to verify fast resolution
        for idx in range(15):
            u_logs = brain / f"unrelated-noise-{idx:02d}" / ".system_generated" / "logs"
            u_rows = [
                {"step_index": k, "type": "GENERIC", "created_at": f"2026-11-18T05:{k:02d}:00Z", "content": "N" * 4000}
                for k in range(30)
            ]
            _write_jsonl(u_logs / "transcript_full.jsonl", u_rows)
            _write_jsonl(
                u_logs / "transcript.jsonl",
                [{"step_index": k, "type": "GENERIC", "created_at": f"2026-11-18T05:{k:02d}:00Z", "content": "short"} for k in range(30)],
            )

        child_logs = brain / "child-fork-score" / ".system_generated" / "logs"
        _write_jsonl(
            child_logs / "transcript_full.jsonl",
            [
                {"step_index": 10, "type": "USER_INPUT", "created_at": "2026-11-18T10:00:00Z", "content": "forked task"},
                {"step_index": 11, "type": "PLANNER_RESPONSE", "created_at": "2026-11-18T10:00:05Z", "content": "ok"},
            ],
        )

        t0 = time.monotonic()
        ancestors = mod.resolve_fork_ancestors(child_logs / "transcript_full.jsonl")
        elapsed = time.monotonic() - t0
        self.assertLess(elapsed, 2.0)
        self.assertEqual(len(ancestors), 1)
        self.assertEqual(ancestors[0]["cid"], "parent-recent-continued")

        # 4. Unconfirmed stale candidate (>24h) and disjoint-workspace candidate must be rejected;
        #    explicit `/fork` event in history.jsonl MUST still match a multi-day delayed fork
        brain2 = self.tmp_root / "brain_stale"
        p_stale = brain2 / "parent-multiday" / ".system_generated" / "logs"
        _write_jsonl(
            p_stale / "transcript_full.jsonl",
            [
                {"step_index": 0, "type": "USER_INPUT", "created_at": "2026-11-10T10:00:00Z", "content": "day1"},
                {"step_index": 9, "type": "PLANNER_RESPONSE", "created_at": "2026-11-10T10:10:00Z", "content": "day1 tip"},
            ],
        )
        p_other_ws = brain2 / "parent-other-ws" / ".system_generated" / "logs"
        _write_jsonl(
            p_other_ws / "transcript_full.jsonl",
            [
                {"step_index": 0, "type": "USER_INPUT", "created_at": "2026-11-18T09:50:00Z", "content": "other"},
                {"step_index": 9, "type": "PLANNER_RESPONSE", "created_at": "2026-11-18T09:59:45Z", "content": "other tip"},
            ],
        )
        c_stale = brain2 / "child-day8" / ".system_generated" / "logs"
        _write_jsonl(
            c_stale / "transcript_full.jsonl",
            [
                {"step_index": 10, "type": "USER_INPUT", "created_at": "2026-11-18T10:00:00Z", "content": "day8 child"},
            ],
        )
        hist2 = self.tmp_root / "history.jsonl"
        _write_jsonl(
            hist2,
            [
                {"conversationId": "parent-multiday", "workspace": ws_a, "display": "old", "timestamp": 1794305400000},
                {"conversationId": "parent-other-ws", "workspace": ws_b, "display": "other", "timestamp": 1795000000000},
                {"conversationId": "child-day8", "workspace": ws_a, "display": "child", "timestamp": 1795000000000},
            ],
        )
        self.assertEqual(mod.resolve_fork_ancestors(c_stale / "transcript_full.jsonl"), [])

        # Adding an explicit `/fork` event on `parent-multiday` right before child start unlocks multi-day match
        fork_ts_ms = int(mod.parse_iso_epoch("2026-11-18T09:59:55Z") * 1000)
        _write_jsonl(
            hist2,
            [
                {"conversationId": "parent-multiday", "workspace": ws_a, "display": "/fork", "timestamp": fork_ts_ms},
                {"conversationId": "parent-other-ws", "workspace": ws_b, "display": "other", "timestamp": fork_ts_ms},
                {"conversationId": "child-day8", "workspace": ws_a, "display": "child", "timestamp": fork_ts_ms},
            ],
        )
        confirmed = mod.resolve_fork_ancestors(c_stale / "transcript_full.jsonl")
        self.assertEqual(len(confirmed), 1)
        self.assertEqual(confirmed[0]["cid"], "parent-multiday")

    def test_same_turn_multiple_final_replies_and_interrupted_turn_tools(self) -> None:
        worker_py = self.ws / "worker_bg.py"
        worker_py.write_text("WORKER_BG = 1\n", encoding="utf-8")

        # 1. Multiple final replies in the same turn + interrupted turns (tools-only & narration+tools)
        logs = self.tmp_root / "brain" / "cid-multi-reply" / ".system_generated" / "logs"
        rows = [
            {"step_index": 0, "type": "USER_INPUT", "content": "<USER_REQUEST>Start migration step A</USER_REQUEST>"},
            {
                "step_index": 1,
                "type": "PLANNER_RESPONSE",
                "content": "",
                "tool_calls": [{"name": "run_command", "args": {"CommandLine": "migrate_step_a"}}],
            },
            {"step_index": 2, "type": "GENERIC", "status": "DONE", "content": "Command exited with code 0.\n\nok"},
            # User interrupts turn 0 before any assistant reply
            {"step_index": 3, "type": "USER_INPUT", "content": "<USER_REQUEST>Actually run step B with narration</USER_REQUEST>"},
            {
                "step_index": 4,
                "type": "PLANNER_RESPONSE",
                "content": "Starting step B migration now.",
                "tool_calls": [{"name": "run_command", "args": {"CommandLine": "migrate_step_b"}}],
            },
            {"step_index": 5, "type": "GENERIC", "status": "DONE", "content": "Command exited with code 0.\n\nok"},
            # User interrupts turn 3 after narration + tool
            {"step_index": 6, "type": "USER_INPUT", "content": "<USER_REQUEST>Launch async job and summarize twice</USER_REQUEST>"},
            {
                "step_index": 7,
                "type": "PLANNER_RESPONSE",
                "content": "",
                "tool_calls": [{"name": "run_command", "args": {"CommandLine": "async_verify"}}],
            },
            {
                "step_index": 8,
                "type": "GENERIC",
                "status": "RUNNING",
                "content": "Created At: 2026-11-18T10:00:01Z\nTool is running as a background task with task id: cid-multi-reply/task-bg-99\nTask logs...",
            },
            {"step_index": 9, "type": "PLANNER_RESPONSE", "content": "First complete conclusion: ALPHA_VERDICT."},
            {
                "step_index": 10,
                "type": "SYSTEM_MESSAGE",
                "status": "DONE",
                "content": 'sender=cid-multi-reply/task-bg-99 content=Task id "cid-multi-reply/task-bg-99" finished with result:\nCommand exited with code 0.\n\nCommand output:\npassed',
            },
            {"step_index": 11, "type": "PLANNER_RESPONSE", "content": "Follow-up update: BETA_VERDICT."},
            {
                "step_index": 12,
                "type": "CHECKPOINT",
                "content": "<CONTEXT_SUMMARY>\n# User Requests\n1. Start migration step A\n2. Actually run step B with narration\n3. Launch async job and summarize twice\n<summary>Done.</summary>\n</CONTEXT_SUMMARY>",
            },
        ]
        _write_jsonl(logs / "transcript_full.jsonl", rows)
        cfg_all = mod.RehydrateConfig(max_recent_entries=10, explicit_keys=frozenset({"maxRecentEntries"}))
        res = mod.build_rehydration(logs / "transcript_full.jsonl", config=cfg_all, workspace_roots=[self.ws])
        self.assertIsNotNone(res)
        assert res is not None
        msg, _ = res

        # Interrupted tools-only turn preserved
        self.assertIn("[Interrupted Turn Tools]: run_command(`migrate_step_a`)", msg)
        # Interrupted narration+tool turn attaches tool to the flushed assistant entry
        self.assertIn("run_command(`migrate_step_b`)", msg)
        self.assertIn("Starting step B migration now.", msg)
        # Same-turn multiple final replies BOTH preserved with Follow-up marker and single (Tools run:)
        self.assertIn("ALPHA_VERDICT", msg)
        self.assertIn("[Follow-up @ step 11]:", msg)
        self.assertIn("BETA_VERDICT", msg)
        step11_block = msg.split("[Step 11 | Assistant]:", 1)[1]
        self.assertEqual(step11_block.count("(Tools run:"), 1)

        # 2. Mid-turn `/compact` MUST NOT wipe pending_tools or turn_running_task_ids
        logs_mc = self.tmp_root / "brain" / "cid-mid-compact" / ".system_generated" / "logs"
        mc_rows = [
            {"step_index": 0, "type": "USER_INPUT", "content": "<USER_REQUEST>Inspect worker_bg.py and run bg_worker</USER_REQUEST>"},
            {
                "step_index": 1,
                "type": "PLANNER_RESPONSE",
                "content": "",
                "tool_calls": [
                    {"name": "read_file", "args": {"Path": str(worker_py)}},
                    {"name": "run_command", "args": {"CommandLine": "bg_worker"}},
                ],
            },
            {"step_index": 2, "type": "GENERIC", "status": "DONE", "content": "WORKER_BG = 1"},
            {
                "step_index": 3,
                "type": "GENERIC",
                "status": "RUNNING",
                "content": "Created At: 2026-11-18T10:01:00Z\nTool is running as a background task with task id: cid-mid-compact/task-live-77\nTask logs...",
            },
            {"step_index": 4, "type": "USER_INPUT", "content": "<USER_REQUEST>/compact</USER_REQUEST>"},
            {
                "step_index": 5,
                "type": "CHECKPOINT",
                "content": "<CONTEXT_SUMMARY>\n# User Requests\n1. Inspect worker_bg.py and run bg_worker\n<summary>In progress.</summary>\n</CONTEXT_SUMMARY>",
            },
        ]
        _write_jsonl(logs_mc / "transcript_full.jsonl", mc_rows)
        res_mc = mod.build_rehydration(logs_mc / "transcript_full.jsonl", workspace_roots=[self.ws])
        self.assertIsNotNone(res_mc)
        assert res_mc is not None
        msg_mc, st_mc = res_mc
        self.assertTrue(st_mc["in_progress_turn"])
        self.assertEqual(st_mc["session_mode"], "mid_turn")
        self.assertIn("[Pre-Compaction In-Progress Tools]:", msg_mc)
        self.assertIn("read_file(worker_bg.py)", msg_mc)
        self.assertIn("run_command(`bg_worker`) [RUNNING]", msg_mc)

    def test_earlier_timeline_budget_header_cap_stats_and_skipped_eligibility(self) -> None:
        # 1. Earlier user timeline budget omission/clipping must NOT claim "verbatim" in section header
        logs_tl = self.tmp_root / "brain" / "cid-tl-budget" / ".system_generated" / "logs"
        tl_rows: list[dict] = []
        step = 0
        for idx in range(1, 13):
            tl_rows.append(
                {
                    "step_index": step,
                    "type": "USER_INPUT",
                    "content": f"<USER_REQUEST>Requirement batch #{idx:02d}: " + (f"detail_{idx} " * 24) + "</USER_REQUEST>",
                }
            )
            step += 1
            tl_rows.append({"step_index": step, "type": "PLANNER_RESPONSE", "content": f"Handled batch {idx}."})
            step += 1
        tl_rows.append(
            {
                "step_index": step,
                "type": "CHECKPOINT",
                "content": "<CONTEXT_SUMMARY>\n# User Requests\n<summary>Many batches.</summary>\n</CONTEXT_SUMMARY>",
            }
        )
        _write_jsonl(logs_tl / "transcript_full.jsonl", tl_rows)
        cfg_tight_tl = mod.RehydrateConfig(
            budget_all_user_chars=1400,
            max_recent_entries=2,
            explicit_keys=frozenset({"budgetAllUserChars", "maxRecentEntries"}),
        )
        res_tl = mod.build_rehydration(logs_tl / "transcript_full.jsonl", config=cfg_tight_tl, workspace_roots=[self.ws])
        self.assertIsNotNone(res_tl)
        assert res_tl is not None
        msg_tl, st_tl = res_tl
        sec1_hdr = next(ln for ln in msg_tl.splitlines() if ln.startswith("## 1. Earlier User Request Timeline"))
        self.assertNotIn("verbatim", sec1_hdr)
        self.assertIn("earlier prompts kept", sec1_hdr)
        self.assertLess(st_tl["kept_earlier_user_msgs"], st_tl["earlier_user_msgs"])
        self.assertIn("omitted for budget", msg_tl)

        # Single long earlier prompt clipped directly by budget also must not claim "verbatim"
        logs_clip = self.tmp_root / "brain" / "cid-tl-single-clip" / ".system_generated" / "logs"
        single_rows = [
            {
                "step_index": 0,
                "type": "USER_INPUT",
                "content": "<USER_REQUEST>Single huge earlier prompt: " + ("alpha beta gamma " * 120) + "</USER_REQUEST>",
            },
            {"step_index": 1, "type": "PLANNER_RESPONSE", "content": "Ack 0."},
            {"step_index": 2, "type": "USER_INPUT", "content": "<USER_REQUEST>Recent follow-up</USER_REQUEST>"},
            {"step_index": 3, "type": "PLANNER_RESPONSE", "content": "Done follow-up."},
            {
                "step_index": 4,
                "type": "CHECKPOINT",
                "content": "<CONTEXT_SUMMARY>\n# User Requests\n<summary>Single.</summary>\n</CONTEXT_SUMMARY>",
            },
        ]
        _write_jsonl(logs_clip / "transcript_full.jsonl", single_rows)
        cfg_clip = mod.RehydrateConfig(
            budget_all_user_chars=240,
            max_recent_entries=2,
            explicit_keys=frozenset({"budgetAllUserChars", "maxRecentEntries"}),
        )
        res_clip = mod.build_rehydration(logs_clip / "transcript_full.jsonl", config=cfg_clip, workspace_roots=[self.ws])
        self.assertIsNotNone(res_clip)
        assert res_clip is not None
        msg_clip, _ = res_clip
        sec1_clip_hdr = next(ln for ln in msg_clip.splitlines() if ln.startswith("## 1. Earlier User Request Timeline"))
        self.assertNotIn("verbatim", sec1_clip_hdr)

        # 2. _enforce_total_cap stats reconciliation: partial clip drops 1 of 3 files AND full pop drops section to 0
        skill_dir = self.ws / "skills" / "demo-skill"
        skill_dir.mkdir(parents=True, exist_ok=True)
        skill_md = skill_dir / "SKILL.md"
        skill_md.write_text("# Demo Skill\n" + ("Skill instruction line padding here.\n" * 60), encoding="utf-8")
        mod_a = self.ws / "mod_a.py"
        mod_b = self.ws / "mod_b.py"
        mod_c = self.ws / "mod_c.py"
        mod_a.write_text("".join(f'var_1_{j:03d} = "alpha_{j:03d}"\n' for j in range(10)), encoding="utf-8")
        mod_b.write_text("".join(f'var_2_{j:03d} = "beta_padding_line_{j:03d}_abcdef123456"\n' for j in range(90)), encoding="utf-8")
        mod_c.write_text("".join(f'var_3_{j:03d} = "gamma_{j:03d}"\n' for j in range(10)), encoding="utf-8")

        att_files = [
            f"attachment_historical_evidence_{k:02d}_extremely_long_filename_proof_xyz.png"
            for k in range(1, 7)
        ]
        att_lines = "\n".join(f"- [Attachment: {fn} (file:///home/user/upload/{fn})]" for fn in att_files)
        logs_cap = self.tmp_root / "brain" / "cid-cap-stats" / ".system_generated" / "logs"
        cap_rows = [
            {
                "step_index": 0,
                "type": "USER_INPUT",
                "media": [{"uri": f"file:///home/user/upload/{fn}"} for fn in att_files],
                "content": "<USER_REQUEST>Earlier kickoff requirement alpha " + ("req " * 40) + "</USER_REQUEST>",
            },
            {"step_index": 1, "type": "PLANNER_RESPONSE", "content": "Ack earlier kickoff."},
            {
                "step_index": 2,
                "type": "USER_INPUT",
                "content": "<USER_REQUEST>Second milestone requirement beta " + ("req " * 40) + "</USER_REQUEST>",
            },
            {
                "step_index": 3,
                "type": "PLANNER_RESPONSE",
                "content": "Ack second milestone.\n" + ("Detailed older explanation line.\n" * 40),
            },
            {
                "step_index": 4,
                "type": "USER_INPUT",
                "content": "<USER_REQUEST>Update mod_a.py, mod_b.py, mod_c.py per SKILL.md</USER_REQUEST>",
            },
            {
                "step_index": 5,
                "type": "PLANNER_RESPONSE",
                "content": "",
                "tool_calls": [
                    {"name": "view_file", "args": {"AbsolutePath": str(skill_md)}},
                    {"name": "edit_file", "args": {"Path": str(mod_a), "Replacement": "var_1_005"}},
                    {"name": "edit_file", "args": {"Path": str(mod_c), "Replacement": "var_3_005"}},
                    {"name": "edit_file", "args": {"Path": str(mod_b), "Replacement": "var_2_005"}},
                ],
            },
            {"step_index": 6, "type": "GENERIC", "status": "DONE", "content": "Viewed SKILL.md"},
            {"step_index": 7, "type": "GENERIC", "status": "DONE", "content": "Edited mod_a.py"},
            {"step_index": 8, "type": "GENERIC", "status": "DONE", "content": "Edited mod_c.py"},
            {"step_index": 9, "type": "GENERIC", "status": "DONE", "content": "Edited mod_b.py"},
            {
                "step_index": 10,
                "type": "PLANNER_RESPONSE",
                "content": "Completed updates across all three files.\n" + ("Summary sentence. " * 45),
            },
            {
                "step_index": 11,
                "type": "CHECKPOINT",
                "content": (
                    "<CONTEXT_SUMMARY>\n# User Requests\n"
                    f"1. Update mod_a.py, mod_b.py, mod_c.py per SKILL.md\n{att_lines}\n"
                    "<summary>Done.</summary>\n</CONTEXT_SUMMARY>"
                ),
            },
        ]
        _write_jsonl(logs_cap / "transcript_full.jsonl", cap_rows)
        res_cap = mod.build_rehydration(logs_cap / "transcript_full.jsonl", workspace_roots=[self.ws])
        self.assertIsNotNone(res_cap)
        assert res_cap is not None
        msg_cap, st_cap = res_cap
        self.assertLessEqual(len(msg_cap), st_cap["ratio_cap"])
        self.assertEqual(st_cap["files"], len(st_cap["file_paths"]))
        self.assertEqual(st_cap["skills"], len(st_cap["skill_paths"]))
        # Dynamic frame_reserve budgets the long attachment preamble upfront so all 3 files stay inside cap cleanly
        self.assertEqual(st_cap["files"], 3)
        for fp_entry in st_cap["file_paths"]:
            _, raw_p = fp_entry.split(":", 1)
            self.assertIn(f"`{raw_p}`", msg_cap)
        for sp_entry in st_cap["skill_paths"]:
            self.assertIn(f"`{sp_entry}`", msg_cap)

        # Case 2a-overflow: when stale-path notice + 3 files genuinely push past ratio_cap,
        # _enforce_total_cap drops trailing whole file blocks cleanly and reconciles stats to surviving headings
        stale_dir_ref = self.ws / "vanished_legacy_module" / "old_file.py"
        cap_rows_ov = list(cap_rows[:-1]) + [
            {
                "step_index": 11,
                "type": "CHECKPOINT",
                "content": (
                    "<CONTEXT_SUMMARY>\n# User Requests\n"
                    f"1. Update mod_a.py, mod_b.py, mod_c.py per SKILL.md\n{att_lines}\n"
                    f"<summary>Done. Primary ref: `{stale_dir_ref}`.</summary>\n</CONTEXT_SUMMARY>"
                ),
            }
        ]
        # Make mod_a and mod_c slightly larger so workspace_notice + 3 files exceed ratio_cap
        mod_a.write_text("".join(f'var_1_{j:03d} = "alpha_detail_{j:03d}_padding"\n' for j in range(18)), encoding="utf-8")
        mod_c.write_text("".join(f'var_3_{j:03d} = "gamma_detail_{j:03d}_padding"\n' for j in range(18)), encoding="utf-8")
        logs_cap_ov = self.tmp_root / "brain" / "cid-cap-stats-ov" / ".system_generated" / "logs"
        _write_jsonl(logs_cap_ov / "transcript_full.jsonl", cap_rows_ov)
        res_ov = mod.build_rehydration(logs_cap_ov / "transcript_full.jsonl", workspace_roots=[self.ws])
        self.assertIsNotNone(res_ov)
        assert res_ov is not None
        msg_ov, st_ov = res_ov
        self.assertLessEqual(len(msg_ov), st_ov["ratio_cap"])
        self.assertEqual(st_ov["files"], len(st_ov["file_paths"]))
        self.assertLess(st_ov["files"], 3)
        for fp_entry in st_ov["file_paths"]:
            _, raw_p = fp_entry.split(":", 1)
            self.assertIn(f"`{raw_p}`", msg_ov)

        # Case 2b: _enforce_total_cap pops the entire Files section -> files==0 and file_paths==[]
        tiny_ref = self.ws / "tiny_ref.py"
        tiny_ref.write_text("TINY_A = 1\nTINY_B = 2\n", encoding="utf-8")
        logs_drop = self.tmp_root / "brain" / "cid-cap-drop" / ".system_generated" / "logs"
        drop_rows = [
            {
                "step_index": 0,
                "type": "USER_INPUT",
                "media": [{"uri": f"file:///home/user/upload/{fn}"} for fn in att_files],
                "content": "<USER_REQUEST>Read tiny_ref.py and explain thoroughly</USER_REQUEST>",
            },
            {
                "step_index": 1,
                "type": "PLANNER_RESPONSE",
                "content": "",
                "tool_calls": [{"name": "read_file", "args": {"Path": str(tiny_ref)}}],
            },
            {"step_index": 2, "type": "GENERIC", "status": "DONE", "content": "TINY_A = 1"},
            {
                "step_index": 3,
                "type": "PLANNER_RESPONSE",
                "content": "Analysis:\n" + ("Reasonable detailed explanation line.\n" * 28),
            },
            {
                "step_index": 4,
                "type": "CHECKPOINT",
                "content": (
                    "<CONTEXT_SUMMARY>\n# User Requests\n"
                    f"1. Read tiny_ref.py and explain thoroughly\n{att_lines}\n"
                    "<summary>Done.</summary>\n</CONTEXT_SUMMARY>"
                ),
            },
        ]
        _write_jsonl(logs_drop / "transcript_full.jsonl", drop_rows)
        cfg_drop = mod.RehydrateConfig(max_referenced_files=2, explicit_keys=frozenset({"maxReferencedFiles"}))
        res_drop = mod.build_rehydration(logs_drop / "transcript_full.jsonl", config=cfg_drop, workspace_roots=[self.ws])
        self.assertIsNotNone(res_drop)
        assert res_drop is not None
        msg_drop, st_drop = res_drop
        self.assertLessEqual(len(msg_drop), st_drop["ratio_cap"])
        self.assertNotIn(". Restored Active Workspace Files (Live Disk Read)", msg_drop)
        self.assertEqual(st_drop["files"], 0)
        self.assertEqual(st_drop["file_paths"], [])

        # 3. skipped_referenced_files must count ONLY eligible workspace files (exclude .env, binary, missing, outside)
        elig_files = []
        for idx_e in range(1, 5):
            ef = self.ws / f"elig_{idx_e}.py"
            ef.write_text(f"ELIG_{idx_e} = True\n", encoding="utf-8")
            elig_files.append(ef)
        env_secret = self.ws / ".env"
        env_secret.write_text("SECRET=1\n", encoding="utf-8")
        bin_png = self.ws / "logo.png"
        bin_png.write_bytes(b"\x89PNG\r\n\x1a\n")
        missing_py = self.ws / "deleted_missing.py"
        outside_py = self.tmp_root / "outside_ws.py"
        outside_py.write_text("OUTSIDE = 1\n", encoding="utf-8")

        # Mode A: completed_qa / active_coding with default max_referenced_files=0 -> 2 eligible reads skipped (not 6)
        logs_skip_a = self.tmp_root / "brain" / "cid-skip-elig-a" / ".system_generated" / "logs"
        skip_a_rows = [
            {"step_index": 0, "type": "USER_INPUT", "content": "<USER_REQUEST>Read eligible and ineligible files</USER_REQUEST>"},
            {
                "step_index": 1,
                "type": "PLANNER_RESPONSE",
                "content": "",
                "tool_calls": [
                    {"name": "read_file", "args": {"Path": str(elig_files[0])}},
                    {"name": "read_file", "args": {"Path": str(elig_files[1])}},
                    {"name": "read_file", "args": {"Path": str(env_secret)}},
                    {"name": "read_file", "args": {"Path": str(bin_png)}},
                    {"name": "read_file", "args": {"Path": str(missing_py)}},
                    {"name": "read_file", "args": {"Path": str(outside_py)}},
                ],
            },
            *[
                {"step_index": 2 + i, "type": "GENERIC", "status": "DONE", "content": f"Read result {i}"}
                for i in range(6)
            ],
            {"step_index": 8, "type": "PLANNER_RESPONSE", "content": "Finished reading all targets and analyzed them thoroughly."},
            {
                "step_index": 9,
                "type": "CHECKPOINT",
                "content": "<CONTEXT_SUMMARY>\n# User Requests\n1. Read eligible and ineligible files\n<summary>Done.</summary>\n</CONTEXT_SUMMARY>",
            },
        ]
        _write_jsonl(logs_skip_a / "transcript_full.jsonl", skip_a_rows)
        res_skip_a = mod.build_rehydration(logs_skip_a / "transcript_full.jsonl", workspace_roots=[self.ws])
        self.assertIsNotNone(res_skip_a)
        assert res_skip_a is not None
        _, st_skip_a = res_skip_a
        self.assertEqual(st_skip_a["skipped_referenced_files"], 2)

        # Mode B: mid_turn (in_progress_turn=True) restores 2 of 4 eligible reads -> skipped=2 (not 5)
        logs_skip_b = self.tmp_root / "brain" / "cid-skip-elig-b" / ".system_generated" / "logs"
        skip_b_rows = [
            {"step_index": 0, "type": "USER_INPUT", "content": "<USER_REQUEST>Mid-turn read 4 eligible + 3 ineligible</USER_REQUEST>"},
            {
                "step_index": 1,
                "type": "PLANNER_RESPONSE",
                "content": "Inspecting files mid-turn.",
                "tool_calls": [
                    {"name": "read_file", "args": {"Path": str(elig_files[0])}},
                    {"name": "read_file", "args": {"Path": str(elig_files[1])}},
                    {"name": "read_file", "args": {"Path": str(elig_files[2])}},
                    {"name": "read_file", "args": {"Path": str(elig_files[3])}},
                    {"name": "read_file", "args": {"Path": str(env_secret)}},
                    {"name": "read_file", "args": {"Path": str(missing_py)}},
                    {"name": "read_file", "args": {"Path": str(outside_py)}},
                ],
            },
            *[
                {"step_index": 2 + i, "type": "GENERIC", "status": "DONE", "content": f"Read result {i}"}
                for i in range(7)
            ],
            {
                "step_index": 9,
                "type": "CHECKPOINT",
                "content": "<CONTEXT_SUMMARY>\n# User Requests\n1. Mid-turn read 4 eligible + 3 ineligible\n<summary>Mid-turn.</summary>\n</CONTEXT_SUMMARY>",
            },
        ]
        _write_jsonl(logs_skip_b / "transcript_full.jsonl", skip_b_rows)
        res_skip_b = mod.build_rehydration(logs_skip_b / "transcript_full.jsonl", workspace_roots=[self.ws])
        self.assertIsNotNone(res_skip_b)
        assert res_skip_b is not None
        _, st_skip_b = res_skip_b
        self.assertTrue(st_skip_b["in_progress_turn"])
        self.assertEqual(st_skip_b["files"], 2)
        self.assertEqual(st_skip_b["skipped_referenced_files"], 2)

    def test_split_into_inject_chunks_fence_safety_and_small_max_bytes(self) -> None:
        md_with_internal_h3 = (
            "### [Modified @ step 5] `docs/guide.md`\n"
            "```\n"
            "# Guide Header\n\n"
            "### Internal Heading Inside Code Fence\n"
            "Some markdown paragraph with 中文內容.\n\n"
            "### Second Internal Heading\n"
            "TAIL_MD_MARKER\n"
            "```"
        )
        big_code_lines = [f"def fn_{i:03d}(): return '中文payload_{i:03d}_padding_text'" for i in range(160)]
        very_long_code_line = "LONG_LINE_START_" + ("漢字A" * 240) + "_LONG_LINE_END"
        big_code_block = (
            "### [Modified @ step 6] `src/big.py`\n"
            "```python\n"
            + "\n".join(big_code_lines[:40])
            + "\n\n"
            + very_long_code_line
            + "\n\n"
            + "\n".join(big_code_lines[40:])
            + "\n```"
        )
        small_atomic_code = "### [Modified @ step 7] `src/tiny.py`\n```python\nTINY_TOP = 1\nTINY_BOT = 2\n```"
        sections = [
            "[POST-COMPACT REHYDRATION v1 | checkpoint=42]",
            "Preamble text explaining ground truth.",
            f"## 1. Restored Active Workspace Files (Live Disk Read)\n{md_with_internal_h3}\n\n{big_code_block}\n\n{small_atomic_code}",
        ]

        for max_b in (500, 1200, 3000, 4096, 8000):
            chunks = mod.split_into_inject_chunks(sections, max_bytes=max_b, checkpoint=42)
            self.assertGreater(len(chunks), 1, f"Expected multiple chunks for max_bytes={max_b}")
            for idx, ch in enumerate(chunks, 1):
                utf8_len = len(ch.encode("utf-8"))
                self.assertLessEqual(
                    utf8_len,
                    max_b,
                    f"Chunk {idx}/{len(chunks)} exceeded max_bytes={max_b} ({utf8_len} bytes)",
                )
                fence_cnt = sum(1 for ln in ch.splitlines() if ln.lstrip().startswith("```"))
                self.assertEqual(
                    fence_cnt % 2,
                    0,
                    f"Unbalanced code fences in chunk {idx}/{len(chunks)} at max_bytes={max_b}:\n{ch}",
                )
                if idx > 1:
                    self.assertTrue(ch.startswith(mod.CONT_MARKER_PREFIX))
            joined_all = "\n".join(chunks)
            self.assertIn("Internal Heading Inside Code Fence", joined_all)
            self.assertIn("TAIL_MD_MARKER", joined_all)
            self.assertIn("LONG_LINE_START_", joined_all)
            self.assertIn("_LONG_LINE_END", joined_all)
            self.assertIn("fn_159", joined_all)
            # Small code block (<= limit) must remain intact within a single chunk
            self.assertTrue(any(("TINY_TOP = 1" in c and "TINY_BOT = 2" in c) for c in chunks))

    def test_eligible_first_mode_and_budget_driven_assistant_folding(self) -> None:
        # 1. Editing only an out-of-workspace scratch script or deleted file must NOT trigger false active_coding
        brain_cid = self.tmp_root / "brain" / "cid-scratch-qa"
        logs = brain_cid / ".system_generated" / "logs"
        scratch_script = brain_cid / "scratch" / "repro_check.mjs"
        scratch_script.parent.mkdir(parents=True, exist_ok=True)
        scratch_script.write_text("console.log('ntfs repro');\n", encoding="utf-8")

        penultimate_reply = (
            "已實測復現 3 項相容性問題：\n"
            "1. sqlite_cache.mjs: wal size=12392, mtimeNs=1795011363933579200, "
            "ctimeNs=1795011363976603700 -> 1795011364017939600 (reads=1..4)。\n"
            + ("DOM 320x36 與 sqlite-cache.test.mjs#L22-L24 詳細對照證據。\n" * 40)
            + "4. errors.mjs 屬功能強化，非 Bug 復現。"
        )
        self.assertGreater(len(penultimate_reply), 2100)
        final_reply = "純文字簡體中文版本：\n" + ("問題描述段落。\n" * 60)

        rows = [
            {"step_index": 0, "type": "USER_INPUT", "content": "<USER_REQUEST>早期初始化設定</USER_REQUEST>"},
            {"step_index": 1, "type": "PLANNER_RESPONSE", "content": "ok " + ("x" * 160000)},
            {"step_index": 10, "type": "USER_INPUT", "content": "<USER_REQUEST>確認問題是否能復現</USER_REQUEST>"},
            {
                "step_index": 11,
                "type": "PLANNER_RESPONSE",
                "content": "",
                "tool_calls": [{"name": "edit_file", "args": {"Path": str(scratch_script), "Replacement": "console.log('ntfs repro');"}}],
            },
            {"step_index": 12, "type": "GENERIC", "status": "DONE", "content": "ok"},
            {"step_index": 13, "type": "PLANNER_RESPONSE", "content": penultimate_reply},
            {"step_index": 14, "type": "USER_INPUT", "content": "<USER_REQUEST>再給我一次純文字版</USER_REQUEST>"},
            {"step_index": 15, "type": "PLANNER_RESPONSE", "content": final_reply},
            {
                "step_index": 16,
                "type": "CHECKPOINT",
                "content": "<CONTEXT_SUMMARY>\n# User Requests\n1. 確認問題是否能復現\n2. 再給我一次純文字版\n<summary>QA done.</summary>\n</CONTEXT_SUMMARY>",
            },
        ]
        _write_jsonl(logs / "transcript_full.jsonl", rows)
        res = mod.build_rehydration(logs / "transcript_full.jsonl", workspace_roots=[self.ws], artifact_dir=brain_cid)
        self.assertIsNotNone(res)
        assert res is not None
        msg, st = res
        self.assertEqual(st["session_mode"], "completed_qa")
        self.assertEqual(st["files"], 0)
        # Generous eff_recent_budget keeps penultimate empirical reply 100% verbatim instead of blindly folding
        self.assertEqual(st["folded_assistant_turns"], 0)
        self.assertIn("1795011363976603700 -> 1795011364017939600", msg)
        self.assertIn("DOM 320x36", msg)

        # 2. When remaining budget is tight, dynamic folding preserves proportionally more than static 420+220
        tight_cfg = mod.RehydrateConfig(
            budget_recent_turns_chars=2400,
            explicit_keys=frozenset({"budgetRecentTurnsChars"}),
        )
        res_tight = mod.build_rehydration(
            logs / "transcript_full.jsonl", config=tight_cfg, workspace_roots=[self.ws], artifact_dir=brain_cid
        )
        self.assertIsNotNone(res_tight)
        assert res_tight is not None
        msg_tight, st_tight = res_tight
        self.assertEqual(st_tight["folded_assistant_turns"], 1)
        self.assertIn("...[assistant details summarized in <CONTEXT_SUMMARY>;", msg_tight)
        step13_blk = msg_tight.split("[Step 13 | Assistant]:", 1)[1].split("[Step 14 | User]:", 1)[0]
        self.assertGreater(len(step13_blk), 1100)

    def test_workspace_reality_stale_paths_external_edits_and_manifest(self) -> None:
        live_keep = self.ws / "agy-rehydrate-main"
        live_keep.mkdir(parents=True, exist_ok=True)
        existing_mod = live_keep / "engine.py"
        existing_mod.write_text("ENGINE_VER = 2\n", encoding="utf-8")
        # Set mtime clearly in the future relative to the transcript step created_at
        future_ts = time.time() + 300.0
        os.utime(existing_mod, (future_ts, future_ts))

        vanished_a = self.ws / "agy-capsule-merged" / "sidecars" / "server.mjs"
        vanished_b = self.ws / "agy-capsule-merged" / "sidecars" / "sqlite_cache.mjs"
        vanished_ref = self.ws / "agy-capsule-1.1.0" / "sidecars" / "server.mjs"
        outside_missing = self.tmp_root / "outside_deleted_secret.txt"
        secret_env = self.ws / ".env"
        denied_missing = self.ws / "denied_never_written.py"

        logs = self.tmp_root / "brain" / "cid-stale-ws" / ".system_generated" / "logs"
        rows: list[dict] = [
            {"step_index": 0, "type": "USER_INPUT", "created_at": "2026-11-18T10:00:00Z", "content": "<USER_REQUEST>編輯舊目錄與 engine.py</USER_REQUEST>"},
            {
                "step_index": 1,
                "type": "PLANNER_RESPONSE",
                "created_at": "2026-11-18T10:00:02Z",
                "content": "",
                "tool_calls": [
                    {"name": "edit_file", "args": {"Path": str(vanished_a), "Replacement": "a"}},
                    {"name": "edit_file", "args": {"Path": str(vanished_b), "Replacement": "b"}},
                    {"name": "edit_file", "args": {"Path": str(existing_mod), "Replacement": "ENGINE_VER = 2"}},
                    {"name": "read_file", "args": {"Path": str(outside_missing)}},
                    {"name": "read_file", "args": {"Path": str(secret_env)}},
                    {"name": "edit_file", "args": {"Path": str(denied_missing), "Replacement": "x"}},
                ],
            },
            {"step_index": 2, "type": "GENERIC", "status": "DONE", "created_at": "2026-11-18T10:00:03Z", "content": "ok"},
            {"step_index": 3, "type": "GENERIC", "status": "DONE", "created_at": "2026-11-18T10:00:03Z", "content": "ok"},
            {"step_index": 4, "type": "GENERIC", "status": "DONE", "created_at": "2026-11-18T10:00:03Z", "content": "ok"},
            {"step_index": 5, "type": "GENERIC", "status": "DONE", "created_at": "2026-11-18T10:00:03Z", "content": "ok"},
            {"step_index": 6, "type": "GENERIC", "status": "DONE", "created_at": "2026-11-18T10:00:03Z", "content": "ok"},
            {"step_index": 7, "type": "GENERIC", "status": "ERROR", "created_at": "2026-11-18T10:00:03Z", "content": "Permission denied"},
            {"step_index": 8, "type": "PLANNER_RESPONSE", "content": "編輯完成。"},
        ]
        # Advance > 60 steps and > 4 user turns of pure Q&A so existing_mod falls outside active coding window
        step = 20
        for q_no in range(1, 7):
            rows.append({"step_index": step, "type": "USER_INPUT", "content": f"<USER_REQUEST>討論第 {q_no} 題文案</USER_REQUEST>"})
            rows.append({"step_index": step + 1, "type": "PLANNER_RESPONSE", "content": f"已完成第 {q_no} 題回覆。" * 12})
            step += 15
        rows.append(
            {
                "step_index": step,
                "type": "CHECKPOINT",
                "content": (
                    "<CONTEXT_SUMMARY>\n# User Requests\n1. 討論第 6 題文案\n"
                    f"<summary>Primary: `{self.ws / 'agy-capsule-merged'}`; ref: `{vanished_ref}#L18-L25`.</summary>\n"
                    "</CONTEXT_SUMMARY>"
                ),
            }
        )
        _write_jsonl(logs / "transcript_full.jsonl", rows)
        res = mod.build_rehydration(logs / "transcript_full.jsonl", workspace_roots=[self.ws])
        self.assertIsNotNone(res)
        assert res is not None
        msg, st = res
        self.assertEqual(st["session_mode"], "completed_qa")
        self.assertEqual(st["files"], 0)
        self.assertGreaterEqual(st["stale_paths"], 2)
        self.assertGreaterEqual(st["externally_modified_files"], 1)
        self.assertEqual(st["uninjected_modified_files"], 1)
        self.assertIn("Workspace Reality & File Status Notice", msg)
        # Collapsed missing root dirs + live workspace directory listing
        self.assertIn("agy-capsule-merged", msg)
        self.assertIn("agy-capsule-1.1.0", msg)
        self.assertIn("`agy-rehydrate-main/`", msg)
        # Externally modified & uninjected modified manifest advisory
        self.assertIn("engine.py", msg)
        self.assertIn("Modified on disk since last read/edit", msg)
        self.assertIn("Modified files not injected in this payload", msg)
        # Security gates strictly upheld: outside, .env, and ERROR-status paths NEVER leaked
        self.assertNotIn("outside_deleted_secret.txt", msg)
        self.assertNotIn(".env", msg)
        self.assertNotIn("denied_never_written.py", msg)

    def test_multi_checkpoint_anti_decay_milestones_and_pre_window_paste_folding(self) -> None:
        core_src = self.ws / "core_worker.mjs"
        core_src.write_text("export const WORKER_READY = true;\n", encoding="utf-8")

        cp1 = (
            "<CONTEXT_SUMMARY>\n# User Requests\n1. UI 融合\n<summary>\n"
            "### 2. Done\n"
            "- **Built frosted/native skin switcher (`ui_skins.mjs`, `ui.mjs`)**: Added dual CSS tokens.\n"
            "- **Added `requestPosition` top/bottom option (`settings.mjs`)**: Synced Popover and Sidebar.\n"
            "### 7. Commitments & Constraints\n"
            "- Never touch upstream read-only reference directories.\n"
            "</summary>\n</CONTEXT_SUMMARY>"
        )
        cp2 = (
            "<CONTEXT_SUMMARY>\n# User Requests\n1. 修正快取\n<summary>\n"
            "### 2. Done\n"
            "- **Upgraded `settings.mjs` bigint `ino:mtimeNs:size` cache key (`settings.mjs`)**: Prevented same-ms stale cache.\n"
            "- **Fixed multi-window ~5s restart loop (`startDeckCoordinator`)**: Added lease coordination.\n"
            "### 7. Commitments & Constraints\n"
            "- Never touch upstream read-only reference directories.\n"
            "</summary>\n</CONTEXT_SUMMARY>"
        )
        cp3 = (
            "<CONTEXT_SUMMARY>\n# User Requests\n1. 最後驗證\n<summary>\n"
            "### 2. Done\n"
            "- **Isolated CDP coordinator in `core_worker.mjs`**: Verified 62/62 tests.\n"
            "### 7. Commitments & Constraints\n"
            "- Never touch upstream read-only reference directories.\n"
            "</summary>\n</CONTEXT_SUMMARY>"
        )

        old_raw_dump = "raw_terminal_output_line_with_no_markdown_headings " * 55
        current_win_prompt = "本次壓縮視窗內的新需求細節：" + ("保留完整規格說明文字。" * 55)
        self.assertGreater(len(old_raw_dump), 2200)
        self.assertGreater(len(current_win_prompt), 600)

        logs = self.tmp_root / "brain" / "cid-multicp" / ".system_generated" / "logs"
        rows = [
            {"step_index": 0, "type": "USER_INPUT", "content": f"<USER_REQUEST>{old_raw_dump}</USER_REQUEST>"},
            {"step_index": 1, "type": "PLANNER_RESPONSE", "content": "收到舊 log。"},
            {"step_index": 10, "type": "CHECKPOINT", "content": cp1},
            {"step_index": 20, "type": "USER_INPUT", "content": "<USER_REQUEST>修正 settings.mjs 快取</USER_REQUEST>"},
            {"step_index": 21, "type": "PLANNER_RESPONSE", "content": "已修正。"},
            {"step_index": 30, "type": "CHECKPOINT", "content": cp2},
            # Edit core_worker.mjs inside current compaction window (> prev_cp_step=30), 75 steps before cp3 (step 110)
            {"step_index": 34, "type": "USER_INPUT", "content": f"<USER_REQUEST>{current_win_prompt}</USER_REQUEST>"},
            {
                "step_index": 35,
                "type": "PLANNER_RESPONSE",
                "content": "",
                "tool_calls": [{"name": "edit_file", "args": {"Path": str(core_src), "Replacement": "export const WORKER_READY = true;"}}],
            },
            {"step_index": 36, "type": "GENERIC", "status": "DONE", "content": "ok " + ("z" * 60000)},
            {"step_index": 37, "type": "PLANNER_RESPONSE", "content": "已更新 core_worker.mjs。"},
            # Two more diagnostic turns burning steps up to 110 (within 4 user turns of step 34!)
            {"step_index": 70, "type": "USER_INPUT", "content": "<USER_REQUEST>跑第一次驗證</USER_REQUEST>"},
            {"step_index": 75, "type": "PLANNER_RESPONSE", "content": "第一次驗證通過。"},
            {"step_index": 105, "type": "USER_INPUT", "content": "<USER_REQUEST>跑最後驗證</USER_REQUEST>"},
            {"step_index": 108, "type": "PLANNER_RESPONSE", "content": "最後驗證 62/62 通過。"},
            {"step_index": 110, "type": "CHECKPOINT", "content": cp3},
        ]
        _write_jsonl(logs / "transcript_full.jsonl", rows)
        res = mod.build_rehydration(logs / "transcript_full.jsonl", workspace_roots=[self.ws])
        self.assertIsNotNone(res)
        assert res is not None
        msg, st = res
        # 1. Same-window turn-aware recency keeps core_worker.mjs active even at 75 steps (> 60)
        self.assertEqual(st["session_mode"], "active_coding")
        self.assertTrue(any(p.endswith("core_worker.mjs") for p in st["file_paths"]))
        self.assertIn("WORKER_READY = true", msg)
        # 2. Anti-decay milestone index recovers vanished cp1 & cp2 milestones with exact line pointers
        self.assertGreaterEqual(st["decayed_milestones"], 3)
        self.assertIn("Earlier Checkpoints Milestone Index", msg)
        self.assertIn("[cp @ step 10, line 3]:", msg)
        self.assertIn("[cp @ step 30, line 6]:", msg)
        self.assertIn("ui_skins.mjs", msg)
        self.assertIn("startDeckCoordinator", msg)
        # Constraint still present in cp3 must NOT be duplicated in the anti-decay index
        ms_sec = msg.split("Earlier Checkpoints Milestone Index", 1)[1].split("## ", 1)[0]
        self.assertNotIn("Never touch upstream read-only", ms_sec)
        # 3. Pre-window raw dump folded while current-window prompt stays verbatim
        self.assertGreaterEqual(st["folded_paste_msgs"], 1)
        self.assertIn("folded", msg.split("Earlier User Request Timeline", 1)[1].split("## ", 1)[0])
        self.assertIn(current_win_prompt, msg)

    def test_delayed_post_auto_compaction_and_file_block_cap_trimming(self) -> None:
        # 1. Mid-turn auto-compaction followed by > FRESH_CHECKPOINT_MAX_GAP live steps must suppress stale Section 2
        logs = self.tmp_root / "brain" / "cid-delayed-cp" / ".system_generated" / "logs"
        cp1 = (
            "<CONTEXT_SUMMARY>\n# User Requests\n1. 初始設計\n<summary>\n"
            "### 2. Done\n- **Implemented `theme_tokens.mjs` dark/light palette**: Verified.\n"
            "</summary>\n</CONTEXT_SUMMARY>"
        )
        cp2 = (
            "<CONTEXT_SUMMARY>\n# User Requests\n1. 中途任務\n<summary>\n"
            "### 2. Done\n- **Finished worker migration**: Verified.\n"
            "</summary>\n</CONTEXT_SUMMARY>"
        )
        rows = [
            {"step_index": 0, "type": "USER_INPUT", "content": "<USER_REQUEST>早期重要架構需求</USER_REQUEST>"},
            {"step_index": 1, "type": "PLANNER_RESPONSE", "content": "收到。"},
            {"step_index": 5, "type": "CHECKPOINT", "content": cp1},
            {"step_index": 10, "type": "USER_INPUT", "content": "<USER_REQUEST>中途任務</USER_REQUEST>"},
            {
                "step_index": 11,
                "type": "PLANNER_RESPONSE",
                "content": "",
                "tool_calls": [{"name": "run_command", "args": {"CommandLine": "mid_turn_check"}}],
            },
            {"step_index": 12, "type": "GENERIC", "status": "DONE", "content": "ok"},
            {"step_index": 13, "type": "CHECKPOINT", "content": cp2},
            # Post-checkpoint live steps continue 25 steps beyond checkpoint 13 before next user turn
            {"step_index": 38, "type": "PLANNER_RESPONSE", "content": "Live post-checkpoint final reply."},
        ]
        _write_jsonl(logs / "transcript_full.jsonl", rows)
        res = mod.build_rehydration(logs / "transcript_full.jsonl", workspace_roots=[self.ws])
        self.assertIsNotNone(res)
        assert res is not None
        msg, st = res
        self.assertTrue(st["delayed_post_cp"])
        self.assertEqual(st["delayed_gap"], 25)
        self.assertFalse(st["in_progress_turn"])
        self.assertEqual(st["kept_turns"], 0)
        self.assertIn("delayed_gap=25", msg)
        self.assertNotIn("[Pre-Compaction In-Progress Tools]", msg)
        self.assertNotIn("Recent Pre-Compaction", msg)
        # Earlier user history & decayed cp1 milestone are still preserved!
        self.assertIn("早期重要架構需求", msg)
        self.assertIn("theme_tokens.mjs", msg)

        # Verify `--hook` end-to-end when `rehydrate_state.json` already exists from cp 5
        _write_jsonl(logs / "transcript.jsonl", rows)
        (logs / "rehydrate_state.json").write_text(
            json.dumps({"last_rehydrated_checkpoint": 5, "checked_offset": 0}),
            encoding="utf-8",
        )
        hook_payload = json.dumps({"transcriptPath": str(logs / "transcript.jsonl"), "workspacePaths": [str(self.ws)]})
        p_hook = subprocess.run(
            [sys.executable, "-S", "-B", str(SCRIPTS_DIR / "rehydrate_after_compact.py"), "--hook"],
            input=hook_payload,
            text=True,
            capture_output=True,
            encoding="utf-8",
            check=True,
        )
        hook_res = json.loads(p_hook.stdout.strip())
        self.assertIn("injectSteps", hook_res)
        injected_txt = "\n".join(st_item["systemMessage"]["systemMessage"] for st_item in hook_res["injectSteps"])
        self.assertIn("delayed_gap=25", injected_txt)
        self.assertIn("theme_tokens.mjs", injected_txt)
        self.assertNotIn("Recent Pre-Compaction", injected_txt)

        # 2. _enforce_total_cap drops trailing whole file blocks cleanly instead of shredding middle files
        f1 = "### [Modified @ step 1] `a.py`\n```python\n" + "".join(f"a_{i:02d} = {i}\n" for i in range(25)) + "```"
        f2 = "### [Modified @ step 2] `b.py`\n```python\n" + "".join(f"b_{i:02d} = {i}\n" for i in range(25)) + "```"
        f3 = "### [Modified @ step 3] `c.py`\n```python\n" + "".join(f"c_{i:02d} = {i}\n" for i in range(25)) + "```"
        secs = [
            "[POST-COMPACT REHYDRATION v1 | checkpoint=10]",
            "Preamble.",
            f"## 1. Restored Active Workspace Files (Live Disk Read)\n{f1}\n\n{f2}\n\n{f3}",
        ]
        # Cap tightly enough to force dropping f3 while comfortably keeping f1 + f2 whole
        cap_keep_two = len("\n\n".join(secs[:2])) + 2 + len("## 1. Restored Active Workspace Files (Live Disk Read)\n") + len(f1) + 2 + len(f2) + 20
        trimmed = mod._enforce_total_cap(secs, cap_keep_two)
        joined = "\n\n".join(trimmed)
        self.assertLessEqual(len(joined), cap_keep_two)
        self.assertIn("`a.py`", joined)
        self.assertIn("`b.py`", joined)
        self.assertNotIn("`c.py`", joined)
        self.assertNotIn("truncated ~", joined)
        self.assertEqual(sum(1 for ln in joined.splitlines() if ln.lstrip().startswith("```")) % 2, 0)

    def test_completed_at_timestamp_tolerance_and_structured_workspace_notice_budget(self) -> None:
        # 1. `Completed At:` in GENERIC tool output (both normal JSON and >2048B fast-path) prevents false external-edit alerts
        self_edited = self.ws / "README_self.md"
        fast_edited = self.ws / "README_fast.md"
        ext_edited = self.ws / "README_ext.md"
        for fp in (self_edited, fast_edited, ext_edited):
            fp.write_text("# Doc\ncontent\n", encoding="utf-8")

        completed_ep = mod.parse_iso_epoch("2026-11-19T18:44:12+08:00")
        self.assertIsNotNone(completed_ep)
        assert completed_ep is not None
        os.utime(self_edited, (completed_ep + 0.7, completed_ep + 0.7))
        os.utime(fast_edited, (completed_ep + 1.2, completed_ep + 1.2))
        os.utime(ext_edited, (completed_ep + 25.0, completed_ep + 25.0))

        logs_ts = self.tmp_root / "brain" / "cid-completed-at" / ".system_generated" / "logs"
        rows_ts: list[dict] = [
            {"step_index": 0, "type": "USER_INPUT", "created_at": "2026-11-19T10:43:30Z", "content": "<USER_REQUEST>更新文件</USER_REQUEST>"},
            {
                "step_index": 1,
                "type": "PLANNER_RESPONSE",
                "created_at": "2026-11-19T10:43:34Z",
                "content": "",
                "tool_calls": [
                    {"name": "edit_file", "args": {"Path": str(self_edited), "Replacement": "# Doc"}},
                    {"name": "edit_file", "args": {"Path": str(fast_edited), "Replacement": "# Doc"}},
                    {"name": "edit_file", "args": {"Path": str(ext_edited), "Replacement": "# Doc"}},
                ],
            },
            {
                "step_index": 2,
                "type": "GENERIC",
                "status": "DONE",
                "created_at": "2026-11-19T10:43:43Z",
                "content": (
                    "Created At: 2026-11-19T18:43:43+08:00\n"
                    "Completed At: 2026-11-19T18:44:12+08:00\n"
                    "Made 1 replacement."
                ),
            },
            {
                "step_index": 3,
                "type": "GENERIC",
                "status": "DONE",
                "created_at": "2026-11-19T10:43:45Z",
                "content": (
                    "Created At: 2026-11-19T18:43:45+08:00\n"
                    "Completed At: 2026-11-19T18:44:12+08:00\n"
                    "Made 1 replacement. " + ("x" * 2400)
                ),
            },
            {
                "step_index": 4,
                "type": "GENERIC",
                "status": "DONE",
                "created_at": "2026-11-19T10:43:46Z",
                "content": (
                    "Created At: 2026-11-19T18:43:46+08:00\n"
                    "Completed At: 2026-11-19T18:44:12+08:00\n"
                    "Made 1 replacement."
                ),
            },
            {"step_index": 5, "type": "PLANNER_RESPONSE", "content": "已更新三份文件。"},
        ]
        # Advance beyond active coding window so files are uninjected and checked for external edits
        st_cur = 20
        for q_i in range(1, 7):
            rows_ts.append({"step_index": st_cur, "type": "USER_INPUT", "content": f"<USER_REQUEST>後續討論 {q_i}</USER_REQUEST>"})
            rows_ts.append({"step_index": st_cur + 1, "type": "PLANNER_RESPONSE", "content": f"回覆 {q_i}。" * 10})
            st_cur += 15
        rows_ts.append(
            {
                "step_index": st_cur,
                "type": "CHECKPOINT",
                "content": "<CONTEXT_SUMMARY>\n# User Requests\n1. 後續討論 6\n<summary>Done.</summary>\n</CONTEXT_SUMMARY>",
            }
        )
        _write_jsonl(logs_ts / "transcript_full.jsonl", rows_ts)
        res_ts = mod.build_rehydration(logs_ts / "transcript_full.jsonl", workspace_roots=[self.ws])
        self.assertIsNotNone(res_ts)
        assert res_ts is not None
        msg_ts, st_ts = res_ts
        self.assertEqual(st_ts["externally_modified_files"], 1)
        ext_line = next(
            (ln for ln in msg_ts.splitlines() if "Modified on disk since last read/edit" in ln),
            "",
        )
        self.assertIn("README_ext.md", ext_line)
        self.assertNotIn("README_self.md", ext_line)
        self.assertNotIn("README_fast.md", ext_line)

        # 2. Structured item-level shrinking in `build_workspace_reality_notice` never mid-clips with `read_file`
        for idx_dir in range(8):
            (self.ws / f"live_project_{idx_dir:02d}").mkdir(exist_ok=True)
        missing_summary = " ".join(str(self.ws / f"vanished_repo_{i:02d}" / "src" / "main.py") for i in range(7))
        for cap_c in (190, 260, 360, 600):
            notice_txt, notice_cnts = mod.build_workspace_reality_notice(
                modified_touch={},
                read_touch={},
                path_touch_epoch={},
                target_cp_content=missing_summary,
                workspace_roots=[self.ws],
                injected_file_keys=set(),
                max_chars=cap_c,
            )
            self.assertEqual(notice_cnts["stale_paths"], 7)
            self.assertLessEqual(len(notice_txt), cap_c)
            self.assertNotIn("read_file for full content", notice_txt)
            self.assertNotIn("truncated ~", notice_txt)
            for ln in notice_txt.splitlines():
                self.assertTrue(ln.startswith("- **"), f"Expected clean bullet line, got: {ln!r}")
                self.assertEqual(ln.count("`") % 2, 0, f"Unbalanced backticks in bullet: {ln!r}")

        # 3. Active coding pre-reserves Workspace Reality budget before `eff_files_budget` so Section 2 stays intact
        mod_a = self.ws / "core_a.py"
        mod_b = self.ws / "core_b.py"
        mod_c = self.ws / "core_c.py"
        for idx_f, fp in enumerate((mod_a, mod_b, mod_c)):
            fp.write_text("".join(f"val_{idx_f}_{ln:03d} = '{'x' * 55}'\n" for ln in range(180)), encoding="utf-8")
        logs_ws = self.tmp_root / "brain" / "cid-ws-pre-reserve" / ".system_generated" / "logs"
        rows_ws = [
            {"step_index": 0, "type": "USER_INPUT", "content": "<USER_REQUEST>修改 core_a.py, core_b.py, core_c.py</USER_REQUEST>"},
            {
                "step_index": 1,
                "type": "PLANNER_RESPONSE",
                "created_at": "2026-11-19T10:44:10Z",
                "content": "",
                "tool_calls": [
                    {"name": "edit_file", "args": {"Path": str(mod_a), "Replacement": "val_0_010 ="}},
                    {"name": "edit_file", "args": {"Path": str(mod_b), "Replacement": "val_1_010 ="}},
                    {"name": "edit_file", "args": {"Path": str(mod_c), "Replacement": "val_2_010 ="}},
                ],
            },
            {"step_index": 2, "type": "GENERIC", "status": "DONE", "created_at": "2026-11-19T10:44:12Z", "content": "ok " + ("p" * 45000)},
            {"step_index": 3, "type": "GENERIC", "status": "DONE", "created_at": "2026-11-19T10:44:12Z", "content": "ok " + ("q" * 45000)},
            {"step_index": 4, "type": "GENERIC", "status": "DONE", "created_at": "2026-11-19T10:44:12Z", "content": "ok " + ("r" * 45000)},
            {"step_index": 5, "type": "PLANNER_RESPONSE", "content": "三個檔案皆已修改並驗證完成。"},
            {
                "step_index": 6,
                "type": "CHECKPOINT",
                "content": (
                    "<CONTEXT_SUMMARY>\n# User Requests\n1. 修改 core_a.py, core_b.py, core_c.py\n"
                    f"<summary>Old paths: `{self.ws / 'gone_old_1'}`, `{self.ws / 'gone_old_2'}`.</summary>\n"
                    "</CONTEXT_SUMMARY>"
                ),
            },
        ]
        _write_jsonl(logs_ws / "transcript_full.jsonl", rows_ws)
        res_ws = mod.build_rehydration(logs_ws / "transcript_full.jsonl", workspace_roots=[self.ws])
        self.assertIsNotNone(res_ws)
        assert res_ws is not None
        msg_ws, st_ws = res_ws
        self.assertEqual(st_ws["session_mode"], "active_coding")
        self.assertLessEqual(st_ws["chars"], st_ws["ratio_cap"])
        self.assertIn("Workspace Reality & File Status Notice", msg_ws)
        ws_sec = msg_ws.split("Workspace Reality & File Status Notice", 1)[1].split("## ", 1)[0]
        self.assertIn("Stale / missing paths", ws_sec)
        self.assertIn("Current live entries in", ws_sec)
        self.assertNotIn("read_file for full content", ws_sec)
        self.assertNotIn("truncated ~", ws_sec)
        self.assertEqual(st_ws["files"], 3)

    def test_progress_template_milestones_and_multi_hunk_radius_shrinking(self) -> None:
        import rehydrate_helpers as helpers

        # 1. Antigravity numbered `/compact` template (`### 2. Progress`, `### 3. Key Findings`, `### 4. Errors & Fixes`)
        cp1_progress = (
            "<CONTEXT_SUMMARY>\n# User Requests\n1. Round 1 refactor\n<summary>\n"
            "### 1. Task Overview\n"
            "- Should be ignored as overview.\n"
            "### 2. Progress\n"
            "- **Modified `D:\\workspace\\long-repo-name\\plugins\\core\\scripts\\cache_engine.py`**:\n"
            "  - Added `build_wal_cache_key(stat)` (`L120`) using `ino:mtimeNs:size`.\n"
            "  - Added `delayed_post_cp = True` guard for stale auto-compactions.\n"
            "- **Verification Results**:\n"
            "  - Ran 27 tests in 1.2s, OK.\n"
            "- **Scratch Files Created**:\n"
            "  - `scratch/tmp_probe.py`\n"
            "#### In Progress\n"
            "- Pending UI polish not finished yet.\n"
            "### 3. Key Findings\n"
            "- **Timestamp Semantics**:\n"
            "  - `Completed At:` inside `GENERIC` output reflects real disk write completion.\n"
            "### 4. Errors & Fixes\n"
            "- **Error 1 (stale `wal_index` lock contention)**:\n"
            "  - Initial naive polling retried for 20s and dropped requests.\n"
            "  - **Fix**: Switched `wal_index` to atomic rename with exponential backoff.\n"
            "### 6. Next Steps\n"
            "- Should be ignored as transient next step.\n"
            "### 7. Commitments & Constraints\n"
            "- Keep zero third-party dependencies in runtime scripts.\n"
            "</summary>\n</CONTEXT_SUMMARY>"
        )
        cp2_progress = (
            "<CONTEXT_SUMMARY>\n# User Requests\n1. Round 2 fixes\n<summary>\n"
            "### 2. Progress\n"
            "- **Round 2 Fixes**:\n"
            "  - `D:\\workspace\\long-repo-name\\plugins\\core\\scripts\\ui_renderer.py`:\n"
            "    - Added `render_frosted_toolbar()` and synced sidebar alignment.\n"
            "    - Added `popover_anchor_sync` telemetry counter.\n"
            "### 7. Commitments & Constraints\n"
            "- Keep zero third-party dependencies in runtime scripts.\n"
            "</summary>\n</CONTEXT_SUMMARY>"
        )
        cp3_latest = (
            "<CONTEXT_SUMMARY>\n# User Requests\n1. Release v2\n<summary>\n"
            "### 2. Progress\n"
            "- **Released v2.0**: Tagged release and kept `delayed_post_cp` guard in `cache_engine.py`.\n"
            "### 7. Commitments & Constraints\n"
            "- Keep zero third-party dependencies in runtime scripts.\n"
            "</summary>\n</CONTEXT_SUMMARY>"
        )

        done1, cons1 = helpers.extract_checkpoint_milestones(cp1_progress)
        self.assertGreaterEqual(len(done1), 4)
        self.assertEqual(len(cons1), 1)
        joined1 = "\n".join(done1)
        # Path shortened to basename, nested sub-bullets captured, Error symptom upgraded to Fix
        self.assertIn("`cache_engine.py`", joined1)
        self.assertNotIn("long-repo-name", joined1)
        self.assertIn("build_wal_cache_key", joined1)
        self.assertIn("atomic rename with exponential backoff", joined1)
        self.assertNotIn("naive polling retried", joined1)
        # Transient sections & noise containers strictly excluded
        self.assertNotIn("Ran 27 tests", joined1)
        self.assertNotIn("tmp_probe.py", joined1)
        self.assertNotIn("Pending UI polish", joined1)
        self.assertNotIn("transient next step", joined1)
        for item in done1 + cons1:
            self.assertEqual(item.count("`") % 2, 0, f"Unbalanced backticks in milestone: {item!r}")

        # 3-level nested bullet in cp2 (`Round 2 Fixes` -> `ui_renderer.py` -> leaf)
        done2, _ = helpers.extract_checkpoint_milestones(cp2_progress)
        self.assertTrue(any("`ui_renderer.py`" in d and "render_frosted_toolbar" in d for d in done2))

        # Anti-decay digest across cp1 + cp2 vs cp3: recovers both checkpoints, dedups `delayed_post_cp` & constraint
        digest, cnt = helpers.build_anti_decay_milestone_digest(
            [(10, 11, cp1_progress), (20, 21, cp2_progress)],
            cp3_latest,
            self.ws / "transcript_full.jsonl",
            budget_chars=1200,
        )
        self.assertGreaterEqual(cnt, 4)
        self.assertIn("[cp @ step 10, line 11]:", digest)
        self.assertIn("[cp @ step 20, line 21]:", digest)
        self.assertIn("build_wal_cache_key", digest)
        self.assertIn("render_frosted_toolbar", digest)
        self.assertNotIn("delayed_post_cp", digest)
        self.assertNotIn("zero third-party dependencies", digest)

        # 2. Multi-hunk `read_modified_hunk_window` shrinks context radius (`ctx=4/2/0`) and water-fills before mid-clip
        big_file = self.ws / "multi_hunk_mod.py"
        file_lines = [f"PAD_LINE_{i:04d} = '{'z' * 42}'" for i in range(1, 501)]
        anchors = [40, 130, 230, 330, 440]
        snips: list[str] = []
        for idx_a, ln_no in enumerate(anchors, 1):
            block_lns = [f"def edit_hunk_{idx_a}_part_{j}(): return 'ANCHOR_{idx_a}_{j}_TOKEN'" for j in range(1, 11)]
            file_lines[ln_no - 1 : ln_no - 1 + len(block_lns)] = block_lns
            snips.append("\n".join(block_lns))
        big_file.write_text("\n".join(file_lines) + "\n", encoding="utf-8")

        # Case A: Medium budget where ctx=8 would overflow, but smaller radii (ctx=4/2/0) fit all 5 hunks cleanly
        out_med, note_med = helpers.read_modified_hunk_window(str(big_file), snips, max_chars=4200, context_lines=45)
        self.assertIsNotNone(out_med)
        assert out_med is not None
        self.assertLessEqual(len(out_med), 4200)
        self.assertNotIn("truncated ~", out_med)
        for idx_a in range(1, 6):
            self.assertIn(f"ANCHOR_{idx_a}_1_TOKEN", out_med)
        self.assertIn("hunk lines", note_med)

        # Case B: Tight budget where even ctx=0 exceeds cap -> water-filling keeps all 5 hunk starts visible
        out_tight, _ = helpers.read_modified_hunk_window(str(big_file), snips, max_chars=2400, context_lines=45)
        self.assertIsNotNone(out_tight)
        assert out_tight is not None
        self.assertLessEqual(len(out_tight), 2400)
        self.assertNotIn("truncated ~", out_tight)
        self.assertIn("trimmed from", out_tight)
        for idx_a in range(1, 6):
            self.assertIn(f"ANCHOR_{idx_a}_1_TOKEN", out_tight)

        # 3. Long backtick identifier closure, clean parent labels (no `...:` or half-UUID), & self-anchored child elision
        long_bt = "Updated `test_rehydrate.py`: Added `test_completed_at_timestamp_tolerance_and_structured_workspace_notice_budget`"
        clipped_bt = helpers._clip_milestone_text(long_bt, 90)
        self.assertLessEqual(len(clipped_bt), 90)
        self.assertEqual(clipped_bt.count("`") % 2, 0)
        self.assertIn("`test_completed_at_timestamp_tolerance", clipped_bt)
        self.assertTrue(clipped_bt.endswith("...`"))

        cp_polish = (
            "<CONTEXT_SUMMARY>\n# User Requests\n1. Polish test\n<summary>\n"
            "### 2. Progress\n"
            "- **Round 2 Live `/compact` (`checkpoint=148`) Bug Fixes**:\n"
            "  - `D:\\workspace\\repo\\tests\\test_rehydrate.py`: Added `test_completed_at_timestamp_tolerance_and_structured_workspace_notice_budget`.\n"
            "### 3. Key Findings\n"
            "- **Forensic findings from `9f280ce9-3eeb-4a21-b4b6-4417ba3993c3`**:\n"
            "  - `step 1981` had been misclassified because `step 1971` touched a scratch file.\n"
            "</summary>\n</CONTEXT_SUMMARY>"
        )
        done_pol, _ = helpers.extract_checkpoint_milestones(cp_polish)
        joined_pol = "\n".join(done_pol)
        self.assertIn("`test_completed_at_timestamp_tolerance_and_structured_workspace_notice_budget`", joined_pol)
        self.assertIn("Forensic findings: `step 1981`", joined_pol)
        self.assertNotIn("...:", joined_pol)
        self.assertNotIn("9f280ce9-3eeb-4a21`", joined_pol)
        for it in done_pol:
            self.assertEqual(it.count("`") % 2, 0, f"Unbalanced backticks: {it!r}")

        # 4. Cross-checkpoint edit anchor scoping: re-edited files drop pre-prev_cp anchors; untouched files keep theirs
        re_edited = self.ws / "window_scoped.py"
        untouched = self.ws / "prev_only.py"
        scoped_lines = [f"FILL_{i:03d} = '{'p' * 24}_{i:03d}'" for i in range(1, 321)]
        scoped_lines[89] = "def old_round1_anchor(): return 'OLD_CP1_ANCHOR'"
        scoped_lines[259] = "def new_round2_anchor(): return 'NEW_CP2_ANCHOR'"
        re_edited.write_text("\n".join(scoped_lines) + "\n", encoding="utf-8")
        untouched_lines = [f"KEEP_{i:03d} = '{'q' * 24}_{i:03d}'" for i in range(1, 201)]
        untouched_lines[119] = "def prev_window_anchor(): return 'PREV_ONLY_ANCHOR'"
        untouched.write_text("\n".join(untouched_lines) + "\n", encoding="utf-8")

        logs_sc = self.tmp_root / "brain" / "cid-cp-anchor-scope" / ".system_generated" / "logs"
        sc_rows = [
            {"step_index": 0, "type": "USER_INPUT", "content": "<USER_REQUEST>Round 1 edit</USER_REQUEST>"},
            {
                "step_index": 1,
                "type": "PLANNER_RESPONSE",
                "content": "",
                "tool_calls": [
                    {
                        "name": "edit_file",
                        "args": {
                            "Path": str(re_edited),
                            "Target": "FILL_090 = 'pppppppppppppppppppppppp_090'",
                            "Replacement": "def old_round1_anchor(): return 'OLD_CP1_ANCHOR'",
                        },
                    },
                    {
                        "name": "edit_file",
                        "args": {
                            "Path": str(untouched),
                            "Target": "KEEP_120 = 'qqqqqqqqqqqqqqqqqqqqqqqq_120'",
                            "Replacement": "def prev_window_anchor(): return 'PREV_ONLY_ANCHOR'",
                        },
                    },
                ],
            },
            {"step_index": 2, "type": "GENERIC", "status": "DONE", "content": "Made 1 replacement"},
            {"step_index": 3, "type": "GENERIC", "status": "DONE", "content": "Made 1 replacement"},
            {"step_index": 4, "type": "PLANNER_RESPONSE", "content": "Round 1 done."},
            {"step_index": 5, "type": "CHECKPOINT", "content": "<CONTEXT_SUMMARY><summary>### 2. Progress\n- Round 1 edited both files.</summary></CONTEXT_SUMMARY>"},
            {"step_index": 6, "type": "USER_INPUT", "content": "<USER_REQUEST>Round 2 edit only window_scoped.py</USER_REQUEST>"},
            {
                "step_index": 7,
                "type": "PLANNER_RESPONSE",
                "content": "",
                "tool_calls": [
                    {
                        "name": "edit_file",
                        "args": {
                            "Path": str(re_edited),
                            "Target": "FILL_260 = 'pppppppppppppppppppppppp_260'",
                            "Replacement": "def new_round2_anchor(): return 'NEW_CP2_ANCHOR'",
                        },
                    }
                ],
            },
            {"step_index": 8, "type": "GENERIC", "status": "DONE", "content": "Made 1 replacement. " + ("x" * 160000)},
            {"step_index": 9, "type": "PLANNER_RESPONSE", "content": "Round 2 done."},
            {"step_index": 10, "type": "CHECKPOINT", "content": "<CONTEXT_SUMMARY><summary>### 2. Progress\n- Round 2 updated window_scoped.py.</summary></CONTEXT_SUMMARY>"},
        ]
        _write_jsonl(logs_sc / "transcript_full.jsonl", sc_rows)
        res_sc = mod.build_rehydration(logs_sc / "transcript_full.jsonl", workspace_roots=[self.ws])
        self.assertIsNotNone(res_sc)
        assert res_sc is not None
        msg_sc, st_sc = res_sc
        self.assertEqual(st_sc["files"], 2)
        self.assertIn("NEW_CP2_ANCHOR", msg_sc)
        self.assertNotIn("OLD_CP1_ANCHOR", msg_sc)
        self.assertIn("PREV_ONLY_ANCHOR", msg_sc)

    def test_balanced_multi_file_budget_code_span_scanner_and_fast_tail_scan(self) -> None:
        import rehydrate_helpers as helpers

        # 1. Backtick state machine: tolerate quoted literal backtick inside single-backtick span & handle double-backtick spans
        quoted_bt = (
            'Milestone Clipping Pitfalls Uncovered on Real Data: Using `close_tok = "`"` when '
            '`add_ellipsis=False` on parent headers fabricated half-UUIDs (`` `9f280ce9-3eeb-4a21` ``); '
            'restricting closure fixed it.'
        )
        clipped_q = helpers._clip_milestone_text(quoted_bt, 138)
        self.assertEqual(helpers._find_unclosed_code_span(clipped_q)[0], -1)
        self.assertFalse(clipped_q.endswith("...`"))
        self.assertTrue(clipped_q.endswith("..."))

        dbl_bt = "Observed malformed token `` `9f280ce9-3eeb-4a21-b4b6-4417ba3993c3` `` during replay verification"
        clipped_d = helpers._clip_milestone_text(dbl_bt, 46)
        self.assertEqual(helpers._find_unclosed_code_span(clipped_d)[0], -1)
        self.assertNotIn("``", clipped_d)

        self.assertEqual(
            helpers._compact_parent_milestone_label("Milestone Clipping Pitfalls Uncovered on Real Data"),
            "Milestone Clipping Pitfalls Uncovered on Real Data",
        )
        self.assertEqual(
            helpers._compact_parent_milestone_label(
                "Round 5 Live `/compact` (`checkpoint=407`) Audit & 4-Axis Optimization (this window)"
            ),
            "Round 5 Live `/compact` (`checkpoint=407`) Audit",
        )
        self.assertEqual(
            helpers._clip_milestone_text("Pipeline phase transition -> deferred cleanup stage", 29, add_ellipsis=False),
            "Pipeline phase transition",
        )
        self.assertEqual(
            helpers._clip_milestone_text("Modern compiler backend for C++ template instantiation", 32, add_ellipsis=False),
            "Modern compiler backend for C++",
        )

        # 2. Balanced weighted multi-file Section 5 sharing + compact subsequent gap markers
        def _make_big_py(path: Path, tag: str) -> list[tuple[str, str]]:
            lines = [f"# {tag} header line {i}" for i in range(1, 25)]
            for i in range(25, 420):
                lines.append(f"VAR_{tag}_{i:03d} = '{tag}_payload_value_padding_content_{i:03d}'")
            edits = [
                (f"VAR_{tag}_060 = '{tag}_payload_value_padding_content_060'", f"def {tag}_fn_a(): return '{tag}_EDIT_A'"),
                (f"VAR_{tag}_170 = '{tag}_payload_value_padding_content_170'", f"def {tag}_fn_b(): return '{tag}_EDIT_B'"),
                (f"VAR_{tag}_280 = '{tag}_payload_value_padding_content_280'", f"def {tag}_fn_c(): return '{tag}_EDIT_C'"),
                (f"VAR_{tag}_380 = '{tag}_payload_value_padding_content_380'", f"def {tag}_fn_d(): return '{tag}_EDIT_D'"),
            ]
            text = "\n".join(lines) + "\n"
            for old, new in edits:
                text = text.replace(old, new, 1)
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(text, encoding="utf-8")
            return edits

        core_a = self.ws / "core_alpha.py"
        core_b = self.ws / "core_beta.py"
        test_f = self.ws / "tests" / "test_balanced_suite.py"
        ed_a = _make_big_py(core_a, "ALPHA")
        ed_b = _make_big_py(core_b, "BETA")
        ed_t = _make_big_py(test_f, "TEST")

        logs_bal = self.tmp_root / "brain" / "cid-balanced-files" / ".system_generated" / "logs"
        tcalls = []
        for fpath, edits in ((core_a, ed_a), (core_b, ed_b), (test_f, ed_t)):
            for old, new in edits:
                tcalls.append({"name": "edit_file", "args": {"Path": str(fpath), "Target": old, "Replacement": new}})
        bal_rows = [
            {"step_index": 0, "type": "USER_INPUT", "content": "<USER_REQUEST>Update alpha, beta, and tests</USER_REQUEST>"},
            {"step_index": 1, "type": "PLANNER_RESPONSE", "content": "", "tool_calls": tcalls},
        ]
        for idx_r in range(len(tcalls)):
            pad = ("x" * 160000) if idx_r == 0 else ""
            bal_rows.append({"step_index": 2 + idx_r, "type": "GENERIC", "status": "DONE", "content": "Made 1 replacement. " + pad})
        fin_step = 2 + len(tcalls)
        bal_rows.append({"step_index": fin_step, "type": "PLANNER_RESPONSE", "content": "Updated all three files."})
        bal_rows.append(
            {
                "step_index": fin_step + 1,
                "type": "CHECKPOINT",
                "content": "<CONTEXT_SUMMARY><summary>### 2. Progress\n- Updated core_alpha.py, core_beta.py, and test_balanced_suite.py.</summary></CONTEXT_SUMMARY>",
            }
        )
        _write_jsonl(logs_bal / "transcript_full.jsonl", bal_rows)
        res_bal = mod.build_rehydration(logs_bal / "transcript_full.jsonl", workspace_roots=[self.ws])
        self.assertIsNotNone(res_bal)
        assert res_bal is not None
        msg_bal, st_bal = res_bal
        self.assertEqual(st_bal["files"], 3)
        sec_blocks = msg_bal.split("\n### [Modified @")
        self.assertEqual(len(sec_blocks), 4)
        test_block = next(b for b in sec_blocks[1:] if "test_balanced_suite.py" in b)
        self.assertGreaterEqual(len(test_block), 2200)
        for tag in ("TEST_EDIT_A", "TEST_EDIT_B", "TEST_EDIT_C", "TEST_EDIT_D"):
            self.assertIn(tag, test_block)
        # First internal gap keeps full read_file hint; subsequent internal gaps use compact omitted marker
        self.assertEqual(test_block.count("omitted; read_file for full context"), 1)

        # 3. Single-open incremental fast tail scan (`need_max_step=False`) with & without CHECKPOINT
        tail_log = self.tmp_root / "fast_tail_scan.jsonl"
        _write_jsonl(
            tail_log,
            [
                {"step_index": 0, "type": "USER_INPUT", "content": "hi"},
                {"step_index": 1, "type": "CHECKPOINT", "content": "cp1"},
                {"step_index": 2, "type": "PLANNER_RESPONSE", "content": "ok"},
            ],
        )
        cp0, max0, off0 = mod._scan_compact_tail_str(str(tail_log), 0, None, need_max_step=True)
        self.assertEqual((cp0, max0), (1, 2))
        with tail_log.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps({"step_index": 3, "type": "USER_INPUT", "content": "next turn"}) + "\n")
            fh.write(json.dumps({"step_index": 4, "type": "PLANNER_RESPONSE", "content": "reply"}) + "\n")
        cp1, max1, off1 = mod._scan_compact_tail_str(str(tail_log), off0, cp0, need_max_step=False)
        self.assertEqual((cp1, max1), (1, -1))
        self.assertGreater(off1, off0)
        with tail_log.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps({"step_index": 5, "type": "CHECKPOINT", "content": "cp2"}) + "\n")
        cp2, _, off2 = mod._scan_compact_tail_str(str(tail_log), off1, cp1, need_max_step=False)
        self.assertEqual(cp2, 5)
        self.assertGreater(off2, off1)

        # 4. Pre-resolve fast deny + cached resolve preserves all security boundaries
        rcache: dict[str, Path | None] = {}
        ws_pfx = helpers._prepare_root_prefixes([self.ws], rcache)
        excl_dir = self.ws / "scratch_excl"
        excl_dir.mkdir(exist_ok=True)
        excl_file = excl_dir / "temp.py"
        excl_file.write_text("x = 1\n", encoding="utf-8")
        excl_pfx = helpers._prepare_root_prefixes([excl_dir], rcache)
        env_file = self.ws / ".env"
        env_file.write_text("SECRET=1\n", encoding="utf-8")
        self.assertFalse(
            helpers.is_eligible_text_file(
                str(env_file),
                workspace_roots=[self.ws],
                resolve_cache=rcache,
                _allowed_prefixes=ws_pfx,
                _excluded_prefixes=excl_pfx,
            )
        )
        self.assertFalse(
            helpers.is_eligible_text_file(
                str(excl_file),
                workspace_roots=[self.ws],
                resolve_cache=rcache,
                _allowed_prefixes=ws_pfx,
                _excluded_prefixes=excl_pfx,
            )
        )
        self.assertTrue(
            helpers.is_eligible_text_file(
                str(core_a),
                workspace_roots=[self.ws],
                resolve_cache=rcache,
                _allowed_prefixes=ws_pfx,
                _excluded_prefixes=excl_pfx,
            )
        )

    def test_missing_workspace_roots_fail_closed_to_trusted_roots(self) -> None:
        import rehydrate_helpers as helpers

        # 1. Hook payload without `workspacePaths`/`cwd` must never re-read tool-touched paths outside trusted roots
        brain_cid = self.tmp_root / "brain" / "cid-no-ws-roots"
        logs = brain_cid / ".system_generated" / "logs"
        outside_dir = self.tmp_root / "outside_no_ws_roots"
        outside_src = outside_dir / "billing.py"
        outside_spec = outside_dir / "API_SPEC.md"
        outside_skill = outside_dir / "SKILL.md"
        artifact_plan = brain_cid / "implementation_plan.md"
        artifact_skill = brain_cid / "skills" / "deploy" / "SKILL.md"
        for fpath, marker in (
            (outside_src, "OUTSIDE_SRC_MARKER = 1"),
            (outside_spec, "OUTSIDE_SPEC_MARKER"),
            (outside_skill, "OUTSIDE_SKILL_MARKER"),
            (artifact_plan, "ARTIFACT_PLAN_MARKER"),
            (artifact_skill, "ARTIFACT_SKILL_MARKER"),
        ):
            fpath.parent.mkdir(parents=True, exist_ok=True)
            fpath.write_text(marker + "\n", encoding="utf-8")

        rows = [
            {"step_index": 0, "type": "USER_INPUT", "content": "<USER_REQUEST>依計畫調整 billing.py</USER_REQUEST>"},
            {
                "step_index": 1,
                "type": "PLANNER_RESPONSE",
                "content": "",
                "tool_calls": [
                    {"name": "edit_file", "args": {"Path": str(outside_src), "Replacement": "OUTSIDE_SRC_MARKER = 1"}},
                    {"name": "read_file", "args": {"Path": str(outside_spec)}},
                    {"name": "read_file", "args": {"Path": str(outside_skill)}},
                    {"name": "edit_file", "args": {"Path": str(artifact_plan), "Replacement": "ARTIFACT_PLAN_MARKER"}},
                    {"name": "read_file", "args": {"Path": str(artifact_skill)}},
                ],
            },
            {"step_index": 2, "type": "GENERIC", "status": "DONE", "content": "ok"},
            {"step_index": 3, "type": "GENERIC", "status": "DONE", "content": "ok"},
            {"step_index": 4, "type": "GENERIC", "status": "DONE", "content": "ok"},
            {"step_index": 5, "type": "GENERIC", "status": "DONE", "content": "ok"},
            {"step_index": 6, "type": "GENERIC", "status": "DONE", "content": "ok"},
            {"step_index": 7, "type": "PLANNER_RESPONSE", "content": "已依計畫完成 billing.py 的調整。"},
            {
                "step_index": 8,
                "type": "CHECKPOINT",
                "content": "<CONTEXT_SUMMARY>\n# User Requests\n1. 依計畫調整 billing.py\n<summary>Done</summary>\n</CONTEXT_SUMMARY>",
            },
        ]
        _write_jsonl(logs / "transcript.jsonl", rows)
        _write_jsonl(logs / "transcript_full.jsonl", rows)
        p_hook = subprocess.run(
            [sys.executable, "-S", "-B", str(SCRIPTS_DIR / "rehydrate_after_compact.py"), "--hook"],
            input=json.dumps({"transcriptPath": str(logs / "transcript.jsonl")}),
            text=True,
            capture_output=True,
            encoding="utf-8",
            check=True,
        )
        hook_res = json.loads(p_hook.stdout.strip())
        self.assertIn("injectSteps", hook_res)
        injected_txt = "\n".join(st_item["systemMessage"]["systemMessage"] for st_item in hook_res["injectSteps"])
        # Transcript dialogue plus fixed trusted roots (session artifact dir) are still restored
        self.assertIn("已依計畫完成 billing.py 的調整。", injected_txt)
        self.assertIn("ARTIFACT_PLAN_MARKER", injected_txt)
        self.assertIn("ARTIFACT_SKILL_MARKER", injected_txt)
        # Unknown workspace scope is never treated as unrestricted for workspace files, specs, or skills
        self.assertNotIn("OUTSIDE_SRC_MARKER", injected_txt)
        self.assertNotIn("OUTSIDE_SPEC_MARKER", injected_txt)
        self.assertNotIn("OUTSIDE_SKILL_MARKER", injected_txt)

        # 2. Scope gate itself denies when no trusted root (or an empty allow-list) is supplied
        self.assertTrue(helpers.is_eligible_text_file(str(outside_src), workspace_roots=[outside_dir]))
        self.assertFalse(helpers.is_eligible_text_file(str(outside_src)))
        self.assertFalse(helpers.is_eligible_text_file(str(outside_src), _allowed_prefixes=()))

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
