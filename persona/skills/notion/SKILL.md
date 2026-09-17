---
name: notion
description: >
  Use for bounded Notion search, page/database reads, database queries, comments,
  and explicitly authorized page writes through the exec notion namespace. Not for
  credentials, raw MCP calls, arbitrary API access, connected-source search
  guarantees, or unsupported mutations.
---

# Notion

## Global rules

- Call Notion only through `exec`, using `notion.<function>`.
- Credentials are implicit. Never request, pass, print, or store access tokens.
- Reads are enabled by default; writes are disabled by default. Read and write scopes are independent. Missing or empty scope means unrestricted access.
- Configured page/database IDs are subtree roots. A root and its descendants may be accessed only when Notion metadata proves the relationship.
- Tool calls return JSON-serializable values or raise an error; they do not return error objects.
- Results are bounded projections. Do not expect raw Notion API objects or raw MCP content blocks.
- The exec result has a global 16,000-character limit in addition to the service-level limits below.
- Obtain explicit user intent before creating/updating pages or adding comments.
- Do not automatically retry a mutation after an uncertain outcome. Verify the target first.
- The current search endpoint may return connected-source results such as Google Drive or Slack. This skill does not promise Notion-only search results; inspect the returned `type` before treating a result as a Notion resource.

## Shared input rules

- **Resource ID:** accept a canonical 32-character Notion ID, UUID-form ID, or a URL hosted by `notion.so`, `www.notion.so`, or a `*.notion.site` domain whose path ends in a Notion ID. Do not pass arbitrary URLs.
- **Search query:** non-empty string, maximum 200 characters.
- **Limit:** integer in the documented range; `bool` is not a valid integer limit.
- **Text:** limits count characters. Search, view, sort, and filter strings must be bounded and non-empty where documented.
- **Properties:** `dict[str, str]`, maximum 50 fields; keys must be non-empty and values are at most 2,000 characters. Empty string property values are accepted.
- **Content:** optional string, maximum 8,000 characters. Empty content is accepted.
- **Titles:** optional non-empty string, maximum 500 characters.
- **Comments:** non-empty body, maximum 8,000 characters.

## Policy and scope

The configured scope has this shape:

```yaml
tools:
  notion:
    read:
      enabled: true
      scope:
        pages: ["<page-id>"]
        databases: ["<database-or-data-source-id>"]
    write:
      enabled: false
      scope:
        pages: []
        databases: []
```

- A page root authorizes that page, descendant pages, and nested databases/database items when ancestor metadata proves the relationship.
- A database/data-source root authorizes that database/data source and descendants/items under it.
- A database root does not authorize unrelated pages.
- Read scope does not grant write access. Writes use the write scope.
- Direct roots can be checked locally. Descendants require a provider metadata preflight; if ancestry cannot be established, access is denied rather than assumed.
- A scoped search performs one provider-wide search, then locally deduplicates and filters results. It may return fewer than `limit`; it does not search each scope separately or retry to fill the limit.
- Search results without usable ancestry metadata are omitted when scope is configured. Search results may expose a human-readable `path`, but path text is not by itself an authorization guarantee.
- Write calls may perform an internal fetch to establish target/parent scope before the mutation. A failed or denied preflight means no mutation should be attempted.

## Shared output projections

### Search result

Search returns a list of bounded objects. Fields are included when Notion provides them:

```json
{
  "id": "3de8a35c-22c3-80c5-80de-f0f67737abc7",
  "type": "page",
  "title": "Archie Test",
  "url": "https://app.notion.com/p/...",
  "path": "Simon’s Scratch Pad / Archie Test",
  "last_edited_time": "2026-09-17T18:31:14.403Z"
}
```

`type` may identify a Notion page/database or a connected source. Do not call `get_page` on a connected-source ID. Search preserves the provider’s ID representation; fetched resources use canonical UUID-style IDs.

### Page projection

`get_page` returns a bounded object such as:

```json
{
  "id": "3de8a35c-22c3-80c5-80de-f0f67737abc7",
  "type": "page",
  "title": "Archie Test",
  "url": "https://app.notion.com/p/...",
  "path": "Simon’s Scratch Pad",
  "last_edited_time": "2026-09-17T18:31:14.403Z",
  "content": "Page content as bounded text",
  "properties": {"title": "Archie Test"}
}
```

`properties` is present only with `include_properties=True`. Content is extracted from the MCP page content and capped at 8,000 characters. The projection may omit fields that Notion does not return.

### Database projection

`get_database` returns the same bounded metadata fields as a page, with `type` normally `database` or `data_source`. When `include_views=True` and views are returned, it includes up to 50 view objects under `views`. This is a bounded metadata projection, not a promise that the complete Notion schema/properties are returned.

### Rows and comments

`query_database` returns a list of provider-shaped row dictionaries, capped by `limit` (maximum 100). Rows are not converted into a universal property schema; inspect the returned keys.

`get_comments` returns up to 50 dictionaries. XML discussion responses are normalized to:

```json
{
  "id": "3de8a35c-...",
  "url": "https://app.notion.com/p/...?...",
  "user_url": "user://...",
  "created": "2026-09-17T18:35:02.375Z",
  "body": "Comment text"
}
```

An empty response such as `{"suggested_edits_status": "not_enabled"}` returns `[]`. Provider list responses may contain additional provider fields.

### Write results

Create, update, and comment calls return bounded provider result dictionaries. Their exact fields depend on the Notion MCP response. Do not assume they are full page/comment projections; use the returned ID when available and fetch the resource to inspect its current state.

## Read functions

