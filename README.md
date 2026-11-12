# Jef

**A user-friendly software factory.**

You tell Jef what you want built. Jef keeps a backlog, and when you say go, it hands the work to a team of AI agents. Each piece of work comes back as a pull request on GitHub, and you decide what gets merged.

> Status: early development. Jef is building itself first. Nothing runs yet.

## Running Jef

### Install (once)

Jef runs its agents in [Claude Code](https://code.claude.com/docs/en/quickstart), so install that first, run `claude` once and sign in. Then:

```
git clone git@github.com:gregyu219/Jef.git
cd Jef
python3 -m venv .venv
.venv/bin/pip install -e '.[dev]'
mkdir -p ~/.local/bin
ln -s "$PWD/.venv/bin/jef" ~/.local/bin/jef
brew install tmux
```

The `ln -s` line puts the `jef` command on your PATH, so you can type `jef` from any folder. (If `jef` isn't found afterwards, add `~/.local/bin` to your PATH.) Because Jef is installed in editable mode, code changes take effect without reinstalling.

### Commands

| Command | What it does |
| --- | --- |
| `jef start` | Runs Jef in the background and opens your browser. Keeps going after you close the terminal. |
| `jef status` | Shows whether Jef is running, and its address |
| `jef attach` | Opens Jef's background session so you can watch it. Press **Ctrl+B**, then **D**, to leave it running. |
| `jef stop` | Stops Jef |
| `jef run` | Runs Jef in this terminal instead. **Ctrl+C** stops it. Plain `jef` does the same. |

Before it starts, Jef sends Claude Code one tiny message (on the smallest model) to make sure agents will be able to run. If that fails, Jef tells you what to fix and doesn't start.

Jef's page is at http://127.0.0.1:8765. It only listens on your own computer; nothing else on your network can reach it.

Options for `start` and `run`: `--port 9000` to use another port, `--no-browser` to skip opening the browser. Set `JEF_HOME=/some/folder` to use a different workspace than `~/.jef`.

### Running all the time

Coordinators heartbeat every hour, so Jef needs to stay running. `jef start` handles that: it runs Jef inside a background [tmux](https://github.com/tmux/tmux) session named `jef`, wrapped in `caffeinate -is` so your Mac doesn't fall asleep while it's plugged in.

Things it can't help with:

- **Sleep:** if your Mac sleeps, Jef pauses until it wakes. Closing a laptop lid puts it to sleep unless it's plugged in with an external display.
- **Restarts:** after a reboot, run `jef start` again.

## How it works

Jef has four levels. Each one hands work to the level below it and keeps track of its own part.

| Level | You talk to it about | It spins up | It keeps track of |
| --- | --- | --- | --- |
| **Jef** (the main UI) | Features you want, and when to start them | One coordinator per project, only after you confirm | Your backlog, plus each project's coordinator, GitHub repo and progress |
| **Project coordinator** | Requirements, and PRs that miss the point | One research agent per project, then one implementation agent per task | `goal.md`, `tasks.md` and each task's status |
| **Research agent** | Nothing directly; it reports to its coordinator | Nobody | The project's findings and proposed approach |
| **Implementation agent** | Nothing directly; it reports to its coordinator | Nobody | Its branch, its code changes and its PR |

### From idea to merged PR

1. **Tell Jef what you want.** Jef adds it to your backlog as *Waiting*. Nothing starts until you confirm, because every running project spends tokens.
2. **Confirm and connect GitHub.** Jef asks which GitHub repo the code goes to and which account commits it, then creates a named project coordinator.
3. **Research.** The coordinator runs one research agent, which writes `research.md`. The coordinator turns that into `tasks.md` for you to approve.
4. **Build.** The coordinator spins up one implementation agent per task and heartbeats it until it opens a PR.
5. **Review.** You review each PR on GitHub and merge it. If one misses the point, chat with its coordinator by name and it will revise the work or start over.

### Task statuses

Every task in a project's status table is in one of five states:

**Not started → Research → Implementation → Pending review → Merged**

*Research* only appears when the coordinator sends extra research for a single task. Most tasks go straight from Not started to Implementation.

### Heartbeats

Each project coordinator wakes up on a heartbeat, every hour by default. You can change the interval for each project in the coordinator UI. On every beat, the coordinator starts agents for new tasks, nudges working agents toward their next hand-off, checks for merged PRs and updates the status table.

### Agents are chats

Like [KiroCrew](https://github.com/kirodotdev/KiroCrew), Jef doesn't call the model itself. It drives Claude Code in headless mode (`claude -p`), and Claude Code signs in however you set it up: your Claude login or your own API key. Jef never sees or stores a key.

Every agent is a named Claude Code session with its own conversation:

- **Spawning** an agent starts a new session with its first message.
- **Messaging** it later (a heartbeat nudge, a small fix) continues the same session, so it remembers everything so far.
- **Stopping** an agent also stops every agent it spawned, so stopping a coordinator stops its workers.

Nobody is around to answer permission prompts, so each role gets a fixed list of what it may do, and anything else is refused. No agent may merge a PR or push to `main`, whatever its role. Agents' Claude Code auto memory is switched off, so they keep their notes in Jef's project files, not in your personal memory.

You're responsible for using Claude Code within [Anthropic's terms](https://www.anthropic.com/legal) for the account you sign in with.

## Where Jef keeps things

Everything lives in plain markdown in Jef's workspace, outside your code repos:

```text
~/.jef/
  backlog.md              everything you asked for
  projects/
    <project>/
      goal.md             what the project is for
      research.md         findings and approach
      tasks.md            every task and its status
      agents.md           every agent: role, who spawned it, status, session
      chats/<agent>.jsonl each agent's full conversation
      research/           extra research for single tasks (rare)
```

## Ground rules

- **Jef never merges.** A human always has the final say.
- **No agent pushes to `main`.** Every change arrives as a PR from its own branch.
- **Nothing starts without you.** Jef asks before turning a backlog item into a project.
- **Skills are yours.** The coordinator and each agent type follow a skill you write, in markdown.

## Tech

- Python
- Runs locally on your computer
- Local web UI in your browser
- Agents run in Claude Code, in headless mode

## Inspiration

The heartbeat-until-done approach comes from [KiroCrew](https://github.com/kirodotdev/KiroCrew), applied here specifically to building software.
