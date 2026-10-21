import asyncio
import os
import subprocess as process
from os import system as run_system


async def shell_calls() -> None:
    # ruleid: shell-true-or-os-shell
    process.run(["echo", "bad"], shell=True)
    # ruleid: shell-true-or-os-shell
    run_system("echo bad")
    # ruleid: shell-true-or-os-shell
    os.popen("echo bad")
    # ruleid: shell-true-or-os-shell
    await asyncio.create_subprocess_shell("echo bad")


def safe_calls() -> None:
    # ok: shell-true-or-os-shell
    process.run(["echo", "safe"])
    # ok: shell-true-or-os-shell
    os.spawnvp(os.P_WAIT, "echo", ["echo", "safe"])
