#!/usr/bin/env python3
"""App Store listing client for the asc-listing workflow (iOS early App Review).

The repository is public, so its Actions logs are public. This script never
prints listing text, review notes, the review phone, credentials or tokens. It
prints field names, unchanged/updated/created/deleted, lengths and short
SHA-256 prefixes only; error bodies are scrubbed and truncated first.

Modes (--src is a checkout of the private source repository)
  plan       Read-only. Compare the repo's listing with App Store Connect and print
             what `apply` would change, plus the attribute names App Store
             Connect returned (names only).
  preflight  Read-only. Exit 0 only when the submission is ready; otherwise print
             the failure reasons. Lists the owner checks it cannot read.
  apply      Not available yet (a later task adds it); exits 2.

Never written by any mode: content rights, age rating, App Privacy, privacy
policy URL, price, availability, phased release, review phone, submission.

Environment: ASC_KEY_ID, ASC_ISSUER_ID, ASC_KEY_PATH (see asc_testflight.py),
ASC_BUNDLE_ID (default com.radioshake).
"""
from __future__ import annotations

import argparse
import dataclasses
import hashlib
import json
import os
import pathlib
import re
import sys
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import urljoin, urlsplit

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from asc_testflight import (  # noqa: E402
    BUILD_RE, Asc, AscError, RequestsHttp, make_token_factory, scrub,
)

EDITABLE_STATES = frozenset({"PREPARE_FOR_SUBMISSION", "DEVELOPER_REJECTED", "REJECTED", "METADATA_REJECTED"})
PAGE_HOSTS = frozenset({"radioshake.media"})
PRIVACY_URL = "https://radioshake.media/privacy"
TAKEDOWN_URL = "https://radioshake.media/stations-and-rights-holders/"
SCREENSHOTS_MIN = 6
SCREENSHOTS_MAX = 8
PAGE_TIMEOUT_SECONDS = 10
PAGE_MAX_REDIRECTS = 3
PAGE_MAX_CHARS = 2_000_000
CONTENT_RIGHTS_OK = "USES_THIRD_PARTY_CONTENT"
# Age-rating attributes that are legitimately null for this app (kids band, regional overrides and
# the free-text info URL are not questions the owner answers).
AGE_RATING_OPTIONAL = frozenset({"kidsAgeBand", "ageRatingOverride", "ageRatingOverrideV2",
                                 "koreaAgeRatingOverride", "developerAgeRatingInfoUrl"})
OWNER_CHECKS = (
    "App Privacy answers are not readable through the API: confirm them in the portal (re-confirm after 4b).",
    "EU DSA trader status (Business) is not readable through the API: confirm it in the portal.",
    "Review phone value is the owner's; only its presence is checked here.",
)
LISTING_FIELDS = ("name", "subtitle", "keywords", "promotional_text", "description", "support_url")
SHOT_NAME_RE = re.compile(r"[0-9]{2}-[A-Za-z0-9._-]+\.jpe?g")
BUNDLE_RE = re.compile(r"[A-Za-z0-9.-]{1,155}")
MARKETING_VERSION_RE = re.compile(r'^[ \t]+MARKETING_VERSION:[ \t]*"([0-9]+\.[0-9]+(\.[0-9]+)?)"[ \t]*$', re.M)
LOCALIZATION_FIELDS = (("description", "description"), ("keywords", "keywords"),
                       ("promotionalText", "promotional_text"), ("supportUrl", "support_url"))


class PageError(Exception):
    """A public page could not be read safely. The message is log-safe (no URL, no body)."""


@dataclasses.dataclass(frozen=True)
class Manifest:
    version: str
    locale: str
    primary_category: str
    copyright: str
    release_type: str
    review_first_name: str
    review_last_name: str
    review_email: str
    notes: str
    attachments: Tuple[pathlib.Path, ...]
    display_type: str
    screenshots: Tuple[pathlib.Path, ...]
    fields: Dict[str, str]


@dataclasses.dataclass(frozen=True)
class Change:
    resource: str
    field: str
    action: str  # "unchanged" | "updated" | "created" | "deleted"


