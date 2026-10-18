import unittest

import mistune

from astrochat.markdown import convert_github_markdown, escape_text, markdown_images, split_message


class ConversionTests(unittest.TestCase):
    def test_common_formatting_and_nested_lists(self):
        source = (
            "# 标题\n\n**bold**, *emphasis*, ~~removed~~.\n\n"
            "- first\n  - nested\n\n1. numbered\n2. next\n\n> quoted\n"
        )
        converted = convert_github_markdown(source)
        for expected in (
            "# 标题",
            "**bold**",
            "*emphasis*",
            "~~removed~~",
            "  - nested",
            "2. next",
            "> quoted",
        ):
            self.assertIn(expected, converted)

    def test_task_lists_are_read_only_and_keep_nested_content(self):
        source = "- [ ] pending\n- [x] **done**\n  - [X] nested\n"
        self.assertEqual(convert_github_markdown(source), "- ☐ pending\n- ☑ **done**\n  - ☑ nested")

    def test_alerts_are_labeled_quotes_and_regular_quotes_are_unchanged(self):
        for label in ("NOTE", "TIP", "IMPORTANT", "WARNING", "CAUTION"):
            with self.subTest(label=label):
                self.assertEqual(
                    convert_github_markdown(f"> [!{label}]\n> **Read this**"),
                    f"> **{label}**\n> **Read this**",
                )
        self.assertEqual(convert_github_markdown("> ordinary quote"), "> ordinary quote")

    def test_math_supports_inline_backtick_variant_and_display_blocks(self):
        self.assertEqual(
            convert_github_markdown("Use $x^2$ and $`a+b`$."), "Use $$x^2$$ and $$a+b$$."
        )
        for source in ("$$\nx^2+y^2=z^2\n$$", "$$x^2+y^2=z^2$$", "```math\nx^2+y^2=z^2\n```"):
            with self.subTest(source=source):
                self.assertEqual(convert_github_markdown(source), "```math\nx^2+y^2=z^2\n```")
        self.assertIn(r"\$5", convert_github_markdown(r"Cost \$5 and \$10."))

    def test_code_is_not_converted_or_neutralized(self):
        code = "@**all**\n- [x] example\n> [!NOTE]\n$x$\n<details>\n<!-- keep -->\n"
        source = "~~~python\n" + code + "~~~"
        converted = convert_github_markdown(source)
        tokens = mistune.create_markdown(renderer="ast")(converted)
        self.assertEqual(tokens[0]["type"], "block_code")
        self.assertEqual(tokens[0]["raw"], code)
        self.assertEqual(tokens[0]["attrs"]["info"], "python")
        inline = "`@**all**` and ``code ` with $x$``"
        self.assertEqual(convert_github_markdown(inline), inline)

    def test_indented_code_and_fences_containing_backticks_are_preserved(self):
        self.assertEqual(convert_github_markdown("    x = '<tag>'\n"), "```text\nx = '<tag>'\n```")
        source = "````markdown\n```python\nx = 1\n```\n````"
        tokens = mistune.create_markdown(renderer="ast")(convert_github_markdown(source))
        self.assertEqual(tokens[0]["raw"], "```python\nx = 1\n```\n")

    def test_zulip_widget_names_in_github_code_remain_code(self):
        for language in ("quote", "spoiler", "poll", "todo"):
            with self.subTest(language=language):
                self.assertEqual(
                    convert_github_markdown(f"```{language}\n@**all**\n```"),
                    "```text\n@**all**\n```",
                )

    def test_mentions_in_prose_and_metadata_are_neutralized_but_email_is_preserved(self):
        source = "@**all** @**User|12** @*team* @_**User** alice@example.com"
        converted = convert_github_markdown(source)
        self.assertNotIn("@**", converted)
        self.assertNotIn("@*", converted)
        self.assertNotIn("@_", converted)
        self.assertIn("alice@example.com", converted)
        self.assertIn("@\u200b", escape_text("@**all**"))
        self.assertNotIn("@**", convert_github_markdown("&#64;**all**"))

    def test_links_and_images_use_explicit_urls_including_references(self):
        source = '[Guide][ref]\n\n[ref]: https://example.com/a_(b) "Guide"\n\n![plot](https://example.com/p.png)'
        converted = convert_github_markdown(source)
        self.assertIn('[Guide](<https://example.com/a_(b)> "Guide")', converted)
        self.assertIn("![plot](https://example.com/p.png)", converted)
        self.assertNotIn("[ref]", converted)
        self.assertEqual(
            convert_github_markdown("![https://example.com/a.png](https://example.com/a.png)"),
            "![https://example.com/a.png](<https://example.com/a.png>)",
        )

    def test_relative_urls_resolve_against_source_page_without_inferring_a_branch(self):
        converted = convert_github_markdown(
            "[comment](#issuecomment-2) [other](../pull/3) [repo](/u/r) ![image](//example.com/a.png)",
            "https://github.com/u/r/issues/1",
        )
        for url in (
            "https://github.com/u/r/issues/1#issuecomment-2",
            "https://github.com/u/r/pull/3",
            "https://github.com/u/r",
            "https://example.com/a.png",
        ):
            self.assertIn(url, converted)
        self.assertNotIn("blob/main", converted)

    def test_table_alignment_and_literal_pipe(self):
        source = "| Name | Value |\n| :--- | ---: |\n| a \\| b | **yes** |"
        converted = convert_github_markdown(source)
        self.assertIn("| :--- | ---: |", converted)
        self.assertIn(r"a \| b", converted)
        self.assertNotIn(r"a \\| b", converted)
        self.assertIn("**yes**", converted)

    def test_simple_details_are_expanded_and_code_remains_code(self):
        source = "<details>\n<summary>Logs</summary>\n\n```python\nx = 1\n```\n\n</details>"
        self.assertEqual(convert_github_markdown(source), "**Logs**\n\n```python\nx = 1\n```")

    def test_html_break_images_and_comments(self):
        converted = convert_github_markdown(
            'first<br>second\n\n<img src="/image.png" alt="plot" width="100">\n\n<!-- hidden -->',
            "https://github.com/u/r/issues/1",
        )
        self.assertIn("first  \nsecond", converted)
        self.assertIn("![plot](https://github.com/image.png)", converted)
        self.assertNotIn("hidden", converted)
        self.assertNotIn("width", converted)

    def test_unknown_html_and_code_embedded_in_html_remain_visible_source(self):
        for source in (
            "<table><tr><td>data</td></tr></table>",
            "<details><summary>Code</summary>\n```html\n<!-- keep -->\n```\n</details>",
        ):
            with self.subTest(source=source):
                converted = convert_github_markdown(source)
                tokens = mistune.create_markdown(renderer="ast")(converted)
                self.assertEqual(tokens[0]["type"], "block_code")
                self.assertEqual(tokens[0]["raw"].rstrip("\n"), source)
        self.assertIn("`<span>`", convert_github_markdown("inline <span>value</span>"))

    def test_footnotes_preserve_references_and_explanations(self):
        converted = convert_github_markdown("text[^a]\n\n[^a]: **explanation**")
        self.assertIn(r"text\[1\]", converted)
        self.assertIn("**Footnotes**", converted)
        self.assertIn("**[1]** **explanation**", converted)

    def test_collect_and_replace_images_in_markdown_references_html_and_details(self):
        url = "https://github.com/user-attachments/assets/abc-def"
        uploaded = "/user_uploads/1/abc/image.png"
        sources = (
            f"![plot]({url})",
            f"![plot][image]\n\n[image]: {url}",
            f'<img width="100" alt="plot" src="{url}">',
            f'Before <img alt="plot" src="{url}"> after',
            f'<details><summary>Plot</summary><img src="{url}"></details>',
            f"[![plot]({url})]({url})",
            "![plot](/user-attachments/assets/abc-def)",
        )
        for source in sources:
            with self.subTest(source=source):
                self.assertEqual(markdown_images(source, "https://github.com/u/r/issues/1"), {url})
                converted = convert_github_markdown(
                    source, "https://github.com/u/r/issues/1", {url: uploaded}
                )
                self.assertIn(uploaded, converted)
                self.assertNotIn(url, converted)
                self.assertNotIn("https://github.com/user_uploads/", converted)

    def test_image_collection_excludes_code_plain_links_comments_and_unsupported_html(self):
        url = "https://github.com/user-attachments/assets/abc-def"
        for source in (
            f"`![plot]({url})`",
            f"```md\n![plot]({url})\n```",
            f"[file]({url})",
            f'<!-- <img src="{url}"> -->',
            f'<table><tr><td><img src="{url}"></td></tr></table>',
        ):
            with self.subTest(source=source):
                self.assertEqual(markdown_images(source), set())

    def test_image_replacement_preserves_code_and_external_links(self):
        url = "https://github.com/user-attachments/assets/abc-def"
        source = f"![plot]({url})\n\n`![plot]({url})`\n\n[external](https://example.com)"
        converted = convert_github_markdown(source, image_urls={url: "/user_uploads/a.png"})
        self.assertIn(f"`![plot]({url})`", converted)
        self.assertIn("[external](https://example.com)", converted)
        self.assertIn("![plot](/user_uploads/a.png)", converted)


