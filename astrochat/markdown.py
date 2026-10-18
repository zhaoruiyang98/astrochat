"""Convert GitHub Markdown to Zulip Markdown without network or storage access.

Mistune parses Markdown before conversion, so prose transformations never touch
code. Unsupported HTML is displayed as source rather than silently discarded.
"""

import html
import re
from html.parser import HTMLParser
from urllib.parse import urljoin, urlsplit

import mistune
from mistune.renderers.markdown import MarkdownRenderer


def escape_text(text: str) -> str:
    """Escape literal metadata/prose, including Zulip's mention introducers."""
    text = re.sub(r"@(?=[_*])|@$", "@\u200b", text)
    return re.sub(r"([\\`*_{}\[\]<>()#!~$])", r"\\\1", text)


def fenced_code(code: str, language: str = "text") -> str:
    # A fence longer than every run in the payload cannot be closed by the code.
    ticks = max((len(run) for run in re.findall(r"`+", code)), default=0)
    waves = max((len(run) for run in re.findall(r"~+", code)), default=0)
    marker = "`" * max(3, ticks + 1) if ticks <= waves else "~" * max(3, waves + 1)
    ending = "" if code.endswith("\n") else "\n"
    return f"{marker}{language}\n{code}{ending}{marker}\n\n"


def _image_url(url, source_url, replacements, found):
    url = urljoin(source_url, url) if source_url else url
    found.add(url)
    return replacements.get(url, url)


class _HTMLFragment(HTMLParser):
    """Expand simple GitHub details/summary fragments; keep unknown HTML visible."""

    def __init__(self, source_url: str, replacements=None, found=None):
        super().__init__(convert_charrefs=False)
        self.source_url = source_url
        self.parts: list[str] = []
        self.supported = True
        self.replacements = replacements if replacements is not None else {}
        self.found = found if found is not None else set()

    def handle_starttag(self, tag, attrs):
        if tag in {"details", "p", "div"}:
            self.parts.append("\n\n")
        elif tag == "summary":
            self.parts.append("**")
        elif tag == "br":
            self.parts.append("  \n")
        elif tag == "img":
            attributes = dict(attrs)
            source = attributes.get("src", "") or ""
            source = _image_url(source, self.source_url, self.replacements, self.found)
            alt = escape_text(attributes.get("alt", "") or "image")
            # Angle brackets and whitespace belong to the URL, not Markdown syntax.
            source = source.replace("<", "%3C").replace(">", "%3E").replace(" ", "%20")
            self.parts.append(f"![{alt}](<{source}>)")
        else:
            self.supported = False

    def handle_endtag(self, tag):
        if tag == "summary":
            self.parts.append("**\n\n")
        elif tag in {"details", "p", "div"}:
            self.parts.append("\n\n")
        elif tag not in {"br", "img"}:
            self.supported = False

    def handle_data(self, data):
        self.parts.append(data)

    def handle_entityref(self, name):
        self.parts.append(f"&{name};")

    def handle_charref(self, name):
        self.parts.append(f"&#{name};")

    def handle_decl(self, decl):
        self.supported = False


