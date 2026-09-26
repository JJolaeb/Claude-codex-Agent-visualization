# agentviz

지금 PC에서 돌아가고 있는 **코딩 에이전트**(Claude Code, Codex 등)를 터미널(cmd / PowerShell / bash)에서 실시간으로 시각화해 보여주는 대시보드입니다.
**에이전트끼리 메시지를 주고받게** 해 주는 메시지 버스(MCP 서버)도 들어 있습니다.

```
 ◆ AGENTVIZ   2 running · 1 busy  ● Claude Code ×1  ● Codex ×1           refresh 1s  14:02:11

╭─ ● Claude Code  pid 153 · 4 procs ─────────────────────────────── ⠹ RUNNING TOOL ─╮
│ 로그인 버그 수정                                                                   │
│ dir ~/work/webapp  ⎇ fix/login  model claude-sonnet-5                              │
│ cpu ▁▁▂▅▇█▆▃▂▁  12.3%  mem 330.5M  up 7m04s  tok 1.69M↑ 45.0k↓  ctx 113.2k  0s ago │
│ 14:02:05 ▶ Grep: def login                                                         │
│ 14:02:06 ✓ src/auth.py:42                                                          │
│ 14:02:09 ▶ Bash: pytest -q                                                         │
╰────────────────────────────────────────────────────────────────────────────────────╯
╭─ ● Codex  pid 4217 ─────────────────────────────────────────── ● WAITING FOR INPUT ─╮
│ ...                                                                                 │
 Activity ─────────────────────────────────────────────────────────────────────────────
14:02:09 로그인 버그 수정   ▶ Bash: pytest -q
14:02:08 API pagination     ◆ Pagination added with limit/offset params.
```

## 무엇을 보여주나

- **실행 중인 에이전트 프로세스**: PID, 하위 프로세스 수, CPU 사용률 스파크라인, 메모리, 실행 시간, 작업 디렉터리
- **세션 활동** (대화 로그를 실시간으로 읽음)
  - Claude Code: `~/.claude/projects/**/*.jsonl` (서브에이전트 포함)
  - Codex: `~/.codex/sessions/YYYY/MM/DD/rollout-*.jsonl`
  - 세션 제목, 모델, git 브랜치, 토큰 사용량(입력↑/출력↓), 현재 컨텍스트 크기
  - 최근 이벤트: 사용자 입력 `›`, 도구 실행 `▶`, 결과 `✓` / 오류 `✗`, 사고 `…`, 답변 `◆`
- **상태 표시**
  - `WORKING` – 방금 로그가 갱신됐거나 CPU를 많이 쓰는 중
  - `RUNNING TOOL` – 도구(명령어, 파일 편집 등) 실행 결과를 기다리는 중
  - `WAITING FOR INPUT` – 답변을 끝내고 사용자 입력을 기다리는 중
  - `IDLE` / `ENDED`
- **Activity 피드**: 모든 에이전트의 이벤트를 시간순으로 합쳐서 표시

프로세스로 감지하는 에이전트: Claude Code, Codex, Gemini CLI, GitHub Copilot CLI, Cursor Agent, OpenCode, Aider, Qwen Code, Amp
(대화 로그까지 읽는 것은 Claude Code와 Codex)

## 설치 / 실행

Python 3.8 이상만 있으면 됩니다. 외부 패키지 없이 동작합니다.

```bash
# 설치 없이 바로 실행
python -m agentviz

# Windows cmd 에서는 저장소 폴더의 배치 파일로도 실행 가능
agentviz.cmd

# 명령어로 설치
pip install .            # 이후 어디서나 `agentviz`
pip install ".[psutil]"  # psutil 포함 (Windows 권장)
```

> **Windows**: psutil이 없으면 PowerShell로 프로세스 목록을 조회하므로 조금 느리고 작업 디렉터리를 알 수 없습니다.
> `pip install psutil`을 권장합니다. Windows 10 이상의 cmd / PowerShell / Windows Terminal에서 색상과 유니코드가 표시됩니다.
> 글자가 깨지면 `--ascii` 옵션을 사용하세요.
>
> **오래된 콘솔**(Windows 10 이전, 또는 창 속성에서 "레거시 콘솔 사용"이 켜진 경우)은 ANSI 색상/커서 제어를 지원하지 않습니다.
> agentviz는 이를 자동으로 감지해 실시간 대시보드 대신 해결 방법을 안내하고 종료합니다(`--once`, `--json`, `messages -f`는 그대로 사용 가능하며
> 자동으로 색상 없이 ASCII로 출력됩니다). 감지가 잘못된 경우 `AGENTVIZ_FORCE_VT=1`로 검사를 건너뛸 수 있습니다.

## 옵션

