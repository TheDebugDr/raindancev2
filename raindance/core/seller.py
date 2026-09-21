"""Who is actually selling this listing — the anti-scalper identity check.

This is the half of the gate that price alone cannot cover. A marketplace
scalper on Target Plus or Walmart Marketplace renders on the retailer's own
domain, in the retailer's own template, and frequently does not *look* like a
third party at all. So the rule here is strict and one-directional:

    is_official is True ONLY on positive, unambiguous evidence.
    Everything else is None, and the classifier fails safe on None.

Three defects this module exists to prevent, all of which shipped before:

1. TAG BOUNDARIES. ``Sold and shipped by <a href="/plus/x">ScalperCo</a>`` was
   invisible to a regex whose character class could not cross ``<``, so the
   match failed and the caller fell through to "must be first-party". We
   linearise the markup first: every tag becomes a newline, so the seller name
   is the text right after the phrase and the newline terminates it.

2. PREFIX MATCHING. ``name.startswith("target")`` admits "Targeted Deals" and
   ``"walmart" in name`` admits "Walmart Resellers LLC". Identity is decided by
   EXACT membership of a normalised name in a per-retailer allowlist.

3. AMBIGUITY. A PDP can carry several "sold by" phrases — a recommendations
   carousel, a bundle module, a comparison table. Taking the first match is the
   same unanchored-read bug that mispriced listings. We collect every match and
   refuse to answer when they disagree.

The observed seller name is always preserved as-is. Overwriting a partner's
name with the retailer's own was how a third-party listing came to be reported
as "Target".
"""
from __future__ import annotations

import html as _html
import re
from typing import Optional

# Exact, normalised names that mean "the retailer itself is the seller".
# Membership is exact after _norm(); no prefixes, no substrings.
FIRST_PARTY_NAMES: dict[str, frozenset] = {
    "target": frozenset({
        "target", "target com", "targetcom", "target corporation",
        "target stores", "target brands",
    }),
    "bestbuy": frozenset({
        "best buy", "bestbuy", "best buy com", "bestbuycom",
        "best buy stores", "best buy co", "best buy canada",
    }),
    "walmart": frozenset({
        "walmart", "walmart com", "walmartcom", "walmart inc",
        "walmart stores", "walmart stores inc",
    }),
    "pokemon_center": frozenset({
        "pokemon center", "pokemoncenter", "pokemon center com",
        "the pokemon company", "the pokemon company international",
    }),
}

# "Sold by X" / "Sold and shipped by X" / "Ships from and sold by X".
# Applied to LINEARISED text (see _linearise), so a newline ends the name.
_SOLD_BY = re.compile(
    r"(?:sold\s*(?:and|&)?\s*(?:shipped\s*)?by"
    r"|ships?\s*from\s*and\s*sold\s*by"
    r"|sold\s*by)"
    # At most one blank line before the name. A wider gap means the next
    # text belongs to a different UI block, not this label's value — see
    # the module docstring's Best Buy example.
    r"[ \t:]*\n{0,2}[ \t]*([^\n.,;|()\[\]]{2,60})",
    re.I,
)

# Seller statements are not always text nodes. On a live Target PDP the partner
# is announced in an aria-label ("Sold & shipped by Collectors Emporium. View
# partner details") on the link wrapping the card. Stripping tags would delete
# it, so these attributes are lifted out as text before tags are removed.
_LABEL_ATTR = re.compile(
    r"""\b(?:aria-label|title|alt|data-seller|data-seller-name)\s*=\s*(?P<q>["'])(?P<v>.*?)(?P=q)""",
    re.I | re.S)

_TAG = re.compile(r"<[^>]*>")
_DROP_BLOCKS = re.compile(r"<(script|style|noscript)\b[^>]*>.*?</\1>", re.I | re.S)
_WS = re.compile(r"[^\S\n]+")          # whitespace except newlines
_PUNCT = re.compile(r"[^a-z0-9 ]+")


def _linearise(markup: str) -> str:
    """Markup → text where every tag boundary is a newline.

    Newlines matter: they are what stops a seller-name capture from running on
    into the next element's text. Replacing tags with a newline (rather than
    with nothing, or with a space) is what makes both of these read correctly:

        Sold and shipped by <a>ScalperCo</a>      → "…by\\nScalperCo\\n"
        <span>Sold and shipped by</span><span>ScalperCo</span>
                                                  → "…by\\nScalperCo\\n"
    """
    if not markup:
        return ""
    text = _DROP_BLOCKS.sub(" ", markup)
    # Attribute-borne statements first, each on its own line, so they read as
    # ordinary sentences to the matcher below and cannot merge into neighbours.
    labels = "\n".join(m.group("v") for m in _LABEL_ATTR.finditer(text))
    text = _TAG.sub("\n", text)
    if labels:
        text = labels + "\n" + text
    text = _html.unescape(text)          # &amp; &nbsp; &#39; …
    text = text.replace(" ", " ")   # decoded nbsp is not ordinary space
    text = _WS.sub(" ", text)
    return text


