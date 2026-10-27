"""CUCM / IM&P / Unity platform CLI over SSH, with expect-style prompt answering.

The platform CLI is interactive ("admin:" prompt, questions such as "Password:" or
"Start installation (yes/no):"), so commands run in one interactive shell and a list
of Responders answers the questions as they appear:

    with CUCMShell("10.1.1.10", "admin", "pw", log=print) as sh:
        out = sh.run("show version active")
        sh.run("utils disaster_recovery device add network BK /backup 10.1.1.5 cucm 3",
               responders=[Responder(r"[Pp]assword.*:\\s*$", "sftp-pw", secret=True)])
"""

import re
import threading
import time

import paramiko

ANSI = re.compile(r"\x1b\[[0-9;?]*[A-Za-z]|\x1b[=>]|\r")
PROMPT = re.compile(r"(?:^|\n)[ \t]*admin:[ \t]*$")
PAGER = re.compile(r"Press <enter> for 1 line, <space> for one page, or <q> to quit\s*$")


class CLIError(Exception):
    pass


class Cancelled(CLIError):
    pass


class Responder:
    """Answer an interactive question.

    pattern  regex searched in the output received since the last answer; anchor it
             with \\s*$ so it only fires when CUCM is actually waiting for input.
    answer   text to send, or a callable(pending_text, match) -> text | None
             (None = not my question after all, keep looking).
    secret   mask the answer in the log.
    once     drop the responder after it fired.
    """

    def __init__(self, pattern, answer, secret=False, newline=True, once=False, name=None):
        self.rx = re.compile(pattern) if isinstance(pattern, str) else pattern
        self.answer = answer
        self.secret = secret
        self.newline = newline
        self.once = once
        self.name = name or (pattern if isinstance(pattern, str) else pattern.pattern)


