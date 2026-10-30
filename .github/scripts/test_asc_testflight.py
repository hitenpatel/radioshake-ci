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
    def setUp(self):
        import sanitize_log as sl
        self.s = sl
        self.root = tempfile.mkdtemp()
        os.makedirs(os.path.join(self.root, "ios/App"))
        open(os.path.join(self.root, "ios/App/A.swift"), "w").close()

    def test_only_compiler_diagnostics_in_existing_project_files(self):
        r = self.root
        lines = [
            "note: fine\n",
            f"{r}/ios/App/A.swift:10:5: error: cannot find 'x' in scope\n",
            f"{r}/ios/App/Missing.swift:1:1: error: boom\n",
            "/Users/runner/Library/Developer/x.m:1:2: error: bad at file:///private/var/folders/ab/c\n",
            "error: no file position\n",
            "** ARCHIVE FAILED **\n",
            "e: file:///x/Y.kt:1:2 Unresolved reference\n",
        ]
        out = self.s.sanitize(lines, r)
        self.assertEqual(out, [
            "ios/App/A.swift:10:5: error: cannot find 'x' in scope",
            "<path>:1:1: error: boom",
            "<path>:1:2: error: bad at <path>",
        ])

    def test_key_fragments_are_redacted_everywhere(self):
        r = self.root
        frag = "MIGTAgEAMBMGByqGSM49AgEGCCqGSM49"  # 32 chars of key-like text
        out = self.s.sanitize([
            f"{r}/ios/App/A.swift:1:1: error: {frag}\n",
            f"{frag}:1:1: error: {frag}\n",
            f"{r}/ios/App/A.swift:2:2: error: ok text then {'a' * 20} and 0123456789abcdef0123456789abcdef\n",
        ], r)
        text = "\n".join(out)
        self.assertNotIn(frag, text)
        self.assertNotIn("a" * 20, text)
        self.assertNotIn("0123456789abcdef", text)
        self.assertIn("<redacted>", text)
        self.assertTrue(out[1].startswith("<path>:1:1: error:"))

    def test_19_chars_kept_20_redacted_and_cap(self):
        r = self.root
        out = self.s.sanitize([f"{r}/ios/App/A.swift:1:1: error: {'b' * 19} {'c' * 20}\n"], r)
        self.assertIn("b" * 19, out[0])
        self.assertNotIn("c" * 20, out[0])
        long = self.s.sanitize([f"{r}/ios/App/A.swift:1:1: error: " + "word " * 100 + "\n"], r)
        self.assertLessEqual(len(long[0]), 160)
        many = self.s.sanitize([f"{r}/ios/App/A.swift:{i}:1: error: m\n" for i in range(1, 100)], r)
        self.assertEqual(len(many), 40)

    def test_dotdot_escape_is_not_shown(self):
        out = self.s.sanitize([f"{self.root}/../etc/passwd:1:1: error: x\n"], self.root)
        self.assertEqual(out, ["<path>:1:1: error: x"])


