#!/usr/bin/env python3
"""Base de conocimiento (RAG) sobre la FUENTE DE VERDAD — backend SQLite FTS5.

Uso:
  python3 kb.py index             # (re)indexa todo 00_FUENTE-DE-VERDAD → .kb_index.db (FTS5)
  python3 kb.py ask "pregunta"    # recupera los pasajes más relevantes, con CITA (fichero › sección)
  python3 kb.py ask "..." -k 8    # nº de pasajes (def. 6)

El 'ask' RECUPERA y cita; la SÍNTESIS la hace el agente Orquestador (que es quien razona).
Idempotente: 'index' regenera el índice desde cero (escritura atómica). Acentos/mayúsculas
normalizados (ES) vía el tokenizador unicode61 de FTS5.

POR QUÉ FTS5: antes el índice era un .kb_index.json que se cargaba ENTERO (json.load) en cada
consulta CLI en frío (~2,6 s / ~3 GB RAM sobre el corpus real). FTS5 es un índice invertido
on-disk que lee páginas bajo demanda: misma recuperación BM25, ~0,05 s / ~20 MB por consulta.

MURO — el gate de sensibilidad NO se fía de la columna guardada: para cada pasaje candidato se
RECOMPUTA `_sensitivity(path, body)` sobre el CONTENIDO devuelto (defensa en profundidad; una
fila mal etiquetada queda bloqueada igual). El pre-filtro SQL por scope es solo EFICIENCIA.

COMPAT: `retrieve()` acepta un `db_path`; si termina en `.json` usa el lector BM25 legado (mismo
scoring K1=1.5). Lo usan llamadas programáticas con index_path propio (contexto_caso) y los tests.

HÍBRIDO (BM25 + vectorial, capa opcional): además del FTS5 de siempre, `index` calcula
embeddings locales (tools/kb_embed.py, ONNX egress-0). Base, vectores y manifiesto salen
JUNTOS como una generación (id + sha256 por fragmento) y se publican con un solo puntero
atómico (`.kb_index.db.current`). `retrieve()` abre solo esa generación: fila i del .npy
sigue siendo el rowid i+1, pero si el puntero, el manifiesto o el hash no cuadran, el brazo
vectorial no corre y la consulta cae a FTS5. La fusión es RRF ANTES del gate de sensibilidad
(recomputa _sensitivity sobre el CONTENIDO, nunca se relaja). Sin modelo o sin generación
consistente → FTS5 puro (cero red).
"""
import sys, os, re, json, math, unicodedata, glob, sqlite3, logging, hashlib, secrets, shutil
from collections import defaultdict

ROOT = os.environ.get("BTP_REPO") or os.path.expanduser("~/claudecode")  # casa base SIEMPRE: la fuente de verdad y el índice viven ahí (gitignored), nunca en un worktree
FV  = os.path.join(ROOT, "00_FUENTE-DE-VERDAD")
DB  = os.path.join(FV, ".kb_index.db")     # índice FTS5 primario
IDX = os.path.join(FV, ".kb_index.json")   # formato legado (solo lo leen callers con index_path=.json / tests)
VEC = os.path.join(FV, ".kb_index.vectors.npy")  # embeddings de la capa híbrida (opcional; junto al FTS5)
K1, B, CHUNK = 1.5, 0.75, 1200             # K1/B: solo para el lector JSON de compat (FTS5 usa k1=1.2 fijo)
RRF_K = 60                                 # constante estándar de reciprocal rank fusion (1/(k+rank))

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import kb_embed  # noqa: E402 — capa vectorial opcional; kb_embed.disponible() gobierna el fallback
from _lexico_publico import (RE_HGVS as _RE_HGVS, RE_GEN_PEGADO as _RE_GENP,  # noqa: E402
                             RE_GEN as _RE_GEN, RE_CLIN as _RE_CLIN,
                             RE_TEL_SEP as _RE_TELS, RE_TEL_PLANO as _RE_TELP,
                             RE_TEL_CTX as _RE_TELC, _norm as _lexnorm)

def norm(s):
    s = unicodedata.normalize("NFKD", s)
    return "".join(c for c in s if not unicodedata.combining(c)).lower()

WORD = re.compile(r"[a-z0-9]+")
def toks(s): return WORD.findall(norm(s))

