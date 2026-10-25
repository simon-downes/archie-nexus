"""Read-side text is returned whole; the exec result cap bounds what reaches the model."""

from archie_plugins.jira.formatting import adf_text, attachment, comment, text


def test_long_adf_description_is_returned_whole():
    body = "d" * 20000
    doc = {"type": "doc", "content": [{"type": "paragraph", "content": [{"text": body}]}]}
    assert adf_text(doc) == body


def test_long_comment_body_is_returned_whole():
    assert comment({"id": "1", "body": "c" * 20000})["body"] == "c" * 20000


def test_text_keeps_explicit_metadata_limits():
    assert text("s" * 20000) == "s" * 20000
    record = attachment({"filename": "f" * 600, "content": "u" * 3000})
    assert len(record["filename"]) == 512
    assert len(record["content_url"]) == 2000
