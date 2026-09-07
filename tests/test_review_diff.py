import pytest

from p4mcp.services.review_diff import build_hunks, looks_binary, parse_unified_diff


def test_build_hunks_uses_one_sided_line_anchors_and_exact_content():
    result = build_hunks("old\nkeep\n", "new\nkeep\n", context_lines=0)

    assert result["hunks"]
    lines = result["hunks"][0]["lines"]
    assert lines[0]["kind"] == "delete"
    assert lines[0]["leftLine"] == 1
    assert lines[0]["rightLine"] is None
    assert lines[0]["content"] == "old\n"
    assert lines[1]["kind"] == "add"
    assert lines[1]["leftLine"] is None
    assert lines[1]["rightLine"] == 1
    assert lines[1]["content"] == "new\n"


def test_build_hunks_recognizes_utf16_without_bom():
    result = build_hunks(
        "a\n".encode("utf-16-le"),
        "b\n".encode("utf-16-le"),
        context_lines=0,
    )

    assert result["oldEncoding"] == "utf-16-le"
    assert result["newEncoding"] == "utf-16-le"
    assert result["hunks"][0]["lines"][0]["content"] == "a\n"


def test_binary_gate_does_not_reject_utf16_without_bom():
    # Structured service paths run the binary check before build_hunks.  Keep
    # that gate consistent with the decoder for BOM-less UTF-16 content.
    assert looks_binary("a\n".encode("utf-16-le")) is False


def test_parse_dual_diff2_header_preserves_both_paths():
    raw = (
        "==== //depot/a.txt#1 (text) - //depot/a.txt#2 (text) ====\n"
        "@@ -1,1 +1,1 @@\n-old\n+new\n"
    )

    result = parse_unified_diff(raw)
    item = result["files"][0]
    assert item["dualPath"] is True
    assert item["sourcePath"] == "//depot/a.txt"
    assert item["targetPath"] == "//depot/a.txt"
    assert result["complete"] is True


def test_parse_diff2_header_separates_at_change_revision():
    raw = (
        "==== //depot/a.txt#7 (text) - //depot/a.txt@=200 (text) ====\n"
        "@@ -1,1 +1,1 @@\n-old\n+new\n"
    )

    item = parse_unified_diff(raw)["files"][0]
    assert item["targetPath"] == "//depot/a.txt"
    assert item["targetRevision"] == "@=200"


def test_parse_unified_diff_preserves_terminator_for_empty_context_record():
    result = parse_unified_diff(
        "==== //depot/a.txt#1 (text) ====\n"
        "@@ -1,1 +1,2 @@\n"
        "\n"
        "+new\n"
    )

    item = result["files"][0]
    assert item["complete"] is True
    assert item["hunks"][0]["lines"][0]["kind"] == "context"
    assert item["hunks"][0]["lines"][0]["content"] == "\n"


def test_parse_identical_and_type_only_sections_are_explicit():
    identical = parse_unified_diff(
        "==== //depot/a.txt#1 (text) - //depot/a.txt#2 (text) ==== identical\n"
    )["files"][0]
    assert identical["identical"] is True
    assert identical["complete"] is True
    assert identical["hunks"] == []

    type_change = parse_unified_diff(
        "==== //depot/a.txt#1 (text) - //depot/a.txt#2 (binary) ==== types\n"
    )["files"][0]
    assert type_change["supported"] is False
    assert type_change["complete"] is False


def test_add_body_header_looking_line_is_not_a_phantom_file():
    raw = (
        "==== //depot/add.txt#1 (text) ====\n"
        "first\n"
        "==== //depot/not-a-file.txt ====\n"
        "last\n"
        "==== //depot/second.txt#1 (text) ====\n"
        "second\n"
    )
    metadata = {
        "files": [
            {"depotFile": "//depot/add.txt", "action": "add", "type": "text", "rev": "1"},
            {"depotFile": "//depot/second.txt", "action": "add", "type": "text", "rev": "1"},
        ]
    }

    result = parse_unified_diff(raw, metadata=metadata)
    assert [item["depotFile"] for item in result["files"]] == [
        "//depot/add.txt", "//depot/second.txt"
    ]
    contents = [line["content"] for line in result["files"][0]["hunks"][0]["lines"]]
    assert "==== //depot/not-a-file.txt ====\n" in contents


def test_metadata_reconciliation_marks_missing_and_unknown_actions():
    metadata = {
        "files": [
            {"depotFile": "//depot/missing.txt", "action": "edit", "type": "text", "rev": "3"},
            {"depotFile": "//depot/future.txt", "action": "future-op", "type": "text", "rev": "1"},
        ]
    }
    result = parse_unified_diff("", metadata=metadata)

    assert result["complete"] is False
    assert {item["depotFile"] for item in result["files"]} == {
        "//depot/missing.txt", "//depot/future.txt"
    }
    assert all(item["supported"] is False for item in result["files"])


