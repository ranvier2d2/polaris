#!/usr/bin/env python3
"""test_borde.py — evals del BORDE no-bypassable (F0-mínima del motor, plan typed-swinging-wand).

Verifica los invariantes ROCA, en estado AISLADO (BTP_STATE_DIR temporal, HALT y canarios por
env) para no tocar el sistema vivo:
  1. FAIL-CLOSED: clínico/genómico/PII/término vetado → DENY a destino no confiable; ALLOW a
     trusted (local). Forzar untrusted con dato sensible → rechaza.
  2. VÁLVULA de declassificación: deny-by-default + tipo + presupuesto + "el valor sigue limpio".
  3. AIR-GAP de embeddings: API externa → DENY; modelo local a destino local → ALLOW.
  4. TRAZA hash-chained: íntegra tras operar; un borrado/alteración la rompe (detectado).
  5. REVOCACIÓN + anti-replay: sesión revocada → DENY.
  6. CANARIOS: canario en la salida → DENY + alarma.
  7. RESILIENCIA (run_agent.sh): límite de capacidad → degrada Opus→Sonnet→Haiku; cadena
     agotada → aplaza (no muere).
Estilo igual que test_muro_fase0: cada caso suma OK/fallo; exit = nº de fallos.
"""
import os as _os, sys as _sys
_sys.path.insert(0, _os.path.dirname(_os.path.abspath(__file__)))
from _entorno import exige as _exige
_exige("nombres", "identidad")
import json
import os
import subprocess
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# Estado AISLADO + canario + HALT inexistentes ANTES de importar borde (lee STATE al importar).
_TMP = tempfile.mkdtemp(prefix="borde_test_")
os.environ["BTP_STATE_DIR"] = _TMP
os.environ["BTP_HALT_FILES"] = os.path.join(_TMP, "no_halt_a") + ":" + os.path.join(_TMP, "no_halt_b")
os.environ["BTP_CANARIOS"] = "CANARIO-XYZZY-7788"
os.environ["BTP_BORDE_PRESUPUESTO"] = "3"
sys.path.insert(0, os.path.join(ROOT, "tools"))
import borde  # noqa: E402

# Sin overlay (CI público, un clon) `clasificar_consulta` vuelve entera al borde estricto y los
# casos de «vacuna PASA» fallarían por el entorno, no por el código: se simula un overlay con un
# lugar ficticio. El fail-closed sin overlay se prueba aparte, explícitamente.
_LUGARES_OVERLAY_REAL = borde.seg._LUGARES_RUTA
if not _LUGARES_OVERLAY_REAL:
    borde.seg._LUGARES_RUTA = frozenset({"villaficticia"})

_pass = 0
_fail = 0


def ok(cond, name):
    global _pass, _fail
    if cond:
        _pass += 1
    else:
        _fail += 1
        print("  ✗ %s" % name)


def _test_cadena_truncada():
    """N eventos, se borra el último. El prefijo sigue encadenado; el head no.
    Tiene que fallar y decir truncada, no darlo por íntegro."""
    import glob
    aislado = os.path.join(_TMP, "borde_trunc")
    os.makedirs(aislado)
    saved = (borde.BORDE_DIR, borde.HEAD_FILE, borde.LOCK_FILE)
    borde.BORDE_DIR = aislado
    borde.HEAD_FILE = os.path.join(aislado, "head.txt")
    borde.LOCK_FILE = os.path.join(aislado, ".lock")
    try:
        n = 3
        for i in range(n):
            ok(borde._sellar({"evento": "test", "i": i}) is not None, "sello de prueba %d" % i)
        ok_antes, det_antes = borde.verificar_cadena()
        ok(ok_antes, "cadena de %d íntegra antes de truncar (%s)" % (n, det_antes))
        ledgers = sorted(glob.glob(os.path.join(aislado, "ledger-*.jsonl")))
        lines = open(ledgers[-1], encoding="utf-8").read().splitlines()
        ok(len(lines) == n, "ledger de prueba tiene %d eventos" % n)
        open(ledgers[-1], "w", encoding="utf-8").write("\n".join(lines[:-1]) + "\n")
        ok_t, det_t = borde.verificar_cadena()
        ok(not ok_t and "truncada" in det_t and "rota" not in det_t,
           "borrar el último evento es cadena truncada (%s)" % det_t)
        rec = json.loads(lines[0])
        rec["i"] = 999
        lines[0] = json.dumps(rec, ensure_ascii=False, sort_keys=True)
        open(ledgers[-1], "w", encoding="utf-8").write("\n".join(lines) + "\n")
        ok_r, det_r = borde.verificar_cadena()
        ok(not ok_r and "rota" in det_r and "truncada" not in det_r,
           "alterar un evento es cadena rota, no truncada (%s)" % det_r)
    finally:
        borde.BORDE_DIR, borde.HEAD_FILE, borde.LOCK_FILE = saved