# H3 (Fase 0): sensibilidad por ruta. Una caja PÚBLICA solo puede leer lo público;
# así el RAG no sirve la historia clínica/PII (que vive en _PRIVADO_*) a contenido
# de cara al mundo. Convención: _PUBLICO en la ruta = publicable (hoy no hay → scope
# public devuelve nada = seguro). Los 4 dirs _PRIVADO_ (whatsapp/x/nucleo/clinico) = private.
def _sensitivity(rel, text=""):
    low = rel.lower()
    if "_privado_" in low:
        return "private"
    # PII clínica FUERTE en el contenido → private viva donde viva (mucho clínico NO está
    # bajo _PRIVADO_: informes en "00 · Bandeja de entrada", "01 · Tratamiento", "Mails").
    # Señales fuertes (no el tema "vacuna/tumor" suelto, que sale en docs de proyecto):
    # HGVS (p.Val600Glu), gen+variante pegada ({{GEN}}{{VARIANTE}}), gen nombrado JUNTO a lenguaje
    # de informe clínico (mutación/biopsia/VAF…), o un teléfono.
    if text:
        if _RE_HGVS.search(text) or _RE_GENP.search(text):
            return "private"
        if _RE_GEN.search(_lexnorm(text)) and _RE_CLIN.search(text):
            return "private"
        if _RE_TELS.search(text) or (_RE_TELP.search(text) and _RE_TELC.search(text)):
            return "private"
    if "_publico" in low:
        return "public"
    return "internal"

def _allowed(sens, scope):
    if scope in ("all", "clinico", "private"):   # acceso pleno (agentes clínicos)
        return True
    if scope == "public":                        # cajas públicas: SOLO lo público
        return sens == "public"
    return sens != "private"                      # internal (def. de cajas): todo menos lo privado

# PDFs corruptos/malformados (offsets de xref rotos, objetos con puntero incorrecto…) son comunes en
# la fuente de verdad (escaneos, exports viejos) y pypdf ya los salta solo — NO son fatales, sigue
# extrayendo lo que puede. Pero pypdf reporta cada aviso vía el `logging` estándar (logger
# "pypdf._reader", nivel WARNING) y, sin handler propio, el "lastResort" de logging vuelca cada línea
# a stderr → con PDFs muy rotos eso son CIENTOS de líneas por fichero (el .err llegó a ~436KB). Este
# handler los CAPTURA (nunca los deja llegar a stderr) y los CUENTA por fichero, para cerrar build()
# con UNA línea resumen. (Traído del kb.py de Polaris al fusionar con el backend FTS5.)
_pdf_actual = [None]            # ruta relativa que read_text está parseando ahora mismo
_pdf_avisos = defaultdict(int)  # ruta relativa -> nº de avisos de pypdf capturados


class _ContadorAvisosPdf(logging.Handler):
    """Maneja los WARNING de pypdf sin propagarlos a stderr; solo suma al contador del fichero
    actual. Fail-soft: si algo falla en el handler, no debe romper el índice."""
    def emit(self, record):
        try:
            _pdf_avisos[_pdf_actual[0] or "?"] += 1
        except Exception:
            pass


def _instalar_contador_pdf():
    """Instala el handler silencioso en el logger de pypdf (idempotente). El silencio lo da
    propagate=False (corta el paso al root logger, que tiene el 'lastResort' a stderr), no un nivel
    más alto: un logger que descarta el nivel nunca genera el LogRecord y el handler no vería nada."""
    logger = logging.getLogger("pypdf")
    if not any(isinstance(h, _ContadorAvisosPdf) for h in logger.handlers):
        logger.addHandler(_ContadorAvisosPdf())
    logger.setLevel(logging.WARNING)
    logger.propagate = False


def hay_pypdf():
    """¿Puede este intérprete leer PDFs? pypdf vive en el .venv, no en el python del sistema."""
    try:
        import pypdf  # noqa: F401
        return True
    except Exception:
        return False


def read_text(path, rel=None):
    if path.lower().endswith(".pdf"):
        _pdf_actual[0] = rel or path
        try:
            from pypdf import PdfReader
            _instalar_contador_pdf()
            return "\n".join((pg.extract_text() or "") for pg in PdfReader(path).pages[:80])
        except ImportError:
            # No es «este PDF está roto», es «este intérprete no sabe leer NINGÚN PDF». Con el
            # `except Exception` de antes las dos cosas se trataban igual y en silencio: correr
            # `index` con el python del sistema en vez del `.venv` dejaba fuera los 1.064 PDFs de
            # la fuente de verdad —el historial clínico entero— y el índice se sobreescribía tan
            # campante. `build()` ahora se planta antes de llegar aquí; esto es el segundo cierre.
            raise
        except Exception:
            return ""
        finally:
            _pdf_actual[0] = None
    with open(path, encoding="utf-8", errors="ignore") as f:
        return f.read()

