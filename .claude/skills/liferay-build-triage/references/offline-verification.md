# Verifying without a runnable bundle

Most liferay-portal checkouts have no built bundle, so Poshi, Playwright and Arquillian
tests cannot be run. The decisive question in a triage is almost always narrower than
"does the test pass", and the narrow question can usually be answered offline.

Every recipe below is one that has settled a real triage. Each ends with a **control** —
a known-good input pushed through the same harness — because a matching failure signature
means nothing until you have shown the harness can produce a pass.

First, confirm the bundle situation rather than discovering it late:

```bash
grep -n "app.server.parent.dir" app.server.properties
ls -d "$(dirname "$PWD")/bundles" 2>/dev/null || echo "no bundle"
ls <bundle>/osgi/modules | wc -l        # 0 means never built into
```

If there is none, say so up front in the report. "The repro step was not possible and
here is why" is a fine thing to write.

---

## Run a single product class

For a failure that turns on one validator, serializer or util. Compile just that class
against `portal-kernel.jar` plus the module's **prebuilt** classes.

```bash
CP="portal-kernel/portal-kernel.jar\
:modules/core/portal-bootstrap/lib/*\
:modules/apps/<app>/<module>-api/build/classes/java/main"

javac -nowarn -d /tmp/out -cp "$CP" path/to/TheClass.java [its interfaces]
java --add-opens java.base/java.lang.invoke=ALL-UNNAMED -cp "$CP:/tmp/out" Driver
```

Two traps:

- Put the module's `build/classes/java/main` on the classpath rather than recompiling its
  configuration interfaces. Those carry `@Meta.AD` / `ExtendedAttributeDefinition`
  annotations that plain `javac` cannot resolve.
- Without `--add-opens java.base/java.lang.invoke=ALL-UNNAMED`, `petra.string.StringBundler`'s
  static initialiser throws `InaccessibleObjectException`, surfacing as
  `ExceptionInInitializerError` from whatever Liferay util you called. That looks like a
  real rejection and is not.

Expect the *failure* branch to die in `LocaleUtil`/`ResourceBundleUtil` with
`NoClassDefFoundError` — there is no portal runtime to build the i18n message. That stack
is itself proof the throw branch was reached. The accept branch returns cleanly, which is
your control.

---

## Probe selector semantics in a real browser

For "element is not present" when a component moved into a shadow root, an iframe, or a
different wrapper. Playwright's engine and browsers are installed even though the specs
cannot run.

```js
// require the absolute path: playwright-core is hoisted to modules/node_modules
const {chromium} = require('/path/to/liferay-portal/modules/node_modules/playwright-core');
```

Rebuild the before and after DOM as **two separate pages** — putting both in one document
lets an absolute XPath (`//span[...]`) match the control and silently report success.
Then compare what each locator API sees:

```js
const n = await page.evaluate((xp) =>
  document.evaluate(xp, document, null, XPathResult.ORDERED_NODE_SNAPSHOT_TYPE, null).snapshotLength, XPATH);
await page.locator('xpath=' + XPATH).count();
await page.getByText('...').count();
```

Established this way: `getByText` and the other accessible-name queries **pierce open
shadow roots**; XPath does not, in `document.evaluate` or through Playwright. So a
component moving into `attachShadow({mode:'open'})` breaks Poshi/Selenium XPath while
leaving Playwright specs green — which is why a shadow-DOM migration shows up as a
Poshi-only failure.

Poshi can pierce shadow roots, via a CSS locator containing `>>>`
(`LiferaySeleniumUtil` routes it to `LiferayByUtil.cssSelectorWithShadowRoot`):

```
css=.host-selector>>>.inner-selector
```

---

## Compile and diff SCSS

For `CSS Value 96.0956px does not match 100.816px`, or a layout/overlap failure.

```bash
SASS=modules/apps/frontend-js/frontend-js-clay-web/clay/clay-css/node_modules/.bin/sass
CLAY=modules/apps/frontend-js/frontend-js-clay-web/clay/clay-css/src/scss

for side in base target; do
  git show <commit>:path/to/main.scss > /tmp/$side.scss
  "$SASS" --no-source-map --style=expanded --load-path="$CLAY" /tmp/$side.scss /tmp/$side.css
done
diff /tmp/base.css /tmp/target.css
```

Often you do not even need to compile. Two cheaper checks answer most CSS questions:

- **Walk the selector nesting** for the rule in question at both ends. A rule that merely
  moved within the same parent block (Liferay's source formatter sorts selectors
  alphabetically) has identical effect.
- **Order-insensitive diff** — `tr -d ' \t' | sort` both files and diff. If the target is
  a strict superset, nothing was removed, and you only need to check whether the additions
  can match the page under test.

Then confirm the new selectors actually apply: `git grep -n "the-new-class" <target> -- '*.jsp'`.
A rule gated on a class that only one JSP emits cannot affect a different page.

---

## Compare XML serializers

For a golden-file mismatch whose first difference is at the XML declaration.

```java
// Liferay's Document#formattedString() is NodeImpl#formattedString:
//   OutputFormat.createPrettyPrint(), tab indent, "\n" separator, and it rewrites
//   <?xml version="1.0" encoding="UTF-8"?> to <?xml version="1.0"?>
// dom4j's asXML() keeps encoding="UTF-8" and emits compact output.
```

Port `NodeImpl#formattedString` verbatim into a driver, run both against the fixture with
`lib/portal/dom4j.jar`, and print the first differing index. The pre-change serializer
reproducing the fixture byte-for-byte is your control.

---

## Diff upgraded third-party jars

For a whole test class failing after a dependency bump. Network is available.

```bash
curl -sO https://repo1.maven.org/maven2/<group>/<artifact>/<ver>/<artifact>-<ver>.jar
# Liferay's patched builds: repository-cdn.liferay.com/nexus/content/groups/public/...
unzip -q -d x <artifact>-<ver>.jar
md5sum $(find x -name '*.class') | sort   # join the two lists to find real differences
javap -p -c <Class>                        # behavioural deltas
```

Diff *patched-old vs stock-old* to isolate a vendor patch, and *stock-old vs stock-new*
to see what upstream changed. This distinguishes "a Liferay patch was dropped in the bump"
from "upstream added a stricter check".

Also worth doing before accepting a library as the culprit: grep the error string in the
new jar. If the message is not in there, the rationale is unsupported — a real bump does
not make an unrelated message its own.