def main():
    # ── 1. FAIL-CLOSED por sensibilidad ──────────────────────────────────────────────────
    sensibles = [
        ("clínico marcador", "El valor de KI-67 fue muy alto"),
        ("genómico variante", "se observó la variante p.Arg175His"),
        ("genómico rsID", "el SNP rs12345678 aparece"),
        ("genómico HLA", "tipo HLA-A*02:01"),
        ("genotipo VCF", "el genotipo es 0/1 en esa posición"),
        ("término vetado", "el plan de la vacuna personalizada"),
        ("PII {{TITULAR}}", "el caso de {{TITULAR}} {{APELLIDO}}"),
        ("PII nombre deny", "hablé con {{CONTACTO}} ayer"),
        ("PII email", "escríbeme a alguien@ejemplo.com"),
        ("PII teléfono", "mi número es +34 600 123 456"),
        # 2-sep-2026: faltaba, y era una fuga CON sello de aprobación. `deid.py` se
        # autoverifica contra clasificar(), así que un NHC salía intacto Y marcado LIMPIO.
        ("PII NHC", "paciente con NHC 999001 en seguimiento"),
        ("PII NHC con dos puntos", "NHC: 999001"),
        ("PII num. historia", "el número de historia 999001 consta en el archivo"),
        ("PII historia clínica", "historia clinica 999001"),
        # 2-sep-2026: el SEGUNDO apellido faltaba. Fuera de España se toma el último apellido
        # como EL apellido, así que «patient Perez» en un informe de Zúrich o de Boston pasaba
        # como limpio.
        ("PII apellido suelto", "la paciente Perez acude a consulta"),
        ("PII apellido con tilde", "Sra. Pérez"),
        ("PII apellido en informe extranjero", "patient Perez, follow-up visit"),
    ]
    for name, txt in sensibles:
        ok(not borde.egress_check(txt, destino="nvidia").permitido, "DENY untrusted: %s" % name)
        # el MISMO dato sensible a destino TRUSTED (local) sí pasa
        ok(borde.egress_check(txt, destino="local:consejero-arneses").permitido, "ALLOW trusted: %s" % name)

    # El otro lado del NHC: un número suelto NO puede marcarse sensible. Sin la etiqueta
    # delante, cualquier recuento o referencia llenaría el juez de falsos positivos y acabaría
    # ignorándose, que es como se muere un guardia.
    no_sensibles_nhc = [
        ("número suelto", "el resultado fue 999001"),
        ("recuento", "el paciente tiene 999001 leucocitos"),
        ("pedido", "pedido 12345 enviado ayer"),
        # El apellido no puede pasarse de frenada: los límites de palabra tienen que aguantar.
        ("palabra que contiene el apellido", "el gato es muy perezoso"),
        ("apellido parecido", "el paciente Perezagua no es ella"),
    ]
    for name, txt in no_sensibles_nhc:
        s_, m_ = borde.clasificar(txt)
        ok(not s_, "NO falso positivo de NHC: %s (dijo %r)" % (name, m_))

    # Regresiones de la auditoría verificacion (23/6): clases de evasión que ANTES colaban.
    evasiones = [
        ("neuroendocrino (regex muerto C2)", "carcinoma neuroendocrino de mama"),
        ("HGVS corto C3", "la mutación R175H está presente"),
        ("HGVS corto V600E", "se detectó V600E"),
        ("HLA espaciado C3", "tipo HLA A 02 01"),
        ("HLA con guion suelto", "alelo HLA-A presente"),
        ("exón", "deleción del exón 19 de un gen"),
        ("cromosoma palabra", "amplificación en cromosoma 8"),
        ("citobanda con contexto", "deleción 17p observada"),
        ("homoglifo término vetado M2", "vacüna personalizada"),
        ("espaciado término vetado M2", "el plan de la v a c u n a"),
        ("guion término vetado M2", "trabajo de neo-antígeno"),
    ]
    for name, txt in evasiones:
        ok(not borde.egress_check(txt, destino="nvidia").permitido, "DENY evasión: %s" % name)

    limpios = [
        ("genérico", "¿cuál es el mejor patrón para reintentos con backoff?"),
        ("operativo", "resume estos tres párrafos en una frase"),
        ("no sobre-bloquea Q4", "el informe del Q4 va con 3 secciones"),
    ]
    for name, txt in limpios:
        ok(borde.egress_check(txt, destino="nvidia").permitido, "ALLOW limpio untrusted: %s" % name)

    # vacío → DENY (nada que enviar)
    ok(not borde.egress_check("", destino="nvidia").permitido, "DENY vacío")
    # confianza fail-closed: destino raro no es trusted
    ok(not borde.es_trusted("openrouter"), "openrouter NO trusted")
    ok(not borde.es_trusted("gemini"), "gemini NO trusted por defecto")
    ok(borde.es_trusted("local:kb"), "local: SÍ trusted")
    # M1: BTP_BORDE_TRUSTED no puede colar un endpoint de nube; sí admite local:/cleared:
    os.environ["BTP_BORDE_TRUSTED"] = "grok,local:vectordb"
    ok(not borde.es_trusted("grok"), "BTP_BORDE_TRUSTED NO cuela un externo (grok)")
    ok(borde.es_trusted("local:vectordb"), "BTP_BORDE_TRUSTED admite local:")
    del os.environ["BTP_BORDE_TRUSTED"]
    # m1: input no-string → fail-closed (no excepción)
    ok(not borde.egress_check(["lista"], destino="nvidia").permitido, "no-string → DENY (m1)")
    ok(not borde.egress_check({"d": 1}, destino="nvidia").permitido, "dict → DENY (m1)")

    # ── 2. Válvula de declassificación ───────────────────────────────────────────────────
    ok(borde.declasificar("veredicto", "aprobado", sesion="d1").permitido, "declass campo OK")
    ok(not borde.declasificar("campo_raro", "x", sesion="d1").permitido, "declass deny-by-default")
    ok(not borde.declasificar("veredicto", "quizá", sesion="d1").permitido, "declass enum inválido")
    ok(not borde.declasificar("score", 5, sesion="d1").permitido, "declass score fuera de rango")
    # un 'resumen' que RELAVA un secreto (sigue clínico) → DENY aunque el campo esté permitido
    ok(not borde.declasificar("resumen_publico", "el KI-67 salió alto", sesion="d1").permitido,
       "declass no relava secreto")
    # presupuesto de revelación (3): la 4ª revelación de la sesión cae
    s = "presup"
    ok(borde.declasificar("estado", "hecho", sesion=s).permitido, "presup 1")
    ok(borde.declasificar("estado", "hecho", sesion=s).permitido, "presup 2")
    ok(borde.declasificar("estado", "hecho", sesion=s).permitido, "presup 3")
    ok(not borde.declasificar("estado", "hecho", sesion=s).permitido, "presup agotado (4ª)")

    # ── 2b. Política ingeniera (radar de literatura) ────────────────────────────────────
    # ciencia genérica PASA (no es identificador), aunque lleve términos vetados en público
    ok(borde.egress_cientifico("neoantigen vaccine in HR+ breast cancer")[0], "ingeniero genérico PASA")
    ok(borde.egress_cientifico("{{DIANA}} PRRT neuroendocrine tumors")[0], "diana genérica PASA")
    # identificador de paciente DENY
    ok(not borde.egress_cientifico("vaccine trial for {{TITULAR}} {{APELLIDO}}")[0], "ingeniero + nombre {{TITULAR}} DENY")
    ok(not borde.egress_cientifico("papers on variant p.Arg175His here")[0], "ingeniero + variante DENY")
    ok(not borde.egress_cientifico("HLA-A*02:01 and rs12345678")[0], "ingeniero + HLA/rsID DENY")
    # 11-sep-26: un rango HGVS (c.68_69delAG) no casaba por el `\b` final y salía sin gen al lado.
    for v in ("papers on c.68_69delAG", "c.5266dupC founder", "c.1234+1G>A splice",
              "p.(Arg175His) frequency"):
        ok(not borde.egress_cientifico(v)[0], "ingeniero + variante HGVS DENY: %s" % v)
    ok(borde.egress_cientifico("p. ej. vacunas de neoantígenos en mama")[0],
       "«p. ej.» no es una variante: PASA")
    # Consulta del enrutador: sin el embargo de palabras, con todo lo demás igual.
    ok(not borde.clasificar("busca la vacuna de BioNTech", veto_publico=False)[0],
       "consulta sin veto público: «vacuna» PASA")
    ok(borde.clasificar("busca la vacuna de BioNTech")[0], "por defecto el veto público sigue")
    for q in ("busca {{CONTACTO}}", "busca FGFR1 amplificado", "busca a {{TITULAR}} {{APELLIDO}}"):
        ok(borde.clasificar(q, veto_publico=False)[0], "consulta sin veto público sigue DENY: %s" % q)
    # verificacion (11-sep-26): sin el embargo, la frase sobre SU caso tiene que volver al estricto.
    for q in ("busca lo último sobre la vacuna para mi caso",
              "busca papers sobre mi vacuna de neoantígenos personalizada",
              "busca en X la vacuna de neoantígenos para una paciente de Madrid, 41 años",
              "search X for news on my vaccine"):
        ok(borde.clasificar_consulta(q)[0], "consulta sobre SU caso DENY: %s" % q)
    ok(not borde.clasificar_consulta("busca en X qué se dice de la vacuna de BioNTech")[0],
       "consulta genérica con «vacuna» PASA")
    ok(borde.egress_check("busca la vacuna de BioNTech", destino="grok", consulta=True).permitido,
       "egress_check en modo consulta deja salir «vacuna»")
    # Segunda ronda de verificacion: huecos de primera persona que tienen que cerrar…
    for q in ("busca la vacuna que me van a hacer", "busca vacunas de neoantígenos para gente como yo",
              "busca en X: tengo {{DIAGNOSTICO}}, vacuna", "me han diagnosticado, busca vacuna",
              "soy paciente de {{DIAGNOSTICO}}, busca vacuna", "busca novedades de nuestra vacuna",
              "busca qué dice mi onco de la vacuna", "busca la vacuna para mi mujer",
              "busca vacuna para una mujer nacida en 1985", "busca m i c a s o y la vacuna",
              "busca la vacuna de neoantígenos de Beyond the Protocol"):
        ok(borde.clasificar_consulta(q)[0], "consulta sobre SU caso DENY: %s" % q)
    # …y búsquedas legítimas con años que NO pueden caer (el tripwire de edad era demasiado ancho).
    for q in ("busca papers de supervivencia a 5 años con vacunas de neoantígenos",
              "busca ensayos de vacunas de los últimos 10 años", "busca vacunas en mayores de 65 años",
              "busca seguimiento a 3 años de {{VACUNA2}}", "busca Beyond the Protocol en prensa",
              "busca supervivencia de pacientes tratadas a 10 años con vacuna"):
        ok(not borde.clasificar_consulta(q)[0], "búsqueda legítima PASA: %s" % q)
    # 22-sep-26: los dos huecos que `verificacion` dejó sobre b9d6c29. Los lugares reales viven en
    # el overlay gitignored; aquí se prueba con uno FICTICIO para no meter su ruta en git.
    seg = borde.seg
    _lugares_reales = seg._LUGARES_RUTA
    seg._LUGARES_RUTA = frozenset({"villaficticia", "hfx"})
    try:
        for q in ("paciente que busca vacuna personalizada en Villaficticia biopsia",
                  "busca ensayos de vacuna de neoantígenos en Villaficticia",
                  "busca neoantigen vaccine trials at HFX",
                  "busca v a c u n a en Villa-Ficticia",
                  "vacuna de neoantígenos para una paciente de mama",
                  "ingeniera con metástasis busca vacuna",
                  "patient seeking a personalized neoantigen vaccine",
                  "busca en X: mujer de Villaficticia con cáncer, vacuna",
                  "pаciente busca vаcuna"):                    # homoglifos cirílicos
            ok(borde.clasificar_consulta(q)[0], "persona descrita o lugar de su ruta + embargo DENY: %s" % q)
        for q in ("busca ensayos de vacunas de neoantígenos en pacientes con mama",
                  "busca supervivencia de pacientes tratadas con vacuna",
                  "busca vacunas para mujeres con {{DIAGNOSTICO}}",
                  "busca neoantigen vaccine trials in patients with breast cancer",
                  "busca ensayos en Villaficticia sobre radioterapia",   # lugar sin embargo
                  "busca vacuna y plus hfxinc",                            # corto pegado a otra palabra
                  "busca en X qué se dice de la vacuna de BioNTech"):
            ok(not borde.clasificar_consulta(q)[0], "búsqueda genérica PASA: %s" % q)
        # Segunda ronda de verificacion (22-sep-26): 32 consultas que la re-identificaban y pasaban
        # con la primera versión (sinónimos, primera persona, otros idiomas, punto en medio, separadores).
        for q in ("afectada de {{DIAGNOSTICO}} que busca vacuna personalizada",
                  "enferma de {{DIAGNOSTICO}} busca vacuna de neoantígenos",
                  "una de 41 con metástasis busca vacuna personalizada",
                  "superviviente busca vacuna personalizada",
                  "madrileña con {{DIAGNOSTICO}} busca vacuna personalizada",
                  "española con {{DIAGNOSTICO}} busca vacuna personalizada",
                  "ella busca vacuna personalizada de neoantígenos",
                  "doctora en informática con metástasis busca vacuna personalizada",
                  "ingeniero de software con {{DIAGNOSTICO}} busca vacuna",
                  "busco vacuna personalizada para {{DIAGNOSTICO}}, biopsia hepática",
                  "necesito vacuna de neoantígenos tras progresión",
                  "quiero vacuna personalizada {{DIAGNOSTICO}}",
                  "I need a personalized neoantigen vaccine for breast cancer",
                  "I'm looking for a personalized cancer vaccine after progression",
                  "looking for a neoantigen vaccine for me",
                  "she is looking for a personalized neoantigen vaccine",
                  "her personalized vaccine after liver biopsy",
                  "41 years old with metastatic breast cancer seeking neoantigen vaccine",
                  "41F mBC seeking personalized vaccine",
                  "41yo female with mBC neoantigen vaccine",
                  "mother of two with metastatic breast cancer seeking neoantigen vaccine",
                  "Patientin mit metastasiertem Brustkrebs sucht personalisierten Impfstoff",
                  "paziente con tumore al seno metastatico cerca vaccino personalizzato",
                  "patiente avec cancer du sein métastatique cherche un vaccin personnalisé",
                  "Frau mit Brustkrebs sucht Neoantigen-Impfstoff",
                  "paciente con {{DIAGNOSTICO}} y biopsia hepática. Vacuna personalizada de neoantígenos.",
                  "Ingeniera con metástasis hepáticas; vacuna de neoantígenos",
                  "patient: metastatic breast cancer, liver biopsy done! neoantigen vaccine options?",
                  "paciente con metástasis\nvacuna personalizada",
                  "ingeniera. busca vacuna personalizada",
                  "p a c i e n t e busca v a c u n a personalizada",
                  "pa-ciente busca vacuna personalizada",
                  "busca vacuna en v i l l a f i c t i c i a",
                  "busca vacuna en h.f.x", "busca vacuna en H-F-X"):
            ok(borde.clasificar_consulta(q)[0], "corpus de evasión DENY: %r" % q)
        # …y el inglés ingeniero con «patient» de modificador, o una cohorte, no es una persona.
        for q in ("patient-reported outcomes of cancer vaccines",
                  "patient selection criteria for neoantigen vaccine trials",
                  "per-patient neoantigen prediction pipelines",
                  "single-patient mRNA vaccine manufacturing timelines",
                  "patient-derived organoids to test neoantigen vaccine responses",
                  "patient population in the KEYNOTE-942 {{VACUNA2}} vaccine trial",
                  "cost per patient of neoantigen vaccine",
                  "vacuna de neoantígenos: selección de paciente y biomarcadores",
                  "cuánto cuesta una vacuna personalizada por paciente",
                  "busca vacunas en mayores de 65 años",
                  "neoantigen vaccine in patients over 65 years old",
                  "vaccine efficacy 10 m follow-up"):
            ok(not borde.clasificar_consulta(q)[0], "búsqueda ingeniera PASA: %r" % q)
        # Ronda 2 de verificacion (22-sep-26): la regresión de «she's», el embargo con otro nombre…
        for q in ("she's looking for a personalized neoantigen vaccine",
                  "she’s looking for a personalized neoantigen vaccine",
                  "paciente busca neoepítopos personalizados tras biopsia",
                  "patient seeking BNT122 after progression"):
            ok(borde.clasificar_consulta(q)[0], "ronda 2 DENY: %r" % q)
        # …y los falsos positivos de clase amplia que metió la primera corrección.
        for q in ("HER 2 vaccine trials",                      # «her» no es ella
                  "phase 3 vaccine trial with 24 M follow-up",
                  "Moderna raises 50 M for its neoantigen vaccine",
                  "quiero saber qué es una vacuna de neoantígenos",
                  "necesito entender cómo se fabrica una vacuna personalizada",
                  "busco revisiones de vacunas de neoantígenos",
                  "vaccine data from 10 years old trials"):
            ok(not borde.clasificar_consulta(q)[0], "ronda 2 PASA: %r" % q)
        # Sin la lista de lugares, el tripwire (a) estaría ciego: vuelve el embargo entero.
        seg._LUGARES_RUTA = frozenset()
        ok(borde.clasificar_consulta("busca la vacuna de BioNTech")[0],
           "sin overlay de lugares la consulta vuelve al borde estricto (fail-closed)")
    finally:
        seg._LUGARES_RUTA = _lugares_reales
    # Con el overlay real (solo en casa base): la forma de la frase que cazó `verificacion`, con cada
    # lugar real, leído del overlay en ejecución para no escribir ninguno aquí.
    for lugar in sorted(_LUGARES_OVERLAY_REAL):
        ok(borde.clasificar_consulta("busca ensayos de vacuna personalizada en %s" % lugar)[0],
           "lugar real del overlay + embargo DENY (#%d)" % sorted(_LUGARES_OVERLAY_REAL).index(lugar))
    # ── Consulta a TEMA (22-sep-26): lo que sale es la reescritura, no la frase ──────────
    # Doble determinista del reescritor: nada sale a la red en el test.
    def _doble(tema, instr=""):
        return lambda q: {"tema": tema, "instrucciones": instr}
    os.environ[borde.ENV_CONSULTA] = "1"
    try:
        q = "young mom with metastatic breast cancer seeking neoantigen vaccine"
        ok(borde.preparar_consulta(q, "grok", reescribir=_doble(
            "neoantigen vaccine for metastatic breast cancer")) ==
           (True, "neoantigen vaccine for metastatic breast cancer"),
           "a tema: sale la reescritura, no la frase")
        ok(borde.preparar_consulta("fundadora con cáncer busca vacuna, cita fuentes", "grok",
                                   reescribir=_doble("vacuna personalizada", "cita fuentes"))[1]
           == "vacuna personalizada. cita fuentes", "a tema: las instrucciones viajan aparte")
        # Cada freno bloquea: sin un tema admisible, una búsqueda sobre el caso no sale.
        ev = "ingeniera con metástasis busca vacuna personalizada"
        for tema, por in (("ingeniera con metástasis y vacuna personalizada", "sigue la persona"),
                          ("vacuna personalizada biopsia hepática", "huella que no se puede recortar"),
                          ("**Tema de búsqueda:**", "formato roto"),
                          ("No puedo acceder a esa información", "respondió en vez de reescribir"),
                          ("vacuna para cáncer de hígado", "inventa un término clínico"),
                          ("vacuna personalizada nacida 1985", "año de nacimiento")):
            ok(borde.preparar_consulta(ev, "grok", reescribir=_doble(tema)) == (False, ev),
               "a tema: freno «%s» bloquea (sin tema admisible no sale)" % por)
        # Rodaje del 22-sep: una evasión que el tripwire de hoy deja pasar, y cuya reescritura sigue
        # describiendo a la persona, NO puede caer a la original: se bloquea.
        r2 = "programadora con metástasis busca vacuna de neoantígenos"
        ok(borde.clasificar_consulta(r2)[0] is False, "precondición: el tripwire de hoy deja pasar r2")
        ok(borde.preparar_consulta(r2, "grok", reescribir=_doble(
            "ingeniera programadora con metástasis y vacuna de neoantígenos")) == (False, r2),
           "a tema: descarte por privacidad bloquea, no cae a la original")
        ok(borde.preparar_consulta("since my liver biopsy, personalized vaccine", "grok",
                                   reescribir=_doble("personalized vaccine for liver cancer"))[0] is False,
           "a tema: «liver cancer» inventado no sale")
        ok(borde._tema_admisible("{{DIAGNOSTICO}}, vacuna personalizada",
                                 "vacuna para {{DIAGNOSTICO}}")[0],
           "a tema: «{{DIAGNOSTICO}}» → «{{DIAGNOSTICO}}» no es inventar")
        # Ronda 3 de verificacion: las rutas por las que aún salía la original.
        ok(borde.preparar_consulta(r2, "grok", reescribir=lambda q: None) == (False, r2),
           "a tema: sin reescritor, la clase r2 NO sale (antes salía entera)")
        for fuga in ("responde para alguien nacida en el 85", "para una persona de Villaficticia, cofundadora",
                     "para alguien joven que vive allí"):
            ok(borde.preparar_consulta(r2, "grok", reescribir=_doble(
                "vacuna de neoantígenos para cáncer metastásico", fuga))[1]
               == "vacuna de neoantígenos para cáncer metastásico",
               "a tema: instrucciones fuera de la allow-list se tiran: %r" % fuga)
        ok(borde._instrucciones_admisibles("cita fuentes, si no puedes acceder dilo, en una frase")
           == "cita fuentes, si no puedes acceder dilo, en una frase", "a tema: allow-list conserva lo de rigor")
        nm = "ingeniera de 41 busca clínicas en Villaficticia"
        ok(borde.preparar_consulta(nm, "grok", reescribir=_doble("clínicas en Villaficticia"))[1]
           != nm, "a tema: no médica pero la describe → se reescribe (no sale tal cual)")
        ok(borde._tema_admisible("resumen de metástasis hepáticas y vacuna del 2026-09-22",
                                 "vacuna para metástasis de hígado del 22 de septiembre de 2026")[0],
           "a tema: sinónimo de órgano y fecha ISO reformateada no bloquean")
        import glob as _glob, json as _json
        trazas = [l for f in _glob.glob(os.path.join(_TMP, "borde", "reescrituras-*.jsonl"))
                  for l in open(f, encoding="utf-8")]
        ok(trazas and not any(r2 in l or "original\"" in l for l in trazas)
           and all("original_sha256" in _json.loads(l) for l in trazas),
           "a tema: la traza guarda el sello de la original, nunca el texto")
        # Ronda 4 de verificacion.
        ok(borde.preparar_consulta(r2, "grok", reescribir=_doble(r2)) == (False, r2),
           "a tema: devolver la MISMA frase personal no cuenta como reescrita")
        ok(borde.preparar_consulta("a woman from Villaficticia with metastatic breast cancer seeking TCR-T trials",
                                   "grok", reescribir=_doble(
                                       "TCR-T trials for metastatic breast cancer in Villaficticia"))[0] is False,
           "a tema: de dónde es la persona no se convierte en lugar de búsqueda")
        ok(borde._instrucciones_admisibles("cita PMID y DOI, devuelve JSON con NCT y fase, para alguien de 41")
           == "cita PMID y DOI, devuelve JSON con NCT y fase", "a tema: siglas de rigor pasan; lo libre no")
        ok(borde.preparar_consulta("ensayos de vacuna de neoantígenos, cita NCT de cada uno", "grok",
                                   reescribir=_doble("ensayos de vacuna de neoantígenos", "cita NCT de cada uno"))
           == (True, "ensayos de vacuna de neoantígenos. cita NCT de cada uno"),
           "a tema: la sigla movida a instrucciones no es objeto perdido")
        ok(borde._RE_MEDICA.search("programadora de 41 busca centros en Boston") is not None,
           "a tema: «centros» dispara la reescritura")
        # La huella con preposición se RECORTA y sale el resto (freno de verificacion).
        ok(borde.preparar_consulta(ev, "grok", reescribir=_doble(
            "vacuna personalizada tras la biopsia hepática")) == (True, "vacuna personalizada"),
           "a tema: «tras la biopsia hepática» se recorta")
        # Perder el objeto: si la original nombra a quién verificar, el tema no puede borrarlo.
        obj = "verifica el ensayo NCT01234567 de vacuna en mama con @xdevelopers"
        ok(borde.preparar_consulta(obj, "grok", reescribir=_doble("verifica el ensayo de vacuna"))
           == (False, obj), "a tema: perder NCT/@handle no sale (sin tema admisible)")
        ok(not borde._tema_admisible("vacuna de Kuvia fundada por Ana Ruiz y Luisa Soto en mama",
                                     "vacuna de Kuvia en mama")[0], "a tema: borrar los nombres objeto no vale")
        # Si el reescritor falla o no hay nada médico, es exactamente guard_cli sobre la original.
        ok(borde.preparar_consulta(ev, "grok", reescribir=lambda q: None) == (False, ev),
           "a tema: sin reescritura, veredicto de siempre")
        def _explota(q):
            raise RuntimeError("red caída")
        ok(borde.preparar_consulta(ev, "grok", reescribir=_explota) == (False, ev),
           "a tema: si el reescritor revienta, veredicto de siempre")
        precio = "X API pricing tiers junio 2026: Free, Basic, Pro"
        ok(borde.preparar_consulta(precio, "grok", reescribir=_explota) == (True, precio),
           "a tema: lo no médico ni se reescribe")
        # Lo duro de la original no se abre reescribiendo: su nombre, marcadores, genómica.
        for duro in ("busca vacuna para {{TITULAR}} {{APELLIDO}}", "busca vacuna para HER2 3+ y PIK3CA H1047R"):
            ok(not borde.preparar_consulta(duro, "grok", reescribir=_doble("vacuna personalizada"))[0],
               "a tema: lo duro no se abre reescribiendo: %r" % duro)
    finally:
        os.environ.pop(borde.ENV_CONSULTA, None)
    # Fuera del modo consulta, o hacia otro destino, no se toca nada.
    ok(borde.preparar_consulta("young mom seeking neoantigen vaccine", "grok",
                               reescribir=_doble("neoantigen vaccine"))[1]
       == "young mom seeking neoantigen vaccine", "a tema: sin modo consulta no se reescribe")
    os.environ[borde.ENV_CONSULTA] = "1"
    try:
        ok(borde.preparar_consulta("young mom seeking neoantigen vaccine", "chatgpt",
                                   reescribir=_doble("neoantigen vaccine"))[1]
           == "young mom seeking neoantigen vaccine", "a tema: solo grok y perplexity")
    finally:
        os.environ.pop(borde.ENV_CONSULTA, None)
    # La env de consulta solo la heredan los carriles de búsqueda.
    import os as _os
    _os.environ[borde.ENV_CONSULTA] = "1"
    try:
        ok(borde.guard_cli("busca la vacuna de BioNTech", "grok"), "grok hereda el modo consulta")
        ok(not borde.guard_cli("busca la vacuna de BioNTech", "chatgpt"), "chatgpt NO lo hereda")
        ok(not borde.guard_cli("busca la vacuna de BioNTech", "drive"), "drive NO lo hereda")
    finally:
        _os.environ.pop(borde.ENV_CONSULTA, None)
    red, _ = borde.de_identificar("the study (n.100) found")
    ok(red.count("(") == red.count(")"), "de_identificar no se come un ')' que no abrió: %r" % red)
    ok(not borde.egress_check("busca la vacuna de BioNTech", destino="grok").permitido,
       "egress_check por defecto sigue con el embargo")
    ok(not borde.egress_check("busca a {{TITULAR}} {{APELLIDO}}", destino="grok", consulta=True).permitido,
       "egress_check en modo consulta sigue cerrando su nombre")
    # deid no puede dejar la cola de la variante a la vista (regresión que vio verificacion).
    for v in ("TP53 c.524G>A y p.Arg175His", "c.5266dupC", "p.(Arg175His)", "c.68_69delAG"):
        red, _ = borde.de_identificar(v)
        ok(not any(t in red for t in ("G>A", "His", "dupC", "delAG")),
           "de_identificar no deja residuo: %r → %r" % (v, red))
    # Agujero cazado el 14-jul-26: la HGVS GENOMICA (g.) no se detectaba, y el formato
    # tipico de un VCF (chr17:g.7676154G>A) salia limpio por el carril ingeniero.
    ok(not borde.egress_cientifico("chr17:g.7676154G>A somatic")[0], "ingeniero + chr:g. HGVS DENY")
    ok(not borde.egress_cientifico("g.7676154G>A")[0], "ingeniero + g. HGVS DENY")
    ok(borde.egress_cientifico("FGFR1 amplification neuroendocrine breast cancer")[0], "diana FGFR1 a nivel de tema PASA")
    ok(not borde.egress_cientifico("contact contacto about results")[0], "ingeniero + tercero DENY")

    # ── 3. Air-gap de embeddings ─────────────────────────────────────────────────────────
    ok(not borde.embedding_egress_check("text-embedding-3-large", "openai").permitido,
       "embedding API externa DENY")
    ok(not borde.embedding_egress_check("bge-m3", "openai").permitido,
       "embedding local pero destino externo DENY")
    ok(borde.embedding_egress_check("bge-m3", "local:vectordb").permitido,
       "embedding bge-m3 local ALLOW")

    # ── 5. Revocación + anti-replay ──────────────────────────────────────────────────────
    ok(borde.egress_check("texto limpio", destino="nvidia", sesion="viva").permitido,
       "sesión viva ALLOW")
    borde.revocar("muerta")
    ok(not borde.egress_check("texto limpio", destino="nvidia", sesion="muerta").permitido,
       "sesión revocada DENY")
    ok(borde.revocada("muerta"), "revocada persiste (anti-replay)")

    # ── 6. Canarios ──────────────────────────────────────────────────────────────────────
    v = borde.egress_check("informe normal CANARIO-XYZZY-7788 cola", destino="nvidia")
    ok(not v.permitido and v.alarma, "canario en egress DENY + alarma")
    vd = borde.declasificar("resumen_publico", "todo bien CANARIO-XYZZY-7788", sesion="c1")
    ok(not vd.permitido and vd.alarma, "canario en declass DENY + alarma")
    # 22-sep-26: un canario de PRUEBA (BTP_CANARIOS) no puede disparar el código rojo real; uno
    # SEMBRADO en canarios.json sí. Espía de codigo_rojo.trigger: nada escala de verdad aquí.
    import codigo_rojo as _cr
    _trigger_real, disparos = _cr.trigger, []
    _cr.trigger = lambda *a, **k: disparos.append(a)
    try:
        v = borde.egress_check("x CANARIO-XYZZY-7788 y", destino="grok", escalar=True)
        ok(not v.permitido and v.alarma and not disparos,
           "canario de prueba con escalar=True: DENY + alarma, SIN código rojo")
        os.makedirs(os.path.dirname(borde.CANARIO_FILE), exist_ok=True)
        with open(borde.CANARIO_FILE, "w", encoding="utf-8") as fh:
            json.dump(["SEMBRADO-QQ-5151", "CANARIO-XYZZY-7788"], fh)
        v = borde.egress_check("x SEMBRADO-QQ-5151 y", destino="grok", escalar=True)
        ok(not v.permitido and v.alarma and len(disparos) == 1,
           "canario SEMBRADO en canarios.json con escalar=True: DENY + código rojo")
        v = borde.egress_check("x CANARIO-XYZZY-7788 y", destino="grok", escalar=True)
        ok(len(disparos) == 2, "en los dos orígenes manda el fichero: escala")
        ok(borde.origen_canario("SEMBRADO-QQ-5151") == "fichero" and borde.origen_canario("nada") is None,
           "origen_canario distingue fichero / ausente")
        ok(not borde.guard_cli("x SEMBRADO-QQ-5151 y", "grok") and len(disparos) == 3,
           "guard_cli (producción) escala el sembrado")
        # verificacion (22-sep): con uno de prueba Y uno sembrado en el mismo texto, escala SIEMPRE,
        # sin depender del orden del set (se prueba en procesos con PYTHONHASHSEED distinto).
        os.environ["BTP_CANARIOS"] = "CANARIO-XYZZY-7788:PRUEBA-ZZ-1"
        for seed in ("0", "1", "2", "3"):
            out = subprocess.run([sys.executable, "-c",
                "import sys; sys.path.insert(0, %r); import borde; "
                "print(borde.canario_y_origen('a PRUEBA-ZZ-1 b SEMBRADO-QQ-5151 c'))" % os.path.join(ROOT, "tools")],
                capture_output=True, text=True, env=dict(os.environ, PYTHONHASHSEED=seed)).stdout.strip()
            ok(out == "('SEMBRADO-QQ-5151', 'fichero')", "ambos canarios → manda el sembrado (seed %s): %s" % (seed, out))
        os.environ["BTP_CANARIOS"] = "CANARIO-XYZZY-7788"
        # Una sola lectura: si el fichero deja de leerse después, la decisión ya está tomada.
        _orig = borde._canarios_con_origen
        llamadas = []
        def _una_vez():
            llamadas.append(1)
            return _orig() if len(llamadas) == 1 else {}
        borde._canarios_con_origen = _una_vez
        try:
            borde.egress_check("x SEMBRADO-QQ-5151 y", destino="grok", escalar=True)
        finally:
            borde._canarios_con_origen = _orig
        ok(len(llamadas) == 1 and len(disparos) == 4, "una lectura por salida y el sembrado escala igual")
    finally:
        _cr.trigger = _trigger_real
        try:
            os.remove(borde.CANARIO_FILE)
        except OSError:
            pass
    ok(borde.origen_canario("CANARIO-XYZZY-7788") == "prueba", "sin fichero, el de la env es de prueba")

    # ── 0. HALT corta todo ───────────────────────────────────────────────────────────────
    halt = os.path.join(_TMP, "no_halt_a")
    open(halt, "w").write("x")
    ok(not borde.egress_check("texto limpio", destino="nvidia").permitido, "HALT → DENY todo")
    os.remove(halt)

    # ── 4. Integridad de la cadena de traza ──────────────────────────────────────────────
    _test_cadena_truncada()
    okc, det = borde.verificar_cadena()
    ok(okc, "cadena íntegra tras operar (%s)" % det)
    # alterar un evento → la cadena debe romperse
    import glob
    ledgers = sorted(glob.glob(os.path.join(_TMP, "borde", "ledger-*.jsonl")))
    if ledgers:
        lines = open(ledgers[0], encoding="utf-8").read().splitlines()
        rec = json.loads(lines[0]); rec["motivo"] = "ALTERADO"
        lines[0] = json.dumps(rec, ensure_ascii=False, sort_keys=True)
        open(ledgers[0], "w", encoding="utf-8").write("\n".join(lines) + "\n")
        okb, detb = borde.verificar_cadena()
        ok(not okb and "rota" in detb and "truncada" not in detb,
           "cadena ROTA tras alterar un evento (%s)" % detb)
        # borrar el primer evento → salto de secuencia (no es un recorte del final)
        open(ledgers[0], "w", encoding="utf-8").write("\n".join(lines[1:]) + "\n")
        okd, detd = borde.verificar_cadena()
        ok(not okd and "rota" in detd and "truncada" not in detd,
           "cadena ROTA tras borrar un evento intermedio (%s)" % detd)
    else:
        ok(False, "había ledger para alterar")

    # ── 6b. Levantar el veto PÚBLICO de «vacuna» no abre el borde de egress ─────────────
    _test_veto_publico_no_abre_borde()

    # ── 7. Resiliencia de run_agent.sh (degradación + aplazar) ───────────────────────────
    _test_run_agent()

    print("RESULTADO borde F0: %d OK, %d fallos" % (_pass, _fail))
    print("✅ F0-MÍNIMA EN VERDE" if _fail == 0 else "❌ revisar fallos")
    return _fail


