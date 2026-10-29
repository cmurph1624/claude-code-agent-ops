---
name: pr-review-loop
description: Wait for the automated reviewer's findings on a PR you opened or pushed to, then apply or rebut each one, push, and repeat, for at most 2 fix rounds, then merge on a clean verdict. Use right after `gh pr create` or a push to a PR branch in a watched repo (a hook will prompt you), or when the user says "address the review", "pick up the review", or `/pr-review-loop <pr>`.
---

# PR review loop

A reviewer on the home server (`~/dev/agent-ops/review/`) reviews every new head commit of open PRs
in the watched repos. It posts one comment per commit, containing findings with a severity, file:line,
problem and recommendation. You are the author. Your job is to act on those findings, not just pass
them along.

The tool used throughout is `~/dev/agent-ops/review/review_data.py`. Call it `RD` below.

## Loop (fix round = 0)
1. **Wait for the review of the current head.** Run `RD <pr> --wait 1500` from the repo, using Bash
   with `run_in_background: true`. You get re-invoked when it exits, so don't poll it yourself.
   Tell the user in one line that you're waiting on the review.
   - exit 0: you have the review JSON.
   - exit 2: the reviewer hit its cap. Stop, and report that the PR needs a human look.
   - exit 3: timed out. Check `tail ~/.local/state/pr-review/log` and `systemctl --user status pr-review.timer`,
     report what you see, and stop. Don't review your own PR in its place.
   - exit 4: the repo isn't watched. Stop.
2. **If `verdict` is `clean`:** apply any minor or nit findings that are clearly right and cheap, or
   none. If you changed anything, commit and push, but don't wait for another review over nits.
   Go to **Finish**.
3. **If `verdict` is `changes` and fix round is already 2:** go to **Finish**, and list the open
   blocker and major findings for the user.
4. **Otherwise, work through every finding:**
   - Verify it against the code first. The reviewer couldn't run anything and can be wrong.
   - **Apply** it if it's right. You may implement it differently from the suggested patch
     if your way is better.
   - **Rebut** it if it's wrong, or if it conflicts with something the user asked for. Give a
     concrete reason, such as "line 40 already guards this" or "the user asked for X".
   - If a finding needs a product decision only the user can make, ask them rather than guessing.
5. Run the repo's tests and lint (see its CLAUDE.md, CONTEXT.md or README). Fix any failures.
6. Commit as `Address review round <n>: <short gist>` and push to the PR branch. Never force-push
   over the reviewed history.
7. Reply on the PR with `gh pr comment <pr> --body-file -`. Number each line to match the review:
   `#1 applied: …` / `#2 rebutted: <reason>`. The next review round reads these replies.
8. Add 1 to fix round. If nothing was pushed (you rebutted everything), go to **Finish**.
   Otherwise go back to step 1 for the new head.

## Finish
- **Final verdict `clean`** (and the PR isn't labeled `hold`, and the user hasn't said to wait): merge it
  with `gh pr merge <pr> --merge`, then confirm with `gh pr view <pr> --json state,mergeCommit`. In
  repos with `merge_deploys: true` a merge deploys, so the deploy gate will prompt the user; that's expected.
- **Not clean** (cap reached, or blocker/major findings still open after 2 fix rounds): don't merge.
- **Never deploy after merging,** even if you planned to earlier. Tell the user it's merged and that
  `/deploy <project>` ships it.

Then report the PR number and URL, the final verdict, merged or not, what you applied and what you rebutted,
and any open items. Send a PushNotification if that tool is available.

## Notes
- Don't open a second PR to address review findings. Push to the same branch.
- The reviewer skips drafts and PRs labeled `no-review`. Mark a PR ready (`gh pr ready`) when you
  want it reviewed.
- If the user wants the review loop skipped for a PR, add the `no-review` label and stop.
