import json
import os
import tempfile
import time
import unittest
from unittest import mock

from agentviz import sessions as S
from agentviz.agents import classify
from agentviz.model import Builder
from agentviz.procs import ProcInfo, _parse_etime, _split_windows_cmdline
from agentviz.render import Theme, frame
from agentviz.util import parse_ts, truncate, width


class UtilTest(unittest.TestCase):
    def test_wide_chars(self):
        self.assertEqual(width("한글ab"), 6)
        self.assertEqual(width("\x1b[1mab\x1b[0m"), 2)
        t = truncate("에이전트 시각화", 7)
        self.assertLessEqual(width(t), 7)
        self.assertTrue(t.endswith("…"))

    def test_parse_ts(self):
        self.assertAlmostEqual(parse_ts("1970-01-01T00:00:01.5Z"), 1.5)
        self.assertAlmostEqual(parse_ts("1970-01-01T00:00:01.123456789Z"), 1.123456, places=5)
        self.assertEqual(parse_ts(2000), 2000)
        self.assertIsNone(parse_ts("garbage"))

    def test_etime_and_cmdline(self):
        self.assertEqual(_parse_etime("1-02:03:04"), 93784)
        self.assertEqual(_parse_etime("05:06"), 306)
        self.assertEqual(_split_windows_cmdline('"C:\\Program Files\\node.exe" cli.js -p'),
                         ["C:\\Program Files\\node.exe", "cli.js", "-p"])


class ClassifyTest(unittest.TestCase):
    def k(self, name, exe=None, cmd=None):
        kind = classify(name, exe, cmd or [name])
        return kind.key if kind else None

    def test_matches(self):
        self.assertEqual(self.k("claude", "/opt/claude-code/bin/claude"), "claude")
        self.assertEqual(self.k("node", "/usr/bin/node",
                                ["node", "/usr/lib/node_modules/@anthropic-ai/claude-code/cli.js"]), "claude")
        self.assertEqual(self.k("node", None, ["node", "/home/u/.npm-global/bin/claude", "--resume"]), "claude")
        self.assertEqual(self.k("codex-x86_64-unknown-linux-musl"), "codex")
        self.assertEqual(self.k("codex.exe", "C:\\bin\\codex.exe"), "codex")
        self.assertEqual(self.k("node", None, ["node", "/x/@google/gemini-cli/dist/index.js"]), "gemini")
        self.assertEqual(self.k("python3", None, ["python3", "/usr/local/bin/aider", "--model", "x"]), "aider")

    def test_non_matches(self):
        self.assertIsNone(self.k("Claude", "/Applications/Claude.app/Contents/MacOS/Claude"))
        self.assertIsNone(self.k("claude.exe", "C:\\Users\\u\\AppData\\Local\\AnthropicClaude\\claude.exe"))
        self.assertIsNone(self.k("bash", "/bin/bash", ["bash", "-c", "grep claude log.txt"]))
        self.assertIsNone(self.k("python3", None, ["python3", "-m", "agentviz"]))
        self.assertIsNone(self.k("vim", None, ["vim", "claude.md"]))


def _write_jsonl(path, rows):
    with open(path, "w", encoding="utf-8") as f:
        for r in rows:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")


CLAUDE_ROWS = [
    {"type": "user", "timestamp": "2026-01-01T00:00:00Z", "cwd": "/w/proj", "sessionId": "abc",
     "gitBranch": "main", "message": {"role": "user", "content": "버그 고쳐줘"}},
    {"type": "assistant", "timestamp": "2026-01-01T00:00:01Z", "cwd": "/w/proj", "sessionId": "abc",
     "message": {"id": "m1", "model": "claude-x", "role": "assistant",
                 "content": [{"type": "tool_use", "name": "Bash", "input": {"command": "ls -la"}}],
                 "usage": {"input_tokens": 10, "cache_read_input_tokens": 90, "output_tokens": 5}}},
    {"type": "assistant", "timestamp": "2026-01-01T00:00:01Z", "sessionId": "abc",
     "message": {"id": "m1", "role": "assistant", "content": [{"type": "text", "text": "hi"}],
                 "usage": {"input_tokens": 10, "cache_read_input_tokens": 90, "output_tokens": 7}}},
    {"type": "user", "timestamp": "2026-01-01T00:00:02Z", "sessionId": "abc",
     "message": {"role": "user", "content": [{"type": "tool_result", "content": "file.txt"}]}},
    {"type": "user", "timestamp": "2026-01-01T00:00:03Z", "sessionId": "abc",
     "message": {"role": "user", "content": "<system-reminder>ignore</system-reminder>"}},
    {"type": "ai-title", "aiTitle": "버그 수정", "sessionId": "abc"},
]