def _fake_claude(script_path, behavior):
    """Escribe un 'claude' falso: lee --model y responde según `behavior` (dict modelo→json)."""
    body = (
        "#!/bin/bash\n"
        "m=''\n"
        "while [ $# -gt 0 ]; do [ \"$1\" = --model ] && { m=\"$2\"; shift 2; continue; }; shift; done\n"
        "case \"$m\" in\n"
    )
    for modelo, jsonout in behavior.items():
        body += "  %s) echo '%s' ;;\n" % (modelo, jsonout.replace("'", "'\\''"))
    body += "  *) echo '{\"is_error\":true}' ;;\nesac\nexit 0\n"
    open(script_path, "w").write(body)
    os.chmod(script_path, 0o755)


def _run_agent(behavior, modelo_pedido):
    fake = os.path.join(_TMP, "fake_claude.sh")
    _fake_claude(fake, behavior)
    env = dict(os.environ, BTP_CLAUDE_BIN=fake, BTP_API_KEY_OVERRIDE="x",
               BTP_MODEL=modelo_pedido, BTP_COST_GUARDED="1", BTP_AGENT="comite-medico")
    p = subprocess.run(["bash", os.path.join(ROOT, "tools", "run_agent.sh"), "haz X"],
                       capture_output=True, text=True, env=env)
    hb = os.path.join(ROOT, "tools", "state", "heartbeat", "comite-medico.json")
    return p.stdout, p.stderr, p.returncode


