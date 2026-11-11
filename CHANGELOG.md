# Changelog

## Unreleased

- Claude Desktop support. `computer mcp install` puts a pinned Desktop Commander (0.3.2) in a managed directory and `computer mcp serve`
  launches it over stdio and needs no `PATH`. `computer claude install|status|uninstall` wires it into Claude Desktop's config
  (atomic write, backup, other keys preserved), ensures the autostart entry, and checks everything with a real MCP handshake.
