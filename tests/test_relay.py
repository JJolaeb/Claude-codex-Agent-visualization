import json
import os
import stat
import subprocess
import sys
import tempfile
import textwrap
import time
import unittest
from unittest import mock

from agentviz import rules
from agentviz.agents import KIND_BY_KEY
from agentviz.bus import Bus
from agentviz.mcp_server import Server
from agentviz.model import AgentView
from agentviz.relay import WAKE_PROMPT, Relay, RelayConfig, build_command
from agentviz.sessions import Session

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


class RulesTest(unittest.TestCase):
    def test_install_update_remove(self):
        with tempfile.TemporaryDirectory() as d:
            claude_md = os.path.join(d, "CLAUDE.md")
            with open(claude_md, "w", encoding="utf-8") as f:
                f.write("# My project\n\nKeep this.\n")
            self.assertEqual(rules.install(claude_md), "added")
            self.assertEqual(rules.install(claude_md), "unchanged")  # idempotent
            text = open(claude_md, encoding="utf-8").read()
            self.assertTrue(text.startswith("# My project\n\nKeep this.\n\n"))
            self.assertEqual(text.count(rules.START), 1)
            self.assertIn("check_messages", text)
            self.assertEqual(rules.remove(claude_md), "removed")
            self.assertEqual(open(claude_md, encoding="utf-8").read(), "# My project\n\nKeep this.\n")

            agents_md = os.path.join(d, "AGENTS.md")
            self.assertEqual(rules.install(agents_md), "added")  # created from scratch
            self.assertEqual(rules.remove(agents_md), "removed")
            self.assertFalse(os.path.exists(agents_md))  # nothing else was in it
            self.assertEqual(rules.remove(agents_md), "missing")


def view(kind, pid, sid, cwd, age, status="IDLE"):
    s = Session(agent=kind, path=f"/p/{sid}.jsonl", session_id=sid, cwd=cwd, mtime=time.time() - age)
    return AgentView(kind=KIND_BY_KEY[kind], pid=pid, cwd=cwd, session=s, status=status)


class FakeProc:
    _next = 50000

    def __init__(self, argv, **kw):
        FakeProc._next += 1
        self.pid, self.argv, self.kw, self.code = FakeProc._next, argv, kw, None

    def poll(self):
        return self.code

    def kill(self):
        self.code = -9


class RelayUnitTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.bus = Bus(os.path.join(self.tmp.name, "bus"))
        self.bus.announce("codex@api", kind="codex", agent_pid=20, cwd=self.tmp.name)
        self.bus.announce("claude@web", kind="claude", agent_pid=10, cwd=self.tmp.name)
        self.procs = []

        def popen(argv, **kw):
            p = FakeProc(argv, **kw)
            self.procs.append(p)
            return p

        self.relay = Relay(self.bus, RelayConfig(idle_secs=30, grace_secs=0, max_wakes_total=3,
                                                 max_wakes_per_agent=2),
                           popen=popen, which=lambda k: f"/bin/{k}")

    def tearDown(self):
        self.tmp.cleanup()

    def old_message(self, sender, to, text):
        m = self.bus.send(sender, to, text)
        return m

    def test_commands_keep_message_text_off_the_command_line(self):
        c = build_command("claude", "claude", "sid-1", RelayConfig(claude_args=["--model", "x"]))
        self.assertEqual(c[:5], ["claude", "-p", "--resume", "sid-1", "--fork-session"])
        self.assertEqual(c[-2:], ["--", WAKE_PROMPT])
        x = build_command("codex", "codex", "sid-2", RelayConfig(codex_args=["--full-auto"]))
        self.assertEqual(x, ["codex", "exec", "--skip-git-repo-check", "--full-auto", "resume", "sid-2", "--",
                             WAKE_PROMPT])
        for ch in '"&|<>^%\n':
            self.assertNotIn(ch, WAKE_PROMPT)

    def test_wakes_only_idle_agents_once_per_message(self):
        self.bus.send("claude@web", "codex", "please review")
        busy = [view("codex", 20, "cx-1", self.tmp.name, age=2, status="WORKING")]
        self.relay.tick(busy)
        self.assertEqual(self.procs, [])  # working agents are not interrupted
        recent = [view("codex", 20, "cx-1", self.tmp.name, age=5, status="WAITING")]
        self.relay.tick(recent)
        self.assertEqual(self.procs, [])  # not idle long enough
        idle = [view("codex", 20, "cx-1", self.tmp.name, age=120, status="WAITING")]
        self.relay.tick(idle)
        self.assertEqual(len(self.procs), 1)
        self.assertEqual(self.procs[0].argv[3:5], ["resume", "cx-1"])
        self.assertEqual(self.procs[0].kw["cwd"], self.tmp.name)
        self.assertEqual(self.bus.claims(), {self.procs[0].pid: "codex@api"})
        self.relay.tick(idle)
        self.assertEqual(len(self.procs), 1)  # one run per agent at a time
        self.procs[0].code = 0
        self.relay.tick(idle)
        self.assertEqual(len(self.procs), 1)  # same message never wakes twice
        self.assertEqual(self.bus.claims(), {})  # claim released after the run
        # message stays unread for the agent itself to read via check_messages
        self.assertEqual([m.text for m in self.bus.unread("codex@api", "codex")], ["please review"])

    def test_limits_pause_and_notify_user(self):
        idle = [view("codex", 20, "cx-1", self.tmp.name, age=120),
                view("claude", 10, "cl-1", self.tmp.name, age=120)]
        for i in range(5):
            self.bus.send("claude@web", "codex", f"ping {i}")
            self.relay.tick(idle)
            for p in self.procs:
                p.code = 0
        self.assertEqual(len(self.procs), 2)  # per-agent limit
        self.assertEqual(self.relay.status(), "relay paused")
        notes = [m for m in self.bus.recent() if m.sender == "relay" and m.to == "user"]
        self.assertEqual(len(notes), 1)
        self.assertIn("endless loop", notes[0].text)
        self.relay.toggle()  # paused -> on
        self.assertEqual(self.relay.status(), "relay on")

    def test_kill_switch(self):
        self.bus.send("claude@web", "codex", "go")
        self.relay.tick([view("codex", 20, "cx-1", self.tmp.name, age=120)])
        self.assertEqual(self.relay.status(), "relay on · 1 running")
        with mock.patch("agentviz.relay._kill_tree", side_effect=lambda p: p.kill()):
            self.relay.toggle()
        self.assertEqual(self.relay.status(), "relay off")
        self.assertEqual(self.relay.runs, {})
        self.bus.send("claude@web", "codex", "again")
        self.relay.tick([view("codex", 20, "cx-1", self.tmp.name, age=120)])
        self.assertEqual(len(self.procs), 1)  # off means off

    def test_missing_executable(self):
        self.relay._which = lambda k: None
        self.bus.send("claude@web", "codex", "go")
        self.relay.tick([view("codex", 20, "cx-1", self.tmp.name, age=120)])
        self.assertEqual(self.procs, [])
        self.assertIn("not found", list(self.relay.events)[-1][2])


