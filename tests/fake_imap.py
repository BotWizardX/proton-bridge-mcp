"""A tiny local fake IMAP server for tests only (no TLS, loopback only)."""
import email.utils
import re
import shlex
import socketserver
import threading


class State:
    def __init__(self):
        self.commands = []  # (verb, args) for every command received
        self.folders = {"INBOX": {}, "Drafts": {}, "Sent": {}, "Trash": {}, "Archive": {},
                        "Spam": {}, "Labels/Client": {}, "Folders/Projects": {}}
        self.next_uid = 100
        self.logins = []

    def add(self, folder, raw):
        self.next_uid += 1
        self.folders[folder][self.next_uid] = raw
        return self.next_uid


class Handler(socketserver.StreamRequestHandler):
    def send(self, s):
        if isinstance(s, str):
            s = s.encode()
        self.wfile.write(s)

    def handle(self):
        st = self.server.state
        selected = None
        self.send("* OK fake IMAP ready\r\n")
        while True:
            line = self.rfile.readline()
            if not line:
                return
            line = line.decode().rstrip("\r\n")
            tag, _, rest = line.partition(" ")
            verb, _, args = rest.partition(" ")
            verb = verb.upper()
            if verb == "UID":
                sub, _, args = args.partition(" ")
                verb = "UID " + sub.upper()
            st.commands.append((verb, args))
            if verb == "CAPABILITY":
                self.send("* CAPABILITY IMAP4rev1 MOVE UIDPLUS\r\n%s OK done\r\n" % tag)
            elif verb == "LOGIN":
                st.logins.append(shlex.split(args)[0])
                self.send("%s OK logged in\r\n" % tag)
            elif verb == "NOOP":
                self.send("%s OK done\r\n" % tag)
            elif verb == "LOGOUT":
                self.send("* BYE\r\n%s OK done\r\n" % tag)
                return
            elif verb == "LIST":
                for f in st.folders:
                    self.send('* LIST (\\HasNoChildren) "/" "%s"\r\n' % f)
                self.send("%s OK done\r\n" % tag)
            elif verb in ("SELECT", "EXAMINE"):
                name = shlex.split(args)[0]
                if name not in st.folders:
                    self.send("%s NO no such folder\r\n" % tag)
                else:
                    selected = name
                    self.send("* %d EXISTS\r\n%s OK [READ-WRITE] selected\r\n" % (len(st.folders[name]), tag))
            elif verb == "UID SEARCH":
                m = re.search(r"\{(\d+)\}$", args)
                if m:  # one synchronizing literal (CHARSET UTF-8 search)
                    self.send("+ go ahead\r\n")
                    data = self.rfile.read(int(m.group(1))).decode("utf-8")
                    self.rfile.readline()
                    args = args[:m.start()] + '"' + data + '"'
                    st.commands[-1] = (verb, args)
                toks = [t for t in shlex.split(args) if t.upper() not in ("CHARSET", "UTF-8")]
                hits = []
                for uid, raw in st.folders[selected].items():
                    ok, i = True, 0
                    while i < len(toks):
                        t = toks[i].upper()
                        if t in ("FROM", "SUBJECT", "TEXT"):
                            val = toks[i + 1].lower()
                            hay = raw.decode("utf-8", errors="replace").lower()
                            ok = ok and val in hay
                            i += 2
                        elif t in ("SINCE", "BEFORE"):
                            i += 2
                        else:
                            i += 1
                    if ok:
                        hits.append(str(uid))
                self.send("* SEARCH %s\r\n%s OK done\r\n" % (" ".join(hits), tag))
            elif verb == "UID FETCH" and "INTERNALDATE" in args.upper():
                uid_s, _, what = args.partition(" ")
                for u in uid_s.split(","):
                    u = int(u)
                    raw = st.folders[selected].get(u)
                    if raw is None:
                        continue
                    m = re.search(rb"^Date: (.+?)\r?$", raw, re.M)
                    if m:
                        dt = email.utils.parsedate_to_datetime(m.group(1).decode())
                        self.send('* 1 FETCH (UID %d INTERNALDATE "%s")\r\n' % (u, dt.strftime("%d-%b-%Y %H:%M:%S %z")))
                    else:
                        self.send("* 1 FETCH (UID %d)\r\n" % u)
                self.send("%s OK done\r\n" % tag)
            elif verb == "UID FETCH":
                uid_s, _, what = args.partition(" ")
                uid = int(uid_s)
                raw = st.folders[selected].get(uid)
                if raw is not None:
                    if "HEADER.FIELDS" in what.upper():
                        data = raw.split(b"\r\n\r\n", 1)[0] + b"\r\n\r\n"
                        item = "BODY[HEADER.FIELDS (DATE FROM TO CC SUBJECT MESSAGE-ID)]"
                    else:
                        data = raw
                        item = "BODY[]"
                    self.send("* 1 FETCH (UID %d %s {%d}\r\n" % (uid, item, len(data)))
                    self.send(data)
                    self.send(")\r\n")
                self.send("%s OK done\r\n" % tag)
            elif verb in ("UID MOVE", "UID COPY"):
                uidset, _, target = args.partition(" ")
                target = shlex.split(target)[0]
                if target not in st.folders:
                    st.folders[target] = {}
                for u in uidset.split(","):
                    u = int(u)
                    raw = st.folders[selected].get(u)
                    if raw is not None:
                        st.folders[target][u] = raw
                        if verb == "UID MOVE":
                            del st.folders[selected][u]
                self.send("%s OK done\r\n" % tag)
            elif verb == "APPEND":
                m = re.match(r'(".*?"|\S+) .*\{(\d+)\}$', args)
                folder = m.group(1).strip('"')
                n = int(m.group(2))
                self.send("+ go ahead\r\n")
                data = self.rfile.read(n)
                self.rfile.readline()
                st.add(folder, data)
                self.send("%s OK appended\r\n" % tag)
            else:
                self.send("%s BAD unsupported\r\n" % tag)


class FakeIMAP:
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