| 옵션 | 설명 |
| --- | --- |
| `-i, --interval SEC` | 갱신 주기 (기본 1초) |
| `-w, --window MIN` | 최근 N분 안에 갱신된 세션 로그만 표시 (기본 15분) |
| `--once` | 한 번만 출력하고 종료 |
| `--json` | 현재 상태를 JSON으로 출력 (스크립트 연동용) |
| `--demo` | 가짜 에이전트로 화면 미리보기 |
| `--ascii` | ASCII 문자만 사용 |
| `--no-color` | 색상 끄기 (`NO_COLOR` 환경변수도 지원) |
| `--no-feed` | Activity 피드 숨기기 |
| `--all` | 기본으로 숨기는 데스크톱 앱(Claude/Codex 앱)과 IDE 확장용 백그라운드 서버도 표시 |

### 단축키

| 키 | 동작 |
| --- | --- |
| `q` / `Esc` | 종료 |
| `p` | 일시정지 / 재개 |
| `+` / `-` | 갱신 속도 빠르게 / 느리게 |
| `f` | Activity 피드 켜기/끄기 |
| `m` | 에이전트에게 메시지 보내기 (`@이름 내용`, Enter 전송, Esc 취소) |
| `r` | 즉시 새로고침 |
| `c` | 끝난 작업(프로세스가 종료되고 로그만 남은 카드) 지우기. 활동 기록에서도 사라지며, 같은 세션이 다시 진행되면 다시 표시 |
| `u` | 지운 작업 되돌리기 |
| `x` | 릴레이 끄기/켜기 (`--relay`로 실행했을 때) |

## 에이전트끼리 대화하기 (메시지 버스)

Claude Code와 Codex는 둘 다 **MCP** 도구를 쓸 수 있으므로, agentviz가 MCP 서버(`agentviz mcp`)를 제공해
에이전트들이 같은 메시지 버스(`~/.agentviz/bus`)로 대화하게 합니다. 대시보드에서는 이 대화가 **Messages** 패널에
실시간으로 보이고, 사용자도 `m` 키로 끼어들 수 있습니다.

### 연결

```bash
agentviz setup      # 내 환경에 맞는 등록 명령을 출력
```

출력되는 명령 예시:

```bash
claude mcp add --scope user agentviz -- python -m agentviz mcp      # Claude Code
codex mcp add agentviz -- python -m agentviz mcp                    # Codex
```

> `agentviz` 명령 대신 `python -m agentviz`를 쓰는 이유: Windows에서 pip의 Scripts 폴더가 PATH에 없으면
> 에이전트가 `agentviz`를 찾지 못해 `Failed to connect`가 납니다. `agentviz setup`은 Python 전체 경로로 된 명령을 출력하므로 가장 확실합니다.

등록 후 에이전트를 다시 시작하면, 각 에이전트는 자동으로 `claude@<프로젝트폴더>`, `codex@<프로젝트폴더>` 같은 이름을 갖습니다.

### 에이전트가 쓰는 도구

| 도구 | 설명 |
| --- | --- |
| `list_agents` | 지금 연결된 에이전트 목록 |
| `send_message(to, text)` | 메시지 보내기. `to`는 이름(`codex@api`), 종류(`codex` → 모든 Codex), `user`(사람), `*`(전체) |
| `check_messages(wait_seconds)` | 새 메시지 읽기. `wait_seconds`를 주면 답이 올 때까지 최대 45초 대기 |
| `whoami` / `set_name` | 내 이름 확인 / 변경 |

사용 예: Claude Code에게 *"codex에게 api 폴더의 페이지네이션 코드를 리뷰해 달라고 하고 답을 기다려줘"* 라고 말하면
Claude가 `send_message` → `check_messages(wait_seconds=45)`를 스스로 호출합니다.

### 메시지가 전달되는 방식

- **Codex / 기타**: 에이전트가 `check_messages`를 호출할 때 읽습니다(에이전트가 일하고 있지 않으면 저절로 깨어나지는 않습니다).
- **Claude Code + 훅(선택)**: `agentviz setup`이 출력하는 hooks 설정을 `~/.claude/settings.json`에 넣으면
  - 도구를 쓸 때마다(PostToolUse)와 사용자가 입력할 때(UserPromptSubmit) 새 메시지가 대화에 자동으로 들어가고,
  - Claude가 작업을 마치려 할 때(Stop) 안 읽은 메시지가 있으면 멈추지 않고 먼저 읽고 답합니다.
- 연결되기 전에 온 메시지도 1시간 이내 것이면 연결 후 받습니다. 각 메시지는 한 번만 전달됩니다.

> 다른 에이전트가 보낸 메시지는 "동료의 요청"으로 전달되며, 에이전트에게 위험한 명령은 그대로 따르지 말라고 안내합니다.
> 버스는 로컬 파일이며 네트워크로 노출되지 않습니다.

### 작업하면서 알아서 소통하게 하기

**1) 협업 규칙 넣기** — 에이전트가 작업 단계마다 메시지를 확인하고, 겹치는 작업은 먼저 물어보게 합니다.

