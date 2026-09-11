"""
Tests for the hop from a failure to the log that explains it.

Testray records an axis console on the failing case result itself, which is a
shorter route than it looked: the evidence was thought to be two hops away
(top-level console, then the downstream Jenkins job behind SSO), but that was
inferred from the aggregate `Top Level Build` row, whose attachments are all
top-level. A real axis result carries its own.

What is pinned here is the discrimination between the two, because getting it
wrong is worse than having no link at all.
"""

import json

from testray_analytics.analysis.prepare import _axis_console_url

AXIS = ("https://storage.cloud.google.com/testray-results/2026-09/test-1-42/"
        "test-portal-testsuite-upstream(master)/1493/"
        "modules-integration-postgresql163_stable/0/0/"
        "jenkins-console.txt.gz?authuser=0")
TOP = ("https://storage.cloud.google.com/testray-results/2026-09/test-1-42/"
       "test-portal-testsuite-upstream(master)/1493/"
       "jenkins-console.txt.gz?authuser=0")


def _att(*entries):
    """Testray stores this field as a JSON *string*, not a list."""
    return json.dumps(list(entries))


def test_the_axis_console_is_found():
    assert _axis_console_url(_att(
        {"name": "Docker Log (postgresql.log)", "url": "https://x/docker.gz"},
        {"name": "Jenkins Console", "url": AXIS},
    )) == AXIS


def test_the_top_level_console_is_refused():
    """`Top Level Build` carries `Jenkins Console (Top Level)`, whose deepest
    message on a broken build is `Timeout waiting for update` — infrastructure
    language for a compile failure. Offering it as *the* log would confirm the
    "this is CI, not a commit" reading the classifier already reaches wrongly,
    so no link is the correct answer, not a fallback."""
    assert _axis_console_url(_att(
        {"name": "Build Report (Top Level)", "url": "https://x/report.gz"},
        {"name": "Jenkins Console (Top Level)", "url": TOP},
    )) is None


def test_a_real_payload_keeps_only_the_console():
    """The live shape, so a field rename upstream fails here rather than in a
    Slack message at 3am."""
    assert _axis_console_url(_att(
        {"name": "Docker Log (i-047_postgresql.log)", "value": "…", "url": "https://x/d.gz"},
        {"name": "GC Log (tomcat-gc-0.log)", "value": "…", "url": "https://x/g.gz"},
        {"name": "Jenkins Console", "value": "…", "url": AXIS},
        {"name": "modules-integration-postgresql163_stable/0/0", "value": "/#/pr"},
    )) == AXIS


def test_junk_never_kills_a_run():
    """The field is free-form on some importers. A triage run must not die
    because one row's attachments did not parse."""
    for junk in (None, "", "not json", "[]", '["a string"]', '{"name": "x"}',
                 _att({"name": "Jenkins Console"}),
                 _att({"name": "Jenkins Console", "url": "  "})):
        assert _axis_console_url(junk) is None
