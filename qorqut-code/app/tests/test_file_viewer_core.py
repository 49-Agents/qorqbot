"""File viewer units: lexical path rules, strict selectors, the no-follow opener and bounded previews.

No service or database: these are the pure and filesystem-only pieces the HTTP routes and ``files_link`` share.
"""
import os
import socket
import stat
import threading

import pytest

from qorqut_operations.contracts import DomainError
from bazarlyk.file_viewer import files, previews
from bazarlyk.file_viewer.paths import (markdown_link, normalize_path, parse_selector, reject_bad_escapes, viewer_url)
from bazarlyk.file_viewer.service import content_disposition, version_name

ROOT = "/srv/businesses"
UUID_A, UUID_B = "abcdef01-1111-4111-8111-111111111111", "22222222-2222-4222-8222-22222222222b"


def code(callable_, *args, **kwargs):
    with pytest.raises(DomainError) as caught:
        callable_(*args, **kwargs)
    return caught.value.code


# Lexical paths


def test_paths_are_normalized_relative_to_the_configured_root_only():
    assert normalize_path("ProjectA/hq/notes/a.md", [ROOT]) == "ProjectA/hq/notes/a.md"
    assert normalize_path(f"{ROOT}/ProjectA//hq/./a.md", [ROOT]) == "ProjectA/hq/a.md"
    assert normalize_path("./a b (1).md", [ROOT]) == "a b (1).md"
    for bad in ("/etc/passwd", f"{ROOT}-other/a.md", "/srv/a.md", f"{ROOT}", f"{ROOT}/", "", "   ", "a/"):
        assert code(normalize_path, bad, [ROOT]) == "validation_error", bad


def test_traversal_nul_control_url_and_double_encoded_paths_are_refused():
    for bad in ("../a.md", "a/../../b", "a/..", f"{ROOT}/a/../../etc/passwd", "a\0b", "a\nb", "a\x7fb",
                "file:///etc/passwd", "https://example.com/a.md", "a%2Fb", "a%2fb.md", "%2e%2e/a", "a%5Cb", "a%00b", "x" * 4097):
        assert code(normalize_path, bad, [ROOT]) == "validation_error", bad
    # A literal percent that is not an escape for a separator is an ordinary file name.
    assert normalize_path("100% done.md", [ROOT]) == "100% done.md"


def test_malformed_percent_escapes_in_the_raw_query_are_refused():
    reject_bad_escapes(b"path=a%20b.md")
    for bad in (b"path=a%zz", b"path=100%", b"path=%2"):
        assert code(reject_bad_escapes, bad) == "validation_error", bad


# Selectors and links


def test_selectors_are_closed_and_unambiguous():
    roots = [ROOT]
    assert parse_selector([("path", "a/b.md")], roots).path == "a/b.md"
    doc = parse_selector([("document_id", UUID_A), ("business_id", UUID_B), ("version", "3")], roots)
    assert (doc.document_id, doc.business_id, doc.version) == (UUID_A, UUID_B, 3)
    assert parse_selector([("document_id", UUID_A)], roots).business_id is None
    for bad in ([], [("path", "a"), ("document_id", UUID_A)], [("path", "a"), ("version", "1")],
                [("path", "a"), ("business_id", UUID_B)], [("document_id", UUID_A), ("document_id", UUID_A)],
                [("document_id", "nope")], [("document_id", UUID_A.upper())], [("document_id", UUID_A), ("business_id", "x")],
                [("document_id", UUID_A), ("version", "0")], [("document_id", UUID_A), ("version", "-1")],
                [("document_id", UUID_A), ("version", "1.5")], [("document_id", UUID_A), ("version", "1234567890")],
                [("path", "a"), ("token", "x")], [("path", "")], [("document_id", "")]):
        assert code(parse_selector, bad, roots) == "validation_error", bad