CODEX_ROWS = [
    {"timestamp": "2026-01-01T00:00:00Z", "type": "session_meta",
     "payload": {"id": "cx1", "cwd": "/w/api", "git": {"branch": "dev"}}},
    {"timestamp": "2026-01-01T00:00:00Z", "type": "turn_context", "payload": {"model": "gpt-5-codex"}},
    {"timestamp": "2026-01-01T00:00:01Z", "type": "response_item",
     "payload": {"type": "message", "role": "user", "content": [{"type": "input_text", "text": "<env>x</env>"}]}},
    {"timestamp": "2026-01-01T00:00:01Z", "type": "response_item",
     "payload": {"type": "message", "role": "user", "content": [{"type": "input_text", "text": "add tests"}]}},
    {"timestamp": "2026-01-01T00:00:02Z", "type": "response_item",
     "payload": {"type": "function_call", "name": "shell",
                 "arguments": json.dumps({"command": ["bash", "-lc", "pytest -q"]})}},
    {"timestamp": "2026-01-01T00:00:03Z", "type": "response_item",
     "payload": {"type": "function_call_output",
                 "output": json.dumps({"output": "1 failed", "metadata": {"exit_code": 1}})}},
    {"timestamp": "2026-01-01T00:00:04Z", "type": "event_msg",
     "payload": {"type": "token_count", "info": {"total_token_usage": {"input_tokens": 500, "output_tokens": 40},
                                                  "last_token_usage": {"input_tokens": 300}}}},
]


class SessionsTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = self.tmp.name
        self.claude = os.path.join(root, "claude")
        self.codex = os.path.join(root, "codex")
        os.makedirs(os.path.join(self.claude, "projects", "-w-proj"))
        os.makedirs(os.path.join(self.codex, "sessions", "2026", "01", "01"))
        self.cpath = os.path.join(self.claude, "projects", "-w-proj", "abc.jsonl")
        self.xpath = os.path.join(self.codex, "sessions", "2026", "01", "01", "rollout-1.jsonl")
        _write_jsonl(self.cpath, CLAUDE_ROWS)
        _write_jsonl(self.xpath, CODEX_ROWS)
        self.env = mock.patch.dict(os.environ, {"CLAUDE_CONFIG_DIR": self.claude, "CODEX_HOME": self.codex})
        self.env.start()

    def tearDown(self):
        self.env.stop()
        self.tmp.cleanup()

    def test_parse_both(self):
        tr = S.SessionTracker(window_minutes=5)
        by_agent = {s.agent: s for s in tr.update()}
        c = by_agent["claude"]
        self.assertEqual((c.session_id, c.cwd, c.title, c.model, c.branch), ("abc", "/w/proj", "버그 수정", "claude-x", "main"))
        self.assertEqual([e.kind for e in c.events], ["user", "tool", "text", "result"])
        self.assertEqual(c.events[1].text, "Bash: ls -la")
        self.assertEqual((c.tokens_in, c.tokens_out, c.context), (100, 7, 100))  # deduped by message id

        x = by_agent["codex"]
        self.assertEqual((x.session_id, x.cwd, x.model, x.branch), ("cx1", "/w/api", "gpt-5-codex", "dev"))
        self.assertEqual([e.kind for e in x.events], ["user", "tool", "error"])
        self.assertEqual(x.events[1].text, "shell: pytest -q")
        self.assertEqual((x.tokens_in, x.tokens_out, x.context), (500, 40, 300))

    def test_incremental_append_and_partial_line(self):
        tr = S.SessionTracker(window_minutes=5)
        tr.update()
        with open(self.cpath, "a", encoding="utf-8") as f:
            line = json.dumps({"type": "user", "timestamp": "2026-01-01T00:01:00Z",
                               "message": {"role": "user", "content": "next"}})
            f.write(line[:20])
            f.flush()
            c = [s for s in tr.update() if s.agent == "claude"][0]
            self.assertEqual(len(c.events), 4)
            f.write(line[20:] + "\n")
        c = [s for s in tr.update() if s.agent == "claude"][0]
        self.assertEqual(c.events[-1].text, "next")


class _FakeSampler:
    def __init__(self, cwds):
        self.cwds = cwds

    def cwd(self, p):
        return self.cwds.get(p.pid)