def _test_run_agent():
    """Los cuatro casos de aquí arrancan `tools/run_agent.sh`, que sale solo si hay un HALT
    («MURO: HALT activo → no arranco», run_agent.sh:18, mirando las rutas a pelo sin pasar
    por BTP_HALT_FILES). Con el lazo parado no prueban nada, así que se DICEN y se saltan —
    los otros 143 casos de esta batería siguen corriendo, que es la razón de no poner la
    guarda arriba del fichero: perderían la cobertura del muro entera por cuatro casos.
    """
    _halt = [os.path.expanduser("~/.btp.HALT"), os.path.join(ROOT, ".HALT")]
    if any(os.path.exists(h) for h in _halt):
        print("  SKIP: 4 casos de run_agent saltados — hay un HALT activo y el lazo está parado")
        return

    lim = '{\"api_error_status\": 429, \"is_error\": true, \"error\": \"overloaded\"}'
    okj = '{\"result\": \"hola\", \"is_error\": false, \"total_cost_usd\": 0.0}'
    # opus y sonnet topados, haiku responde → debe salir el JSON de haiku
    out, err, rc = _run_agent({"opus": lim, "sonnet": lim, "haiku": okj}, "opus")
    ok('"result": "hola"' in out, "run_agent degrada opus→sonnet→haiku y entrega haiku")
    ok("degrado a sonnet" in err and "degrado a haiku" in err, "run_agent logea la degradación")
    # toda la cadena topada + agente CLÍNICO (comite-medico) → 🔴 BLOQUEO crítico (no muere callado,
    # y NO degrada a un cerebro flojo): rc 75 y el stderr lo dice (freno de criticidad).
    out2, err2, rc2 = _run_agent({"opus": lim, "sonnet": lim, "haiku": lim}, "opus")
    ok(rc2 == 75 and ("BLOQUEO" in err2 or "aplazo" in err2),
       "run_agent BLOQUEA (clínico) cuando se agota la cadena por límite")
    # primer modelo responde a la primera → sin degradación
    out3, err3, rc3 = _run_agent({"sonnet": okj, "haiku": okj}, "sonnet")
    ok('"result": "hola"' in out3 and "degrado" not in err3, "run_agent sin límite no degrada")


