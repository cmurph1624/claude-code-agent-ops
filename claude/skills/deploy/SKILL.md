---
name: deploy
description: Deploy, roll back, or check the status of the projects in ops/deployments.yaml. Use for "/deploy <project> [ref]", "/deploy status", "deploy X", "roll back X", "is X up", or "what's out of date on the box".
---

# /deploy

- **`/deploy status`**, or "is everything up": run `python3 ~/dev/agent-ops/ops/status.py` yourself
  (read-only, fast) and summarise what needs attention. You don't need the ops agent for this.
- **`/deploy <project> [ref]`**, or "roll back <project>": delegate to the **ops** subagent with the
  project, the ref (if given), and the user's exact words. If the agent comes back with a
  confirmation question, ask the user that question verbatim. Then resume the same agent (SendMessage)
  with their answer. Don't start a new one.
- Project names follow the keys in `~/dev/agent-ops/ops/deployments.yaml`. Map loose names onto them,
  e.g. "the api" → example-api.
- **Artifact projects** (`mechanism: artifact`): the ops agent returns `READY TO PUBLISH`
  with a bundle in `/tmp/claude-1000/ops-<name>`. The Artifact tool can't read that directory, so
  copy the bundle into your session scratchpad first (`<scratchpad>/ops-<name>`), then publish it
  from there with the Artifact tool, following the registry's `publish` field.
  The deploy gate prompts the user; that prompt is expected. Then check `verify`, and append the
  deploy.log line the agent's step 6 describes (`ok`, or `failed` with the reason). If the publish
  fails, nothing changed on the live page, so there's nothing to roll back.
- A user asking you to deploy is the authorisation for that one deploy. Never deploy as a side effect
  of merging, finishing a PR, or a status check.
