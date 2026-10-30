#!/usr/bin/env python3
"""App Store Connect helper for the ios-testflight workflow (TASK-142).

The repository is public, so its Actions logs are public. This script never
prints credentials, tokens, headers or tester identities; it prints short
status lines only, and error bodies are scrubbed and truncated first.

Subcommands
  allocate    Print the next free build number (max integer build number of any
              of the app's builds + 1, minimum 1) on stdout. Read-only.
  distribute  Poll App Store Connect until the uploaded build finishes
              processing, then make sure it is in the internal TestFlight group
              and (optionally) set the en-GB "What to Test" text. Idempotent.

Environment
  ASC_KEY_ID, ASC_ISSUER_ID   API key identifiers
  ASC_KEY_PATH                path of the decoded .p8 private key (mode 600)
  ASC_BUNDLE_ID               app bundle id (default com.radioshake)
  GITHUB_OUTPUT               optional; key=value lines are appended to it

A fresh JWT (10 minutes, Apple's maximum is 20) is signed for every request.
"""
from __future__ import annotations

import argparse
import os
import re
import sys
import time
from typing import Any, Callable, Dict, Iterator, List, Optional

API_ROOT = "https://api.appstoreconnect.apple.com"
JWT_LIFETIME_SECONDS = 600
MAX_PAGES = 200
RETRY_STATUSES = {429, 500, 502, 503, 504}
RETRY_DELAYS = (2, 4, 8, 16)
NOTES_LOCALE = "en-GB"
NOTES_MAX_CHARS = 4000

VERSION_RE = re.compile(r"[0-9]+\.[0-9]+(\.[0-9]+)?")
BUILD_RE = re.compile(r"[1-9][0-9]{0,8}")
GROUP_RE = re.compile(r"[A-Za-z0-9 _-]{1,50}")
INTEGER_RE = re.compile(r"[0-9]{1,9}")
_SECRETISH = re.compile(r"eyJ[A-Za-z0-9_\-.]{10,}|[A-Za-z0-9+/=_-]{60,}|-----BEGIN[^-]*-----")


class AscError(Exception):
    """Raised for any App Store Connect failure; the message is log-safe."""


def scrub(text: str, limit: int = 300) -> str:
    """Make arbitrary server text safe for a public log."""
    text = _SECRETISH.sub("<redacted>", text.replace("\r", " ").replace("\n", " "))
    return text[:limit]


def emit(key: str, value: str) -> None:
    """Append a step output; values are constrained to a single safe line."""
    if not re.fullmatch(r"[A-Za-z0-9_.:-]*", value):
        raise AscError(f"refusing to write unsafe output value for {key}")
    path = os.environ.get("GITHUB_OUTPUT")
    if path:
        with open(path, "a", encoding="utf-8") as fh:
            fh.write(f"{key}={value}\n")


def log(message: str) -> None:
    print(message, file=sys.stderr, flush=True)


def make_token_factory(key_id: str, issuer_id: str, key_path: str) -> Callable[[], str]:
    import jwt  # PyJWT, imported lazily so unit tests need no crypto

    with open(key_path, "r", encoding="utf-8") as fh:
        private_key = fh.read()

    def factory() -> str:
        now = int(time.time())
        return jwt.encode(
            {"iss": issuer_id, "iat": now, "exp": now + JWT_LIFETIME_SECONDS, "aud": "appstoreconnect-v1"},
            private_key,
            algorithm="ES256",
            headers={"kid": key_id, "typ": "JWT"},
        )

    return factory


class RequestsHttp:
    """Thin adapter so tests can substitute a fake transport."""

    def __init__(self) -> None:
        import requests

        self._session = requests.Session()

    def request(self, method: str, url: str, headers: Dict[str, str], json: Any = None,
                params: Optional[Dict[str, str]] = None, timeout: int = 30):
        return self._session.request(method, url, headers=headers, json=json, params=params, timeout=timeout)


