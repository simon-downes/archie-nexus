"""Tests for web_fetch and web_search exec tools."""

import io
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from archie_agent.exec.tools import ContentTypeError, get_all_tools
from archie_agent.exec.tools.images import MAX_LONG_EDGE, ImageError, ToolImage
from archie_agent.exec.tools.web import (
    _classify_content_type,
    _extract_selective,
    _html_to_text,
    web_fetch,
    web_search,
)
from PIL import Image


def _png(size=(32, 16)) -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", size, (255, 0, 0)).save(buf, format="PNG")
    return buf.getvalue()


def _client_returning(content_type: str, content: bytes) -> AsyncMock:
    """Build a mocked httpx.AsyncClient whose GET returns ``content``."""
    mock_response = MagicMock()
    mock_response.status_code = 200
    mock_response.headers = {"content-type": content_type}
    mock_response.content = content
    mock_response.text = content.decode("utf-8", errors="replace")
    mock_response.raise_for_status = MagicMock()

    mock_client = AsyncMock()
    mock_client.get = AsyncMock(return_value=mock_response)
    mock_client.__aenter__ = AsyncMock(return_value=mock_client)
    mock_client.__aexit__ = AsyncMock(return_value=False)
    return mock_client


# ---------------------------------------------------------------------------
# Registration tests
# ---------------------------------------------------------------------------


class TestRegistration:
    """Tests for tool registration and guidelines."""

    def test_web_fetch_registered(self):
        tools = get_all_tools()
        assert "web_fetch" in tools

    def test_web_search_registered(self):
        tools = get_all_tools()
        assert "web_search" in tools

    def test_web_tools_are_native(self):
        # Tool choice guidance lives in tools-strategy.md; no per-tool guideline needed.
        from archie_agent.exec.native import make_native_specs

        names = {spec.name for spec in make_native_specs()}
        assert {"web_fetch", "web_search"} <= names


# ---------------------------------------------------------------------------
# _is_binary_content_type tests
# ---------------------------------------------------------------------------


class TestClassifyContentType:
    """Tests for content type classification."""

    @pytest.mark.parametrize(
        "ct",
        [
            "text/html",
            "text/plain",
            "text/css",
            "text/html; charset=utf-8",
            "application/json",
            "application/json; charset=utf-8",
            "application/xml",
            "application/javascript",
            "application/x-javascript",
            "application/xhtml+xml",
            "application/rss+xml",
            "application/atom+xml",
            "application/ld+json",
            "application/problem+json",
            "application/vnd.api+json",
            "image/svg+xml",
            "application/x-yaml",
            "application/yaml",
            "application/toml",
            "application/x-ndjson",
            "application/x-sh",
            "application/sql",
            "APPLICATION/TOML",
        ],
    )
    def test_text_types(self, ct):
        assert _classify_content_type(ct) == "text"

    @pytest.mark.parametrize(
        "ct",
        [
            "image/png",
            "image/jpeg",
            "application/pdf",
            "application/zip",
            "audio/mpeg",
            "video/mp4",
        ],
    )
    def test_binary_types(self, ct):
        assert _classify_content_type(ct) == "binary"

    @pytest.mark.parametrize(
        "ct", ["", "application/octet-stream", "binary/octet-stream", "application/unknown"]
    )
    def test_generic_types(self, ct):
        assert _classify_content_type(ct) == "generic"


# ---------------------------------------------------------------------------
# _html_to_text tests
# ---------------------------------------------------------------------------


class TestHtmlToText:
    """Tests for HTML to text conversion."""

    def test_strips_script_tags(self):
        html = "<p>Hello</p><script>alert('x')</script><p>World</p>"
        result = _html_to_text(html)
        assert "Hello" in result
        assert "World" in result
        assert "alert" not in result

    def test_strips_style_tags(self):
        html = "<style>.x { color: red; }</style><p>Content</p>"
        result = _html_to_text(html)
        assert "Content" in result
        assert "color" not in result

    def test_strips_nav_header_footer(self):
        html = "<nav>Menu</nav><main><p>Body</p></main><footer>Foot</footer>"
        result = _html_to_text(html)
        assert "Body" in result
        assert "Menu" not in result
        assert "Foot" not in result

    def test_decodes_entities(self):
        html = "<p>a &amp; b &lt; c</p>"
        result = _html_to_text(html)
        assert "a & b < c" in result

    def test_collapses_blank_lines(self):
        html = "<p>Line 1</p><p></p><p>Line 2</p>"
        result = _html_to_text(html)
        lines = result.splitlines()
        assert "" not in lines

    def test_block_elements_to_newlines(self):
        html = "<div>A</div><div>B</div>"
        result = _html_to_text(html)
        assert "A" in result
        assert "B" in result
        # Should be on separate lines
        lines = result.splitlines()
        assert len(lines) >= 2


