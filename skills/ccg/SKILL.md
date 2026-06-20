---
name: ccg
description: Claude-Codex-Gemini tri-model orchestration via /ask codex + /ask gemini, then Claude synthesizes results
level: 5
---

# CCG - Claude-Codex-Gemini Tri-Model Orchestration

CCG routes through the canonical `/ask` skill (`/ask codex` + `/ask gemini`), then Claude synthesizes both outputs into one answer.

Use this when you want parallel external perspectives without launching tmux team workers.

## When to Use

- Backend/analysis + frontend/UI work in one request
- Code review from multiple perspectives (architecture + design/UX)
- Cross-validation where Codex and Gemini may disagree
- Fast advisor-style parallel input without team runtime orchestration

## Requirements

- **Codex CLI**: `npm install -g @openai/codex` (or `@openai/codex`)
- **Gemini CLI**: `npm install -g @google/gemini-cli`
- `omc ask` command available
- If either CLI is unavailable, continue with whichever provider is available and note the limitation

## How It Works

```text
1. Claude decomposes the request into two advisor prompts:
   - Codex prompt (analysis/architecture/backend)
   - Gemini prompt (UX/design/docs/alternatives)

2. Claude runs via CLI (skill nesting not supported):
   - `omc ask codex "<codex prompt>"`
   - `omc ask gemini "<gemini prompt>"`

3. Artifacts are written under `.omc/artifacts/ask/`

4. Claude synthesizes both outputs into one final response
```

## Execution Protocol

When invoked, Claude MUST follow this workflow:

### 0. Apply video-analyst-lens (auto, every run)

`/ccg`가 시작되면 **별도 지시 없이** `video-analyst-lens` 스킬을 적용한다 (참조: `.claude/skills/video-analyst-lens/SKILL.md`). 효과: 근거 중심 증류·단순요약 금지·사실/해석 구분·미확인은 "확실치 않음"·최신 수치는 확인일 명시; 다중/장시간 영상은 전 프레임 확장 금지(전체 구조 먼저, 핵심 구간만 재스캔).

**필수 출력 구조 (정확히 이 헤더 레벨로 — PTM의 `## N.` 평면 스타일과 혼동 금지):**
```md
## 영상 분석 (6-Section)

### 1. 전체 분석 개요
### 2. 핵심 구간 선별
### 3. 고밀도 분석
### 4. 자료 통합 검토
### 5. 재사용 가능한 지식 증류
### 6. 결론
```
- ✅ 반드시 `## 영상 분석 (6-Section)` **래퍼 헤더** 아래 6개 섹션을 `### 1.`~`### 6.` (H3)로 중첩한다. ❌ `## 1.`~`## 6.` H2 평면화 금지(래퍼 없으면 Dataview/검색 누락처럼 보임 — 2026-06-20 mgw 사고).
- 표준 위치: source note 본문 `## Strategies` 직후(Synthesis 앞). 이 6섹션이 Synthesis(Step 4)와 개념 추출의 입력이 된다.

> 이 Step 0은 vault 전용 `.claude/skills/ccg/SKILL.md`와 동일 규칙이다. 이 fallback 사본만 따르더라도 렌즈가 누락되지 않게 하기 위해 명시한다.

### 1. Decompose Request
Split the user request into:

- **Codex prompt:** architecture, correctness, backend, risks, test strategy
- **Gemini prompt:** UX/content clarity, alternatives, edge-case usability, docs polish
- **Synthesis plan:** how to reconcile conflicts

### 2. Invoke advisors via CLI

> **Note:** Skill nesting (invoking a skill from within an active skill) is not supported in Claude Code. Always use the direct CLI path via Bash tool.

Run both advisors:

```bash
omc ask codex "<codex prompt>"
omc ask gemini "<gemini prompt>"
```

### 3. Collect artifacts

Read latest ask artifacts from:

```text
.omc/artifacts/ask/codex-*.md
.omc/artifacts/ask/gemini-*.md
```

### 4. Synthesize

Return one unified answer with:

- Agreed recommendations
- Conflicting recommendations (explicitly called out)
- Chosen final direction + rationale
- Action checklist

## Fallbacks

If one provider is unavailable:

- Continue with available provider + Claude synthesis
- Clearly note missing perspective and risk

If both unavailable:

- Fall back to Claude-only answer and state CCG external advisors were unavailable

## Invocation

```bash
/oh-my-claudecode:ccg <task description>
```

Example:

```bash
/oh-my-claudecode:ccg Review this PR - architecture/security via Codex and UX/readability via Gemini
```