class Asc:
    def __init__(self, http: Any, token_factory: Callable[[], str],
                 sleep: Callable[[float], None] = time.sleep,
                 clock: Callable[[], float] = time.monotonic) -> None:
        self.http = http
        self.token_factory = token_factory
        self.sleep = sleep
        self.clock = clock

    # -- transport -------------------------------------------------------
    def request(self, method: str, path_or_url: str, params: Optional[Dict[str, str]] = None,
                body: Any = None, ok: tuple = (200,)) -> Optional[Dict[str, Any]]:
        url = path_or_url if path_or_url.startswith("https://") else f"{API_ROOT}{path_or_url}"
        if not url.startswith(API_ROOT + "/"):
            # Never send the bearer token anywhere but Apple's API host.
            raise AscError("refusing to call a non-App-Store-Connect URL")
        last: Optional[str] = None
        for attempt in range(len(RETRY_DELAYS) + 1):
            headers = {"Authorization": f"Bearer {self.token_factory()}"}
            if body is not None:
                headers["Content-Type"] = "application/json"
            try:
                resp = self.http.request(method, url, headers=headers, json=body, params=params, timeout=30)
            except Exception as exc:  # network error: retry, log class only
                last = f"{type(exc).__name__}"
                resp = None
            if resp is not None:
                if resp.status_code in ok:
                    if resp.status_code == 204 or not getattr(resp, "text", ""):
                        return None
                    return resp.json()
                if resp.status_code not in RETRY_STATUSES:
                    raise AscError(f"{method} {self._short(url)} -> HTTP {resp.status_code}: {self._errors(resp)}")
                last = f"HTTP {resp.status_code}"
            if attempt < len(RETRY_DELAYS):
                self.sleep(RETRY_DELAYS[attempt])
        raise AscError(f"{method} {self._short(url)} failed after retries ({last})")

    @staticmethod
    def _short(url: str) -> str:
        return url[len(API_ROOT):].split("?", 1)[0]

    @staticmethod
    def _errors(resp: Any) -> str:
        try:
            errors = resp.json().get("errors", [])
            parts = [f"{e.get('code', '')} {e.get('title', '')} {e.get('detail', '')}".strip() for e in errors]
            return scrub("; ".join(parts) or resp.text)
        except Exception:
            return scrub(getattr(resp, "text", ""))

    def paginate(self, path: str, params: Optional[Dict[str, str]] = None) -> Iterator[Dict[str, Any]]:
        """Yield each page's JSON, following links.next (validated by request())."""
        url: Optional[str] = path
        first = True
        for _ in range(MAX_PAGES):
            page = self.request("GET", url, params=params if first else None)
            first = False
            yield page or {}
            url = ((page or {}).get("links") or {}).get("next")
            if not url:
                return
        raise AscError(f"pagination for {self._short(path)} exceeded {MAX_PAGES} pages")

    # -- queries ---------------------------------------------------------
    def find_app(self, bundle_id: str) -> str:
        apps = [a for page in self.paginate("/v1/apps", {"filter[bundleId]": bundle_id, "limit": "200"})
                for a in page.get("data", []) if a["attributes"].get("bundleId") == bundle_id]
        if len(apps) != 1:
            raise AscError(f"expected exactly one app record for {bundle_id}, found {len(apps)}")
        return apps[0]["id"]

    def builds(self, app_id: str, build_number: Optional[str] = None) -> Iterator[Dict[str, Any]]:
        """Yield builds as dicts with version (build number), marketing, platform, state, id."""
        params = {"filter[app]": app_id, "limit": "200", "include": "preReleaseVersion",
                  "sort": "-uploadedDate"}
        if build_number is not None:
            params["filter[version]"] = build_number
        prerelease: Dict[str, Dict[str, Any]] = {}
        for page in self.paginate("/v1/builds", params):
            for inc in page.get("included", []):
                if inc.get("type") == "preReleaseVersions":
                    prerelease[inc["id"]] = inc.get("attributes", {})
            for b in page.get("data", []):
                rel = ((b.get("relationships") or {}).get("preReleaseVersion") or {}).get("data") or {}
                pre = prerelease.get(rel.get("id", ""), {})
                a = b.get("attributes", {})
                yield {
                    "id": b["id"],
                    "number": a.get("version", ""),
                    "marketing": pre.get("version"),
                    "platform": pre.get("platform"),
                    "state": a.get("processingState"),
                    "expired": bool(a.get("expired")),
                }

    def next_build_number(self, app_id: str) -> int:
        highest = 0
        for b in self.builds(app_id):
            if INTEGER_RE.fullmatch(str(b["number"])):
                highest = max(highest, int(b["number"]))
        return highest + 1

    def find_build(self, app_id: str, version: str, number: str) -> Optional[Dict[str, Any]]:
        for b in self.builds(app_id, number):
            if str(b["number"]) == number and b["marketing"] == version and b["platform"] in (None, "IOS"):
                return b
        return None

    def internal_group(self, app_id: str, name: str) -> Dict[str, Any]:
        groups = [g for page in self.paginate(f"/v1/apps/{app_id}/betaGroups", {"limit": "200"})
                  for g in page.get("data", [])
                  if g["attributes"].get("name") == name and g["attributes"].get("isInternalGroup")]
        if len(groups) != 1:
            raise AscError(f"expected exactly one internal TestFlight group named {name!r}, found {len(groups)}")
        return groups[0]

    def in_group(self, group_id: str, build_id: str) -> bool:
        for page in self.paginate(f"/v1/betaGroups/{group_id}/builds", {"limit": "200"}):
            if any(b["id"] == build_id for b in page.get("data", [])):
                return True
        return False

    def add_to_group(self, group_id: str, build_id: str) -> None:
        self.request("POST", f"/v1/betaGroups/{group_id}/relationships/builds",
                     body={"data": [{"type": "builds", "id": build_id}]}, ok=(200, 204))

    def internal_build_state(self, build_id: str) -> str:
        try:
            data = self.request("GET", f"/v1/builds/{build_id}/buildBetaDetail") or {}
            state = (data.get("data") or {}).get("attributes", {}).get("internalBuildState")
            return state if state and re.fullmatch(r"[A-Z_]{1,60}", state) else "UNKNOWN"
        except AscError as exc:
            log(f"could not read build beta detail: {exc}")
            return "UNKNOWN"

    def set_whats_new(self, build_id: str, text: str) -> None:
        existing = None
        for page in self.paginate(f"/v1/builds/{build_id}/betaBuildLocalizations", {"limit": "200"}):
            for loc in page.get("data", []):
                if loc["attributes"].get("locale") == NOTES_LOCALE:
                    existing = loc["id"]
        if existing:
            self.request("PATCH", f"/v1/betaBuildLocalizations/{existing}", body={
                "data": {"type": "betaBuildLocalizations", "id": existing, "attributes": {"whatsNew": text}}})
        else:
            self.request("POST", "/v1/betaBuildLocalizations", ok=(200, 201), body={
                "data": {"type": "betaBuildLocalizations",
                         "attributes": {"locale": NOTES_LOCALE, "whatsNew": text},
                         "relationships": {"build": {"data": {"type": "builds", "id": build_id}}}}})

    # -- workflows -------------------------------------------------------
    def distribute(self, bundle_id: str, version: str, number: str, group_name: str,
                   notes: str, wait_minutes: int, poll_seconds: int = 30,
                   group_wait_minutes: int = 10) -> int:
        app_id = self.find_app(bundle_id)
        deadline = self.clock() + wait_minutes * 60
        build: Optional[Dict[str, Any]] = None
        announced = False
        last_logged = ""
        while True:
            build = self.find_build(app_id, version, number)
            if build:
                if not announced:
                    announced = True
                    emit("asc_build_id", build["id"])
                    log(f"found build {version} ({number}) as App Store Connect build {build['id']}")
                state = build["state"] or "UNKNOWN"
                if state != last_logged:
                    last_logged = state
                    log(f"processing state: {state}")
                if build["expired"]:
                    emit("state", "EXPIRED")
                    log("build is expired")
                    return 1
                if state in ("FAILED", "INVALID"):
                    emit("state", state)
                    log(f"build processing ended in {state}")
                    return 1
                if state == "VALID":
                    break
            elif last_logged != "ABSENT":
                last_logged = "ABSENT"
                log("build not visible in App Store Connect yet")
            if self.clock() >= deadline:
                emit("state", "PENDING")
                log(f"still not VALID after {wait_minutes} minutes: PENDING. "
                    f"Rerun with mode=resume resume_build={number} later.")
                return 0
            self.sleep(poll_seconds)

        emit("state", "VALID")
        build_id = build["id"]

        beta_state = self.internal_build_state(build_id)
        emit("beta_state", beta_state)
        log(f"internal build state: {beta_state}")

        if notes:
            self.set_whats_new(build_id, notes)
            emit("whats_new", "set")
            log(f"What to Test set ({NOTES_LOCALE})")
        else:
            emit("whats_new", "skipped")

        group = self.internal_group(app_id, group_name)
        group_id = group["id"]
        member = self.in_group(group_id, build_id)
        if not member and group["attributes"].get("hasAccessToAllBuilds"):
            # Automatic distribution adds builds itself; give it time first.
            end = self.clock() + group_wait_minutes * 60
            while not member and self.clock() < end:
                self.sleep(poll_seconds)
                member = self.in_group(group_id, build_id)
        if not member:
            try:
                self.add_to_group(group_id, build_id)
            except AscError as exc:
                log(f"adding the build to the group failed: {exc}")
            member = self.in_group(group_id, build_id)
        emit("in_group", "true" if member else "false")
        log(f"build in internal group {group_name!r}: {member}")
        if not member:
            return 1
        if beta_state == "MISSING_EXPORT_COMPLIANCE":
            log("build is missing export compliance information")
            return 1
        return 0


