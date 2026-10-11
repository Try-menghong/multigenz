"""Run a plan but hard-crash (os._exit) right after git fast-forwards the integration branch."""
import asyncio
import os
import sys
from pathlib import Path

from multigenz.config import load_policy
from multigenz.contracts.task import PlanDocument
from multigenz.core.driver import start_run
from multigenz.execution.git import Git

policy_file, repo, plan_file = sys.argv[1:4]
orig = Git.ff_merge


def crashing(self, git_dir, work_tree, sha):
    orig(self, git_dir, work_tree, sha)
    os._exit(9)


Git.ff_merge = crashing
policy = load_policy(policy_file)
plan = PlanDocument.model_validate_json(Path(plan_file).read_text())
asyncio.run(start_run(policy, objective="crash test", repo=Path(repo), plan=plan, home=Path(os.environ["MULTIGENZ_HOME"])))
