"""Offline tests for asc_listing.py (read-only half: manifest, plan, preflight, page reads).
Run: python3 -m unittest discover -s .github/scripts

The repository is public: every text below is fake. Nothing here may echo a real listing."""
import contextlib
import hashlib
import io
import json
import os
import pathlib
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
        self.pages = {
            m.fields["support_url"]: Page(200, f"Write to {EMAIL} for help"),
            a.PRIVACY_URL: Page(200, "We use ShazamKit to identify songs."),
            a.TAKEDOWN_URL: Page(200, "Rights holders: contact us"),
        }

    def http(self) -> FakeHttp:
        h = FakeHttp()
        r = h.routes
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
        r[("GET", "/v1/builds/b1/preReleaseVersion")] = {"data": self.pre}
        r[("GET", "/v1/apps/app1/appPriceSchedule")] = {"data": self.price}
        r[("GET", "/v1/apps/app1/appAvailabilityV2")] = {"data": {"id": "av1"}}
        r[("GET", "/v1/appAvailabilities/av1/territoryAvailabilities")] = {"data": self.terr}
        return h

    def listing(self):
        return a.Listing(t.Asc(self.http(), lambda: "tok", sleep=lambda s: None), mock.Mock(),
                         FakePages(self.pages), "com.radioshake")


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

    def test_apply_is_not_available_yet(self):
        code, out = self.run_main(["apply", "--src", str(self.root)])
        self.assertEqual(code, 2)
        self.assertIn("apply", out)

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
