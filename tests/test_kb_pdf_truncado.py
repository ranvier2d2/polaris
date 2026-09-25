#!/usr/bin/env python3
"""test_kb_pdf_truncado.py — un PDF de más de 80 páginas no se corta en silencio.

Issue #18. `leer_pdf` sigue en PDF_MAX_PAGES, pero el índice guarda en doc_meta
cuántas páginas hay, cuántas se leyeron, cuántas salieron vacías, si hubo OCR y
si se truncó. `kb.ask` lo enseña cuando el pasaje sale de un documento cortado.
"""
import io
import os
import sqlite3
import sys
import tempfile
from contextlib import redirect_stdout

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "tools"))
import kb  # noqa: E402

_pass = 0
_fail = 0


def ok(cond, name):
    global _pass, _fail
    if cond:
        _pass += 1
    else:
        _fail += 1
        print("  ✗ %s" % name)


def escribir_pdf(path, paginas):
    """PDF mínimo de una página por cadena. '' = página sin texto. Solo ASCII."""
    def esc(s):
        return s.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")

    def page_id(i):
        return 4 + 2 * i

    def content_id(i):
        return 5 + 2 * i

    n = len(paginas)
    kids = " ".join("%d 0 R" % page_id(i) for i in range(n))
    objetos = {
        1: b"<< /Type /Catalog /Pages 2 0 R >>",
        2: ("<< /Type /Pages /Count %d /Kids [%s] >>" % (n, kids)).encode("latin-1"),
        3: b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
    }
    for i, texto in enumerate(paginas):
        stream = b""
        if texto:
            stream = ("BT /F1 12 Tf 72 700 Td (%s) Tj ET" % esc(texto)).encode("latin-1")
        objetos[page_id(i)] = (
            "<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] "
            "/Resources << /Font << /F1 3 0 R >> >> /Contents %d 0 R >>" % content_id(i)
        ).encode("latin-1")
        objetos[content_id(i)] = (
            b"<< /Length %d >>\nstream\n" % len(stream) + stream + b"\nendstream"
        )
    out = bytearray(b"%PDF-1.4\n")
    offsets = {}
    for num in sorted(objetos):
        offsets[num] = len(out)
        out += ("%d 0 obj\n" % num).encode("ascii")
        out += objetos[num]
        out += b"\nendobj\n"
    xref = len(out)
    nobj = max(objetos)
    out += ("xref\n0 %d\n" % (nobj + 1)).encode("ascii")
    out += b"0000000000 65535 f \n"
    for i in range(1, nobj + 1):
        out += ("%010d 00000 n \n" % offsets[i]).encode("ascii")
    out += ("trailer << /Size %d /Root 1 0 R >>\nstartxref\n%d\n%%%%EOF\n" % (nobj + 1, xref)).encode("ascii")
    with open(path, "wb") as f:
        f.write(out)


def main():
    if not kb.hay_pypdf():
        print("  ✗ hace falta pypdf para leer el PDF sintético")
        print("RESULTADO kb PDF truncado: 0 OK, 1 fallos")
        return 1

    tmp = tempfile.mkdtemp(prefix="kb_pdf_trunc_")
    largo = os.path.join(tmp, "informe-largo.pdf")
    corto = os.path.join(tmp, "nota-corta.pdf")
    dentro = "alfaunicoindice en la primera pagina del informe sintetico."
    fuera = "omegafueradelimite solo vive pasado el tope de extraccion."
    paginas = [dentro if i == 0 else "" if i == 1 else "pagina %d de relleno sin marca." % (i + 1)
               for i in range(85)]
    paginas[84] = fuera
    escribir_pdf(largo, paginas)
    escribir_pdf(corto, ["betacorto cabe entero en el indice sin cortar el documento."])

    orig = (kb.FV, kb.IDX, kb.DB, kb.VEC)
    kb.FV = tmp
    kb.IDX = os.path.join(tmp, ".kb_index.json")
    kb.DB = os.path.join(tmp, ".kb_index.db")
    kb.VEC = os.path.join(tmp, ".kb_index.vectors.npy")
    try:
        buf = io.StringIO()
        with redirect_stdout(buf):
            rc = kb.build()
        ok(rc is None or rc == 0, "build() indexa el FV sintético")
        con = sqlite3.connect(kb.DB)
        fila = con.execute(
            "SELECT pages_total, pages_read, pages_empty, ocr, truncated "
            "FROM doc_meta WHERE path = ?", ("informe-largo.pdf",)).fetchone()
        con.close()
        ok(fila is not None, "doc_meta tiene registro del PDF largo")
        if fila:
            total, leidas, vacias, ocr, truncado = fila
            ok(total == 85, "el registro dice 85 páginas en total (vio %s)" % (total,))
            ok(leidas == kb.PDF_MAX_PAGES, "el registro dice que se leyeron %d (vio %s)" % (kb.PDF_MAX_PAGES, leidas))
            ok(vacias >= 1, "el registro cuenta la página vacía (vio %s)" % (vacias,))
            ok(ocr == 0, "el registro dice que no hubo OCR (kb.py no OCRea)")
            ok(truncado == 1, "el registro marca el truncamiento")
        ok("truncados" in buf.getvalue(), "build() avisa en stdout que hubo un PDF cortado")

        cuerpo = " ".join(t for _p, _h, t, _s in kb.retrieve("alfaunicoindice", k=3, scope="all"))
        ok("alfaunicoindice" in cuerpo, "lo que cabe en las 80 páginas sí se indexa")
        ok("omegafueradelimite" not in cuerpo, "la página 85 no entra en el pasaje recuperado")
        perdido = kb.retrieve("omegafueradelimite", k=3, scope="all")
        ok(not perdido, "una palabra que solo está pasadas las 80 páginas no se recupera")

        out = io.StringIO()
        with redirect_stdout(out):
            kb.ask("alfaunicoindice", k=3, scope="all")
        texto = out.getvalue()
        ok("truncado" in texto and "80/85" in texto, "ask muestra el truncamiento junto al pasaje")
        ok("sin OCR" in texto, "ask dice que ese pasaje salió sin OCR")

        out2 = io.StringIO()
        with redirect_stdout(out2):
            kb.ask("betacorto", k=3, scope="all")
        ok("truncado" not in out2.getvalue(), "un PDF corto no se anuncia como truncado")
        con = sqlite3.connect(kb.DB)
        corto_row = con.execute(
            "SELECT pages_read, truncated FROM doc_meta WHERE path = ?",
            ("nota-corta.pdf",)).fetchone()
        con.close()
        ok(corto_row == (1, 0), "el PDF corto queda leído entero y sin marca de corte")
    finally:
        kb.FV, kb.IDX, kb.DB, kb.VEC = orig

    print("RESULTADO kb PDF truncado: %d OK, %d fallos" % (_pass, _fail))
    print("✅ KB PDF TRUNCADO EN VERDE" if _fail == 0 else "❌ revisar fallos")
    return _fail


if __name__ == "__main__":
    sys.exit(main())
