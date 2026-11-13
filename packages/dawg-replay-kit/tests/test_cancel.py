import signal
import threading

import pytest

from dawg_replay_kit import cancel


@pytest.fixture
def clean_signals(monkeypatch):
    saved = {s: signal.getsignal(s) for s in cancel._signals()}
    monkeypatch.setattr(cancel, "_received", None)
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