### `notion.search(query, limit=10, resource_type=None)`

Performs one global provider search and locally returns at most `limit` results. `limit` must be `1..50`. `resource_type`, when supplied, must be `page` or `database`; it is passed to the provider but does not guarantee that connected-source results are excluded.

The result may contain fewer than `limit` items because scoped results are filtered locally and the provider response is not refetched. Deduplication uses the result ID. Inspect `type` before using a result as a Notion page or database.

### `notion.get_page(id_or_url, include_properties=False)`

Fetches one authorized page. A page root or proven descendant is allowed. `include_properties=True` includes up to 50 returned property fields; otherwise properties are omitted. Content is bounded text and may contain Notion markup such as child-page/database markers.

### `notion.get_database(id_or_url, include_views=True)`

Fetches one authorized database/data source. It returns bounded metadata and, when requested and available, up to 50 views. It does not guarantee a complete database schema projection.

### `notion.query_database(id_or_url, view=None, filters=None, sort=None, columns=None, limit=50)`

Authorizes the database before issuing the row query. `limit` must be `1..100`.

- `view`: optional non-empty string, maximum 200 characters.
- `filters`: optional list of at most 20 non-empty strings, each maximum 200 characters. The current tool bounds and forwards these expressions; it does not locally parse or guarantee `Property=Value`, `Property!=Value`, or `Property~=Value` semantics.
- `sort`: optional non-empty string, maximum 200 characters.
- `columns`: optional list of at most 50 non-empty strings.

Rows are capped locally at `limit`. Use the database ID rather than a connected-source search result ID.

### `notion.get_comments(id_or_url, limit=50)`

Fetches up to 50 comments after authorizing the page. A page fetch is performed first to establish scope. No comments are returned for a blocked or metadata-unverifiable page.

## Write functions

All writes require enabled write policy and a permitted target/parent in the write scope. Internal scope preflight fetches are also part of the write authorization sequence.

### `notion.create_page(parent_id_or_url, title=None, properties=None, content=None)`

Creates one child page under a write-authorized parent.

- `title`: optional non-empty string, maximum 500 characters.
- `properties`: optional string mapping, maximum 50 fields and 2,000 characters per value.
- `content`: optional string, maximum 8,000 characters; empty content is accepted.

Obtain explicit confirmation of the parent, title, properties, and content before calling. The returned object is provider-shaped; fetch the created page using its returned ID when available.

### `notion.update_page(id_or_url, properties=None, replacements=None)`

Updates page properties or applies content replacement operations. At least one non-empty update is required.

- `properties`: optional bounded string mapping.
- `replacements`: optional list of at most 20 dictionaries. Each item must have a non-empty string `old`; provider-specific fields such as `new` and `replace_all` may be passed but are not fully locally validated.
- Do not provide `properties` and `replacements` together. The current implementation sends one MCP command field, so combined updates are not a supported atomic operation.

Review the target and exact intended replacement with the user first. Fetch the page afterward to verify the resulting state.

### `notion.add_comment(id_or_url, body)`

Adds an externally visible comment. `body` must be 1–8,000 characters. Confirm the target page and exact comment text before calling. If the result is uncertain, use `get_comments` or fetch the page before attempting another comment.

## Errors and retry policy

Errors are sanitized in the exec result. They do not contain credentials or raw provider payloads.

- `NotionValidationError`: local input, ID, limit, property, filter, or option is invalid. Correct the input; do not retry unchanged.
- `NotionPolicyError` / `NotionWritePolicyError`: the operation is disabled or the resource is outside scope. Do not bypass or retry.
- `NotionWriteError`: the write target/parent is outside write scope. Do not mutate another resource unless the user confirms it.
- `NotionConfigurationError`: credentials are missing, incomplete, or unreadable. Do not retry unchanged; configure authentication through Nexus.
- `NotionAuthenticationError`: provider authentication failed. Reauthenticate; do not retry unchanged.
- `NotionTransportError`: MCP connection or request failed. Reads may be retried after checking connectivity; a mutation may have been transmitted, so treat its outcome as unknown.
- `NotionResponseError` / `NotionQueryError`: the provider returned an unsupported or malformed response. Do not automatically repeat a mutation.
- `NotionMutationIndeterminateError`: a mutation may have succeeded or its result could not be confirmed. Fetch the target or list comments to verify before repeating. Never automatically retry.

## Safe examples

### Search and fetch

```python
async def main():
    results = await notion.search("Archie Test", limit=5, resource_type="page")
    pages = [item for item in results if item.get("type") == "page"]
    return await notion.get_page(pages[0]["id"], include_properties=True) if pages else {"page": None}
```

### Query a database

```python
async def main():
    rows = await notion.query_database(
        "<database-id>",
        filters=["Status=Done"],
        sort="Delivery:desc",
        columns=["Name", "Status"],
        limit=10,
    )
    return rows
```

Treat filter and sort expressions as provider-facing strings; inspect returned rows rather than assuming every property type is supported.

### Confirm before writing

```python
async def main():
    page = await notion.get_page("<page-id>")
    # Only call this after the user confirms the exact target and body.
    return await notion.add_comment("<page-id>", "Confirmed comment text")
```

If a write returns `NotionMutationIndeterminateError`, do not call the write again until the page or comments have been checked.

## Unsupported capabilities

Raw MCP calls, arbitrary Notion API methods, credentials, deletion/archive, database/schema mutations, view creation/update, file uploads, unrestricted block editing, arbitrary property JSON, and general-purpose page mutation are unsupported. The write surface is intentionally limited to string properties, bounded content/replacement operations, child-page creation, and comments.
