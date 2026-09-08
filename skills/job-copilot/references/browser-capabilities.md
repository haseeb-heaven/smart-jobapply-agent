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

## Existing-session capability descriptor

The bundled Playwright host binding accepts a strict host assertion with
`protocolVersion: 1`, an opaque `sessionId`, and `true` values for
`existingSession`, `completeUrlSnapshots`, `exactListingOpen`, and
`persistentConnection`. The descriptor is validated locally and never crosses
the daemon bridge or enters queue storage or status. It describes a
host-selected existing session; it does not discover, launch, connect to, or
authenticate a browser.

Use `createPlaywrightListingBinding(context, capabilities)` only with the
host-selected existing context. It returns a raw complete-session URL snapshot
to the existing bridge, which verifies session presence and filters unsupported
URLs before Python sees them. On an ambiguous page-create or navigation failure,
the binding pauses additional opens and leaves snapshot recovery to the queue.
For `openListing`, the binding accepts only an already-canonical approved
LinkedIn or Indeed listing URL, creates one page in that supplied context, and
navigates that page to that exact URL. It does not inspect page content, DOM,
cookies, or storage, and it makes no additional redirect-driven navigation or
classification decisions. A browser may return a redirect as part of its
normal navigation; the binding does not follow it with another action or treat
the destination as queue evidence. It never closes the possible blank tab.