def test_viewer_urls_are_root_relative_encoded_and_round_trip():
    from urllib.parse import parse_qsl, urlsplit
    for path in ("ProjectA/hq/a b (1) & c?.md", "ünïcode/файл.md", "q=1&x=2/a#b.md", "100% done.md", "a+b.md"):
        url = viewer_url(path=path)
        assert url.startswith("/files/view?path=") and "://" not in url and " " not in url
        assert parse_selector(parse_qsl(urlsplit(url).query, strict_parsing=True), [ROOT]).path == path
    assert viewer_url(document_id=UUID_A) == f"/files/view?document_id={UUID_A}"
    assert viewer_url(document_id=UUID_A, business_id=UUID_B, version=4) == f"/files/view?document_id={UUID_A}&business_id={UUID_B}&version=4"


def test_markdown_labels_are_escaped_and_single_line():
    url = viewer_url(path="a/b.md")
    assert markdown_link("Plan [draft] *v2*\nnext_step", url) == f"[Plan \\[draft\\] \\*v2\\* next\\_step]({url})"
    assert markdown_link("", url) == f"[file]({url})"
    assert len(markdown_link("x" * 1000, url)) < 300


def test_attachment_names_cannot_inject_headers_or_quotes():
    header = content_disposition('evil"\r\nSet-Cookie: x=1; ü.md')
    assert "\r" not in header and "\n" not in header and header.count('"') == 2
    assert header.startswith('attachment; filename="') and "filename*=UTF-8''" in header
    assert "%0D%0A" in header and "%C3%BC" in header
    assert version_name("plan.md", 3) == "plan.v3.md" and version_name("Makefile", 2) == "Makefile.v2"


# The no-follow opener


@pytest.fixture
def tree(tmp_path):
    root = tmp_path / "root"
    (root / "biz/hq/notes").mkdir(parents=True)
    (root / "biz/hq/notes/a.md").write_text("# Hello\n")
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "secret.txt").write_text("secret")
    return root.resolve(), outside


def test_a_regular_file_opens_and_reads_only_what_is_asked(tree):
    root, _ = tree
    opened = files.open_regular(root, "biz/hq/notes/a.md")
    try:
        assert (opened.name, opened.size) == ("a.md", 8) and opened.modified_at.endswith("Z")
        assert opened.read_preview(3) == (b"# H", True)
        assert opened.read_preview(100) == (b"# Hello\n", False)  # Always from the start.
        assert opened.sha256(100) and opened.sha256(2) is None
    finally:
        opened.close()


def test_symlinks_are_never_followed_anywhere_below_the_root(tree):
    root, outside = tree
    (root / "biz/link.txt").symlink_to(outside / "secret.txt")
    (root / "biz/dirlink").symlink_to(outside)
    (root / "biz/inside").symlink_to(root / "biz/hq")
    (root / "biz/dangling").symlink_to(root / "nowhere")
    for relative in ("biz/link.txt", "biz/dirlink/secret.txt", "biz/inside/notes/a.md", "biz/dangling"):
        assert code(files.open_regular, root, relative) == "symlink_not_followed", relative


def test_directories_devices_pipes_and_sockets_are_refused_without_opening(tree):
    root, _ = tree
    os.mkfifo(root / "biz/pipe")
    with socket.socket(socket.AF_UNIX) as listener:
        listener.bind(str(root / "biz/sock"))
        for relative in ("biz/hq", "biz/pipe", "biz/sock"):
            assert code(files.open_regular, root, relative) == "not_a_regular_file", relative
    assert stat.S_ISCHR(os.stat("/dev/null").st_mode)
    (root / "biz/null").symlink_to("/dev/null")
    assert code(files.open_regular, root, "biz/null") == "symlink_not_followed"


def test_missing_paths_and_files_used_as_directories_are_not_found(tree):
    root, _ = tree
    for relative in ("biz/hq/missing.md", "nothing/here.md", "biz/hq/notes/a.md/deeper.md", "x" * 300):
        assert code(files.open_regular, root, relative) == "not_found", relative


