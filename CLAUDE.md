# Project Configuration

## Workspace
This is the root workspace at C:/Claude containing cloned repos from github.com/fewleh4459.

## Preferences
- Auto-allow all tool permissions (configured in settings.json)
- Use agent swarms (parallel subagents) for complex tasks
- Use smart routing: delegate to haiku for simple searches, sonnet for moderate tasks, opus for complex work
- Auto-compact conversations at 70% context usage
- Keep responses concise and action-oriented
- Remote control enabled by default

## Active Projects
- labelmover: First priority project (needs .env setup)

## Workflow
- Always use parallel agents when tasks are independent
- Break large tasks into subtasks and run concurrently
- Prefer editing existing files over creating new ones
- Run tests after changes
