"""Unit tests for proton_mcp.py against local fake IMAP and SMTP servers. No real Bridge is used."""
import ast
import io
import json
import os
import re
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
sys.path.insert(0, HERE)

import proton_mcp as pm  # noqa: E402
from fake_imap import FakeIMAP  # noqa: E402

SRC = os.path.join(os.path.dirname(HERE), "proton_mcp.py")
TEST_PW = "test-bridge-password-XYZ"
SUBJECT = "Quarterly report secret subject 7731"
LOG_RE = re.compile(r"^\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2} [+-]\d{4} \| (a|b) \| [a-z_]+ \| \d+ \| "
                    r"(ok|refused|error:[A-Za-z_][A-Za-z0-9_]*)( \| uids [0-9,]+)?$")
A_ADDR = "orders@example.com"
A_NAME = "Example Org LLC"
B_ADDR = "me@example.org"


def sample(subject=SUBJECT, frm="client@example.com"):
    return ("From: %s\r\nTo: orders@example.com\r\nSubject: %s\r\nDate: Wed, 02 Jan 2030 10:00:00 +0000\r\n"
            "Message-ID: <a1@example.com>\r\nContent-Type: text/plain; charset=utf-8\r\n\r\nBody line one.\r\n"
            % (frm, subject)).encode("utf-8")


def default_profiles(sig_path):
    """Profile a: shared address with a fixed From name and a required signature (login shown in review).
    Profile b: private address, login hidden in the review, refuses any address at profile a's domain."""
    return {
        "a": {"username": A_ADDR, "from_name": A_NAME, "signature_file": sig_path,
              "hide_login_in_review": False, "send_enabled": True},
        "b": {"username": B_ADDR, "hide_login_in_review": True, "send_enabled": True,
              "refuse_domains": ["example.com"]},
    }


