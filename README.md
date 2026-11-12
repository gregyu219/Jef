# Jef

**A user-friendly software factory.**

You tell Jef what you want built. Jef keeps a backlog, and when you say go, it hands the work to a team of AI agents. Each piece of work comes back as a pull request on GitHub, and you decide what gets merged.

> Status: early development. Jef is building itself first. The whole loop works: backlog → coordinator → research → tasks → implementers → PRs you merge.

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
3. **Research.** The coordinator runs one research agent, which writes `research.md`. The coordinator turns that into `tasks.md` for you to approve. If you haven't approved it by the next heartbeat, the coordinator decides: it approves the list or revises it. You can turn that off per project.
4. **Build.** Implementation agents build one task at a time, and Jef heartbeats each one until it opens a PR. The first starts when you approve the list. After each merge, the coordinator looks at what merged and decides when the next task starts (`ACTION: start-task <n>`), revising the tasks that are left if needed, or waits for you (`ACTION: wait <reason>`). You can turn this off per project; then Jef starts the next task straight away. The project page shows who started each agent.
5. **Review.** You review each PR on GitHub and merge it. If one misses the point, chat with its coordinator by name and it will revise the work or start over.

### Task statuses

Every task in a project's status table is in one of five states:

**Not started → Research → Implementation → Pending review → Merged**

*Research* only appears when the coordinator sends extra research for a single task. Most tasks go straight from Not started to Implementation.

### Heartbeats

Jef's heartbeat runs in the background while Jef is running. Every few seconds it acts on replies agents have finished: it saves research, records the task list, and notes PRs and stuck agents. It also starts the next task's implementer when it's that task's turn.

Once per heartbeat interval (an hour by default; change it on the project page), it nudges implementers that are still working. While a task list waits for your approval, it asks the coordinator to decide once per interval, starting one full interval after the list was proposed: the coordinator either approves the list or sends a revised one (which restarts the wait). You can still approve by hand at any time. To approve every list yourself, turn the coordinator's heartbeat approval off on the project page (it's on by default, and saved as `Coordinator decides: no` in `goal.md`); while it's off, the coordinator isn't asked and Jef ignores its `ACTION: approve-tasks`. It checks GitHub for merged PRs every five minutes. After a merge it asks the coordinator which task starts next; if the coordinator hasn't said by the next heartbeat, Jef reminds it once, and a heartbeat after that it stops and shows you a problem instead of starting anything on its own. To have Jef start the next task without asking, turn off the coordinator starting the next task on the project page (saved as `Coordinator starts tasks: no` in `goal.md`). Each PR check also asks GitHub whether open PRs can still merge. If one has conflicts, the coordinator decides: the implementer rebases it on `main` and Jef pushes the fix to the same PR with a comment saying why (`ACTION: fix-conflict <n>`), or it waits for you (`ACTION: wait <reason>`). With the setting off, Jef sends the fix straight to the implementer. A merge in any project makes every project check its PRs at its next tick, since projects can share a repo. Jef only opens a new PR for a task if its old one was closed, and links the old one.

Don't want to wait? The project page has a **Check status now** button that does the same checks straight away: it reads finished replies, asks GitHub about PRs waiting for review, and starts the next task. It doesn't nudge agents, so it costs no tokens.

To see every project at once, press **Read status** above the Projects table on the home page. It rewrites each project's `status.md` from Jef's own files and shows each project's stage and its coordinator's state. It makes no AI or GitHub calls. You can also read one project's file at `/projects/<slug>/status`, or on its page under Status file.

Tasks are built **one at a time** by default, because they usually build on each other. The next task starts after you merge the last one's PR. Each implementer works in its own checkout (a git worktree) on its own branch, `jef/<project>/task-<n>`.

### How agents hand work back

Agents only reply in text, and Jef reads a few plain markers in those replies:

| Who | Marker | What Jef does |
| --- | --- | --- |
| Coordinator | `ACTION: start-research` on its own line, after you say go | Starts the research agent |
| Research agent | Its whole final reply | Saves it as `research.md`, then asks the coordinator for tasks |
| Coordinator | A fenced `tasks` block with a numbered list | Writes `tasks.md` for you to approve (a new block replaces it, until you approve) |
| Coordinator | `ACTION: approve-tasks` on its own line, while the list waits (never in the same reply as a new `tasks` block) | Approves the tasks and starts building |
| Implementer | `STATUS: working` or `STATUS: stuck <reason>` | Updates the agent's note |
| Implementer | A fenced `pr` block (title and description), then `STATUS: ready` | Checks for secrets, pushes the branch and opens the PR (or updates the open one) |

The skills in `skills/` tell each agent how to write these.

