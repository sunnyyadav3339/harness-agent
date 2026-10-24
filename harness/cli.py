"""Command line: python -m harness <command>."""

from __future__ import annotations

import argparse
import asyncio
import sys

from dotenv import load_dotenv

from . import platform_win
from .config import REPO_ROOT, get_settings
from .logging_setup import setup_logging


def _bootstrap() -> None:
    load_dotenv(REPO_ROOT / ".env")  # exposes SECRET_* to the secret store
    platform_win.init_process()
    if sys.platform == "win32":
        for stream in (sys.stdout, sys.stderr):
            try:
                stream.reconfigure(encoding="utf-8", errors="replace")
            except Exception:
                pass
    import harness.tools  # noqa: F401  (registers tools)

    setup_logging(get_settings().log_level)


async def cmd_run(args) -> int:
    from .channels import build_channel
    from .runtime.runner import Runner
    from .timeutil import CLOCK

    settings = get_settings()
    if args.background:
        settings.browser_foreground = False
    await CLOCK.sync(settings.ntp_server)
    channel = build_channel(settings)
    await channel.start()
    runner = Runner(settings, channel)
    try:
        outcome = await runner.run(" ".join(args.task) if args.task else "", args.skill, trigger="cli",
                                   resume_run_id=getattr(args, "run_id", None),
                                   record=False if args.no_record else None)
    finally:
        await runner.browser.close()
        await runner.router.aclose()
        await channel.stop()
    print(f"\n{outcome.status.upper()}: {outcome.summary}\nsteps={outcome.steps} handoffs={outcome.handoffs} "
          f"time={outcome.seconds}s\nlog: {outcome.run_dir}\\replay.html")
    return 0 if outcome.status == "done" else 1


async def cmd_daemon(args) -> int:
    from .triggers.daemon import Daemon

    await Daemon(get_settings()).serve()
    return 0


async def cmd_doctor(args) -> int:
    import httpx

    from .llm.router import get_router
    from .runlog import find_ffmpeg
    from .skills.loader import SkillLibrary
    from .timeutil import CLOCK
    from .tools.browser import BrowserManager
    from .perception.screen import screen_size

    s = get_settings()
    ok = True

    def line(status: str, label: str, detail: str = "") -> None:
        print(f"  [{status:^4}] {label}{': ' + detail if detail else ''}")

    print(f"Workspace: {s.workspace}")
    s.ensure_dirs()
    skills = SkillLibrary(s.skills_dir)
    found = skills.all()
    line("ok" if found else "warn", "skills", ", ".join(x.name for x in found) or "none found")

    print("Models:")
    router = get_router()
    if not router.deployments:
        ok = False
        line("FAIL", "no model deployments", "set GEMINI_API_KEYS / GROQ_API_KEYS in .env")
    results = await router.warmup()
    for dep_id, status in sorted(results.items()):
        line("ok" if status == "ok" else "FAIL", dep_id, status)
    for group in router.groups:
        healthy = [d for d in router.groups[group] if results.get(d) == "ok"]
        keys = {router.deployments[d].api_key for d in healthy}
        if not healthy:
            ok = False
            line("FAIL", f"group '{group}'", "no working deployment")
        else:
            line("ok" if len(keys) > 1 else "warn", f"group '{group}'",
                 f"{len(healthy)} working deployment(s) on {len(keys)} key(s)"
                 + ("" if len(keys) > 1 else "; add keys from other accounts for rate-limit headroom"))

    print("Machine:")
    line("ok", "screen", "%dx%d physical px" % screen_size())
    synced = await CLOCK.sync(s.ntp_server)
    # The offset is applied to every reading, so a large one is fine; only a failed sync matters.
    line("ok" if synced else "warn", "clock",
         f"system clock off by {CLOCK.offset * 1000:+.1f} ms, corrected via {s.ntp_server}" if synced
         else "NTP unreachable; using the uncorrected system clock")
    ff = find_ffmpeg()
    line("ok" if ff else "warn", "ffmpeg", ff or "not found (no screen recording)")
    from pathlib import Path

    line("ok" if Path(s.chrome_path).exists() else "FAIL", "chrome", s.chrome_path)
    bm = BrowserManager(s)
    line("ok" if await bm.cdp_alive() else "info", "chrome CDP", f"port {s.cdp_port} "
         + ("attached-ready" if await bm.cdp_alive() else "not running yet (started on demand)"))

    print("Channels:")
    if s.telegram_bot_token:
        try:
            async with httpx.AsyncClient(timeout=10) as c:
                r = (await c.get(f"https://api.telegram.org/bot{s.telegram_bot_token}/getMe")).json()
            line("ok" if r.get("ok") else "FAIL", "telegram bot", "@" + r.get("result", {}).get("username", "?")
                 if r.get("ok") else r.get("description", ""))
            line("ok" if s.allowed_chat_ids() else "FAIL", "telegram chat ids",
                 ", ".join(s.allowed_chat_ids()) or "missing: run `python -m harness telegram-setup`")
        except Exception as exc:
            line("FAIL", "telegram", str(exc))
    else:
        line("warn", "telegram", "not configured (handoffs only on console)")
    line("ok" if s.imap_user else "info", "email trigger", s.imap_user or "not configured")
    from .credentials import SECRETS

    line("ok", "secrets available", ", ".join(SECRETS.available_names()) or "none")
    await router.aclose()
    print("\nREADY" if ok else "\nNOT READY: fix the FAIL lines above")
    return 0 if ok else 1


