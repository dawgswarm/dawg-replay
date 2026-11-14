"""Демо-каталог dawg-replay: примеры скриптов, пресеты, расписания и неделя истории запусков.

    uv run --all-packages python testing/demo_home.py            # заполнить и открыть UI
    uv run --all-packages python testing/demo_home.py --seed-only

Всё живёт во временном каталоге (или в --home): своя база, свой фейковый crontab,
пустое хранилище паролей (keyring null). Настоящий crontab, ключ DAWG и сеть не
используются: base_url указывает на закрытый порт, ключа нет.

Что внутри:

- ozon-search-cards и wb-search-cards из examples/, третий скрипт sports-results
  пишется на месте;
- пресеты и три расписания (каждый день, каждые 6 часов, по понедельникам);
- плановые запуски за неделю с минутами браузеров (события session);
- тихая поломка: последний плановый запуск Ozon собрал 6 строк при обычных ~120;
- упавший запуск WB с traceback, в котором есть токен в адресе (он скрыт);
- отменённый и ручные запуски;
- прошлый визит в UI вчера вечером, поэтому на главной виден блок «С прошлого визита».
"""

from __future__ import annotations

import argparse
import csv
import json
import os
import random
import shlex
import signal
import subprocess
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
EXAMPLES = ROOT / "examples"
CLOSED_URL = "http://127.0.0.1:9"  # закрытый порт: сеть не нужна, запросы к DAWG сразу падают

FAKE_CRONTAB = '''\
"""Фейковый crontab демо: -l и - над файлом DEMO_CRONTAB_FILE."""
import os, sys
from pathlib import Path
path = Path(os.environ["DEMO_CRONTAB_FILE"])
if sys.argv[1:] == ["-l"]:
    if not path.exists():
        print("crontab: no crontab for demo", file=sys.stderr)
        sys.exit(1)
    sys.stdout.buffer.write(path.read_bytes())
elif sys.argv[1:] == ["-"]:
    path.write_bytes(sys.stdin.buffer.read())
else:
    sys.exit(2)
'''

SPORTS_SCRIPT = '''\
# /// script
# requires-python = ">=3.10"
# dependencies = ["dawg-replay-kit>=1.1,<2"]
# ///

# /// dawg-script
# contract = "1.1"
# name = "sports-results"
# title = "Футбол: результаты матчей тура"
# description = "Собирает счёт, составы и статистику матчей выбранного тура."
# version = 2
# timeout_minutes = 30
# site = "Спорт"
# tags = ["футбол", "статистика"]
#
# [[params]]
# name = "league"
# type = "string"
# title = "Лига"
# required = true
#
# [[params]]
# name = "round"
# type = "int"
# title = "Тур"
# default = 1
# min = 1
# max = 38
# ///

print("демо: скрипт не запускается")
'''


def env_for(home: Path) -> dict[str, str]:
    """Окружение демо: свой каталог данных, фейковый crontab, пустой keyring, без ключа DAWG."""
    sched = home.parent / "scheduler"
    sched.mkdir(parents=True, exist_ok=True)
    fake = sched / "fake_crontab.py"
    fake.write_text(FAKE_CRONTAB, encoding="utf-8")
    env = {k: v for k, v in os.environ.items() if k != "DAWG_API_KEY"}
    env.update({
        "DAWG_REPLAY_HOME": str(home),
        "DAWG_REPLAY_SCHEDULER": "cron",
        "DAWG_REPLAY_CRONTAB": json.dumps([sys.executable, str(fake)]),
        "DEMO_CRONTAB_FILE": str(sched / "crontab"),
        "PYTHON_KEYRING_BACKEND": "keyring.backends.null.Keyring",
        "NO_PROXY": "*",
        "no_proxy": "*",
    })
    return env


def iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


# --- содержимое запусков -----------------------------------------------------------------

PRODUCTS = ("Ноутбук {b} 15.6\" i5/16/512", "Смартфон {b} 8/256", "Кроссовки {b} беговые", "Наушники {b} TWS")
BRANDS = ("Lenovo", "ASUS", "HUAWEI", "Xiaomi", "Acer", "HONOR", "Nike", "Asics", "Sony", "JBL")
SELLERS = ("Ozon", "ТехноМир", "Компьютерный двор", "Электросила", "SportLine")


