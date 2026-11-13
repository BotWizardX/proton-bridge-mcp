"""A tiny local fake SMTP server for tests only (no TLS, loopback only). Python 3.13 has no smtpd."""
import base64
import socketserver
import threading


class State:
    def __init__(self):
        self.commands = []   # verbs received, in order
        self.logins = []     # usernames from AUTH (passwords are not kept)
        self.messages = []   # dicts: mail_from, rcpts, data (bytes)
        self.starttls = False  # advertise STARTTLS (it is never completed here)


class Handler(socketserver.StreamRequestHandler):
    def out(self, s):
        self.wfile.write((s + "\r\n").encode())

    def handle(self):
        st = self.server.state
        mail_from, rcpts = None, []
        self.out("220 fake SMTP ready")
        while True:
            line = self.rfile.readline()
            if not line:
                return
            line = line.decode("utf-8", "replace").rstrip("\r\n")
            verb, _, arg = line.partition(" ")
            verb = verb.upper()
            st.commands.append(verb)
            if verb in ("EHLO", "HELO"):
                lines = ["250-fake", "250-AUTH PLAIN LOGIN"] + (["250-STARTTLS"] if st.starttls else []) + ["250 8BITMIME"]
                for l in lines:
                    self.out(l)
            elif verb == "STARTTLS":
                self.out("454 TLS not available in the fake")
            elif verb == "AUTH":
                mech, _, initial = arg.partition(" ")
                if mech.upper() == "PLAIN":
                    if not initial:
                        self.out("334 ")
                        initial = self.rfile.readline().decode().strip()
                    parts = base64.b64decode(initial).split(b"\x00")
                    st.logins.append(parts[1].decode())
                    self.out("235 ok")
                elif mech.upper() == "LOGIN":
                    if initial:
                        user = base64.b64decode(initial).decode()
                    else:
                        self.out("334 VXNlcm5hbWU6")
                        user = base64.b64decode(self.rfile.readline().strip()).decode()
                    self.out("334 UGFzc3dvcmQ6")
                    self.rfile.readline()
                    st.logins.append(user)
                    self.out("235 ok")
                else:
                    self.out("504 unsupported")
            elif verb == "MAIL":
                mail_from, rcpts = arg.split(":", 1)[1].split()[0].strip("<>"), []
                self.out("250 ok")
            elif verb == "RCPT":
                rcpts.append(arg.split(":", 1)[1].strip().strip("<>"))
                self.out("250 ok")
            elif verb == "DATA":
                self.out("354 go ahead")
                buf = []
                while True:
                    l = self.rfile.readline()
                    if l in (b".\r\n", b".\n", b""):
                        break
                    buf.append(l[1:] if l.startswith(b"..") else l)
                st.messages.append({"mail_from": mail_from, "rcpts": list(rcpts), "data": b"".join(buf)})
                self.out("250 queued")
            elif verb == "RSET":
                mail_from, rcpts = None, []
                self.out("250 ok")
            elif verb == "NOOP":
                self.out("250 ok")
            elif verb == "QUIT":
                self.out("221 bye")
                return
            else:
                self.out("502 unsupported")


class FakeSMTP:
    def __init__(self):
        self.state = State()
        self.srv = socketserver.ThreadingTCPServer(("127.0.0.1", 0), Handler)
        self.srv.daemon_threads = True
        self.srv.state = self.state
        self.port = self.srv.server_address[1]
        self.thread = threading.Thread(target=self.srv.serve_forever, daemon=True)

    def __enter__(self):
        self.thread.start()
        return self

    def __exit__(self, *a):
        self.srv.shutdown()
        self.srv.server_close()
