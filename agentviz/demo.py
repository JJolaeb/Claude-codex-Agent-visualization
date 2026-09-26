"""Synthetic agents for `--demo`, so the dashboard can be previewed without real agents."""

from __future__ import annotations

import random
import time

from .agents import KIND_BY_KEY
from .model import AgentView, derive_status
from .sessions import Session

SCRIPTS = {
    "claude": [
        ("user", "로그인 버그 고쳐줘"), ("think", "thinking…"), ("tool", "Grep: def login"),
        ("result", "src/auth.py:42"), ("tool", "Read: src/auth.py"), ("result", "(120 lines)"),
        ("tool", "Edit: src/auth.py"), ("result", "(done)"), ("tool", "Bash: pytest -q"),
        ("result", "18 passed in 2.1s"), ("text", "로그인 토큰 만료 검사를 수정했습니다."),
    ],
    "codex": [
        ("user", "Add pagination to /api/items"), ("think", "Planning pagination changes"),
        ("tool", "shell: rg -n 'def list_items'"), ("result", "api/items.py:17"),
        ("tool", "apply_patch: *** Begin Patch"), ("result", "Success. Updated api/items.py"),
        ("tool", "shell: npm test"), ("error", "1 failing: expected 20 items, got 50"),
        ("tool", "apply_patch: *** Begin Patch"), ("tool", "shell: npm test"),
        ("result", "42 passing"), ("text", "Pagination added with limit/offset params."),
    ],
    "gemini": [
        ("user", "Explain the build pipeline"), ("tool", "ReadFile: .github/workflows/ci.yml"),
        ("result", "(64 lines)"), ("text", "The pipeline runs lint, test and deploy stages."),
    ],
}


class Demo:
    def __init__(self) -> None:
        self.start = time.time()
        self.views = []
        cwds = {"claude": "~/work/webapp", "codex": "~/work/api-server", "gemini": "~/work/infra"}
        titles = {"claude": "로그인 버그 수정", "codex": "API pagination", "gemini": "CI pipeline Q&A"}
        models = {"claude": "claude-sonnet-5", "codex": "gpt-5-codex", "gemini": "gemini-2.5-pro"}
        for i, key in enumerate(SCRIPTS):
            s = Session(agent=key, path=f"demo-{key}", session_id=f"demo-{key}", cwd=cwds[key],
                        title=titles[key], model=models[key], branch="main" if i else "fix/login",
                        mtime=self.start)
            v = AgentView(kind=KIND_BY_KEY[key], pid=4200 + i * 17, cwd=cwds[key], started=self.start - 900 * (i + 1),
                          nprocs=random.randint(1, 4), rss=random.randint(150, 600) * 2**20, session=s)
            v._step, v._next = 0, self.start + random.uniform(0.5, 2.0)
            self.views.append(v)
        sub = Session(agent="claude", path="demo-claude-sub", session_id="demo-claude-sub", cwd=cwds["claude"],
                      is_subagent=True, parent_id="demo-claude", mtime=self.start)
        self.views[0].subagents = [sub]

    def update(self) -> tuple:
        now = time.time()
        sessions = []
        for v in self.views:
            s = v.session
            script = SCRIPTS[v.kind.key]
            if now >= v._next:
                kind, text = script[v._step % len(script)]
                s.add(now, kind, text)
                s.mtime = now
                s.tokens_in += random.randint(2000, 9000)
                s.tokens_out += random.randint(100, 900)
                s.context = min(200_000, s.context + random.randint(1000, 6000))
                v._step += 1
                pause = 12.0 if kind == "text" else random.uniform(0.8, 3.0)
                v._next = now + pause
                if v.subagents and kind == "tool":
                    v.subagents[0].add(now, "tool", "Grep: TODO")
                    v.subagents[0].mtime = now
            busy = now - s.mtime < 3
            v.cpu = random.uniform(15, 80) if busy else random.uniform(0, 3)
            v.history.append(v.cpu)
            v.status = derive_status(v, now)
            sessions.append(s)
            sessions.extend(v.subagents)
        return self.views, sessions