```bash
python -m agentviz setup --rules            # 현재 폴더의 CLAUDE.md, AGENTS.md에 규칙 추가
python -m agentviz setup --rules --global   # 모든 프로젝트 (~/.claude/CLAUDE.md, ~/.codex/AGENTS.md)
python -m agentviz setup --rules --remove   # 규칙 제거
```

규칙은 표시된 블록(`<!-- agentviz:rules:start -->` ~ `end`)으로만 추가되어 기존 내용은 건드리지 않습니다. 에이전트를 다시 시작하면 적용됩니다.

**2) 릴레이 켜기** — 입력을 기다리며 쉬고 있는 에이전트에게 메시지가 오면 agentviz가 그 세션을 이어서 실행해
메시지를 읽고 답하게 합니다 (Claude: `claude -p --resume <세션> --fork-session`, Codex: `codex exec resume <세션>`).

```bash
python -m agentviz --relay        # 대시보드 + 릴레이 (x 키로 끄기/켜기)
python -m agentviz relay          # 릴레이만 (로그 출력)
```

안전장치:
- 작업 중(WORKING / RUNNING TOOL)이거나 최근 30초 안에 활동한 에이전트는 깨우지 않습니다.
- 에이전트당 동시에 하나만 실행, 10분 넘으면 강제 종료, 같은 메시지로는 한 번만 깨웁니다.
- 한 시간에 전체 12회 / 에이전트당 6회를 넘으면 **자동으로 멈추고** 사용자에게 메시지를 남깁니다.
- 메시지 내용은 명령줄에 넣지 않습니다(에이전트가 `check_messages`로 직접 읽음).
- 대시보드에서 `x`를 누르면 실행 중인 것까지 즉시 중단합니다.

조절 옵션: `--relay-max`, `--relay-max-per-agent`, `--relay-idle`, `--relay-timeout`.
깨운 실행은 기본적으로 agentviz 도구만 허용됩니다. 파일 수정까지 맡기려면
`--claude-args="--permission-mode acceptEdits"`, `--codex-args="--full-auto"`처럼 권한을 직접 넘겨 주세요.
실행 로그는 `~/.agentviz/bus/relay-logs/`에 남습니다.

> 주의: 릴레이는 사람 확인 없이 에이전트를 실행하므로 사용량이 늘어납니다. Claude는 원래 대화를 건드리지 않도록
> 세션을 복제(fork)해서 이어가고, Codex는 같은 세션 기록에 이어서 기록됩니다.

### 사람이 쓰는 명령

```bash
agentviz                          # 대시보드에서 m → "@codex 테스트 돌려줘" → Enter  (@ 없으면 전체에게)
agentviz send codex "테스트 돌려줘"   # 명령줄에서 보내기 (보낸 사람: user)
agentviz messages -f              # 대화 로그 실시간 보기
agentviz agents                   # 연결된 에이전트 목록
```

## 잘못 감지될 때

`agentviz doctor`를 실행하면 에이전트로 인식된 모든 프로세스의 실행 파일, 명령줄, 작업 폴더와 표시/숨김 이유가 출력됩니다.

기본으로 숨기는 것:
- Claude / Codex **데스크톱 앱** (Electron 앱, Microsoft Store 설치본)
- Codex 보조 실행 파일 (`codex-command-runner` 등, 에이전트가 아님)
- 세션이 없는 **백그라운드 서버** (`codex app-server`, `codex mcp-server`, `claude mcp serve` — IDE 확장이나 앱이 띄운 것)

## 환경 변수

- `CLAUDE_CONFIG_DIR` – Claude Code 설정 폴더 (기본 `~/.claude`)
- `CODEX_HOME` – Codex 폴더 (기본 `~/.codex`)
- `AGENTVIZ_HOME` – 메시지 버스 폴더 (기본 `~/.agentviz`)
- `AGENTVIZ_FORCE_VT` – `1`이면 Windows 레거시 콘솔 감지를 건너뜀

## 동작 방식

1. 프로세스 목록(psutil → `/proc` → `ps` → PowerShell 순으로 사용 가능한 것)에서 에이전트 실행 파일/패키지 이름으로 프로세스를 찾습니다.
   같은 에이전트의 부모-자식 프로세스(예: `node` 래퍼 + 네이티브 바이너리)는 하나로 합칩니다.
2. 세션 로그 파일을 끝에서부터 증분으로 읽어 이벤트와 토큰 사용량을 모읍니다.
3. 작업 디렉터리(Claude Code는 `~/.claude/sessions/<pid>.json`으로 정확히)를 기준으로 프로세스와 세션을 연결합니다.
   프로세스를 찾지 못한 최근 세션은 `log only` 카드로 표시됩니다.

모든 데이터는 로컬에서만 읽으며 외부로 전송하지 않습니다.

## 테스트

```bash
python -m unittest discover -s tests
```
