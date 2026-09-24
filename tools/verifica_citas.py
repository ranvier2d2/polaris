#!/usr/bin/env python3
"""Gate determinista de EXISTENCIA de citas — caza citas FABRICADAS antes de razonar.

Por qué: los buscadores LLM inventan ~1 de cada 3 citas (CJR mar-2025). Antes de que
un agente le ponga a {{TITULAR}} delante un DOI / PMID / ensayo (NCT) / arXiv, este gate
comprueba —sin LLM, con APIs públicas gratis— si la referencia EXISTE de verdad. Lo
que no existe se marca FABRICADA y se bloquea; lo que existe pasa al juicio adversarial
de siempre. Patrón copiado (no el código) de academic-research-skills, aislado del
multiagente caro. Lo usa el agente `verificacion` como PRIMER filtro barato.

Sin pip. Usa curl (consistente con tools/umami.py; evita líos de TLS/anti-bot).
Solo IDs PÚBLICOS de literatura — CERO PII, no toca el muro.

APIs (todas gratis, sin clave):
  DOI   -> Crossref      https://api.crossref.org/works/{doi}
           Un 404 de /works NO basta: DataCite y otras agencias no están en ese
           índice. Se resuelve la agencia en /works/{doi}/agency y solo se acusa
           si el identificador no existe. Si esa segunda llamada no responde,
           el estado es no_resoluble, nunca fabricada.
  PMID  -> NCBI eutils   https://eutils.ncbi.nlm.nih.gov/entrez/eutils/esummary.fcgi
  NCT   -> ClinicalTrials.gov v2   https://clinicaltrials.gov/api/v2/studies/{nct}
  arXiv -> http://export.arxiv.org/api/query?id_list={id}

Uso:
  python3 tools/verifica_citas.py "10.1186/s13073-024-01388-3" "PMID:39538331" "NCT07112053"
  python3 tools/verifica_citas.py --json "<id>" ...        # salida JSON (para agentes)
  echo "10.xxx ; PMID:123 ; NCT012..." | python3 tools/verifica_citas.py -   # desde stdin
Salida: por cada id -> existe / FABRICADA / no_resoluble  (+ título canónico si existe).
Código de salida: 2 si hay alguna FABRICADA (para que verificacion la bloquee), si no 0.
"""
import os, sys, re, json, subprocess

UA = "BeyondTheProtocol-citecheck/1.0"
TIMEOUT = "25"

# --- estados ---
EXISTE = "existe"
FABRICADA = "no_existe"        # confirmado que NO existe en el registro -> cita fabricada
NO_RES = "no_resoluble"        # red caída / API muda -> no acusar (puede existir)
# Distinto de NO_RES a propósito: aquí el problema NO es la red, es que no supimos leer un id
# de la cadena. Mezclarlos hacía que una cita ilegible se leyera como «la red falló», que el
# agente `verificacion` tiene instrucción de NO acusar → pasaba limpia. Con la etiqueta propia,
# quien la reciba sabe que hay que mirarla a mano en vez de darla por buena.
NO_PARSE = "no_parseable"      # no encontré ningún id verificable en la cadena


def _curl(url, accept="application/json"):
    """GET con curl. Devuelve (codigo_http:int|None, cuerpo:str). None si curl falla."""
    p = subprocess.run(
        ["curl", "-sS", "-L", "--max-time", TIMEOUT, "-A", UA,
         "-H", "accept: " + accept, "-w", "\n%{http_code}", url],
        capture_output=True, text=True)
    if p.returncode != 0:
        return None, (p.stderr or "")[:160]
    raw = p.stdout or ""
    nl = raw.rfind("\n")
    if nl < 0:
        return None, raw[:160]
    body, code = raw[:nl], raw[nl + 1:].strip()
    try:
        return int(code), body
    except ValueError:
        return None, body[:160]