class ExportDigestTests(unittest.TestCase):
    def setUp(self):
        import sanitize_log as sl
        self.s = sl
        self.d = tempfile.mkdtemp()

    def write(self, path, text):
        with open(path, "w") as fh:
            fh.write(text)

    def test_apple_messages_kept_noise_and_key_material_dropped(self):
        log = os.path.join(self.d, "export.log")
        self.write(log, "\n".join([
            "2026-10-30 xcodebuild chatter that should not appear",
            "error: exportArchive: No signing certificate \"iOS Distribution\" found",
            "Error Domain=IDEProvisioningErrorDomain Code=9 \"\" UserInfo={NSLocalizedDescription=Provisioning profile \"x\" missing, NSLocalizedRecoverySuggestion=Add a device}",
            "** EXPORT FAILED **",
            "No profiles for 'com.radioshake' were found",
            "IDEDistribution: -[IDEDistributionLogging _createLoggingBundleAtPath:]: Created bundle at path /var/folders/zz/T/RadioShake.xcdistributionlogs",
            "error: could not read /Users/runner/work/_temp/asc-key.p8",
            "-authenticationKeyID SECRETKEYID1234567890",
            "error: blob MIGTAgEAMBMGByqGSM49AgEGCCqGSM49AgEHBG0wawIBAQQg",
            "  error: leading space is fine",
        ]))
        out = self.s.export_digest(log)
        text = "\n".join(out)
        self.assertIn("error: exportArchive: No signing certificate", text)
        self.assertIn("Error Domain=IDEProvisioningErrorDomain Code=9", text)
        self.assertIn("NSLocalizedDescription=Provisioning profile", text)
        self.assertIn("NSLocalizedRecoverySuggestion=Add a device", text)   # keyword not redacted
        self.assertIn("** EXPORT FAILED **", text)
        self.assertIn("No profiles for 'com.radioshake'", text)
        self.assertIn("error: leading space is fine", text)
        self.assertNotIn("chatter", text)
        self.assertNotIn("asc-key", text)
        self.assertNotIn("SECRETKEYID", text)
        self.assertNotIn("MIGTAgEAM", text)
        self.assertNotIn("/var/folders", text)
        self.assertIn("<path>", text)

    def test_distribution_log_folder_lines_are_included(self):
        folder = os.path.join(self.d, "RadioShake.xcdistributionlogs")
        os.makedirs(folder)
        self.write(os.path.join(folder, "IDEDistribution.standard.log"), "\n".join([
            "noise line",
            "2026 IDEDistribution: Provisioning profile for com.radioshake requires a registered device",
            "error: Your team has no devices from which to generate a provisioning profile.",
            "error: token " + "A1b2C3d4" * 6,
        ]))
        self.write(os.path.join(folder, "other.txt"), "error: not a .log file")
        log = os.path.join(self.d, "export.log")
        self.write(log, f"IDEDistribution logs at {folder}\n** EXPORT FAILED **\n")
        text = "\n".join(self.s.export_digest(log))
        self.assertIn("Your team has no devices", text)
        self.assertIn("requires a registered device", text)
        self.assertNotIn("noise line", text)
        self.assertNotIn("not a .log file", text)
        self.assertNotIn("A1b2C3d4" * 3, text)

    def test_caps_and_dedup_and_traversal(self):
        log = os.path.join(self.d, "export.log")
        self.write(log, "\n".join(f"error: line {i}" for i in range(60)) + "\nerror: line 3\n"
                   + "error: " + "word " * 100 + "\nIDEDistribution ../../etc.xcdistributionlogs\n")
        out = self.s.export_digest(log)
        self.assertEqual(len(out), 20)
        self.assertEqual(len(set(out)), 20)
        self.write(log, "error: " + "word " * 100 + "\n")
        self.assertLessEqual(len(self.s.export_digest(log)[0]), 200)

    def test_cli_export_mode(self):
        import io, contextlib
        log = os.path.join(self.d, "export.log")
        self.write(log, "** EXPORT FAILED **\n")
        buf = io.StringIO()
        old = sys.argv
        sys.argv = ["sanitize_log.py", "--export", log]
        try:
            with contextlib.redirect_stdout(buf):
                self.assertEqual(self.s.main(), 0)
        finally:
            sys.argv = old
        self.assertEqual(buf.getvalue().strip(), "** EXPORT FAILED **")


class InputTests(unittest.TestCase):
    OK = {"SHA": "a" * 40, "VALIDATED_RUN": "123", "MODE": "release", "RESUME_BUILD": "", "RELEASE_RUN": "", "NOTES": "hi"}

    def errs(self, **kw):
        import validate_inputs as v
        return v.check({**self.OK, **kw})

    def test_valid(self):
        self.assertEqual(self.errs(), [])
        self.assertEqual(self.errs(MODE="resume", RESUME_BUILD="7", RELEASE_RUN="99"), [])
        self.assertEqual(self.errs(NOTES="a\nb\tc"), [])

    def test_rejects(self):
        for kw in ({"SHA": "A" * 40}, {"SHA": "a" * 39}, {"SHA": "a" * 40 + "\n"}, {"SHA": "main"},
                   {"VALIDATED_RUN": "12 3"}, {"VALIDATED_RUN": "0"}, {"VALIDATED_RUN": "1\n2"},
                   {"MODE": "Release"}, {"MODE": "resume"}, {"MODE": "resume", "RESUME_BUILD": "x", "RELEASE_RUN": "9"},
                   {"MODE": "resume", "RESUME_BUILD": "7"}, {"MODE": "resume", "RESUME_BUILD": "7", "RELEASE_RUN": "x"},
                   {"RESUME_BUILD": "5"}, {"RELEASE_RUN": "5"}, {"NOTES": "x" * 4001}, {"NOTES": "a\x00b"}):
            self.assertTrue(self.errs(**kw), kw)