def chunk_file(path, rel):
    lines = read_text(path, rel).splitlines()
    out, cur, curlen = [], [], 0
    title = os.path.basename(rel); heading = title
    def flush():
        nonlocal cur, curlen
        txt = "\n".join(cur).strip()
        if len(txt) > 30: out.append((heading, txt))
        cur, curlen = [], 0
    for ln in lines:
        if re.match(r"#{1,6}\s", ln):
            flush(); heading = ln.lstrip("#").strip() or title
        cur.append(ln); curlen += len(ln) + 1
        if curlen > CHUNK and not ln.strip(): flush()
    flush()
    return out

# ─── índice FTS5 (primario) ────────────────────────────────────────────────────────────────
# Solo `body` es indexable/consultable (kb histórico solo tokeniza el texto, no el título).
# path/title/sensitivity se guardan pero no entran en el MATCH. unicode61 + remove_diacritics 2
# = plegado de acentos equivalente al norm() histórico.
_SCHEMA = (
    "CREATE VIRTUAL TABLE chunks USING fts5("
    "  path UNINDEXED, title UNINDEXED, sensitivity UNINDEXED, body,"
    "  tokenize = 'unicode61 remove_diacritics 2'"
    ")"
)

def _hash_fragmento(path, title, body):
    """sha256 del fragmento que el lector vuelve a calcular. Cambia si el texto o su sitio cambian."""
    h = hashlib.sha256()
    h.update((path or "").encode("utf-8"))
    h.update(b"\0")
    h.update((title or "").encode("utf-8"))
    h.update(b"\0")
    h.update((body or "").encode("utf-8"))
    return h.hexdigest()


def _puntero(db_path):
    return db_path + ".current"


def _raiz_gen(db_path):
    return db_path + ".gen"


def _escribir_meta(con, gen_id):
    con.execute("CREATE TABLE IF NOT EXISTS kb_meta (generation TEXT NOT NULL)")
    con.execute("DELETE FROM kb_meta")
    con.execute("INSERT INTO kb_meta(generation) VALUES (?)", (gen_id,))


def _generacion_en_db(db_path):
    try:
        con = sqlite3.connect("file:%s?mode=ro" % db_path, uri=True)
        try:
            row = con.execute("SELECT generation FROM kb_meta LIMIT 1").fetchone()
        finally:
            con.close()
    except sqlite3.Error:
        return None
    if not row or not row[0]:
        return None
    return row[0]


def _leer_manifiesto(path):
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, json.JSONDecodeError, UnicodeError):
        return None
    if not isinstance(data, dict):
        return None
    gen = data.get("generation")
    chunks = data.get("chunks")
    if not isinstance(gen, str) or not gen or not isinstance(chunks, list):
        return None
    by_rowid = {}
    for c in chunks:
        if not isinstance(c, dict):
            return None
        try:
            rowid = int(c["rowid"])
        except (KeyError, TypeError, ValueError):
            return None
        sha = c.get("sha256")
        if not isinstance(sha, str) or len(sha) != 64:
            return None
        if rowid in by_rowid:
            return None
        by_rowid[rowid] = sha
    data = dict(data)
    data["_by_rowid"] = by_rowid
    return data


def _fragmento_cuadra(man, rowid, path, title, body):
    esperado = man["_by_rowid"].get(int(rowid))
    if esperado is None:
        return False
    return esperado == _hash_fragmento(path, title, body)


def _vectores_utilizables(db_path, vec_path, man_path):
    """True solo si la db, el .npy y el manifiesto son la misma generación y el mismo tamaño."""
    if not (db_path and vec_path and man_path):
        return False
    if not (os.path.isfile(db_path) and os.path.isfile(vec_path) and os.path.isfile(man_path)):
        return False
    man = _leer_manifiesto(man_path)
    if man is None or _generacion_en_db(db_path) != man["generation"]:
        return False
    if len(man["_by_rowid"]) != len(man["chunks"]):
        return False
    try:
        import numpy as np
        nvec = int(np.load(vec_path, mmap_mode="r").shape[0])
    except Exception:
        return False
    if nvec != len(man["chunks"]):
        return False
    try:
        con = sqlite3.connect("file:%s?mode=ro" % db_path, uri=True)
        try:
            n = con.execute("SELECT count(*) FROM chunks").fetchone()[0]
        finally:
            con.close()
    except sqlite3.Error:
        return False
    return n == nvec


