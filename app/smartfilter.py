"""The smart filter: a nested condition tree, compiled to SQL.

A smart pass asks a question of the guide rather than naming one team or one
programme. "Any comedy or sci-fi, not rated TV-MA, first shown after 2015" is
a thing a person wants and a thing Plex cannot be told, so CouchElephant holds
the rule and books each airing itself.

The tree is JSON and nests to any depth:

    {"op": "all", "nodes": [
       {"field": "genre", "cmp": "is", "value": "Comedy"},
       {"op": "any", "nodes": [ ... ]}
    ]}

A group has `op` (all, any or none) and `nodes`. A condition has `field`, `cmp`
and `value`, plus an optional `blank` saying whether an item with no value for
that field should count as a match.

WHY BLANK IS A CHOICE AND NOT A DEFAULT. The guide does not rate everything.
On a real server 294 of 400 shows carried a content rating and 40 of 159 sports
did. So "content rating is not TV-MA" quietly admits every unrated programme
unless somebody decides what a blank means. The condition carries the decision,
and the panel shows it.

Everything here is parameterised. A field name is only ever used to look up a
fixed SQL fragment from FIELDS, never interpolated, so a filter out of the
database cannot reach the query.
"""
import datetime

MAX_NODES = 60          # a tree past this is a mistake, not a filter
MAX_DEPTH = 8


class FilterError(ValueError):
    """A tree that cannot be compiled. The message is shown to the user."""


# ---------------------------------------------------------------- the fields
#
# `sql` is the expression a condition tests. `kind` picks which comparisons
# are offered and how the value is read. `values` names the list the panel
# should offer, and is filled in by `field_options()`.

FIELDS = {
    "title":      {"label": "Title", "kind": "text", "sql": "p.title"},
    "series":     {"label": "Series", "kind": "text", "sql": "p.grandparent_title"},
    "summary":    {"label": "Description", "kind": "text", "sql": "p.summary"},
    "genre":      {"label": "Genre", "kind": "tag", "sql": "p.genres",
                   "values": "genres"},
    "rating":     {"label": "Content rating", "kind": "choice",
                   "sql": "p.content_rating", "values": "ratings"},
    "kind":       {"label": "Kind", "kind": "choice", "sql": "p.section",
                   "values": "kinds"},
    "year":       {"label": "Year", "kind": "number", "sql": "p.year"},
    "duration":   {"label": "Length in minutes", "kind": "number",
                   "sql": "(p.duration / 60000)"},
    "aired":      {"label": "First shown", "kind": "date",
                   "sql": "p.originally_available"},
    "channel":    {"label": "Channel", "kind": "choice", "sql": "a.channel_vcn",
                   "values": "channels"},
    "network":    {"label": "Network", "kind": "choice", "sql": "c.network",
                   "values": "networks"},
    # Resolution is stored as text and '1080' sorts below '720' as a string,
    # which is how HD once dropped every 1080 channel. Compare as a number.
    "hd":         {"label": "High definition", "kind": "bool",
                   "sql": "CAST(a.resolution AS INTEGER) >= 720"},
    "live":       {"label": "Live broadcast", "kind": "bool", "sql": "a.premiere = 1"},
    "weekday":    {"label": "Day of the week", "kind": "choice",
                   "sql": "CAST(strftime('%w', a.begins_at, 'unixepoch', 'localtime')"
                          " AS INTEGER)", "values": "weekdays"},
    "hour":       {"label": "Start hour", "kind": "number",
                   "sql": "CAST(strftime('%H', a.begins_at, 'unixepoch', 'localtime')"
                          " AS INTEGER)"},
}

# What each kind of field can be asked. The panel reads this, so the two can
# never disagree about what is offered.
COMPARISONS = {
    "text":   [("contains", "contains"), ("!contains", "does not contain"),
               ("is", "is"), ("!is", "is not"),
               ("starts", "starts with"), ("ends", "ends with")],
    "tag":    [("is", "is"), ("!is", "is not")],
    "choice": [("is", "is"), ("!is", "is not")],
    "number": [("is", "is"), ("!is", "is not"), ("gt", "is more than"),
               ("lt", "is less than")],
    "date":   [("after", "is after"), ("before", "is before")],
    "bool":   [("yes", "yes"), ("no", "no")],
}

GROUP_OPS = {"all": "AND", "any": "OR", "none": "OR"}

KINDS = [("shows", "TV show"), ("movies", "Movie"), ("sports", "Sport")]
WEEKDAYS = [(0, "Sunday"), (1, "Monday"), (2, "Tuesday"), (3, "Wednesday"),
            (4, "Thursday"), (5, "Friday"), (6, "Saturday")]


# ---------------------------------------------------------------- compiling

def like(v):
    """A value made safe inside a LIKE pattern. Pair with ESCAPE '\\'.

    Without this a title containing % or _ is a wildcard, and "100% Hotter"
    matches every programme with "100" and "Hotter" in it.
    """
    return str(v or "").replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def _text(sql, cmp_, value):
    v = str(value or "")
    if cmp_ in ("is", "!is"):
        frag, args = f"{sql} = ?", [v]
    elif cmp_ in ("contains", "!contains"):
        frag, args = f"{sql} LIKE ? ESCAPE '\\'", [f"%{like(v)}%"]
    elif cmp_ == "starts":
        frag, args = f"{sql} LIKE ? ESCAPE '\\'", [f"{like(v)}%"]
    elif cmp_ == "ends":
        frag, args = f"{sql} LIKE ? ESCAPE '\\'", [f"%{like(v)}"]
    else:
        raise FilterError(f"{cmp_} is not something a text field can be asked")
    return frag, args, cmp_.startswith("!")


