"""Detached, single-instance watchdog for this checkout's loopback gateway."""

from contextlib import contextmanager, suppress
from datetime import datetime
import json
import logging
import os
from pathlib import Path
import socket
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from urllib.parse import quote

from app import qwen_config
import psutil

from app.qwen_config import gateway_port, share_setting

from app.paths import PROJECT_ROOT

ROOT = PROJECT_ROOT
PYTHON = Path(sys.executable).resolve()
SCRIPT = ROOT / "qwen_service.py"
MANAGER = ROOT / "mode_server.py"
# Resolved through the shared settings helper so a re-pointed gateway keeps being
# probed, restarted and adopted on the port it actually listens on.
GATEWAY_PORT = gateway_port()
URL = f"http://127.0.0.1:{GATEWAY_PORT}"
STOP = ROOT / "service.stop"
SERVICE_RECORD = ROOT / "supervisor.process.json"
MANAGER_RECORD = ROOT / "supervisor-manager.process.json"
SERVICE_STATE = ROOT / "supervisor.status.json"
OPENER = urllib.request.build_opener(
    urllib.request.ProxyHandler({}),
    # Sessions are cookies now, so this opener has to keep them: the watchdog logs
    # in like any other client and then reuses what the gateway issued.
    urllib.request.HTTPCookieProcessor(),
)
HEARTBEAT_INTERVAL = 5.0
HEARTBEAT_TIMEOUT = 1.5
HEARTBEAT_FAILURE_LIMIT = 3


def heartbeat_should_count() -> bool:
    """探活失败要不要计入"卡死"计数。

    网关**故意**换权重（语音窗口、模型档位）时事件循环忙着装载，几秒内答不上探活是正常的；
    这段窗口由 `qwen_config.mark_switching()` 写在文件里（带截止时间），所以：
    * 窗口内 → 不计数（只记一行日志）；
    * 窗口过期（一个真的卡死清不掉标记）→ 照常计数，看门狗该重启还是重启。
    """
    return not qwen_config.switching_now()
# Until the watchdog holds a session, a gated gateway answers 401 and looks dead.
SESSION_READY = threading.Event()
SESSION_LOCK = threading.Lock()


def normalized(value):
    return os.path.normcase(os.path.abspath(value))


def read_json(path):
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
        return value if isinstance(value, dict) else None
    except (OSError, ValueError):
        return None


def write_json(path, data):
    temporary = path.with_name(path.name + f".{os.getpid()}.tmp")
    temporary.write_text(json.dumps(data, ensure_ascii=True), encoding="utf-8")
    os.replace(temporary, path)


@contextmanager
def file_lock(path, timeout=0):
    """OS-owned locks are released automatically even after a forced process exit."""
    # Reopening a persistent lock needs neither append nor creation access.
    # Never replace or delete it: another process may already hold its byte lock.
    while True:
        try:
            handle = path.open("r+b")
            break
        except FileNotFoundError:
            try:
                handle = path.open("x+b")
                break
            except FileExistsError:
                continue  # A concurrent starter created the same lock first.
    deadline = time.monotonic() + timeout
    locked = False
    try:
        while True:
            handle.seek(0)
            try:
                if os.name == "nt":
                    import msvcrt
                    msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
                else:
                    import fcntl
                    fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
                locked = True
                break
            except OSError:
                if time.monotonic() >= deadline:
                    raise TimeoutError(f"Another Qwen operation holds {path.name}")
                time.sleep(0.1)
        # Windows permits locking a byte beyond EOF. Initialize only while we
        # own the lock so concurrent starts cannot write an unowned lock byte.
        handle.seek(0, os.SEEK_END)
        if handle.tell() == 0:
            handle.write(b"\0")
            handle.flush()
        yield
    finally:
        try:
            if locked:
                handle.seek(0)
                if os.name == "nt":
                    import msvcrt
                    msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
                else:
                    import fcntl
                    fcntl.flock(handle, fcntl.LOCK_UN)
        finally:
            handle.close()


def record_process(process, script):
    return {
        "pid": process.pid,
        "created_unix_ms": int(process.create_time() * 1000),
        "executable": str(PYTHON),
        "script": str(script),
    }


def matches_script(process, script):
    try:
        command = process.cmdline()
        arguments = command[1:]
        if arguments[:2] == ["-X", "utf8"]:
            arguments = arguments[2:]
        return (
            normalized(process.exe()) == normalized(PYTHON)
            and bool(arguments) and normalized(arguments[0]) == normalized(script)
        )
    except (psutil.Error, OSError, ValueError):
        return False


