"""Offline tests for asc_testflight.py. Run: python3 -m unittest discover -s .github/scripts"""
import os
import sys
import tempfile
import time
import unittest

sys.path.insert(0, os.path.dirname(__file__))
import asc_testflight as t  # noqa: E402

APP = "https://api.appstoreconnect.apple.com"


class Resp:
    def __init__(self, status=200, body=None):
        self.status_code = status
        self._body = body
        self.text = "" if body is None else "x"

    def json(self):
        return self._body


class FakeHttp:
    """Routes (method, path) to a callable or a list of responses."""

    def __init__(self):
        self.routes = {}
        self.calls = []

    def on(self, method, path, *responses):
        self.routes[(method, path)] = list(responses)

    def request(self, method, url, headers, json=None, params=None, timeout=30):
        assert headers["Authorization"].startswith("Bearer ")
        path = url[len(APP):]
        self.calls.append((method, path, params, json))
        queue = self.routes[(method, path)]
        return queue.pop(0) if len(queue) > 1 else queue[0]


class Clock:
    def __init__(self):
        self.now = 0.0

    def __call__(self):
        return self.now

    def sleep(self, seconds):
        self.now += seconds


def client(http):
    c = Clock()
    return t.Asc(http, lambda: "tok", sleep=c.sleep, clock=c), c


def build(bid, number, marketing="1.0.0", state="VALID", pre="p1"):
    return {"id": bid, "type": "builds",
            "attributes": {"version": number, "processingState": state, "expired": False},
            "relationships": {"preReleaseVersion": {"data": {"type": "preReleaseVersions", "id": pre}}}}


def pre_inc(pid="p1", version="1.0.0"):
    return {"type": "preReleaseVersions", "id": pid, "attributes": {"version": version, "platform": "IOS"}}


class AllocationTests(unittest.TestCase):
    def test_empty_account_starts_at_one(self):
        h = FakeHttp()
        h.on("GET", "/v1/builds", Resp(body={"data": []}))
        c, _ = client(h)
        self.assertEqual(c.next_build_number("A"), 1)

    def test_max_plus_one_across_pages_ignoring_non_integers(self):
        h = FakeHttp()
        h.on("GET", "/v1/builds", Resp(body={
            "data": [build("b1", "3"), build("b2", "1.2.3"), build("b3", "abc")],
            "included": [pre_inc()],
            "links": {"next": f"{APP}/v1/builds?cursor=2"}}))
        h.on("GET", "/v1/builds?cursor=2", Resp(body={"data": [build("b4", "12"), build("b5", "7")]}))
        c, _ = client(h)
        self.assertEqual(c.next_build_number("A"), 13)
        self.assertEqual(len(h.calls), 2)

    def test_pagination_cap(self):
        h = FakeHttp()
        h.on("GET", "/v1/builds", Resp(body={"data": [], "links": {"next": f"{APP}/v1/builds"}}))
        c, _ = client(h)
        with self.assertRaises(t.AscError):
            c.next_build_number("A")

    def test_next_link_to_foreign_host_refused(self):
        h = FakeHttp()
        h.on("GET", "/v1/builds", Resp(body={"data": [], "links": {"next": "https://evil.example/x"}}))
        c, _ = client(h)
        with self.assertRaises(t.AscError):
            c.next_build_number("A")
        self.assertEqual(len(h.calls), 1)


class TransportTests(unittest.TestCase):
    def test_retries_then_succeeds(self):
        h = FakeHttp()
        h.on("GET", "/v1/apps", Resp(503), Resp(429), Resp(body={"data": [
            {"id": "A", "attributes": {"bundleId": "com.radioshake"}}]}))
        c, clock = client(h)
        self.assertEqual(c.find_app("com.radioshake"), "A")
        self.assertEqual(clock.now, 2 + 4)

    def test_client_error_not_retried_and_scrubbed(self):
        h = FakeHttp()
        h.on("GET", "/v1/apps", Resp(403, {"errors": [{"code": "FORBIDDEN", "title": "no",
                                                       "detail": "eyJhbGciOiJFUzI1NiJ9.abcdefghij.klmnop"}]}))
        c, _ = client(h)
        with self.assertRaises(t.AscError) as ctx:
            c.find_app("com.radioshake")
        self.assertNotIn("eyJ", str(ctx.exception))
        self.assertEqual(len(h.calls), 1)

    def test_scrub_redacts_long_base64_and_keys(self):
        self.assertNotIn("A" * 60, t.scrub("x " + "A" * 60))
        self.assertNotIn("BEGIN", t.scrub("-----BEGIN PRIVATE KEY-----"))