SHA_A = "a" * 40
SHA_B = "b" * 40
TS = "2026-10-30T10:11:12.1234567Z "


def run_json(**kw):
    base = {"repository": {"full_name": "o/r"}, "status": "completed", "conclusion": "success",
            "event": "workflow_dispatch", "head_branch": "main",
            "display_title": f"ios-testflight release {SHA_A} (validated 5)"}
    base.update(kw)
    return base


class GateTests(unittest.TestCase):
    def setUp(self):
        import gate_check as g
        self.g = g

    def validate(self, log, run=None, workflow=None, compare=None, jobs=None):
        return self.g.check_validate(
            run or run_json(), workflow or {"path": ".github/workflows/ios-shared-validate.yml"},
            compare or {"status": "ahead"},
            [{"name": "validate", "conclusion": "success"}] if jobs is None else jobs, log, "o/r", SHA_A)

    def test_first_clone_line_wins_over_forged_later_line(self):
        log = f"{TS}Cloned main at {SHA_B}\n{TS}build output\n{TS}Cloned main at {SHA_A}\n"
        self.assertEqual(self.g.first_clone_line(log), f"Cloned main at {SHA_B}")
        self.assertTrue(self.validate(log))  # first line is for B, not A: rejected
        self.assertEqual(self.validate(f"{TS}Cloned main at {SHA_A}\n{TS}Cloned main at {SHA_B}\n"), [])

    def test_script_echo_lines_and_other_refs_do_not_match(self):
        script = f'{TS}          echo "Cloned $REF at $HEAD_SHA"\n'
        self.assertIsNone(self.g.first_clone_line(script))
        self.assertTrue(self.validate(f"{TS}Cloned feat/x at {SHA_A}\n"))
        self.assertTrue(self.validate(f"{TS}Cloned main at {SHA_A} extra\n"))
        self.assertTrue(self.validate(f"x{TS}Cloned main at {SHA_A}\n"))
        self.assertEqual(self.validate(f"Cloned main at {SHA_A}\r\n"), [])

    def test_trusted_revision_event_branch_and_ancestry(self):
        ok = f"{TS}Cloned main at {SHA_A}\n"
        self.assertEqual(self.validate(ok), [])
        self.assertEqual(self.validate(ok, compare={"status": "identical"}), [])
        self.assertTrue(self.validate(ok, compare={"status": "diverged"}))
        self.assertTrue(self.validate(ok, compare={"status": "behind"}))
        self.assertTrue(self.validate(ok, run=run_json(event="repository_dispatch")))
        self.assertTrue(self.validate(ok, run=run_json(head_branch="evil")))
        self.assertTrue(self.validate(ok, run=run_json(conclusion="failure")))
        self.assertTrue(self.validate(ok, run=run_json(status="in_progress")))
        self.assertTrue(self.validate(ok, run=run_json(repository={"full_name": "x/y"})))
        self.assertTrue(self.validate(ok, workflow={"path": ".github/workflows/other.yml"}))
        self.assertTrue(self.validate(ok, jobs=[{"name": "validate", "conclusion": "failure"}]))
        self.assertTrue(self.validate(ok, jobs=[]))
        self.assertTrue(self.validate(ok, jobs=[{"name": "validate", "conclusion": "success"}] * 2))

    def release(self, log, run=None, workflow=None, compare=None, jobs=None, build="7", sha=SHA_A):
        return self.g.check_release(
            run or run_json(), workflow or {"path": ".github/workflows/ios-testflight.yml"},
            compare or {"status": "ahead"}, [{"name": "testflight"}] if jobs is None else jobs, log, "o/r", sha, build)

    def test_resume_provenance(self):
        line = f"{TS}Uploaded build 7 for sha {SHA_A}\n"
        self.assertEqual(self.release(line), [])
        self.assertTrue(self.release(line, build="8"))                      # other build number
        self.assertTrue(self.release(f"{TS}Uploaded build 7 for sha {SHA_B}\n"))
        self.assertTrue(self.release("no line\n"))
        self.assertTrue(self.release(line, sha=SHA_B))                      # display_title is for A
        self.assertTrue(self.release(line, run=run_json(display_title=f"ios-testflight archive_only {SHA_A} (validated 5)")))
        self.assertTrue(self.release(line, run=run_json(display_title=f"ios-testflight resume {SHA_A} (validated 5)")))
        self.assertTrue(self.release(line, workflow={"path": ".github/workflows/ios-shared-validate.yml"}))
        self.assertTrue(self.release(line, compare={"status": "diverged"}))
        self.assertTrue(self.release(line, jobs=[]))
        self.assertTrue(self.release(f"{TS}Uploaded build 77 for sha {SHA_A}\n"))  # anchored, no prefix match

    def test_cli_reads_files(self):
        import json
        d = tempfile.mkdtemp()
        files = {"run": run_json(), "workflow": {"path": ".github/workflows/ios-shared-validate.yml"},
                 "compare": {"status": "ahead"}, "jobs": [{"name": "validate", "conclusion": "success"}]}
        args = ["validate", "--repo", "o/r", "--sha", SHA_A]
        for k, v in files.items():
            with open(os.path.join(d, k), "w") as fh:
                json.dump(v, fh)
            args += [f"--{k}", os.path.join(d, k)]
        with open(os.path.join(d, "log"), "w") as fh:
            fh.write(f"{TS}Cloned main at {SHA_A}\n")
        self.assertEqual(self.g.main(args + ["--log", os.path.join(d, "log")]), 0)
        with open(os.path.join(d, "log"), "w") as fh:
            fh.write(f"{TS}Cloned main at {SHA_B}\n")
        self.assertEqual(self.g.main(args + ["--log", os.path.join(d, "log")]), 1)