def test_metadata_reconciliation_explains_missing_binary_section():
    result = parse_unified_diff(
        "",
        metadata={"files": [{
            "depotFile": "//depot/image.bin",
            "action": "add",
            "type": "binary+ prospect",
        }]},
    )

    item = result["files"][0]
    assert item["binary"] is True
    assert item["reason"] == "binary file; line diff unavailable"


def test_malformed_hunk_is_not_reported_complete():
    result = parse_unified_diff(
        "==== //depot/a.txt#2 (text) ====\n"
        "@@ -1,2 +1,2 @@\n"
        "-only-one-line\n"
    )
    item = result["files"][0]
    assert item["complete"] is False
    assert result["complete"] is False


def test_p4python_chunk_boundary_is_reconstructed_without_losing_header():
    chunks = [
        "==== //depot/a.txt#2 (text) ====",
        "\n@@ -1,1 +1,1 @@",
        "\n-old\n+new\n",
    ]

    result = parse_unified_diff(chunks)
    assert result["complete"] is True
    assert result["files"][0]["hunks"][0]["lines"][-1]["content"] == "new\n"


def test_p4python_unterminated_hunk_records_are_separated_safely():
    chunks = [
        "==== //depot/a.txt#1 (text) ====\n",
        "@@ -1,1 +1,1 @@",
        "-old\n",
        "+new\n",
        "@@ -3,1 +3,1 @@",
        "-before\n",
        "+after\n",
    ]

    result = parse_unified_diff(chunks)
    assert result["complete"] is True
    assert len(result["files"][0]["hunks"]) == 2
    assert result["files"][0]["hunks"][1]["lines"][-1]["content"] == "after\n"


def test_hunk_section_split_after_closing_marker_is_not_made_context():
    whole = (
        "==== //depot/a.txt#1 (text) - //depot/a.txt#2 (text) ====\n"
        "@@ -1,3 +1,3 @@ section\n"
        " one\n"
        "-old\n"
        "+new\n"
        " three\n"
    )
    chunks = [
        "==== //depot/a.txt#1 (text) - //depot/a.txt#2 (text) ====\n",
        "@@ -1,3 +1,3 @@",
        " section\n one\n-old\n+new\n three\n",
    ]

    expected = parse_unified_diff(whole)
    result = parse_unified_diff(chunks)

    assert result == expected
    assert result["complete"] is True
    assert result["files"][0]["hunks"][0]["section"] == "section"
    assert result["files"][0]["hunks"][0]["lines"][0]["content"] == "one\n"


def test_hunk_context_split_after_closing_marker_remains_context():
    chunks = [
        "==== //depot/a.txt#1 (text) - //depot/a.txt#2 (text) ====\n",
        "@@ -1,3 +1,3 @@",
        " one\n-old\n+new\n three\n",
    ]

    result = parse_unified_diff(chunks)

    assert result["complete"] is True
    assert result["files"][0]["hunks"][0]["section"] is None
    assert result["files"][0]["hunks"][0]["lines"][0]["content"] == "one\n"


def test_hunk_section_split_keeps_header_like_context_line():
    whole = (
        "==== //depot/a.txt#1 (text) - //depot/a.txt#2 (text) ====\n"
        "@@ -1,2 +1,2 @@ section\n"
        " ==== //example ====\n"
        "-old\n"
        "+new\n"
    )
    chunks = [
        "==== //depot/a.txt#1 (text) - //depot/a.txt#2 (text) ====\n",
        "@@ -1,2 +1,2 @@",
        " section\n ==== //example ====\n-old\n+new\n",
    ]

    result = parse_unified_diff(chunks)

    assert result == parse_unified_diff(whole)
    assert result["complete"] is True
    assert result["files"][0]["hunks"][0]["lines"][0]["content"] == (
        "==== //example ====\n"
    )


@pytest.mark.parametrize("section_split", ["function", "function-name"])
def test_hunk_section_continuation_with_diff_prefix_is_not_made_body(
        section_split):
    section = "function-name here"
    split_at = len(section_split)
    prefix = (
        "==== //depot/a.txt#1 (text) - //depot/a.txt#2 (text) ====\n"
        "@@ -1,1 +1,1 @@ "
    )
    body = "-old\n+new\n"
    whole = prefix + section + "\n" + body
    chunks = [
        prefix + section[:split_at],
        section[split_at:] + "\n" + body,
    ]

    result = parse_unified_diff(chunks)

    assert result == parse_unified_diff(whole)
    assert result["complete"] is True
    assert result["files"][0]["hunks"][0]["section"] == section