# ---------------------------------------------------------------------------
# _extract_selective tests
# ---------------------------------------------------------------------------


class TestExtractSelective:
    """Tests for selective extraction."""

    def test_extracts_around_match(self):
        lines = [f"line {i}" for i in range(30)]
        lines[15] = "the target keyword here"
        text = "\n".join(lines)
        result = _extract_selective(text, "target")
        assert "target keyword" in result
        # Should include context lines
        assert "line 5" in result or "line 6" in result
        assert "line 25" in result or "line 24" in result

    def test_no_match_returns_truncated(self):
        text = "a" * 10000
        result = _extract_selective(text, "nonexistent")
        assert len(result) == 8000

    def test_empty_terms_returns_truncated(self):
        text = "content " * 2000
        result = _extract_selective(text, "")
        assert len(result) <= 8000

    def test_section_markers_between_gaps(self):
        lines = [f"line {i}" for i in range(100)]
        lines[10] = "first match"
        lines[90] = "second match"
        text = "\n".join(lines)
        result = _extract_selective(text, "match")
        assert "..." in result

    def test_case_insensitive(self):
        text = "Line with TARGET here\nother line"
        result = _extract_selective(text, "target")
        assert "TARGET" in result


# ---------------------------------------------------------------------------
# web_fetch tests
# ---------------------------------------------------------------------------


