"""Tests for disk_guard.sh and its start.sh hooks, with fake du/df/sleep/tmux in a temp dir."""
import os
import re
import shutil
import stat
import subprocess
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
GB = 1024 ** 3


def write_exec(path, body):
    with open(path, "w") as fh:
        fh.write("#!/bin/bash\n" + body)
    os.chmod(path, 0o755)


class GuardBase(unittest.TestCase):
    def setUp(self):
        self.t = tempfile.mkdtemp()
        self.dir = os.path.join(self.t, "pm")
        self.bin = os.path.join(self.t, "bin")
        self.home = os.path.join(self.t, "home")
        self.vals = os.path.join(self.t, "vals")
        for d in (self.dir, self.bin, self.home, self.vals):
            os.makedirs(d)
        shutil.copy(os.path.join(ROOT, "disk_guard.sh"), self.dir)
        self.gluon = os.path.join(self.home, ".local/share/protonmail/bridge-v3/gluon")
        # fake du: size from vals/du-<basename>, or fail if vals/du-fail exists
        write_exec(os.path.join(self.bin, "du"),
                   'V=%s; [ -f $V/du-fail ] && exit 1; d="${@: -1}"; '
                   'echo "$(cat $V/du-$(basename "$d") 2>/dev/null || echo 0) $d"\n' % self.vals)
        write_exec(os.path.join(self.bin, "df"),
                   'V=%s; [ -f $V/df-fail ] && exit 1; echo "Avail"; cat $V/df\n' % self.vals)
        # fake sleep: counts calls, flips values on the 2nd tick if asked, ends the loop after 3
        write_exec(os.path.join(self.bin, "sleep"),
                   'V=%s; n=$(( $(cat $V/ticks 2>/dev/null || echo 0) + 1 )); echo $n > $V/ticks; '
                   '[ $n -eq 2 ] && [ -f $V/df-later ] && cp $V/df-later $V/df; [ $n -ge 3 ] && exit 1; exit 0\n'
                   % self.vals)
        write_exec(os.path.join(self.dir, "start.sh"), 'echo "$1" >> %s/start-calls\n' % self.t)
        self.set_free(90 * GB)

    def tearDown(self):
        shutil.rmtree(self.t)

    def set_free(self, n, name="df"):
        open(os.path.join(self.vals, name), "w").write("%d\n" % n)

    def set_cache(self, n, base="gluon"):
        open(os.path.join(self.vals, "du-" + base), "w").write("%d\n" % n)

    def run_guard(self, mode):
        env = {"PATH": self.bin + ":/usr/bin:/bin", "HOME": self.home}
        return subprocess.run(["bash", os.path.join(self.dir, "disk_guard.sh"), mode], env=env,
                              capture_output=True, text=True, timeout=30)

    def log(self):
        p = os.path.join(self.dir, "run", "start.log")
        return open(p).read() if os.path.exists(p) else ""

    def start_calls(self):
        p = os.path.join(self.t, "start-calls")
        return open(p).read().split() if os.path.exists(p) else []


