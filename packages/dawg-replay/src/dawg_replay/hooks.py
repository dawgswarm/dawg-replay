"""Хуки после завершения процесса запуска.

Вызываются после того, как процесс скрипта и его дерево завершились, с любым
исходом, в том числе для потерянных запусков (`failed (lost)`). Сюда
подключается страховка браузеров (ТЗ 5.6): она читает `events.jsonl` из
`outcome.run_dir` и освобождает сессии с `open` без `close`.

    from dawg_replay import hooks
    hooks.register(release_leftover_browsers)

Регистрация должна происходить при импорте ядра (например, из этого модуля),
потому что хуки фонового запуска выполняет процесс-супервизор, а не CLI.
Исключение в хуке не ломает итог запуска: текст ошибки попадает в поле
`error` запуска.
"""

from __future__ import annotations

from dawg_replay.runner import AfterExit

_hooks: list[AfterExit] = []


def register(hook: AfterExit) -> AfterExit:
    """Добавляет хук. Повторная регистрация того же объекта ничего не меняет. Можно как декоратор."""
    if hook not in _hooks:
        _hooks.append(hook)
    return hook


def unregister(hook: AfterExit) -> None:
    if hook in _hooks:
        _hooks.remove(hook)


def after_exit_hooks() -> list[AfterExit]:
    return list(_hooks)
