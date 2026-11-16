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


if __name__ == "__main__":
    unittest.main()
