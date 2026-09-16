"""
Tests for `pre_classify` — the gate that decides a failure is infrastructure
and never needs a model.

It is the cheapest rule in the pipeline and the most dangerous one. A false
positive here is invisible: the failure is dropped before the prompt is built,
the run reports `0 to classify`, and the Slack message says nothing is wrong.
Nobody re-reads a build that claimed to have no work.
"""

from testray_analytics.analysis.prompt_helpers import pre_classify

# Stable 19704, trimmed. LPD-105774 bumped site-staticexport-api to 1.1.0; the
# module has never been published, so `baseline` resolves (,1.1.0) and finds
# nothing. Gradle then lists every repository it searched — which is why a bare
# `repository-cdn.liferay.com` pattern used to swallow this.
BASELINE_NO_PUBLISHED_VERSION = (
    "[exec] * What went wrong: [exec] Execution failed for task "
    "':apps:site:site-staticexport-api:baseline'. [exec] > Could not resolve "
    "all files for configuration ':apps:site:site-staticexport-api:baseline'. "
    "[exec] > Could not find any matches for "
    "com.liferay:com.liferay.site.staticexport.api:(,1.1.0) as no versions of "
    "com.liferay:com.liferay.site.staticexport.api are available. [exec] "
    "Searched in the following locations: [exec] - file:/opt/dev/projects/"
    "github/liferay-portal/.m2/com/liferay/com.liferay.site.staticexport.api/ "
    "[exec] - https://repository-cdn.liferay.com/nexus/content/groups/public/"
    "com/liferay/com.liferay.site.staticexport.api/maven-metadata.xml"
)


def test_a_missing_published_version_is_not_an_environment_failure():
    """The regression this file exists for.

    A commit in range bumped a module past its only published version. That is
    a product defect with a named culprit, and it reached prod labelled
    ENV_DEPENDENCY because the Nexus host appears in Gradle's
    "Searched in the following locations" list.
    """
    assert pre_classify(BASELINE_NO_PUBLISHED_VERSION) is None, \
        "must reach the classifier — a commit in range caused this"


def test_a_real_cdn_outage_is_still_an_environment_failure():
    """The other direction: tightening the pattern must not disarm it."""
    assert pre_classify(
        "[exec] Could not GET 'https://repository-cdn.liferay.com/nexus/"
        "content/groups/public/com/liferay/foo.jar'. Received status code 502 "
        "from server: Bad Gateway") == "ENV_DEPENDENCY"


def test_a_cdn_read_timeout_is_still_an_environment_failure():
    assert pre_classify(
        "Could not resolve com.liferay:foo:1.0.0. repository-cdn.liferay.com "
        "failed: Read timed out") == "ENV_DEPENDENCY"


def test_gradle_download_chatter_is_still_an_environment_failure():
    assert pre_classify(
        "Downloaded https://repository-cdn.liferay.com/nexus/foo.jar"
    ) == "ENV_DEPENDENCY"


def test_tensorflow_is_untouched():
    assert pre_classify(
        "java.lang.UnsatisfiedLinkError: org.tensorflow native library"
    ) == "ENV_DEPENDENCY"


def test_no_error_text_is_its_own_answer():
    """The aggregate row's shape. Distinct from None: there is nothing to
    reason about, rather than something a classifier should look at."""
    assert pre_classify("") == "NO_ERROR"
    assert pre_classify(None) == "NO_ERROR"


def test_an_ordinary_assertion_failure_reaches_the_classifier():
    assert pre_classify(
        "org.junit.ComparisonFailure: expected:<Download[]> but was:"
        "<Download[ Folder]>") is None
