# Jef

**A beginner-friendly software factory.**

Jef takes a project idea and keeps building it until it's ready for review. It runs on a heartbeat loop: every so often it wakes up, checks what's left, does the next piece of work, and goes back to sleep until the tasks are done. When the work is ready, Jef opens a pull request on GitHub, and a human reviews and merges it.

> Status: early design. Nothing runs yet.

## How it works (MVP)

1. **Start a project:** describe what you want to build.
2. **Heartbeat build loop:** Jef wakes periodically, picks the next task, does the work, and records progress until every task is done.
3. **Open a PR:** when the work is ready, Jef opens a pull request on GitHub.
4. **Human merges:** you review, and you decide what gets merged.

## Goals

- **Beginner friendly:** you shouldn't need to be an expert to go from an idea to working code.
- **Focused on software:** Jef only builds software, which keeps it simple and dependable.
- **A human has the final say:** Jef never merges its own work.

## Inspiration

The heartbeat-until-done approach comes from kirocrew bots, applied here specifically to building software.