async def cmd_skills(args) -> int:
    from .skills.loader import SkillLibrary, load_skill

    lib = SkillLibrary(get_settings().skills_dir)
    for path in sorted(lib.root.glob("*/SKILL.md")):
        try:
            sk = load_skill(path)
            print(f"OK   {sk.name:32} macros={len(sk.macros)} triggers={sk.triggers[:4]}")
        except Exception as exc:
            print(f"BAD  {path}: {exc}")
    if args.match:
        skill, ranking = lib.match(args.match)
        print(f"\nmatch -> {skill.name if skill else None}   ranking={ranking[:5]}")
    return 0


async def cmd_secret(args) -> int:
    import getpass

    import keyring

    from .credentials import KEYRING_SERVICE

    if args.action == "set":
        value = getpass.getpass(f"value for {args.name.upper()} (hidden): ")
        keyring.set_password(KEYRING_SERVICE, args.name.upper(), value)
        print(f"stored {args.name.upper()} in Windows Credential Manager")
    elif args.action == "delete":
        keyring.delete_password(KEYRING_SERVICE, args.name.upper())
        print("deleted")
    return 0


async def cmd_telegram_setup(args) -> int:
    from .channels.telegram import discover_chat_ids

    token = get_settings().telegram_bot_token
    if not token:
        print("Set TELEGRAM_BOT_TOKEN in .env first (create a bot with @BotFather).")
        return 1
    print("Send any message to your bot from your phone now; waiting up to 60s ...")
    chats = await discover_chat_ids(token, 60)
    for cid, name in chats:
        print(f"  chat id {cid}  ({name})")
    if chats:
        print("Put the id(s) in TELEGRAM_CHAT_IDS in .env")
    else:
        print("No messages received.")
    return 0


async def cmd_replay(args) -> int:
    from pathlib import Path

    from .runlog import build_replay

    print(build_replay(Path(args.run_dir)))
    return 0


async def cmd_learn(args) -> int:
    from .skills.demo import record_demo

    path = await record_demo(get_settings(), args.name, args.description or "")
    print(f"draft skill written to {path}" if path else "no skill produced")
    return 0 if path else 1


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="harness", description="Generic computer-use agent harness")
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("run", help="run one task now")
    p.add_argument("task", nargs="+")
    p.add_argument("--skill", help="force a skill by name")
    p.add_argument("--no-record", action="store_true", help="skip screen recording")
    p.add_argument("--background", action="store_true",
                   help="dev mode: keep the agent's Chrome off-screen and unfocused")
    p.set_defaults(fn=cmd_run)

    p = sub.add_parser("resume", help="resume a crashed/stopped run from its checkpoint")
    p.add_argument("run_id")
    p.add_argument("--no-record", action="store_true")
    p.add_argument("--background", action="store_true")
    p.set_defaults(fn=cmd_run, task=None, skill=None)

    sub.add_parser("daemon", help="start the trigger daemon (schedules, Telegram, email)").set_defaults(fn=cmd_daemon)
    sub.add_parser("doctor", help="check keys, models, browser, clock, channels").set_defaults(fn=cmd_doctor)

    p = sub.add_parser("skills", help="validate skills; --match to test routing")
    p.add_argument("--match")
    p.set_defaults(fn=cmd_skills)

    p = sub.add_parser("secret", help="store a secret in Windows Credential Manager")
    p.add_argument("action", choices=["set", "delete"])
    p.add_argument("name")
    p.set_defaults(fn=cmd_secret)

    sub.add_parser("telegram-setup", help="discover your Telegram chat id").set_defaults(fn=cmd_telegram_setup)

    p = sub.add_parser("replay", help="rebuild replay.html for a run folder")
    p.add_argument("run_dir")
    p.set_defaults(fn=cmd_replay)

    p = sub.add_parser("learn", help="record a demonstration and draft a skill from it")
    p.add_argument("--name", required=True)
    p.add_argument("--description")
    p.set_defaults(fn=cmd_learn)

    args = parser.parse_args(argv)
    _bootstrap()
    try:
        return asyncio.run(args.fn(args))
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    sys.exit(main())
