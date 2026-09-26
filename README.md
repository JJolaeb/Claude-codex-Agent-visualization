# agentviz

지금 PC에서 돌아가고 있는 **코딩 에이전트**(Claude Code, Codex 등)를 터미널(cmd / PowerShell / bash)에서 실시간으로 시각화해 보여주는 대시보드입니다.

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

### 단축키

| 키 | 동작 |
| --- | --- |
| `q` / `Esc` | 종료 |
| `p` | 일시정지 / 재개 |
| `+` / `-` | 갱신 속도 빠르게 / 느리게 |
| `f` | Activity 피드 켜기/끄기 |
| `r` | 즉시 새로고침 |

## 환경 변수

- `CLAUDE_CONFIG_DIR` – Claude Code 설정 폴더 (기본 `~/.claude`)
- `CODEX_HOME` – Codex 폴더 (기본 `~/.codex`)

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