class _ZulipRenderer(MarkdownRenderer):
    def __init__(self, source_url: str, replacements, found):
        super().__init__()
        self.source_url = source_url
        self.replacements = replacements
        self.found = found

    def text(self, token, state):
        return escape_text(html.unescape(token["raw"]))

    def strikethrough(self, token, state):
        return "~~" + self.render_children(token, state) + "~~"

    def link(self, token, state):
        # Inline reference links so a later message part needs no definitions
        # from an earlier part. Resolve URLs against the original discussion.
        attrs = dict(token["attrs"])
        url = attrs["url"]
        if self.source_url and not urlsplit(url).scheme:
            attrs["url"] = urljoin(self.source_url, url)
        # A clickable image may also wrap the original image URL in a link.
        attrs["url"] = self.replacements.get(attrs["url"], attrs["url"])
        return super().link({**token, "label": None, "attrs": attrs}, state)

    def render_referrences(self, state):
        return ()

    def image(self, token, state):
        _image_url(token["attrs"]["url"], self.source_url, self.replacements, self.found)
        link = self.link(token, state)
        # The base renderer abbreviates a URL-shaped label to <url>; image
        # syntax still needs explicit brackets around its alternative text.
        if link.startswith("<"):
            return f"![{self.render_children(token, state)}]({link})"
        return "!" + link

    def task_list_item(self, token, state):
        children = list(token["children"])
        first = children[0]
        marker = "☑ " if token["attrs"]["checked"] else "☐ "
        children[0] = {
            **first,
            "children": [{"type": "text", "raw": marker}, *first["children"]],
        }
        return super().list_item({**token, "children": children}, state)

    def block_quote(self, token, state):
        rendered = super().block_quote(token, state)
        # The parser treats GitHub alerts as quoted text. Change only the first
        # line, after literal brackets have been escaped by text().
        return re.sub(
            r"^> \\\[\\!?(NOTE|TIP|IMPORTANT|WARNING|CAUTION)\\\]",
            r"> **\1**",
            rendered,
        )

    def block_code(self, token, state):
        info = token.get("attrs", {}).get("info", "").split()
        language = info[0] if info else "text"
        # These are widgets/formatting in Zulip, but ordinary code in GitHub.
        if language.lower() in {"quote", "spoiler", "poll", "todo"}:
            language = "text"
        language = re.sub(r"[^A-Za-z0-9_+.#-]", "", language) or "text"
        return fenced_code(token["raw"], language)

    def inline_math(self, token, state):
        return "$$" + token["raw"] + "$$"

    def block_math(self, token, state):
        return "\n\n" + fenced_code(token["raw"], "math")

    def footnote_ref(self, token, state):
        return f"\\[{token['attrs']['index']}\\]"

    def footnotes(self, token, state):
        return "\n\n**Footnotes**\n\n" + self.render_children(token, state)

    def footnote_item(self, token, state):
        return f"**[{token['attrs']['index']}]** " + self.render_children(token, state)

    def inline_html(self, token, state):
        fragment = _HTMLFragment(self.source_url, self.replacements, self.found)
        fragment.feed(token["raw"])
        fragment.close()
        if fragment.supported:
            return "".join(fragment.parts)
        return self.codespan(token, state)

    def block_html(self, token, state):
        # Without Markdown-separating blank lines, GitHub HTML can contain code
        # inside the same token. Do not let HTMLParser consume tags/comments in it.
        if "`" in token["raw"] or re.search(r"(?m)^\s*~{3,}", token["raw"]):
            return fenced_code(token["raw"], "html")
        fragment = _HTMLFragment(self.source_url)
        fragment.feed(token["raw"])
        fragment.close()
        if fragment.supported:
            return (
                _convert("".join(fragment.parts), self.source_url, self.replacements, self.found)
                + "\n\n"
            )
        return fenced_code(token["raw"], "html")


def _convert(source, source_url, replacements, found):
    parser = mistune.create_markdown(
        renderer=_ZulipRenderer(source_url, replacements, found),
        plugins=["strikethrough", "table", "task_lists", "math", "footnotes"],
    )
    return parser(source).strip("\n")


def markdown_images(source: str, source_url: str = "") -> set[str]:
    """Collect displayed image URLs, excluding code and unsupported HTML."""
    found: set[str] = set()
    _convert(source, source_url, {}, found)
    return found


def convert_github_markdown(
    source: str, source_url: str = "", image_urls: dict[str, str] | None = None
) -> str:
    """Convert a body, optionally replacing image URLs using an already prepared map."""
    return _convert(source, source_url, image_urls if image_urls is not None else {}, set())


_FENCE = re.compile(
    r"^(?P<prefix>[ >\t]*(?:(?:[-+*]|\d+[.)]) +)?)"
    r"(?P<marker>`{3,}|~{3,})(?P<info>[^\n]*)\n?$"
)


