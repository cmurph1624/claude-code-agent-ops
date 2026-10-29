# Working across ~/dev (from ~/dev/agent-ops)

## Changes go through PRs
- In any repo with a GitHub remote, make changes on a branch and open a PR (`gh pr create`) rather than
  pushing to the default branch, unless the user says otherwise. Keep each PR to one coherent change.
- Merge your own PR (`gh pr merge --merge`) once the automated review's verdict is `clean` for the
  current head, unless the PR is labeled `hold` or the user said to wait. Never merge a PR the reviewer
  hasn't passed, and don't merge after the review cap without the user's OK.
- Merging is not deploying. In repos with `merge_deploys: true` a merge *does* deploy, so the deploy gate
  will prompt the user there; that prompt is expected.
- PRs in the watched repos (`~/dev/agent-ops/review/repos.txt`) get an automated review a few minutes
  after each push. After opening or pushing to one, run the **pr-review-loop** skill. It waits for the review and
  applies or rebuts the findings. Open the PR as a draft if you aren't ready for review yet.

## Deploys
- Deploys and rollbacks go through the **deploy** skill and **ops** agent, using
  `~/dev/agent-ops/ops/deployments.yaml`. Deploy only when the user asks. Merging a PR, or a plan you announced earlier, is not a request to deploy.
  A hook (deploy-gate) makes every deploy-shaped command prompt the user, so expect it and don't work around it.
- Services with `live_checkout: true` run straight from their `~/dev` checkout. Switching branches or
  pulling in those directories changes what is running.
  Do branch work in a worktree (`git worktree add ../<repo>-<branch>`) when the main tree is live.
- Projects with `mechanism: artifact` are deployed by publishing a claude.ai artifact. That's a deploy too:
  republish only through the deploy skill when the user asks, never as a step after merging.
