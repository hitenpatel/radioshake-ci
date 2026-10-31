"""Offline tests for asc_listing.py (read-only half: manifest, plan, preflight, page reads).
Run: python3 -m unittest discover -s .github/scripts

The repository is public: every text below is fake. Nothing here may echo a real listing."""
import contextlib
import hashlib
import io
import json
import os
import pathlib
import re
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(__file__))
import asc_listing as a  # noqa: E402
import asc_testflight as t  # noqa: E402

APP = "https://api.appstoreconnect.apple.com"
NOTES = "FAKE-NOTES-BODY-7731 first reviewer line"
DESCRIPTION = "FAKE-DESCRIPTION-BODY-5520 long text"
PHONE = "+00 0000 000000"
EMAIL = "contact@radioshake.media"
SHOTS = [f"{n:02d}-shot{n}.jpg" for n in range(1, 7)]


def md5(data: bytes) -> str:
    return hashlib.md5(data).hexdigest()


class Resp:
    def __init__(self, status=200, body=None):
        self.status_code = status
        self._body = body
        self.text = "" if body is None else "x"

    def json(self):
        return self._body


class FakeHttp:
    """Apple API fake: routes (method, path) to a body; records every call."""

    def __init__(self):
        self.routes = {}
        self.calls = []

    def request(self, method, url, headers, json=None, params=None, timeout=30):
        assert headers["Authorization"].startswith("Bearer ")
        path = url[len(APP):]
        self.calls.append((method, path, params, json))
        if path.endswith("/build") and params and "include" in params:
            return Resp(400, {"errors": [{"code": "PARAMETER_ERROR.ILLEGAL", "title": "x", "detail": "include"}]})
        if (method, path) not in self.routes:
            return Resp(404, {"errors": [{"code": "NOT_FOUND", "title": "nope", "detail": path}]})
        return Resp(200, self.routes[(method, path)])


class LiveHttp(FakeHttp):
    """Stateful Apple API fake: GETs read the world as it is now, writes mutate it."""

    def __init__(self, world):
        super().__init__()
        self.world = world
        self.errors = {}  # (method, path) -> (status, body)

    def request(self, method, url, headers, json=None, params=None, timeout=30):
        assert headers["Authorization"].startswith("Bearer ")
        assert url.startswith(APP + "/"), "the bearer token went to a non-Apple URL"
        path = url[len(APP):]
        self.calls.append((method, path, params, json))
        if (method, path) in self.errors:
            status, body = self.errors[(method, path)]
            return Resp(status, body)
        if method == "GET":
            routes = self.world.routes()
            if path.endswith("/build") and params and "include" in params:
                return Resp(400, {"errors": [{"code": "PARAMETER_ERROR.ILLEGAL", "title": "x", "detail": "include"}]})
            polled = self.world.poll_asset(path)
            if polled is not None:
                return Resp(200, {"data": polled})
            if ("GET", path) in routes:
                return Resp(200, routes[("GET", path)])
            return Resp(404, {"errors": [{"code": "NOT_FOUND", "title": "nope", "detail": path}]})
        status, body = self.world.write(method, path, json)
        return Resp(status, body)


class FakeUpload:
    """Apple's upload hosts: records every PUT; never sees an Authorization header."""

    def __init__(self):
        self.calls = []  # (url, headers, data)
        self.status = 200

    def put(self, url, headers, data, timeout=120):
        self.calls.append((url, dict(headers), bytes(data)))
        return Resp(self.status, None)


class Page:
    def __init__(self, status=200, text="", location=None):
        self.status_code = status
        self.text = text
        self.headers = {"Location": location} if location else {}


class FakePages:
    def __init__(self, pages):
        self.pages = pages
        self.calls = []

    def get(self, url, timeout=None, allow_redirects=True):
        self.calls.append((url, timeout, allow_redirects))
        return self.pages[url]


def write_tree(root: pathlib.Path, version: str = "1.0.0", shots: int = 6) -> None:
    (root / "ios" / "appstore" / "listing" / "en-GB").mkdir(parents=True)
    (root / "docs" / "app-review").mkdir(parents=True)
    (root / "marketing" / "shots").mkdir(parents=True)
    (root / "ios" / "project.yml").write_text(
        f'settings:\n  base:\n    MARKETING_VERSION: "{version}"\n', encoding="utf-8")
    (root / "ios" / "appstore" / "appstore.json").write_text(json.dumps({
        "locale": "en-GB", "primaryCategory": "MUSIC", "copyright": "2026 Fake Person",
        "releaseType": "MANUAL",
        "review": {"firstName": "Fa", "lastName": "Ke", "email": EMAIL,
                   "notesPath": "docs/app-review/ios-review-notes.txt",
                   "attachmentPaths": ["docs/app-review/pack.pdf"]},
        "screenshots": {"displayType": "APP_IPHONE_67", "dir": "marketing/shots",
                        "width": 1320, "height": 2868, "min": 6, "max": 8},
        "storeNameExceptions": [], "listingDir": "ios/appstore/listing/en-GB"}), encoding="utf-8")
    listing = root / "ios" / "appstore" / "listing" / "en-GB"
    for name, text in {"name": "Fake Name", "subtitle": "Fake subtitle", "keywords": "fake,words",
                       "promotional_text": "Fake promo", "description": DESCRIPTION,
                       "support_url": "https://radioshake.media/support/"}.items():
        (listing / f"{name}.txt").write_text(text + "\n", encoding="utf-8")
    (root / "docs" / "app-review" / "ios-review-notes.txt").write_text(NOTES + "\n", encoding="utf-8")
    (root / "docs" / "app-review" / "pack.pdf").write_bytes(b"%PDF fake pack")
    for n in range(1, shots + 1):
        (root / "marketing" / "shots" / f"{n:02d}-shot{n}.jpg").write_bytes(b"jpeg-%d" % n)


def attrs_age(**over):
    base = {k: "NONE" for k in ("alcoholTobaccoOrDrugUseOrReferences", "contests", "gamblingSimulated",
                                "gunsOrOtherWeapons", "medicalOrTreatmentInformation",
                                "profanityOrCrudeHumor", "sexualContentGraphicAndNudity",
                                "sexualContentOrNudity", "horrorOrFearThemes",
                                "matureOrSuggestiveThemes", "violenceCartoonOrFantasy",
                                "violenceRealistic", "violenceRealisticProlongedGraphicOrSadistic")}
    base.update({"gambling": False, "unrestrictedWebAccess": False, "lootBox": False,
                 "kidsAgeBand": None, "ageRatingOverride": None, "koreaAgeRatingOverride": None,
                 "ageRatingOverrideV2": None, "developerAgeRatingInfoUrl": None,
                 "gracRatingClassificationNumber": None, "socialMedia": False,
                 "socialMediaAgeRestricted": None})
    base.update(over)
    return base


def cf_obfuscated(prefix: str, email: str, key: int = 0x75) -> str:
    """Page text as Cloudflare Email Address Obfuscation serves it: the address only as data-cfemail hex."""
    token = "%02x" % key + "".join("%02x" % (ord(ch) ^ key) for ch in email)
    return (f'<p>{prefix}</p><a href="/cdn-cgi/l/email-protection#{token}"><span class="__cf_email__" '
            f'data-cfemail="{token}">[email&#160;protected]</span></a>')