def _norm(name: str) -> str:
    """Normalise a seller name for exact allowlist comparison."""
    n = _html.unescape(name or "").lower().strip()
    n = n.replace(" ", " ")
    n = _PUNCT.sub(" ", n)               # drop . , ' & - etc.
    n = re.sub(r"\s+", " ", n).strip()
    # Trailing corporate suffixes carry no identity.
    n = re.sub(r"\s+(inc|llc|ltd|co|corp|corporation)$", "", n).strip()
    return n


def sold_by_names(markup: str) -> list[str]:
    """Every distinct seller name asserted anywhere in the markup, in order.

    Returned verbatim (trimmed) — callers surface the real name to the user.
    """
    seen: list[str] = []
    for m in _SOLD_BY.finditer(_linearise(markup)):
        raw = m.group(1).strip(" \t-–—:·|")
        if not raw or len(raw) < 2:
            continue
        # Guard against capturing a leftover UI fragment rather than a name.
        if _norm(raw) in ("", "this seller", "seller"):
            continue
        if raw not in seen:
            seen.append(raw)
    return seen


# Definitive "this listing is a marketplace listing" markers, observed on live
# pages. These are structural, not textual, so they survive copy changes and
# they cannot be spoofed away by a partner whose display name looks official.
MARKETPLACE_MARKERS: dict[str, tuple] = {
    # Target Plus. The fulfillment card carries this data-test, and the partner
    # storefront link is always /sp/<partner>/-/N-<id>.
    "target": (
        re.compile(r'data-test=["\']targetPlus[A-Za-z]*["\']', re.I),
        re.compile(r'href=["\'][^"\']*/sp/[a-z0-9\-]+/-/N-\d+', re.I),
    ),
    "walmart": (
        re.compile(r'"sellerType"\s*:\s*"(?!INTERNAL)[A-Z_]+"', re.I),
    ),
    "bestbuy": (
        re.compile(r'data-marketplace-seller|"isMarketplace"\s*:\s*true', re.I),
    ),
}


def marketplace_marker(markup: str, retailer: str) -> bool:
    """True when the page structurally identifies itself as a marketplace listing."""
    for pat in MARKETPLACE_MARKERS.get(retailer, ()):
        if pat.search(markup or ""):
            return True
    return False


def is_first_party_name(name: str, retailer: str) -> bool:
    """Exact allowlist test. 'Targeted Deals' is not Target."""
    return _norm(name) in FIRST_PARTY_NAMES.get(retailer, frozenset())

def verdict(markup: str, retailer: str) -> dict:
    """{"is_official": bool|None, "seller": str|None} from page markup.

    is_official is True only when a "sold by" statement exists AND names the
    retailer exactly. It is False when such a statement names someone else.
    It is None when no statement was found, or when statements conflict —
    absence of evidence is never evidence of first-party.
    """
    names = sold_by_names(markup)

    # A structural marketplace marker outranks any name. A partner trading as
    # "Target Deals" cannot talk its way past this.
    if marketplace_marker(markup, retailer):
        third = [n for n in names if not is_first_party_name(n, retailer)]
        return {"is_official": False, "seller": third[0] if third else (names[0] if names else None)}

    if not names:
        return {"is_official": None, "seller": None}

    official = [n for n in names if is_first_party_name(n, retailer)]
    third = [n for n in names if not is_first_party_name(n, retailer)]

    if official and third:
        # A page asserting both is ambiguous — commonly a carousel or a bundle
        # module belonging to a different listing. Refuse rather than pick.
        return {"is_official": None, "seller": third[0]}
    if official:
        return {"is_official": True, "seller": official[0]}
    return {"is_official": False, "seller": third[0]}




def official_host(url: str, expected: str) -> Optional[bool]:
    """Domain identity for first-party-only stores (Pokemon Center).

    Exact host match or a real subdomain of it. Deliberately not `endswith`:
    'pokemoncenter.com.evil.example' and 'notpokemoncenter.com' are not it.
    """
    from urllib.parse import urlparse
    host = (urlparse(url or "").hostname or "").lower().rstrip(".")
    if not host:
        return None
    if host.startswith("www."):
        host = host[4:]
    expected = expected.lower().lstrip(".")
    if host == expected or host.endswith("." + expected):
        return True
    return False
