#!/usr/bin/env python3
"""test_kb_hibrido.py — capa vectorial (BM25 + embeddings locales, RRF) de kb.py.

Cubre las tres cosas que pide el diseño aprobado ({{TITULAR}}, 12/7/26):
  (1) retrieve() híbrido devuelve resultados con la fusión RRF cuando hay vectores;
  (2) el gate de sensibilidad (_sensitivity/_allowed) sigue bloqueando PII clínica
      EXACTAMENTE igual con el brazo vectorial activo (nunca se relaja el muro);
  (3) fallback DURO a FTS5 puro si no hay vectores disponibles (.npy ausente, o
      kb_embed.disponible()==False porque onnxruntime no está instalado).

No depende del modelo ONNX real (118 MB, no se descarga en el test): para (1)/(2) se
sintetizan vectores deterministas con un hash estable del texto (no son embeddings
semánticos de verdad, solo ejercitan el PIPELINE — carga del .npy, coseno, RRF, gate — que
es lo que hay que probar aquí; la calidad semántica real ya se validó a mano contra el
modelo intfloat/multilingual-e5-small en el subconjunto de prueba, ver reporte de la rama).
Por eso (1)/(2) se SALTAN con aviso (no fallan) si kb_embed.disponible() es False en este
intérprete — es el comportamiento correcto y consistente con el fallback: sin onnxruntime
instalado no hay brazo vectorial que probar, y test_kb_fts5.py YA cubre el gate en BM25 puro.

Correr:  python3 tests/test_kb_hibrido.py
Exit 0 = verde (incluye el caso "todo se salta menos el fallback", legítimo sin onnxruntime).
"""
import hashlib
import io
import os
import sqlite3
import sys
import tempfile
from contextlib import redirect_stdout

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "tools"))
import kb  # noqa: E402

DOCS = [
    # (path, sensitivity_ETIQUETADA, body) — mismo patrón que test_kb_fts5.py.
    ("04 · IA/proyecto.md", "internal",
     "El proyecto Beyond the Protocol busca una vacuna de peptidos contra el tumor mediante neoantigenos."),
    ("04 · IA/inmuno.md", "internal",
     "La inmunoterapia con inhibidores de checkpoint bloquea puntos de control del sistema inmune."),
    ("01 · Tratamiento/informe.md", "private",
     "Resultado de la biopsia: se detecta la mutacion p.Val600Glu en el analisis molecular."),
    ("Mails/contacto.md", "private",
     "Llamame al +34 600 123 456 para hablar del caso."),
    ("04 · IA/cocina.md", "internal",
     "La receta de paella valenciana lleva arroz bomba, pollo, conejo y azafran."),
]
CLINICAL_MARKERS = ("p.val600glu", "600 123 456")


def _fake_vector(text, dim=384):
    """Vector determinista y normalizado L2 a partir de un hash del texto — NO es un embedding
    semántico real (no hace falta serlo para probar carga/coseno/RRF/gate), pero es estable
    entre llamadas (mismo texto → mismo vector) para que el test sea reproducible."""
    import numpy as np
    h = hashlib.sha256(text.encode("utf-8")).digest()
    rng = np.random.default_rng(int.from_bytes(h[:8], "big"))
    v = rng.standard_normal(dim).astype("float32")
    return v / np.linalg.norm(v)


def _build_temp_index_with_vectors(tmp):
    """Índice FTS5 + .npy hermano, con vectores sintéticos en el MISMO orden de inserción
    (rowid i+1 ↔ fila i del .npy — igual invariante que kb.build())."""
    import numpy as np
    dbpath = os.path.join(tmp, ".kb_index.db")
    vecpath = os.path.join(tmp, ".kb_index.vectors.npy")
    con = sqlite3.connect(dbpath)
    con.execute(kb._SCHEMA)
    con.executemany(
        "INSERT INTO chunks(path, title, sensitivity, body) VALUES (?,?,?,?)",
        [(p, os.path.basename(p), s, b) for (p, s, b) in DOCS])
    con.commit()
    con.close()
    mat = np.stack([_fake_vector(body) for (_p, _s, body) in DOCS])
    np.save(vecpath, mat)
    kb._sellar_manifiesto_hermano(dbpath)
    return dbpath, vecpath