class RetryAndReadinessTests(DistributeTests):
    def test_allocation_is_strictly_greater_than_rejected_even_if_not_visible(self):
        h = FakeHttp()
        h.on("GET", "/v1/builds", Resp(body={"data": [build("b1", "4")], "included": [pre_inc()]}))
        c, _ = client(h)
        self.assertEqual(c.next_build_number("A"), 5)
        self.assertEqual(c.next_build_number("A", greater_than=5), 6)   # 5 rejected, not yet visible
        self.assertEqual(c.next_build_number("A", greater_than=2), 5)   # remote max still wins
        self.assertEqual(c.next_build_number("A", greater_than=0), 5)

    def test_classify(self):
        for state, want in [("IN_BETA_TESTING", "ready"), ("READY_FOR_BETA_TESTING", "ready"),
                            ("MISSING_EXPORT_COMPLIANCE", "blocked"), ("PROCESSING_EXCEPTION", "blocked"),
                            ("EXPIRED", "blocked"), ("PROCESSING", "pending"),
                            ("IN_EXPORT_COMPLIANCE_REVIEW", "pending"), ("UNKNOWN", "pending")]:
            self.assertEqual(t.classify_beta_state(state), want, state)

    def beta_http(self, states, group_builds=None):
        h = self.base([self.valid_page()], group_builds or [Resp(body={"data": [{"id": "B1"}]})])
        h.routes[("GET", "/v1/builds/B1/buildBetaDetail")] = [
            s if isinstance(s, Resp) else Resp(body={"data": {"attributes": {"internalBuildState": s}}}) for s in states]
        return h

    def test_polls_beta_state_after_membership_until_ready(self):
        h = self.beta_http(["PROCESSING", Resp(500), Resp(500), Resp(500), Resp(500), Resp(500), "IN_BETA_TESTING"])
        c, clock = client(h)
        self.assertEqual(c.distribute("com.radioshake", "1.0.0", "5", "Internal", "", 60), 0)
        o = self.outputs()
        self.assertEqual((o["beta_state"], o["readiness"]), ("IN_BETA_TESTING", "ready"))
        detail_calls = [x for x in h.calls if x[1].endswith("buildBetaDetail")]
        group_calls = [i for i, x in enumerate(h.calls) if x[1] == "/v1/betaGroups/G/builds"]
        first_detail = next(i for i, x in enumerate(h.calls) if x[1].endswith("buildBetaDetail"))
        self.assertGreater(first_detail, group_calls[0])  # beta state read only after membership known
        self.assertGreater(len(detail_calls), 1)

    def test_unknown_after_window_is_green_pending(self):
        h = self.beta_http([Resp(404, {"errors": []})])
        c, clock = client(h)
        self.assertEqual(c.distribute("com.radioshake", "1.0.0", "5", "Internal", "", 60, beta_wait_minutes=2), 0)
        o = self.outputs()
        self.assertEqual((o["beta_state"], o["readiness"], o["state"]), ("UNKNOWN", "pending", "VALID"))

    def test_pending_state_after_window_is_green_pending(self):
        h = self.beta_http(["PROCESSING"])
        c, _ = client(h)
        self.assertEqual(c.distribute("com.radioshake", "1.0.0", "5", "Internal", "", 60, beta_wait_minutes=1), 0)
        self.assertEqual(self.outputs()["readiness"], "pending")

    def test_blocked_state_is_red_and_stops_polling(self):
        h = self.beta_http(["PROCESSING_EXCEPTION"])
        c, _ = client(h)
        self.assertEqual(c.distribute("com.radioshake", "1.0.0", "5", "Internal", "", 60), 1)
        self.assertEqual(self.outputs()["readiness"], "blocked")
        self.assertEqual(len([x for x in h.calls if x[1].endswith("buildBetaDetail")]), 1)

    def test_membership_polled_after_add(self):
        h = self.beta_http(["IN_BETA_TESTING"], group_builds=[Resp(body={"data": []}), Resp(body={"data": []}),
                                                              Resp(body={"data": [{"id": "B1"}]})])
        c, _ = client(h)
        self.assertEqual(c.distribute("com.radioshake", "1.0.0", "5", "Internal", "", 60), 0)
        self.assertEqual(self.outputs()["in_group"], "true")

    def test_exists_polls_then_reports(self):
        h = self.base([Resp(body={"data": []}), self.valid_page()], [Resp(body={"data": []})])
        c, _ = client(h)
        self.assertTrue(c.build_visible("com.radioshake", "1.0.0", "5", 5))
        h2 = self.base([Resp(body={"data": []})], [Resp(body={"data": []})])
        c2, _ = client(h2)
        self.assertFalse(c2.build_visible("com.radioshake", "1.0.0", "5", 1))