def _blocks(content: str) -> list[str]:
    """Group paragraphs and complete fences before choosing message boundaries."""
    blocks = []
    lines = content.splitlines(keepends=True)
    current: list[str] = []
    fence = None
    for line in lines:
        if fence is not None:
            current.append(line)
            prefix, marker = fence
            if re.fullmatch(
                re.escape(prefix) + re.escape(marker[0]) + "{" + str(len(marker)) + r",}[ \t]*\n?",
                line,
            ):
                blocks.append("".join(current))
                current = []
                fence = None
            continue
        opening = _FENCE.match(line)
        if opening:
            if current:
                blocks.append("".join(current))
            current = [line]
            prefix = re.sub(r"(?:[-+*]|\d+[.)]) +$", lambda m: " " * len(m[0]), opening["prefix"])
            fence = prefix, opening["marker"]
        else:
            current.append(line)
            if not line.strip():
                blocks.append("".join(current))
                current = []
    if current:
        blocks.append("".join(current))
    return blocks


def _text_parts(text: str, size: int) -> list[str]:
    parts = []
    while len(text) > size:
        boundary = text.rfind("\n", 0, size)
        end = boundary + 1 if boundary >= size // 2 else size
        parts.append(text[:end])
        text = text[end:]
    if text:
        parts.append(text)
    return parts


def _oversized_block(block: str, size: int) -> list[str]:
    lines = block.splitlines(keepends=True)
    opening = _FENCE.match(lines[0])
    if opening and not opening["prefix"]:
        # Converted fenced blocks always have a matching closing line.
        marker, info = opening["marker"], opening["info"]
        if info.strip() == "math":
            # A fragment of a formula may not be valid TeX by itself.
            info = "text"
        wrapper_size = len(marker) * 2 + len(info) + 3
        if wrapper_size >= size:
            raise ValueError("Code fence is too long for the message limit")
        closed = re.fullmatch(
            re.escape(marker[0]) + "{" + str(len(marker)) + r",}[ \t]*\n?", lines[-1]
        )
        code = "".join(lines[1:-1] if closed else lines[1:])
        parts = []
        for part in _text_parts(code, size - wrapper_size):
            ending = "" if part.endswith("\n") else "\n"
            parts.append(f"{marker}{info}\n{part}{ending}{marker}\n")
        return parts
    complex_markup = re.search(r"[`*_\[\]|>$]|(?m:^[ \t]*(?:[-+]|\d+[.)]|#{1,6}) )", block)
    if not complex_markup:
        return _text_parts(block, size)
    # A table, nested code block or very long inline link cannot always be cut
    # into valid Markdown. Show its source in individually fenced text parts.
    parts = []
    for part in _text_parts(block, size // 3):
        rendered = fenced_code(part)
        if len(rendered) > size:
            raise ValueError("Markdown block is too complex for the message limit")
        parts.append(rendered)
    return parts


def split_message(content: str, limit: int = 10000) -> list[str]:
    """Split on block boundaries, reopening oversized code fences in each part.

    Oversized complex blocks fall back to fenced source. An oversized single code
    line is displayed across parts; no source characters are dropped.
    """
    if limit < 128:
        raise ValueError("Message limit must be at least 128 characters")
    if len(content) <= limit:
        return [content]
    # Even one character per part cannot exceed this number of header digits.
    size = limit - len(f"*(part {len(content)}/{len(content)})*\n\n")
    pieces = []
    for block in _blocks(content):
        pieces.extend([block] if len(block) <= size else _oversized_block(block, size))
    chunks: list[str] = []
    current = ""
    for piece in pieces:
        if current and len(current) + len(piece) > size:
            chunks.append(current)
            current = ""
        current += piece
    if current:
        chunks.append(current)
    return [f"*(part {index + 1}/{len(chunks)})*\n\n{chunk}" for index, chunk in enumerate(chunks)]