def _ask(q, scope, k=10, db_path=None):
    buf = io.StringIO()
    with redirect_stdout(buf):
        rows = kb.retrieve(q, k=k, scope=scope, db_path=db_path)
        for path, title, text, sc in rows:
            print(path, title, text)
    return buf.getvalue().lower(), rows


def main():
    fails = []
    tmp = tempfile.mkdtemp()

    # ── (3) FALLBACK DURO: sin .npy junto al .db, retrieve() debe devolver resultados vía
    #     BM25 puro (comportamiento de hoy) — esto corre SIEMPRE, con o sin onnxruntime.
    dbpath_sin_vec = os.path.join(tmp, "sinvec", ".kb_index.db")
    os.makedirs(os.path.dirname(dbpath_sin_vec), exist_ok=True)
    con = sqlite3.connect(dbpath_sin_vec)
    con.execute(kb._SCHEMA)
    con.executemany(
        "INSERT INTO chunks(path, title, sensitivity, body) VALUES (?,?,?,?)",
        [(p, os.path.basename(p), s, b) for (p, s, b) in DOCS])
    con.commit(); con.close()
    out, rows = _ask("vacuna peptidos neoantigenos", "internal", db_path=dbpath_sin_vec)
    if not rows:
        fails.append("[fallback] sin .npy, retrieve() no devolvió nada (debería caer a FTS5 puro)")
    if "proyecto.md" not in out:
        fails.append("[fallback] sin .npy, no se recuperó el pasaje esperado por BM25")

    # ── kb_embed no disponible en este intérprete → aquí acaba el test, legítimamente verde.
    if not kb.kb_embed.disponible():
        print("=" * 60)
        print("ℹ️  onnxruntime/modelo no disponibles en este intérprete — se prueba SOLO el")
        print("   fallback FTS5 puro (arriba, verde). El brazo vectorial + RRF + gate se corren")
        print("   con .venv-embed/bin/python3 (ver reporte de la rama). Esto es el fallback")
        print("   duro funcionando como se diseñó, no un test incompleto.")
        if fails:
            print(f"❌ FALLO — {len(fails)} problema(s):")
            for f in fails:
                print("   -", f)
            sys.exit(1)
        print("✅ VERDE (solo fallback FTS5 — sin capa vectorial en este intérprete)")
        sys.exit(0)

    # ── (1) Híbrido CON vectores (sintéticos, deterministas): retrieve() debe devolver
    #     resultados fusionados por RRF.
    dbpath, vecpath = _build_temp_index_with_vectors(tmp)
    kb.DB, kb.VEC = dbpath, vecpath   # kb.retrieve(db_path=None) usa kb.DB/kb.VEC por defecto
    out, rows = _ask("vacuna peptidos neoantigenos tumor", "internal")
    if not rows:
        fails.append("[hibrido] retrieve() con vectores disponibles no devolvió resultados")
    if "proyecto.md" not in out:
        fails.append("[hibrido] no se recuperó el pasaje esperado (vacuna) con RRF activo")

    # ── (2) Gate de sensibilidad INTACTO con el brazo vectorial activo: el pasaje con HGVS
    #     (rowid 3, 'informe.md') y el de teléfono (rowid 4) NUNCA deben salir en public/internal,
    #     pase lo que pase con el ranking vectorial (que no sabe de sensibilidad, solo de similitud).
    for scope in ("public", "internal"):
        for q in ("mutacion biopsia", "contacto llamame telefono", "vacuna tumor"):
            out, rows = _ask(q, scope)
            leaked = [m for m in CLINICAL_MARKERS if m in out]
            if leaked:
                fails.append(f"[gate-hibrido] fuga de PII con RRF activo, scope={scope} q='{q}': {leaked}")

    # Contraprueba: scope=all SÍ debe poder recuperar lo clínico (agente clínico).
    out, rows = _ask("mutacion biopsia", "all")
    if "p.val600glu" not in out:
        fails.append("[gate-hibrido] scope=all no recuperó el pasaje clínico esperado")

    print("=" * 60)
    if fails:
        print(f"❌ FALLO — {len(fails)} problema(s):")
        for f in fails:
            print("   -", f)
        sys.exit(1)
    print("✅ VERDE — híbrido BM25+vectorial (RRF) devuelve resultados, el gate de sensibilidad")
    print("   sigue intacto con el brazo vectorial activo, y el fallback a FTS5 puro funciona")
    print("   sin .npy disponible.")
    sys.exit(0)


if __name__ == "__main__":
    main()
