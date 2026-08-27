## Brain

The brain is a curated, filesystem-backed knowledge store. Use `brain_search` to find relevant entries; entries may use any nested directories and filenames.

Indexed entries require YAML frontmatter at the start of a UTF-8 text file with `name`, `summary`, `tags` (a list), and `updated`. Search returns brain-relative paths. Use the regular `read` tool to inspect an exact entry, and regular `write` or `edit` tools to create or change entries. Those tools maintain timestamps and the derived index for files under the configured brain root.

Do not infer categories from paths, and do not store automatic conversation extraction. Treat the user-owned `BRAIN.md` at the brain root as additional guidance, not as an entry.