def test_context_prefix_split_before_header_like_content_stays_in_hunk():
    whole = (
        "==== //depot/a.txt#1 (text) - //depot/a.txt#2 (text) ====\n"
        "@@ -1,2 +1,2 @@ section\n"
        " ==== //example ====\n"
        "-old\n"
        "+new\n"
    )
    chunks = [
        "==== //depot/a.txt#1 (text) - //depot/a.txt#2 (text) ====\n"
        "@@ -1,2 +1,2 @@ section\n ",
        "==== //example ====\n-old\n+new\n",
    ]

    result = parse_unified_diff(chunks)

    assert result == parse_unified_diff(whole)
    assert result["complete"] is True


@pytest.mark.parametrize(
    ("prefix", "header", "body"),
    [
        (" ", "@@ -1,2 +1,2 @@ section\n", "-old\n+new\n"),
        ("+", "@@ -1,1 +1,2 @@ section\n", "-old\n+new\n"),
        ("-", "@@ -1,2 +1,1 @@ section\n", "-old\n+new\n"),
    ],
)
def test_diff_prefix_split_before_header_like_content_stays_in_hunk(
        prefix, header, body):
    file_header = (
        "==== //depot/a.txt#1 (text) - //depot/a.txt#2 (text) ====\n"
    )
    source_record = prefix + "==== //example ====\n"
    whole = file_header + header + body + source_record
    chunks = [file_header + header + body + prefix, "==== //example ====\n"]

    result = parse_unified_diff(chunks)

    assert result == parse_unified_diff(whole)
    assert result["complete"] is True
    assert len(result["files"]) == 1


def test_byte_chunks_are_joined_before_utf8_decoding():
    raw = [
        b"==== //depot/a.txt#1 (text) ====\n",
        b"@@ -1,1 +1,1 @@\n-old\n+",
        "新".encode("utf-8") + b"\n",
    ]

    result = parse_unified_diff(raw)
    assert result["complete"] is True
    assert result["files"][0]["hunks"][0]["lines"][-1]["content"] == "新\n"


def test_unterminated_diff_line_before_next_file_header_is_separated():
    chunks = [
        "==== //depot/one.txt#1 (text) ====\n",
        "@@ -1,1 +1,1 @@\n-old\n+one",
        "==== //depot/two.txt#1 (text) ====\n",
        "@@ -1,1 +1,1 @@\n-old\n+two\n",
    ]

    result = parse_unified_diff(chunks)
    assert result["complete"] is True
    assert [item["depotFile"] for item in result["files"]] == [
        "//depot/one.txt", "//depot/two.txt"
    ]


def test_invalid_context_limit_is_rejected():
    with pytest.raises(ValueError):
        parse_unified_diff("", context_lines=-1)


def test_max_bytes_removes_hunks_with_and_without_metadata():
    raw = (
        "==== //depot/a.txt#1 (text) - //depot/a.txt#2 (text) ====\n"
        "@@ -1,1 +1,1 @@\n-old-value\n+new-value\n"
    )
    metadata = {"files": [{
        "depotFile": "//depot/a.txt", "action": "edit", "type": "text",
    }]}

    for result in (
            parse_unified_diff(raw, max_bytes=1),
            parse_unified_diff(raw, metadata=metadata, max_bytes=1)):
        item = result["files"][0]
        assert item["supported"] is False
        assert item["complete"] is False
        assert item["hunks"] == []


def test_binary_metadata_removes_text_looking_hunks():
    raw = (
        "==== //depot/a.bin#1 (text) - //depot/a.bin#2 (text) ====\n"
        "@@ -1,1 +1,1 @@\n-old\n+new\n"
    )
    result = parse_unified_diff(raw, metadata={"files": [{
        "depotFile": "//depot/a.bin", "action": "edit", "type": "binary",
    }]})

    item = result["files"][0]
    assert item["binary"] is True
    assert item["supported"] is False
    assert item["hunks"] == []


def test_large_single_hunk_uses_incremental_line_accounting():
    line_count = 5_000
    raw = (
        "==== //depot/a.txt#1 (text) - //depot/a.txt#2 (text) ====\n"
        f"@@ -1,{line_count} +1,{line_count} @@\n"
        + " x\n" * (line_count - 1)
        + "-old\n+new\n"
    )

    result = parse_unified_diff(raw)

    assert result["complete"] is True
    assert result["files"][0]["hunks"][0]["lines"][-1]["content"] == "new\n"
