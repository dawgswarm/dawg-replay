import signal
import threading
import time

import pytest

from dawg_replay_kit import cancel


@pytest.fixture
def clean_signals(monkeypatch):
    saved = {s: signal.getsignal(s) for s in [*cancel._signals(), signal.SIGINT]}
    monkeypatch.setattr(cancel, "_received", None)
    monkeypatch.setattr(cancel, "_watcher", None)
    yield
    for s, h in saved.items():
        signal.signal(s, h)


def test_install_turns_signal_into_single_system_exit(clean_signals):
    for s in cancel._signals():
        signal.signal(s, signal.SIG_DFL)
    assert cancel.install() is True
    with pytest.raises(SystemExit) as e:
        cancel._handler(signal.SIGTERM, None)
    assert e.value.code == cancel.EXIT_CANCELLED
    assert cancel.received() == signal.SIGTERM
    cancel._handler(signal.SIGTERM, None)  # повтор не прерывает уборку


def test_install_keeps_script_handler(clean_signals):
    def mine(signum, frame):
        pass

    signal.signal(signal.SIGTERM, mine)
    cancel.install()
    assert signal.getsignal(signal.SIGTERM) is mine


def test_install_outside_main_thread_is_noop(clean_signals):
    got = []
    t = threading.Thread(target=lambda: got.append(cancel.install()))
    t.start()
    t.join()
    assert got == [False]


def test_stop_file_wakes_main_thread_from_sleep(clean_signals, tmp_path):
    for s in cancel._signals():
        signal.signal(s, signal.SIG_DFL)
    stop = tmp_path / "stop"
    assert cancel.install(str(stop)) is True
    threading.Timer(0.3, stop.touch).start()
    t0 = time.monotonic()
    with pytest.raises(SystemExit) as e:
        time.sleep(10)
    assert e.value.code == cancel.EXIT_CANCELLED and time.monotonic() - t0 < 3
    assert cancel.received() is not None


def test_no_stop_file_no_watcher(clean_signals, monkeypatch):
    monkeypatch.delenv(cancel.ENV_STOP_FILE, raising=False)
    cancel.install()
    assert cancel._watcher is None
