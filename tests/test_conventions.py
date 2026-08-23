"""Rules the code holds itself to, checked rather than remembered."""
import pathlib
import re

ROOT = pathlib.Path(__file__).resolve().parents[1]
TEMPLATES = ROOT / "app" / "templates"
STATIC = ROOT / "app" / "static"
STYLESHEET = STATIC / "css" / "app.css"


# The tokens run from the dark block to the end of the light one. Everything
# after that is ordinary stylesheet and must reach for var().
_START = ':root, [data-theme="dark"]'
_END = '[data-theme="dark"] .lightonly'


def _token_block():
    css = STYLESHEET.read_text()
    return css[css.index(_START):css.index(_END)]


def test_every_colour_comes_from_the_token_block():
    """One source for a colour, so a theme is a swap of one block."""
    offenders = []
    files = sorted(TEMPLATES.glob("*.html")) + [STYLESHEET] + sorted(STATIC.glob("js/*.js"))
    for f in files:
        text = f.read_text()
        offset = text.index(_START) if f == STYLESHEET else -1
        stop = text.index(_END) if f == STYLESHEET else -1
        for m in re.finditer(r"#[0-9a-fA-F]{3}(?:[0-9a-fA-F]{3})?\b", text):
            if offset >= 0 and offset <= m.start() < stop:
                continue
            line = text[text.rfind("\n", 0, m.start()) + 1:
                        text.find("\n", m.start())]
            # A fallback beside a getPropertyValue is the token, spelled out
            # for the one frame before the stylesheet has parsed.
            if "href=" in line or 'content="#' in line or "getPropertyValue" in line:
                continue
            offenders.append(f"{f.name}: {line.strip()[:70]}")
    assert not offenders, "hex colours outside the token block:\n" + "\n".join(offenders)


def test_the_dark_and_light_themes_define_the_same_tokens():
    """A token defined in one theme and not the other is a hole in the theme."""
    tokens = _token_block()
    dark = tokens[:tokens.index('[data-theme="light"]')]
    light = tokens[tokens.index('[data-theme="light"]'):]
    def names(s):
        return {m.group(1) for m in re.finditer(r"(--[a-z0-9-]+)\s*:", s)}
    missing = names(dark) - names(light)
    assert not missing, f"light theme never defines {sorted(missing)}"


def test_no_template_reaches_for_a_python_only_strftime_extension():
    """`%-I` is glibc. It is fine in `fmt()`, which is documented; a template
    that grows its own copy is not."""
    for f in TEMPLATES.glob("*.html"):
        if re.search(r"strftime\(", f.read_text()):
            raise AssertionError(f"{f.name} formats time itself; use fmt()")


def test_the_shared_helpers_are_not_reimplemented_per_page():
    """M1 and M2. The escape helper and the source picker each existed twice."""
    for name in ("guide.html", "recordings.html"):
        text = (TEMPLATES / name).read_text()
        assert "function esc(" not in text, f"{name} redefines esc; use CE.esc"
        assert "multibody" not in text or "CE.SourcePicker" in text
    # The option row and the Plex setting renderer existed twice and had
    # already drifted: one panel showed Plex's own explanations, the other
    # did not.
    for path in (STATIC / "js" / "app.js", TEMPLATES / "recordings.html"):
        text = path.read_text()
        assert "function field(" not in text, f"{path.name} redefines field; use CE.settingField"
        assert "function row(owner" not in text, f"{name} redefines row; use CE.optRow"


def test_every_migration_is_listed_rather_than_only_in_the_schema():
    """`CREATE TABLE IF NOT EXISTS` skips a table that exists, so a column
    added to the schema string alone never reaches an install."""
    dbpy = (ROOT / "app" / "db.py").read_text()
    schema = dbpy[dbpy.index("SCHEMA = "):dbpy.index("MIGRATIONS")]
    migrations = dbpy[dbpy.index("MIGRATIONS"):]
    for column in re.findall(r"ALTER TABLE (\w+) ADD COLUMN (\w+)", migrations):
        assert column[1] in schema, \
            f"{column[0]}.{column[1]} is migrated in but missing from the schema"


def test_every_script_is_asked_for_by_build():
    """A deploy has to reach the browser. Unversioned, the scripts were served
    from cache and a shipped fix looked like it had never been made."""
    base = (TEMPLATES / "base.html").read_text()
    for m in re.finditer(r'<(?:script src|link rel="stylesheet" href)="(/static/[^"]+)"', base):
        assert "?v=" in m.group(1), f"{m.group(1)} is not versioned"