def marketing_version(project_yml: str) -> str:
    found = MARKETING_VERSION_RE.findall(project_yml)
    if len(found) != 1:
        raise ValueError(f"expected exactly one quoted MARKETING_VERSION in project.yml, found {len(found)}")
    return found[0][0]


def _file(root: pathlib.Path, rel: str, what: str) -> pathlib.Path:
    path = (root / rel).resolve()
    if root.resolve() not in path.parents:
        raise ValueError(f"{what} path leaves the source tree")
    return path


def _text(path: pathlib.Path, what: str) -> str:
    try:
        return path.read_text(encoding="utf-8").strip()
    except (OSError, UnicodeDecodeError):
        raise ValueError(f"cannot read {what}") from None


def _need(data: Any, *keys: str) -> Any:
    cur = data
    for key in keys:
        if not isinstance(cur, dict) or key not in cur or cur[key] in (None, ""):
            raise ValueError(f"appstore.json is missing {'.'.join(keys)}")
        cur = cur[key]
    return cur


def load_manifest(src_root: pathlib.Path) -> Manifest:
    root = pathlib.Path(src_root)
    try:
        version = marketing_version((root / "ios" / "project.yml").read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError):
        raise ValueError("cannot read ios/project.yml") from None
    try:
        data = json.loads(_text(root / "ios" / "appstore" / "appstore.json", "ios/appstore/appstore.json"))
    except json.JSONDecodeError:
        raise ValueError("appstore.json is not valid JSON") from None
    listing_dir = _file(root, _need(data, "listingDir"), "listingDir")
    fields = {name: _text(listing_dir / f"{name}.txt", f"listing file {name}") for name in LISTING_FIELDS}
    attachments = _need(data, "review", "attachmentPaths")
    if not isinstance(attachments, list) or not attachments:
        raise ValueError("appstore.json review.attachmentPaths must be a non-empty list")
    attachment_paths = []
    for rel in attachments:
        path = _file(root, rel, "attachment")
        if not path.is_file():
            raise ValueError("a review attachment file is missing")
        attachment_paths.append(path)
    shot_dir = _file(root, _need(data, "screenshots", "dir"), "screenshots.dir")
    shots = tuple(sorted(p for p in shot_dir.iterdir() if p.is_file() and SHOT_NAME_RE.fullmatch(p.name))) \
        if shot_dir.is_dir() else ()
    return Manifest(
        version=version, locale=_need(data, "locale"), primary_category=_need(data, "primaryCategory"),
        copyright=_need(data, "copyright"), release_type=_need(data, "releaseType"),
        review_first_name=_need(data, "review", "firstName"), review_last_name=_need(data, "review", "lastName"),
        review_email=_need(data, "review", "email"),
        notes=_text(_file(root, _need(data, "review", "notesPath"), "notesPath"), "review notes"),
        attachments=tuple(attachment_paths), display_type=_need(data, "screenshots", "displayType"),
        screenshots=shots, fields=fields)


def md5_hex(path: pathlib.Path) -> str:
    return hashlib.md5(path.read_bytes()).hexdigest()


def _norm(text: Any) -> str:
    return str(text or "").replace("\r\n", "\n").strip()


def fetch_page(url: str, http: Any) -> str:
    """GET a public page: https only, host in PAGE_HOSTS, 10 s, redirects only to the same rules."""
    current = url
    for _ in range(PAGE_MAX_REDIRECTS + 1):
        parts = urlsplit(current)
        if parts.scheme != "https" or parts.hostname not in PAGE_HOSTS or parts.username or parts.password \
                or parts.port not in (None, 443):
            raise PageError("page URL is not an allowed https address")
        resp = http.get(current, timeout=PAGE_TIMEOUT_SECONDS, allow_redirects=False)
        status = resp.status_code
        if status in (301, 302, 303, 307, 308):
            location = (resp.headers or {}).get("Location")
            if not location:
                raise PageError("redirect without a location")
            current = urljoin(current, location)
            continue
        if status != 200:
            raise PageError(f"page returned HTTP {status}")
        return (resp.text or "")[:PAGE_MAX_CHARS]
    raise PageError("too many redirects")