def test_a_directory_swapped_for_a_symlink_during_the_walk_cannot_escape_the_root(tree, monkeypatch):
    root, outside = tree
    (root / "biz/hq/notes/b.md").write_text("inside")
    (outside / "b.md").write_text("OUTSIDE")
    real, swapped = files._open, []

    def racing(parent, name, flags):
        if name == "notes" and not swapped:  # After the path was checked, before this component opens.
            swapped.append(True)
            os.rename(root / "biz/hq/notes", root / "biz/hq/notes-moved")
            os.symlink(outside, root / "biz/hq/notes")
        return real(parent, name, flags)

    monkeypatch.setattr(files, "_open", racing)
    assert code(files.open_regular, root, "biz/hq/notes/b.md") == "symlink_not_followed"
    assert swapped


def test_a_file_replaced_by_a_pipe_after_the_check_never_blocks_the_open(tree, monkeypatch):
    root, _ = tree
    real, swapped = files._lstat, []

    def stale(parent, name):
        info = real(parent, name)
        if name == "a.md" and not swapped:
            swapped.append(True)
            os.unlink(root / "biz/hq/notes/a.md")
            os.mkfifo(root / "biz/hq/notes/a.md")
        return info

    monkeypatch.setattr(files, "_lstat", stale)
    result = []
    worker = threading.Thread(target=lambda: result.append(code(files.open_regular, root, "biz/hq/notes/a.md")))
    worker.start()
    worker.join(5)
    assert not worker.is_alive(), "opening a FIFO blocked"
    assert result == ["not_a_regular_file"]


def test_a_download_streams_the_opened_snapshot_not_a_reopened_path(tree):
    root, _ = tree
    path = root / "biz/hq/notes/big.bin"
    path.write_bytes(b"x" * 200_000)
    opened = files.open_regular(root, "biz/hq/notes/big.bin")
    os.unlink(path)  # The name is gone; the opened descriptor is what is read.
    path.write_bytes(b"replacement")
    body = b"".join(opened.chunks())
    assert body == b"x" * 200_000
    assert opened.stream.closed


def test_a_growing_file_streams_only_its_opened_size(tree):
    root, _ = tree
    path = root / "biz/hq/notes/log.txt"
    path.write_bytes(b"a" * 1000)
    opened = files.open_regular(root, "biz/hq/notes/log.txt")
    with open(path, "ab") as stream:
        stream.write(b"b" * 1000)
    assert len(b"".join(opened.chunks())) == 1000


# Previews


def preview(name, data, **kwargs):
    return previews.build_preview(name, data, truncated=kwargs.pop("truncated", False), size=len(data), **kwargs)


def test_markdown_renders_structure_and_never_raw_html_images_or_unsafe_links():
    source = ("# Title\n\nPara with *em* and **strong** and `code`.\n\n- one\n- two\n\n| a | b |\n|---|---|\n| 1 | 2 |\n\n"
              "```py\nprint('<b>')\n```\n\n<script>alert(1)</script>\n\n<img src=x onerror=alert(1)>\n\n"
              "[js](javascript:alert(1)) [data](data:text/html,x) [file](file:///etc/passwd) [proto](//evil.example/x) "
              "[ok](https://example.com/a?b=1&c=2) [mail](mailto:a@b.co) ![pixel](https://tracker.example/p.png)\n")
    html = preview("a.md", source.encode())["html"]
    for tag in ("<h1>Title</h1>", "<em>em</em>", "<strong>strong</strong>", "<li>one</li>", "<table>", "<th>a</th>", "<pre><code"):
        assert tag in html, tag
    assert "<script" not in html and "<img" not in html and "onerror=alert" not in html.replace("&lt;img src=x onerror=alert(1)&gt;", "")
    assert "&lt;script&gt;alert(1)&lt;/script&gt;" in html and "&lt;b&gt;" in html
    for blocked in ('href="javascript', 'href="data:', 'href="file:', 'href="//evil'):
        assert blocked not in html, blocked
    assert 'href="https://example.com/a?b=1&amp;c=2" target="_blank" rel="noopener noreferrer"' in html
    assert 'href="mailto:a@b.co"' in html and "tracker.example/p.png" in html and "<img" not in html


