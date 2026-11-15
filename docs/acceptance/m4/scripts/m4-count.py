# /// script
# requires-python = ">=3.10"
# dependencies = ["dawg-replay-kit>=1.3,<2"]
# ///

# /// dawg-script
# contract = "1.3"
# name = "m4-count"
# title = "Приёмка M4: счётчик в итоге"
# description = "Без браузера. Итог с count для детектора тихой поломки."
# version = 1
#
# [[params]]
# name = "n"
# type = "int"
# title = "Сколько записей «собрано»"
# default = 50
# min = 0
# ///

"""Приёмка M4 (dr-89): `result.count` в детекторе тихой поломки, файл с одной шапкой при count=0."""

import dawg_replay_kit as kit

n = kit.params.n
path = kit.output_dir / "items.csv"
path.write_text("id\n" + "".join(f"{i}\n" for i in range(n)), encoding="utf-8")
kit.artifact(path, "Записи (CSV)", "text/csv")
kit.result("ok", f"собрано {n}", count=n)
