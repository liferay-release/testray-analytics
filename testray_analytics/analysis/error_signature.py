"""
error_signature.py — normalize error text into a stable signature, and derive
the `clusterKey` from it (§7, §12).

One normalization function, two uses:

* **Clustering (§7)** — failures sharing a root cause share a `clusterKey`, so
  the view groups them and the classifier can judge a cluster once instead of
  each member.
* **Changed-failure detection (§12)** — a `FAILED→FAILED` row is a triage
  candidate only when `normalize(baseline) != normalize(target)`. Without this
  a baseline that already had failures makes PASSED→FAILED undercount real
  regressions (decision #13).

Both uses fail in opposite directions, which is what the normalization has to
balance:

* **Under-normalizing** leaves volatile tokens (line numbers, durations, ports,
  UUIDs) in the signature. Clusters shatter into singletons, and every
  pre-existing baseline failure looks "changed" and floods the run. Measured on
  a real build: Testray's own subtask grouping is exact-match on raw error text
  and left 544 of 767 failures (71%) as singletons — three identical upgrade
  failures split three ways because the duration differed by two seconds.
* **Over-normalizing** merges genuinely different causes. One Jira ticket then
  covers two bugs and one is forgotten when the other is fixed. This is the
  dangerous direction because nothing looks wrong — the cluster is simply
  holding the wrong members.

So: strip tokens that vary run-to-run for the *same* cause, and keep everything
that distinguishes causes — exception types, messages, quoted literals,
selectors, identifiers.

**Versioning.** Any behavioural change here changes the key for the same input,
which breaks re-clustering continuity and §11 dedup. `SIGNATURE_VERSION` is
embedded in every key (`v1:<hash>`), so a stored key always declares which
generation produced it; bump it on any behavioural change and re-key the
retention window (open-Q #9).
"""

import hashlib
import re

# Bump on ANY behavioural change to normalize() — see open-Q #9. Stored keys
# carry this prefix, so a bump makes old and new keys visibly incomparable
# rather than silently different.
SIGNATURE_VERSION = "v1"

# How many stack frames to keep when an error is nothing but a trace. Enough to
# tell two traces apart, few enough that a deep-frame difference in the same
# failure does not split the cluster.
_TRACE_FRAMES_KEPT = 3

_FRAME_RE = re.compile(r"^\s*at\s+\S+\(.*?\)\s*$", re.MULTILINE)

# Playwright reports the test identity on the SAME line as the error:
#   › some/spec.ts:110:2 › Suite › Test title @LPD-67470    Error: locator.click: …
# The prefix is unique per test, so leaving it in makes every Playwright
# failure its own cluster — 434 of 515 singletons on the build measured. Strip
# it and keep the error. Measured on build 36424 (767 failures): 552 -> 465
# clusters, and it is what surfaces shared causes like the 14 tests all failing
# on "login via api failed / ERR_NAME_NOT_RESOLVED".
_PLAYWRIGHT_ID_RE = re.compile(r"^\s*›.*?(?=\bError:|\bTimeoutError:|$)", re.DOTALL)

# Order matters: the specific patterns must run before the generic number
# collapse, or the number collapse eats the parts that identify them.
_SUBSTITUTIONS = (
    # UUIDs (8-4-4-4-12)
    (re.compile(r"\b[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\b",
                re.IGNORECASE), "<uuid>"),
    # ISO-8601 timestamps, with or without fractional seconds / offset
    (re.compile(r"\b\d{4}-\d{2}-\d{2}[t ]\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:z|[+-]\d{2}:?\d{2})?\b",
                re.IGNORECASE), "<ts>"),
    # Plain dates and clock times left over from the above
    (re.compile(r"\b\d{4}-\d{2}-\d{2}\b"), "<date>"),
    (re.compile(r"\b\d{2}:\d{2}:\d{2}\b"), "<time>"),
    # Java identity hashes: com.liferay.Foo@1a2b3c4d
    (re.compile(r"@[0-9a-f]{6,}\b", re.IGNORECASE), "@<id>"),
    # Long hex-ish tokens that contain a digit — AWS host ids, sha hashes,
    # session ids. Requiring a digit keeps ordinary words out.
    (re.compile(r"\b(?=[0-9a-f]*\d)[0-9a-f]{8,}\b", re.IGNORECASE), "<hex>"),
    # Ports in URLs / host:port pairs
    (re.compile(r"(?<=:)//([^/\s:]+):\d+"), r"//\1:<port>"),
    # Memory addresses
    (re.compile(r"\b0x[0-9a-f]+\b", re.IGNORECASE), "<addr>"),
)

# Everything numeric that survives — line numbers, durations, build numbers,
# counts, ports outside URLs. Collapsing all of them is deliberate: it is what
# makes "failed in 107 seconds" and "failed in 109 seconds" one cluster.
# Nothing in the corpus distinguishes two causes by a bare integer alone.
_NUMBER_RE = re.compile(r"\d+")

_WS_RE = re.compile(r"\s+")

# Cap the signature. Some failures carry the whole build log — 2.4KB of ant
# output — where the actual error is in the first line or two and the rest is
# repeating chatter ("… 1 hour 12 minutes until timeout" logged once more in a
# slower run). Comparing the full text makes two runs of the SAME failure
# differ at character ~2300 and read as a changed error, which is precisely the
# false positive §12 warns floods a run. Observed: normalized similarity 0.977
# between two identical upgrade failures.
#
# A head-only signature is the standard answer for log clustering: the head
# carries the cause, the tail carries the noise. The risk is two genuinely
# different failures that share a long preamble; at 600 chars the exception
# type and message are always inside the window, so that stays unlikely.
_SIGNATURE_MAX_CHARS = 600