def test_markdown_relative_links_become_viewer_links_inside_the_root_only():
    html = preview("a.md", b"[sib](other.md) [up](../x/y.md) [out](../../../../etc/passwd) [abs](/etc/passwd) [frag](#top)",
                   base_dir="biz/hq/notes")["html"]
    assert 'href="/files/view?path=biz/hq/notes/other.md"' in html and 'href="/files/view?path=biz/hq/x/y.md"' in html
    assert "<span>out</span>" in html and "<span>abs</span>" in html and "<span>frag</span>" in html
    assert "etc" not in html.replace("noreferrer", "") and 'href="#' not in html
    assert 'href="/files/view?document_id=1"' in preview("a.md", b"[v](/files/view?document_id=1)")["html"]
    assert "<a " not in preview("a.md", b"[sib](other.md)")["html"]  # No known location: left as text.


def test_text_html_and_binary_kinds():
    assert preview("n.txt", "héllo\nworld".encode())["text"] == "héllo\nworld"
    assert preview("unknown.xyz", b"plain")["kind"] == "text"
    assert preview("bytes.bin", b"\x00\x01\x02")["kind"] == "binary"
    assert preview("latin.txt", "café".encode("latin-1"))["kind"] == "binary"
    assert preview("bom.txt", b"\xef\xbb\xbfhello")["text"] == "hello"
    page = preview("p.html", b"<script>alert(1)</script><p>hi</p>")
    assert page["kind"] == "html" and page["text"].startswith("<script>")
    assert "default-src 'none'" in page["frame_prefix"] and "script-src" not in page["frame_prefix"]
    assert page["frame_prefix"].endswith('<base target="_blank">') and "href" not in page["frame_prefix"]
    assert previews.sniff("a.md", b"# ok") == "markdown" and previews.sniff("a.md", b"\x00\x00") == "binary"
    assert previews.sniff("a.htm", b"<p>") == "html" and previews.sniff("a", b"x") == "text"


def test_a_truncated_preview_cuts_a_character_cleanly_and_says_so():
    data = ("é" * 10).encode()  # Two bytes each.
    cut = preview("n.txt", data[:7], truncated=True)
    assert cut["truncated"] and cut["text"] == "é" * 3 and cut["preview_bytes"] == 7
    assert preview("n.txt", data[:7], truncated=False)["kind"] == "binary"  # Complete, so malformed.


def test_csv_handles_bom_quotes_newlines_and_formulas_as_plain_text():
    data = b'\xef\xbb\xbfname,note\r\n"Smith, J","line1\nline2"\r\n=SUM(A1:A2),"say ""hi"""\r\n\r\n'
    table = preview("t.csv", data)
    assert table["kind"] == "csv"
    assert table["rows"] == [["name", "note"], ["Smith, J", "line1\nline2"], ["=SUM(A1:A2)", 'say "hi"']]
    assert table["columns"] == 2 and not (table["rows_truncated"] or table["columns_truncated"] or table["cells_truncated"])
    # Source is the decoded original (BOM gone, quoting and line endings kept), never the rebuilt cells.
    assert table["text"] == data[3:].decode()


def test_csv_source_is_the_bounded_original_even_when_the_table_is_truncated():
    lines = [f'"row {row}, quoted","line1\nline2",{row}' for row in range(300)]
    data = ("\n".join(lines)).encode()
    table = preview("t.csv", data)
    assert table["rows_truncated"] and len(table["rows"]) == 200
    assert table["text"] == data.decode() and '"row 250, quoted"' in table["text"]
    long = preview("t.csv", b"a," + b"x" * 5000 + b"\n")
    assert long["cells_truncated"] and long["text"] == "a," + "x" * 5000 + "\n"  # The cell cut applies to the table only.
    cut = preview("t.csv", data[:40], truncated=True)  # A preview-limited file keeps its prefix and says so.
    assert cut["truncated"] and cut["text"] == data[:40].decode() and len(cut["text"]) <= previews.PREVIEW_BYTES
    assert preview("t.csv", b"a," + b"z" * 200_000 + b"\n")["text"].startswith("a,zzz")  # Unparseable CSV stays text.