# --- detección de tipo de id ---
def clasifica(raw):
    """Devuelve (tipo, id_limpio). tipo ∈ {doi,pmid,nct,arxiv,desconocido}."""
    s = (raw or "").strip().strip(".,;()[]<>\"' ")
    low = s.lower()
    # DOI EMBEBIDO, sin exigir que la cadena empiece por 10./doi/http.
    # Antes solo se extraía si la cita venía "pelada", así que una referencia bibliográfica
    # normal —«Pérez J, et al. Lancet Oncol. 2025;26(4):e123. doi:10.1016/…»— caía a
    # `desconocido` → `no_resoluble`, y el agente `verificacion` tiene instrucción de NO
    # acusar ante `no_resoluble` (lo trata como red caída). O sea que un DOI FABRICADO dentro
    # de una bibliografía pasaba limpio: justo el fallo nº1 de los buscadores LLM que este
    # tool existe para cazar. El patrón `10.\d{4,9}/` es muy distintivo y no colisiona con
    # dosis clínicas tipo «10.5/mg» (exige 4+ dígitos tras el punto).
    m = re.search(r'10\.\d{4,9}/\S+', s)
    if m:
        return "doi", m.group(0).rstrip(".,);]>\"'")
    # NCT
    m = re.search(r'(NCT\d{8})', s, re.I)
    if m:
        return "nct", m.group(1).upper()
    # PMID con prefijo
    m = re.match(r'pmid[:\s]*([0-9]{1,8})$', low)
    if m:
        return "pmid", m.group(1)
    # arXiv (con o sin prefijo): 2401.01234 ó arXiv:2401.01234v2
    m = re.search(r'(\d{4}\.\d{4,5})(v\d+)?', s)
    if "arxiv" in low or (m and "/" not in s and len(s) <= 16):
        if m:
            return "arxiv", m.group(1) + (m.group(2) or "")
    # DOI suelto (10.xxxx/yyy) sin url
    m = re.match(r'(10\.\d{4,9}/\S+)$', s)
    if m:
        return "doi", m.group(1)
    # dígitos pelados -> en nuestro contexto clínico casi siempre es un PMID
    if re.fullmatch(r'[0-9]{1,8}', s):
        return "pmid", s
    return "desconocido", s


# --- comprobadores por fuente ---
def _agencia_doi(doi):
    """Tras un 404 de /works: ¿otra agencia, el servicio mudo, o el DOI no existe?

    Crossref documenta que /works solo indexa sus DOIs; /works/{doi}/agency
    dice quién lo registró (datacite, medra, crossref…) o 404 si nadie.
    """
    code, body = _curl("https://api.crossref.org/works/" + doi + "/agency")
    if code == 404:
        return FABRICADA, "identificador no existe", "DOI"
    if code != 200:
        return NO_RES, "no se pudo resolver la agencia del DOI (%s)" % (
            code if code else (body or "")[:60]), "DOI"
    try:
        ag = (json.loads(body).get("message") or {}).get("agency") or {}
        aid = (ag.get("id") or "").strip()
        label = (ag.get("label") or aid).strip()
    except Exception:
        return NO_RES, "respuesta de agencia no legible", "DOI"
    if not aid:
        return NO_RES, "la agencia no vino en la respuesta", "DOI"
    # /works ocultó el registro pero la agencia es Crossref (alias, p.ej.).
    # No es «no existe»: tampoco hay título que enseñar, así que no se afirma.
    if aid.lower() == "crossref":
        return NO_RES, "Crossref conoce el DOI pero no devolvió el registro", "Crossref"
    return EXISTE, "no está en Crossref; registrado en %s" % label, label


def check_doi(doi):
    code, body = _curl("https://api.crossref.org/works/" + doi)
    if code == 200:
        try:
            t = (json.loads(body).get("message", {}).get("title") or [""])[0]
        except Exception:
            t = ""
        return EXISTE, t or "(sin título)", "Crossref"
    # 404 aquí solo dice «no está en Crossref». DataCite (y mEDRA, etc.)
    # responden exactamente así y el DOI es real.
    if code == 404:
        return _agencia_doi(doi)
    return NO_RES, "Crossref no respondió (%s)" % (code if code else body[:60]), "Crossref"


def check_pmid(pmid):
    url = ("https://eutils.ncbi.nlm.nih.gov/entrez/eutils/esummary.fcgi"
           "?db=pubmed&retmode=json&id=" + pmid)
    code, body = _curl(url)
    if code != 200:
        return NO_RES, "eutils no respondió (%s)" % (code if code else body[:60]), "PubMed"
    try:
        res = json.loads(body).get("result", {})
    except Exception:
        return NO_RES, "respuesta no-JSON de eutils", "PubMed"
    rec = res.get(pmid)
    # PMID inexistente: eutils mete una 'error' en el registro o no lo incluye en uids
    if isinstance(rec, dict) and not rec.get("error") and pmid in (res.get("uids") or [pmid]):
        return EXISTE, rec.get("title") or "(sin título)", "PubMed"
    if isinstance(rec, dict) and rec.get("error"):
        return FABRICADA, "PMID inexistente (%s)" % rec.get("error"), "PubMed"
    return FABRICADA, "PMID no encontrado en PubMed", "PubMed"