### Agents are chats

Like [KiroCrew](https://github.com/kirodotdev/KiroCrew), Jef doesn't call the model itself. It drives Claude Code in headless mode (`claude -p`), and Claude Code signs in however you set it up: your Claude login or your own API key. Jef never sees or stores a key.

Every agent is a named Claude Code session with its own conversation:

- **Spawning** an agent starts a new session with its first message.
- **Messaging** it later (a heartbeat nudge, a small fix) continues the same session, so it remembers everything so far.
- **Stopping** an agent also stops every agent it spawned, so stopping a coordinator stops its workers.

Nobody is around to answer permission prompts, so each role gets a fixed list of what it may do, and anything else is refused. **No agent can push, open a PR or merge**, whatever its role: implementers only commit, and Jef's own code does the rest.

### Chat with Jef

The home page has its own chat with Jef. Ask it how projects are going, or talk through an idea before you add it to the backlog. Your first message starts a session whose briefing lists every backlog item and every project, one line each, and Jef can read the workspace files for more. It's read-only: to add or start backlog items, use the page's buttons. Its usage isn't counted in any project's total. **Reset** starts Jef over: it ends the session, saves the old conversation, and starts a new session with a fresh briefing, so Jef greets you already knowing where things stand.

### Jef opens the PRs

When an implementer reports `STATUS: ready`, Jef:

1. checks it's on its own `jef/...` branch, with everything committed,
2. runs the **secret check**: it scans every line the agent added, its commit messages and the PR text for things shaped like secrets (Anthropic API keys, GitHub tokens, private keys). If it finds one, it doesn't push, and it tells the agent which file to fix. It never repeats the secret itself,
3. pushes the branch, only ever the agent's own `jef/...` branch, never `main`, and
4. opens the PR with your GitHub login, or updates the PR that's already open.

If something needs fixing, Jef tells the agent, up to three times, then marks it stuck for you. The secret check only knows common secret shapes, so it's a safety net, not a guarantee. Agents can also run code (to build and test), so these rules aren't a sandbox. Turn on GitHub branch protection for `main` as the final guard. Agents' Claude Code auto memory is switched off, so they keep their notes in Jef's project files, not in your personal memory.

### Usage and cost

Every finished agent turn ends with a summary from Claude Code that includes the tokens it used and an estimated cost. Jef adds these up straight from the chat logs (no separate ledger) and shows them per agent and per project on the project page, the agent page and the home page. The costs are Claude Code's estimates at API list prices: if you sign in with a Claude subscription you aren't billed per token, so read them as "what this would cost on the API". A turn that's stopped before it finishes writes no summary, so its usage isn't counted.

### Budgets

Each project can have a dollar budget, set under Usage on its project page (it's saved as a `- Budget: 20.00 USD` line in `goal.md`). Projects have no budget unless you set one. With a budget:

- Before starting any agent turn, Jef checks what's left. Once the estimated cost reaches the budget, the project pauses: no agent starts another turn, the heartbeat stops nudging, and messages to the coordinator are refused. A banner on the project page says so.
- Each turn is also started with Claude Code's `--max-budget-usd` set to what's left, so one long turn gets stopped partway instead of running far past the budget. This needs a recent Claude Code (v2.1.217 or later).
- Raise the budget (or clear it) and the project carries on by itself.

Spending can still go a little over: a turn's cost is only known once it's finished, and Claude Code stops a turn a bit after it reaches its cap.

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
      status.md           where the project is: its phase, the coordinator's state
                          and each task's stage. Jef writes it from the files above
                          on every heartbeat, with no AI call, so you can just read it
      chats/<agent>.jsonl each agent's full conversation
      research/           extra research for single tasks (rare)
      repo/               the coordinator's clone of the project's repo
      work/impl-<n>/      each implementer's own checkout
  jef/projects/home/      the home page's chat with Jef (agents.md, chats/jef.jsonl;
                          chats/jef-<time>.jsonl are earlier chats kept by Reset)
```

## Ground rules

- **Jef never merges.** A human always has the final say.
- **No agent pushes to `main`.** Every change arrives as a PR from its own branch.
- **Nothing starts without you.** Jef asks before turning a backlog item into a project.
- **Costs are estimates.** Usage and budgets use Claude Code's own cost estimates at API list prices.
- **Skills are yours.** The coordinator and each agent type follow a skill you write, in markdown.

## Tech

- Python
- Runs locally on your computer
- Local web UI in your browser
- Agents run in Claude Code, in headless mode

## Inspiration

The heartbeat-until-done approach comes from [KiroCrew](https://github.com/kirodotdev/KiroCrew), applied here specifically to building software.