class TestCheck(GuardBase):
    def test_ok_when_no_cache_yet(self):
        self.assertEqual(self.run_guard("check").returncode, 0)
        self.assertEqual(self.log(), "")

    def test_ok_at_limits(self):
        os.makedirs(self.gluon)
        self.set_cache(5 * GB)
        self.set_free(60 * GB)
        self.assertEqual(self.run_guard("check").returncode, 0)

    def test_cache_over_5gb(self):
        os.makedirs(self.gluon)
        self.set_cache(5 * GB + 1)
        r = self.run_guard("check")
        self.assertEqual(r.returncode, 3)
        self.assertIn("GUARD BREACH cache over 5 GB", self.log())

    def test_extra_location_counts(self):
        os.makedirs(self.gluon)
        extra = os.path.join(self.t, "elsewhere")
        os.makedirs(extra)
        os.makedirs(os.path.join(self.dir, "run"))
        open(os.path.join(self.dir, "run", "gluon-extra-dirs"), "w").write(extra + "\nrelative/ignored\n")
        self.set_cache(3 * GB)
        self.set_cache(3 * GB, base="elsewhere")
        self.assertEqual(self.run_guard("check").returncode, 3)

    def test_free_under_60gb(self):
        self.set_free(60 * GB - 1)
        r = self.run_guard("check")
        self.assertEqual(r.returncode, 3)
        self.assertIn("GUARD BREACH free space on / under 60 GB", self.log())

    def test_fail_closed_when_unmeasurable(self):
        open(os.path.join(self.vals, "df-fail"), "w").close()
        self.assertEqual(self.run_guard("check").returncode, 4)
        os.remove(os.path.join(self.vals, "df-fail"))
        os.makedirs(self.gluon)
        open(os.path.join(self.vals, "du-fail"), "w").close()
        self.assertEqual(self.run_guard("check").returncode, 4)
        self.assertIn("disk could not be measured", self.log())

    def test_limits_are_constants_not_env(self):
        src = open(os.path.join(ROOT, "disk_guard.sh")).read()
        self.assertIn("readonly MAX_CACHE_BYTES=$((5 * 1024 * 1024 * 1024))", src)
        self.assertIn("readonly MIN_FREE_BYTES=$((60 * 1024 * 1024 * 1024))", src)
        self.set_free(10 * GB)
        env = {"PATH": self.bin + ":/usr/bin:/bin", "HOME": self.home,
               "MIN_FREE_BYTES": "1", "MAX_CACHE_BYTES": str(10 ** 15)}
        r = subprocess.run(["bash", os.path.join(self.dir, "disk_guard.sh"), "check"], env=env,
                           capture_output=True, text=True)
        self.assertEqual(r.returncode, 3)

    def mark_synced(self):
        os.makedirs(os.path.join(self.dir, "run"), exist_ok=True)
        open(os.path.join(self.dir, "run", "synced"), "w").close()

    def test_before_sync_missing_or_empty_cache_is_ok(self):
        self.assertEqual(self.run_guard("check").returncode, 0)
        os.makedirs(self.gluon)
        self.set_cache(0)
        self.assertEqual(self.run_guard("check").returncode, 0)

    def test_after_sync_missing_gluon_is_breach(self):
        self.mark_synced()
        r = self.run_guard("check")
        self.assertEqual(r.returncode, 4)
        self.assertIn("GUARD BREACH disk could not be measured", self.log())

    def test_after_sync_zero_du_is_breach(self):
        self.mark_synced()
        os.makedirs(self.gluon)
        self.set_cache(0)
        self.assertEqual(self.run_guard("check").returncode, 4)

    def test_after_sync_normal_cache_ok(self):
        self.mark_synced()
        os.makedirs(self.gluon)
        self.set_cache(300 * 1024 * 1024)
        self.assertEqual(self.run_guard("check").returncode, 0)

    def test_after_sync_watch_stops_bridge_when_cache_vanishes(self):
        self.mark_synced()
        r = self.run_guard("watch")
        self.assertEqual(r.returncode, 3)
        self.assertEqual(self.start_calls(), ["stop-bridge"])


class TestWatch(GuardBase):
    def test_stops_bridge_on_breach_mid_run(self):
        self.set_free(59 * GB, name="df-later")
        r = self.run_guard("watch")
        self.assertEqual(r.returncode, 3)
        self.assertEqual(self.start_calls(), ["stop-bridge"])
        self.assertIn("GUARD STOP free space on / under 60 GB; stopping Bridge", self.log())

    def test_quiet_while_ok(self):
        r = self.run_guard("watch")
        self.assertEqual(r.returncode, 0)
        self.assertEqual(self.start_calls(), [])
        self.assertNotIn("STOP", self.log())

    def test_log_lines_hold_no_paths_or_numbers_beyond_limits(self):
        os.makedirs(self.gluon)
        self.set_cache(7 * GB)
        self.run_guard("watch")
        for line in self.log().splitlines():
            self.assertNotIn(self.home, line)
            self.assertNotIn(str(7 * GB), line)