def owned_process(record_path, script):
    record = read_json(record_path)
    if not record:
        return None
    try:
        process = psutil.Process(int(record["pid"]))
        if (
            normalized(record["executable"]) == normalized(PYTHON)
            and normalized(record["script"]) == normalized(script)
            and abs(process.create_time() * 1000 - record["created_unix_ms"]) < 20
            and matches_script(process, script)
            and process.is_running()
        ):
            return process
    except (psutil.Error, KeyError, TypeError, ValueError):
        pass
    return None


def owned_backend():
    record = read_json(ROOT / "backend.process.json")
    if not record:
        return None
    from app.qwen_config import EXECUTABLE, MODEL
    try:
        process = psutil.Process(int(record["pid"]))
        command = process.cmdline()
        def argument(*names):
            for name in names:
                if name in command and command.index(name) + 1 < len(command):
                    return command[command.index(name) + 1]
            return None
        if (
            abs(process.create_time() * 1000 - record["created_unix_ms"]) < 20
            and normalized(process.exe()) == normalized(EXECUTABLE)
            and normalized(record["model"]) == normalized(MODEL)
            and normalized(argument("-m", "--model") or "") == normalized(MODEL)
            and record["port"] == 8082 and argument("--port") == "8082"
        ):
            return process
    except (psutil.Error, KeyError, TypeError, ValueError):
        pass
    return None


def supervisor_credential():
    """The credential this watchdog should present on its own gateway requests.

    The admin token is preferred over the sharing passphrase: it lives in a file on
    this machine, it is what the gate accepts for owner-level access, and it keeps
    working after the owner rotates the passphrase, which would otherwise look like
    an unhealthy gateway and trigger a restart loop.
    """
    try:
        token = (ROOT / "logs" / "admin.token").read_text(encoding="ascii").strip()
    except OSError:
        token = ""
    if token:
        # GATE_PARAM, not the console's OWNER_PARAM: that one is exchanged for a
        # cookie by the gate, so a probe carrying it would receive the exchange
        # redirect instead of the route's own answer.
        return "gate", token
    shared = str(share_setting("access_token") or "").strip()
    return ("k", shared) if shared else (None, "")


def gateway_url(path):
    """Health probe path carrying the credential when one is configured.

    Sharing puts a gate in front of every route, including the heartbeat this
    supervisor polls. Without a credential here the supervisor reads the 401 as an
    unhealthy gateway and restarts a perfectly good one forever.
    """
    parameter, credential = supervisor_credential()
    if not parameter or not credential:
        return path
    separator = "&" if "?" in path else "?"
    return f"{path}{separator}{parameter}={quote(credential, safe='')}"


def http_json(path, method="GET", base=URL, timeout=3):
    request = urllib.request.Request(
        base + path, data=b"{}" if method == "POST" else None,
        headers={"Content-Type": "application/json"}, method=method,
    )
    with OPENER.open(request, timeout=timeout) as response:
        return json.load(response)


def ensure_session():
    """Log the watchdog in once, so its probes are answered instead of redirected.

    Sharing puts a passphrase gate in front of every route, and the gate answers an
    unknown caller with 401 — which this supervisor would otherwise read as an
    unhealthy gateway and restart in a loop. The passphrase is resolved the same way
    the server resolves it (environment, then the sharing overlay, then settings),
    so any launcher that configured sharing also gives the watchdog a way in, and
    the resulting cookie keeps working after the owner rotates that passphrase.
    """
    if SESSION_READY.is_set():
        return True
    with SESSION_LOCK:
        if SESSION_READY.is_set():
            return True
        passphrase = ""
        try:
            passphrase = str(share_setting("access_token") or "").strip()
        except Exception:  # pylint: disable=broad-except
            passphrase = ""
        if not passphrase:
            # Sharing is off, so there is no gate to log in to: a plain probe is
            # the whole test.
            if gateway_status(timeout=HEARTBEAT_TIMEOUT) is not None:
                SESSION_READY.set()
            return SESSION_READY.is_set()
        body = urllib.parse.urlencode({"token": passphrase}).encode("ascii")
        request = urllib.request.Request(
            URL + "/__login", data=body,
            headers={"Content-Type": "application/x-www-form-urlencoded"}, method="POST",
        )
        try:
            OPENER.open(request, timeout=10).read()
        except (OSError, ValueError):
            return False
        if gateway_status(timeout=5) is not None:
            SESSION_READY.set()
        return SESSION_READY.is_set()


def gateway_status(timeout=3):
    try:
        status = http_json(gateway_url("/local/status"), timeout=timeout)
        return status if status.get("service") == "qwen-local-modes" else None
    except (OSError, ValueError, AttributeError):
        return None


def port_open(port):
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=0.3):
            return True
    except OSError:
        return False