# Matches a phrase of >=12 chars repeated back-to-back, so "ABCABCABC" -> "ABC".
# Non-greedy so it finds the shortest repeating unit. 12 is long enough that
# ordinary English repetition ("the the") is untouched.
_REPEAT_RE = re.compile(r"(.{12,}?)\1+")


def normalize(text) -> str:
    """Reduce error text to a signature that is stable across runs of the same
    failure. Returns "" for missing/blank input — callers must treat that as
    *unknown*, never as a match."""
    if text is None:
        return ""
    s = str(text).strip()
    if not s:
        return ""

    if s.lstrip().startswith("›"):
        stripped = _PLAYWRIGHT_ID_RE.sub("", s, count=1).strip()
        # Only take it if something survived — a report with no "Error:" marker
        # would otherwise normalize to nothing and match every other unknown.
        if stripped:
            s = stripped

    s = _message_part(s)
    s = _error_bearing_line(s)

    for pattern, replacement in _SUBSTITUTIONS:
        s = pattern.sub(replacement, s)
    s = _NUMBER_RE.sub("<n>", s)

    # Case and whitespace last, so the patterns above can rely on layout.
    s = _WS_RE.sub(" ", s).strip().casefold()
    s = _collapse_repeats(s)
    return s[:_SIGNATURE_MAX_CHARS]


def _collapse_repeats(s: str) -> str:
    """Collapse an immediately-repeated phrase to a single copy.

    Build logs repeat status lines while they wait — "1 hour 12 minutes until
    timeout" every few minutes — so a slower run of the *same* failure emits
    the line one extra time. After number normalization those repeats are
    byte-identical, and they sit early enough in the text that truncation does
    not remove them, so two identical failures still differ. Collapsing
    "ABCABC" to "ABC" makes the count of repeats irrelevant, which is the point:
    how many times a countdown printed is not part of the failure's identity.
    """
    return _REPEAT_RE.sub(r"\1", s)


# Words that mark a line as carrying the actual failure reason. Inherited from
# the previous Release Analytics Platform's `changed_failures._signature()`,
# which this module supersedes — that approach (take ONE error-bearing line
# rather than the whole blob) is what makes 2KB build logs comparable at all.
_ERR_HINT_RE = re.compile(
    r"(error|exception|not present|not found|does not match|timeout|timed out|"
    r"cannot|expected|assert|failed)", re.IGNORECASE)


def _error_bearing_line(s: str) -> str:
    """Pick the line that states the failure.

    Errors here range from a one-line assertion to a 2.4KB ant log where the
    cause is one line among dozens of progress and countdown lines. Comparing
    whole blobs makes two runs of the same failure differ on incidental
    output — a countdown printed once more in a slower run was enough. Taking
    the first error-bearing line ignores all of it by construction.

    Falls back to the first non-descriptor line, then to the raw text, so a
    failure whose wording we do not recognise still gets a signature instead
    of collapsing to "unknown".
    """
    lines = [l.strip() for l in s.splitlines() if l.strip()]
    if not lines:
        return s
    # Playwright/Poshi descriptor lines name the test, not the failure.
    candidates = [l for l in lines if not l.startswith(("›", ">"))] or lines
    for line in candidates:
        if _ERR_HINT_RE.search(line):
            return line
    return candidates[0]


def _message_part(s: str) -> str:
    """Keep the message and drop the bulk of a stack trace.

    A Java failure is usually one message line followed by dozens of frames;
    the frames vary with unrelated code changes, so including them all splits
    clusters that share a cause. But a trace with no message line must not
    reduce to nothing — an empty signature reads as "unknown" and compares
    equal to every other unknown, silently merging unrelated failures. So when
    there is no message, keep the first few frames instead.
    """
    lines = s.splitlines()
    message_lines, frames = [], []
    for line in lines:
        (frames if _FRAME_RE.match(line) else message_lines).append(line)

    message = "\n".join(l for l in message_lines if l.strip())
    if message.strip():
        return message
    return "\n".join(frames[:_TRACE_FRAMES_KEPT])


def signatures_differ(baseline_error, target_error) -> bool:
    """True when the failure reason genuinely changed (§12's FAILED→FAILED
    condition).

    An unknown error on either side returns False. An unknown baseline cannot
    establish that anything changed, and treating it as changed would surface
    every pre-existing failure in the run — the exact flood §12 is trying to
    avoid.
    """
    a, b = normalize(baseline_error), normalize(target_error)
    if not a or not b:
        return False
    return a != b


def cluster_key(culprit_file, test_name, error) -> str:
    """`v<N>:<sha256-16>` over culprit_file + test name + normalized error.

    `culprit_file` is an LLM output, so the full key is only knowable *after*
    classification — pre-send grouping (the `by-cluster` mode) can key on the
    error signature alone. Passing None for it is therefore legitimate and
    yields the signature-only key.

    `test_name` is deliberately **only a fallback**, used when the error is
    blank. Folding it into every key would make each test its own cluster and
    defeat the point — the 34 different tests failing on one ElementNotFound
    are exactly what we want grouped. But failures with no error text at all
    must not all collapse into one giant "unknown" cluster either, so those
    fall back to the test identity.
    """
    normalized_error = normalize(error)
    signature = normalized_error or f"noerror:{normalize(test_name)}"
    material = f"{(culprit_file or '').strip().casefold()}\x00{signature}"
    digest = hashlib.sha256(material.encode("utf-8")).hexdigest()[:16]
    return f"{SIGNATURE_VERSION}:{digest}"
