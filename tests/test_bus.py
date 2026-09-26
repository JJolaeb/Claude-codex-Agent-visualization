import io
import json
import os
import tempfile
import time
import unittest
from unittest import mock

from agentviz import hook
from agentviz.app import parse_compose
from agentviz.bus import Bus, Message, unique_name
from agentviz.mcp_server import Server
from agentviz.render import Theme, frame


def ident(kind, pid, name):
    return {"kind": kind, "agent_pid": pid, "cwd": "/w/" + name.split("@")[-1], "name": name}


class BusTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.bus = Bus(os.path.join(self.tmp.name, "bus"))

    def tearDown(self):
        self.tmp.cleanup()

    def test_addressing(self):
        m = Message("1", 0, "claude@web", "codex", "hi")
        self.assertTrue(m.addressed_to("codex@api", "codex"))
        self.assertTrue(m.addressed_to("codex@api"))  # by name prefix even without kind
        self.assertFalse(m.addressed_to("gemini@infra", "gemini"))
        self.assertFalse(Message("2", 0, "codex@api", "*", "x").addressed_to("codex@api", "codex"))
        self.assertTrue(Message("3", 0, "user", "all", "x").addressed_to("gemini@infra"))

    def test_unread_once_and_backlog(self):
        old = Message("0", time.time() - 7200, "user", "*", "ancient")
        with open(self.bus.log, "w", encoding="utf-8") as f:
            f.write(json.dumps(old.__dict__) + "\n")
        self.bus.send("user", "codex", "recent")
        got = self.bus.unread("codex@api", "codex")
        self.assertEqual([m.text for m in got], ["recent"])  # old backlog skipped
        self.assertEqual(self.bus.unread("codex@api", "codex"), [])  # delivered once
        self.bus.send("claude@web", "codex@api", "second")
        self.assertEqual([m.text for m in self.bus.unread("codex@api")], ["second"])

    def test_partial_line_not_consumed(self):
        self.bus.unread("a@x")  # create cursor
        with open(self.bus.log, "a", encoding="utf-8") as f:
            f.write('{"id": "9", "ts": 1')
        self.assertEqual(self.bus.unread("a@x"), [])
        with open(self.bus.log, "a", encoding="utf-8") as f:
            f.write(f'{time.time()}, "sender": "user", "to": "*", "text": "late"}}\n')
        self.assertEqual([m.text for m in self.bus.unread("a@x")], ["late"])

    def test_presence_and_unique_name(self):
        self.bus.announce("claude@web", agent_pid=10)
        self.assertEqual(unique_name(self.bus, "claude@web", 10), "claude@web")
        self.assertEqual(unique_name(self.bus, "claude@web", 11), "claude@web#2")
        self.bus.retire("claude@web")
        self.assertEqual(self.bus.agents(), [])

    def test_mcp_conversation(self):
        claude = Server(bus=self.bus, identity=ident("claude", 10, "claude@web"))
        codex = Server(bus=self.bus, identity=ident("codex", 20, "codex@api"))
        init = claude.handle({"jsonrpc": "2.0", "id": 1, "method": "initialize",
                              "params": {"protocolVersion": "2025-03-26"}})
        self.assertEqual(init["result"]["protocolVersion"], "2025-03-26")
        names = [t["name"] for t in claude.handle({"jsonrpc": "2.0", "id": 2, "method": "tools/list"})["result"]["tools"]]
        self.assertIn("send_message", names)
        self.assertIsNone(claude.handle({"jsonrpc": "2.0", "method": "notifications/initialized"}))

        def call(srv, tool, **args):
            r = srv.handle({"jsonrpc": "2.0", "id": 3, "method": "tools/call",
                            "params": {"name": tool, "arguments": args}})["result"]
            return r["content"][0]["text"], r["isError"]

        self.assertIn("codex@api", call(claude, "list_agents")[0])
        self.assertFalse(call(claude, "send_message", to="codex", text="리뷰 부탁해")[1])
        text, _ = call(codex, "check_messages")
        self.assertIn("claude@web -> codex", text)
        self.assertIn("리뷰 부탁해", text)
        call(codex, "send_message", to="claude@web", text="LGTM")
        self.assertIn("LGTM", call(claude, "check_messages", wait_seconds=1)[0])
        self.assertEqual(call(claude, "check_messages")[0], "No new messages.")
        self.assertTrue(call(claude, "send_message", to="codex", text="  ")[1])
        self.assertTrue(call(codex, "set_name", name="claude@web")[1])  # taken
        self.assertFalse(call(codex, "set_name", name="reviewer")[1])
        self.assertIn("reviewer", [a["name"] for a in self.bus.agents()])
        err = claude.handle({"jsonrpc": "2.0", "id": 4, "method": "nope"})
        self.assertEqual(err["error"]["code"], -32601)

    def test_serve_stdio(self):
        srv = Server(bus=self.bus, identity=ident("claude", 10, "claude@web"))
        stdin = io.StringIO('{"jsonrpc":"2.0","id":1,"method":"ping"}\nnot json\n')
        stdout = io.StringIO()
        srv.serve(stdin, stdout)
        lines = [json.loads(l) for l in stdout.getvalue().splitlines()]
        self.assertEqual(lines[0], {"jsonrpc": "2.0", "id": 1, "result": {}})
        self.assertEqual(lines[1]["error"]["code"], -32700)
        self.assertEqual(self.bus.agents(), [])  # retired on exit

    def test_hook(self):
        Server(bus=self.bus, identity=ident("claude", 10, "claude@web"))
        self.bus.send("codex@api", "claude", "done!")
        with mock.patch.object(hook, "Bus", return_value=self.bus), \
                mock.patch.object(hook, "ancestor_pids", return_value=[99, 10, 1]):
            out = io.StringIO()
            hook.run(io.StringIO('{"hook_event_name": "Stop"}'), out)
            data = json.loads(out.getvalue())
            self.assertEqual(data["decision"], "block")
            self.assertIn("done!", data["reason"])
            out = io.StringIO()
            hook.run(io.StringIO('{"hook_event_name": "PostToolUse"}'), out)
            self.assertEqual(out.getvalue(), "")  # already delivered
            self.bus.send("user", "*", "hi all")
            hook.run(io.StringIO('{"hook_event_name": "PostToolUse"}'), out)
            ctx = json.loads(out.getvalue())["hookSpecificOutput"]
            self.assertEqual(ctx["hookEventName"], "PostToolUse")
            self.assertIn("hi all", ctx["additionalContext"])
        with mock.patch.object(hook, "Bus", return_value=self.bus), \
                mock.patch.object(hook, "ancestor_pids", return_value=[5, 1]):
            out = io.StringIO()
            hook.run(io.StringIO('{"hook_event_name": "Stop"}'), out)
            self.assertEqual(out.getvalue(), "")  # not a connected agent


class ComposeRenderTest(unittest.TestCase):
    def test_parse_compose(self):
        self.assertEqual(parse_compose("@codex@api 안녕"), ("codex@api", "안녕"))
        self.assertEqual(parse_compose("hello all"), ("*", "hello all"))
        self.assertEqual(parse_compose("@codex"), ("*", "@codex"))

    def test_messages_panel_and_compose_footer(self):
        msgs = [Message("1", time.time(), "claude@web", "codex@api", "로그인 API 확인 부탁"),
                Message("2", time.time(), "codex@api", "user", "done")]
        for theme in (Theme(color=True), Theme(color=False, ascii_only=True)):
            lines = frame([], [], {}, (70, 20), 0, theme, messages=msgs, compose="@codex hi",
                          targets=["codex@api"])
            text = "\n".join(lines)
            self.assertIn("Messages", text)
            self.assertIn("로그인 API", text)
            self.assertIn("@codex hi", lines[-1])
            from agentviz.util import width
            for line in lines:
                self.assertLessEqual(width(line), 69)


if __name__ == "__main__":
    unittest.main()