def adopt_manager():
    process = owned_process(MANAGER_RECORD, MANAGER)
    if process:
        return process
    # The gateway also writes its own identity. Prefer it over enumerating every
    # system socket, which may be denied on a restricted Windows account.
    process = owned_process(ROOT / "manager.process.json", MANAGER)
    if process:
        write_json(MANAGER_RECORD, record_process(process, MANAGER))
        return process
    # An older launcher may have started this exact script without a supervisor.
    try:
        connections = psutil.net_connections(kind="tcp")
    except (psutil.Error, OSError):
        return None
    for connection in connections:
        if connection.status == psutil.CONN_LISTEN and connection.laddr.port == GATEWAY_PORT and connection.pid:
            try:
                process = psutil.Process(connection.pid)
                if matches_script(process, MANAGER):
                    write_json(MANAGER_RECORD, record_process(process, MANAGER))
                    return process
            except psutil.Error:
                continue
    return None


def spawn(script, suffix, detached=False):
    logs = ROOT / "logs"
    logs.mkdir(exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S-%f")
    flags = 0
    if os.name == "nt":
        flags = (subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP
                 if detached else subprocess.CREATE_NO_WINDOW)
    with (logs / f"{stamp}-{suffix}.out.log").open("wb") as out, \
            (logs / f"{stamp}-{suffix}.err.log").open("wb") as err:
        return subprocess.Popen(
            [str(PYTHON), "-X", "utf8", str(script)] + (["run"] if detached else []),
            cwd=ROOT, stdin=subprocess.DEVNULL, stdout=out, stderr=err,
            creationflags=flags, close_fds=True,
            start_new_session=os.name != "nt" and detached,
        )


def backoff(failures):
    return min(60, 2 ** min(failures, 6))


def process_alive(process):
    try:
        return process is not None and process.is_running() and process.status() != psutil.STATUS_ZOMBIE
    except psutil.Error:
        return False


def restore_manager_record(process):
    """Repair a lost record only for the exact process already under supervision."""
    current = psutil.Process(process.pid)
    if (not process_alive(current) or not matches_script(current, MANAGER)
            or abs(current.create_time() - process.create_time()) >= 0.02):
        raise RuntimeError("Gateway process identity changed; no process was stopped.")
    write_json(MANAGER_RECORD, record_process(current, MANAGER))


def supervise():
    with supervisor_lock(ROOT / "supervisor.lock", timeout=45,
                         takeover=lambda: not port_open(GATEWAY_PORT),
                         fallback=ROOT / "supervisor.run.lock") as note:
        logs = ROOT / "logs"
        logs.mkdir(exist_ok=True)
        logging.basicConfig(
            filename=logs / "supervisor.log", level=logging.INFO,
            format="%(asctime)s %(levelname)s %(message)s", encoding="utf-8",
        )
        if note:
            logging.warning(note)
        # The scope has to be republished here, not only by the starter: this process writes its
        # own record a moment after being spawned, and without the field it silently clobbered the
        # starter's copy. `stop`/`start` then read the record back as "unknown scope", decided the
        # running supervisor was the wrong one, and tried to retire it - which fails when that
        # supervisor runs with administrator rights. Measured twice: "监督进程 pid=… 是以「未知」
        # 口径启动的…它无法被结束" from a service that was in fact perfectly healthy.
        publish_identity(psutil.Process(), requested_scope())
        process = None
        child = None
        failures = 0
        launched_at = 0
        retry_at = 0
        next_probe_at = 0
        probe_failures = 0
        logging.info("Supervisor started pid=%s", os.getpid())
        try:
            while True:
                alive = process_alive(process)
                if not alive and process is not None:
                    code = child.poll() if child is not None else "adopted process exited"
                    failures = 1 if time.monotonic() - launched_at > 120 else failures + 1
                    delay = backoff(failures)
                    retry_at = time.monotonic() + delay
                    logging.warning("Manager pid=%s exited (%s); retry in %ss", process.pid, code, delay)
                    write_json(SERVICE_STATE, {"state": "recovering", "error": f"Manager exited ({code})", "retry_seconds": delay})
                    process = child = None
                    next_probe_at = 0
                    probe_failures = 0
                if STOP.exists() and process is None:
                    break
                if process is None and time.monotonic() >= retry_at:
                    try:
                        with file_lock(ROOT / "service-control.lock", timeout=1):
                            if STOP.exists():
                                break
                            process = adopt_manager()
                            if process is None:
                                if port_open(GATEWAY_PORT):
                                    raise RuntimeError(
                                        f"Port {GATEWAY_PORT} belongs to another service; "
                                        "no process was stopped."
                                    )
                                child = spawn(MANAGER, "manager")
                                process = psutil.Process(child.pid)
                                write_json(MANAGER_RECORD, record_process(process, MANAGER))
                                logging.info("Manager started pid=%s", process.pid)
                            else:
                                logging.info("Adopted manager pid=%s", process.pid)
                            launched_at = time.monotonic()
                            next_probe_at = launched_at + HEARTBEAT_INTERVAL
                            probe_failures = 0
                            write_json(SERVICE_STATE, {"state": "running", "manager_pid": process.pid})
                    except (OSError, RuntimeError, psutil.Error) as exc:
                        process = child = None
                        failures += 1
                        retry_at = time.monotonic() + backoff(failures)
                        logging.error("Manager start deferred: %s", exc)
                        write_json(SERVICE_STATE, {"state": "error", "error": str(exc)})
                elif alive and process is not None and time.monotonic() >= next_probe_at:
                    # A live PID is not enough: uvicorn can remain alive while its
                    # event loop or socket is wedged. Probe the manager endpoint and
                    # restart only after several consecutive failures.
                    next_probe_at = time.monotonic() + HEARTBEAT_INTERVAL
                    # A gated gateway answers 401 until this watchdog has a session,
                    # so the login is attempted before a failed probe is counted.
                    if gateway_status(timeout=HEARTBEAT_TIMEOUT) is None and not ensure_session():
                        if not heartbeat_should_count():
                            # 网关正在**故意**换权重（语音窗口、模型档位），它此刻答不上探活是正常的：
                            # 不计数，只留一行日志。标记本身带截止时间，所以真卡死仍会被重启。
                            logging.info(
                                "Manager is switching the backend (pid=%s); not counting a heartbeat miss",
                                process.pid)
                            next_probe_at = time.monotonic() + HEARTBEAT_INTERVAL
                            continue
                        probe_failures += 1
                        logging.warning(
                            "Manager heartbeat failed pid=%s (%s/%s)",
                            process.pid, probe_failures, HEARTBEAT_FAILURE_LIMIT,
                        )
                        if probe_failures >= HEARTBEAT_FAILURE_LIMIT and not STOP.exists():
                            try:
                                with file_lock(ROOT / "service-control.lock", timeout=1):
                                    if not STOP.exists() and process_alive(process):
                                        restore_manager_record(process)
                                        terminate_owned(process, MANAGER_RECORD, MANAGER)
                                        MANAGER_RECORD.unlink(missing_ok=True)
                                        (ROOT / "manager.pid").unlink(missing_ok=True)
                                        logging.error("Manager heartbeat lost; restarting pid=%s", process.pid)
                                        failures += 1
                                        retry_at = time.monotonic() + backoff(failures)
                                        write_json(SERVICE_STATE, {
                                            "state": "recovering",
                                            "error": "Manager heartbeat lost; restarting.",
                                            "retry_seconds": backoff(failures),
                                        })
                                        process = child = None
                                        next_probe_at = 0
                                        probe_failures = 0
                            except (OSError, RuntimeError, psutil.Error) as exc:
                                logging.error("Manager heartbeat restart deferred: %s", exc)
                                probe_failures = HEARTBEAT_FAILURE_LIMIT - 1
                    else:
                        probe_failures = 0
                time.sleep(0.5)
        finally:
            write_json(SERVICE_STATE, {"state": "stopped"})
            SERVICE_RECORD.unlink(missing_ok=True)
            logging.info("Supervisor stopped")


def stale_lock(path):
    """Whether a lock file is held by something that is not a live Qwen supervisor.

    The lock is an OS byte lock, so it cannot be stolen and it is not released while the holder
    lives — even if that holder is wedged, elevated and unreachable. Measured on this machine: a
    supervisor from 9/17 held ``supervisor.lock`` for days, so every later start died with
    "Another Qwen operation holds supervisor.lock" and the service could not be brought back at
    all. A lock whose *recorded* owner is gone (or is not this script) protects nothing, so it is
    rotated out of the way instead of blocking the machine's own service forever.
    """
    record = read_json(SERVICE_RECORD) or {}
    owner = record.get("pid")
    if owner:
        try:
            process = psutil.Process(int(owner))
            if process_alive(process) and matches_script(process, SCRIPT):
                return False                     # a real, live supervisor: leave it alone
        except (psutil.Error, TypeError, ValueError):
            pass
    return True


@contextmanager
def supervisor_lock(path, timeout=45, takeover=None, fallback=None):
    """Yield while holding the lock, rotating or bypassing one that protects nothing.

    ``takeover`` is consulted when the recorded owner *is* alive but the thing it was supposed to
    be running is not (no gateway on the port). A supervisor whose lock outlives its own service is
    a wedged supervisor — measured here: one from 9/17 held ``supervisor.lock`` for three days
    while nothing listened on 8081, and every start died with "Another Qwen operation holds
    supervisor.lock". Holding the lock only means something while there is something to protect.

    A stale lock held open by a *live* process cannot be renamed on Windows (the holder does not
    share delete access), so ``fallback`` is where new starters exclude each other instead: the
    wedged process keeps its unusable lock and the machine stops being blocked by it.

    This has to *enter* the lock: ``file_lock`` is itself a context manager, so the timeout fires
    on ``__enter__`` — the first version of this repair only called it, never saw the failure, and
    therefore repaired nothing.
    """
    try:
        with file_lock(path, timeout=timeout):
            yield ""
        return
    except TimeoutError:
        reason = ""
        if stale_lock(path):
            reason = "its recorded owner is not a live supervisor"
        elif takeover is not None and takeover():
            reason = "its owner is alive but the service it should be running is not"
        if not reason:
            raise
    target, note = path, ""
    rotated = path.with_name(f"{path.name}.stale-{int(time.time())}")
    try:
        path.rename(rotated)
    except OSError:
        if fallback is None:
            raise
        target = fallback
        note = (f"{path.name} is held by a stale process that will not let it be renamed; "
                f"new starters contend on {fallback.name} instead")
    else:
        note = f"Rotated a stale {path.name} to {rotated.name}: {reason}"
    with file_lock(target, timeout=timeout):
        yield note


def requested_scope() -> str:
    """这次启动想要的共享口径：'share'、'local'，或空串（没指定）。"""
    return str(os.environ.get("QWEN_SHARE_SCOPE") or "").strip().lower()


def published_scope() -> str:
    """记录里那个监督进程是用什么口径起来的（没有记录就返回空串＝不知道）。"""
    record = read_json(SERVICE_RECORD) or {}
    value = record.get("share_scope")
    return value.strip().lower() if isinstance(value, str) else ""


def publish_identity(process, scope: str = "") -> None:
    """Record who this process is, and which sharing scope it serves, in ``SERVICE_RECORD``.

    Two callers have to agree on the field list: the starter (it records the child it just spawned,
    so a second launch cannot start a duplicate) and the supervisor itself (which rewrites the
    record for its own pid a moment later). When only the starter wrote the scope, the supervisor's
    rewrite dropped it, and every later ``stop``/``start`` read the record back as "unknown scope",
    decided the running supervisor was the wrong one and tried to retire it - which fails outright
    when that supervisor runs with administrator rights. Measured twice on this machine, both times
    reported as "口径不同、无法结束" on a service that was in fact healthy and sharing fine.
    """
    stamp = record_process(process, SCRIPT)
    if scope:
        stamp["share_scope"] = scope
    write_json(SERVICE_RECORD, stamp)


def ensure_supervisor():
    with supervisor_lock(ROOT / "service-control.lock", timeout=45,
                         fallback=ROOT / "service-control.run.lock") as note:
        if note:
            logging.warning(note)
        STOP.unlink(missing_ok=True)
        wanted = requested_scope()
        published = published_scope()
        supervisor = owned_process(SERVICE_RECORD, SCRIPT)
        if supervisor is not None and wanted and published != wanted:
            # 真机踩到（09-23 18:2x，共享"开了却连不上"）：监督进程是**上一次启动**留下的，
            # 共享口径写在它自己的环境里，而它每次重拉网关都沿用那个口径。于是"这次要共享"
            # 永远不生效：网关只监听回环，启动脚本却照样把公网地址打出来。口径不同就换掉它；
            # `published` 为空说明记录还是老格式（没有那个字段），既然调用方明确说了要什么，
            # 就不去猜——一样换掉。
            # 换不掉（例如它是以管理员身份起的、权限比调用者高）必须**明说**并给出办法，
            # 否则就是静默起了一个本机网关。
            try:
                terminate_owned(supervisor, SERVICE_RECORD, SCRIPT)
            except (psutil.Error, RuntimeError, OSError) as exc:
                raise RuntimeError(
                    f"监督进程 pid={supervisor.pid} 是以「{published or '未知'}」口径启动的，"
                    f"而这次要的是「{wanted}」；它无法被结束（{exc}）。"
                    "请以管理员身份结束它再重试："
                    f'powershell -Command "Stop-Process -Id {supervisor.pid} -Force"'
                ) from exc
            logging.warning("Ended a supervisor started with scope=%s (wanted %s)",
                            published or "unknown", wanted)
        if not owned_process(SERVICE_RECORD, SCRIPT):
            child = spawn(SCRIPT, "supervisor", detached=True)
            # Publish identity while holding the start lock, before a second BAT
            # can race the supervisor's initialization and launch another one.
            publish_identity(psutil.Process(child.pid), wanted)


def start():
    # A deliberate stop leaves this marker so the supervisor does not respawn the
    # gateway; the supervisor's whole job is to check it and exit. Starting is the
    # owner overriding that decision, so the marker is cleared first. Without this,
    # starting after "stop sharing and quit" in the panel could never come back up.
    STOP.unlink(missing_ok=True)
    # A recorded gateway that is alive but not listening is a wedged one: it serves nothing, the
    # supervisor cannot adopt it (adoption is by port) and every later start waits out its whole
    # deadline — the owner sees "打不开". End it here, identity-checked, so the launch can proceed.
    wedged = owned_process(MANAGER_RECORD, MANAGER)
    if wedged is not None and not port_open(GATEWAY_PORT):
        logging.warning("Gateway pid=%s is alive but nothing is listening on %s; ending it",
                        wedged.pid, GATEWAY_PORT)
        try:
            terminate_owned(wedged, MANAGER_RECORD, MANAGER)
        except (psutil.Error, RuntimeError, OSError) as exc:
            logging.warning("Could not end the wedged gateway: %s", exc)
    ensure_supervisor()
    deadline = time.monotonic() + 60
    next_recover_at = 0
    next_supervisor_check = time.monotonic() + 2
    while time.monotonic() < deadline:
        if time.monotonic() >= next_supervisor_check:
            # An existing supervisor may have been finishing an earlier Stop.
            # Recover if it exits during this launch instead of waiting 60 s.
            ensure_supervisor()
            next_supervisor_check = time.monotonic() + 2
        # With sharing on, the gate answers 401 until this process holds a session,
        # so the status poll has to log in first. Without this the launcher waits out
        # its whole deadline against a gateway that is already healthy.
        if gateway_status(timeout=2) is None:
            ensure_session()
        status = gateway_status()
        if status:
            if status.get("state") in {"error", "recovering"}:
                if time.monotonic() >= next_recover_at:
                    next_recover_at = time.monotonic() + 3
                    try:
                        status = http_json(gateway_url("/local/recover"), method="POST")
                        if not isinstance(status, dict):
                            status = None
                    except urllib.error.HTTPError as exc:
                        if exc.code == 409:
                            next_recover_at = time.monotonic() + 1
                        elif exc.code < 500:
                            raise RuntimeError(f"Gateway recovery request failed: HTTP {exc.code}") from exc
                        status = None
                    except (OSError, ValueError):
                        # Recovery may race a watchdog restart; keep the launcher
                        # alive instead of asking the user to run the BAT again.
                        status = None
            if status and status.get("state") in {"ready", "starting", "switching", "waiting"}:
                return {"url": URL, "state": status["state"]}
        time.sleep(0.5)
    details = read_json(SERVICE_STATE) or {}
    raise RuntimeError(details.get("error") or "The website did not respond within 60 seconds. Check Qwen-Local/logs/supervisor.log.")


def backend_idle():
    """Whether the model process is provably not generating.

    Patience is the point. The first `/slots` after a long idle period takes *tens of seconds* on
    this build — measured again while recovering a wedged gateway: the endpoint answered
    `is_processing=false` in **under 45 s** while the process had burned 0.02 s of CPU in 4 s, so a
    20 s budget (and the 5 s budget before it) still judged an idle backend "busy" and refused
    every stop exactly when a stop was the only way out.

    So the budget is now 60 s per attempt with one retry. ``/props`` is tried first and answers
    immediately when the server reports itself asleep, which is the common idle case; the long
    timeout only costs anything on the recovery path (a healthy stop shuts the gateway down
    gracefully and never reaches here).
    """
    try:
        props = http_json("/props", base="http://127.0.0.1:8082", timeout=5)
    except (OSError, ValueError):
        return False
    if isinstance(props, dict) and props.get("is_sleeping") is True:
        # A sleeping server cannot be generating, and asking it for slots would wake it up.
        return True
    for _attempt in range(2):
        try:
            slots = http_json("/slots", base="http://127.0.0.1:8082", timeout=60)
        except (OSError, ValueError):
            continue
        if isinstance(slots, list) and slots:
            return all(isinstance(slot, dict) and slot.get("is_processing") is False
                       for slot in slots)
    return False


def terminate_owned(process, record_path, script=None):
    current = owned_process(record_path, script) if script else owned_backend()
    if current is None or current.pid != process.pid:
        raise RuntimeError("Process ownership changed; no process was stopped.")
    current.terminate()
    try:
        current.wait(timeout=15)
    except psutil.TimeoutExpired:
        current = owned_process(record_path, script) if script else owned_backend()
        if current:
            current.kill()
            current.wait(timeout=10)


# ------------------------------------------------------------------ giving the card back
# "Stopped" has to mean the card is free, not that one process happened to exit. Three
# different processes can be holding video memory at that moment: the chat backend, the
# voice recogniser, and voice mode's own copy of the 9B. The synthesiser is a fourth
# process that holds no VRAM by default but does hold its own 1.1 GB of weights.
#
# They are identified by the port they listen on rather than by process name, and every
# candidate is re-checked against its command line before anything is terminated — this
# runs on the owner's machine, where "python.exe" alone means nothing.

# The chat backend, plus voice mode's recogniser and its own brain. Imported from the
# modules that own them so a re-pointed port cannot leave a leak behind.
BACKEND_PORT = 8082
# The synthesis worker's script and private interpreter; both must match before anything
# is terminated.
TTS_SCRIPT = "tts_server.py"


def model_ports():
    """Every port this service may be serving a model on."""
    from app.voice.voice_mode import ASR_PORT, BRAIN_PORT

    return (BACKEND_PORT, ASR_PORT, BRAIN_PORT)


def llama_servers():
    """All llama-server processes on this machine, ours or not."""
    found = []
    for process in psutil.process_iter(["name", "exe", "cmdline"]):
        try:
            if (process.info["name"] or "").lower() != "llama-server.exe":
                continue
        except psutil.Error:
            continue
        found.append(process)
    return found


def is_ours(process):
    """Whether a process is one of this service's model servers.

    Port is the first answer, because that is what makes it ours operationally; the
    executable check is what keeps an unrelated llama-server on another port untouched.
    Reads through `info` when psutil already cached it and through the live accessors
    otherwise, so the same function works on a `process_iter()` result and on a plain
    `psutil.Process`.
    """
    try:
        command = process.cmdline()
    except (psutil.Error, OSError):
        return False
    cached = getattr(process, "info", None) or {}
    exe = str(cached.get("exe") or "")
    if not exe:
        try:
            exe = str(process.exe())
        except (psutil.Error, OSError):
            exe = ""
    if "llama-server" not in exe.lower() and not any(
            "llama-server" in part.lower() for part in command):
        return False
    ports = {str(port) for port in model_ports()}
    try:
        index = command.index("--port")
    except ValueError:
        return False
    return index + 1 < len(command) and command[index + 1] in ports


def tts_worker_processes():
    """The synthesis worker and its decoder children, matched by script and interpreter.

    Both the script and the private virtualenv are checked, so a stray python.exe running
    something else is never a target.
    """
    script = str(ROOT / "tools" / TTS_SCRIPT)
    venv = str(ROOT / "tools" / "tts-venv").lower()
    matches = []
    for process in psutil.process_iter(["name", "exe", "cmdline"]):
        try:
            if not (process.info["name"] or "").lower().startswith("python"):
                continue
            command = " ".join(process.info["cmdline"] or [])
        except psutil.Error:
            continue
        if script in command and venv in command.lower():
            matches.append(process)
    return matches


def terminate_tree(process, grace=8):
    """End a process and whatever it started. True if it is gone afterwards.

    Every step is guarded: a process owned by another account (or one that exits between two
    calls) answers with AccessDenied or NoSuchProcess, and an unguarded call there aborted the
    whole stop — leaving every later process unreleased because of one that could not be
    touched anyway.
    """
    try:
        children = process.children(recursive=True)
    except psutil.Error:
        children = []
    targets = [process, *children]
    for target in targets:
        try:
            target.terminate()
        except psutil.Error:
            continue
    try:
        _, alive = psutil.wait_procs(targets, timeout=grace)
    except psutil.Error:
        alive = []
        for target in targets:
            try:
                if target.is_running():
                    alive.append(target)
            except psutil.Error:
                continue
    for target in alive:
        try:
            target.kill()
        except psutil.Error:
            continue
    with suppress(psutil.Error):
        psutil.wait_procs(alive, timeout=5)
    try:
        return not process.is_running()
    except psutil.Error:
        # A process this account cannot even inspect is treated as gone rather than as
        # "released": claiming to have released it would be the lie worth avoiding.
        return True


def card_state():
    """(used_mib, free_mib) from nvidia-smi, or None when it cannot be read."""
    try:
        completed = subprocess.run(
            ["nvidia-smi", "--query-gpu=memory.used,memory.free",
             "--format=csv,noheader,nounits"],
            capture_output=True, text=True, timeout=15, check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    line = (completed.stdout or "").strip().splitlines()
    if not line:
        return None
    parts = [part.strip() for part in line[0].split(",")]
    if len(parts) != 2 or not all(part.isdigit() for part in parts):
        return None
    return int(parts[0]), int(parts[1])


def release_card(expect_free_mib=2048, wait_seconds=20):
    """Terminate every model process this service owns and report what the card looks like.

    Called after the gateway has exited, so anything still holding a model port is by
    definition an orphan: a recogniser whose parent was killed, a brain left by a failed
    voice-mode start, or a backend that outlived a hard restart. Killing them here is the
    difference between "the window closed" and "the memory is free".
    """
    before = card_state()
    killed, remaining = [], []
    for process in llama_servers():
        try:
            if not is_ours(process):
                continue
            identity = f"llama-server pid={process.pid} port={model_port_of(process)}"
        except psutil.Error:
            # A process this account cannot read is not ours to judge; leaving it alone is
            # the only safe answer, and saying so is better than claiming it was released.
            continue
        if terminate_tree(process):
            killed.append(identity)
        else:
            remaining.append(identity)
    for process in tts_worker_processes():
        try:
            identity = f"tts pid={process.pid}"
        except psutil.Error:
            continue
        if terminate_tree(process):
            killed.append(identity)
        else:
            remaining.append(identity)

    # The driver releases an exited process's memory asynchronously, and a process that was
    # signalled a moment ago may still be winding down; a short wait turns a misleading "still
    # in use" reading into the truth rather than a flaky report. The processes are re-enumerated
    # on every pass, because a process that finishes during the wait has been released — judging
    # it once, immediately after the signal, reported a successful release as a failure.
    deadline = time.monotonic() + wait_seconds
    after = card_state()
    while True:
        ours_now = []
        for process in llama_servers():
            try:
                if is_ours(process):
                    ours_now.append(f"llama-server pid={process.pid} port={model_port_of(process)}")
            except psutil.Error:
                continue
        remaining = ours_now
        after = card_state()
        if not remaining and (after is None or after[1] >= expect_free_mib):
            break
        if time.monotonic() >= deadline:
            break
        time.sleep(1)
    return {
        "killed": killed,
        "remaining": remaining,
        "free_mib_before": before[1] if before else None,
        "free_mib_after": after[1] if after else None,
        "card_free": not remaining,
    }


def model_port_of(process):
    """The `--port` a model process was started with, as a string, or ''."""
    try:
        command = process.cmdline()
        index = command.index("--port")
    except (psutil.Error, ValueError):
        return ""
    return command[index + 1] if index + 1 < len(command) else ""


def stop():
    with file_lock(ROOT / "service-control.lock", timeout=45):
        status = gateway_status()
        if status and status.get("active_requests", 0):
            raise RuntimeError("Qwen is generating. Wait for the current response to finish.")
        process = adopt_manager()
        if status and process is None:
            raise RuntimeError("The gateway process could not be verified; it was not stopped.")
        STOP.write_text(datetime.now().isoformat(), encoding="ascii")
        try:
            if status:
                http_json(gateway_url("/local/shutdown"), method="POST", timeout=5)
                if process:
                    try:
                        process.wait(timeout=35)
                    except psutil.TimeoutExpired:
                        # The gateway answered /local/shutdown and then never exited: it stops
                        # serving first and keeps waiting for the model to go idle, so the machine
                        # is left with a process that holds nothing and blocks every later start
                        # ("Another Qwen operation…", or a launcher that waits out its deadline).
                        # The identity was verified above and the request was delivered, so this
                        # is our own wedged gateway: end it, and say so.
                        logging.warning("Gateway shutdown did not finish in 35 s; ending pid=%s",
                                        process.pid)
                        if process_alive(process):
                            terminate_owned(process, MANAGER_RECORD, MANAGER)
            elif process:
                backend = owned_backend()
                if port_open(8082) and (backend is None or not backend_idle()):
                    raise RuntimeError("Backend activity cannot be ruled out. Wait until Qwen responds before stopping.")
                terminate_owned(process, MANAGER_RECORD, MANAGER)
            backend = owned_backend()
            if backend:
                if not backend_idle():
                    raise RuntimeError("The remaining model process is busy or unavailable; it was not stopped.")
                terminate_owned(backend, ROOT / "backend.process.json")
            # The gateway releases its own processes on the way out; this is the part that
            # survives it going down badly. Without it a recogniser or a voice brain whose
            # parent was killed keeps its weights on the card after "Qwen stopped".
            released = release_card()
            for name in ("backend.process.json", "backend.pid", "manager.pid", MANAGER_RECORD.name):
                (ROOT / name).unlink(missing_ok=True)
        except Exception:
            STOP.unlink(missing_ok=True)
            raise
    deadline = time.monotonic() + 10
    while owned_process(SERVICE_RECORD, SCRIPT) and time.monotonic() < deadline:
        time.sleep(0.2)
    if owned_process(SERVICE_RECORD, SCRIPT):
        raise RuntimeError("Supervisor is still finishing shutdown. Check Qwen-Local/logs.")
    if not released["card_free"]:
        # Reported rather than raised: the service is down either way, and hiding a
        # leftover process behind a failed stop would make the next start look broken.
        logging.warning("Card not fully released: %s", ", ".join(released["remaining"]))
    return {
        "state": "stopped",
        "message": "Qwen stopped; automatic restart is disabled.",
        "released": released,
    }


def main():
    if len(sys.argv) != 2 or sys.argv[1] not in {"start", "stop", "run"}:
        raise SystemExit("Usage: qwen_service.py start|stop|run")
    try:
        result = {"start": start, "stop": stop, "run": supervise}[sys.argv[1]]()
        if result is not None:
            print(json.dumps(result, ensure_ascii=True))
    except Exception as exc:
        print(f"Qwen: {exc}", file=sys.stderr)
        raise SystemExit(1) from exc


if __name__ == "__main__":
    main()