class DistributeTests(unittest.TestCase):
    def setUp(self):
        self.out = tempfile.NamedTemporaryFile("w+", delete=False)
        os.environ["GITHUB_OUTPUT"] = self.out.name
        self.addCleanup(lambda: os.unlink(self.out.name))

    def outputs(self):
        with open(self.out.name) as fh:
            return dict(line.strip().split("=", 1) for line in fh if "=" in line)

    def base(self, builds_responses, group_builds, group_attrs=None, detail="IN_BETA_TESTING"):
        h = FakeHttp()
        h.on("GET", "/v1/apps", Resp(body={"data": [{"id": "A", "attributes": {"bundleId": "com.radioshake"}}]}))
        h.on("GET", "/v1/builds", *builds_responses)
        h.on("GET", "/v1/builds/B1/buildBetaDetail",
             Resp(body={"data": {"attributes": {"internalBuildState": detail}}}))
        h.on("GET", "/v1/apps/A/betaGroups", Resp(body={"data": [
            {"id": "G", "attributes": {"name": "Internal", "isInternalGroup": True, **(group_attrs or {})}},
            {"id": "X", "attributes": {"name": "Other", "isInternalGroup": False}}]}))
        h.on("GET", "/v1/betaGroups/G/builds", *group_builds)
        h.on("POST", "/v1/betaGroups/G/relationships/builds", Resp(204))
        h.on("GET", "/v1/builds/B1/betaBuildLocalizations", Resp(body={"data": []}))
        h.on("POST", "/v1/betaBuildLocalizations", Resp(201, {"data": {}}))
        return h

    def valid_page(self):
        return Resp(body={"data": [build("B1", "5")], "included": [pre_inc()]})

    def test_tolerates_missing_build_then_processes_and_distributes(self):
        h = self.base([Resp(body={"data": []}),
                       Resp(body={"data": [build("B1", "5", state="PROCESSING")], "included": [pre_inc()]}),
                       self.valid_page()],
                      [Resp(body={"data": []}), Resp(body={"data": [{"id": "B1"}]})])
        c, _ = client(h)
        rc = c.distribute("com.radioshake", "1.0.0", "5", "Internal", "Try the radio", 60)
        self.assertEqual(rc, 0)
        o = self.outputs()
        self.assertEqual((o["state"], o["in_group"], o["whats_new"], o["asc_build_id"]), ("VALID", "true", "set", "B1"))
        posts = [x for x in h.calls if x[0] == "POST"]
        self.assertEqual(posts[0][3]["data"]["attributes"], {"locale": "en-GB", "whatsNew": "Try the radio"})
        self.assertEqual(posts[1][1], "/v1/betaGroups/G/relationships/builds")

    def test_already_in_group_is_not_readded_and_no_notes_skipped(self):
        h = self.base([self.valid_page()], [Resp(body={"data": [{"id": "B1"}]})])
        c, _ = client(h)
        self.assertEqual(c.distribute("com.radioshake", "1.0.0", "5", "Internal", "", 60), 0)
        self.assertEqual([x for x in h.calls if x[0] == "POST"], [])
        self.assertEqual(self.outputs()["whats_new"], "skipped")

    def test_wrong_marketing_version_is_not_matched(self):
        h = self.base([Resp(body={"data": [build("B1", "5")], "included": [pre_inc(version="0.9.0")]})],
                      [Resp(body={"data": []})])
        c, _ = client(h)
        self.assertEqual(c.distribute("com.radioshake", "1.0.0", "5", "Internal", "", 2), 0)
        self.assertEqual(self.outputs()["state"], "PENDING")

    def test_invalid_build_fails(self):
        h = self.base([Resp(body={"data": [build("B1", "5", state="INVALID")], "included": [pre_inc()]})], [Resp(body={"data": []})])
        c, _ = client(h)
        self.assertEqual(c.distribute("com.radioshake", "1.0.0", "5", "Internal", "", 60), 1)
        self.assertEqual(self.outputs()["state"], "INVALID")

    def test_still_processing_after_deadline_is_pending_green(self):
        h = self.base([Resp(body={"data": [build("B1", "5", state="PROCESSING")], "included": [pre_inc()]})],
                      [Resp(body={"data": []})])
        c, clock = client(h)
        self.assertEqual(c.distribute("com.radioshake", "1.0.0", "5", "Internal", "", 60), 0)
        self.assertEqual(self.outputs()["state"], "PENDING")
        self.assertGreaterEqual(clock.now, 3600)

    def test_all_builds_group_waits_then_falls_back_to_add(self):
        h = self.base([self.valid_page()], [Resp(body={"data": []}), Resp(body={"data": []}),
                                            Resp(body={"data": []}), Resp(body={"data": [{"id": "B1"}]})],
                      group_attrs={"hasAccessToAllBuilds": True})
        c, _ = client(h)
        self.assertEqual(c.distribute("com.radioshake", "1.0.0", "5", "Internal", "", 60, group_wait_minutes=1), 0)
        self.assertEqual(self.outputs()["in_group"], "true")

    def test_missing_export_compliance_fails(self):
        h = self.base([self.valid_page()], [Resp(body={"data": [{"id": "B1"}]})], detail="MISSING_EXPORT_COMPLIANCE")
        c, _ = client(h)
        self.assertEqual(c.distribute("com.radioshake", "1.0.0", "5", "Internal", "", 60), 1)

    def test_emit_rejects_newlines(self):
        with self.assertRaises(t.AscError):
            t.emit("k", "a\nb=c")


