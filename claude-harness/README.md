# Claude Code 하네스 백업

HIRA 앱 개발용 Claude Code 하네스 구성 파일 백업 (2026-07-16 구성).

## 복원 방법

```bash
# 1. 설정·규칙·팀 복원
cp claude-harness/settings.json ~/.claude/settings.json
cp claude-harness/CLAUDE.md ~/.claude/CLAUDE.md
cp -R claude-harness/teams ~/.claude/teams
cat claude-harness/zshrc >> ~/.zshrc

# 2. claude CLI 설치 (없을 때)
curl -fsSL https://claude.ai/install.sh | bash

# 3. 마켓플레이스 + 플러그인
claude plugin marketplace add anthropics/claude-plugins-official
claude plugin marketplace add anthropics/skills
claude plugin marketplace add obra/superpowers-marketplace
for p in code-review code-simplifier commit-commands feature-dev \
         security-guidance skill-creator hookify frontend-design; do
  claude plugin install "$p@claude-plugins-official"; done
claude plugin install superpowers@superpowers-marketplace
for p in document-skills example-skills claude-api; do
  claude plugin install "$p@anthropic-agent-skills"; done

# 4. (선택) tmux — teammateMode: tmux 용
brew install tmux
```

- 전체 명세와 배경: `PROMPT-HIRA.md`
- pdf/xlsx/docx 스킬은 document-skills에, webapp-testing/frontend-design 스킬은 example-skills에 포함
- `.env`(API 키)와 cache.pkl은 백업에 포함되지 않음