class ShadowIdentityTest(unittest.TestCase):
    def test_claimed_ancestor_shares_name_without_touching_presence(self):
        with tempfile.TemporaryDirectory() as d:
            bus = Bus(os.path.join(d, "bus"))
            bus.announce("claude@web", kind="claude", agent_pid=10)
            bus.claim(4242, "claude@web")
            with mock.patch("agentviz.procs.ancestor_pids", return_value=[4243, 4242, 1]), \
                    mock.patch("agentviz.mcp_server.detect_identity",
                               return_value={"kind": "claude", "agent_pid": 4242, "cwd": d, "name": "claude@x"}):
                srv = Server(bus=bus)
            self.assertTrue(srv.shadow)
            self.assertEqual(srv.name, "claude@web")
            self.assertEqual([a["agent_pid"] for a in bus.agents()], [10])  # record not overwritten
            srv.bus.send("codex@api", "claude@web", "hi")
            text = srv.call("check_messages", {})[0]
            self.assertIn("hi", text)
            srv._stop.set()
            import io
            srv.serve(io.StringIO(""), io.StringIO())
            self.assertEqual(len(bus.agents()), 1)  # interactive agent's record survives


@unittest.skipIf(os.name == "nt", "fake agent executable is a POSIX script")
class RelayEndToEndTest(unittest.TestCase):
    """Relay starts a fake `codex` that really spawns `agentviz mcp` and answers through it."""

    def test_woken_agent_replies_under_its_own_name(self):
        with tempfile.TemporaryDirectory() as d:
            home = os.path.join(d, "home")
            bindir = os.path.join(d, "bin")
            os.makedirs(bindir)
            fake = os.path.join(bindir, "codex")
            with open(fake, "w") as f:
                f.write(textwrap.dedent(f"""\
                    #!{sys.executable}
                    import json, subprocess, sys
                    sys.path.insert(0, {ROOT!r})
                    srv = subprocess.Popen([sys.executable, "-m", "agentviz", "mcp"], stdin=subprocess.PIPE,
                                           stdout=subprocess.PIPE, text=True, cwd={ROOT!r})
                    def call(i, method, params):
                        srv.stdin.write(json.dumps({{"jsonrpc": "2.0", "id": i, "method": method,
                                                     "params": params}}) + "\\n")
                        srv.stdin.flush()
                        return json.loads(srv.stdout.readline())["result"]
                    call(1, "initialize", {{}})
                    who = call(2, "tools/call", {{"name": "whoami", "arguments": {{}}}})["content"][0]["text"]
                    got = call(3, "tools/call", {{"name": "check_messages", "arguments": {{}}}})["content"][0]["text"]
                    print("ARGV", sys.argv[1:])
                    print(who)
                    print(got)
                    call(4, "tools/call", {{"name": "send_message",
                                          "arguments": {{"to": "claude@web", "text": "reviewed: LGTM"}}}})
                    srv.stdin.close(); srv.wait()
                """))
            os.chmod(fake, os.stat(fake).st_mode | stat.S_IEXEC)
            env = {"AGENTVIZ_HOME": home, "PATH": bindir + os.pathsep + os.environ.get("PATH", ""),
                   "PYTHONPATH": ROOT}
            with mock.patch.dict(os.environ, env):
                bus = Bus()
                bus.announce("codex@api", kind="codex", agent_pid=999999, cwd=d)
                bus.send("claude@web", "codex@api", "please review api.py")
                relay = Relay(bus, RelayConfig(idle_secs=0, grace_secs=0))
                relay.tick([view("codex", 999999, "cx-9", d, age=60)])
                self.assertIn("codex@api", relay.runs)
                run = relay.runs["codex@api"]
                run.proc.wait(timeout=30)
                relay.tick([])
                log = open(run.log_path, encoding="utf-8").read()
                self.assertIn("You are 'codex@api'", log)  # same name as the interactive agent
                self.assertIn("please review api.py", log)
                self.assertIn("'resume', 'cx-9'", log)
                replies = [m for m in bus.recent() if m.sender == "codex@api"]
                self.assertEqual([m.text for m in replies], ["reviewed: LGTM"])
                self.assertEqual([a["name"] for a in bus.agents()], ["codex@api"])
                self.assertEqual(bus.agents()[0]["agent_pid"], 999999)  # presence untouched
                self.assertEqual(bus.claims(), {})


if __name__ == "__main__":
    unittest.main()
