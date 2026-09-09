"""
cli.py — `testray-analysis <prepare|classify|submit>` dispatcher.

Each subcommand delegates to that module's own argparse `main()`, so
`testray-analysis prepare --help` shows prepare's flags. The pipeline is:

  prepare   read builds over REST, compute the diff + hunks, write a run bundle
  classify  send the bundle to the Anthropic API (or classify in a Claude Code
            session and write results.json by hand)
  submit    validate results, render the report, hand verdicts to the writer

Two more wrap it, and they are a producer/consumer pair rather than steps:
`scan` registers work for a routine's failing builds, `watch` drains whatever
is registered — by the scanner or by Run Triage in the UI — through the
pipeline above.
"""

import sys

_SUBCOMMANDS = ("prepare", "classify", "submit", "scan", "watch", "slack",
                 "preflight")


def _usage() -> None:
    print(
        "usage: testray-analysis <prepare|classify|submit|scan|watch|slack|preflight>\n"
        "  prepare   read builds over REST, compute the diff, write a run bundle\n"
        "  classify  send the bundle to the Anthropic API\n"
        "  submit    validate results and hand verdicts to the Testray writer\n"
        "  scan      queue a routine's unexplained failures (never classifies)\n"
        "  watch     drain the queue — scanner jobs and Run Triage requests\n"
        "  slack     re-render a run bundle as the Slack message Jenkins posts\n"
        "  preflight check credentials, OAuth scopes and the triage Objects\n"
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
    elif sub == "preflight":
        from .preflight import main as run
    elif sub == "slack":
        from .slack_message import main as run
    elif sub == "scan":
        from .scan import main as run
    elif sub == "watch":
        from .runner import main as run
    else:
        from .submit import main as run

    try:
        run()
    except KeyboardInterrupt:
        # A classify batch runs for minutes, so Ctrl-C is a normal way to stop
        # one. Report it as a decision, not a crash — and say what survived.
        print("\nInterrupted. Any completed batches are in "
              "results.partial.jsonl; re-run to resume from there.",
              file=sys.stderr)
        raise SystemExit(130)   # 128 + SIGINT, the conventional shell code


if __name__ == "__main__":
    main()