class LocalizationTests(unittest.TestCase):
    def http(self, post_response, listings):
        h = FakeHttp()
        h.on("GET", "/v1/builds/B1/betaBuildLocalizations", *listings)
        h.on("POST", "/v1/betaBuildLocalizations", post_response)
        h.on("PATCH", "/v1/betaBuildLocalizations/L1", Resp(body={"data": {}}))
        return h

    def loc(self, locale="en-GB"):
        return Resp(body={"data": [{"id": "L1", "attributes": {"locale": locale}}]})

    def test_conflict_on_create_reconciles_with_patch(self):
        h = self.http(Resp(409, {"errors": [{"code": "ENTITY_ERROR.ATTRIBUTE.INVALID.DUPLICATE"}]}),
                      [Resp(body={"data": []}), self.loc()])
        c, _ = client(h)
        c.set_whats_new("B1", "notes")
        self.assertEqual([x[0] for x in h.calls], ["GET", "POST", "GET", "PATCH"])
        self.assertEqual(h.calls[-1][3]["data"]["attributes"], {"whatsNew": "notes"})

    def test_ambiguous_create_timeout_after_retries_reconciles(self):
        h = self.http(Resp(503), [Resp(body={"data": []}), self.loc()])
        c, _ = client(h)
        c.set_whats_new("B1", "notes")
        self.assertEqual(h.calls[-1][0], "PATCH")

    def test_existing_localization_is_patched_without_post(self):
        h = self.http(Resp(201, {"data": {}}), [self.loc()])
        c, _ = client(h)
        c.set_whats_new("B1", "notes")
        self.assertEqual([x[0] for x in h.calls], ["GET", "PATCH"])

    def test_other_locale_ignored_and_genuine_failure_raised(self):
        h = self.http(Resp(403, {"errors": []}), [Resp(body={"data": []}), Resp(body={"data": []})])
        c, _ = client(h)
        with self.assertRaises(t.AscError):
            c.set_whats_new("B1", "notes")
        h = self.http(Resp(201, {"data": {}}), [Resp(body={"data": [{"id": "L9", "attributes": {"locale": "de-DE"}}]})])
        c, _ = client(h)
        c.set_whats_new("B1", "notes")
        self.assertEqual([x[0] for x in h.calls], ["GET", "POST"])


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