class Roots:
    """Point the profiles file and state folder at temp paths for one test (in-process only)."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.state = os.path.join(self.tmp.name, "state")
        self.roots = {k: os.path.join(self.state, k) for k in ("a", "b")}
        for d in self.roots.values():
            os.makedirs(d)
        self.sig_path = os.path.join(self.tmp.name, "signature-a.txt")
        self.config_path = os.path.join(self.tmp.name, "profiles.json")
        self.write_config()
        pm._TEST_HARNESS = True

    def tearDown(self):
        pm._TEST_HARNESS = False
        self.tmp.cleanup()

    def write_config(self, profiles=None, mode=0o600, path=None, **top):
        data = {"state_dir": self.state, "timezone": "UTC",
                "profiles": profiles if profiles is not None else default_profiles(self.sig_path)}
        data.update(top)
        path = path or self.config_path
        with open(path, "w") as fh:
            json.dump(data, fh)
        os.chmod(path, mode)
        return path

    def env(self, profile, **extra):
        e = {"PROTON_MCP_CONFIG": self.config_path,
             "PROTON_MCP_LOG_DIR": os.path.join(self.roots[profile], "log"),
             "PROTON_MCP_ERR_DIR": os.path.join(self.roots[profile], "err")}
        e.update(extra)
        return e


class Base(Roots, unittest.TestCase):
    instance = "a"

    def setUp(self):
        Roots.setUp(self)
        self.fake = FakeIMAP().__enter__()
        self.cfg = pm.load_config(self.instance, self.env(self.instance, PROTON_MCP_IMAP_PORT=str(self.fake.port),
                                                          PROTON_MCP_TEST_PLAIN="1"))
        self.mb = pm.Mailbox(self.cfg, password_reader=lambda cfg: TEST_PW)
        self.uid = self.fake.state.add("INBOX", sample())

    def tearDown(self):
        self.fake.__exit__()
        Roots.tearDown(self)

    def logs(self):
        out = []
        for d in (self.cfg["log_dir"], self.cfg["err_dir"]):
            if os.path.isdir(d):
                for f in os.listdir(d):
                    if os.path.isfile(os.path.join(d, f)):
                        out.append(open(os.path.join(d, f)).read())
        return "\n".join(out)

    def call(self, name, args):
        return pm.call_tool(self.mb, name, args)


class TestAddressRefusal(Roots, unittest.TestCase):
    def test_profile_refuses_other_address(self):
        with self.assertRaises(pm.Refused):
            pm.load_config("a", self.env("a", PROTON_MCP_USERNAME="someone@example.com"))

    def test_username_comes_from_profiles_file(self):
        self.assertEqual(pm.load_config("a", self.env("a"))["username"], A_ADDR)
        self.assertEqual(pm.load_config("a", self.env("a", PROTON_MCP_USERNAME="ORDERS@example.com"))
                         ["username"], A_ADDR)
        self.assertEqual(pm.load_config("b", self.env("b"))["username"], B_ADDR)

    def test_main_refuses_to_start(self):
        old = {k: os.environ.get(k) for k in ("PROTON_MCP_USERNAME", "PROTON_MCP_CONFIG")}
        os.environ["PROTON_MCP_USERNAME"] = "someone@example.com"
        os.environ["PROTON_MCP_CONFIG"] = self.config_path
        try:
            self.assertEqual(pm.main(["--profile", "a"]), 3)
            self.assertEqual(pm.main(["--instance", "a"]), 2)
        finally:
            for k, v in old.items():
                if v is None:
                    os.environ.pop(k, None)
                else:
                    os.environ[k] = v

    def test_unknown_profile(self):
        for name in ("c", "A", "../a", "", "a b"):
            with self.assertRaises(pm.Refused, msg=name):
                pm.load_config(name, self.env("a"))

    def test_host_is_loopback_only(self):
        self.assertEqual(pm.load_config("a", self.env("a", PROTON_MCP_HOST="10.0.0.1"))["host"],
                         "127.0.0.1")

    def test_login_uses_configured_address(self):
        with FakeIMAP() as fake:
            cfg = pm.load_config("a", self.env("a", PROTON_MCP_IMAP_PORT=str(fake.port),
                                               PROTON_MCP_TEST_PLAIN="1"))
            pm.call_tool(pm.Mailbox(cfg, password_reader=lambda c: TEST_PW), "list_folders", {})
            self.assertEqual(fake.state.logins, [A_ADDR])


class TestProfilesFile(Roots, unittest.TestCase):
    def test_good_file(self):
        cfg = pm.load_config("b", self.env("b"))
        self.assertEqual(cfg["username"], B_ADDR)
        self.assertEqual(cfg["other_usernames"], [A_ADDR])
        self.assertEqual(cfg["ledger"], os.path.join(self.state, "shared", "TAPS-USED"))

    def test_missing_file(self):
        with self.assertRaises(pm.Refused):
            pm.load_config("b", self.env("b", PROTON_MCP_CONFIG=os.path.join(self.tmp.name, "nope")))

    def test_loose_permissions(self):
        for mode in (0o644, 0o640, 0o660, 0o700):
            p = self.write_config(mode=mode, path=os.path.join(self.tmp.name, "p%o.json" % mode))
            with self.assertRaises(pm.Refused, msg=oct(mode)):
                pm.load_config("b", self.env("b", PROTON_MCP_CONFIG=p))

    def test_symlink_refused(self):
        link = os.path.join(self.tmp.name, "link.json")
        os.symlink(self.config_path, link)
        with self.assertRaises(pm.Refused):
            pm.load_config("b", self.env("b", PROTON_MCP_CONFIG=link))

    def test_refused_domain_bad_and_duplicate_addresses(self):
        cases = []
        for value in ("other@Example.com", "", "not-an-address", "a@b@c"):
            pr = default_profiles(self.sig_path)
            pr["b"]["username"] = value
            cases.append(pr)
        pr = default_profiles(self.sig_path)
        pr["b"]["username"] = A_ADDR  # two profiles, one address
        cases.append(pr)
        for i, pr in enumerate(cases):
            p = self.write_config(profiles=pr, path=os.path.join(self.tmp.name, "c%d.json" % i))
            with self.assertRaises(pm.Refused, msg=pr["b"]["username"]):
                pm.load_config("b", self.env("b", PROTON_MCP_CONFIG=p))

    def test_bad_json_and_shape(self):
        p = os.path.join(self.tmp.name, "bad.json")
        for body in ("{", "[]", '{"profiles": {}}', '{"profiles": {"Bad Name": {"username": "x@y.z"}}}',
                     '{"profiles": {"a": {"username": "x@y.z", "from_name": "x\\ny"}}}',
                     '{"profiles": {"a": {"username": "x@y.z"}}, "timezone": "Not/AZone"}'):
            with open(p, "w") as fh:
                fh.write(body)
            os.chmod(p, 0o600)
            with self.assertRaises(pm.Refused, msg=body):
                pm.load_config("a", self.env("a", PROTON_MCP_CONFIG=p))

    def test_from_name_plain_only(self):
        for bad in ("Example\\Org", "Example \u202eOrg", "Example\u200bOrg", "Example\u0007Org", "Ex\tample",
                    "Ex\x7fample", 'Ex"ample', "Ex<ample>"):
            pr = default_profiles(self.sig_path)
            pr["a"]["from_name"] = bad
            p = self.write_config(profiles=pr, path=os.path.join(self.tmp.name, "fn.json"))
            with self.assertRaises(pm.Refused, msg=repr(bad)):
                pm.load_config("a", self.env("a", PROTON_MCP_CONFIG=p))
        pr = default_profiles(self.sig_path)
        pr["a"]["from_name"] = "Example Org, Ltd. & Co."
        p = self.write_config(profiles=pr, path=os.path.join(self.tmp.name, "fn.json"))
        self.assertEqual(pm.load_config("a", self.env("a", PROTON_MCP_CONFIG=p))["from_name"], "Example Org, Ltd. & Co.")

    def test_sending_needs_absolute_state_dir(self):
        for state_dir in (None, "", "relative/state", "~/state", "./state"):
            top = {} if state_dir is None else {"state_dir": state_dir}
            path = os.path.join(self.tmp.name, "sd.json")
            data = {"timezone": "UTC", "profiles": default_profiles(self.sig_path)}
            data.update(top)
            with open(path, "w") as fh:
                json.dump(data, fh)
            os.chmod(path, 0o600)
            for prof in ("a", "b"):
                with self.assertRaises(pm.Refused, msg=(state_dir, prof)) as cm:
                    pm.load_config(prof, {"PROTON_MCP_CONFIG": path, "XDG_STATE_HOME": self.tmp.name})
                self.assertIn("state_dir must be an absolute path", str(cm.exception))
        # with sending off everywhere, the XDG fallback is still allowed
        pr = default_profiles(self.sig_path)
        pr["a"]["send_enabled"] = pr["b"]["send_enabled"] = False
        path = os.path.join(self.tmp.name, "nosend.json")
        with open(path, "w") as fh:
            json.dump({"profiles": pr}, fh)
        os.chmod(path, 0o600)
        cfg = pm.load_config("a", {"PROTON_MCP_CONFIG": path, "XDG_STATE_HOME": self.tmp.name})
        self.assertEqual(cfg["root"], os.path.join(self.tmp.name, "proton-bridge-mcp", "a"))

    def test_send_off_by_default(self):
        pr = {"a": {"username": A_ADDR}}
        p = self.write_config(profiles=pr, path=os.path.join(self.tmp.name, "min.json"))
        cfg = pm.load_config("a", self.env("a", PROTON_MCP_CONFIG=p))
        self.assertFalse(cfg["send_enabled"])
        self.assertTrue(cfg["hide_login_in_review"])
        self.assertIsNone(cfg["signature_file"])

    def test_example_file_loads(self):
        import shutil
        p = os.path.join(self.tmp.name, "example.json")
        shutil.copy(os.path.join(os.path.dirname(HERE), "profiles.example.json"), p)
        os.chmod(p, 0o600)
        for name in ("a", "b"):
            cfg = pm.load_config(name, {"PROTON_MCP_CONFIG": p, "XDG_STATE_HOME": self.tmp.name})
            self.assertFalse(cfg["send_enabled"])  # the example ships with sending off

    def test_default_paths_follow_xdg(self):
        env = {"XDG_CONFIG_HOME": "/x/cfg", "XDG_STATE_HOME": "/x/state"}
        self.assertEqual(pm.default_config_path(env), "/x/cfg/proton-bridge-mcp/profiles.json")
        self.assertEqual(pm.default_state_dir(env), "/x/state/proton-bridge-mcp")


class TestConfigGuards(Roots, unittest.TestCase):
    def test_log_dirs_must_stay_on_own_side(self):
        bad = [("a", self.roots["b"]), ("b", self.roots["a"]), ("a", "/tmp"),
               ("a", os.path.join(self.roots["a"], "..", "b"))]
        for inst, d in bad:
            for key in ("PROTON_MCP_LOG_DIR", "PROTON_MCP_ERR_DIR"):
                with self.assertRaises(pm.Refused, msg=(inst, key, d)):
                    pm.load_config(inst, self.env(inst, **{key: d}))

    def test_default_dirs_are_under_profile_root(self):
        for inst in ("a", "b"):
            cfg = pm.load_config(inst, {"PROTON_MCP_CONFIG": self.config_path})
            for d in (cfg["log_dir"], cfg["err_dir"]):
                self.assertTrue(pm._under(d, self.roots[inst]), d)
            self.assertFalse(pm._under(cfg["ledger"], self.roots[inst]))

    def test_test_plain_needs_harness(self):
        pm._TEST_HARNESS = False
        cfg = pm.load_config("a", self.env("a", PROTON_MCP_TEST_PLAIN="1"))
        self.assertFalse(cfg["plain_for_tests"])
        pm._TEST_HARNESS = True
        cfg = pm.load_config("a", self.env("a", PROTON_MCP_TEST_PLAIN="1"))
        self.assertTrue(cfg["plain_for_tests"])

    def test_allow_trash_is_code_constant(self):
        self.assertIs(pm.ALLOW_TRASH, True)
        src = open(SRC).read()
        self.assertNotRegex(src, r"environ[^\n]*TRASH|get\([\"']PROTON_MCP_[A-Z_]*TRASH|[\"']allow_trash[\"']")


class TestTools(Base):
    def test_list_and_search_and_read(self):
        ok, p = self.call("list_folders", {})
        self.assertTrue(ok)
        self.assertIn("Drafts", p["folders"])
        ok, p = self.call("search", {"folder": "INBOX", "subject": "Quarterly"})
        self.assertTrue(ok)
        self.assertEqual(p["messages"][0]["uid"], self.uid)
        ok, p = self.call("read_message", {"folder": "INBOX", "uid": self.uid})
        self.assertTrue(ok)
        self.assertIn("Body line one.", p["text"])

    def test_search_newest_first_by_date_not_uid(self):
        # Bridge's first sync gives UIDs out of date order; search must sort by arrival date.
        def m(day, subj):
            return ("Date: %s Jan 2030 08:00:00 +0000\r\nFrom: a@example.com\r\nTo: orders@example.com\r\n"
                    "Subject: %s\r\nMessage-ID: <%s@x>\r\n\r\nbody\r\n" % (day, subj, subj)).encode()
        st = self.fake.state
        u_new = st.add("Archive", m("12", "Newest"))   # lowest UID, newest date
        u_old = st.add("Archive", m("01", "Oldest"))
        u_mid = st.add("Archive", m("06", "Middle"))  # highest UID, middle date
        ok, p = self.call("search", {"folder": "Archive", "limit": 2})
        self.assertTrue(ok)
        self.assertEqual([x["uid"] for x in p["messages"]], [u_new, u_mid])
        ok, p = self.call("search", {"folder": "Archive"})
        self.assertEqual([x["uid"] for x in p["messages"]], [u_new, u_mid, u_old])
        logs = self.logs()
        for word in ("Newest", "Oldest", "Middle", "a@example.com"):
            self.assertNotIn(word, logs)

    def test_non_ascii_search(self):
        u2 = self.fake.state.add("INBOX", sample(subject="Überprüfung Bericht"))
        ok, p = self.call("search", {"folder": "INBOX", "subject": "Überprüfung"})
        self.assertTrue(ok, p)
        self.assertEqual([m["uid"] for m in p["messages"]], [u2])
        cmd = [a for v, a in self.fake.state.commands if v == "UID SEARCH"][-1]
        self.assertIn("CHARSET UTF-8", cmd)
        ok, p = self.call("search", {"subject": "Überprüfung", "text": "Straße"})
        self.assertFalse(ok)
        self.assertIn("only one non-ASCII", p["error"])

    def test_target_folder_refusals(self):
        for target in ("Drafts", "Sent", "Outbox", "Scheduled", "All Mail", "sent", "/Drafts/", "ALL MAIL",
                       "All Sent", "all drafts", "All"):
            ok, p = self.call("move_message", {"folder": "INBOX", "uids": [self.uid], "target": target})
            self.assertFalse(ok, target)
            self.assertIn("refused", p["error"])
        self.assertFalse(any(v == "UID MOVE" for v, _ in self.fake.state.commands))
        self.assertIn(self.uid, self.fake.state.folders["INBOX"])

    def test_source_folder_refusals(self):
        for src in ("Drafts", "Sent", "Outbox", "Scheduled", "All Mail", "/sent/", "ALL MAIL", "All Archive"):
            ok, p = self.call("move_message", {"folder": src, "uids": [5], "target": "Archive"})
            self.assertFalse(ok, src)
            self.assertIn("source folder not allowed", p["error"])
        self.assertFalse(any(v in ("UID MOVE", "SELECT") for v, _ in self.fake.state.commands))

    def test_trash_move_allowed(self):
        ok, p = self.call("move_message", {"folder": "INBOX", "uids": [self.uid], "target": "Trash"})
        self.assertTrue(ok)
        self.assertIn(self.uid, self.fake.state.folders["Trash"])

    def test_trash_refused_when_constant_off(self):
        pm.ALLOW_TRASH = False
        try:
            ok, p = self.call("move_message", {"folder": "INBOX", "uids": [self.uid], "target": "Trash"})
            self.assertFalse(ok)
        finally:
            pm.ALLOW_TRASH = True

    def test_label(self):
        ok, p = self.call("set_label", {"folder": "INBOX", "uids": [self.uid], "label": "Client"})
        self.assertTrue(ok)
        self.assertIn(self.uid, self.fake.state.folders["Labels/Client"])

    def test_no_expunge_store_or_close_on_the_wire(self):
        self.call("list_folders", {})
        self.call("search", {})
        self.call("read_message", {"uid": self.uid})
        self.call("move_message", {"uids": [self.uid], "target": "Trash"})
        self.call("save_draft", {"to": ["a@example.com"], "subject": "s", "body": "b"})
        verbs = {v for v, _ in self.fake.state.commands}
        for bad in ("EXPUNGE", "UID EXPUNGE", "STORE", "UID STORE", "CLOSE", "DELETE"):
            self.assertNotIn(bad, verbs)

    def test_log_format_and_no_content(self):
        self.call("list_folders", {})
        self.call("search", {"subject": "Quarterly"})
        self.call("read_message", {"uid": self.uid})
        self.call("move_message", {"uids": [self.uid], "target": "Sent"})
        self.call("move_message", {"uids": [self.uid], "target": "Archive"})
        self.call("no_such_tool_with_secret_name", {})
        text = self.logs()
        lines = [l for l in text.splitlines() if l.strip()]
        self.assertEqual(len(lines), 6)
        for l in lines:
            self.assertRegex(l, LOG_RE)
        self.assertIn("| unknown | 0 | error:UnknownTool", text)
        for secret in (SUBJECT, "Quarterly", "client@example.com", "Body line", TEST_PW, "orders@", "secret_name"):
            self.assertNotIn(secret, text)

    def test_errors_log_type_only(self):
        mb = pm.Mailbox(self.cfg, password_reader=lambda cfg: (_ for _ in ()).throw(RuntimeError("leaky detail 42")))
        ok, p = pm.call_tool(mb, "list_folders", {})
        self.assertFalse(ok)
        self.assertEqual(p, {"error": "RuntimeError"})
        text = self.logs()
        self.assertIn("RuntimeError", text)
        self.assertNotIn("leaky detail", text)


class TestFromRuleProfileA(Base):
    def test_a_from_fixed(self):
        ok, p = self.call("save_draft", {"to": ["a@example.com"], "subject": "Re: x", "body": "hi"})
        self.assertTrue(ok)
        self.assertEqual(p["saved"], "DRAFT, not sent")
        drafts = list(self.fake.state.folders["Drafts"].values())
        self.assertEqual(len(drafts), 1)
        self.assertIn(b"From: Example Org LLC <orders@example.com>", drafts[0])
        appends = [a for v, a in self.fake.state.commands if v == "APPEND"]
        self.assertTrue(all(a.startswith('"Drafts"') for a in appends))

    def test_a_refuses_other_from(self):
        ok, p = self.call("save_draft", {"to": ["a@example.com"], "subject": "x", "body": "y",
                                         "from": "other@example.net"})
        self.assertFalse(ok)
        self.assertEqual(self.fake.state.folders["Drafts"], {})

    def test_header_injection_refused(self):
        for args in ({"to": ["a@example.com\r\nBcc: x@y.z"]}, {"to": ["a@example.com"], "subject": "x\nBcc: q"},
                     {"to": ["a@example.com"], "in_reply_to": "<m@x>\r\nBcc: q"},
                     {"to": ["a@example.com"], "in_reply_to": "<m@x>", "references": "<m@x>\r\nBcc: q"}):
            a = dict({"subject": "x", "body": "y"}, **args)
            ok, p = self.call("save_draft", a)
            self.assertFalse(ok, args)
        self.assertEqual(self.fake.state.folders["Drafts"], {})


class TestFromRuleProfileB(Base):
    instance = "b"

    def test_b_from_is_login(self):
        ok, p = self.call("save_draft", {"to": ["a@example.com"], "subject": "x", "body": "y"})
        self.assertTrue(ok)
        raw = list(self.fake.state.folders["Drafts"].values())[0]
        self.assertIn(b"From: me@example.org", raw)
        self.assertEqual(self.fake.state.logins, [B_ADDR])

    def test_b_refuses_info(self):
        ok, p = self.call("save_draft", {"to": ["a@example.com"], "subject": "x", "body": "y",
                                         "from": "orders@example.com"})
        self.assertFalse(ok)


class TestNoSendNoDelete(unittest.TestCase):
    """SMTP, port 1025 and message sending exist only inside tool_send_draft; no delete anywhere."""

    def setUp(self):
        self.src = open(SRC).read()
        self.tree = ast.parse(self.src)
        self.send_fn = [n for n in self.tree.body if isinstance(n, ast.FunctionDef) and n.name == "tool_send_draft"]
        self.inside = {id(x) for f in self.send_fn for x in ast.walk(f)}

    def outside(self):
        return [n for n in ast.walk(self.tree) if id(n) not in self.inside]

    def test_send_function_exists_once(self):
        self.assertEqual(len(self.send_fn), 1)

    def test_smtp_only_inside_send_draft(self):
        for node in self.outside():
            if isinstance(node, (ast.Import, ast.ImportFrom)):
                names = [a.name for a in node.names] + [getattr(node, "module", "") or ""]
                for n in names:
                    self.assertNotIn("smtp", n.lower())
                    self.assertNotIn("socket", n.lower())
            if isinstance(node, ast.Name):
                self.assertNotIn("smtp", node.id.lower())
            if isinstance(node, ast.Attribute):
                self.assertNotIn(node.attr.lower(), {"sendmail", "send_message", "smtp_ssl", "lmtp"})  # email.policy.SMTP is a format, not smtplib
            if isinstance(node, ast.Constant) and isinstance(node.value, (str, int)) and not isinstance(node.value, bool):
                self.assertNotIn("1025", str(node.value))
                if isinstance(node.value, str) and node.value not in self.docstrings():
                    self.assertNotIn("smtplib", node.value)
        inside_src = ast.get_source_segment(self.src, self.send_fn[0])
        self.assertIn("import smtplib", inside_src)
        self.assertIn("1025", inside_src)
        self.assertEqual(self.src.count("import smtplib"), 1)

    def docstrings(self):
        out = set()
        for n in ast.walk(self.tree):
            if isinstance(n, (ast.Module, ast.FunctionDef, ast.ClassDef)) and n.body and \
                    isinstance(n.body[0], ast.Expr) and isinstance(n.body[0].value, ast.Constant):
                out.add(n.body[0].value.value)
        return out

    def test_send_draft_only_reachable_from_tool_table(self):
        refs = [n for n in ast.walk(self.tree) if isinstance(n, ast.Name) and n.id == "tool_send_draft"]
        self.assertEqual(len(refs), 1)  # the TOOLS entry; no other caller, no auto-send path
        tools = [n for n in self.tree.body if isinstance(n, ast.Assign) and
                 any(isinstance(t, ast.Name) and t.id == "TOOLS" for t in n.targets)][0]
        self.assertIn(id(refs[0]), {id(x) for x in ast.walk(tools)})

    def test_send_constants_are_code_not_env(self):
        self.assertEqual(pm.SEND_DAILY_CAP, 10)
        self.assertEqual(pm.SEND_REVIEW_TTL, 1800)
        self.assertEqual(pm.SEND_MAX_RECIPIENTS, 20)
        self.assertNotRegex(self.src, r"environ[^\n]*(CAP|TTL|VIEWS|SIGNATURE|SEND)|PROTON_MCP_[A-Z_]*(CAP|TTL|VIEWS|SIGNATURE|SEND)")
        # The profiles file may switch sending on or off per profile, but never sets the cap, TTL or limits.
        self.assertNotRegex(self.src, r"p\.get\([\"'](daily_cap|cap|ttl|review_ttl|max_recipients|max_body)[\"']")

    def test_no_expunge_store_close_delete_calls(self):
        banned = {"expunge", "store", "close", "delete", "sendmail", "send_message", "deleteacl"}
        docs = set()  # docstring nodes are prose, not code
        for n in ast.walk(self.tree):
            if isinstance(n, (ast.Module, ast.FunctionDef, ast.ClassDef)) and n.body and \
                    isinstance(n.body[0], ast.Expr) and isinstance(n.body[0].value, ast.Constant):
                docs.add(id(n.body[0].value))
        for node in ast.walk(self.tree):
            if id(node) in docs:
                continue
            if isinstance(node, ast.Attribute):
                allowed = {"send_message"} if id(node) in self.inside else set()
                if node.attr.lower() not in allowed:
                    self.assertNotIn(node.attr.lower(), banned, node.attr)
            if isinstance(node, ast.Constant) and isinstance(node.value, str):
                v = node.value.upper()
                for bad in ("EXPUNGE", "\\DELETED", "STORE"):
                    if bad in v:
                        self.fail("banned string: %s" % bad)

    def test_send_draft_holds_both_locks(self):
        """The profile lock and the global tap lock wrap the whole send, in that order."""
        inside_src = ast.get_source_segment(self.src, self.send_fn[0])
        self.assertIn('with _view_lock(cfg), _taps_lock(cfg["ledger"]):', inside_src)
        self.assertEqual(inside_src.count("_taps_lock("), 1)
        self.assertIn('_ledger_ready(cfg["ledger"])', inside_src)

    def test_tool_list(self):
        self.assertEqual(set(pm.TOOLS), {"list_folders", "search", "read_message", "move_message",
                                         "set_label", "save_draft", "prepare_send", "send_draft"})


class TestStdio(Roots, unittest.TestCase):
    def test_initialize_and_list(self):
        cfg = pm.load_config("a", self.env("a"))
        reqs = [{"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": "2025-06-18"}},
                {"jsonrpc": "2.0", "method": "notifications/initialized"},
                {"jsonrpc": "2.0", "id": 2, "method": "tools/list"}]
        out = io.StringIO()
        pm.serve(cfg, io.StringIO("\n".join(json.dumps(r) for r in reqs) + "\n"), out)
        resp = [json.loads(l) for l in out.getvalue().splitlines()]
        self.assertEqual(resp[0]["result"]["serverInfo"]["name"], "proton-a")
        tools = {t["name"]: t for t in resp[1]["result"]["tools"]}
        self.assertEqual(len(tools), 8)
        self.assertEqual({n for n in tools if "send" in n}, {"prepare_send", "send_draft"})
        self.assertIn("Sends real email. Call this only after the human's own approval of that exact review.",
                      tools["send_draft"]["description"])
        self.assertIn("Sends nothing.", tools["prepare_send"]["description"])
        self.assertIn("only after the human approved", resp[0]["result"]["instructions"])



SEND_SUBJ = "Bridge send secret subject 4417"
SEND_BODY = "Private body words 9931.\nSecond line."
SEND_LOG_RE = re.compile(r"^\d{4}-\d{2}-\d{2} \d{2}:\d{2} [+-]\d{4} \| (a|b) send_draft \| draft uid "
                         r"(\d+|none) \| hash ([0-9a-f]{12}|none) \| \d+ recipients \| "
                         r"tap (appr-[a-z0-9-]+|none) \(cited by caller, not proof\) \| "
                         r"(sent|sent:partial|refused:[a-z-]+|error:[A-Za-z_][A-Za-z0-9_]*)$")
TAP = "appr-772-3130"
TEST_SIG = "Example Org LLC\nTest signature line"
AUTO = object()


def raw_draft(frm="Example Org LLC <orders@example.com>", to="Ann Client <ann@example.com>",
              cc="bob@example.net", subject=SEND_SUBJ, body=SEND_BODY, extra=""):
    h = "From: %s\r\nTo: %s\r\n" % (frm, to) if frm else "To: %s\r\n" % to
    if cc:
        h += "Cc: %s\r\n" % cc
    h += extra
    h += ("Subject: %s\r\nDate: Thu, 03 Jan 2030 09:00:00 +0000\r\nMessage-ID: <d1@example.com>\r\n"
          "MIME-Version: 1.0\r\nContent-Type: text/plain; charset=utf-8\r\n\r\n" % subject)
    return (h + body.replace("\n", "\r\n") + "\r\n").encode("utf-8")


def raw_with_attachment():
    from email.message import EmailMessage
    m = EmailMessage()
    m["From"] = "orders@example.com"
    m["To"] = "ann@example.com"
    m["Subject"] = SEND_SUBJ
    m.set_content(SEND_BODY)
    m.add_attachment(b"%PDF-1.4 fake", maintype="application", subtype="pdf", filename="file.pdf")
    return m.as_bytes(policy=pm.email.policy.SMTP)


class SendBase(Base):
    def setUp(self):
        Base.setUp(self)
        from fake_smtp import FakeSMTP
        self.smtp = FakeSMTP().__enter__()
        self.cfg = pm.load_config(self.instance, self.env(self.instance, PROTON_MCP_IMAP_PORT=str(self.fake.port),
                                                          PROTON_MCP_SMTP_PORT=str(self.smtp.port),
                                                          PROTON_MCP_TEST_PLAIN="1"))
        self.mb = pm.Mailbox(self.cfg, password_reader=lambda cfg: TEST_PW)
        self.set_sig(TEST_SIG)  # profile a's signature_file (see default_profiles)
        self.assertEqual(self.cfg["ledger"], os.path.join(self.state, "shared", "TAPS-USED"))
        self.assertIsNone(pm.init_taps_ledger(self.cfg["ledger"]))  # install step; the send path never creates it
        self.tapn = 0

    def set_sig(self, text):
        with open(self.sig_path, "w", encoding="utf-8") as fh:
            fh.write(text + "\n")

    def tearDown(self):
        self.smtp.__exit__()
        Base.tearDown(self)

    def draft(self, **kw):
        if self.instance == "b" and "frm" not in kw:
            kw["frm"] = B_ADDR
        return self.fake.state.add("Drafts", raw_draft(**kw))

    def prepare(self, uid):
        ok, p = self.call("prepare_send", {"draft_uid": uid})
        self.assertTrue(ok, p)
        return p

    def send(self, uid, sha, tap=AUTO):
        if tap is AUTO:  # a fresh tap id per send, since each tap may send only once
            self.tapn += 1
            tap = "appr-9%03d-1" % self.tapn
        return self.call("send_draft", {"draft_uid": uid, "sha256": sha, "tap_ref": tap})

    def send_lines(self):
        d = self.cfg["log_dir"]
        files = sorted(os.path.join(d, f) for f in os.listdir(d) if f.startswith("SEND-LOG-")) if os.path.isdir(d) else []
        return [l for f in files for l in open(f).read().splitlines() if l.strip()]

    def all_text(self):
        out = [self.logs()]
        for root, _, files in os.walk(self.cfg["log_dir"]):
            for f in files:
                out.append(open(os.path.join(root, f)).read())
        return "\n".join(out)


class TestSendProfileA(SendBase):
    def test_send_only_after_prepare(self):
        uid = self.draft()
        ok, p = self.send(uid, "a" * 64)
        self.assertFalse(ok)
        self.assertIn("no open review", p["error"])
        self.assertEqual(self.smtp.state.messages, [])
        rv = self.prepare(uid)
        self.assertEqual(self.smtp.state.messages, [])  # prepare sends nothing
        self.assertIn("Nothing is sent yet", rv["review"])
        for part in (SEND_SUBJ, "Private body words 9931.", "ann@example.com", "bob@example.net", rv["sha256"]):
            self.assertIn(part, rv["review"])
        ok, p = self.send(uid, rv["sha256"])
        self.assertTrue(ok, p)
        self.assertEqual(p["status"], "SENT")
        self.assertEqual(len(self.smtp.state.messages), 1)
        m = self.smtp.state.messages[0]
        self.assertEqual(m["mail_from"], "orders@example.com")
        self.assertEqual(m["rcpts"], ["ann@example.com", "bob@example.net"])  # exactly To + Cc, nothing else
        self.assertEqual(self.smtp.state.logins, ["orders@example.com"])
        sent = pm.email.message_from_bytes(m["data"], policy=pm.email.policy.default)
        self.assertIn(b'\nFrom: "Example Org LLC" <orders@example.com>\r\n', b"\n" + m["data"])
        self.assertEqual(str(sent["Subject"]), SEND_SUBJ)
        self.assertIsNone(sent["Bcc"])
        self.assertEqual(sent.get_content().replace("\r\n", "\n").rstrip("\n"), SEND_BODY + "\n\n" + TEST_SIG)

    def test_hash_given_does_not_match_review(self):
        uid = self.draft()
        rv = self.prepare(uid)
        bad = ("0" if rv["sha256"][0] != "0" else "1") + rv["sha256"][1:]
        ok, p = self.send(uid, bad)
        self.assertFalse(ok)
        self.assertIn("hash does not match", p["error"])
        self.assertEqual(self.smtp.state.messages, [])
        ok, p = self.send(uid, rv["sha256"])  # the failed try used up the review
        self.assertFalse(ok)
        self.assertEqual(self.smtp.state.messages, [])

    def test_draft_edited_between_prepare_and_send(self):
        for change in ({"body": SEND_BODY + " extra"}, {"subject": SEND_SUBJ + "!"}, {"cc": "eve@example.org"},
                       {"to": "Ann Client <ann@example.net>"}, {"extra": "In-Reply-To: <x@y>\r\n"}):
            uid = self.draft()
            rv = self.prepare(uid)
            self.fake.state.folders["Drafts"][uid] = raw_draft(**change)
            ok, p = self.send(uid, rv["sha256"])
            self.assertFalse(ok, change)
            self.assertIn("draft changed", p["error"])
        self.assertEqual(self.smtp.state.messages, [])

    def test_expired_review_refused(self):
        uid = self.draft()
        rv = self.prepare(uid)
        path = pm._pending_path(self.cfg, uid)
        rec = json.load(open(path))
        rec["created"] -= pm.SEND_REVIEW_TTL + 5
        json.dump(rec, open(path, "w"))
        ok, p = self.send(uid, rv["sha256"])
        self.assertFalse(ok)
        self.assertIn("older than 30 minutes", p["error"])
        self.assertEqual(self.smtp.state.messages, [])

    def test_review_is_single_use(self):
        uid = self.draft()
        rv = self.prepare(uid)
        self.assertTrue(self.send(uid, rv["sha256"])[0])
        os.rename(os.path.join(self.cfg["log_dir"], [f for f in os.listdir(self.cfg["log_dir"])
                                                     if f.startswith("SEND-LOG-")][0]),
                  os.path.join(self.cfg["log_dir"], "moved.md"))  # hide the log so only the record guard is left
        ok, p = self.send(uid, rv["sha256"])
        self.assertFalse(ok)
        self.assertIn("no open review", p["error"])
        self.assertEqual(len(self.smtp.state.messages), 1)

    def test_attachment_refused(self):
        uid = self.fake.state.add("Drafts", raw_with_attachment())
        ok, p = self.call("prepare_send", {"draft_uid": uid})
        self.assertFalse(ok)
        self.assertIn("attachments are not allowed", p["error"])
        uid2 = self.draft()  # attachment added after the review
        rv = self.prepare(uid2)
        self.fake.state.folders["Drafts"][uid2] = raw_with_attachment()
        ok, p = self.send(uid2, rv["sha256"])
        self.assertFalse(ok)
        self.assertIn("attachments are not allowed", p["error"])
        self.assertEqual(self.smtp.state.messages, [])

    def test_html_or_multipart_refused(self):
        raw = raw_draft().replace(b"text/plain", b"text/html")
        uid = self.fake.state.add("Drafts", raw)
        ok, p = self.call("prepare_send", {"draft_uid": uid})
        self.assertFalse(ok)
        self.assertIn("plain-text", p["error"])

    def test_bcc_refused(self):
        uid = self.draft(extra="Bcc: hidden@example.com\r\n")
        ok, p = self.call("prepare_send", {"draft_uid": uid})
        self.assertFalse(ok)
        self.assertIn("Bcc is not allowed", p["error"])
        self.assertFalse(os.path.exists(pm._pending_path(self.cfg, uid)))

    def test_from_forced_or_refused_a(self):
        uid = self.draft(frm="")  # no From: forced to the profile's own address
        rv = self.prepare(uid)
        self.assertIn('From: "Example Org LLC" <orders@example.com>', rv["review"])
        self.assertTrue(self.send(uid, rv["sha256"])[0])
        self.assertEqual(self.smtp.state.messages[0]["mail_from"], "orders@example.com")
        for frm in ("someone@example.com", "Someone <me@example.org>", "orders@example.com, x@example.com"):
            uid = self.draft(frm=frm)
            ok, p = self.call("prepare_send", {"draft_uid": uid})
            self.assertFalse(ok, frm)
            self.assertRegex(p["error"], "login address only|another profile")

    def test_a_from_display_name_exact(self):
        for frm in ("", "orders@example.com", '"Example Org LLC" <orders@example.com>'):
            uid = self.draft(frm=frm)
            rv = self.prepare(uid)
            self.assertIn('From: "Example Org LLC" <orders@example.com>', rv["review"])
            self.assertTrue(self.send(uid, rv["sha256"])[0], frm)
            data = self.smtp.state.messages[-1]["data"]
            self.assertIn(b'From: "Example Org LLC" <orders@example.com>\r\n', data)
        for frm in ("Someone <orders@example.com>", "Example Org <orders@example.com>",
                    '"Example Org LLC, Inc" <orders@example.com>'):
            uid = self.draft(frm=frm)
            ok, p = self.call("prepare_send", {"draft_uid": uid})
            self.assertFalse(ok, frm)
            self.assertIn("configured from_name", p["error"])

    def test_empty_to_and_bad_addresses_refused(self):
        for kw, word in (({"to": ""}, "no To"), ({"to": "not-an-address"}, "not valid"),
                         ({"cc": ", ".join("p%d@example.com" % i for i in range(20))}, "too many")):
            uid = self.draft(**kw)
            ok, p = self.call("prepare_send", {"draft_uid": uid})
            self.assertFalse(ok, kw)
            self.assertIn(word, p["error"])

    def test_daily_cap(self):
        old = pm.SEND_DAILY_CAP
        pm.SEND_DAILY_CAP = 2
        try:
            for _ in range(2):
                uid = self.draft()
                self.assertTrue(self.send(uid, self.prepare(uid)["sha256"])[0])
            uid = self.draft()
            rv = self.prepare(uid)
            self.assertEqual(rv["sends_left_today"], 0)
            ok, p = self.send(uid, rv["sha256"])
            self.assertFalse(ok)
            self.assertIn("daily send cap", p["error"])
            self.assertTrue(os.path.exists(pm._pending_path(self.cfg, uid)))  # not consumed by a cap refusal
            self.assertEqual(len(self.smtp.state.messages), 2)
        finally:
            pm.SEND_DAILY_CAP = old

    def test_non_drafts_refused(self):
        ok, p = self.call("prepare_send", {"draft_uid": self.uid})  # self.uid is in INBOX
        self.assertFalse(ok)
        self.assertIn("in Drafts", p["error"])
        uid = self.draft()
        ok, p = self.call("prepare_send", {"draft_uid": uid, "folder": "INBOX"})
        self.assertFalse(ok)
        ok, p = self.call("send_draft", {"draft_uid": uid, "sha256": "a" * 64, "tap_ref": TAP, "folder": "Sent"})
        self.assertFalse(ok)
        selects = [a for v, a in self.fake.state.commands if v in ("SELECT", "EXAMINE")]
        self.assertTrue(selects and all(a.startswith('"Drafts"') for a in selects), selects)
        self.assertEqual(self.smtp.state.messages, [])

    def test_tap_ref_required(self):
        uid = self.draft()
        rv = self.prepare(uid)
        for tap in (None, "", "ab", "appr-772-3130 and more", "-appr", "a" * 65, "appr/1", 7):
            ok, p = self.call("send_draft", {"draft_uid": uid, "sha256": rv["sha256"], "tap_ref": tap})
            self.assertFalse(ok, tap)
            self.assertIn("tap_ref", p["error"])
        self.assertTrue(os.path.exists(pm._pending_path(self.cfg, uid)))  # bad input does not use up the review
        self.assertEqual(self.smtp.state.messages, [])

    def test_pending_record_is_private_and_content_free(self):
        uid = self.draft()
        rv = self.prepare(uid)
        path = pm._pending_path(self.cfg, uid)
        self.assertEqual(os.stat(path).st_mode & 0o777, 0o600)
        self.assertEqual(os.stat(os.path.dirname(path)).st_mode & 0o777, 0o700)
        self.assertTrue(pm._under(path, self.roots["a"]))
        self.assertEqual(set(json.load(open(path))), {"uid", "sha256", "created", "view"})

    def test_send_log_line_and_no_sensitive_content(self):
        uid = self.draft()
        rv = self.prepare(uid)
        self.send(uid, "b" * 64)                    # refused: hash mismatch
        uid = self.draft()
        rv = self.prepare(uid)
        self.assertTrue(self.send(uid, rv["sha256"], tap=TAP)[0])
        self.call("send_draft", {"draft_uid": uid, "sha256": rv["sha256"], "tap_ref": "bad\nline"})
        lines = self.send_lines()
        self.assertEqual(len(lines), 3, lines)
        for l in lines:
            self.assertRegex(l, SEND_LOG_RE)
        self.assertTrue(lines[1].endswith("| sent"))
        self.assertIn("hash %s" % rv["sha256"][:12], lines[1])
        self.assertIn("| 2 recipients |", lines[1])
        self.assertIn("tap appr-772-3130 (cited by caller, not proof)", lines[1])
        name = os.path.basename([f for f in os.listdir(self.cfg["log_dir"]) if f.startswith("SEND-LOG-")][0])
        self.assertRegex(name, r"^SEND-LOG-\d{4}-\d{2}\.md$")
        text = self.all_text()
        for secret in (SEND_SUBJ, "4417", "Private body", "ann@", "bob@", "example.com", "example.net",
                       "Ann Client", TEST_PW, "orders@", rv["sha256"][12:]):
            self.assertNotIn(secret, text)
        for l in self.logs().splitlines():
            if l.strip() and not pm._SEND_LINE.match(l):  # call-log lines; SEND-LOG lines checked above
                self.assertRegex(l, LOG_RE)

    def test_errors_log_type_only(self):
        uid = self.draft()
        rv = self.prepare(uid)
        self.mb.password_reader = lambda cfg: (_ for _ in ()).throw(RuntimeError("leaky detail 77 ann@example.com"))
        ok, p = self.send(uid, rv["sha256"])
        self.assertFalse(ok)
        self.assertEqual(p, {"error": "RuntimeError"})
        lines = self.send_lines()
        self.assertTrue(lines[-1].endswith("| error:RuntimeError"), lines)
        text = self.all_text()
        self.assertIn("RuntimeError", text)
        self.assertNotIn("leaky", text)
        self.assertNotIn("ann@", text)
        self.assertEqual(self.smtp.state.messages, [])

    def test_starttls_required_outside_test_mode(self):
        uid = self.draft()
        rv = self.prepare(uid)  # IMAP connection stays open and is reused below
        self.smtp.state.starttls = True
        cert = os.path.join(self.tmp.name, "cert.pem")
        import subprocess
        subprocess.run(["openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes", "-subj", "/CN=127.0.0.1",
                        "-days", "1", "-keyout", os.path.join(self.tmp.name, "k.pem"), "-out", cert],
                       check=True, capture_output=True)
        self.mb.cfg = dict(self.cfg, plain_for_tests=False, cert=cert)
        ok, p = self.send(uid, rv["sha256"])
        self.assertFalse(ok)
        self.assertIn("STARTTLS", self.smtp.state.commands)  # tried TLS first, and stopped when it failed
        self.assertNotIn("AUTH", self.smtp.state.commands)
        self.assertNotIn("MAIL", self.smtp.state.commands)
        self.assertEqual(self.smtp.state.messages, [])

    def test_cyrillic_lookalike_address_refused(self):
        for kw in ({"to": "ann@ex\u0430mple.com"}, {"cc": "b\u043eb@example.net"}):
            uid = self.draft(**kw)
            ok, p = self.call("prepare_send", {"draft_uid": uid})
            self.assertFalse(ok, kw)
            self.assertIn("plain ASCII", p["error"])
        self.assertIn("refused", self.logs())

    def test_hidden_characters_refused(self):
        for kw in ({"subject": "Invoice \u202Efdp.exe"}, {"body": "Pay to acc\u200Bount 1"},
                   {"to": "\"Ann \u202EClient\" <ann@example.com>"}, {"body": "x\uFEFFy"}):
            uid = self.draft(**kw)
            ok, p = self.call("prepare_send", {"draft_uid": uid})
            self.assertFalse(ok, kw)
            self.assertIn("hidden or direction-changing", p["error"])
        uid = self.draft()  # hidden character added after the review
        rv = self.prepare(uid)
        self.fake.state.folders["Drafts"][uid] = raw_draft(body=SEND_BODY + "\u200B")
        ok, p = self.send(uid, rv["sha256"])
        self.assertFalse(ok)
        self.assertEqual(self.smtp.state.messages, [])

    def test_review_lists_address_first(self):
        rv = self.prepare(self.draft())
        self.assertIn('  - ann@example.com (name: "Ann Client")', rv["review"])
        self.assertIn("  - bob@example.net", rv["review"])
        self.assertIn("Hash12: %s" % rv["sha256"][:12], rv["review"])
        self.assertIn(TEST_SIG, rv["review"])

    def test_already_sent_refused(self):
        uid = self.draft()
        self.assertTrue(self.send(uid, self.prepare(uid)["sha256"])[0])
        rv = self.prepare(uid)  # the leftover draft, unchanged
        self.assertTrue(rv["already_sent"])
        ok, p = self.send(uid, rv["sha256"])  # even with a fresh tap
        self.assertFalse(ok)
        self.assertIn("already sent", p["error"])
        self.assertEqual(len(self.smtp.state.messages), 1)
        self.assertTrue(self.send_lines()[-1].endswith("| refused:already-sent"))

    def test_tap_reuse_refused(self):
        a, b = self.draft(), self.draft(subject="Second one")
        self.assertTrue(self.send(a, self.prepare(a)["sha256"], tap="appr-700-1")[0])
        rv = self.prepare(b)
        ok, p = self.send(b, rv["sha256"], tap="appr-700-1")
        self.assertFalse(ok)
        self.assertIn("tap was already used", p["error"])
        self.assertTrue(os.path.exists(pm._pending_path(self.cfg, b)))  # refused before the claim
        self.assertTrue(self.send(b, rv["sha256"], tap="appr-701-1")[0])
        # a tap on an ERROR line is used up too
        c = self.draft(subject="Third")
        rv = self.prepare(c)
        good = self.mb.password_reader
        self.mb.password_reader = lambda cfg: (_ for _ in ()).throw(OSError("x"))
        self.assertFalse(self.send(c, rv["sha256"], tap="appr-702-1")[0])
        self.mb.password_reader = good
        rv = self.prepare(c)
        ok, p = self.send(c, rv["sha256"], tap="appr-702-1")
        self.assertFalse(ok)
        self.assertIn("tap was already used", p["error"])
        self.assertEqual(len(self.smtp.state.messages), 2)

    def test_parallel_sends_respect_cap(self):
        import threading
        import time as _t
        old = pm.SEND_DAILY_CAP
        pm.SEND_DAILY_CAP = 2
        try:
            jobs = []
            for i in range(4):
                uid = self.draft(subject="Parallel %d" % i)
                jobs.append((uid, self.prepare(uid)["sha256"], "appr-par-%d" % i))
            results = []

            def slow(cfg):
                _t.sleep(0.3)  # widen the window between the cap check and the log write
                return TEST_PW

            def run(job):
                mb = pm.Mailbox(self.cfg, password_reader=slow)
                results.append(pm.call_tool(mb, "send_draft", {"draft_uid": job[0], "sha256": job[1],
                                                               "tap_ref": job[2]}))
            ts = [threading.Thread(target=run, args=(j,)) for j in jobs]
            for t in ts:
                t.start()
            for t in ts:
                t.join(30)
            self.assertEqual(sum(1 for ok, _ in results if ok), 2, results)
            self.assertEqual(len(self.smtp.state.messages), 2)
            self.assertEqual(sum(1 for ok, p in results if not ok and "daily send cap" in p["error"]), 2)
        finally:
            pm.SEND_DAILY_CAP = old

    def test_signature_not_set_refused(self):
        for setup in ("missing", "empty", "blank"):
            if setup == "missing":
                os.unlink(self.sig_path)
            else:
                self.set_sig("" if setup == "empty" else "  \n ")
            uid = self.draft()
            ok, p = self.call("prepare_send", {"draft_uid": uid})
            self.assertFalse(ok, setup)
            self.assertIn("signature file is", p["error"])
        self.assertEqual(self.cfg["signature_file"], self.sig_path)

    def test_signature_hidden_chars_and_lookalike_refused(self):
        for text, word in (("Example \u202EOrg", "signature has hidden"), ("Call\u200B us", "signature has hidden"),
                           ("Mail inf\u043e@example.com", "non-ASCII")):
            self.set_sig(text)
            ok, p = self.call("prepare_send", {"draft_uid": self.draft()})
            self.assertFalse(ok, text)
            self.assertIn(word, p["error"])

    def test_signature_other_profile_address_refused(self):
        self.set_sig("Example Org\nreach me at ME@Example.org")
        ok, p = self.call("prepare_send", {"draft_uid": self.draft()})
        self.assertFalse(ok)
        self.assertIn("signature contains another profile's address", p["error"])
        self.assertNotIn("example.org", json.dumps(p).lower())
        self.assertNotIn("example.org", self.all_text().lower())

    def test_no_signature_when_not_configured(self):
        self.mb.cfg["signature_file"] = None
        uid = self.draft()
        rv = self.prepare(uid)
        self.assertIsNone(rv["signature_sha12"])
        self.assertNotIn(TEST_SIG, rv["review"])
        self.assertTrue(self.send(uid, rv["sha256"])[0])

    def test_signature_is_in_the_hash(self):
        uid = self.draft()
        rv = self.prepare(uid)
        self.assertIn(TEST_SIG, rv["review"])
        self.assertRegex(rv["signature_sha12"], r"^[0-9a-f]{12}$")
        self.assertIn("signature file sha12 %s" % rv["signature_sha12"], rv["review"])
        self.set_sig(TEST_SIG + " changed")
        ok, p = self.send(uid, rv["sha256"])
        self.assertFalse(ok)
        self.assertIn("draft changed", p["error"])
        self.assertEqual(self.smtp.state.messages, [])

    def test_message_id_domain_is_from_domain(self):
        uid = self.draft()
        self.assertTrue(self.send(uid, self.prepare(uid)["sha256"])[0])
        sent = pm.email.message_from_bytes(self.smtp.state.messages[0]["data"], policy=pm.email.policy.default)
        self.assertTrue(str(sent["Message-ID"]).endswith("@example.com>"), str(sent["Message-ID"]))

    def test_real_port_is_1025_outside_harness(self):
        pm._TEST_HARNESS = False
        cfg = pm.load_config("a", self.env("a", PROTON_MCP_SMTP_PORT="2525"))
        self.assertIsNone(cfg["smtp_port_for_tests"])
        self.assertEqual(cfg["host"], "127.0.0.1")


class TestSendLedgerAndTap(SendBase):
    """Approval-id and ledger hardening: fullmatch on tap_ref, ledger fails closed, no links, owner and modes."""

    def ledger_text(self):
        return open(self.cfg["ledger"]).read() if os.path.exists(self.cfg["ledger"]) else None

    def refused_code(self, uid, sha, tap):
        before = len(self.send_lines())
        ok, p = self.call("send_draft", {"draft_uid": uid, "sha256": sha, "tap_ref": tap})
        self.assertFalse(ok, tap)
        lines = self.send_lines()
        self.assertEqual(len(lines), before + 1, lines)  # exactly one whole line per attempt, never split
        return p, lines[-1]

    def test_tap_ref_whitespace_and_newline_refused(self):
        uid = self.draft()
        rv = self.prepare(uid)
        for tap in ("appr-772\n", "appr-772 ", " appr-772", "appr-772\r", "appr-772-3130\n", "appr-772\r\n"):
            p, line = self.refused_code(uid, rv["sha256"], tap)
            self.assertIn("tap_ref must name", p["error"], repr(tap))
            self.assertIn("refused:bad-tap-ref", line)
            self.assertIn("tap none", line)
            self.assertNotIn("appr-772", line)
        raw = open(pm._send_log_path(self.cfg)).read()
        self.assertNotIn("\r", raw)
        self.assertTrue(all(pm._SEND_LINE.match(l) for l in raw.splitlines()), raw)
        self.assertEqual(self.ledger_text(), "")  # nothing written to the shared ledger
        self.assertEqual(pm._attempts_today(self.cfg), 0)
        self.assertEqual(self.smtp.state.messages, [])
        self.assertTrue(os.path.exists(pm._pending_path(self.cfg, uid)))  # review not used up
        ok, p = self.send(uid, rv["sha256"], tap="appr-772")  # the clean tap still works once
        self.assertTrue(ok, p)
        self.assertEqual(self.ledger_text(), "appr-772 a sent\n")

    def test_tap_regex_is_fullmatch_everywhere(self):
        for tap in ("appr-772\n", "appr-772 ", " appr-772", "appr-772\r", "appr-772\x00"):
            self.assertIsNone(pm.TAP_REF_RE.fullmatch(tap), repr(tap))
        self.assertTrue(pm.TAP_REF_RE.fullmatch("appr-772") and pm.TAP_REF_RE.fullmatch("appr-772-3130"))
        pm._ledger_add(self.cfg, "appr-772\n", "sent")
        self.assertEqual(self.ledger_text(), "")
        pm._send_log(self.cfg, 5, "a" * 64, 1, "refused:input", "appr-772\n")
        line = self.send_lines()[-1]
        self.assertIn("tap none", line)
        src = open(SRC).read()
        self.assertNotIn("TAP_REF_RE.match(", src)
        self.assertNotIn("TAP_REF_RE.search(", src)
        self.assertEqual(src.count("TAP_REF_RE.fullmatch("), 1)  # only inside _tap_ok
        self.assertEqual(src.count("_tap_ok("), 4)  # the definition plus the ledger, SEND-LOG and send checks

    def test_tap_ref_none_refused(self):
        uid = self.draft()
        rv = self.prepare(uid)
        for tap in ("none", "NONE", "None", "nOnE"):
            self.assertFalse(pm._tap_ok(tap), tap)
            p, line = self.refused_code(uid, rv["sha256"], tap)
            self.assertIn("tap_ref must name", p["error"], tap)
            self.assertIn("refused:bad-tap-ref", line)
        pm._ledger_add(self.cfg, "none", "sent")
        self.assertEqual(self.ledger_text(), "")  # never written to the shared ledger
        self.assertEqual(self.smtp.state.messages, [])
        self.assertTrue(os.path.exists(pm._pending_path(self.cfg, uid)))  # review not used up
        self.assertTrue(pm._tap_ok("none-1") and pm._tap_ok("appr-none"))  # only the bare word is reserved

    def test_ledger_missing_refused_fail_closed(self):
        uid = self.draft()
        rv = self.prepare(uid)
        os.unlink(self.cfg["ledger"])  # file gone, folder still there
        p, line = self.refused_code(uid, rv["sha256"], "appr-801")
        self.assertIn("ledger is missing", p["error"])
        self.assertIn("refused:ledger-missing", line)
        self.assertEqual(self.smtp.state.messages, [])
        self.assertTrue(os.path.exists(pm._pending_path(self.cfg, uid)))  # no claim consumed
        self.assertFalse(os.path.exists(self.cfg["ledger"]))  # the send path never re-creates it
        state = os.path.dirname(self.cfg["ledger"])
        for f in os.listdir(state):
            os.unlink(os.path.join(state, f))
        os.rmdir(state)  # whole folder gone
        p, line = self.refused_code(uid, rv["sha256"], "appr-801")
        self.assertIn("refused:ledger-missing", line)
        self.assertFalse(os.path.exists(state))  # never created by a send
        self.assertEqual(self.smtp.state.messages, [])
        self.assertIsNone(pm.init_taps_ledger(self.cfg["ledger"]))
        ok, p = self.send(uid, rv["sha256"], tap="appr-801")
        self.assertTrue(ok, p)

    def test_ledger_ready_requires_lock_held(self):
        led = self.cfg["ledger"]
        self.assertIsNone(pm._ledger_problem(led))  # ledger is safe here, but no global lock is held
        with self.assertRaises(pm.SendRefused) as cm:
            pm._ledger_ready(led)
        self.assertEqual(cm.exception.code, "ledger-unsafe")
        with pm._taps_lock(led):
            pm._ledger_ready(led)  # held: no refusal
        self.assertFalse(pm._TAPS.held)

    def test_ledger_has_fails_closed_on_its_own(self):
        os.unlink(self.cfg["ledger"])
        with self.assertRaises(pm.SendRefused) as cm:
            pm._ledger_has(self.cfg["ledger"], "appr-801")
        self.assertEqual(cm.exception.code, "ledger-missing")

    def test_ledger_unsafe_refused(self):
        state = os.path.dirname(self.cfg["ledger"])
        other = os.path.join(self.tmp.name, "elsewhere")
        os.makedirs(other, mode=0o700)
        os.chmod(other, 0o700)
        target = os.path.join(other, "TAPS-USED")
        open(target, "w").close()
        os.chmod(target, 0o600)

        def ledger_link():
            os.unlink(self.cfg["ledger"])
            os.symlink(target, self.cfg["ledger"])

        def ledger_644():
            os.chmod(self.cfg["ledger"], 0o644)

        def state_755():
            os.chmod(state, 0o755)

        def state_link():
            os.rename(state, state + ".real")
            os.symlink(other, state)

        for name, breaker in (("ledger link", ledger_link), ("ledger 644", ledger_644),
                              ("state 755", state_755), ("state link", state_link)):
            uid = self.draft(subject="Unsafe %s" % name)
            rv = self.prepare(uid)
            breaker()
            p, line = self.refused_code(uid, rv["sha256"], "appr-802")
            self.assertIn("refused:ledger-unsafe", line, name)
            self.assertEqual(self.smtp.state.messages, [], name)
            self.assertTrue(os.path.exists(pm._pending_path(self.cfg, uid)), name)
            self.assertEqual(open(target).read(), "", name)  # nothing written through a link
            # repair for the next case
            if os.path.islink(state):
                os.unlink(state)
                os.rename(state + ".real", state)
            os.chmod(state, 0o700)
            if os.path.islink(self.cfg["ledger"]):
                os.unlink(self.cfg["ledger"])
            self.assertIsNone(pm.init_taps_ledger(self.cfg["ledger"]), name)
            os.chmod(self.cfg["ledger"], 0o600)

    def test_ledger_wrong_owner_refused(self):
        uid = self.draft()
        rv = self.prepare(uid)
        from unittest import mock
        real = os.getuid()
        with mock.patch.object(pm.os, "getuid", return_value=real + 1):
            p, line = self.refused_code(uid, rv["sha256"], "appr-803")
        self.assertIn("refused:ledger-unsafe", line)
        self.assertEqual(self.smtp.state.messages, [])
        self.assertTrue(os.path.exists(pm._pending_path(self.cfg, uid)))

    def test_init_taps_ledger_modes_and_no_truncate(self):
        state = os.path.dirname(self.cfg["ledger"])
        self.assertEqual(os.stat(state).st_mode & 0o777, 0o700)
        self.assertEqual(os.stat(self.cfg["ledger"]).st_mode & 0o777, 0o600)
        self.assertEqual(os.stat(self.cfg["ledger"]).st_uid, os.getuid())
        with open(self.cfg["ledger"], "a") as fh:
            fh.write("appr-1 a sent\n")
        self.assertIsNone(pm.init_taps_ledger(self.cfg["ledger"]))
        self.assertEqual(self.ledger_text(), "appr-1 a sent\n")
        os.unlink(self.cfg["ledger"])
        for f in os.listdir(state):
            os.unlink(os.path.join(state, f))
        os.rmdir(state)
        os.symlink(self.tmp.name, state)  # a link is never accepted as the folder
        self.assertEqual(pm.init_taps_ledger(self.cfg["ledger"]), "ledger-unsafe")

    def test_init_ledger_cli(self):
        os.unlink(self.cfg["ledger"])
        out = io.StringIO()
        old, old_cfg = sys.stdout, os.environ.get("PROTON_MCP_CONFIG")
        sys.stdout = out
        os.environ["PROTON_MCP_CONFIG"] = self.config_path  # temp profiles file, temp state folder
        try:
            self.assertEqual(pm.main(["--init-ledger"]), 0)
        finally:
            sys.stdout = old
            if old_cfg is None:
                os.environ.pop("PROTON_MCP_CONFIG", None)
            else:
                os.environ["PROTON_MCP_CONFIG"] = old_cfg
        self.assertEqual(out.getvalue(), "tap ledger ready\n")
        self.assertTrue(os.path.exists(self.cfg["ledger"]))


class TestSendProfileB(SendBase):
    instance = "b"

    def test_b_from_forced_or_refused(self):
        uid = self.draft(frm="")
        rv = self.prepare(uid)
        self.assertIn("From: this profile's login address (hidden in the review)", rv["review"])
        self.assertNotIn(B_ADDR, rv["review"])
        self.assertTrue(self.send(uid, rv["sha256"])[0])
        m = self.smtp.state.messages[0]
        self.assertEqual(m["mail_from"], B_ADDR)
        self.assertEqual(self.smtp.state.logins, [B_ADDR])
        for frm, word in (("orders@example.com", "another profile"), ("Other <x@example.com>", "another profile"),
                          ("Other <x@example.net>", "login address")):
            uid = self.draft(frm=frm)
            ok, p = self.call("prepare_send", {"draft_uid": uid})
            self.assertFalse(ok, frm)
            self.assertIn(word, p["error"])

    def test_b_send_log(self):
        uid = self.draft()
        rv = self.prepare(uid)
        self.assertTrue(self.send(uid, rv["sha256"])[0])
        self.send(uid, rv["sha256"])
        lines = self.send_lines()
        self.assertEqual(len(lines), 2)
        for l in lines:
            self.assertRegex(l, SEND_LOG_RE)
        self.assertRegex(lines[0], r"^\d{4}-\d{2}-\d{2} \d{2}:\d{2} [+-]\d{4} \| b send_draft \| .* \| sent$")
        self.assertTrue(lines[1].endswith("| refused:already-sent"))
        path = pm._send_log_path(self.cfg)
        self.assertTrue(pm._under(path, self.roots["b"]))
        self.assertTrue(os.path.exists(path))
        text = self.all_text()
        for secret in (SEND_SUBJ, "Private body", "ann@", B_ADDR, TEST_PW):
            self.assertNotIn(secret, text)

    def test_b_refused_when_view_off(self):
        self.assertTrue(self.cfg["send_enabled"])  # on in the test profiles file
        self.mb.cfg["send_enabled"] = False
        uid = self.draft()
        ok, p = self.call("prepare_send", {"draft_uid": uid})
        self.assertFalse(ok)
        self.assertIn("sending is off for this profile", p["error"])
        ok, p = self.send(uid, "a" * 64)
        self.assertFalse(ok)
        self.assertIn("sending is off for this profile", p["error"])
        self.assertIn("refused:send-off-for-view", self.send_lines()[-1])
        self.assertEqual(self.smtp.state.messages, [])

    def test_b_review_masks_login_address(self):
        uid = self.draft(to="Me Myself <me@example.org>, ann@example.com", cc="ME@EXAMPLE.ORG")
        rv = self.prepare(uid)
        self.assertNotIn(B_ADDR, rv["review"].lower())
        self.assertNotIn("Me Myself", rv["review"])
        self.assertEqual(rv["review"].count("this profile's login address (hidden in the review)"), 3)
        self.assertIn("  - ann@example.com", rv["review"])
        self.assertNotIn(TEST_SIG, rv["review"])  # profile b has no signature_file
        self.assertIsNone(rv["signature_sha12"])

    def test_b_message_id_domain(self):
        uid = self.draft()
        self.assertTrue(self.send(uid, self.prepare(uid)["sha256"])[0])
        sent = pm.email.message_from_bytes(self.smtp.state.messages[0]["data"], policy=pm.email.policy.default)
        self.assertTrue(str(sent["Message-ID"]).endswith("@example.org>"))

    def test_tap_used_on_a_is_refused_on_b(self):
        a_cfg = pm.load_config("a", self.env("a", PROTON_MCP_IMAP_PORT=str(self.fake.port),
                                                      PROTON_MCP_SMTP_PORT=str(self.smtp.port),
                                                      PROTON_MCP_TEST_PLAIN="1"))
        mb_a = pm.Mailbox(a_cfg, password_reader=lambda c: TEST_PW)
        b = self.fake.state.add("Drafts", raw_draft())
        ok, p = pm.call_tool(mb_a, "prepare_send", {"draft_uid": b})
        self.assertTrue(ok, p)
        ok, p = pm.call_tool(mb_a, "send_draft", {"draft_uid": b, "sha256": p["sha256"], "tap_ref": "appr-990-7"})
        self.assertTrue(ok, p)
        uid = self.draft()
        rv = self.prepare(uid)
        ok, p = self.send(uid, rv["sha256"], tap="appr-990-7")
        self.assertFalse(ok)
        self.assertIn("tap was already used", p["error"])
        self.assertEqual(len(self.smtp.state.messages), 1)
        self.assertTrue(os.path.exists(pm._pending_path(self.cfg, uid)))  # refused before the claim
        self.assertTrue(self.send(uid, rv["sha256"], tap="appr-991-7")[0])
        # the ledger holds only "tap view result", mode 600 in a 700 folder, no content
        lines = open(self.cfg["ledger"]).read().splitlines()
        self.assertEqual(lines, ["appr-990-7 a sent", "appr-991-7 b sent"])
        for l in lines:
            self.assertRegex(l, r"^appr-[a-z0-9-]+ (a|b) (sent|error)$")
        self.assertEqual(os.stat(self.cfg["ledger"]).st_mode & 0o777, 0o600)
        self.assertEqual(os.stat(os.path.dirname(self.cfg["ledger"])).st_mode & 0o777, 0o700)
        for secret in (SEND_SUBJ, "Private body", "example", "@", TEST_PW):
            self.assertNotIn(secret, open(self.cfg["ledger"]).read())

    def test_a_review_not_usable_on_b(self):
        uid = self.draft()
        rv = self.prepare(uid)
        mb_a = dict(self.cfg, profile="a", log_dir=self.cfg["log_dir"])
        mb = pm.Mailbox(mb_a, password_reader=lambda c: TEST_PW)
        ok, p = pm.call_tool(mb, "send_draft", {"draft_uid": uid, "sha256": rv["sha256"], "tap_ref": TAP})
        self.assertFalse(ok)
        self.assertEqual(self.smtp.state.messages, [])


class TestPassword(Roots, unittest.TestCase):
    def test_reads_from_pass_at_runtime_only(self):
        d = tempfile.mkdtemp()
        fake = os.path.join(d, "pass")
        with open(fake, "w") as fh:
            fh.write("#!/bin/sh\n[ \"$1\" = show ] && [ \"$2\" = proton-bridge/mailbox ] && echo %s && exit 0\nexit 1\n"
                     % TEST_PW)
        os.chmod(fake, 0o700)
        old = os.environ["PATH"]
        os.environ["PATH"] = d + ":" + old
        try:
            cfg = pm.load_config("a", {"PROTON_MCP_CONFIG": self.config_path})
            self.assertEqual(pm.read_password(cfg), TEST_PW)
            self.assertNotIn(TEST_PW, json.dumps(dict(os.environ)))
            self.assertNotIn(TEST_PW, json.dumps(cfg, default=str))
            with self.assertRaises(pm.Refused):
                pm.read_password(dict(cfg, pass_entry="other/entry"))
        finally:
            os.environ["PATH"] = old


if __name__ == "__main__":
    unittest.main(verbosity=2)
