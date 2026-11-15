"""Команды `dawg-replay telegram`: подключение личного бота, проверка, состояние, отключение."""

from __future__ import annotations

import argparse
import sys
from typing import Any

from dawg_replay import telegram
from dawg_replay.app import App
from dawg_replay.cli_store import _read_secret, run_handler
from dawg_replay.errors import ReplayError

CONNECT_COMMAND = "dawg-replay telegram connect"


def _status_text(st: dict[str, Any]) -> str:
    if st["keyring_error"]:
        lines = [f"хранилище паролей недоступно: {st['keyring_error']}"]
    elif st["connected"]:
        lines = [f"Telegram подключён: бот @{st['bot']}, чат «{st['chat_name']}»"]
    elif st["token_set"]:
        lines = [f"бот @{st['bot']} сохранён, чат не привязан: {CONNECT_COMMAND} --same-token"]
    else:
        lines = [f"Telegram не подключён: {CONNECT_COMMAND}"]
    if st["api_url_custom"]:
        lines.append(f"адрес Bot API: {st['api_url']}")
    if st["last_error"]:
        lines.append(f"последняя ошибка отправки ({st['last_error']['at']}): {st['last_error']['text']}")
    return "\n".join(lines)


def _connect(app: App, args: argparse.Namespace) -> tuple[dict, str]:
    if args.same_token:
        pairing = telegram.start_pairing(app)
    else:
        print("Токен бота: создайте бота у @BotFather (/newbot) и вставьте токен, который он пришлёт.", file=sys.stderr)
        pairing = telegram.start_pairing(app, _read_secret("Токен бота: ", CONNECT_COMMAND), command=CONNECT_COMMAND)
    minutes = max(1, round(args.wait / 60))
    print(
        f"Бот @{pairing.bot} найден. Откройте ссылку и нажмите «Start»:\n  {pairing.link}\n"
        f"или отправьте боту сообщение: {pairing.command}\n"
        f"Жду до {minutes} мин. Ctrl+C: прервать.",
        file=sys.stderr,
        flush=True,
    )
    try:
        got = telegram.wait_pairing(app, pairing, seconds=args.wait)
    except KeyboardInterrupt:
        raise ReplayError("cancelled", "привязка чата прервана; токен сохранён, повторить: "
                                       f"{CONNECT_COMMAND} --same-token") from None
    if got is None:
        raise ReplayError("telegram_timeout", f"сообщение {pairing.command} не пришло за {minutes} мин.; "
                                              f"повторить: {CONNECT_COMMAND} --same-token")
    return ({"telegram": telegram.status(app)},
            f"подключено: бот @{pairing.bot} пишет в чат «{got['chat_name']}»")


def _test(app: App, args: argparse.Namespace) -> tuple[dict, str]:
    telegram.send_test(app)
    return {"sent": True}, "проверочное сообщение отправлено, посмотрите в Telegram"


def _status(app: App, args: argparse.Namespace) -> tuple[dict, str]:
    st = telegram.status(app)
    return {"telegram": st}, _status_text(st)


def _disconnect(app: App, args: argparse.Namespace) -> tuple[dict, str]:
    telegram.disconnect(app)
    return {"telegram": telegram.status(app)}, "Telegram отключён, токен бота стёрт из хранилища паролей"


def register(sub: argparse._SubParsersAction, common: argparse.ArgumentParser) -> None:
    tg = sub.add_parser(
        "telegram",
        parents=[common],
        help="сообщения о запусках в Telegram: подключение и проверка",
        description="Личный бот пользователя пишет о запусках в Telegram. Токен бота хранится в системном хранилище паролей.",
    )
    tg_sub = tg.add_subparsers(dest="telegram_command", metavar="<действие>", required=True)

    p = tg_sub.add_parser(
        "connect",
        parents=[common],
        help="сохранить токен бота и привязать чат",
        description=(
            "Спрашивает токен бота (с терминала без эха, иначе первая строка stdin), проверяет его в Telegram "
            "и ждёт, пока вы отправите боту /start с одноразовым кодом."
        ),
    )
    p.add_argument("--same-token", action="store_true", help="не спрашивать токен, взять сохранённый")
    p.add_argument("--wait", type=int, default=telegram.PAIR_SECONDS, metavar="SECONDS",
                   help=f"сколько ждать /start, секунд ({telegram.PAIR_SECONDS})")
    p.set_defaults(func=lambda args: run_handler(_connect, args))

    p = tg_sub.add_parser("test", parents=[common], help="отправить проверочное сообщение")
    p.set_defaults(func=lambda args: run_handler(_test, args))

    p = tg_sub.add_parser("status", parents=[common], help="подключён ли Telegram, последняя ошибка отправки")
    p.set_defaults(func=lambda args: run_handler(_status, args))

    p = tg_sub.add_parser("disconnect", parents=[common], help="стереть токен бота и забыть чат")
    p.set_defaults(func=lambda args: run_handler(_disconnect, args))

    # Сюда другие модули добавляют свои действия telegram (например, `telegram set`): f(tg_sub, common).