class World:
    """A fully ready App Store Connect account for one manifest; tests mutate pieces."""

    def __init__(self, m: "a.Manifest"):
        self.m = m
        self.app = {"id": "app1", "attributes": {"bundleId": "com.radioshake",
                                                 "contentRightsDeclaration": "USES_THIRD_PARTY_CONTENT"}}
        self.version = {"id": "v1", "attributes": {"versionString": m.version, "appStoreState": "PREPARE_FOR_SUBMISSION",
                                                   "releaseType": "MANUAL", "copyright": m.copyright, "platform": "IOS"}}
        self.phased = None
        f = m.fields
        self.loc = {"id": "l1", "attributes": {"locale": m.locale, "description": f["description"],
                                               "keywords": f["keywords"], "promotionalText": f["promotional_text"],
                                               "supportUrl": f["support_url"]}}
        self.info = {"id": "i1", "attributes": {"appStoreState": "PREPARE_FOR_SUBMISSION"}}
        self.info_loc = {"id": "il1", "attributes": {"locale": m.locale, "name": f["name"], "subtitle": f["subtitle"],
                                                     "privacyPolicyUrl": a.PRIVACY_URL}}
        self.category = {"id": m.primary_category}
        self.age = {"id": "age1", "attributes": attrs_age()}
        self.detail = {"id": "rd1", "attributes": {"contactFirstName": m.review_first_name,
                                                   "contactLastName": m.review_last_name,
                                                   "contactEmail": m.review_email, "contactPhone": PHONE,
                                                   "demoAccountRequired": False, "notes": m.notes}}
        self.attachments = [{"id": f"at{i}", "attributes": {
            "fileName": p.name, "sourceFileChecksum": md5(p.read_bytes()),
            "assetDeliveryState": {"state": "COMPLETE"}}} for i, p in enumerate(m.attachments)]
        self.set = {"id": "s1", "attributes": {"screenshotDisplayType": m.display_type}}
        self.shots = [{"id": f"sc{i}", "attributes": {
            "fileName": p.name, "sourceFileChecksum": md5(p.read_bytes()),
            "assetDeliveryState": {"state": "COMPLETE"}}} for i, p in enumerate(m.screenshots)]
        self.build = {"id": "b1", "attributes": {"version": "7", "processingState": "VALID", "expired": False}}
        self.pre = {"type": "preReleaseVersions", "id": "p1", "attributes": {"version": m.version, "platform": "IOS"}}
        self.price = {"id": "price1"}
        self.terr = [{"id": "ta1", "attributes": {"available": True},
                      "relationships": {"territory": {"data": {"type": "territories", "id": "GBR"}}}},
                     {"id": "ta2", "attributes": {"available": False},
                      "relationships": {"territory": {"data": {"type": "territories", "id": "CHN"}}}}]
        self.catalog = [
            {"id": "b1", "number": "7", "state": "VALID", "expired": False, "marketing": m.version},
            {"id": "b2", "number": "8", "state": "VALID", "expired": False, "marketing": m.version}]
        self.unexpected = []
        self.counter = 0
        self.fail_delivery = set()
        self.processing_polls = 0
        self.remaining = {}
        self.upload = FakeUpload()
        self.pages = {
            m.fields["support_url"]: Page(200, f"Write to {EMAIL} for help"),
            a.PRIVACY_URL: Page(200, "We use ShazamKit to identify songs."),
            a.TAKEDOWN_URL: Page(200, "Rights holders: contact us"),
        }

    def builds_body(self):
        return {"data": [{"id": b["id"], "attributes": {"version": b["number"], "processingState": b["state"],
                                                        "expired": b["expired"]},
                          "relationships": {"preReleaseVersion": {"data": {"type": "preReleaseVersions",
                                                                           "id": "p-" + b["id"]}}}}
                         for b in self.catalog],
                "included": [{"type": "preReleaseVersions", "id": "p-" + b["id"],
                              "attributes": {"version": b["marketing"], "platform": "IOS"}} for b in self.catalog]}

    def routes(self):
        r = {}
        r[("GET", "/v1/apps")] = {"data": [self.app]}
        r[("GET", "/v1/apps/app1/appStoreVersions")] = {"data": [self.version] if self.version else []}
        r[("GET", "/v1/appStoreVersions/v1/appStoreVersionPhasedRelease")] = {"data": self.phased}
        r[("GET", "/v1/appStoreVersions/v1/appStoreVersionLocalizations")] = {"data": [self.loc] if self.loc else []}
        r[("GET", "/v1/apps/app1/appInfos")] = {"data": [self.info]}
        r[("GET", "/v1/appInfos/i1/appInfoLocalizations")] = {"data": [self.info_loc] if self.info_loc else []}
        r[("GET", "/v1/appInfos/i1/primaryCategory")] = {"data": self.category}
        r[("GET", "/v1/appInfos/i1/ageRatingDeclaration")] = {"data": self.age}
        r[("GET", "/v1/appStoreVersions/v1/appStoreReviewDetail")] = {"data": self.detail}
        r[("GET", "/v1/appStoreReviewDetails/rd1/appStoreReviewAttachments")] = {"data": self.attachments}
        r[("GET", "/v1/appStoreVersionLocalizations/l1/appScreenshotSets")] = {"data": [self.set] if self.set else []}
        r[("GET", "/v1/appScreenshotSets/s1/appScreenshots")] = {"data": self.shots}
        r[("GET", "/v1/appStoreVersions/v1/build")] = {"data": self.build}
        if self.build:
            r[("GET", f"/v1/builds/{self.build['id']}/preReleaseVersion")] = {"data": self.pre}
        r[("GET", "/v1/builds")] = self.builds_body()
        r[("GET", "/v1/apps/app1/appPriceSchedule")] = {"data": self.price}
        r[("GET", "/v1/apps/app1/appAvailabilityV2")] = {"data": {"id": "av1"}}
        r[("GET", "/v2/appAvailabilities/av1/territoryAvailabilities")] = {"data": self.terr}
        return r

    def http(self) -> FakeHttp:
        h = FakeHttp()
        h.routes = self.routes()
        return h

    def listing(self):
        return a.Listing(t.Asc(self.http(), lambda: "tok", sleep=lambda s: None), mock.Mock(),
                         FakePages(self.pages), "com.radioshake")

    def live_listing(self, upload=None, factory=None):
        self.upload = upload or FakeUpload()
        return a.Listing(t.Asc(LiveHttp(self), factory or (lambda: "tok"), sleep=lambda s: None),
                         self.upload, FakePages(self.pages), "com.radioshake")

    def _reserve(self, bucket, attrs, rel_name):
        self.counter += 1
        rid = f"n{self.counter}"
        size = attrs["fileSize"]
        cuts = [(0, size // 2), (size // 2, size - size // 2)] if size >= 2 else [(0, size)]
        ops = [{"method": "PUT", "url": f"https://store.apple.com/up/{rid}/{i}", "offset": off, "length": ln,
                "requestHeaders": [{"name": "Content-Type", "value": "application/octet-stream"}]}
               for i, (off, ln) in enumerate(cuts)]
        res = {"id": rid, "attributes": {"fileName": attrs["fileName"], "fileSize": size, "uploadOperations": ops,
                                         "assetDeliveryState": {"state": "AWAITING_UPLOAD"}}}
        bucket.append(res)
        return 201, {"data": res}

    def _find_asset(self, rid):
        return next((x for x in self.attachments + self.shots if x["id"] == rid), None)

    def _commit(self, rid, attrs):
        res = self._find_asset(rid)
        if res is None:
            return 404, {"errors": [{"code": "NOT_FOUND", "title": "x", "detail": rid}]}
        res["attributes"]["sourceFileChecksum"] = attrs.get("sourceFileChecksum")
        parts = sorted((u for u in self.upload.calls if f"/up/{rid}/" in u[0]), key=lambda c: c[0])
        got = md5(b"".join(c[2] for c in parts))
        if res["attributes"]["fileName"] in self.fail_delivery or got != attrs.get("sourceFileChecksum"):
            res["attributes"]["assetDeliveryState"] = {"state": "FAILED", "errors": [
                {"code": "IMAGE_TOO_SMALL", "description": "SECRETISH-" + "A" * 90 + " description"}]}
        elif self.processing_polls:
            res["attributes"]["assetDeliveryState"] = {"state": "UPLOAD_COMPLETE"}
            self.remaining[rid] = self.processing_polls
        else:
            res["attributes"]["assetDeliveryState"] = {"state": "COMPLETE"}
        return 200, {"data": res}

    def poll_asset(self, path):
        hit = re.fullmatch(r"/v1/(?:appScreenshots|appStoreReviewAttachments)/([^/]+)", path)
        res = self._find_asset(hit.group(1)) if hit else None
        if res is None:
            return None
        rid = res["id"]
        if self.remaining.get(rid, 0) > 0:
            self.remaining[rid] -= 1
            if self.remaining[rid] == 0:
                res["attributes"]["assetDeliveryState"] = {"state": "COMPLETE"}
        return res

    def write(self, method, path, body):
        """Apply one write the way App Store Connect would; returns (status, body)."""
        data = (body or {}).get("data")
        attrs = dict(data.get("attributes") or {}) if isinstance(data, dict) else {}
        rels = (data.get("relationships") or {}) if isinstance(data, dict) else {}
        if (method, path) == ("POST", "/v1/appStoreVersions"):
            self.version = {"id": "v1", "attributes": {**attrs, "appStoreState": "PREPARE_FOR_SUBMISSION"}}
            self.phased = self.loc = self.detail = self.set = self.build = None
            self.attachments, self.shots = [], []
            return 201, {"data": self.version}
        if (method, path) == ("PATCH", "/v1/appStoreVersions/v1"):
            self.version["attributes"].update(attrs)
            return 200, {"data": self.version}
        if (method, path) == ("PATCH", "/v1/appStoreVersions/v1/relationships/build"):
            entry = next(b for b in self.catalog if b["id"] == data["id"])
            self.build = {"id": entry["id"], "attributes": {"version": entry["number"],
                                                            "processingState": entry["state"],
                                                            "expired": entry["expired"]}}
            self.pre = {"type": "preReleaseVersions", "id": "p-" + entry["id"],
                        "attributes": {"version": entry["marketing"], "platform": "IOS"}}
            return 204, None
        if (method, path) == ("POST", "/v1/appStoreVersionLocalizations"):
            self.loc = {"id": "l1", "attributes": attrs}
            return 201, {"data": self.loc}
        if (method, path) == ("PATCH", "/v1/appStoreVersionLocalizations/l1"):
            self.loc["attributes"].update(attrs)
            return 200, {"data": self.loc}
        if (method, path) == ("POST", "/v1/appInfoLocalizations"):
            self.info_loc = {"id": "il1", "attributes": attrs}
            return 201, {"data": self.info_loc}
        if (method, path) == ("PATCH", "/v1/appInfoLocalizations/il1"):
            self.info_loc["attributes"].update(attrs)
            return 200, {"data": self.info_loc}
        if (method, path) == ("PATCH", "/v1/appInfos/i1"):
            self.category = {"id": rels["primaryCategory"]["data"]["id"]}
            return 200, {"data": self.info}
        if (method, path) == ("POST", "/v1/appStoreReviewDetails"):
            self.detail = {"id": "rd1", "attributes": attrs}
            return 201, {"data": self.detail}
        if (method, path) == ("PATCH", "/v1/appStoreReviewDetails/rd1"):
            self.detail["attributes"].update(attrs)
            return 200, {"data": self.detail}
        if (method, path) == ("POST", "/v1/appStoreReviewAttachments"):
            return self._reserve(self.attachments, attrs, "appStoreReviewDetail")
        if (method, path) == ("POST", "/v1/appScreenshots"):
            return self._reserve(self.shots, attrs, "appScreenshotSet")
        if (method, path) == ("POST", "/v1/appScreenshotSets"):
            self.set = {"id": "s1", "attributes": attrs}
            return 201, {"data": self.set}
        hit = re.fullmatch(r"/v1/(appStoreReviewAttachments|appScreenshots)/([^/]+)", path)
        if hit and method == "PATCH":
            return self._commit(hit.group(2), attrs)
        if hit and method == "DELETE":
            bucket = self.attachments if hit.group(1) == "appStoreReviewAttachments" else self.shots
            bucket[:] = [x for x in bucket if x["id"] != hit.group(2)]
            return 204, None
        if (method, path) == ("PATCH", "/v1/appScreenshotSets/s1/relationships/appScreenshots"):
            order = [item["id"] for item in data]
            self.shots.sort(key=lambda x: order.index(x["id"]))
            return 204, None
        self.unexpected.append((method, path))
        return 400, {"errors": [{"code": "UNEXPECTED_WRITE", "title": "unexpected", "detail": path}]}


class Tree(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = pathlib.Path(self._tmp.name)
        write_tree(self.root)
        self.m = a.load_manifest(self.root)
        self.w = World(self.m)

    def reasons(self, **kw):
        return self.w.listing().preflight(self.m, **kw)

    def diverge_text(self):
        """The text, review detail, category and build are wrong or missing (so are their child assets)."""
        self.w.version["attributes"].update({"versionString": "1.0", "releaseType": "AFTER_APPROVAL",
                                             "copyright": "old"})
        self.w.loc = None
        self.w.set, self.w.shots = None, []  # children of a missing localization or detail do not exist either
        self.w.info_loc = None
        self.w.category = {"id": "GAMES"}
        self.w.detail = None
        self.w.attachments = []
        self.w.build = None

    def assertReason(self, fragment, **kw):
        reasons = self.reasons(**kw)
        self.assertTrue(any(fragment in r for r in reasons), f"{fragment!r} not in {reasons}")


class ManifestTests(Tree):
    def test_marketing_version_reads_the_single_quoted_value(self):
        self.assertEqual(a.marketing_version('  MARKETING_VERSION: "1.2.3"\n'), "1.2.3")
        self.assertEqual(a.marketing_version('  MARKETING_VERSION: "1.2"\n'), "1.2")

    def test_marketing_version_rejects_missing_malformed_and_duplicate(self):
        for bad in ["", "other: 1\n", "  MARKETING_VERSION: 1.0.0\n", '  MARKETING_VERSION: "v1.0"\n',
                    '  MARKETING_VERSION: "1.0.0"\n  MARKETING_VERSION: "1.0.1"\n']:
            with self.assertRaises(ValueError, msg=repr(bad)):
                a.marketing_version(bad)

    def test_load_manifest_values(self):
        m = self.m
        self.assertEqual((m.version, m.locale, m.primary_category, m.copyright, m.release_type),
                         ("1.0.0", "en-GB", "MUSIC", "2026 Fake Person", "MANUAL"))
        self.assertEqual((m.review_first_name, m.review_last_name, m.review_email), ("Fa", "Ke", EMAIL))
        self.assertEqual(m.notes, NOTES)
        self.assertEqual(m.fields["description"], DESCRIPTION)
        self.assertEqual(sorted(m.fields), ["description", "keywords", "name", "promotional_text",
                                            "subtitle", "support_url"])
        self.assertEqual([p.name for p in m.screenshots], SHOTS)
        self.assertEqual([p.name for p in m.attachments], ["pack.pdf"])
        self.assertEqual(m.display_type, "APP_IPHONE_67")

    def test_missing_project_yml_fails(self):
        (self.root / "ios" / "project.yml").unlink()
        with self.assertRaises(ValueError):
            a.load_manifest(self.root)

    def test_malformed_version_fails(self):
        (self.root / "ios" / "project.yml").write_text("MARKETING_VERSION: 1\n", encoding="utf-8")
        with self.assertRaises(ValueError):
            a.load_manifest(self.root)

    def test_missing_manifest_key_or_file_fails(self):
        p = self.root / "ios" / "appstore" / "appstore.json"
        data = json.loads(p.read_text())
        del data["review"]["email"]
        p.write_text(json.dumps(data))
        with self.assertRaises(ValueError):
            a.load_manifest(self.root)
        (self.root / "docs" / "app-review" / "ios-review-notes.txt").unlink()
        with self.assertRaises(ValueError):
            a.load_manifest(self.root)

    def test_absent_screenshot_dir_gives_empty_tuple(self):
        import shutil
        shutil.rmtree(self.root / "marketing" / "shots")
        self.assertEqual(a.load_manifest(self.root).screenshots, ())


class FetchPageTests(unittest.TestCase):
    URL = "https://radioshake.media/privacy"

    def test_returns_text_with_timeout_and_no_auto_redirect(self):
        pages = FakePages({self.URL: Page(200, "body")})
        self.assertEqual(a.fetch_page(self.URL, pages), "body")
        self.assertEqual(pages.calls, [(self.URL, 10, False)])

    def test_refuses_http_other_hosts_userinfo_and_ports(self):
        for bad in ["http://radioshake.media/privacy", "https://example.com/x",
                    "https://radioshake.media.evil.example/x", "https://radioshake.media@evil.example/x",
                    "https://radioshake.media:8443/x", "ftp://radioshake.media/x", "//radioshake.media/x"]:
            pages = FakePages({})
            with self.assertRaises(a.PageError, msg=bad):
                a.fetch_page(bad, pages)
            self.assertEqual(pages.calls, [], bad)

    def test_off_host_and_downgrade_redirects_refused(self):
        for location in ["https://example.com/x", "http://radioshake.media/x"]:
            pages = FakePages({self.URL: Page(301, "", location)})
            with self.assertRaises(a.PageError, msg=location):
                a.fetch_page(self.URL, pages)
            self.assertEqual(len(pages.calls), 1)

    def test_same_host_redirect_is_followed_and_loops_are_bounded(self):
        pages = FakePages({self.URL: Page(301, "", "/privacy/"),
                           "https://radioshake.media/privacy/": Page(200, "ok")})
        self.assertEqual(a.fetch_page(self.URL, pages), "ok")
        loop = FakePages({self.URL: Page(302, "", self.URL)})
        with self.assertRaises(a.PageError):
            a.fetch_page(self.URL, loop)

    def test_non_200_raises(self):
        with self.assertRaises(a.PageError):
            a.fetch_page(self.URL, FakePages({self.URL: Page(404, "gone")}))


class PlanTests(Tree):
    def plan(self):
        lst = self.w.listing()
        changes = lst.plan(self.m)
        http = lst.asc.http
        self.assertTrue(http.calls and all(c[0] == "GET" for c in http.calls), "plan must be GET-only")
        return {(c.resource, c.field): c.action for c in changes}

    def test_everything_equal_is_unchanged(self):
        plan = self.plan()
        self.assertTrue(plan)
        self.assertEqual(set(plan.values()), {"unchanged"}, plan)
        for key in [("appStoreVersion", "versionString"), ("appStoreVersionLocalization", "description"),
                    ("appInfoLocalization", "name"), ("appStoreReviewDetail", "notes"),
                    ("appScreenshot", "01-shot1.jpg")]:
            self.assertIn(key, plan)

    def test_missing_localization_is_created(self):
        self.w.loc = None
        self.w.set = None
        plan = self.plan()
        self.assertEqual(plan[("appStoreVersionLocalization", "description")], "created")
        self.assertEqual(plan[("appStoreVersionLocalization", "supportUrl")], "created")
        self.assertEqual(plan[("appScreenshot", "01-shot1.jpg")], "created")

    def test_differing_text_is_updated(self):
        self.w.loc["attributes"]["keywords"] = "other"
        self.w.version["attributes"]["versionString"] = "1.0"
        plan = self.plan()
        self.assertEqual(plan[("appStoreVersionLocalization", "keywords")], "updated")
        self.assertEqual(plan[("appStoreVersion", "versionString")], "updated")
        self.assertEqual(plan[("appStoreVersionLocalization", "description")], "unchanged")

    def test_no_version_at_all_is_created(self):
        self.w.version = None
        self.assertEqual(self.plan()[("appStoreVersion", "versionString")], "created")

    def test_screenshot_checksum_change_and_extras(self):
        self.w.shots[2]["attributes"]["sourceFileChecksum"] = "0" * 32
        self.w.shots.append({"id": "x", "attributes": {"fileName": "09-old.jpg", "sourceFileChecksum": "1" * 32,
                                                       "assetDeliveryState": {"state": "COMPLETE"}}})
        plan = self.plan()
        self.assertEqual(plan[("appScreenshot", "03-shot3.jpg")], "updated")
        self.assertEqual(plan[("appScreenshot", "09-old.jpg")], "deleted")
        self.assertEqual(plan[("appScreenshot", "01-shot1.jpg")], "unchanged")

    def test_attachment_states(self):
        self.w.attachments[0]["attributes"]["sourceFileChecksum"] = "0" * 32
        self.assertEqual(self.plan()[("appStoreReviewAttachment", "pack.pdf")], "updated")
        self.w.attachments.clear()
        self.assertEqual(self.plan()[("appStoreReviewAttachment", "pack.pdf")], "created")

    def test_missing_review_detail_is_created(self):
        self.w.detail = None
        plan = self.plan()
        self.assertEqual(plan[("appStoreReviewDetail", "notes")], "created")


class PreflightTests(Tree):
    def test_ready_world_has_no_reasons(self):
        self.assertEqual(self.reasons(), [])

    def test_every_editable_state_is_accepted_and_others_refused(self):
        for state in a.EDITABLE_STATES:
            self.w.version["attributes"]["appStoreState"] = state
            self.assertEqual(self.reasons(), [], state)
        for state in ["WAITING_FOR_REVIEW", "IN_REVIEW", "PENDING_DEVELOPER_RELEASE", "READY_FOR_SALE",
                      "PENDING_APPLE_RELEASE", "PROCESSING_FOR_APP_STORE"]:
            self.w.version["attributes"]["appStoreState"] = state
            self.assertReason("state")

    def test_editable_state_set_is_the_spec_set(self):
        self.assertEqual(a.EDITABLE_STATES, frozenset({"PREPARE_FOR_SUBMISSION", "DEVELOPER_REJECTED",
                                                       "REJECTED", "METADATA_REJECTED"}))
        self.assertEqual(a.PAGE_HOSTS, frozenset({"radioshake.media"}))

    def test_no_version_at_all(self):
        self.w.version = None
        self.assertReason("no App Store version")

    def test_version_string_must_equal_build_and_marketing_version(self):
        self.w.version["attributes"]["versionString"] = "1.0"
        self.assertReason("version")
        self.setUp()
        self.w.pre["attributes"]["version"] = "1.0.1"
        self.assertReason("build")
        self.setUp()
        (self.root / "ios" / "project.yml").write_text('  MARKETING_VERSION: "1.0.1"\n', encoding="utf-8")
        self.m = a.load_manifest(self.root)
        self.assertReason("version")

    def test_build_must_be_attached_valid_and_unexpired(self):
        self.w.build = None
        self.assertReason("no build")
        self.setUp()
        self.w.build["attributes"]["processingState"] = "PROCESSING"
        self.assertReason("VALID")
        self.setUp()
        self.w.build["attributes"]["expired"] = True
        self.assertReason("expired")

    def test_build_read_never_sends_include_which_apple_rejects(self):
        lst = self.w.listing()
        lst.preflight(self.m)
        for method, path, params, _ in lst.asc.http.calls:
            if path.endswith("/build"):
                self.assertFalse(params, "GET .../build rejects include (HTTP 400 on the first live read)")

    def test_requested_build_number_must_match(self):
        self.assertEqual(self.reasons(build="7"), [])
        self.assertReason("requested build", build="8")

    def test_release_type_must_be_manual(self):
        self.w.version["attributes"]["releaseType"] = "AFTER_APPROVAL"
        self.assertReason("MANUAL")

    def test_phased_release_must_be_absent(self):
        self.w.phased = {"id": "ph1", "attributes": {"phasedReleaseState": "INACTIVE"}}
        self.assertReason("phased")

    def test_null_age_rating_attribute(self):
        self.w.age["attributes"]["profanityOrCrudeHumor"] = None
        self.assertReason("age rating")

    def test_optional_age_rating_attributes_may_be_null(self):
        self.assertIsNone(self.w.age["attributes"]["kidsAgeBand"])
        self.assertEqual(self.reasons(), [])

    def test_live_optional_attributes_and_the_social_media_follow_up(self):
        # Names seen on the first live read: these are null when unanswered and not owner questions.
        self.assertEqual(self.reasons(), [])
        self.w.age["attributes"]["socialMedia"] = True
        self.assertReason("socialMediaAgeRestricted")
        self.w.age["attributes"]["socialMediaAgeRestricted"] = False
        self.assertEqual(self.reasons(), [])
        self.w.age["attributes"]["socialMedia"] = None
        self.assertReason("socialMedia")

    def test_china_must_be_unavailable_and_something_available(self):
        self.w.terr[1]["attributes"]["available"] = True
        self.assertReason("CHN")
        self.setUp()
        self.w.terr[0]["attributes"]["available"] = False
        self.assertReason("territor")

    def test_territories_are_read_from_the_v2_endpoint_with_territory_included(self):
        lst = self.w.listing()
        self.assertEqual(lst.preflight(self.m), [])
        reads = [c for c in lst.asc.http.calls if "territoryAvailabilities" in c[1]]
        self.assertEqual([c[1] for c in reads], ["/v2/appAvailabilities/av1/territoryAvailabilities"])
        self.assertEqual(reads[0][2].get("include"), "territory")

    def test_a_missing_list_endpoint_fails_loudly_instead_of_reading_as_empty(self):
        for path in ("/v2/appAvailabilities/av1/territoryAvailabilities",
                     "/v1/appStoreVersions/v1/appStoreVersionLocalizations",
                     "/v1/appStoreReviewDetails/rd1/appStoreReviewAttachments",
                     "/v1/appScreenshotSets/s1/appScreenshots"):
            with self.subTest(path=path):
                h = self.w.http()
                del h.routes[("GET", path)]
                lst = a.Listing(t.Asc(h, lambda: "tok", sleep=lambda s: None), mock.Mock(),
                                FakePages(self.w.pages), "com.radioshake")
                with self.assertRaises(t.AscError):
                    lst.preflight(self.m)
                with self.assertRaises(t.AscError):
                    lst.plan(self.m)

    def test_a_missing_to_one_relationship_still_reads_as_absent(self):
        h = self.w.http()
        del h.routes[("GET", "/v1/appStoreVersions/v1/appStoreVersionPhasedRelease")]
        lst = a.Listing(t.Asc(h, lambda: "tok", sleep=lambda s: None), mock.Mock(),
                        FakePages(self.w.pages), "com.radioshake")
        self.assertEqual(lst.preflight(self.m), [])

    def test_support_email_obfuscated_by_cloudflare_is_decoded(self):
        self.w.pages[self.m.fields["support_url"]] = Page(200, cf_obfuscated("Write to", EMAIL))
        self.assertNotIn(EMAIL, self.w.pages[self.m.fields["support_url"]].text)
        self.assertEqual(self.reasons(), [])

    def test_obfuscated_other_address_is_not_the_contact_email(self):
        self.w.pages[self.m.fields["support_url"]] = Page(200, cf_obfuscated("Write to x", "other@example.com"))
        self.assertReason("support page")

    def test_malformed_cfemail_token_is_ignored_not_a_crash(self):
        page = '<a data-cfemail="zz"></a><a data-cfemail="7"></a><a data-cfemail=""></a>'
        self.w.pages[self.m.fields["support_url"]] = Page(200, page)
        self.assertReason("support page")

    def test_page_reads(self):
        self.w.pages[self.m.fields["support_url"]] = Page(200, "no mail here")
        self.assertReason("support page")
        self.setUp()
        self.w.pages[a.PRIVACY_URL] = Page(200, "no recognition mention")
        self.assertReason("ShazamKit")
        self.setUp()
        self.w.pages[a.TAKEDOWN_URL] = Page(404, "")
        self.assertReason("takedown")

    def test_support_url_on_another_host_is_a_reason_not_a_fetch(self):
        self.w.loc["attributes"]["supportUrl"] = "https://example.com/support"
        (self.root / "ios/appstore/listing/en-GB/support_url.txt").write_text("https://example.com/support\n")
        self.m = a.load_manifest(self.root)
        lst = self.w.listing()
        reasons = lst.preflight(self.m)
        self.assertTrue(any("support page" in r for r in reasons), reasons)
        self.assertNotIn("https://example.com/support", [c[0] for c in lst.page_http.calls])

    def test_screenshots_must_be_complete_counted_and_equal_to_repo(self):
        self.w.shots[0]["attributes"]["assetDeliveryState"] = {"state": "UPLOAD_COMPLETE"}
        self.assertReason("COMPLETE")
        self.setUp()
        self.w.shots.pop()
        self.assertReason("screenshots")
        self.setUp()
        self.w.shots[1]["attributes"]["sourceFileChecksum"] = "0" * 32
        self.assertReason("screenshots")
        self.setUp()
        self.w.set = None
        self.assertReason("screenshots")

    def test_localization_text_must_equal_repo(self):
        self.w.loc["attributes"]["promotionalText"] = "other"
        self.assertReason("promotionalText")
        self.setUp()
        self.w.info_loc["attributes"]["name"] = "Other"
        self.assertReason("name")

    def test_review_details(self):
        self.w.detail["attributes"]["contactPhone"] = None
        self.assertReason("phone")
        self.setUp()
        self.w.detail["attributes"]["notes"] = "other"
        self.assertReason("notes")
        self.setUp()
        self.w.detail["attributes"]["contactEmail"] = "x@example.invalid"
        self.assertReason("contactEmail")
        self.setUp()
        self.w.detail["attributes"]["demoAccountRequired"] = True
        self.assertReason("demoAccountRequired")
        self.setUp()
        self.w.detail = None
        self.assertReason("review detail")

    def test_attachments_must_be_complete_and_equal_repo(self):
        self.w.attachments[0]["attributes"]["assetDeliveryState"] = {"state": "FAILED"}
        self.assertReason("attachment")
        self.setUp()
        self.w.attachments.clear()
        self.assertReason("attachment")
        self.setUp()
        self.w.attachments[0]["attributes"]["sourceFileChecksum"] = "0" * 32
        self.assertReason("attachment")

    def test_category_content_rights_policy_price_copyright(self):
        self.w.category = {"id": "GAMES"}
        self.assertReason("category")
        self.setUp()
        self.w.app["attributes"]["contentRightsDeclaration"] = "DOES_NOT_USE_THIRD_PARTY_CONTENT"
        self.assertReason("content rights")
        self.setUp()
        self.w.app["attributes"]["contentRightsDeclaration"] = None
        self.assertReason("content rights")
        self.setUp()
        self.w.info_loc["attributes"]["privacyPolicyUrl"] = "https://example.com/p"
        self.assertReason("privacy policy")
        self.setUp()
        self.w.price = None
        self.assertReason("price")
        self.setUp()
        self.w.version["attributes"]["copyright"] = "1999 Someone"
        self.assertReason("copyright")

    def test_reasons_never_echo_listing_text_notes_or_phone(self):
        self.w.loc["attributes"]["description"] = DESCRIPTION + " changed"
        self.w.detail["attributes"]["notes"] = NOTES + " changed"
        self.w.detail["attributes"]["contactPhone"] = PHONE
        text = "\n".join(self.reasons())
        for secret in (DESCRIPTION, NOTES, PHONE, "FAKE-DESCRIPTION", "FAKE-NOTES"):
            self.assertNotIn(secret, text)

    def test_owner_checks_are_listed(self):
        self.assertIn("App Privacy", " ".join(a.OWNER_CHECKS))
        self.assertIn("trader", " ".join(a.OWNER_CHECKS))

    def test_preflight_is_read_only(self):
        lst = self.w.listing()
        lst.preflight(self.m)
        self.assertTrue(all(c[0] == "GET" for c in lst.asc.http.calls))


class ApplyTree(Tree):
    def setUp(self):
        super().setUp()
        self.addCleanup(lambda: self.assertEqual(self.w.unexpected, []))
        self.upload = FakeUpload()

    def lst(self, **kw):
        return self.w.live_listing(self.upload, **kw)

    def apply(self, build=None, lst=None):
        lst = lst or self.lst()
        changes = lst.apply(self.m, build)
        return lst, {(c.resource, c.field): c.action for c in changes}

    @staticmethod
    def writes(lst):
        return [c for c in lst.asc.http.calls if c[0] != "GET"]

    def assertNoWrites(self, lst):
        self.assertEqual(self.writes(lst), [])


class ApplyTextTests(ApplyTree):
    def test_default_one_point_zero_is_renamed_to_marketing_version(self):
        self.w.version["attributes"]["versionString"] = "1.0"
        lst, plan = self.apply()
        self.assertEqual(plan[("appStoreVersion", "versionString")], "updated")
        self.assertEqual(self.w.version["attributes"]["versionString"], self.m.version)
        patch = [c for c in self.writes(lst) if c[1] == "/v1/appStoreVersions/v1"]
        self.assertEqual(len(patch), 1)
        self.assertEqual(patch[0][0], "PATCH")
        self.assertEqual(patch[0][3]["data"]["attributes"], {"versionString": self.m.version})

    def test_missing_version_is_created_for_ios_with_the_manifest_values(self):
        self.w.version = None
        lst, plan = self.apply()
        post = [c for c in self.writes(lst) if (c[0], c[1]) == ("POST", "/v1/appStoreVersions")]
        self.assertEqual(len(post), 1)
        data = post[0][3]["data"]
        self.assertEqual(data["type"], "appStoreVersions")
        self.assertEqual(data["attributes"], {"platform": "IOS", "versionString": self.m.version,
                                              "releaseType": "MANUAL", "copyright": self.m.copyright})
        self.assertEqual(data["relationships"]["app"]["data"], {"type": "apps", "id": "app1"})
        self.assertEqual(plan[("appStoreVersion", "versionString")], "created")
        self.assertEqual(self.w.version["attributes"]["versionString"], self.m.version)

    def test_release_type_is_set_to_manual_and_copyright_synced(self):
        self.w.version["attributes"].update({"releaseType": "AFTER_APPROVAL", "copyright": "old"})
        lst, plan = self.apply()
        self.assertEqual(self.w.version["attributes"]["releaseType"], "MANUAL")
        self.assertEqual(self.w.version["attributes"]["copyright"], self.m.copyright)
        self.assertEqual(plan[("appStoreVersion", "releaseType")], "updated")
        self.assertEqual(plan[("appStoreVersion", "copyright")], "updated")

    def test_every_editable_state_is_accepted(self):
        for state in sorted(a.EDITABLE_STATES):
            with self.subTest(state=state):
                self.w.version["attributes"]["appStoreState"] = state
                lst, plan = self.apply()
                self.assertNoWrites(lst)

    def test_states_that_must_not_be_touched_are_refused_before_any_write(self):
        self.diverge_text()
        for state in ["WAITING_FOR_REVIEW", "PENDING_DEVELOPER_RELEASE", "IN_REVIEW", "READY_FOR_SALE",
                      "PENDING_APPLE_RELEASE", "PROCESSING_FOR_APP_STORE"]:
            with self.subTest(state=state):
                self.w.version["attributes"]["appStoreState"] = state
                lst = self.lst()
                with self.assertRaises(t.AscError) as ctx:
                    lst.apply(self.m, "8")
                self.assertIn(state, str(ctx.exception))
                self.assertNoWrites(lst)

    def test_rejected_version_with_a_newer_build_swaps_the_build_and_keeps_the_version(self):
        self.w.version["attributes"]["appStoreState"] = "REJECTED"
        self.assertEqual(self.w.build["id"], "b1")
        lst, plan = self.apply("8")
        writes = self.writes(lst)
        self.assertEqual([(c[0], c[1]) for c in writes], [("PATCH", "/v1/appStoreVersions/v1/relationships/build")])
        self.assertEqual(writes[0][3], {"data": {"type": "builds", "id": "b2"}})
        self.assertEqual(self.w.build["id"], "b2")
        self.assertEqual(self.w.version["attributes"]["versionString"], self.m.version)
        self.assertEqual(plan[("appStoreVersion", "build")], "updated")
        self.assertEqual(plan[("appStoreVersion", "versionString")], "unchanged")

    def test_attaching_a_build_when_none_is_attached(self):
        self.w.build = None
        lst, plan = self.apply("7")
        self.assertEqual(plan[("appStoreVersion", "build")], "created")
        self.assertEqual(self.w.build["id"], "b1")

    def test_the_already_attached_build_is_not_rewritten(self):
        lst, plan = self.apply("7")
        self.assertNoWrites(lst)
        self.assertEqual(plan[("appStoreVersion", "build")], "unchanged")

    def test_without_a_build_number_the_attachment_is_left_alone(self):
        lst, plan = self.apply(None)
        self.assertNoWrites(lst)
        self.assertEqual(plan[("appStoreVersion", "build")], "unchanged")
        self.w.build = None
        lst, plan = self.apply(None)
        self.assertNoWrites(lst)
        self.assertEqual(plan[("appStoreVersion", "build")], "skipped")

    def test_a_build_that_is_not_valid_or_has_another_version_is_refused_before_any_write(self):
        self.diverge_text()
        cases = {
            "state": lambda: self.w.catalog[1].update(state="PROCESSING"),
            "expired": lambda: self.w.catalog[1].update(expired=True),
            "version": lambda: self.w.catalog[1].update(marketing="1.0.1"),
        }
        for label, mutate in cases.items():
            with self.subTest(label):
                self.w.catalog[1].update(state="VALID", expired=False, marketing=self.m.version)
                mutate()
                lst = self.lst()
                with self.assertRaises(t.AscError):
                    lst.apply(self.m, "8")
                self.assertNoWrites(lst)
        self.w.catalog[1].update(state="VALID", expired=False, marketing=self.m.version)
        lst = self.lst()
        with self.assertRaises(t.AscError):
            lst.apply(self.m, "99")  # no such build
        self.assertNoWrites(lst)

    def test_build_marketing_version_must_equal_marketing_version_in_project_yml(self):
        (self.root / "ios" / "project.yml").write_text('  MARKETING_VERSION: "1.0.1"\n', encoding="utf-8")
        self.m = a.load_manifest(self.root)
        lst = self.lst()
        with self.assertRaises(t.AscError):
            lst.apply(self.m, "7")
        self.assertNoWrites(lst)

    def test_missing_localizations_review_detail_and_category_are_written(self):
        self.diverge_text()
        lst, plan = self.apply("7")
        f = self.m.fields
        self.assertEqual(self.w.loc["attributes"], {
            "locale": self.m.locale, "description": f["description"], "keywords": f["keywords"],
            "promotionalText": f["promotional_text"], "supportUrl": f["support_url"]})
        self.assertEqual(self.w.info_loc["attributes"], {"locale": self.m.locale, "name": f["name"],
                                                         "subtitle": f["subtitle"]})
        self.assertEqual(self.w.category, {"id": "MUSIC"})
        self.assertEqual(self.w.detail["attributes"], {
            "contactFirstName": self.m.review_first_name, "contactLastName": self.m.review_last_name,
            "contactEmail": self.m.review_email, "demoAccountRequired": False, "notes": self.m.notes})
        self.assertEqual(plan[("appStoreVersionLocalization", "description")], "created")
        self.assertEqual(plan[("appInfoLocalization", "subtitle")], "created")
        self.assertEqual(plan[("appStoreReviewDetail", "notes")], "created")
        self.assertEqual(plan[("appInfo", "primaryCategory")], "updated")

    def test_the_new_localizations_point_at_their_parents(self):
        self.diverge_text()
        lst, _ = self.apply()
        by_path = {c[1]: c[3]["data"] for c in self.writes(lst) if c[0] == "POST"}
        self.assertEqual(by_path["/v1/appStoreVersionLocalizations"]["relationships"],
                         {"appStoreVersion": {"data": {"type": "appStoreVersions", "id": "v1"}}})
        self.assertEqual(by_path["/v1/appInfoLocalizations"]["relationships"],
                         {"appInfo": {"data": {"type": "appInfos", "id": "i1"}}})
        self.assertEqual(by_path["/v1/appStoreReviewDetails"]["relationships"],
                         {"appStoreVersion": {"data": {"type": "appStoreVersions", "id": "v1"}}})

    def test_only_differing_fields_are_patched(self):
        self.w.loc["attributes"]["keywords"] = "other"
        self.w.info_loc["attributes"]["subtitle"] = "other"
        self.w.detail["attributes"]["notes"] = "other"
        self.w.category = {"id": "GAMES"}
        lst, plan = self.apply()
        sent = {c[1]: c[3]["data"] for c in self.writes(lst)}
        self.assertEqual(sent["/v1/appStoreVersionLocalizations/l1"]["attributes"], {"keywords": self.m.fields["keywords"]})
        self.assertEqual(sent["/v1/appInfoLocalizations/il1"]["attributes"], {"subtitle": self.m.fields["subtitle"]})
        self.assertEqual(sent["/v1/appStoreReviewDetails/rd1"]["attributes"], {"notes": self.m.notes})
        self.assertEqual(sent["/v1/appInfos/i1"]["relationships"],
                         {"primaryCategory": {"data": {"type": "appCategories", "id": "MUSIC"}}})
        self.assertNotIn("attributes", sent["/v1/appInfos/i1"])
        self.assertEqual(plan[("appStoreVersionLocalization", "description")], "unchanged")
        self.assertEqual(plan[("appStoreVersionLocalization", "keywords")], "updated")
        self.assertEqual(len(sent), 4)

    def test_a_second_apply_writes_nothing(self):
        self.diverge_text()
        first, plan = self.apply("8")
        self.assertTrue(self.writes(first))
        self.assertNotEqual(set(plan.values()), {"unchanged"})
        second, plan = self.apply("8", lst=self.lst())
        self.assertNoWrites(second)
        self.assertEqual(set(plan.values()), {"unchanged"}, plan)

    def test_apply_from_nothing_then_plan_is_all_unchanged(self):
        self.w.version = None
        self.apply("7")
        lst = self.lst()
        self.assertEqual({c.action for c in lst.plan(self.m, build="7")}, {"unchanged"})

    def test_review_phone_is_never_written_and_demo_account_is_false(self):
        self.diverge_text()
        self.w.detail = {"id": "rd1", "attributes": {"contactPhone": PHONE, "notes": "x"}}
        lst, _ = self.apply()
        self.assertEqual(self.w.detail["attributes"]["contactPhone"], PHONE)
        for method, path, params, body in self.writes(lst):
            self.assertNotIn("contactPhone", json.dumps(body))
        self.assertIs(self.w.detail["attributes"]["demoAccountRequired"], False)

    def test_no_request_ever_targets_the_attestations_price_availability_or_phased_release(self):
        self.diverge_text()
        lst, _ = self.apply("8")
        forbidden_paths = ("ageRating", "appPrice", "appAvailabilit", "territor", "phasedRelease",
                           "appPrivacy", "reviewSubmissions", "appStoreVersionSubmissions", "/v1/apps/",
                           "betaGroups", "builds/")
        forbidden_attrs = ("contentRightsDeclaration", "privacyPolicyUrl", "privacyChoicesUrl", "contactPhone",
                           "appStoreAgeRating", "ageRating", "price", "availableInNewTerritories")
        for method, path, params, body in self.writes(lst):
            for bad in forbidden_paths:
                self.assertNotIn(bad, path, (method, path))
            for bad in forbidden_attrs:
                self.assertNotIn(bad, json.dumps(body or {}), (method, path))
        self.assertEqual(self.w.app["attributes"].get("contentRightsDeclaration"), "USES_THIRD_PARTY_CONTENT")

    def test_the_write_guard_refuses_everything_outside_the_listing(self):
        lst = self.lst()
        refused = [
            ("PATCH", "/v1/apps/app1", {"data": {"type": "apps", "id": "app1",
                                                  "attributes": {"contentRightsDeclaration": "X"}}}),
            ("PATCH", "/v1/ageRatingDeclarations/age1", {"data": {"type": "ageRatingDeclarations", "id": "age1",
                                                                  "attributes": {"gambling": False}}}),
            ("POST", "/v1/appPriceSchedules", {"data": {"type": "appPriceSchedules"}}),
            ("PATCH", "/v2/appAvailabilities/av1", {"data": {"type": "appAvailabilities"}}),
            ("POST", "/v1/appStoreVersionPhasedReleases", {"data": {"type": "appStoreVersionPhasedReleases"}}),
            ("POST", "/v1/reviewSubmissions", {"data": {"type": "reviewSubmissions"}}),
            ("PATCH", "/v1/appInfoLocalizations/il1", {"data": {"type": "appInfoLocalizations", "id": "il1",
                                                                "attributes": {"privacyPolicyUrl": "https://x.example"}}}),
            ("PATCH", "/v1/appStoreReviewDetails/rd1", {"data": {"type": "appStoreReviewDetails", "id": "rd1",
                                                                 "attributes": {"contactPhone": PHONE}}}),
            ("PATCH", "/v1/appInfos/i1", {"data": {"type": "appInfos", "id": "i1",
                                                    "relationships": {"secondaryCategory": {"data": None}}}}),
            ("DELETE", "/v1/appStoreVersions/v1", None),
        ]
        for method, path, body in refused:
            with self.subTest(method=method, path=path):
                with self.assertRaises(t.AscError):
                    lst._write(method, path, body)
        self.assertEqual(lst.asc.http.calls, [])

    def test_a_write_failure_withholds_the_server_text_but_keeps_the_codes(self):
        self.diverge_text()
        lst = self.lst()
        lst.asc.http.errors[("POST", "/v1/appStoreVersionLocalizations")] = (409, {"errors": [{
            "code": "ENTITY_ERROR.ATTRIBUTE.INVALID", "title": "bad", "detail": DESCRIPTION + " echoed back"}]})
        with self.assertRaises(t.AscError) as ctx:
            lst.apply(self.m, "7")
        message = str(ctx.exception)
        self.assertIn("ENTITY_ERROR.ATTRIBUTE.INVALID", message)
        self.assertIn("HTTP 409", message)
        self.assertNotIn("FAKE-DESCRIPTION", message)
        self.assertNotIn("echoed", message)


class UploadOperationsTests(unittest.TestCase):
    URL = "https://store.apple.com/up/x"

    @staticmethod
    def op(url=URL, method="PUT", offset=0, length=10, headers=None):
        return {"method": method, "url": url, "offset": offset, "length": length,
                "requestHeaders": headers if headers is not None else [{"name": "Content-Type", "value": "image/jpeg"}]}

    def test_each_part_is_put_with_its_slice_and_the_operation_headers_only(self):
        up = FakeUpload()
        a.upload_operations([self.op(self.URL + "/1", offset=0, length=4), self.op(self.URL + "/2", offset=4, length=6)],
                            b"abcdefghij", up)
        self.assertEqual([(u, d) for u, h, d in up.calls], [(self.URL + "/1", b"abcd"), (self.URL + "/2", b"efghij")])
        for url, headers, data in up.calls:
            self.assertEqual(headers, {"Content-Type": "image/jpeg"})

    def test_no_authorization_is_ever_sent_and_an_operation_asking_for_one_is_refused(self):
        up = FakeUpload()
        for name in ("Authorization", "authorization", "Proxy-Authorization"):
            with self.assertRaises(t.AscError, msg=name):
                a.upload_operations([self.op(headers=[{"name": name, "value": "Bearer x"}])], b"0123456789", up)
        self.assertEqual(up.calls, [])

    def test_http_and_non_apple_hosts_are_refused_and_nothing_is_sent(self):
        bad_urls = ["http://store.apple.com/up/x", "https://evil.example/up/x", "https://apple.com.evil.example/x",
                    "https://notapple.com/x", "https://apple.com/x", "https://store.apple.com@evil.example/x",
                    "https://user:pw@store.apple.com/x", "https://store.apple.com:8443/x", "https://store.apple.com./x",
                    "ftp://store.apple.com/x", "//store.apple.com/x", "", None, 7]
        for url in bad_urls:
            with self.subTest(url=url):
                up = FakeUpload()
                with self.assertRaises(t.AscError):
                    a.upload_operations([self.op(self.URL + "/1", offset=0, length=5), self.op(url, offset=5, length=5)],
                                        b"0123456789", up)
                self.assertEqual(up.calls, [], "a bad later part must stop the first part from being sent")

    def test_malformed_operations_are_refused(self):
        up = FakeUpload()
        cases = [[], [self.op(method="POST")], [self.op(method="GET")], [self.op(offset=-1)],
                 [self.op(offset=5, length=6)], [self.op(length=0)], [self.op(length="10")],
                 [self.op(offset=True)], ["not a dict"], [self.op(headers="x")],
                 [self.op(headers=[{"name": 5, "value": "x"}])]]
        for ops in cases:
            with self.subTest(ops=ops):
                with self.assertRaises(t.AscError):
                    a.upload_operations(ops, b"0123456789", up)
        self.assertEqual(up.calls, [])

    def test_a_failed_part_raises_without_the_signed_url(self):
        up = FakeUpload()
        up.status = 403
        with self.assertRaises(t.AscError) as ctx:
            a.upload_operations([self.op(self.URL + "?sig=SECRET-SIGNATURE")], b"0123456789", up)
        self.assertIn("HTTP 403", str(ctx.exception))
        self.assertNotIn("SECRET-SIGNATURE", str(ctx.exception))
        self.assertNotIn("store.apple.com", str(ctx.exception))

        class Boom:
            def put(self, url, headers, data, timeout=120):
                raise OSError("connect to https://store.apple.com/up/x?sig=SECRET-SIGNATURE failed")

        with self.assertRaises(t.AscError) as ctx:
            a.upload_operations([self.op()], b"0123456789", Boom())
        self.assertIn("OSError", str(ctx.exception))
        self.assertNotIn("SECRET-SIGNATURE", str(ctx.exception))

    def test_the_plain_transport_does_not_follow_redirects(self):
        with mock.patch("requests.Session") as session:
            a.RequestsPlainHttp().put(self.URL, {"Content-Type": "x"}, b"data")
        session.return_value.put.assert_called_once_with(
            self.URL, headers={"Content-Type": "x"}, data=b"data", timeout=120, allow_redirects=False)


class ApplyAssetsTests(ApplyTree):
    def names(self, bucket):
        return [x["attributes"]["fileName"] for x in bucket]

    def kinds(self, lst):
        return [(c[0], c[1]) for c in self.writes(lst)]

    def test_matching_attachments_and_screenshots_are_untouched(self):
        lst, plan = self.apply()
        self.assertNoWrites(lst)
        self.assertEqual(self.upload.calls, [])
        self.assertEqual(plan[("appStoreReviewAttachment", "pack.pdf")], "unchanged")
        self.assertEqual(plan[("appScreenshot", "01-shot1.jpg")], "unchanged")
        self.assertEqual(plan[("appScreenshotSet", "order")], "unchanged")

    def test_an_attachment_is_replaced_only_when_the_checksum_differs(self):
        self.w.attachments[0]["attributes"]["sourceFileChecksum"] = "0" * 32
        lst, plan = self.apply()
        self.assertEqual(plan[("appStoreReviewAttachment", "pack.pdf")], "updated")
        self.assertEqual(self.kinds(lst), [("DELETE", "/v1/appStoreReviewAttachments/at0"),
                                           ("POST", "/v1/appStoreReviewAttachments"),
                                           ("PATCH", "/v1/appStoreReviewAttachments/n1")])
        post, commit = self.writes(lst)[1][3]["data"], self.writes(lst)[2][3]["data"]
        pack = self.m.attachments[0].read_bytes()
        self.assertEqual(post["attributes"], {"fileName": "pack.pdf", "fileSize": len(pack)})
        self.assertEqual(post["relationships"]["appStoreReviewDetail"]["data"],
                         {"type": "appStoreReviewDetails", "id": "rd1"})
        self.assertEqual(commit["attributes"], {"uploaded": True, "sourceFileChecksum": md5(pack)})
        self.assertEqual(b"".join(d for u, h, d in self.upload.calls), pack)
        self.assertEqual(self.names(self.w.attachments), ["pack.pdf"])
        self.assertEqual(self.w.attachments[0]["attributes"]["assetDeliveryState"]["state"], "COMPLETE")

    def test_a_missing_attachment_is_created_and_an_unknown_one_deleted(self):
        self.w.attachments[0]["attributes"]["fileName"] = "old.pdf"
        lst, plan = self.apply()
        self.assertEqual(plan[("appStoreReviewAttachment", "pack.pdf")], "created")
        self.assertEqual(plan[("appStoreReviewAttachment", "old.pdf")], "deleted")
        self.assertEqual(self.names(self.w.attachments), ["pack.pdf"])

    def test_one_differing_screenshot_replaces_only_that_shot_then_reorders(self):
        self.w.shots[2]["attributes"]["sourceFileChecksum"] = "0" * 32
        lst, plan = self.apply()
        self.assertEqual(plan[("appScreenshot", "03-shot3.jpg")], "updated")
        self.assertEqual(plan[("appScreenshot", "01-shot1.jpg")], "unchanged")
        self.assertEqual(plan[("appScreenshotSet", "order")], "updated")
        self.assertEqual(self.kinds(lst), [("DELETE", "/v1/appScreenshots/sc2"), ("POST", "/v1/appScreenshots"),
                                           ("PATCH", "/v1/appScreenshots/n1"),
                                           ("PATCH", "/v1/appScreenshotSets/s1/relationships/appScreenshots")])
        shot = self.m.screenshots[2].read_bytes()
        self.assertEqual(self.writes(lst)[1][3]["data"]["attributes"], {"fileName": "03-shot3.jpg", "fileSize": len(shot)})
        self.assertEqual(self.writes(lst)[1][3]["data"]["relationships"]["appScreenshotSet"]["data"],
                         {"type": "appScreenshotSets", "id": "s1"})
        self.assertEqual(self.writes(lst)[2][3]["data"]["attributes"],
                         {"uploaded": True, "sourceFileChecksum": md5(shot)})
        order = self.writes(lst)[3][3]["data"]
        self.assertEqual(order, [{"type": "appScreenshots", "id": i} for i in ("sc0", "sc1", "n1", "sc3", "sc4", "sc5")])
        self.assertEqual(self.names(self.w.shots), SHOTS)
        self.assertEqual(b"".join(d for u, h, d in self.upload.calls), shot)

    def test_a_missing_screenshot_set_is_created_with_the_display_type_and_filled_in_order(self):
        self.w.set = None
        self.w.shots = []
        lst, plan = self.apply()
        self.assertEqual(plan[("appScreenshotSet", "screenshotDisplayType")], "created")
        post = self.writes(lst)[0]
        self.assertEqual((post[0], post[1]), ("POST", "/v1/appScreenshotSets"))
        self.assertEqual(post[3]["data"]["attributes"], {"screenshotDisplayType": "APP_IPHONE_67"})
        self.assertEqual(post[3]["data"]["relationships"]["appStoreVersionLocalization"]["data"],
                         {"type": "appStoreVersionLocalizations", "id": "l1"})
        self.assertEqual(self.names(self.w.shots), SHOTS)
        self.assertFalse([c for c in self.writes(lst) if "relationships" in c[1]], "already in order: no reorder")

    def test_unknown_and_duplicate_screenshots_are_deleted(self):
        self.w.shots.append({"id": "x1", "attributes": {"fileName": "09-old.jpg", "sourceFileChecksum": "1" * 32,
                                                        "assetDeliveryState": {"state": "COMPLETE"}}})
        self.w.shots.append({"id": "x2", "attributes": dict(self.w.shots[0]["attributes"])})
        lst, plan = self.apply()
        self.assertEqual(plan[("appScreenshot", "09-old.jpg")], "deleted")
        self.assertEqual(sorted(self.kinds(lst)), [("DELETE", "/v1/appScreenshots/x1"), ("DELETE", "/v1/appScreenshots/x2")])
        self.assertEqual(self.names(self.w.shots), SHOTS)

    def test_a_failed_or_abandoned_remote_asset_is_replaced(self):
        self.w.shots[1]["attributes"]["assetDeliveryState"] = {"state": "FAILED"}
        self.w.shots[4]["attributes"].update({"sourceFileChecksum": None,
                                              "assetDeliveryState": {"state": "AWAITING_UPLOAD"}})
        lst, plan = self.apply()
        self.assertEqual(self.names(self.w.shots), SHOTS)
        self.assertEqual({c[1] for c in self.writes(lst) if c[0] == "DELETE"},
                         {"/v1/appScreenshots/sc1", "/v1/appScreenshots/sc4"})
        self.assertTrue(all(x["attributes"]["assetDeliveryState"]["state"] == "COMPLETE" for x in self.w.shots))

    def test_uploads_carry_no_authorization_and_no_token_is_minted_for_them(self):
        minted = []

        def factory():
            minted.append(1)
            return "SECRET-TOKEN-XYZ"

        self.w.shots[0]["attributes"]["sourceFileChecksum"] = "0" * 32
        self.w.attachments[0]["attributes"]["sourceFileChecksum"] = "0" * 32
        lst, _ = self.apply(lst=self.lst(factory=factory))
        self.assertEqual(len(self.upload.calls), 4)  # two parts for the attachment, two for the shot
        for url, headers, data in self.upload.calls:
            self.assertTrue(url.startswith("https://store.apple.com/up/"))
            self.assertNotIn("authorization", {k.lower() for k in headers})
            self.assertNotIn("SECRET-TOKEN-XYZ", " ".join(headers.values()) + url)
        self.assertEqual(len(minted), len(lst.asc.http.calls), "exactly one token per Apple API request, none for uploads")
        before = len(minted)
        with self.assertRaises(t.AscError):
            lst.asc.request("PUT", "https://store.apple.com/up/x")
        self.assertEqual(len(minted), before)

    def test_processing_is_awaited_and_a_stuck_asset_times_out(self):
        self.w.processing_polls = 2
        self.w.shots[0]["attributes"]["sourceFileChecksum"] = "0" * 32
        lst, _ = self.apply()
        self.assertEqual(self.w.shots[-1]["attributes"]["assetDeliveryState"]["state"], "COMPLETE")
        gets = [c for c in lst.asc.http.calls if c[0] == "GET" and c[1].startswith("/v1/appScreenshots/n")]
        self.assertEqual(len(gets), 2)
        self.setUp()
        self.w.processing_polls = 10 ** 6
        self.w.shots[0]["attributes"]["sourceFileChecksum"] = "0" * 32
        with self.assertRaises(t.AscError) as ctx:
            self.lst().apply(self.m)
        self.assertIn("did not finish", str(ctx.exception))

    def test_a_kept_asset_that_is_still_processing_is_awaited_not_replaced(self):
        self.w.shots[0]["attributes"]["assetDeliveryState"] = {"state": "UPLOAD_COMPLETE"}
        self.w.remaining["sc0"] = 1
        lst, _ = self.apply()
        self.assertNoWrites(lst)
        self.assertEqual(self.w.shots[0]["attributes"]["assetDeliveryState"]["state"], "COMPLETE")

    def test_delivery_failed_raises_a_scrubbed_message(self):
        self.w.fail_delivery = {"03-shot3.jpg"}
        self.w.shots[2]["attributes"]["sourceFileChecksum"] = "0" * 32
        with self.assertRaises(t.AscError) as ctx:
            self.lst().apply(self.m)
        message = str(ctx.exception)
        self.assertIn("FAILED", message)
        self.assertIn("IMAGE_TOO_SMALL", message)
        self.assertNotIn("SECRETISH", message)
        self.assertNotIn("https://", message)
        self.assertNotIn("store.apple.com", message)

    def test_a_rejected_upload_part_stops_the_apply_without_the_url(self):
        self.upload.status = 403
        self.w.shots[0]["attributes"]["sourceFileChecksum"] = "0" * 32
        with self.assertRaises(t.AscError) as ctx:
            self.lst().apply(self.m)
        self.assertIn("HTTP 403", str(ctx.exception))
        self.assertNotIn("store.apple.com", str(ctx.exception))

    def test_a_screenshot_count_outside_the_range_is_refused_before_any_write(self):
        self.diverge_text()
        (self.root / "marketing" / "shots" / "06-shot6.jpg").unlink()
        self.m = a.load_manifest(self.root)
        self.assertEqual(len(self.m.screenshots), 5)
        lst = self.lst()
        with self.assertRaises(t.AscError) as ctx:
            lst.apply(self.m, "7")
        self.assertIn("screenshot", str(ctx.exception))
        self.assertNoWrites(lst)
        for n in range(6, 10):
            (self.root / "marketing" / "shots" / f"{n:02d}-shot{n}.jpg").write_bytes(b"x%d" % n)
        self.m = a.load_manifest(self.root)
        self.assertEqual(len(self.m.screenshots), 9)
        lst = self.lst()
        with self.assertRaises(t.AscError):
            lst.apply(self.m, "7")
        self.assertNoWrites(lst)

    def test_from_nothing_apply_uploads_everything_then_a_second_apply_writes_nothing(self):
        self.w.version = None
        first, _ = self.apply("7")
        self.assertEqual(self.names(self.w.shots), SHOTS)
        self.assertEqual(self.names(self.w.attachments), ["pack.pdf"])
        second, plan = self.apply("7", lst=self.lst())
        self.assertNoWrites(second)
        self.assertEqual(set(plan.values()), {"unchanged"}, plan)
        self.assertEqual({c.action for c in self.lst().plan(self.m, build="7")}, {"unchanged"})

    def test_no_upload_or_api_text_reaches_the_summary_output(self):
        self.diverge_text()
        out, err = io.StringIO(), io.StringIO()
        with mock.patch.object(a, "build_listing", return_value=self.w.live_listing()), \
                contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = a.main(["apply", "--src", str(self.root), "--build", "7"])
        text = out.getvalue() + err.getvalue()
        self.assertEqual(code, 0, text)
        self.assertIn("appScreenshot.01-shot1.jpg: created", text)
        for secret in ("store.apple.com", "tok", "Bearer", DESCRIPTION, NOTES, PHONE):
            self.assertNotIn(secret, text)


class MainTests(Tree):
    def run_main(self, argv, listing=None):
        out, err = io.StringIO(), io.StringIO()
        with mock.patch.object(a, "build_listing", return_value=listing or self.w.listing()), \
                contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = a.main(argv)
        return code, out.getvalue() + err.getvalue()

    def test_bad_arguments_exit_2(self):
        for argv in [[], ["plan"], ["nope", "--src", str(self.root)], ["plan", "--src", "/does/not/exist"],
                     ["plan", "--src", str(self.root), "--build", "abc"],
                     ["plan", "--src", str(self.root), "--build", "0"]]:
            code, _ = self.run_main(argv)
            self.assertEqual(code, 2, argv)

    def test_apply_prints_actions_never_the_texts_and_exits_zero(self):
        self.diverge_text()
        code, out = self.run_main(["apply", "--src", str(self.root), "--build", "8"], self.w.live_listing())
        self.assertEqual(code, 0, out)
        self.assertIn("appStoreVersionLocalization.description: created", out)
        self.assertIn("appStoreVersion.versionString: updated", out)
        for secret in (DESCRIPTION, NOTES, PHONE, "FAKE-DESCRIPTION", "FAKE-NOTES", "fake,words"):
            self.assertNotIn(secret, out)
        code, out = self.run_main(["apply", "--src", str(self.root), "--build", "8"], self.w.live_listing())
        self.assertEqual(code, 0, out)
        self.assertNotIn(": created", out)
        self.assertNotIn(": updated", out)

    def test_apply_refusal_exits_one_with_a_scrubbed_message(self):
        self.w.version["attributes"]["appStoreState"] = "WAITING_FOR_REVIEW"
        code, out = self.run_main(["apply", "--src", str(self.root)], self.w.live_listing())
        self.assertEqual(code, 1)
        self.assertIn("WAITING_FOR_REVIEW", out)
        self.assertNotIn(NOTES, out)

    def test_plan_prints_field_names_and_never_the_texts(self):
        self.w.loc["attributes"]["keywords"] = "other"
        code, out = self.run_main(["plan", "--src", str(self.root)])
        self.assertEqual(code, 0)
        self.assertIn("appStoreVersionLocalization.keywords", out)
        self.assertIn("updated", out)
        for secret in (DESCRIPTION, NOTES, PHONE, "FAKE-DESCRIPTION", "FAKE-NOTES", "fake,words"):
            self.assertNotIn(secret, out)

    def test_preflight_exit_codes_and_output(self):
        code, out = self.run_main(["preflight", "--src", str(self.root)])
        self.assertEqual(code, 0, out)
        self.assertIn("App Privacy", out)
        self.w.version["attributes"]["appStoreState"] = "IN_REVIEW"
        code, out = self.run_main(["preflight", "--src", str(self.root)], self.w.listing())
        self.assertEqual(code, 1)
        for secret in (DESCRIPTION, NOTES, PHONE):
            self.assertNotIn(secret, out)

    def test_manifest_failure_exits_1_without_echoing_paths_or_text(self):
        (self.root / "ios" / "project.yml").write_text("x\n", encoding="utf-8")
        code, out = self.run_main(["plan", "--src", str(self.root)])
        self.assertEqual(code, 1)
        self.assertNotIn(NOTES, out)

    def test_api_failure_is_scrubbed_exit_1(self):
        h = self.w.http()
        del h.routes[("GET", "/v1/apps")]
        lst = a.Listing(t.Asc(h, lambda: "tok", sleep=lambda s: None), mock.Mock(), FakePages({}), "com.radioshake")
        code, out = self.run_main(["plan", "--src", str(self.root)], lst)
        self.assertEqual(code, 1)
        self.assertNotIn("Bearer", out)


if __name__ == "__main__":
    unittest.main()
