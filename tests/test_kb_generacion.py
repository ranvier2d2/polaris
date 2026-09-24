#!/usr/bin/env python3
"""test_kb_generacion.py — el lector no mezcla una db nueva con vectores viejos.

La ventana (auditoría 22-sep-2026, punto 4.5): se publicaba el .db y se soltaba el
lock antes de escribir el .npy. Quien consultaba en medio asociaba la fila i del
vector viejo con el rowid i+1 de la db nueva.

Ahora db + vectores + manifiesto salen como una generación y se publican con un
puntero. Este test deja montada la ventana (db nueva en la ruta legada, .npy viejo
al lado) y comprueba que retrieve() sigue la generación del puntero.
"""
import os
import sqlite3
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "tools"))
import kb  # noqa: E402

TOKEN_A = "tokenunicaalfagen"
TOKEN_B = "tokenunicabetagen"
CONSULTA = "consultavectorialsinsolape"


def _db(path, body, gen_id=None):
    if os.path.exists(path):
        os.remove(path)
    con = sqlite3.connect(path)
    con.execute(kb._SCHEMA)
    con.execute(
        "INSERT INTO chunks(path, title, sensitivity, body) VALUES (?,?,?,?)",
        ("notas/a.md", "A", "internal", body))
    if gen_id:
        kb._escribir_meta(con, gen_id)
    con.commit()
    con.close()


def main():
    import numpy as np
    fails = []
    tmp = tempfile.mkdtemp(prefix="kbgen-")
    orig = (kb.DB, kb.VEC, kb.FV)
    kb.DB = os.path.join(tmp, ".kb_index.db")
    kb.VEC = os.path.join(tmp, ".kb_index.vectors.npy")
    kb.FV = tmp
    qv = np.zeros(8, dtype="float32")
    qv[0] = 1.0

    def _build_falso(rows, vec_path):
        np.save(vec_path, np.stack([qv]))
        return True

    real_build = kb._build_vectors
    real_disp = kb.kb_embed.disponible
    real_eq = kb.kb_embed.embed_query
    try:
        kb._build_vectors = _build_falso
        kb.kb_embed.disponible = lambda: True
        kb.kb_embed.embed_query = lambda q: qv

        gen_id = "gen-a-fija"
        tmp_db = "%s.tmp.%d" % (kb.DB, os.getpid())
        _db(tmp_db, "cuerpo %s sin las palabras de la consulta." % TOKEN_A, gen_id)
        rows = [("notas/a.md", "A", "internal", "cuerpo %s sin las palabras de la consulta." % TOKEN_A)]
        kb._publicar_generacion(rows, tmp_db, gen_id)

        man_path = os.path.join(kb._raiz_gen(kb.DB), gen_id, "manifest.json")
        man = kb._leer_manifiesto(man_path)
        if man is None or man.get("generation") != gen_id:
            fails.append("el manifiesto no lleva el identificador de generación")
        hashes = [c.get("sha256") for c in (man or {}).get("chunks", [])]
        if len(hashes) != 1 or not hashes[0] or len(hashes[0]) != 64:
            fails.append("el manifiesto no lleva el hash por fragmento")
        esperado = kb._hash_fragmento(rows[0][0], rows[0][1], rows[0][3])
        if hashes and hashes[0] != esperado:
            fails.append("el hash del fragmento no cuadra con path/título/cuerpo")

        # Ventana: la ruta legada ya es la db nueva; el .npy suelto sigue siendo el viejo.
        _db(kb.DB, "cuerpo %s de la otra generacion." % TOKEN_B, "gen-b-nueva")
        np.save(kb.VEC, np.stack([qv]))

        _texto, rows_out = _preguntar()
        cuerpos = " ".join(b for (_p, _t, b, _s) in rows_out)
        if TOKEN_B in cuerpos:
            fails.append("se sirvió el fragmento de la otra generación vía los vectores viejos")
        if TOKEN_A not in cuerpos:
            fails.append("no se sirvió el fragmento de la generación publicada (puntero)")

        # Misma ventana dentro de la generación: db sustituida, vectores y manifiesto viejos.
        gen_db = os.path.join(kb._raiz_gen(kb.DB), gen_id, "index.db")
        _db(gen_db, "cuerpo %s de la otra generacion." % TOKEN_B, gen_id)
        _texto, rows_out = _preguntar()
        cuerpos = " ".join(b for (_p, _t, b, _s) in rows_out)
        if TOKEN_B in cuerpos:
            fails.append("con la db de la generación cambiada, se sirvió el texto nuevo con los vectores viejos")
    finally:
        kb._build_vectors = real_build
        kb.kb_embed.disponible = real_disp
        kb.kb_embed.embed_query = real_eq
        kb.DB, kb.VEC, kb.FV = orig

    print("=" * 60)
    if fails:
        print("❌ FALLO — %d problema(s):" % len(fails))
        for f in fails:
            print("   -", f)
        sys.exit(1)
    print("✅ VERDE — la ventana no sirve vectores de otra generación;")
    print("   el manifiesto lleva generación y hash por fragmento.")
    sys.exit(0)


def _preguntar():
    return "", kb.retrieve(CONSULTA, k=5, scope="internal")


if __name__ == "__main__":
    main()
