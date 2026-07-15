"""
cli.py — `testray-analysis <prepare|classify|submit>` dispatcher.

Each subcommand delegates to that module's own argparse `main()`, so
`testray-analysis prepare --help` shows prepare's flags. The pipeline is:

  prepare   read builds over REST, compute the diff + hunks, write a run bundle
  classify  send the bundle to the Anthropic API (or classify in a Claude Code
            session and write results.json by hand)
  submit    validate results, render the report, hand verdicts to the writer
"""

import sys

_SUBCOMMANDS = ("prepare", "classify", "submit")


def _usage() -> None:
    print(
        "usage: testray-analysis <prepare|classify|submit> [args]\n"
        "  prepare   read builds over REST, compute the diff, write a run bundle\n"
        "  classify  send the bundle to the Anthropic API\n"
        "  submit    validate results and hand verdicts to the Testray writer\n"
        "\nRun `testray-analysis <subcommand> --help` for subcommand flags."
    )


def main() -> None:
    argv = sys.argv[1:]
    if not argv or argv[0] in ("-h", "--help"):
        _usage()
        raise SystemExit(0 if argv else 2)

    sub = argv[0]
    if sub not in _SUBCOMMANDS:
        print(f"Unknown subcommand: {sub!r}\n", file=sys.stderr)
        _usage()
        raise SystemExit(2)

    # Re-shape argv so the delegated main() sees a clean program name + its args.
    sys.argv = [f"testray-analysis {sub}", *argv[1:]]

    if sub == "prepare":
        from .prepare import main as run
    elif sub == "classify":
        from .classify import main as run
    else:
        from .submit import main as run
    run()


if __name__ == "__main__":
    main()
