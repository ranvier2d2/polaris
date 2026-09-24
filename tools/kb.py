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
embeddings locales (tools/kb_embed.py, ONNX egress-0) y los guarda en `.kb_index.vectors.npy`
(float32, mmap, fila i ↔ rowid i+1 de la tabla FTS5 — mismo orden de inserción). `retrieve()`
fusiona el ranking BM25 con el ranking por coseno vía RRF (reciprocal rank fusion) ANTES del
gate de sensibilidad, que se aplica exactamente igual que hoy (recomputa _sensitivity sobre el
CONTENIDO, nunca se relaja). Si el modelo/vectores no están disponibles (kb_embed.disponible()
False, o el .npy no existe) → FALLBACK DURO a FTS5 puro (comportamiento de hoy, cero red).
"""
import sys, os, re, json, math, unicodedata, glob, sqlite3, logging, hashlib
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


# Tope de extracción. Subirlo es opcional; callarlo no. Un informe más largo se indexa
# a medias y el registro (doc_meta) lo dice: páginas leídas, vacías, si hubo OCR y si se cortó.
PDF_MAX_PAGES = 80


def leer_pdf(path):
    """Texto de las primeras PDF_MAX_PAGES páginas y el registro de cobertura.

    kb.py no OCRea: un escaneado sin capa de texto sale como páginas vacías y ocr=False.
    El sidecar lo hace ocr_informes.py; aquí no se inventa un OCR que no ocurrió.
    """
    from pypdf import PdfReader
    _instalar_contador_pdf()
    pages = PdfReader(path).pages
    total = len(pages)
    leer = pages[:PDF_MAX_PAGES]
    textos, vacias = [], 0
    for pg in leer:
        t = pg.extract_text() or ""
        if not t.strip():
            vacias += 1
        textos.append(t)
    meta = {
        "pages_total": total,
        "pages_read": len(leer),
        "pages_empty": vacias,
        "ocr": False,
        "truncated": total > PDF_MAX_PAGES,
    }
    return "\n".join(textos), meta


def read_text(path, rel=None):
    if path.lower().endswith(".pdf"):
        _pdf_actual[0] = rel or path
        try:
            return leer_pdf(path)[0]
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

def _trocear(texto, rel):
    lines = texto.splitlines()
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

def chunk_file(path, rel):
    """(pasajes, meta_pdf | None). La meta solo existe en PDF: cobertura real de la extracción."""
    if path.lower().endswith(".pdf"):
        _pdf_actual[0] = rel or path
        try:
            texto, meta = leer_pdf(path)
        except ImportError:
            raise
        except Exception:
            return [], None
        finally:
            _pdf_actual[0] = None
        return _trocear(texto, rel), meta
    return _trocear(read_text(path, rel), rel), None

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

# Cobertura de cada PDF indexado. Tabla normal (no FTS): el MATCH no debe tokenizar estos números.
_META_SCHEMA = (
    "CREATE TABLE doc_meta ("
    "  path TEXT PRIMARY KEY,"
    "  pages_total INTEGER NOT NULL,"
    "  pages_read INTEGER NOT NULL,"
    "  pages_empty INTEGER NOT NULL,"
    "  ocr INTEGER NOT NULL,"
    "  truncated INTEGER NOT NULL"
    ")"
)

def _build_vectors(rows, vec_path):
    """Calcula embeddings para `rows` (mismo orden → mismo rowid que la tabla FTS5, 1-indexed)
    y los guarda en vec_path (.npy float32, N×384). Fail-soft TOTAL: si la capa vectorial no
    está disponible (modelo/deps ausentes) o CUALQUIER cosa falla, no escribe nada y deja el
    índice en FTS5-puro (retrieve() lo detecta por la ausencia del fichero, ver disponible()
    de kb_embed) — nunca rompe `index`, que es la vía de siempre."""
    if not kb_embed.disponible():
        print("ℹ️  Capa vectorial no disponible (onnxruntime/modelo ausentes) — índice solo-FTS5 (fallback normal).")
        return
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
    except Exception as e:
        print(f"⚠️  Capa vectorial: fallo calculando embeddings ({e}) — índice queda solo-FTS5.")
        if os.path.exists(vec_path):
            os.remove(vec_path)   # no dejar un .npy viejo/desincronizado con la DB nueva


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
    con.execute(_META_SCHEMA)
    _pdf_avisos.clear()   # cuenta fresca de avisos de PDF en cada build() (idempotente)
    rows, n, pdf_metas = [], 0, []
    for p in sorted(files):
        rel = os.path.relpath(p, FV)
        if rel.startswith("."):
            continue
        try:
            chunks, meta = chunk_file(p, rel)
            for heading, text in chunks:
                rows.append((rel, heading, _sensitivity(rel, text), text)); n += 1
            if meta is not None:
                pdf_metas.append((rel, meta))
        except Exception:
            pass
    con.executemany(
        "INSERT INTO chunks(path, title, sensitivity, body) VALUES (?,?,?,?)", rows)
    con.executemany(
        "INSERT INTO doc_meta(path, pages_total, pages_read, pages_empty, ocr, truncated) "
        "VALUES (?,?,?,?,?,?)",
        [(rel, m["pages_total"], m["pages_read"], m["pages_empty"],
          int(m["ocr"]), int(m["truncated"])) for rel, m in pdf_metas])
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
    con.close()
    os.replace(tmp, DB)   # swap atómico: no corrompe si alguien consulta a la vez
    lock.close()          # sueltas el lock DESPUÉS del swap: hasta aquí el índice no es el nuevo
    print(f"✅ Indexados {n} pasajes de {len(files)} ficheros → .kb_index.db ({os.path.getsize(DB)//1024} KB)")
    cortados = [rel for rel, m in pdf_metas if m["truncated"]]
    if cortados:
        nombres = ", ".join(os.path.basename(p) for p in sorted(cortados))
        print(f"⚠️  {len(cortados)} PDFs truncados a {PDF_MAX_PAGES} páginas "
              f"(el resto no entra en el índice; doc_meta lo marca): {nombres}")
    if _pdf_avisos:
        nombres = ", ".join(os.path.basename(p) for p in sorted(_pdf_avisos))
        print(f"⚠️  {len(_pdf_avisos)} PDFs con partes ilegibles (no-fatal, pypdf saltó esos objetos/páginas "
              f"rotos y siguió con el resto del texto): {nombres}")
    _build_vectors(rows, VEC)   # capa híbrida opcional — mismo orden de `rows` = mismo rowid FTS5

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

def _vector_candidates(q, k, scope, db_path):
    """Candidatos por coseno YA filtrados por el gate — [(rowid, path, title, body, score)],
    mayor score = mejor. [] si la capa vectorial no está disponible, el .npy no existe, o
    db_path no es el índice FTS5 primario (el .npy solo tiene sentido junto a su .db hermano).
    Fail-soft: cualquier excepción (npy corrupto, mismatch de tamaño…) → [] y retrieve() sigue
    con BM25 solo — nunca rompe una consulta."""
    if not kb_embed.disponible() or not os.path.exists(db_path):
        return []
    vec_path = _vector_path_for(db_path)
    if not os.path.exists(vec_path):
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
                continue
            path, title, body = row
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

def _retrieve_hybrid(q, k, scope, db_path):
    """BM25 (siempre) + vectorial (si disponible) fusionados por RRF. Si la capa vectorial no
    aporta candidatos (modelo ausente, .npy ausente, o falla) esto colapsa exactamente a
    _retrieve_fts5 reordenado por RRF-de-una-sola-rama, que preserva el MISMO orden relativo
    que el BM25 puro (rrf monótono en el rango) — no hay pérdida de comportamiento hoy."""
    bm25 = _fts5_candidates(q, max(k * 4, 40), scope, db_path)
    vec = _vector_candidates(q, max(k * 4, 40), scope, db_path)
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
    return _retrieve_hybrid(q, k, scope, path)

def _metas_pdf(paths, db_path=None):
    """path → cobertura guardada en doc_meta. Índice viejo o sin la tabla → {} (no se inventa)."""
    db_path = db_path or DB
    if not paths or not os.path.exists(db_path):
        return {}
    try:
        con = sqlite3.connect(db_path)
        if not con.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='doc_meta'").fetchone():
            con.close()
            return {}
        marcas = ",".join("?" * len(paths))
        cur = con.execute(
            "SELECT path, pages_total, pages_read, pages_empty, ocr, truncated "
            "FROM doc_meta WHERE path IN (%s)" % marcas, list(paths))
        out = {}
        for path, total, leidas, vacias, ocr, truncado in cur:
            out[path] = {
                "pages_total": total, "pages_read": leidas, "pages_empty": vacias,
                "ocr": bool(ocr), "truncated": bool(truncado),
            }
        con.close()
        return out
    except sqlite3.Error:
        return {}


def _aviso_truncado(meta):
    if not meta or not meta.get("truncated"):
        return ""
    ocr = "con OCR" if meta.get("ocr") else "sin OCR"
    vacias = ""
    if meta.get("pages_empty"):
        vacias = ", %d vacías" % meta["pages_empty"]
    return "   ⚠ truncado: leídas %d/%d páginas%s, %s" % (
        meta["pages_read"], meta["pages_total"], vacias, ocr)


def ask(q, k=6, scope="internal"):  # default SEGURO: 'internal' = todo menos PII/clínico (private). Lo privado exige --scope private/all explícito.
    rows = retrieve(q, k, scope)
    if not rows:
        if not os.path.exists(DB):
            print("No hay índice. Corre primero: python3 kb.py index"); return
        print("(sin resultados — prueba otras palabras o amplía --scope)"); return
    metas = _metas_pdf([path for path, _title, _text, _sc in rows])
    print(f"🔎 {len(rows)} pasajes para: «{q}»  (scope: {scope})")
    for rank, (path, title, text, sc) in enumerate(rows, 1):
        snippet = re.sub(r"\s+", " ", text)[:480]
        aviso = _aviso_truncado(metas.get(path))
        print(f"\n[{rank}] {path}  ›  {title}   (rel {sc:.1f}){aviso}\n    {snippet}")

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
