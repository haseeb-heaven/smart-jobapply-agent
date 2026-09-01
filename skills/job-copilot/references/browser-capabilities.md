# Browser capability contract

This project does not require Chrome, macOS, Playwright, or a particular agent.
The operator brings a browser and a bridge compatible with the host environment.

The bridge surface is intentionally two operations:

- `list-tabs` returns a JSON array of visible tab URLs.
- `open-listing <exact-url>` opens one approved canonical listing URL.

For an external bridge, configure an argv prefix. The product appends the
operation and arguments, invokes it without a shell, enforces a timeout, and
redacts command text, URLs, stdout, and stderr from failures. The bridge may be
implemented with Playwright, WebDriver, a browser extension, an agent browser
tool, Windows UI Automation, macOS Apple Events, Linux desktop automation, or
another mechanism. Those implementation details stay outside the core.

An adapter is non-conforming if it exposes or performs clicks, DOM inspection,
typing, selection, file upload, credential access, cookie access, email access,
form navigation, or submission. The optional macOS Chrome adapter is a
compatibility adapter, not the default architecture.
