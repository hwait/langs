"""The public shell: the three files a browser fetches before it can hold the token.

The launch token rides in the URL fragment, which a browser never sends, so the first
navigation cannot carry the token header -- and `server._dispatch` checks the token before
routing. The shell is therefore answered *before* that check, from an exact-match allowlist,
and is the only thing that is. `Host` is still checked first: DNS rebinding is the same
attack whether the answer is a page or a learner's record.

**The root is the installed package, never the workspace.** A workspace holds learner data
and no core source, so a static root under it is one misconfiguration from serving the
database. The files are read by their allowlisted *name* through `importlib.resources`; no
path is ever joined from the request, so there is no traversal to get wrong.
"""

from __future__ import annotations

from importlib.resources import files

#: Request path -> (file under `static/`, media type). Matched against the raw request
#: target, so a query string or any other spelling is not the shell and goes through the
#: token check like everything else.
SHELL_FILES: dict[str, tuple[str, str]] = {
    "/": ("index.html", "text/html; charset=utf-8"),
    "/app.js": ("app.js", "text/javascript; charset=utf-8"),
    "/app.css": ("app.css", "text/css; charset=utf-8"),
}

#: No inline script or style anywhere, so nothing injected into a page can run. Audio is
#: played from an object URL the page builds from bytes it fetched with the token, which
#: is why `media-src` admits `blob:` and nothing else beyond this origin.
CONTENT_SECURITY_POLICY = "; ".join(
    (
        "default-src 'self'",
        "script-src 'self'",
        "style-src 'self'",
        "connect-src 'self'",
        "media-src 'self' blob:",
        "img-src 'self'",
        "base-uri 'none'",
        "form-action 'none'",
        "frame-ancestors 'none'",
    )
)

#: Sent with every shell file. `no-store` so a cached shell cannot outlive the server that
#: minted its token; `no-referrer` so nothing the page navigates to learns its address.
HEADERS: dict[str, str] = {
    "Content-Security-Policy": CONTENT_SECURITY_POLICY,
    "Referrer-Policy": "no-referrer",
}


def shell_file(target: str) -> tuple[bytes, str] | None:
    """The bytes and media type for a shell request target, or `None` if it is not one."""

    entry = SHELL_FILES.get(target)
    if entry is None:
        return None
    name, media = entry
    return (files("linguawiki.client") / "static" / name).read_bytes(), media


__all__ = ["CONTENT_SECURITY_POLICY", "HEADERS", "SHELL_FILES", "shell_file"]