def check_nct(nct):
    code, body = _curl("https://clinicaltrials.gov/api/v2/studies/" + nct)
    if code == 200:
        try:
            t = (json.loads(body).get("protocolSection", {})
                 .get("identificationModule", {}).get("briefTitle") or "")
        except Exception:
            t = ""
        return EXISTE, t or "(sin título)", "ClinicalTrials.gov"
    if code == 404:
        return FABRICADA, "ensayo inexistente en ClinicalTrials.gov", "ClinicalTrials.gov"
    return NO_RES, "ClinicalTrials no respondió (%s)" % (code if code else body[:60]), "ClinicalTrials.gov"


def check_arxiv(aid):
    code, body = _curl("https://export.arxiv.org/api/query?id_list=" + aid, accept="application/atom+xml")
    if code != 200 or not body:
        return NO_RES, "arXiv no respondió (%s)" % (code if code else ""), "arXiv"
    # un id inexistente devuelve un <entry> con <title>Error</title>
    m = re.search(r'<entry>.*?<title>(.*?)</title>', body, re.S)
    if not m:
        return FABRICADA, "sin entrada en arXiv", "arXiv"
    title = re.sub(r'\s+', ' ', m.group(1)).strip()
    if title.lower() == "error":
        return FABRICADA, "id inexistente en arXiv", "arXiv"
    return EXISTE, title, "arXiv"


_CHECKERS = {"doi": check_doi, "pmid": check_pmid, "nct": check_nct, "arxiv": check_arxiv}


def verifica(ids):
    """Lista de strings -> lista de dicts {entrada,tipo,id,estado,detalle,fuente}.
    Importable por otras tools/agentes (verificacion la usa como primer filtro)."""
    out = []
    for raw in ids:
        raw = (raw or "").strip()
        if not raw:
            continue
        tipo, cid = clasifica(raw)
        fn = _CHECKERS.get(tipo)
        if not fn:
            out.append({"entrada": raw, "tipo": tipo, "id": cid,
                        "estado": NO_PARSE,
                        "detalle": "no encontré DOI/PMID/NCT/arXiv en la cita — "
                                   "NO es «la red falló»: verifícala a mano antes de darla por buena",
                        "fuente": "—"})
            continue
        estado, detalle, fuente = fn(cid)
        out.append({"entrada": raw, "tipo": tipo, "id": cid,
                    "estado": estado, "detalle": detalle, "fuente": fuente})
    return out


def _leer_ids(argv):
    if argv == ["-"] or (not argv and not sys.stdin.isatty()):
        data = sys.stdin.read()
        return re.split(r'[\s;,]+', data.strip())
    return argv


def main():
    argv = sys.argv[1:]
    as_json = False
    if argv and argv[0] in ("--json", "-j"):
        as_json, argv = True, argv[1:]
    ids = _leer_ids(argv)
    ids = [x for x in ids if x and x != "-"]
    if not ids:
        print(__doc__.strip().split("\n\n")[0])
        print("\nUso: python3 tools/verifica_citas.py \"10.xxx/yyy\" \"PMID:123\" \"NCT012...\"")
        return 0
    res = verifica(ids)
    if as_json:
        print(json.dumps(res, ensure_ascii=False, indent=2))
    else:
        # NO_PARSE estaba definido y se emitía (línea ~177) pero faltaba aquí: imprimir un
        # resultado no_parseable reventaba con KeyError DESPUÉS de listar todo, y un exit 1
        # en una tool del muro de evidencia se lee como "hay una cita fabricada" cuando en
        # realidad era "la herramienta se rompió". Son cosas opuestas.
        glyph = {EXISTE: "✓ existe", FABRICADA: "✗ FABRICADA", NO_RES: "? no resoluble",
                 NO_PARSE: "· sin id"}
        print("=== Verificación de existencia de citas (gate determinista, sin LLM) ===")
        for r in res:
            print("[%-14s] %-30s (%s · %s)" % (glyph.get(r["estado"], "? %s" % r["estado"]), r["id"], r["tipo"], r["fuente"]))
            print("                 ↳ %s" % r["detalle"])
        nf = sum(1 for r in res if r["estado"] == FABRICADA)
        ne = sum(1 for r in res if r["estado"] == EXISTE)
        nr = sum(1 for r in res if r["estado"] == NO_RES)
        np_ = sum(1 for r in res if r["estado"] == NO_PARSE)
        print("Resumen: %d existe · %d FABRICADA · %d no resoluble · %d sin id" % (ne, nf, nr, np_))
        if nf:
            print("⚠️  Hay %d cita(s) que NO existen → trátalas como FABRICADAS y bloquéalas." % nf)
    return 2 if any(r["estado"] == FABRICADA for r in res) else 0


if __name__ == "__main__":
    sys.exit(main())
