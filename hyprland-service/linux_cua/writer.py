"""One virtual keyboard per request; host permission gates each complete key pair."""
import os
from pathlib import Path
import select
import subprocess
import time

DRIVER = Path(__file__).resolve().parent.parent / "bin/linux-cua-wtype"


class WriteStopped(RuntimeError):
    def __init__(self, message, completed=0):
        super().__init__(message)
        self.completed = completed


def _supervise(text, env, check, driver, host_key=None):
    completed = 0
    child = None
    descriptors = []
    try:
        if not os.access(driver, os.X_OK):
            raise WriteStopped("Private text driver missing; run build-driver.py")
        check()
        permission_read, permission_write = os.pipe()
        descriptors.extend((permission_read, permission_write))
        ack_read, ack_write = os.pipe()
        descriptors.extend((ack_read, ack_write))
        child_env = dict(env)
        # C mbstowcs must decode UTF-8 independently of the controller's locale.
        child_env["LC_ALL"] = "C.UTF-8"
        child_env["LINUX_CUA_STEP_FD"] = str(permission_read)
        child_env["LINUX_CUA_STEP_ACK_FD"] = str(ack_write)
        arguments = ([str(driver), "--host-key", host_key] if host_key is not None
                     else [str(driver), "-"])
        child = subprocess.Popen(arguments, stdin=subprocess.PIPE,
                                 stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
                                 env=child_env, pass_fds=(permission_read, ack_write),
                                 start_new_session=True)
        for descriptor in (permission_read, ack_write):
            os.close(descriptor)
            descriptors.remove(descriptor)
        # This only queues literal data. The native child cannot press a key
        # until the separate permission pipe receives a host T byte.
        child.stdin.write(text.encode("utf-8"))
        child.stdin.close()
        for _ in range(1 if host_key is not None else len(text)):
            # The child must finish keyboard/keymap preparation before host
            # authority is refreshed. Never queue permission during setup.
            readable, _, _ = select.select([ack_read], [], [], 2.0)
            if not readable or os.read(ack_read, 1) != b"R":
                raise WriteStopped("Input driver readiness failed or timed out", completed)
            check()
            os.write(permission_write, b"T")
            readable, _, _ = select.select([ack_read], [], [], 2.0)
            if not readable or os.read(ack_read, 1) != b"D":
                raise WriteStopped("Text driver stopped or acknowledgement timed out", completed)
            completed += 1
        child.wait(timeout=2)
        if child.returncode:
            raise WriteStopped("Text driver exited unsuccessfully", completed)
        # The acknowledgement confirms a compositor key pair, not that an app
        # rendered the intended text. Allow one short rendering interval, then
        # require the usual screenshot verification.
        time.sleep(0.12)
        check()
        return completed
    except Exception as error:
        if isinstance(error, WriteStopped):
            raise
        raise WriteStopped(str(error), completed) from error
    finally:
        for descriptor in descriptors:
            os.close(descriptor)
        if child is not None:
            if child.poll() is None:
                child.terminate()
                try:
                    child.wait(timeout=1)
                except subprocess.TimeoutExpired:
                    child.kill()
                    child.wait()
            if child.stdin and not child.stdin.closed:
                child.stdin.close()
            if child.stderr:
                child.stderr.close()


def write_text(text, env, check, driver=DRIVER):
    return _supervise(text, env, check, driver)


def write_key(key, env, check, driver=DRIVER):
    # Import lazily: Desktop calls this only after its module is initialized.
    from .desktop import NAVIGATION_KEYS
    if not isinstance(key, str) or key not in NAVIGATION_KEYS:
        raise WriteStopped("Unsupported navigation key")
    return _supervise("", env, check, driver, host_key=key)