class TestWebFetch:
    """Tests for web_fetch (mocked HTTP)."""

    @pytest.mark.asyncio
    async def test_successful_fetch_html(self):
        """Fetches HTML and returns cleaned text."""
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.headers = {"content-type": "text/html; charset=utf-8"}
        mock_response.text = "<html><body><p>Hello World</p></body></html>"
        mock_response.raise_for_status = MagicMock()

        mock_client = AsyncMock()
        mock_client.get = AsyncMock(return_value=mock_response)
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)

        with patch("httpx.AsyncClient", return_value=mock_client):
            result = await web_fetch("https://example.com")
        assert "Hello World" in result

    @pytest.mark.asyncio
    async def test_successful_fetch_json(self):
        """Fetches JSON and returns raw text (no HTML cleaning)."""
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.headers = {"content-type": "application/json"}
        mock_response.text = '{"key": "value"}'
        mock_response.raise_for_status = MagicMock()

        mock_client = AsyncMock()
        mock_client.get = AsyncMock(return_value=mock_response)
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)

        with patch("httpx.AsyncClient", return_value=mock_client):
            result = await web_fetch("https://api.example.com/data", mode="full")
        assert '{"key": "value"}' in result

    @pytest.mark.asyncio
    async def test_mode_truncated(self):
        """Truncated mode returns first 8000 chars."""
        long_text = "x" * 20000
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.headers = {"content-type": "text/plain"}
        mock_response.text = long_text
        mock_response.raise_for_status = MagicMock()

        mock_client = AsyncMock()
        mock_client.get = AsyncMock(return_value=mock_response)
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)

        with patch("httpx.AsyncClient", return_value=mock_client):
            result = await web_fetch("https://example.com", mode="truncated")
        assert result.startswith("URL: https://example.com\n")
        assert "Type: text/plain" in result
        # Content portion is truncated to 8000 chars
        assert "x" * 100 in result

    @pytest.mark.asyncio
    async def test_mode_full(self):
        """Full mode returns all content."""
        long_text = "x" * 20000
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.headers = {"content-type": "text/plain"}
        mock_response.text = long_text
        mock_response.raise_for_status = MagicMock()

        mock_client = AsyncMock()
        mock_client.get = AsyncMock(return_value=mock_response)
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)

        with patch("httpx.AsyncClient", return_value=mock_client):
            result = await web_fetch("https://example.com", mode="full")
        assert result.startswith("URL: https://example.com\n")
        assert "Type: text/plain" in result
        # Full content present (20000 x chars + header)
        assert len(result) > 20000

    @pytest.mark.asyncio
    async def test_mode_selective_with_terms(self):
        """Selective mode with terms extracts around matches."""
        lines = [f"line {i}" for i in range(50)]
        lines[25] = "important keyword here"
        text = "\n".join(lines)

        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.headers = {"content-type": "text/plain"}
        mock_response.text = text
        mock_response.raise_for_status = MagicMock()

        mock_client = AsyncMock()
        mock_client.get = AsyncMock(return_value=mock_response)
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)

        with patch("httpx.AsyncClient", return_value=mock_client):
            result = await web_fetch("https://example.com", search_terms="keyword")
        assert "important keyword here" in result
        # Should not include all 50 lines
        assert "line 0" not in result

    @pytest.mark.asyncio
    async def test_mode_selective_no_terms_falls_back(self):
        """Selective mode without terms returns truncated."""
        long_text = "y" * 20000
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.headers = {"content-type": "text/plain"}
        mock_response.text = long_text
        mock_response.raise_for_status = MagicMock()

        mock_client = AsyncMock()
        mock_client.get = AsyncMock(return_value=mock_response)
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)

        with patch("httpx.AsyncClient", return_value=mock_client):
            result = await web_fetch("https://example.com")
        assert result.startswith("URL: https://example.com\n")
        assert "Type: text/plain" in result
        # Content is truncated (8000 y's + header)
        assert "y" * 100 in result

    @pytest.mark.asyncio
    async def test_empty_url_raises(self):
        with pytest.raises(ConnectionError, match="empty"):
            await web_fetch("")

    @pytest.mark.asyncio
    async def test_non_http_url_raises(self):
        with pytest.raises(ConnectionError, match="Unsupported URL scheme"):
            await web_fetch("ftp://example.com/file")

    @pytest.mark.asyncio
    async def test_binary_content_type_raises(self):
        """Binary content that is not a supported image raises ContentTypeError."""
        mock_client = _client_returning("application/pdf", b"%PDF-1.7\n\x00\x01binary")
        with patch("httpx.AsyncClient", return_value=mock_client):
            with pytest.raises(ContentTypeError, match="Non-text"):
                await web_fetch("https://example.com/doc.pdf")

    @pytest.mark.asyncio
    async def test_image_content_type_with_non_image_body_raises(self):
        mock_client = _client_returning("image/png", b"not really a png")
        with patch("httpx.AsyncClient", return_value=mock_client):
            with pytest.raises(ContentTypeError, match="Non-text"):
                await web_fetch("https://example.com/image.png")

    @pytest.mark.asyncio
    async def test_svg_returned_as_text(self):
        svg = b'<svg xmlns="http://www.w3.org/2000/svg"><circle r="4"/></svg>'
        with patch("httpx.AsyncClient", return_value=_client_returning("image/svg+xml", svg)):
            result = await web_fetch("https://example.com/logo.svg", mode="full")
        assert isinstance(result, str)
        assert "Type: image/svg+xml" in result
        assert '<circle r="4"/>' in result  # XML is kept, not stripped like HTML

    @pytest.mark.asyncio
    @pytest.mark.parametrize("ct", ["application/octet-stream", ""])
    async def test_generic_type_text_body_returned_as_text(self, ct):
        body = b"[tool.ruff]\nline-length = 100\n"
        with patch("httpx.AsyncClient", return_value=_client_returning(ct, body)):
            result = await web_fetch("https://example.com/pyproject.toml", mode="full")
        assert isinstance(result, str)
        assert "line-length = 100" in result

    @pytest.mark.asyncio
    @pytest.mark.parametrize("ct", ["application/octet-stream", ""])
    async def test_generic_type_binary_body_raises(self, ct):
        body = b"PK\x03\x04\x00\x00binary zip"
        with patch("httpx.AsyncClient", return_value=_client_returning(ct, body)):
            with pytest.raises(ContentTypeError, match="Non-text"):
                await web_fetch("https://example.com/archive.zip")

    @pytest.mark.asyncio
    @pytest.mark.parametrize("ct", ["image/png", "application/octet-stream", ""])
    async def test_image_returns_tool_image(self, ct):
        """Image bytes come back as a ToolImage, whatever binary type the server sends."""
        data = _png()
        url = "https://example.com/a.png"
        with patch("httpx.AsyncClient", return_value=_client_returning(ct, data)):
            result = await web_fetch(url, mode="full", search_terms="ignored")
        assert isinstance(result, ToolImage)
        assert result.image.data == data
        assert result.image.format == "png"
        assert result.image.source == url
        assert result.caption.startswith(f"Image: {url} (PNG, 32x16")

    @pytest.mark.asyncio
    async def test_large_image_is_downscaled(self):
        data = _png(size=(4000, 2000))
        with patch("httpx.AsyncClient", return_value=_client_returning("image/png", data)):
            result = await web_fetch("https://example.com/big.png")
        assert isinstance(result, ToolImage)
        assert max(result.image.width, result.image.height) <= MAX_LONG_EDGE
        assert "resized from 4000x2000" in result.caption

    @pytest.mark.asyncio
    async def test_corrupt_image_raises(self):
        body = b"\x89PNG\r\n\x1a\n" + b"\x00garbage" * 10
        with patch("httpx.AsyncClient", return_value=_client_returning("image/png", body)):
            with pytest.raises(ImageError):
                await web_fetch("https://example.com/bad.png")

    @pytest.mark.asyncio
    async def test_image_inside_exec_returns_caption_text(self):
        from archie_agent.exec.runner import _wrap_for_audit

        calls = []
        client = _client_returning("image/png", _png())
        with patch("httpx.AsyncClient", return_value=client):
            result = await _wrap_for_audit(web_fetch, "web_fetch", calls)(
                "https://example.com/a.png"
            )
        assert isinstance(result, str)
        assert "Image: https://example.com/a.png" in result
        assert "native `web_fetch`" in result

    @pytest.mark.asyncio
    async def test_timeout_raises(self):
        """Timeout raises TimeoutError."""
        import httpx

        mock_client = AsyncMock()
        mock_client.get = AsyncMock(side_effect=httpx.TimeoutException("timed out"))
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)

        with patch("httpx.AsyncClient", return_value=mock_client):
            with pytest.raises(TimeoutError, match="timed out"):
                await web_fetch("https://example.com")

    @pytest.mark.asyncio
    async def test_connect_error_raises(self):
        """Connection errors raise ConnectionError."""
        import httpx

        mock_client = AsyncMock()
        mock_client.get = AsyncMock(side_effect=httpx.ConnectError("DNS failed"))
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)

        with patch("httpx.AsyncClient", return_value=mock_client):
            with pytest.raises(ConnectionError, match="Connection failed"):
                await web_fetch("https://unreachable.example.com")

    @pytest.mark.asyncio
    async def test_http_status_error_raises(self):
        """Non-2xx status raises ConnectionError."""
        import httpx

        mock_response = MagicMock()
        mock_response.status_code = 404
        mock_response.raise_for_status = MagicMock(
            side_effect=httpx.HTTPStatusError(
                "Not Found", request=MagicMock(), response=mock_response
            )
        )

        mock_client = AsyncMock()
        mock_client.get = AsyncMock(return_value=mock_response)
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)

        with patch("httpx.AsyncClient", return_value=mock_client):
            with pytest.raises(ConnectionError, match="404"):
                await web_fetch("https://example.com/missing")