def _resolver_indice(db_path):
    """(db, vec|None, manifest|None). Con puntero, solo esa generación. Si no cuadra, FTS sin vectores."""
    ptr = _puntero(db_path)
    if os.path.isfile(ptr):
        try:
            gen_id = open(ptr, encoding="utf-8").read().strip()
        except OSError:
            gen_id = ""
        gen_dir = os.path.join(_raiz_gen(db_path), gen_id) if gen_id else ""
        db = os.path.join(gen_dir, "index.db") if gen_dir else ""
        vec = os.path.join(gen_dir, "vectors.npy") if gen_dir else ""
        man_path = os.path.join(gen_dir, "manifest.json") if gen_dir else ""
        man = _leer_manifiesto(man_path) if man_path else None
        if (db and os.path.isfile(db) and man and man["generation"] == gen_id
                and _generacion_en_db(db) == gen_id):
            if _vectores_utilizables(db, vec, man_path):
                return db, vec, man_path
            return db, None, man_path
        return db_path, None, None
    man_path = db_path + ".manifest.json"
    vec = _vector_path_for(db_path)
    if _vectores_utilizables(db_path, vec, man_path):
        return db_path, vec, man_path
    return db_path, None, None


def _sellar_manifiesto_hermano(db_path):
    """Manifiesto + kb_meta para un .db abierto directo (tests). build() publica por puntero."""
    con = sqlite3.connect(db_path)
    rows = con.execute(
        "SELECT rowid, path, title, body FROM chunks ORDER BY rowid").fetchall()
    gen_id = secrets.token_hex(16)
    _escribir_meta(con, gen_id)
    con.commit()
    con.close()
    man = {
        "generation": gen_id,
        "chunks": [
            {"rowid": rowid, "sha256": _hash_fragmento(path, title, body)}
            for rowid, path, title, body in rows
        ],
    }
    man_path = db_path + ".manifest.json"
    tmp = man_path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(man, f)
    os.replace(tmp, man_path)
    return man_path


def _publicar_generacion(rows, tmp_db, gen_id):
    """Mueve la db ya cerrada, escribe vectores y manifiesto, y voltea el puntero al final.

    Hasta el os.replace del puntero el lector sigue en la generación anterior. El .db legado
    (kb.DB) se actualiza después, para quien lo abre por ruta; retrieve() no lo usa si hay puntero.
    """
    gen_dir = os.path.join(_raiz_gen(DB), gen_id)
    os.makedirs(gen_dir, exist_ok=True)
    db_final = os.path.join(gen_dir, "index.db")
    os.replace(tmp_db, db_final)
    vec_final = os.path.join(gen_dir, "vectors.npy")
    if not _build_vectors(rows, vec_final) and os.path.exists(vec_final):
        os.remove(vec_final)
    man = {
        "generation": gen_id,
        "chunks": [
            {"rowid": i + 1, "sha256": _hash_fragmento(rel, title, body)}
            for i, (rel, title, _sens, body) in enumerate(rows)
        ],
    }
    man_final = os.path.join(gen_dir, "manifest.json")
    man_tmp = man_final + ".tmp.%d" % os.getpid()
    with open(man_tmp, "w", encoding="utf-8") as f:
        json.dump(man, f, ensure_ascii=False)
    os.replace(man_tmp, man_final)
    ptr = _puntero(DB)
    ptr_tmp = "%s.tmp.%d" % (ptr, os.getpid())
    with open(ptr_tmp, "w", encoding="utf-8") as f:
        f.write(gen_id)
    os.replace(ptr_tmp, ptr)
    legacy_tmp = "%s.pub.%d" % (DB, os.getpid())
    shutil.copy2(db_final, legacy_tmp)
    os.replace(legacy_tmp, DB)
    if os.path.exists(VEC):
        os.remove(VEC)
    raiz = _raiz_gen(DB)
    for nombre in os.listdir(raiz):
        if nombre != gen_id:
            shutil.rmtree(os.path.join(raiz, nombre), ignore_errors=True)