def test_csv_bounds_rows_columns_cells_and_total_size():
    many = "\n".join(",".join(str(row * 100 + col) for col in range(60)) for row in range(300)).encode()
    table = preview("t.csv", many)
    assert len(table["rows"]) == 200 and table["columns"] == 50 and all(len(row) == 50 for row in table["rows"])
    assert table["rows_truncated"] and table["columns_truncated"]
    long = preview("t.csv", b"a," + b"x" * 5000 + b"\n")
    assert len(long["rows"][0][1]) == previews.CSV_CELL_CHARS + 1 and long["cells_truncated"]
    wide = ("\n".join(",".join("y" * 390 for _ in range(50)) for _ in range(200))).encode()
    capped = preview("t.csv", wide)
    assert capped["rows_truncated"] and sum(len(cell) for row in capped["rows"] for cell in row) <= previews.CSV_TOTAL_CHARS
    table_only = {key: value for key, value in capped.items() if key != "text"}
    assert len(str(table_only)) < 1_000_000  # The table is bounded on its own, apart from the bounded source text.
    # The whole response stays bounded: at most the 2 MiB read as source text plus that bounded table.
    limited = preview("t.csv", wide[:previews.PREVIEW_BYTES], truncated=True)
    assert len(limited["text"]) <= previews.PREVIEW_BYTES
    assert len(str({key: value for key, value in limited.items() if key != "text"})) < 1_000_000
    # A cell beyond the stdlib field limit is not parseable CSV: shown as text, not dropped or crashed.
    huge = preview("t.csv", b"a," + b"z" * 200_000 + b"\n")
    assert huge["kind"] == "text" and huge["csv_unparseable"]


def test_rendered_markdown_is_bounded_and_falls_back_to_source():
    source = ("- " * 1_200_000).encode()[:previews.PREVIEW_BYTES]
    result = preview("big.md", source, truncated=True)
    assert result["truncated"] and len(result["text"]) <= previews.PREVIEW_BYTES
    assert result["html"] is None or len(result["html"]) <= previews.MAX_RENDERED_CHARS


def test_a_notes_frontmatter_is_shown_as_collapsed_plain_properties():
    html = preview("n.md", b"---\ntitle: <b>Plan</b>\npinned: false\n---\n\n# Body\n")["html"]
    assert html.startswith('<details class="frontmatter"><summary>Properties</summary><pre>title: &lt;b&gt;Plan&lt;/b&gt;')
    assert "<h1>Body</h1>" in html and "<hr" not in html and "<h2>" not in html
    # An unclosed or distant opening rule is an ordinary horizontal rule.
    assert "<hr />" in preview("n.md", b"---\nnot frontmatter\n")["html"]


# Local images in an HTML preview are embedded; nothing else about the page changes


PNG = b"\x89PNG\r\n\x1a\n" + b"\0" * 24
SVG = b'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 4 4"><rect width="4" height="4"/></svg>'
AVIF = b"\0\0\0\x1cftypavif" + b"\0" * 16


def embed(tmp_path, page, files_=None, base="docs", **limits):
    from bazarlyk.file_viewer import inline_images as module
    for name, value in (files_ or {}).items():
        target = tmp_path / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(value)
    saved = {key: getattr(module, key) for key in limits}
    try:
        for key, value in limits.items():
            setattr(module, key, value)
        return module.inline_images(page, base, lambda path: files.open_regular(str(tmp_path), path))
    finally:
        for key, value in saved.items():
            setattr(module, key, value)