def _test_veto_publico_no_abre_borde():
    """Deuda embargo-publico-flag-contradice-claude-md (11-sep-26). {{TITULAR}} levantó «vacuna» para
    el COPY PÚBLICO; eso vive en seguimiento._terminos_vetados_ahora(). El borde de egress a LLMs
    de terceros usa la lista ENTERA (_TERMINOS_VETADOS) y no puede heredar ese levantamiento, ni
    siquiera con el flag reveal:true: publicar una palabra y mandar su caso a un tercero son dos
    reglas distintas."""
    seg = borde.seg
    ok("vacuna" not in seg._terminos_vetados_ahora(), "copy público: «vacuna» levantada")
    ok("vacuna" in seg._TERMINOS_VETADOS, "egress: _TERMINOS_VETADOS conserva «vacuna»")
    for texto in ("ayúdame con la vacuna personalizada de este informe",
                  "vacüna fase 1", "neoantígenos del panel"):
        ok(borde.clasificar(texto)[0], "borde estricto sigue marcando: %s" % texto)
    original = seg._embargo_activo
    try:
        seg._embargo_activo = lambda: False           # como si {{TITULAR}} pusiera reveal:true
        ok(seg._terminos_vetados_ahora() == seg._VETO_DURABLE, "reveal: copy solo veta lo durable")
        ok(borde.clasificar("la vacuna personalizada")[0],
           "reveal tampoco abre el borde de egress («vacuna»)")
        ok(borde.clasificar("neoantígenos")[0], "reveal tampoco abre el borde (neoantígenos)")
        ok(borde.clasificar("{{CONTACTO}}")[0], "«{{CONTACTO}}» durable en el borde")
    finally:
        seg._embargo_activo = original