def _build_vectors(rows, vec_path):
    """Calcula embeddings para `rows` (mismo orden → mismo rowid que la tabla FTS5, 1-indexed)
    y los guarda en vec_path (.npy float32, N×384). Fail-soft TOTAL: si la capa vectorial no
    está disponible (modelo/deps ausentes) o CUALQUIER cosa falla, no escribe nada y devuelve
    False — la generación se publica igual, solo-FTS5. True solo si el .npy quedó escrito."""
    if not kb_embed.disponible():
        print("ℹ️  Capa vectorial no disponible (onnxruntime/modelo ausentes) — índice solo-FTS5 (fallback normal).")
        return False
    import numpy as np
    textos = [body for (_rel, _heading, _sens, body) in rows]
    BATCH = 64
    vecs = []
    try:
        for i in range(0, len(textos), BATCH):
            batch = kb_embed.embed_texts(textos[i:i + BATCH], prefix="passage: ")
            if batch is None:
                raise RuntimeError("embed_texts devolvió None a mitad de indexado")
            vecs.append(batch)
        mat = np.concatenate(vecs, axis=0).astype("float32") if vecs else np.zeros((0, kb_embed.DIM), "float32")
        tmp = vec_path + ".tmp.npy"
        np.save(tmp, mat)
        os.replace(tmp, vec_path)   # swap atómico, igual que el .db
        print(f"✅ Vectores calculados: {mat.shape[0]} pasajes × {mat.shape[1]}-dim → "
              f"{os.path.basename(vec_path)} ({os.path.getsize(vec_path)//1024} KB)")
        return True
    except Exception as e:
        print(f"⚠️  Capa vectorial: fallo calculando embeddings ({e}) — índice queda solo-FTS5.")
        if os.path.exists(vec_path):
            os.remove(vec_path)   # no dejar un .npy viejo/desincronizado con la DB nueva
        return False


# El historial ordenado (`_PRIVADO_CLINICO/_historial/`) es la copia BUENA de cada informe:
# nombre canónico, carpeta correcta, sin repetidos. Los mismos PDFs siguen existiendo en los
# volcados de los que salieron —«Informes médicos Murcia», `_consolidado-desde-raiz-*`,
# `archivo-clinico-*`— y ahí seguirán, porque no se borra nada. Pero indexar cuatro veces el
# mismo informe reparte su señal entre cuatro rutas y hace que el RAG cite la copia con el
# nombre peor. Se indexa UNA vez, y gana la del historial.
CANONICO = os.path.join("01 · Tratamiento", "_PRIVADO_CLINICO", "_historial")


def _orden_canonico(p):
    """Primero el historial: el `dict` de hashes se queda con el PRIMERO que ve."""
    return (0 if CANONICO in p else 1, p)


def _dedup_por_contenido(files):
    """Quita los ficheros byte a byte idénticos a otro ya visto. Solo hashea cuando hay dos
    del mismo tamaño: hashear 24.000 ficheros para encontrar 400 repetidos no sale a cuenta."""
    por_tam = defaultdict(list)
    for p in files:
        try:
            por_tam[os.path.getsize(p)].append(p)
        except OSError:
            pass
    vistos, salida, repes = set(), [], 0
    for tam, grupo in por_tam.items():
        if len(grupo) == 1:
            salida.append(grupo[0])
            continue
        for p in sorted(grupo, key=_orden_canonico):
            try:
                h = hashlib.sha256(open(p, "rb").read()).hexdigest()
            except OSError:
                continue
            if h in vistos:
                repes += 1
                continue
            vistos.add(h)
            salida.append(p)
    return sorted(salida), repes


