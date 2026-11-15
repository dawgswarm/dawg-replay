# /// script
# requires-python = ">=3.10"
# dependencies = ["dawg-replay-kit>=1.3,<2", "openpyxl"]
# ///

# /// dawg-script
# contract = "1.3"
# name = "m4-orders"
# title = "Приёмка M4: заказы по таблице"
# description = "Без браузера. Таблица с input, column_types, файлы результата: картинка, PDF, CSV, ссылки, главный файл."
# version = 1
#
# [[params]]
# name = "orders"
# type = "table"
# title = "Заказы"
# required = true
# columns = ["order", "qty"]
# input = ["file", "preset"]
# column_types = { qty = "int", price = "float", paid = "bool", day = "date" }
#
# [[params]]
# name = "fail_order"
# type = "string"
# title = "Заказ, который упадёт"
# default = "A-3"
# ///

"""Приёмка M4 (dr-89): типы колонок доходят до скрипта, итог с count/main/links, превью картинки и PDF."""

import csv
import json
import struct
import zlib

import dawg_replay_kit as kit


def png(width: int = 160, height: int = 90) -> bytes:
    """Простая PNG-картинка без зависимостей: зелёная полоса на сером."""
    raw = b""
    for y in range(height):
        raw += b"\x00" + b"".join(bytes((40, 160, 90) if 30 <= y < 60 else (200, 200, 200)) for _ in range(width))
    def chunk(t: bytes, d: bytes) -> bytes:
        return struct.pack(">I", len(d)) + t + d + struct.pack(">I", zlib.crc32(t + d) & 0xFFFFFFFF)
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(raw)) + chunk(b"IEND", b""))


def pdf(text: str) -> bytes:
    """Одностраничный PDF с одной строкой текста."""
    stream = f"BT /F1 18 Tf 40 760 Td ({text}) Tj ET".encode()
    objs = [b"<< /Type /Catalog /Pages 2 0 R >>", b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
            b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 595 842] /Contents 4 0 R /Resources << /Font << /F1 5 0 R >> >> >>",
            b"<< /Length %d >>\nstream\n" % len(stream) + stream + b"\nendstream",
            b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>"]
    out, offsets = b"%PDF-1.4\n", []
    for i, o in enumerate(objs, 1):
        offsets.append(len(out))
        out += b"%d 0 obj\n" % i + o + b"\nendobj\n"
    xref = len(out)
    out += b"xref\n0 %d\n0000000000 65535 f \n" % (len(objs) + 1) + b"".join(b"%010d 00000 n \n" % o for o in offsets)
    return out + b"trailer\n<< /Size %d /Root 1 0 R >>\nstartxref\n%d\n%%%%EOF\n" % (len(objs) + 1, xref)


rows = kit.params.orders
out = kit.output_dir
(out / "seen.json").write_text(json.dumps(rows, ensure_ascii=False), encoding="utf-8")
types = {k: type(v).__name__ for k, v in rows[0].items()}
kit.log.info(f"типы значений первой строки: {types}")

done = []
for r in rows:
    if r["order"] == kit.params.fail_order:
        kit.item(r["order"], "failed", "заказ не найден", row=r["_row"])
    else:
        kit.item(r["order"], "ok", f"{r['qty']} шт.", row=r["_row"])
        done.append(r)

with open(out / "orders.csv", "w", encoding="utf-8-sig", newline="") as f:
    w = csv.writer(f, delimiter=";")
    w.writerow(["order", "qty", "price", "paid", "day"])
    for r in done:
        w.writerow([r.get("order"), r.get("qty"), r.get("price"), r.get("paid"), r.get("day")])
(out / "chart.png").write_bytes(png())
(out / "report.pdf").write_bytes(pdf(f"Orders done: {len(done)} of {len(rows)}"))
kit.artifact(out / "orders.csv", "Сделанные заказы (CSV)", "text/csv")
kit.artifact(out / "chart.png", "Картинка: полоса", "image/png")
kit.artifact(out / "report.pdf", "Отчёт PDF", "application/pdf")
status = "ok" if len(done) == len(rows) else "partial"
kit.result(status, f"обработано {len(done)} из {len(rows)} заказов", count=len(done), main="orders.csv",
           links=[{"title": "Учебный сайт", "url": "https://the-internet.herokuapp.com/"}])