if __name__ == "__main__":
    sys.exit(main())


def test_de_identificar_tapa_terceros_de_la_denylist():
    """`borde.de_identificar` tapaba solo el nombre de {{TITULAR}}, no el de terceros.

    Salió el 20-sep-26 construyendo el set dorado del triage: el primer fichero generado
    llevaba en claro los apellidos del equipo médico. `clasificar()` e `identificador_directo()`
    ya miraban `seg._NOMBRES_DENY`; esta función no, y es la que prepara un texto para enseñarlo
    fuera. Fija también que el match es por PALABRA: "sid" no puede destrozar "considera".
    """
    import seguimiento as seg
    if not seg._NOMBRES_DENY:
        print("⏭️  SKIP: sin overlay de nombres (nombres.local.json) no hay nada que comprobar")
        return
    alguno = sorted(seg._NOMBRES_DENY, key=len, reverse=True)[0]
    red, n = borde.de_identificar(f"Escribir a {alguno} el lunes")
    assert alguno.lower() not in red.lower(), f"de_identificar deja pasar a un tercero: {red!r}"
    assert n >= 1
    intacto, _ = borde.de_identificar("considera bien esto")
    assert intacto == "considera bien esto", f"sobre-redacta por substring: {intacto!r}"
    print("  ✓ de_identificar tapa a terceros de la deny-list, y por palabra")


def test_denylist_no_se_vacia_en_un_worktree():
    """La deny-list es un overlay gitignored: en un worktree no existía y quedaba VACÍA sin
    avisar, así que cada sesión que editaba en rama corría con el muro más flojo. Ahora
    `_cargar_nombres_deny` une el overlay local con el de casa base."""
    import os
    import seguimiento as seg
    base = os.path.join(os.environ.get("BTP_REPO") or os.path.expanduser("~/claudecode"),
                        "tools", "nombres.local.json")
    if not os.path.exists(base):
        print("⏭️  SKIP: no hay overlay en casa base")
        return
    assert seg._NOMBRES_DENY, "la deny-list está vacía pese a existir el overlay de casa base"
    print(f"  ✓ deny-list cargada ({len(seg._NOMBRES_DENY)} nombres) también desde un worktree")
