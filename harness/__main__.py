import sys

try:
    from .cli import main

    code = main()
except ModuleNotFoundError as exc:  # almost always: launched with the wrong Python
    sys.stderr.write(
        f"Missing dependency '{exc.name}': this Python ({sys.executable}) is not the project's environment.\n"
        "Use the launcher instead:  .\\harness.cmd <command>\n"
        "or:  C:\\Users\\DELL\\.venvs\\agenticps\\Scripts\\python -m harness <command>\n"
    )
    code = 2

sys.exit(code)