def _require(name: str) -> str:
    value = os.environ.get(name, "")
    if not value:
        raise AscError(f"{name} is not set")
    return value


def build_client() -> Asc:
    factory = make_token_factory(_require("ASC_KEY_ID"), _require("ASC_ISSUER_ID"), _require("ASC_KEY_PATH"))
    return Asc(RequestsHttp(), factory)


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("allocate")
    dist = sub.add_parser("distribute")
    dist.add_argument("--version", required=True)
    dist.add_argument("--build", required=True)
    dist.add_argument("--group", default="Internal")
    dist.add_argument("--notes-file")
    dist.add_argument("--wait-minutes", type=int, default=60)
    args = parser.parse_args(argv)
    bundle_id = os.environ.get("ASC_BUNDLE_ID", "com.radioshake")
    if not re.fullmatch(r"[A-Za-z0-9.-]{1,155}", bundle_id):
        print("invalid ASC_BUNDLE_ID", file=sys.stderr)
        return 2
    try:
        client = build_client()
        if args.command == "allocate":
            number = client.next_build_number(client.find_app(bundle_id))
            print(number)
            return 0
        if not VERSION_RE.fullmatch(args.version) or not BUILD_RE.fullmatch(args.build) \
                or not GROUP_RE.fullmatch(args.group) or not 1 <= args.wait_minutes <= 120:
            print("invalid distribute arguments", file=sys.stderr)
            return 2
        notes = ""
        if args.notes_file:
            with open(args.notes_file, "r", encoding="utf-8") as fh:
                notes = fh.read().strip()
            if len(notes) > NOTES_MAX_CHARS:
                print("notes exceed 4000 characters", file=sys.stderr)
                return 2
        return client.distribute(bundle_id, args.version, args.build, args.group, notes, args.wait_minutes)
    except AscError as exc:
        print(f"error: {scrub(str(exc))}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
