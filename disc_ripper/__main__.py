import sys

from .cli import COMMANDS


def _run() -> int:
    argv = sys.argv[1:]
    # a command (or --help) -> command-line mode; nothing (or --offscreen-smoke) -> the window
    if argv and (argv[0] in COMMANDS or argv[0] in ("-h", "--help", "--json")):
        from .cli import main as cli_main
        return cli_main(argv)
    from .app import main
    return main()


sys.exit(_run())
