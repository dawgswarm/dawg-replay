# /// script
# requires-python = ">=3.10"
# dependencies = ["dawg-replay-kit>=1.4,<2"]
# ///

# /// dawg-script
# contract = "1.4"
# name = "demo-pong"
# title = "Демо: понг"
# description = "Без браузера и сайта: пишет ball.txt со счётчиком. Вместе с demo-ping даёт круг связей для проверки предела в сутки."
# site = "Демо"
# tags = ["Демо", "Связи"]
# version = 1
#
# [[params]]
# name = "ball"
# type = "file"
# title = "Мяч от другого скрипта"
# description = "Приходит по связи; без него счёт с нуля"
#
# [[outputs]]
# name = "ball.txt"
# title = "Мяч"
# main = true
# ///

"""Демо связей (M6): круг пинг → понг → пинг разрешён только с пределом запусков в сутки на одной из связей."""

from pathlib import Path

import dawg_replay_kit as kit

ME = "понг"
ball = kit.params.ball
hits = int(Path(ball["path"]).read_text(encoding="utf-8").split()[0]) if ball else 0
hits += 1
path = kit.output_dir / "ball.txt"
path.write_text(f"{hits} {ME}\n", encoding="utf-8")
kit.artifact(path, "Мяч", "text/plain")
kit.result("ok", f"удар {hits}: {ME}", count=hits, main="ball.txt")