def cards(rng: random.Random, n: int, kind: int) -> list[dict]:
    out = []
    for i in range(n):
        b = rng.choice(BRANDS)
        out.append({
            "sku": str(1_200_000_000 + rng.randrange(99_999_999)),
            "name": PRODUCTS[kind].format(b=b),
            "price": rng.randrange(1990, 89990, 10),
            "rating": round(rng.uniform(3.9, 5.0), 1),
            "seller": rng.choice(SELLERS),
            "position": i + 1,
        })
    return out


def write_run(app, rid: str, *, script: str, preset: str | None, params: dict, start: datetime, minutes: float,
              status: str, rows: int, kind: int, schedule_id: str | None = None, reason: str | None = None,
              stderr: str = "", errors: tuple[str, ...] = (), summary: str | None = None, browsers: int = 1,
              pages: int = 3, rng: random.Random) -> None:
    version = app.registry.version(script)
    app.runs.reserve(run_id=rid, script_name=script, script_version=version.version, script_path=str(version.path),
                     draft=False, preset=preset, params=params, timeout_seconds=3600, limit=1000,
                     schedule_id=schedule_id)
    d = app.paths.run_dir(rid)
    (d / "output").mkdir(parents=True, exist_ok=True)
    end = start + timedelta(minutes=minutes)
    ev: list[dict] = []

    def at(frac: float) -> str:
        return iso(start + (end - start) * frac)

    ev.append({"t": "stage", "name": "Открываю браузер", "ts": at(0.01)})
    for b in range(browsers):
        sid = f"demo-{rid[-6:]}-{b}"
        ev.append({"t": "session", "action": "provisioning", "ts": at(0.02)})
        ev.append({"t": "session", "action": "open", "session_id": sid, "browser_id": f"br-{b}", "ts": at(0.04)})
    ev.append({"t": "stage", "name": "Обхожу выдачу", "ts": at(0.08)})
    data = cards(rng, rows, kind)
    per_page = max(1, -(-rows // pages)) if rows else 0
    for p in range(pages):
        chunk = data[p * per_page:(p + 1) * per_page] if per_page else []
        if not chunk and rows:
            break
        frac = 0.1 + 0.8 * (p + 1) / pages
        if chunk:
            ev.append({"t": "rows", "table": "cards", "columns": list(chunk[0]), "rows": chunk, "ts": at(frac)})
        ev.append({"t": "progress", "id": "pages", "label": "Страницы выдачи", "done": p + 1, "total": pages, "ts": at(frac)})
        ev.append({"t": "metric", "key": "cards", "label": "Собрано карточек", "value": min(rows, (p + 1) * per_page),
                   "ts": at(frac)})
    if data:
        ev.append({"t": "metric", "key": "min_price", "label": "Самая низкая цена", "value": min(r["price"] for r in data),
                   "unit": "₽", "ts": at(0.92)})
    for msg in errors:
        ev.append({"t": "log", "level": "error", "msg": msg, "ts": at(0.93)})
    if status != "failed" or reason != "exit_code" or data:
        ev.append({"t": "log", "level": "info", "msg": f"Собрано {len(data)} карточек", "ts": at(0.94)})
    if data:
        csv_path = d / "output" / "cards.csv"
        with open(csv_path, "w", encoding="utf-8-sig", newline="") as f:
            w = csv.DictWriter(f, fieldnames=list(data[0]))
            w.writeheader()
            w.writerows(data)
        ev.append({"t": "artifact", "path": "cards.csv", "label": "Карточки (CSV)", "mime": "text/csv", "ts": at(0.95)})
    for b in range(browsers):
        ev.append({"t": "session", "action": "close", "session_id": f"demo-{rid[-6:]}-{b}", "ts": at(0.98)})
    if summary is not None and status != "failed":
        ev.append({"t": "result", "status": status, "summary": summary, "ts": at(0.99)})
    elif summary is not None and reason == "exit_code" and not stderr:
        ev.append({"t": "result", "status": "failed", "summary": summary, "ts": at(0.99)})
    (d / "events.jsonl").write_text("".join(json.dumps(e, ensure_ascii=False) + "\n" for e in ev), encoding="utf-8")
    (d / "stdout.log").write_text("", encoding="utf-8")
    (d / "stderr.log").write_text(stderr, encoding="utf-8")
    exit_code = 0 if status != "failed" else (1 if reason == "exit_code" else None)
    app.runs.finish(rid, status=status, reason=reason, exit_code=exit_code, summary=summary, finished_at=iso(end))
    app.db.execute("UPDATE runs SET created_at = ?, started_at = ?, heartbeat_at = ? WHERE id = ?",
                   (iso(start), iso(start + timedelta(seconds=3)), iso(end), rid))


def run_id(dt: datetime, n: int) -> str:
    return dt.astimezone(timezone.utc).strftime("%Y%m%d-%H%M%S-") + f"{n:06x}"


# --- заполнение --------------------------------------------------------------------------


def seed(home: Path, now: datetime | None = None) -> dict:
    """Заполняет каталог данных. Возвращает номера запусков, интересных для скриншотов."""
    os.environ.update(env_for(home))
    from dawg_replay.app import App
    from dawg_replay.schedules import Schedules, Spec

    now = (now or datetime.now(timezone.utc)).astimezone()
    rng = random.Random(20261115)
    n = iter(range(1, 10_000))
    sports = home.parent / "sports-results.py"
    sports.write_text(SPORTS_SCRIPT, encoding="utf-8")

    with App(home) as app:
        app.set_config("base_url", CLOSED_URL)
        for f in (EXAMPLES / "ozon-search-cards" / "script.py", EXAMPLES / "wb-search-cards" / "script.py", sports):
            app.registry.add(f)
        app.save_preset("ozon-search-cards", "ноутбуки", {"query": "ноутбук", "max_pages": 3})
        app.save_preset("ozon-search-cards", "смартфоны", {"query": "смартфон", "max_pages": 2, "sort": "price"})
        app.save_preset("wb-search-cards", "кроссовки", {"query": "кроссовки беговые", "max_pages": 2})
        app.save_preset("sports-results", "рпл", {"league": "РПЛ", "round": 15})
        sch = Schedules(app)
        daily = sch.add("ozon-search-cards", "ноутбуки", Spec.daily("09:00"))
        hours = sch.add("wb-search-cards", "кроссовки", Spec.every_hours(6))
        sch.add("sports-results", "рпл", Spec.weekly("mon", "07:30"))

        ids: dict[str, str] = {}
        today9 = now.replace(hour=9, minute=0, second=0, microsecond=0)
        if today9 > now - timedelta(minutes=30):
            today9 -= timedelta(days=1)
        # Ozon «ноутбуки», каждый день в 09:00: неделя по ~120 строк, последний собрал 6.
        for day in range(6, 0, -1):
            t = today9 - timedelta(days=day)
            rid = run_id(t, next(n))
            rows = rng.randrange(112, 131)
            write_run(app, rid, script="ozon-search-cards", preset="ноутбуки", params={"query": "ноутбук", "max_pages": 3},
                      start=t, minutes=rng.uniform(6, 9), status="ok", rows=rows, kind=0, schedule_id=daily.id,
                      summary=f"«ноутбук»: {rows} карточек с 3 страниц выдачи", rng=rng)
        rid = run_id(today9, next(n))
        write_run(app, rid, script="ozon-search-cards", preset="ноутбуки", params={"query": "ноутбук", "max_pages": 3},
                  start=today9, minutes=4.2, status="ok", rows=6, kind=0, schedule_id=daily.id,
                  summary="«ноутбук»: 6 карточек с 3 страниц выдачи", rng=rng)
        ids["silent"] = rid
        # Ozon «смартфоны» вручную позавчера.
        t = now - timedelta(days=2, hours=3)
        rid = run_id(t, next(n))
        write_run(app, rid, script="ozon-search-cards", preset="смартфоны",
                  params={"query": "смартфон", "max_pages": 2, "sort": "price"}, start=t, minutes=5.5, status="ok",
                  rows=78, kind=1, pages=2, summary="«смартфон»: 78 карточек с 2 страниц выдачи", rng=rng)
        ids["manual"] = rid

        # WB «кроссовки», каждые 6 часов: последние двое суток, последний упал.
        slots = []
        t = now.replace(minute=0, second=0, microsecond=0)
        t -= timedelta(hours=t.hour % 6)
        while len(slots) < 8:
            if t < now - timedelta(minutes=20):
                slots.append(t)
            t -= timedelta(hours=6)
        slots.reverse()
        for i, t in enumerate(slots):
            rid = run_id(t, next(n))
            last = i == len(slots) - 1
            if last:
                stderr = (
                    "Traceback (most recent call last):\n"
                    '  File "script.py", line 214, in main\n'
                    "    page.wait_for_selector(\".product-card\", timeout=30000)\n"
                    "playwright._impl._errors.TimeoutError: Page.wait_for_selector: Timeout 30000ms exceeded.\n"
                    "  waiting for locator(\".product-card\") to be visible\n"
                    "  last request: https://search.wb.ru/exactmatch/ru/common/v9/search?query=кроссовки&token=wbTK_9f8e7d6c5b4a&page=1\n"
                )
                write_run(app, rid, script="wb-search-cards", preset="кроссовки",
                          params={"query": "кроссовки беговые", "max_pages": 2}, start=t, minutes=1.6, status="failed",
                          reason="exit_code", rows=0, kind=2, schedule_id=hours.id, stderr=stderr, pages=1,
                          errors=("Карточки не появились за 30 секунд: страница выдачи изменилась",), rng=rng)
                ids["failed"] = rid
            else:
                rows = rng.randrange(180, 201)
                write_run(app, rid, script="wb-search-cards", preset="кроссовки",
                          params={"query": "кроссовки беговые", "max_pages": 2}, start=t, minutes=rng.uniform(3, 5),
                          status="ok", rows=rows, kind=2, schedule_id=hours.id, pages=2,
                          summary=f"«кроссовки беговые»: {rows} карточек", rng=rng)

        # Футбол: ручной успешный два дня назад и отменённый вчера.
        t = now - timedelta(days=2, hours=6)
        rid = run_id(t, next(n))
        write_run(app, rid, script="sports-results", preset="рпл", params={"league": "РПЛ", "round": 15}, start=t,
                  minutes=2.4, status="ok", rows=8, kind=3, pages=1, browsers=2,
                  summary="РПЛ, 15 тур: 8 матчей", rng=rng)
        t = now - timedelta(days=1, hours=2)
        rid = run_id(t, next(n))
        write_run(app, rid, script="sports-results", preset="рпл", params={"league": "РПЛ", "round": 15}, start=t,
                  minutes=0.8, status="failed", reason="cancelled", rows=0, kind=3, pages=1, rng=rng)

        # Прошлый визит вчера вечером: ночные запуски попадут в «С прошлого визита».
        evening = (now - timedelta(days=1)).replace(hour=21, minute=40, second=0, microsecond=0)
        (home / "ui-state.json").write_text(json.dumps({"last_seen": iso(evening)}), encoding="utf-8")
        if today9 < evening:  # демо запустили рано утром: тихая поломка вчерашняя, граница раньше неё
            (home / "ui-state.json").write_text(json.dumps({"last_seen": iso(today9 - timedelta(hours=2))}),
                                                encoding="utf-8")
    return ids


def start_ui(home: Path, port: int | None = None) -> tuple[subprocess.Popen, str]:
    """Запускает `dawg-replay ui --no-open` с окружением демо. Возвращает процесс и ссылку с токеном."""
    argv = [sys.executable, "-m", "dawg_replay", "ui", "--no-open"]
    if port:
        argv += ["--port", str(port)]
    proc = subprocess.Popen(argv, env=env_for(home), stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
                            encoding="utf-8")
    assert proc.stdout is not None
    for line in proc.stdout:
        if "token=" in line:
            return proc, line.split()[-1]
    raise SystemExit("dawg-replay ui не напечатал ссылку")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--home", type=Path, help="каталог данных демо (по умолчанию новый временный)")
    ap.add_argument("--port", type=int, help="порт UI")
    ap.add_argument("--seed-only", action="store_true", help="только заполнить и напечатать путь")
    args = ap.parse_args()
    home = (args.home or Path(tempfile.mkdtemp(prefix="dawg-replay-demo-")) / "home").resolve()
    if home.exists() and any(home.iterdir()):
        raise SystemExit(f"{home} не пуст: демо заполняет только новый каталог")
    seed(home)
    print(f"демо-каталог: {home}", flush=True)
    if args.seed_only:
        keys = ("DAWG_REPLAY_HOME", "DAWG_REPLAY_SCHEDULER", "DAWG_REPLAY_CRONTAB", "DEMO_CRONTAB_FILE",
                "PYTHON_KEYRING_BACKEND", "NO_PROXY")
        env = env_for(home)
        print("UI вручную (с тем же фейковым crontab и пустым keyring):")
        print(" ".join(f"{k}={shlex.quote(env[k])}" for k in keys) + " dawg-replay ui")
        return 0
    proc, url = start_ui(home, args.port)
    print(f"откройте: {url}", flush=True)
    print("остановить: Ctrl+C", flush=True)
    try:
        proc.wait()
    except KeyboardInterrupt:
        proc.send_signal(signal.SIGINT)
        proc.wait(timeout=20)
    return 0


if __name__ == "__main__":
    sys.exit(main())