# ---------------------------------------------------------------------------
# web_search tests
# ---------------------------------------------------------------------------


class TestWebSearch:
    """Tests for web_search (mocked DDGS)."""

    @pytest.mark.asyncio
    async def test_successful_search(self):
        """Returns formatted results from DDGS."""
        mock_results = [
            {"title": "Result 1", "href": "https://a.com", "body": "First result"},
            {"title": "Result 2", "href": "https://b.com", "body": "Second result"},
        ]
        mock_ddgs = MagicMock()
        mock_ddgs.return_value.text.return_value = mock_results

        with patch("ddgs.DDGS", mock_ddgs):
            results = await web_search("test query")

        assert "1. Result 1" in results
        assert "https://a.com" in results
        assert "First result" in results
        assert "2. Result 2" in results
        assert "https://b.com" in results
        assert "Second result" in results

    @pytest.mark.asyncio
    async def test_empty_query_returns_empty(self):
        result = await web_search("")
        assert result == "No results found."

    @pytest.mark.asyncio
    async def test_whitespace_query_returns_empty(self):
        result = await web_search("   ")
        assert result == "No results found."

    @pytest.mark.asyncio
    async def test_strips_query_whitespace(self):
        """Query is stripped before searching."""
        mock_ddgs = MagicMock()
        mock_ddgs.return_value.text.return_value = []

        with patch("ddgs.DDGS", mock_ddgs):
            await web_search("  hello  ")

        mock_ddgs.return_value.text.assert_called_once_with(
            "hello", safesearch="off", backend="auto", max_results=8
        )

    @pytest.mark.asyncio
    async def test_exception_returns_empty(self):
        """Any exception from DDGS returns no results message."""
        mock_ddgs = MagicMock()
        mock_ddgs.return_value.text.side_effect = RuntimeError("API down")

        with patch("ddgs.DDGS", mock_ddgs):
            result = await web_search("test")
        assert result == "No results found."

    @pytest.mark.asyncio
    async def test_none_results_returns_empty(self):
        """DDGS returning None yields no results message."""
        mock_ddgs = MagicMock()
        mock_ddgs.return_value.text.return_value = None

        with patch("ddgs.DDGS", mock_ddgs):
            result = await web_search("test")
        assert result == "No results found."