class BuilderTest(unittest.TestCase):
    def test_folding_linking_and_render(self):
        now = time.time()
        procs = {
            10: ProcInfo(10, 1, "bash", cmdline=["bash"]),
            20: ProcInfo(20, 10, "node", cmdline=["node", "/x/bin/codex"], cpu_percent=1.0, rss=100, create_time=now - 50),
            21: ProcInfo(21, 20, "codex-aarch64-apple-darwin", cpu_percent=30.0, rss=200, create_time=now - 49),
            22: ProcInfo(22, 21, "bash", cmdline=["bash", "-lc", "pytest"], cpu_percent=5.0, rss=50),
            30: ProcInfo(30, 1, "claude", cpu_percent=0.0, rss=300, create_time=now - 100),
        }
        sess = S.Session(agent="codex", path="/p/x.jsonl", session_id="cx", cwd="/w/api", mtime=now - 2)
        sess.add(now - 2, "text", "done")
        old = S.Session(agent="claude", path="/p/old.jsonl", session_id="old", cwd="/w/proj", mtime=now - 3600)
        views = Builder(window_minutes=15).build(procs, _FakeSampler({20: "/w/api", 30: "/w/proj"}), [sess, old])
        self.assertEqual(len(views), 2)
        codex = [v for v in views if v.kind.key == "codex"][0]
        self.assertEqual((codex.pid, codex.nprocs, codex.cpu, codex.rss), (20, 3, 36.0, 350))
        self.assertIs(codex.session, sess)
        self.assertEqual(codex.status, "WORKING")
        claude = [v for v in views if v.kind.key == "claude"][0]
        self.assertIsNone(claude.session)  # transcript predates the process
        self.assertEqual(claude.status, "IDLE")

        for theme in (Theme(color=True), Theme(color=False, ascii_only=True)):
            lines = frame(views, list(sess.events), {sess.path: "api"}, (80, 30), 3, theme, now=now)
            self.assertEqual(len(lines), 30)
            for line in lines:
                self.assertLessEqual(width(line), 79, line)

    def test_hides_desktop_apps_helpers_and_idle_servers(self):
        now = time.time()
        store = "C:\\Program Files\\WindowsApps\\Claude_1.0_x64\\app\\claude.exe"
        procs = {
            # Claude desktop app: Electron main + renderer helper
            100: ProcInfo(100, 1, "claude.exe", exe="C:\\Users\\u\\AppData\\Local\\Claude\\claude.exe",
                          cmdline=["claude.exe"], create_time=now - 99),
            101: ProcInfo(101, 100, "claude.exe", cmdline=["claude.exe", "--type=renderer"]),
            # Microsoft Store install
            110: ProcInfo(110, 1, "claude.exe", exe=store, cmdline=[store]),
            # Codex Windows sandbox helper (not the CLI)
            120: ProcInfo(120, 1, "codex-command-runner.exe", cmdline=["codex-command-runner.exe"]),
            # Codex backend for an IDE extension, no session
            130: ProcInfo(130, 1, "codex.exe", cmdline=["codex.exe", "app-server"], create_time=now - 50),
            # A real terminal Claude Code
            140: ProcInfo(140, 1, "claude.exe", exe="C:\\Users\\u\\.local\\bin\\claude.exe",
                          cmdline=["claude.exe", "--resume"], create_time=now - 10),
        }
        b = Builder()
        views = b.build(procs, _FakeSampler({}), [])
        self.assertEqual([v.pid for v in views], [140])
        reasons = {pid: r for pid, _k, r in b.hidden}
        self.assertEqual(reasons[100], "desktop app (Electron)")
        self.assertIn("background server", reasons[130])
        self.assertNotIn(110, [v.pid for v in views])
        self.assertNotIn(120, reasons)  # not even classified as an agent
        shown_all = {v.pid for v in Builder(show_all=True).build(procs, _FakeSampler({}), [])}
        self.assertTrue({100, 130, 140} <= shown_all)

    def test_dismiss_finished_sessions(self):
        from agentviz.model import AgentView, Dismissed
        from agentviz.agents import KIND_BY_KEY
        now = time.time()
        done = S.Session(agent="codex", path="/p/done.jsonl", session_id="d", mtime=now - 60)
        done.add(now - 60, "text", "finished")
        sub = S.Session(agent="claude", path="/p/sub.jsonl", session_id="s", is_subagent=True, mtime=now - 60)
        live = S.Session(agent="claude", path="/p/live.jsonl", session_id="l", mtime=now)
        views = [AgentView(kind=KIND_BY_KEY["codex"], session=done, subagents=[sub]),
                 AgentView(kind=KIND_BY_KEY["claude"], pid=5, session=live)]
        d = Dismissed()
        self.assertEqual(d.clear_finished(views), 1)  # running agent is kept
        v2, s2 = d.apply(views, [done, sub, live])
        self.assertEqual([v.pid for v in v2], [5])
        self.assertEqual([s.path for s in s2], ["/p/live.jsonl"])  # feed events of cleared ones go too
        done.mtime = now + 1  # session resumed -> shows up again
        self.assertEqual(len(d.apply(views, [done])[0]), 2)
        done.mtime = now - 60
        self.assertEqual(d.restore(), 2)
        self.assertEqual(len(d.apply(views, [done])[0]), 2)

    def test_empty_frame(self):
        lines = frame([], [], {}, (60, 12), 0, Theme(color=False))
        self.assertIn("No active coding agents", "\n".join(lines))


if __name__ == "__main__":
    unittest.main()