def test_relative_images_are_embedded_with_explicit_mime_types_and_the_rest_of_the_page_is_untouched(tmp_path):
    page = ('<!doctype html>\n<style>.a{background:url(bg.png)}</style><script>var i = \'<img src="x.png">\'</script>\n'
            '<p>x <IMG\n  class=hero src="a.svg" alt="A &amp; B" hidden>\n<img src=\'../shared/p.png?v=2#f\'/><img src="a%2Eavif">'
            '<!-- <img src="c.png"> --><img src="data:image/png;base64,AAAA"><img src="https://x.test/e.png"><img src="/abs.png">')
    rendered, counts = embed(tmp_path, page, {"docs/a.svg": SVG, "shared/p.png": PNG, "docs/a.avif": AVIF, "docs/c.png": PNG})
    assert counts == {"inlined": 3, "unavailable": 0}   # data:, external and absolute references are not relative files: left as written
    assert rendered.count("data:image/svg+xml;base64,") == 1 and rendered.count("data:image/png;base64,") == 2
    assert "data:image/avif;base64," in rendered and 'src="a%2Eavif"' not in rendered
    for kept in ('<style>.a{background:url(bg.png)}</style>', '<script>var i = \'<img src="x.png">\'</script>', '<!-- <img src="c.png"> -->',
                 'src="https://x.test/e.png"', 'src="/abs.png"', 'src="data:image/png;base64,AAAA"', 'class="hero"', 'alt="A &amp; B"', ' hidden'):
        assert kept in rendered, kept


def test_unavailable_images_stay_as_written_and_never_fail_the_preview(tmp_path):
    (tmp_path / "outside").mkdir()
    (tmp_path / "outside/o.png").write_bytes(PNG)
    (tmp_path / "docs").mkdir()
    os.symlink(tmp_path / "outside/o.png", tmp_path / "docs/link.png")
    page = ''.join(f'<img src="{src}">' for src in ("missing.png", "../../escape.png", "link.png", "text.png", "big.png"))
    rendered, counts = embed(tmp_path, page, {"docs/text.png": b"not an image", "docs/big.png": PNG + b"\0" * 100}, IMAGE_BYTES=64)
    assert counts == {"inlined": 0, "unavailable": 5} and rendered == page


def test_image_limits_reuse_duplicates_and_bound_the_total(tmp_path):
    names = {f"docs/i{n}.png": PNG + bytes([n]) for n in range(5)}
    page = "".join(f'<img src="i{n}.png">' for n in (0, 0, 1, 2, 3, 4, 0))
    rendered, counts = embed(tmp_path, page, names, MAX_IMAGES=3)   # three unique files; every use of a duplicate is reused
    assert counts == {"inlined": 5, "unavailable": 2} and rendered.count("base64,") == 5
    rendered, counts = embed(tmp_path, page, names, TOTAL_BYTES=2 * (len(PNG) + 1) + 2)   # room for two unique files
    assert counts == {"inlined": 4, "unavailable": 3}


def test_saved_versions_do_not_read_sibling_files_and_truncated_pages_are_not_rewritten(tmp_path):
    from bazarlyk.file_viewer.inline_images import inline_images
    page = '<img src="a.png"><img src="https://x.test/a.png">'
    assert inline_images(page, "docs", None) == (page, {"inlined": 0, "unavailable": 1})
    payload = previews.build_preview("p.html", page.encode(), truncated=True, size=99, embed_images=lambda text: (text, {}))
    assert "rendered" not in payload and payload["text"] == page
    payload = previews.build_preview("p.html", page.encode(), truncated=False, size=len(page), embed_images=lambda text: ("X", {"inlined": 0}))
    assert payload["text"] == page and payload["rendered"] == "X" and payload["images"] == {"inlined": 0}


def test_malformed_duplicate_and_nul_sources_are_unavailable_not_fatal(tmp_path):
    page = '<img src="http://["><img src="a%00.png"><img src><img src src="x.svg"><img src="ok.png"><img src="ok.png" src="gone.png">'
    rendered, counts = embed(tmp_path, page, {"docs/ok.png": PNG, "docs/x.svg": SVG})
    assert counts == {"inlined": 2, "unavailable": 2}
    assert '<img src="http://[">' in rendered and '<img src="a%00.png">' in rendered and "<img src>" in rendered
    assert rendered.count("data:image/png;base64,") == 2 and rendered.count("gone.png") == 1