def _tomar_lock():
    """Lock exclusivo del reindexado. Devuelve el fichero (hay que conservarlo vivo) o None.

    No bloquea: si otro proceso está reindexando, el que llega se va en verde. Dos reindex
    simultáneos no aportan nada y antes se pisaban el tmp compartido (ver build()).
    """
    import fcntl
    f = open(DB + ".lock", "w")
    try:
        fcntl.flock(f.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        f.close()
        return None
    return f


def build():
    files = []
    for ext in ("*.md", "*.txt", "*.pdf"):
        files += glob.glob(os.path.join(FV, "**", ext), recursive=True)
    # FAIL-LOUD antes de tocar nada: sin pypdf este build dejaría fuera todos los PDFs y
    # reemplazaría un índice bueno por uno cojo, sin decir una palabra.
    if not hay_pypdf() and any(f.lower().endswith(".pdf") for f in files):
        print("⛔ Este intérprete no tiene pypdf: se perderían %d PDFs (el historial clínico\n"
              "   entero). NO se toca el índice. Corre:\n"
              "       %s/.venv/bin/python3 tools/kb.py index"
              % (sum(1 for f in files if f.lower().endswith(".pdf")), ROOT), file=sys.stderr)
        return 1
    files, repetidos = _dedup_por_contenido(sorted(set(files), key=_orden_canonico))
    if repetidos:
        print("· %d ficheros repetidos byte a byte, indexados una sola vez "
              "(gana la copia del historial ordenado)." % repetidos)
    # Un tmp COMPARTIDO entre procesos era una carrera: el daemon `kb-reindex` y un reindex
    # manual (o dos sesiones a la vez, que aquí es lo normal) escribían el mismo fichero y el
    # segundo moría con «table chunks already exists» o «attempt to write a readonly database»
    # —el primero ya le había hecho el `os.replace` debajo—. Sin índice nuevo, el RAG sigue
    # contestando con la foto vieja, que es lo que sostiene el sello de evidencia. Iba así
    # 56 días (deuda `daemon_fallando:com.btp.kb-reindex`, 7 detecciones, 3 remisiones).
    # Arreglo: un tmp por proceso + lock exclusivo. Si otro reindex ya corre, esto NO es un
    # fallo: se dice y se sale en verde, porque el índice lo está rehaciendo él.
    lock = _tomar_lock()
    if lock is None:
        print("kb: ya hay otro reindexado en marcha; no duplico el trabajo.", file=sys.stderr)
        return 0
    tmp = "%s.tmp.%d" % (DB, os.getpid())
    if os.path.exists(tmp):
        os.remove(tmp)
    con = sqlite3.connect(tmp)
    con.execute(_SCHEMA)
    _pdf_avisos.clear()   # cuenta fresca de avisos de PDF en cada build() (idempotente)
    rows, n = [], 0
    for p in sorted(files):
        rel = os.path.relpath(p, FV)
        if rel.startswith("."):
            continue
        try:
            for heading, text in chunk_file(p, rel):
                rows.append((rel, heading, _sensitivity(rel, text), text)); n += 1
        except Exception:
            pass
    con.executemany(
        "INSERT INTO chunks(path, title, sensitivity, body) VALUES (?,?,?,?)", rows)
    con.commit()
    # `optimize` COMPACTA, no indexa: reescribe el índice FTS5 entero (404 MB a 17-sep-2026)
    # y necesita ~2x en temporal. Cuando eso reventaba con «disk I/O error» se perdía el
    # reindex ENTERO —con los datos ya escritos y el índice ya válido— y el daemon quedaba en
    # rojo (deuda `daemon_fallando:com.btp.kb-reindex`, intermitente, 6 veces en 53 días).
    # Un índice sin compactar busca igual, solo ocupa más: el fallo se dice y no se propaga.
    try:
        con.execute("INSERT INTO chunks(chunks) VALUES('optimize')")
        con.commit()
    except sqlite3.Error as e:
        print("kb: índice escrito, sin compactar (%s: %s)" % (type(e).__name__, e),
              file=sys.stderr)
    gen_id = secrets.token_hex(16)
    _escribir_meta(con, gen_id)
    con.commit()
    con.close()
    # Db, vectores y manifiesto se escriben ANTES de voltear el puntero. El lock sigue
    # cogido: nadie publica otra generación en medio. El lector no ve la nueva hasta el
    # replace del puntero, así que no casa una db nueva con vectores de la anterior.
    _publicar_generacion(rows, tmp, gen_id)
    lock.close()
    print(f"✅ Indexados {n} pasajes de {len(files)} ficheros → .kb_index.db ({os.path.getsize(DB)//1024} KB)")
    if _pdf_avisos:
        nombres = ", ".join(os.path.basename(p) for p in sorted(_pdf_avisos))
        print(f"⚠️  {len(_pdf_avisos)} PDFs con partes ilegibles (no-fatal, pypdf saltó esos objetos/páginas "
              f"rotos y siguió con el resto del texto): {nombres}")

# FTS5 tiene sintaxis de query propia; el texto crudo del usuario puede romperla. Plegamos con
# el MISMO toks() y construimos un OR de términos citados (semántica "cualquier término suma").
def _match_expr(q):
    terms = sorted(set(toks(q)))
    return " OR ".join('"%s"' % t for t in terms)

# Pre-filtro de scope en SQL (solo EFICIENCIA: evita que miles de pasajes private acaparen el
# presupuesto de candidatos y tapen a los permitidos). NO es el muro: el muro es el reescaneo de
# contenido de abajo, que caza filas mal etiquetadas aunque pasen este WHERE.
def _scope_sql(scope):
    if scope in ("all", "clinico", "private"):
        return ""
    if scope == "public":
        return " AND sensitivity = 'public'"
    return " AND sensitivity != 'private'"

def _fts5_candidates(q, k, scope, db_path):
    """Candidatos BM25 YA filtrados por el gate de sensibilidad (recomputado sobre contenido),
    hasta `k` filas — [(rowid, path, title, body, score)], mayor score = mejor. Base compartida
    de _retrieve_fts5 (uso público, sin rowid) y del brazo BM25 de la fusión híbrida (con rowid,
    para poder casar con el ranking vectorial por identidad de fila, no por texto)."""
    if not os.path.exists(db_path):
        return []
    expr = _match_expr(q)
    if not expr:
        return []
    con = sqlite3.connect(db_path)
    limit = max(k * 20, 100)
    cur = con.execute(
        "SELECT rowid, path, title, body, bm25(chunks) FROM chunks "
        "WHERE chunks MATCH ?" + _scope_sql(scope) +
        " ORDER BY bm25(chunks) LIMIT ?", (expr, limit))
    out = []
    for rowid, path, title, body, score in cur:
        # MURO (autoritativo): recomputa la sensibilidad sobre el CONTENIDO, no la columna.
        if not _allowed(_sensitivity(path, body), scope):
            continue
        out.append((rowid, path, title, body, -score))   # bm25() es negativo; -score = mayor mejor
        if len(out) >= k:
            break
    con.close()
    return out

def _retrieve_fts5(q, k, scope, db_path):
    return [(path, title, body, score) for (_rowid, path, title, body, score)
            in _fts5_candidates(q, k, scope, db_path)]

# ─── capa vectorial (opcional) + fusión RRF ────────────────────────────────────────────────
def _vector_path_for(db_path):
    """El .npy vive JUNTO al .db (mismo directorio, nombre derivado) — igual convención que
    IDX/VEC en producción. Para un db_path custom (tests / contexto_caso con index_path propio)
    buscamos "<db_path sin extensión>.vectors.npy"; si no existe, el brazo vectorial no aporta
    nada y la fusión cae sola a BM25-solo (ver retrieve())."""
    if db_path == DB:
        return VEC
    base, _ext = os.path.splitext(db_path)
    return base + ".vectors.npy"

def _vector_candidates(q, k, scope, db_path, vec_path, man_path):
    """Candidatos por coseno YA filtrados por el gate — [(rowid, path, title, body, score)].

    vec_path/man_path vienen de _resolver_indice: o son la generación del puntero, o None.
    Si un fragmento no tiene el hash del manifiesto, se tira el brazo entero (no se sirve
    una fila de otra generación). Fail-soft: cualquier excepción → [] y retrieve() sigue en BM25."""
    if not vec_path or not man_path or not kb_embed.disponible() or not os.path.exists(db_path):
        return []
    man = _leer_manifiesto(man_path)
    if man is None:
        return []
    try:
        import numpy as np
        mat = np.load(vec_path, mmap_mode="r")
        qv = kb_embed.embed_query(q)
        if qv is None or mat.shape[0] == 0 or mat.shape[1] != qv.shape[0]:
            return []
        sims = mat @ qv   # coseno = producto punto (todo ya normalizado L2 al indexar/embeder)
        limit = min(max(k * 20, 100), sims.shape[0])
        # argpartition: los `limit` mejores sin ordenar el resto (150k filas, barato); luego
        # se ordenan solo esos `limit` — evita un sort completo de todo el corpus por consulta.
        top_idx = np.argpartition(-sims, limit - 1)[:limit]
        top_idx = top_idx[np.argsort(-sims[top_idx])]
        con = sqlite3.connect(db_path)
        out = []
        for i in top_idx:
            rowid = int(i) + 1   # fila i (0-indexed, orden de build()) ↔ rowid i+1 (FTS5)
            row = con.execute("SELECT path, title, body FROM chunks WHERE rowid=?", (rowid,)).fetchone()
            if row is None:
                con.close()
                return []
            path, title, body = row
            if not _fragmento_cuadra(man, rowid, path, title, body):
                con.close()
                return []
            if not _allowed(_sensitivity(path, body), scope):   # MURO: idéntico al brazo BM25
                continue
            out.append((rowid, path, title, body, float(sims[i])))
            if len(out) >= k:
                break
        con.close()
        return out
    except Exception:
        return []

def _rrf_fuse(list_a, list_b, k_out, rrf_k=RRF_K):
    """Reciprocal rank fusion de dos listas [(rowid, path, title, body, score)] YA filtradas
    por el gate (cada rama aplicó _allowed sobre su propio contenido — fusionar no relaja nada,
    solo reordena candidatos que cada brazo ya declaró permitidos). Identidad de fusión = rowid
    cuando existe (FTS5 vivo); si una fila solo aparece en una rama, se queda con su score RRF
    parcial (1/(rrf_k+rango) de esa rama, 0 de la otra) — no se descarta por estar en una sola
    rama, así el híbrido nunca pierde recall frente al BM25-solo de hoy."""
    rrf = {}
    meta = {}
    for lst in (list_a, list_b):
        for rank, (rowid, path, title, body, _score) in enumerate(lst, start=1):
            rrf[rowid] = rrf.get(rowid, 0.0) + 1.0 / (rrf_k + rank)
            meta.setdefault(rowid, (path, title, body))
    ranked = sorted(rrf.items(), key=lambda kv: -kv[1])[:k_out]
    return [(meta[rowid][0], meta[rowid][1], meta[rowid][2], score) for rowid, score in ranked]

def _retrieve_hybrid(q, k, scope, db_path, vec_path=None, man_path=None):
    """BM25 (siempre) + vectorial (si la generación cuadra) fusionados por RRF. Sin vectores
    consistentes colapsa al BM25 de esa misma db — nunca a un .npy de otra generación."""
    bm25 = _fts5_candidates(q, max(k * 4, 40), scope, db_path)
    vec = _vector_candidates(q, max(k * 4, 40), scope, db_path, vec_path, man_path)
    if not vec:
        return [(path, title, body, score) for (_rowid, path, title, body, score) in bm25[:k]]
    return _rrf_fuse(bm25, vec, k)

# Lector BM25 legado (formato .kb_index.json). Compat para callers con index_path=.json y tests.
def _retrieve_json(q, k, scope, idx_path):
    if not os.path.exists(idx_path):
        return []
    with open(idx_path) as f:
        idx = json.load(f)
    N, avgdl, L = idx["N"], idx["avgdl"], idx["lengths"]
    df, post, chunks = idx["df"], idx["postings"], idx["chunks"]
    scores = defaultdict(float)
    for w in set(toks(q)):
        if w not in post:
            continue
        idf = math.log((N - df[w] + 0.5) / (df[w] + 0.5) + 1)
        for ci, f in post[w]:
            d = L[ci]
            scores[ci] += idf * (f * (K1 + 1)) / (f + K1 * (1 - B + B * d / avgdl))
    ranked = sorted(scores.items(), key=lambda x: -x[1])
    top = [(ci, sc) for ci, sc in ranked
           if _allowed(chunks[ci].get("sensitivity", "internal"), scope)][:k]
    return [(chunks[ci]["path"], chunks[ci]["title"], chunks[ci]["text"], sc) for ci, sc in top]

def retrieve(q, k=6, scope="internal", db_path=None):
    """[(path, title, text, score), …] — los k pasajes permitidos más relevantes. Backend por
    extensión: db_path .json → lector legado; si no (def.) → híbrido BM25+vectorial (RRF) sobre
    .kb_index.db, con fallback automático a FTS5 puro si la capa vectorial no está disponible."""
    path = db_path or DB
    if str(path).endswith(".json"):
        return _retrieve_json(q, k, scope, path)
    db, vec, man = _resolver_indice(path)
    return _retrieve_hybrid(q, k, scope, db, vec, man)

def ask(q, k=6, scope="internal"):  # default SEGURO: 'internal' = todo menos PII/clínico (private). Lo privado exige --scope private/all explícito.
    rows = retrieve(q, k, scope)
    if not rows:
        if not os.path.exists(DB) and not os.path.isfile(_puntero(DB)):
            print("No hay índice. Corre primero: python3 kb.py index"); return
        print("(sin resultados — prueba otras palabras o amplía --scope)"); return
    print(f"🔎 {len(rows)} pasajes para: «{q}»  (scope: {scope})")
    for rank, (path, title, text, sc) in enumerate(rows, 1):
        snippet = re.sub(r"\s+", " ", text)[:480]
        print(f"\n[{rank}] {path}  ›  {title}   (rel {sc:.1f})\n    {snippet}")

if __name__ == "__main__":
    if len(sys.argv) < 2 or sys.argv[1] not in ("index", "ask"):
        print(__doc__); sys.exit(1)
    if sys.argv[1] == "index":
        build()
    else:
        args = sys.argv[2:]; k = 6; scope = "internal"
        if "-k" in args:
            i = args.index("-k"); k = int(args[i + 1]); args = args[:i] + args[i + 2:]
        if "--scope" in args:   # public | internal | private | all (def. internal = SEGURO: sin PII/clínico)
            i = args.index("--scope"); scope = args[i + 1]; args = args[:i] + args[i + 2:]
        ask(" ".join(args), k, scope)
