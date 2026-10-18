# T3 Code PR Backlog Audit

By web3theo ([@web3_theo](https://x.com/web3_theo)).

Every open pull request on [pingdotgg/t3code](https://github.com/pingdotgg/t3code), test-merged into the latest main: what to merge, which PRs fix the same bug, what can be closed, and what is worth taking over.

**Live page:** https://theophilus2003.github.io/t3-pr-audit/

A GitHub Action reruns the audit every 3 hours (`.github/workflows/refresh.yml`) and republishes the page. To refresh it now, open **Actions → Refresh audit → Run workflow**.

- `audit/run.py` lists the open PRs, test-merges each branch with git, finds overlapping PRs and writes `index.html`.
- `audit/curated.json` holds the hand-picked lanes. Their status chips update automatically.
- `history.json` keeps one data point per refresh for the trend chart.

Independent audit, not affiliated with the T3 Code team.