class TestStartHooks(unittest.TestCase):
    """start.sh refuses to start or open the sign-in CLI when the guard check fails."""

    def setUp(self):
        self.t = tempfile.mkdtemp()
        self.dir = os.path.join(self.t, "pm")
        self.bin = os.path.join(self.t, "bin")
        os.makedirs(self.dir)
        os.makedirs(self.bin)
        shutil.copy(os.path.join(ROOT, "start.sh"), self.dir)
        write_exec(os.path.join(self.dir, "disk_guard.sh"), "exit 3\n")
        write_exec(os.path.join(self.bin, "protonmail-bridge"), 'echo BRIDGE-RAN >> %s/ran\n' % self.t)
        write_exec(os.path.join(self.bin, "tmux"), 'echo "tmux $*" >> %s/ran; [ "$1" = has-session ] && exit 1; exit 0\n'
                   % self.t)

    def tearDown(self):
        shutil.rmtree(self.t)

    def test_refuses_start_and_cli(self):
        for mode in ("start", "cli", "guard"):
            r = subprocess.run(["bash", os.path.join(self.dir, "start.sh"), mode],
                               env={"PATH": self.bin + ":/usr/bin:/bin", "HOME": self.t},
                               capture_output=True, text=True, timeout=30)
            self.assertEqual(r.returncode, 3, mode)
        ran = open(os.path.join(self.t, "ran")).read() if os.path.exists(os.path.join(self.t, "ran")) else ""
        self.assertNotIn("BRIDGE-RAN", ran)
        self.assertNotIn("new-session", ran)
        self.assertIn("REFUSED start: disk guard limit", open(os.path.join(self.dir, "run", "start.log")).read())

    def test_cli_starts_guard_before_bridge(self):
        write_exec(os.path.join(self.dir, "disk_guard.sh"), "exit 0\n")
        sess = os.path.join(self.t, "sess")
        os.makedirs(sess)
        write_exec(os.path.join(self.bin, "tmux"),
                   'n="${3#=}"; echo "tmux $1 $n" >> %s/ran; case "$1" in has-session) [ -f %s/$n ];; '
                   'new-session) touch %s/$4;; kill-session) rm -f %s/$n;; esac\n' % (self.t, sess, sess, sess))
        r = subprocess.run(["bash", os.path.join(self.dir, "start.sh"), "cli"],
                           env={"PATH": self.bin + ":/usr/bin:/bin", "HOME": self.t},
                           capture_output=True, text=True, timeout=30)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        ran = open(os.path.join(self.t, "ran")).read()
        self.assertLess(ran.index("tmux new-session"), ran.index("BRIDGE-RAN"))
        self.assertTrue(os.path.exists(os.path.join(sess, "proton-bridge-guard")))
        self.assertIn("guard started", open(os.path.join(self.dir, "run", "start.log")).read())


    def _status(self, guard_up, ss_port=1143, env_port=None):
        sess = os.path.join(self.t, "sess")
        os.makedirs(sess, exist_ok=True)
        open(os.path.join(sess, "proton-bridge"), "w").close()
        g = os.path.join(sess, "proton-bridge-guard")
        if guard_up:
            open(g, "w").close()
        elif os.path.exists(g):
            os.remove(g)
        write_exec(os.path.join(self.bin, "tmux"), '[ "$1" = has-session ] && [ "${3:0:1}" = "=" ] && [ -f %s/${3#=} ]\n' % sess)
        write_exec(os.path.join(self.bin, "ss"), 'echo "LISTEN 0 4096 127.0.0.1:%d 0.0.0.0:*"\n' % ss_port)
        env = {"PATH": self.bin + ":/usr/bin:/bin", "HOME": self.t}
        if env_port is not None:
            env["PROTON_MCP_IMAP_PORT"] = env_port
        return subprocess.run(["bash", os.path.join(self.dir, "start.sh"), "status"],
                              env=env, capture_output=True, text=True, timeout=30)

    def test_status_uses_configured_imap_port(self):
        r = self._status(guard_up=True, ss_port=1144, env_port="1144")
        self.assertEqual(r.returncode, 0, r.stdout)
        self.assertIn("UP", r.stdout)
        r = self._status(guard_up=True, ss_port=1143, env_port="1144")  # Bridge not on the configured port
        self.assertEqual(r.returncode, 1, r.stdout)
        self.assertIn("IMAP not listening", r.stdout)
        r = self._status(guard_up=True, ss_port=1143, env_port="1144; rm -rf x")  # not digits: falls back to 1143
        self.assertEqual(r.returncode, 0, r.stdout)

    def test_status_fails_when_bridge_up_but_guard_down(self):
        r = self._status(guard_up=False)
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("GUARD DOWN", r.stdout)
        r = self._status(guard_up=True)
        self.assertEqual(r.returncode, 0, r.stdout)
        self.assertIn("GUARD UP", r.stdout)

    def test_pkill_is_anchored_and_own_user(self):
        src = open(os.path.join(ROOT, "start.sh")).read()
        for line in [l for l in src.splitlines() if l.strip().startswith("pkill")]:
            self.assertIn('-u "$(id -u)"', line)
            self.assertRegex(line, r"-f ['\"]\^(/|\$HOME/)")

    def test_tmux_targets_are_exact(self):
        # 'proton-bridge' must never prefix-match 'proton-bridge-guard' (tmux -t does prefix matching).
        src = open(os.path.join(ROOT, "start.sh")).read()
        targets = re.findall(r'-t "([^"]*)"', src)
        self.assertTrue(targets)
        for t in targets:
            self.assertTrue(t.startswith("="), t)

    def test_status_not_fooled_by_guard_session_name(self):
        # Only the guard is up; Bridge must read as DOWN, not RUNNING.
        sess = os.path.join(self.t, "sess")
        os.makedirs(sess, exist_ok=True)
        open(os.path.join(sess, "proton-bridge-guard"), "w").close()
        write_exec(os.path.join(self.bin, "tmux"),
                   'n="${3#=}"; [ "$1" = has-session ] || exit 0; '
                   '[ "${3:0:1}" = "=" ] && { [ -f %s/$n ]; exit; }; ls %s | grep -q "^$n"\n' % (sess, sess))
        write_exec(os.path.join(self.bin, "ss"), 'exit 0\n')
        r = subprocess.run(["bash", os.path.join(self.dir, "start.sh"), "status"],
                           env={"PATH": self.bin + ":/usr/bin:/bin", "HOME": self.t},
                           capture_output=True, text=True, timeout=30)
        self.assertIn("GUARD UP", r.stdout)
        self.assertIn("DOWN", r.stdout.splitlines()[-1])
        self.assertNotIn("RUNNING", r.stdout)