class RequestsPlainHttp:
    """Plain requests transport for public pages (and, later, Apple's upload hosts): no credentials."""

    def __init__(self) -> None:
        import requests

        self._session = requests.Session()

    def get(self, url: str, timeout: int = 10, allow_redirects: bool = False):
        return self._session.get(url, timeout=timeout, allow_redirects=allow_redirects)

    def put(self, url: str, headers: Dict[str, str], data: bytes, timeout: int = 120):
        return self._session.put(url, headers=headers, data=data, timeout=timeout)


def _attrs(resource: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    return ((resource or {}).get("attributes")) or {}


def _delivery(resource: Dict[str, Any]) -> str:
    state = _attrs(resource).get("assetDeliveryState") or {}
    return str(state.get("state") or "UNKNOWN")


class Snapshot:
    """Everything read from App Store Connect for one app (GET only)."""

    app: Dict[str, Any]
    version: Optional[Dict[str, Any]] = None
    phased: Optional[Dict[str, Any]] = None
    loc: Optional[Dict[str, Any]] = None
    info: Optional[Dict[str, Any]] = None
    info_loc: Optional[Dict[str, Any]] = None
    category: Optional[Dict[str, Any]] = None
    age: Optional[Dict[str, Any]] = None
    detail: Optional[Dict[str, Any]] = None
    attachments: List[Dict[str, Any]]
    shot_set: Optional[Dict[str, Any]] = None
    set_types: List[str]
    shots: List[Dict[str, Any]]
    build: Optional[Dict[str, Any]] = None
    build_marketing: Optional[str] = None
    price: Optional[Dict[str, Any]] = None
    territories: Optional[List[Dict[str, Any]]] = None

    def __init__(self) -> None:
        self.attachments, self.shots, self.set_types = [], [], []


def _state(resource: Optional[Dict[str, Any]]) -> str:
    a = _attrs(resource)
    return str(a.get("appStoreState") or a.get("appVersionState") or a.get("state") or "UNKNOWN")


_ID_RE = re.compile(r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}")


def _generic(exc: AscError) -> AscError:
    """Same error with resource ids blanked so scrub() keeps the request path readable in the log."""
    return AscError(_ID_RE.sub("{id}", str(exc)))


class Listing:
    def __init__(self, asc: Asc, upload_http: Any, page_http: Any, bundle_id: str) -> None:
        self.asc = asc
        self.upload_http = upload_http
        self.page_http = page_http
        self.bundle_id = bundle_id

    # -- reads -----------------------------------------------------------
    def _one(self, path: str, params: Optional[Dict[str, str]] = None) -> Optional[Dict[str, Any]]:
        """GET a to-one relationship; absent (404 or null data) is None."""
        try:
            page = self.asc.request("GET", path, params=params) or {}
        except AscError as exc:
            if "HTTP 404" in str(exc):
                return None
            raise _generic(exc) from None
        return page.get("data") or None

    def _all(self, path: str, params: Optional[Dict[str, str]] = None) -> List[Dict[str, Any]]:
        out: List[Dict[str, Any]] = []
        try:
            for page in self.asc.paginate(path, {"limit": "200", **(params or {})}):
                out.extend(page.get("data") or [])
        except AscError as exc:
            if "HTTP 404" in str(exc):
                return []
            raise _generic(exc) from None
        return out

    def _app(self) -> Dict[str, Any]:
        apps = [x for page in self.asc.paginate("/v1/apps", {"filter[bundleId]": self.bundle_id, "limit": "200"})
                for x in page.get("data", []) if _attrs(x).get("bundleId") == self.bundle_id]
        if len(apps) != 1:
            raise AscError(f"expected exactly one app record for {self.bundle_id}, found {len(apps)}")
        return apps[0]

    def snapshot(self, m: Manifest) -> Snapshot:
        s = Snapshot()
        s.app = self._app()
        app_id = s.app["id"]
        versions = self._all(f"/v1/apps/{app_id}/appStoreVersions", {"filter[platform]": "IOS"})
        editable = [v for v in versions if _state(v) in EDITABLE_STATES]
        s.version = (editable or versions or [None])[0]
        infos = self._all(f"/v1/apps/{app_id}/appInfos")
        s.info = ([i for i in infos if _state(i) in EDITABLE_STATES] or infos or [None])[0]
        if s.version:
            vid = s.version["id"]
            s.phased = self._one(f"/v1/appStoreVersions/{vid}/appStoreVersionPhasedRelease")
            s.loc = next((x for x in self._all(f"/v1/appStoreVersions/{vid}/appStoreVersionLocalizations")
                          if _attrs(x).get("locale") == m.locale), None)
            s.detail = self._one(f"/v1/appStoreVersions/{vid}/appStoreReviewDetail")
            # Apple rejects `include` on this request (HTTP 400): read the pre-release version separately.
            s.build = self._one(f"/v1/appStoreVersions/{vid}/build")
            if s.build:
                pre = self._one(f"/v1/builds/{s.build['id']}/preReleaseVersion")
                s.build_marketing = _attrs(pre).get("version")
        if s.detail:
            s.attachments = self._all(f"/v1/appStoreReviewDetails/{s.detail['id']}/appStoreReviewAttachments")
        if s.loc:
            sets = self._all(f"/v1/appStoreVersionLocalizations/{s.loc['id']}/appScreenshotSets")
            s.set_types = sorted(str(_attrs(x).get("screenshotDisplayType")) for x in sets)
            s.shot_set = next((x for x in sets if _attrs(x).get("screenshotDisplayType") == m.display_type), None)
            if s.shot_set:
                s.shots = self._all(f"/v1/appScreenshotSets/{s.shot_set['id']}/appScreenshots")
        if s.info:
            iid = s.info["id"]
            s.info_loc = next((x for x in self._all(f"/v1/appInfos/{iid}/appInfoLocalizations")
                               if _attrs(x).get("locale") == m.locale), None)
            s.category = self._one(f"/v1/appInfos/{iid}/primaryCategory")
            s.age = self._one(f"/v1/appInfos/{iid}/ageRatingDeclaration")
        s.price = self._one(f"/v1/apps/{app_id}/appPriceSchedule")
        availability = self._one(f"/v1/apps/{app_id}/appAvailabilityV2")
        if availability:
            s.territories = self._all(f"/v1/appAvailabilities/{availability['id']}/territoryAvailabilities",
                                      {"include": "territory"})
        return s

    # -- plan ------------------------------------------------------------
    def plan(self, m: Manifest, *, build: Optional[str] = None) -> List[Change]:
        return self._plan(m, self.snapshot(m), build)

    @staticmethod
    def _cmp(changes: List[Change], resource: str, field: str, present: bool, remote: Any, local: Any) -> None:
        if not present:
            action = "created"
        else:
            action = "unchanged" if _norm(remote) == _norm(local) else "updated"
        changes.append(Change(resource, field, action))

    def _plan(self, m: Manifest, s: Snapshot, build: Optional[str]) -> List[Change]:
        out: List[Change] = []
        v = _attrs(s.version)
        for field, local in (("versionString", m.version), ("releaseType", m.release_type),
                             ("copyright", m.copyright)):
            self._cmp(out, "appStoreVersion", field, bool(s.version), v.get(field), local)
        if not s.build:
            out.append(Change("appStoreVersion", "build", "created"))
        else:
            wrong = build is not None and _attrs(s.build).get("version") != build
            out.append(Change("appStoreVersion", "build", "updated" if wrong else "unchanged"))
        loc = _attrs(s.loc)
        for field, key in LOCALIZATION_FIELDS:
            self._cmp(out, "appStoreVersionLocalization", field, bool(s.loc), loc.get(field), m.fields[key])
        info_loc = _attrs(s.info_loc)
        for field in ("name", "subtitle"):
            self._cmp(out, "appInfoLocalization", field, bool(s.info_loc), info_loc.get(field), m.fields[field])
        self._cmp(out, "appInfo", "primaryCategory", bool(s.category), (s.category or {}).get("id"), m.primary_category)
        d = _attrs(s.detail)
        for field, local in (("contactFirstName", m.review_first_name), ("contactLastName", m.review_last_name),
                             ("contactEmail", m.review_email), ("notes", m.notes)):
            self._cmp(out, "appStoreReviewDetail", field, bool(s.detail), d.get(field), local)
        self._cmp(out, "appStoreReviewDetail", "demoAccountRequired", bool(s.detail),
                  str(d.get("demoAccountRequired")), "False")
        out.extend(self._asset_changes("appStoreReviewAttachment", m.attachments, s.attachments))
        out.append(Change("appScreenshotSet", "screenshotDisplayType", "unchanged" if s.shot_set else "created"))
        out.extend(self._asset_changes("appScreenshot", m.screenshots, s.shots))
        local_names = [p.name for p in m.screenshots]
        remote_common = [_attrs(x).get("fileName") for x in s.shots if _attrs(x).get("fileName") in local_names]
        local_common = [n for n in local_names if n in remote_common]
        out.append(Change("appScreenshotSet", "order", "unchanged" if remote_common == local_common else "updated"))
        return out

    @staticmethod
    def _asset_changes(resource: str, local: Tuple[pathlib.Path, ...], remote: List[Dict[str, Any]]) -> List[Change]:
        by_name = {_attrs(x).get("fileName"): x for x in remote}
        out = []
        for path in local:
            hit = by_name.get(path.name)
            if hit is None:
                out.append(Change(resource, path.name, "created"))
            elif _attrs(hit).get("sourceFileChecksum") == md5_hex(path):
                out.append(Change(resource, path.name, "unchanged"))
            else:
                out.append(Change(resource, path.name, "updated"))
        names = {p.name for p in local}
        out.extend(Change(resource, str(name), "deleted") for name in by_name if name not in names)
        return out

    @staticmethod
    def observed_fields(s: Snapshot) -> List[str]:
        """Attribute NAMES App Store Connect returned (never values), for the first live read."""
        def names(label: str, resource: Optional[Dict[str, Any]]) -> str:
            return f"{label}: " + (", ".join(sorted(_attrs(resource))) if resource else "(absent)")
        lines = [names("app", s.app), names("appStoreVersion", s.version), names("appStoreVersionLocalization", s.loc),
                 names("appInfo", s.info), names("appInfoLocalization", s.info_loc),
                 names("appStoreReviewDetail", s.detail), names("build", s.build),
                 names("appScreenshotSet", s.shot_set),
                 names("appScreenshot", s.shots[0] if s.shots else None),
                 names("appStoreReviewAttachment", s.attachments[0] if s.attachments else None)]
        age = _attrs(s.age)
        lines.append("ageRatingDeclaration: " + (", ".join(sorted(age)) if s.age else "(absent)"))
        lines.append("ageRatingDeclaration null: " + (", ".join(sorted(k for k, x in age.items() if x is None)) or "none"))
        lines.append("screenshotDisplayTypes present: " + (", ".join(s.set_types) or "none"))
        lines.append(f"appStoreState: {_state(s.version)}; territoryAvailabilities: "
                     f"{'(absent)' if s.territories is None else len(s.territories)}")
        return lines

    # -- preflight -------------------------------------------------------
    def preflight(self, m: Manifest, *, build: Optional[str] = None) -> List[str]:
        return self._preflight(m, self.snapshot(m), build)

    def _page(self, url: str) -> Tuple[Optional[str], Optional[str]]:
        try:
            return fetch_page(url, self.page_http), None
        except PageError as exc:
            return None, str(exc)
        except Exception as exc:  # network failure: class name only
            return None, f"read failed ({type(exc).__name__})"

    def _preflight(self, m: Manifest, s: Snapshot, requested_build: Optional[str]) -> List[str]:
        why: List[str] = []
        if not s.version:
            why.append("no App Store version exists for iOS")
        else:
            v = _attrs(s.version)
            state = _state(s.version)
            if state not in EDITABLE_STATES:
                why.append(f"version state {state} is not editable ({', '.join(sorted(EDITABLE_STATES))})")
            if v.get("versionString") != m.version:
                why.append(f"version string {v.get('versionString')} differs from MARKETING_VERSION {m.version}")
            if v.get("releaseType") != "MANUAL":
                why.append(f"release type {v.get('releaseType')} is not MANUAL")
            if _norm(v.get("copyright")) != _norm(m.copyright):
                why.append("copyright differs from the repo")
            if s.phased:
                why.append("a phased release is present (the first version has none)")
            if not s.build:
                why.append("no build is attached to the version")
            else:
                b = _attrs(s.build)
                if b.get("processingState") != "VALID":
                    why.append(f"build is {b.get('processingState')}, not VALID")
                if b.get("expired"):
                    why.append("build is expired")
                if s.build_marketing != m.version or s.build_marketing != v.get("versionString"):
                    why.append(f"build version {s.build_marketing} must equal the version string "
                               f"{v.get('versionString')} and MARKETING_VERSION {m.version}")
                if requested_build is not None and b.get("version") != requested_build:
                    why.append(f"attached build {b.get('version')} is not the requested build {requested_build}")
        why.extend(self._text_reasons(m, s))
        why.extend(self._asset_reasons(m, s))
        why.extend(self._account_reasons(m, s))
        why.extend(self._page_reasons(m))
        return why

    @staticmethod
    def _text_reasons(m: Manifest, s: Snapshot) -> List[str]:
        why: List[str] = []
        if not s.loc:
            why.append(f"no {m.locale} version localization")
        else:
            loc = _attrs(s.loc)
            for field, key in LOCALIZATION_FIELDS:
                if _norm(loc.get(field)) != _norm(m.fields[key]):
                    why.append(f"{field} differs from the repo")
        if not s.info_loc:
            why.append(f"no {m.locale} app information localization")
        else:
            il = _attrs(s.info_loc)
            for field in ("name", "subtitle"):
                if _norm(il.get(field)) != _norm(m.fields[field]):
                    why.append(f"{field} differs from the repo")
            if il.get("privacyPolicyUrl") != PRIVACY_URL:
                why.append("privacy policy URL is not the expected one")
        if (s.category or {}).get("id") != m.primary_category:
            why.append(f"primary category {(s.category or {}).get('id')} is not {m.primary_category}")
        if not s.detail:
            why.append("no review detail exists")
        else:
            d = _attrs(s.detail)
            for field, local in (("contactFirstName", m.review_first_name), ("contactLastName", m.review_last_name),
                                 ("contactEmail", m.review_email)):
                if _norm(d.get(field)) != _norm(local):
                    why.append(f"{field} differs from the repo")
            if _norm(d.get("notes")) != _norm(m.notes):
                why.append("review notes differ from the repo")
            if not _norm(d.get("contactPhone")):
                why.append("review contact phone is missing (enter it in the portal)")
            if d.get("demoAccountRequired") is not False:
                why.append("demoAccountRequired must be false")
        return why

    @staticmethod
    def _asset_reasons(m: Manifest, s: Snapshot) -> List[str]:
        why: List[str] = []
        remote = {_attrs(x).get("fileName"): x for x in s.attachments}
        for path in m.attachments:
            hit = remote.get(path.name)
            if hit is None:
                why.append(f"attachment {path.name} is missing")
            elif _delivery(hit) != "COMPLETE":
                why.append(f"attachment {path.name} is {_delivery(hit)}, not COMPLETE")
            elif _attrs(hit).get("sourceFileChecksum") != md5_hex(path):
                why.append(f"attachment {path.name} differs from the repo")
        if not s.shot_set:
            why.append(f"screenshots: no {m.display_type} set exists")
            return why
        count = len(s.shots)
        if not SCREENSHOTS_MIN <= count <= SCREENSHOTS_MAX:
            why.append(f"screenshots: {count} uploaded, expected {SCREENSHOTS_MIN} to {SCREENSHOTS_MAX}")
        for x in s.shots:
            if _delivery(x) != "COMPLETE":
                why.append(f"screenshots: {_attrs(x).get('fileName')} is {_delivery(x)}, not COMPLETE")
        want = [(p.name, md5_hex(p)) for p in m.screenshots]
        have = [(_attrs(x).get("fileName"), _attrs(x).get("sourceFileChecksum")) for x in s.shots]
        if want != have:
            why.append("screenshots: uploaded set or order differs from the repo files")
        return why

    @staticmethod
    def _account_reasons(m: Manifest, s: Snapshot) -> List[str]:
        why: List[str] = []
        if _attrs(s.app).get("contentRightsDeclaration") != CONTENT_RIGHTS_OK:
            why.append(f"content rights declaration is not {CONTENT_RIGHTS_OK} (owner answers it in the portal)")
        if not s.price:
            why.append("no price schedule exists")
        if not s.age:
            why.append("age rating declaration is missing")
        else:
            null = sorted(k for k, x in _attrs(s.age).items() if x is None and k not in AGE_RATING_OPTIONAL)
            if null:
                why.append("age rating has unanswered attributes: " + ", ".join(null))
        if s.territories is None:
            why.append("no availability is set")
        else:
            by_id = {(((x.get("relationships") or {}).get("territory") or {}).get("data") or {}).get("id"):
                     bool(_attrs(x).get("available")) for x in s.territories}
            if by_id.get("CHN", False):
                why.append("CHN is available (China mainland must be excluded)")
            if not any(av for tid, av in by_id.items() if tid != "CHN"):
                why.append("no territory other than CHN is available")
        return why

    def _page_reasons(self, m: Manifest) -> List[str]:
        why: List[str] = []
        text, err = self._page(m.fields["support_url"])
        if err:
            why.append(f"support page could not be read: {err}")
        elif m.review_email not in text:
            why.append("support page does not contain the contact email")
        text, err = self._page(PRIVACY_URL)
        if err:
            why.append(f"privacy policy page could not be read: {err}")
        elif "ShazamKit" not in text:
            why.append("privacy policy page does not mention ShazamKit")
        _, err = self._page(TAKEDOWN_URL)
        if err:
            why.append(f"takedown page is not reachable: {err}")
        return why


def build_listing(bundle_id: str) -> Listing:
    def need(name: str) -> str:
        value = os.environ.get(name, "")
        if not value:
            raise AscError(f"{name} is not set")
        return value

    factory = make_token_factory(need("ASC_KEY_ID"), need("ASC_ISSUER_ID"), need("ASC_KEY_PATH"))
    return Listing(Asc(RequestsHttp(), factory), RequestsPlainHttp(), RequestsPlainHttp(), bundle_id)


def _summary(m: Manifest) -> List[str]:
    lines = []
    for name in LISTING_FIELDS:
        value = m.fields[name]
        lines.append(f"{name}: {len(value)} chars sha256:{hashlib.sha256(value.encode()).hexdigest()[:8]}")
    lines.append(f"notes: {len(m.notes)} chars sha256:{hashlib.sha256(m.notes.encode()).hexdigest()[:8]}")
    lines.append(f"version {m.version}; {len(m.screenshots)} screenshots; {len(m.attachments)} attachments")
    return lines


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("mode", choices=["plan", "preflight", "apply"])
    parser.add_argument("--src", required=True)
    parser.add_argument("--build")
    try:
        args = parser.parse_args(argv)
    except SystemExit as exc:
        return 0 if exc.code in (0, None) else 2
    if args.build is not None and not BUILD_RE.fullmatch(args.build):
        print("invalid --build", file=sys.stderr)
        return 2
    src = pathlib.Path(args.src)
    if not src.is_dir():
        print("--src is not a directory", file=sys.stderr)
        return 2
    bundle_id = os.environ.get("ASC_BUNDLE_ID", "com.radioshake")
    if not BUNDLE_RE.fullmatch(bundle_id):
        print("invalid ASC_BUNDLE_ID", file=sys.stderr)
        return 2
    if args.mode == "apply":
        print("apply is not available yet", file=sys.stderr)
        return 2
    try:
        manifest = load_manifest(src)
    except ValueError as exc:
        print(f"error: manifest: {scrub(str(exc))}", file=sys.stderr)
        return 1
    try:
        listing = build_listing(bundle_id)
        for line in _summary(manifest):
            print(line)
        snap = listing.snapshot(manifest)
        if args.mode == "plan":
            for change in listing._plan(manifest, snap, args.build):
                print(f"{change.resource}.{change.field}: {change.action}")
            print("-- attribute names returned by App Store Connect (names only)")
            for line in listing.observed_fields(snap):
                print(line)
            return 0
        reasons = listing._preflight(manifest, snap, args.build)
        for reason in reasons:
            print(f"FAIL: {scrub(reason, 400)}")
        for check in OWNER_CHECKS:
            print(f"OWNER CHECK: {check}")
        print("preflight: ready" if not reasons else f"preflight: {len(reasons)} problem(s)")
        return 0 if not reasons else 1
    except AscError as exc:
        print(f"error: {scrub(str(exc))}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