class MessageSplittingTests(unittest.TestCase):
    def payloads(self, parts):
        return [part.split("\n\n", 1)[1] for part in parts]

    def test_short_and_exact_limit_messages_do_not_gain_headers(self):
        for content in ("", "text", "x" * 128):
            self.assertEqual(split_message(content, 128), [content])

    def test_paragraph_boundaries_and_unicode_text_are_preserved(self):
        content = ("段落 " * 8 + "\n\n") * 20
        parts = split_message(content, 180)
        self.assertTrue(all(len(part) <= 180 for part in parts))
        self.assertEqual("".join(self.payloads(parts)), content)
        self.assertTrue(all(payload.endswith("\n\n") for payload in self.payloads(parts)))

    def test_long_code_blocks_have_complete_fences_in_every_part(self):
        code = "print('@**all**')\n" * 100
        content = convert_github_markdown("```python\n" + code + "```")
        parts = split_message(content, 180)
        self.assertTrue(all(len(part) <= 180 for part in parts))
        reconstructed = []
        for payload in self.payloads(parts):
            tokens = mistune.create_markdown(renderer="ast")(payload)
            self.assertEqual(tokens[0]["type"], "block_code")
            self.assertEqual(tokens[0]["attrs"]["info"], "python")
            reconstructed.append(tokens[0]["raw"])
        self.assertEqual("".join(reconstructed), code)

    def test_long_single_code_line_and_unclosed_code_do_not_lose_characters(self):
        for content in ("```text\n" + "甲" * 2000 + "\n```", "```text\n" + "甲" * 2000):
            with self.subTest(closed=content.endswith("```")):
                parts = split_message(content, 180)
                recovered = []
                for payload in self.payloads(parts):
                    tokens = mistune.create_markdown(renderer="ast")(payload)
                    recovered.append(tokens[0]["raw"].rstrip("\n"))
                self.assertEqual("".join(recovered), "甲" * 2000)
                self.assertTrue(all(len(part) <= 180 for part in parts))

    def test_fences_with_inner_backticks_survive_splitting(self):
        source = "````markdown\n" + "```python\nx = 1\n```\n" * 50 + "````"
        content = convert_github_markdown(source)
        payloads = self.payloads(split_message(content, 180))
        tokens = [mistune.create_markdown(renderer="ast")(payload)[0] for payload in payloads]
        self.assertTrue(all(token["type"] == "block_code" for token in tokens))
        self.assertEqual("".join(token["raw"] for token in tokens), "```python\nx = 1\n```\n" * 50)

    def test_oversized_math_and_complex_blocks_fall_back_to_source(self):
        inputs = [
            "```math\n" + "x + " * 150 + "y\n```",
            "| A | B |\n|---|---|\n" + "| long | row |\n" * 100,
            "> ```python\n" + "> print(1)\n" * 100 + "> ```\n",
            "- ```python\n" + "  print(1)\n" * 100 + "  ```\n",
        ]
        for content in inputs:
            with self.subTest(content=content[:20]):
                parts = split_message(content, 180)
                self.assertTrue(all(len(part) <= 180 for part in parts))
                for payload in self.payloads(parts):
                    tokens = mistune.create_markdown(renderer="ast")(payload)
                    for token in tokens:
                        if token["type"] != "blank_line":
                            self.assertEqual(token["type"], "block_code")
                            self.assertEqual(token["attrs"]["info"], "text")

    def test_small_code_block_is_not_split_when_adjacent_text_is_long(self):
        content = "paragraph\n\n" * 20 + "```python\nx = 1\n```\n" + "more\n\n" * 20
        parts = split_message(content, 180)
        self.assertEqual("".join(self.payloads(parts)), content)
        self.assertEqual(sum("```python\nx = 1\n```" in part for part in parts), 1)

    def test_too_small_message_limit_is_rejected(self):
        with self.assertRaises(ValueError):
            split_message("text", 10)