class TestRollbackFingerprint(unittest.TestCase):
    """rollback.sh deletes a GPG key only for an exact 40-hex fingerprint (stubs; nothing real removed)."""

    def run_rb(self, fpr):
        t = tempfile.mkdtemp()
        try:
            d, b = os.path.join(t, "pm"), os.path.join(t, "bin")
            os.makedirs(os.path.join(d, "run"))
            os.makedirs(b)
            src = open(os.path.join(ROOT, "rollback.sh")).read()
            src = re.sub(r"^DIR=.*$", "DIR=" + d, src, count=1, flags=re.M)
            open(os.path.join(d, "rollback.sh"), "w").write(src)
            write_exec(os.path.join(d, "start.sh"), "exit 0\n")
            for c in ("pass", "gpg", "sudo"):
                write_exec(os.path.join(b, c), 'echo "STUB %s $*"\n' % c)
            if fpr is not None:
                open(os.path.join(d, "run", "gpg-key-fpr"), "w").write(fpr + "\n")
            r = subprocess.run(["bash", os.path.join(d, "rollback.sh"), "full", "--confirm", "2026-01-01 1200 c1"],
                               input="yes\n", env={"PATH": b + ":/usr/bin:/bin", "HOME": os.path.join(t, "home")},
                               capture_output=True, text=True, timeout=30)
            return r.stdout
        finally:
            shutil.rmtree(t)

    def test_fingerprint_rules(self):
        good = "0123456789ABCDEFabcdef0123456789ABCDEF01"
        self.assertIn("STUB gpg --batch --yes --delete-secret-and-public-key " + good, self.run_rb(good))
        for bad in ("", good[:39], good + "0", good[:39] + "g", "-" + good[:39], "ABC; rm -rf ~"):
            out = self.run_rb(bad)
            self.assertNotIn("STUB gpg", out, bad)
            self.assertIn("not a 40-hex fingerprint", out)
        out = self.run_rb(None)
        self.assertNotIn("STUB gpg", out)
        self.assertIn("gpg-key-fpr is missing", out)


if __name__ == "__main__":
    unittest.main(verbosity=2)