class SanitizeTests(unittest.TestCase):
    def test_paths_keys_and_noise(self):
        import sanitize_log as s
        lines = [
            "note: fine\n",
            "/Users/runner/work/radioshake-ci/radioshake-ci/src/ios/App/A.swift:10:5: error: cannot find 'x'\n",
            "error: could not read /Users/runner/Library/secret/file.p8\n",
            "/Users/runner/Library/Developer/x.m:1: error: boom at file:///private/var/folders/ab/c\n",
            "error: token eyJhbGciOiJFUzI1NiJ9.payloadpayload.sig\n",
            "error: blob " + "A" * 70 + "\n",
            "** ARCHIVE FAILED **\n",
        ]
        out = s.sanitize(lines, "/Users/runner/work/radioshake-ci/radioshake-ci/src")
        self.assertEqual(out[0], "ios/App/A.swift:10:5: error: cannot find 'x'")
        self.assertEqual(out[1], "<path>:1: error: boom at <path>")
        self.assertEqual(out[2], "** ARCHIVE FAILED **")
        self.assertEqual(len(out), 3)


class InputTests(unittest.TestCase):
    OK = {"SHA": "a" * 40, "VALIDATED_RUN": "123", "MODE": "release", "RESUME_BUILD": "", "NOTES": "hi"}

    def errs(self, **kw):
        import validate_inputs as v
        return v.check({**self.OK, **kw})

    def test_valid(self):
        self.assertEqual(self.errs(), [])
        self.assertEqual(self.errs(MODE="resume", RESUME_BUILD="7"), [])
        self.assertEqual(self.errs(NOTES="a\nb\tc"), [])

    def test_rejects(self):
        for kw in ({"SHA": "A" * 40}, {"SHA": "a" * 39}, {"SHA": "a" * 40 + "\n"}, {"SHA": "main"},
                   {"VALIDATED_RUN": "12 3"}, {"VALIDATED_RUN": "0"}, {"VALIDATED_RUN": "1\n2"},
                   {"MODE": "Release"}, {"MODE": "resume"}, {"MODE": "resume", "RESUME_BUILD": "x"},
                   {"RESUME_BUILD": "5"}, {"NOTES": "x" * 4001}, {"NOTES": "a\x00b"}):
            self.assertTrue(self.errs(**kw), kw)


class JwtTests(unittest.TestCase):
    def test_token_claims_and_freshness(self):
        try:
            import jwt
            from cryptography.hazmat.primitives import serialization
            from cryptography.hazmat.primitives.asymmetric import ec
        except ImportError:
            self.skipTest("PyJWT/cryptography not installed")
        key = ec.generate_private_key(ec.SECP256R1())
        pem = key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                                serialization.NoEncryption())
        with tempfile.NamedTemporaryFile("wb", delete=False) as fh:
            fh.write(pem)
        self.addCleanup(lambda: os.unlink(fh.name))
        factory = t.make_token_factory("KID123", "ISSUER", fh.name)
        tok = factory()
        claims = jwt.decode(tok, key.public_key(), algorithms=["ES256"], audience="appstoreconnect-v1")
        self.assertEqual(claims["iss"], "ISSUER")
        self.assertLessEqual(claims["exp"] - claims["iat"], 1200)
        self.assertLess(abs(claims["iat"] - int(time.time())), 5)
        self.assertEqual(jwt.get_unverified_header(tok)["kid"], "KID123")


if __name__ == "__main__":
    unittest.main()