def _tag(sql, cmp_, value):
    """Genres are a JSON list, so the test is on the encoded text.

    The quotes are part of the pattern on purpose: without them "Drama" also
    matches "Docudrama".
    """
    return f"{sql} LIKE ? ESCAPE '\\'", [f'%"{like(value)}"%'], cmp_.startswith("!")


def _choice(sql, cmp_, value):
    return f"{sql} = ?", [value], cmp_.startswith("!")


def _number(sql, cmp_, value):
    try:
        n = float(value)
    except (TypeError, ValueError):
        raise FilterError(f"{value!r} is not a number")
    ops = {"is": "=", "!is": "=", "gt": ">", "lt": "<"}
    if cmp_ not in ops:
        raise FilterError(f"{cmp_} is not something a number can be asked")
    return f"{sql} {ops[cmp_]} ?", [n], cmp_ == "!is"


def _date(sql, cmp_, value):
    v = str(value or "").strip()
    try:
        datetime.date.fromisoformat(v)
    except ValueError:
        raise FilterError(f"{v!r} is not a date. Use YYYY-MM-DD.")
    return f"{sql} {'>' if cmp_ == 'after' else '<'} ?", [v], False


def _bool(sql, cmp_, value):
    # The expression is already a test, so there is nothing to compare.
    return f"({sql})", [], cmp_ == "no"


_BUILDERS = {"text": _text, "tag": _tag, "choice": _choice, "number": _number,
             "date": _date, "bool": _bool}


def _condition(node):
    field = FIELDS.get(node.get("field"))
    if not field:
        raise FilterError(f"{node.get('field')!r} is not something the guide holds")
    cmp_ = node.get("cmp") or ""
    allowed = {c for c, _ in COMPARISONS[field["kind"]]}
    if cmp_ not in allowed:
        raise FilterError(f"{field['label']} cannot be asked {cmp_!r}")

    frag, args, negate = _BUILDERS[field["kind"]](field["sql"], cmp_, node.get("value"))

    if field["kind"] == "bool":
        return (f"NOT {frag}" if negate else frag), args

    # An item with no value for this field. NULL fails every comparison in SQL,
    # including a negative one, so both directions have to say what they mean.
    blank_ok = bool(node.get("blank"))
    empty = f"({field['sql']} IS NULL OR {field['sql']} = '')"
    if negate:
        # "is not X" over a blank: only a match if the user asked for it.
        frag = f"NOT ({frag})"
        frag = f"({frag} OR {empty})" if blank_ok else f"({frag} AND NOT {empty})"
    elif blank_ok:
        frag = f"({frag} OR {empty})"
    return frag, args


def _compile(node, depth, counter):
    counter[0] += 1
    if counter[0] > MAX_NODES:
        raise FilterError(f"a filter is limited to {MAX_NODES} rows and groups")
    if depth > MAX_DEPTH:
        raise FilterError(f"groups are limited to {MAX_DEPTH} deep")

    if "op" not in node:
        return _condition(node)

    op = node.get("op")
    if op not in GROUP_OPS:
        raise FilterError(f"{op!r} is not all, any or none")
    parts, args = [], []
    for child in node.get("nodes") or []:
        frag, a = _compile(child, depth + 1, counter)
        parts.append(frag)
        args.extend(a)
    if not parts:
        # An empty group must not silently mean "everything".
        raise FilterError("a group with nothing in it matches nothing")
    joined = f"({f' {GROUP_OPS[op]} '.join(parts)})"
    return (f"NOT {joined}" if op == "none" else joined), args


def build(tree):
    """Compile a tree to (sql_fragment, args). Raises FilterError on nonsense."""
    if not isinstance(tree, dict):
        raise FilterError("a filter is a group of conditions")
    return _compile(tree, 0, [0])


# ---------------------------------------------------------------- describing

def describe(tree):
    """The tree as a sentence, for the pass list and the schedule reason."""
    try:
        return _describe(tree, top=True)
    except Exception:
        return "a smart filter"


def _describe(node, top=False):
    if "op" not in node:
        f = FIELDS.get(node.get("field"))
        if not f:
            return "?"
        label = dict(COMPARISONS[f["kind"]]).get(node.get("cmp"), node.get("cmp"))
        if f["kind"] == "bool":
            return f"{f['label'].lower()}: {label}"
        return f"{f['label'].lower()} {label} {node.get('value')}"
    joiner = {"all": " and ", "any": " or ", "none": " or "}[node.get("op", "all")]
    inner = joiner.join(_describe(n) for n in (node.get("nodes") or []))
    if node.get("op") == "none":
        return f"not ({inner})"
    return inner if top else f"({inner})"


def count_nodes(tree):
    if not isinstance(tree, dict):
        return 0
    if "op" not in tree:
        return 1
    return 1 + sum(count_nodes(n) for n in (tree.get("nodes") or []))


# ---------------------------------------------------------------- looseness

# A filter that names nothing specific about the programme matches most of the
# guide. These fields narrow by broadcast, not by content, so a filter made
# only of them is loose however many rows it has.
_BROAD_ONLY = {"channel", "network", "hd", "live", "weekday", "hour"}


def is_loose(tree):
    """Whether this filter is likely to book a great deal.

    Judged on the shape rather than the count, so the panel can warn before it
    has finished counting.
    """
    fields = _fields_used(tree)
    if not fields:
        return True
    return not (fields - _BROAD_ONLY)


def _fields_used(node, out=None):
    out = set() if out is None else out
    if not isinstance(node, dict):
        return out
    if "op" not in node:
        if node.get("field"):
            out.add(node["field"])
        return out
    for n in node.get("nodes") or []:
        _fields_used(n, out)
    return out