class CUCMShell:
    def __init__(self, host, username, password, port=22, log=None, connect_timeout=30):
        self.host = host.strip()
        self.username = username
        self.password = password
        self.port = int(port)
        self.log = log or (lambda msg: None)
        self.connect_timeout = connect_timeout
        self.client = None
        self.chan = None
        self.tap = None            # tap(data): sees every byte of CLI output (transcripts)
        self.secrets = {password} if password else set()   # masked in on_output / tap

    # -- session ------------------------------------------------------------
    def connect(self):
        self.log(f"[{self.host}] connecting SSH as {self.username} ...")
        c = paramiko.SSHClient()
        c.set_missing_host_key_policy(paramiko.AutoAddPolicy())
        try:
            c.connect(self.host, port=self.port, username=self.username, password=self.password,
                      look_for_keys=False, allow_agent=False, timeout=self.connect_timeout,
                      banner_timeout=90, auth_timeout=90)
        except paramiko.AuthenticationException as e:
            raise CLIError(f"{self.host}: authentication failed ({e}). Use the OS/platform "
                           "administrator, not the application (CCMAdmin) user.") from e
        except Exception as e:
            raise CLIError(f"{self.host}: SSH connection failed - {e}") from e
        c.get_transport().set_keepalive(30)
        self.client = c
        self.chan = c.invoke_shell(term="vt100", width=250, height=2000)
        self.chan.settimeout(0.0)
        # The CLI takes a while to start (Java); wait for the first prompt.
        self._read_until_prompt(timeout=180, on_output=None)
        self.log(f"[{self.host}] CLI ready")
        try:
            self.run("set cli pagination off", timeout=30)
        except CLIError:
            pass
        return self

    def close(self):
        try:
            if self.chan:
                self.chan.send("exit\n")
        except Exception:
            pass
        if self.client:
            self.client.close()
        self.client = self.chan = None

    def __enter__(self):
        return self.connect()

    def __exit__(self, *exc):
        self.close()

    @property
    def alive(self):
        return bool(self.chan) and not self.chan.closed and self.client.get_transport().is_active()

    # -- I/O ------------------------------------------------------------------
    def _recv(self):
        out = ""
        while self.chan.recv_ready():
            out += self.chan.recv(65535).decode("utf-8", "replace")
        return ANSI.sub("", out)

    def _read_until_prompt(self, timeout, on_output):
        return self.run(None, timeout=timeout, on_output=on_output)

    def redact(self, text):
        for sec in self.secrets:
            text = text.replace(sec, "******")
        return text

    def send_raw(self, text):
        self.chan.send(text)

    def run(self, command, responders=(), timeout=300, idle_timeout=None,
            on_output=None, cancel=None, expect_disconnect=False):
        """Send `command` (None = just wait) and return its output once the prompt is back.

        timeout       overall limit in seconds (None = no limit)
        idle_timeout  give up when nothing arrives for this long
        cancel        threading.Event; when set, Ctrl-C is sent and Cancelled raised
        expect_disconnect  a closed session counts as success (reboot / switch-version)
        """
        if not self.chan:
            raise CLIError("not connected")
        responders = list(responders) + [Responder(PAGER, " ", newline=False, name="pager")]
        if command is not None:
            self.chan.send(command + "\n")
        start = last_rx = time.time()
        buf = pending = ""
        while True:
            if cancel is not None and cancel.is_set():
                try:
                    self.chan.send("\x03")
                except Exception:
                    pass
                raise Cancelled("cancelled by user")
            data = ""
            try:
                if self.chan.recv_ready():
                    data = self._recv()
            except OSError:
                data = ""
            if data:
                last_rx = time.time()
                buf += data
                pending += data
                shown = self.redact(data)
                if on_output:
                    on_output(shown)
                if self.tap:
                    self.tap(shown)
                if PROMPT.search(pending[-200:]):
                    break
                for r in list(responders):
                    m = r.rx.search(pending)
                    if not m:
                        continue
                    ans = r.answer(pending, m) if callable(r.answer) else r.answer
                    if ans is None:
                        continue
                    if r.secret and ans:
                        self.secrets.add(ans)
                    self.chan.send(ans + ("\n" if r.newline else ""))
                    if r.name != "pager":
                        shown = "******" if r.secret else (ans if ans != "" else "<enter>")
                        self.log(f"[{self.host}] auto-answer '{r.name.strip()[:60]}' -> {shown}")
                    pending = ""
                    if r.once:
                        responders.remove(r)
                    break
                continue
            if self.chan.closed or self.chan.exit_status_ready() or \
                    not self.client.get_transport().is_active():
                if expect_disconnect:
                    return buf
                raise CLIError(f"{self.host}: SSH session closed\n{buf[-2000:]}")
            now = time.time()
            if timeout is not None and now - start > timeout:
                raise CLIError(f"{self.host}: timed out after {int(timeout)} s waiting for "
                               f"'{command}'\nLast output:\n{buf[-1500:]}")
            if idle_timeout is not None and now - last_rx > idle_timeout:
                raise CLIError(f"{self.host}: no output for {int(idle_timeout)} s\n{buf[-1500:]}")
            time.sleep(0.1)
        return self._clean(buf, command)

    @staticmethod
    def _clean(buf, command):
        text = buf.replace("\r", "")
        text = PROMPT.sub("", text.rstrip()).rstrip()
        if command:
            lines = text.split("\n")
            if lines and lines[0].strip().endswith(command.strip()[-40:]):
                lines = lines[1:]
            text = "\n".join(lines)
        return text.strip("\n")


def run_on_nodes(nodes, worker, parallel=True, log=None):
    """Run worker(node) for each node; returns {host: (ok, result_or_error)}."""
    results = {}
    lock = threading.Lock()

    def _one(node):
        host = node["host"] if isinstance(node, dict) else node
        try:
            res = worker(node)
            ok = True
        except Exception as e:
            res, ok = str(e), False
            if log:
                log(f"[{host}] FAILED: {e}")
        with lock:
            results[host] = (ok, res)

    if parallel:
        threads = [threading.Thread(target=_one, args=(n,), daemon=True) for n in nodes]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
    else:
        for n in nodes:
            _one(n)
    return results
